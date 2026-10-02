"""Weekly mark-to-market settlement engine.

Every Saturday 00:00 IST (leader-only) this walks every OPEN ``Position``,
realises its running P&L into the wallet ledger using the EXISTING wallet
service, closes the old position and re-opens an identical fresh one at the
settlement price. Continuity is preserved — the user keeps the same side /
lots, only the entry price resets to the settlement LTP and P&L restarts
from zero.

Design guarantees (see the plan doc):
  • Purely additive — never imported by order_service / matching_engine /
    risk_enforcer, so the live trading, margin and risk paths are untouched.
  • Idempotent — a ``PositionSettlement`` row is written FIRST (unique on
    ``(batch_id, old_position_id)``); a re-run / crash-resume skips anything
    already attempted, so P&L is NEVER double-booked.
  • Duplicate-batch-proof — the ``SettlementBatch.week_key`` unique index
    means only one batch can exist per ISO week across all workers.
  • Per-position isolation — one position's failure is recorded and the
    batch keeps going for everyone else.
  • Sequential processing — wallet ``adjust`` is a read-modify-write, so we
    settle positions one at a time to avoid lost-update races on a user
    with several positions. A once-weekly job has no latency pressure.

Settlement price = last cached LTP (market is closed Saturday midnight). A
``<= 0`` / missing price means the feed has nothing valid, so that position
is SKIPPED (never settled at 0) and retried on the next run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal

from beanie import PydanticObjectId
from bson import Decimal128
from pymongo.errors import DuplicateKeyError

from app.core.exceptions import InsufficientFundsError
from app.models.position import Position, PositionStatus
from app.models.position_settlement import (
    PositionSettlement,
    PositionSettlementStatus,
    SettlementBatch,
    SettlementBatchStatus,
)
from app.models.platform_setting import PlatformSetting
from app.models.transaction import TransactionType
from app.services import market_data_service, position_service, wallet_service
from app.utils.decimal_utils import quantize_money, to_decimal
from app.utils.time_utils import (
    is_saturday_settlement_window,
    iso_week_key,
    now_utc,
)

logger = logging.getLogger(__name__)

# Admin kill-switch. ON by default — the engine runs from deploy. An admin
# can set this PlatformSetting to False to disable it without a code change.
SETTLEMENT_ENABLED_KEY = "weekly_settlement.enabled"


async def is_settlement_enabled() -> bool:
    """Read the admin kill-switch. Defaults to True (enabled) when the
    setting row is absent, so a fresh deploy is live immediately. Any read
    error is treated as enabled=True to match the documented default."""
    try:
        row = await PlatformSetting.find_one(
            PlatformSetting.setting_key == SETTLEMENT_ENABLED_KEY
        )
        if row is None or row.setting_value is None:
            return True
        val = row.setting_value
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:  # noqa: BLE001
        logger.exception("weekly_settlement_flag_read_failed_defaulting_on")
        return True


async def _settle_one_position(
    pos: Position,
    *,
    batch: SettlementBatch,
    fx_rate: Decimal,
) -> str:
    """Settle a single OPEN position. Returns one of:
    ``"settled"`` / ``"skipped"`` / ``"failed"`` / ``"duplicate"``.

    Idempotent: writes the ``PositionSettlement`` record first; if a record
    already exists for this (batch, position) it returns ``"duplicate"``
    WITHOUT touching the wallet again — this is what makes a crash-resume
    safe against double-booking.
    """
    qty_signed = float(pos.quantity or 0)
    if abs(qty_signed) < 1e-9:
        # Defensive — apply_fill flips status to CLOSED at qty 0, so an OPEN
        # row at 0 is anomalous. Skip rather than book a phantom 0-qty close.
        return "skipped"

    token = pos.instrument.token
    avg = to_decimal(pos.avg_price)

    # ── Settlement price = last cached LTP (market closed at midnight) ──
    try:
        ltp = await market_data_service.get_ltp(token)
    except Exception:  # noqa: BLE001
        ltp = Decimal(0)
    ltp = quantize_money(to_decimal(ltp))

    side = (
        pos.opened_side.value
        if getattr(pos, "opened_side", None) is not None
        else ("BUY" if qty_signed > 0 else "SELL")
    )

    # No valid price → never settle at 0. We deliberately DON'T write a
    # record here: a SKIPPED row would occupy the unique (week_key,
    # old_position_id) slot and block a later run (e.g. the Saturday job
    # after an admin's early Friday run) from settling it once a real price
    # is available. So we just skip and let a subsequent run retry.
    if ltp <= Decimal(0):
        logger.info(
            "weekly_settlement_skip_no_price pos=%s token=%s week=%s",
            pos.id,
            token,
            batch.week_key,
        )
        return "skipped"

    # ── Realized P&L (same formula the matching engine uses on a close) ──
    closed_qty = to_decimal(abs(qty_signed))
    sign = Decimal(1) if qty_signed > 0 else Decimal(-1)
    realized = (ltp - avg) * closed_qty * sign
    is_usd = market_data_service.is_usd_quoted_segment(
        pos.segment_type
    ) or market_data_service.is_usd_quoted_segment(pos.instrument.segment)
    if is_usd:
        realized = realized * fx_rate
    realized = quantize_money(realized)

    # ── Idempotency anchor: write the record FIRST (PENDING) ───────────
    try:
        record = PositionSettlement(
            batch_id=batch.id,
            week_key=batch.week_key,
            scope_admin_id=batch.scope_admin_id,
            user_id=pos.user_id,
            old_position_id=pos.id,
            symbol=pos.instrument.symbol,
            instrument_token=token,
            side=side,
            quantity=abs(qty_signed),
            previous_avg_price=Decimal128(str(avg)),
            settlement_price=Decimal128(str(ltp)),
            realized_pnl=Decimal128(str(realized)),
            fx_rate=Decimal128(str(fx_rate if is_usd else Decimal(1))),
            status=PositionSettlementStatus.PENDING,
            is_demo=bool(getattr(pos, "is_demo", False)),
        )
        await record.insert()
    except DuplicateKeyError:
        # Already attempted in a previous (crashed) run. Do NOT re-book the
        # wallet — skip to stay safe against double-counting.
        logger.warning(
            "weekly_settlement_duplicate_skipped pos=%s week=%s",
            pos.id,
            batch.week_key,
        )
        return "duplicate"

    try:
        # ── Book realized P&L into the wallet via the existing service ──
        if realized != Decimal(0):
            narration = (
                f"Weekly settlement {batch.week_key}: "
                f"{'profit' if realized > 0 else 'loss'} on {pos.instrument.symbol}"
            )
            try:
                await wallet_service.adjust(
                    pos.user_id,
                    realized,
                    transaction_type=TransactionType.PNL,
                    narration=narration,
                    reference_type="POSITION_SETTLEMENT",
                    reference_id=str(record.id),
                )
            except InsufficientFundsError:
                # Loss exceeds available balance — mirror the stop-out path:
                # debit what we can, book the overflow to settlement_outstanding.
                if realized < Decimal(0):
                    await wallet_service.force_debit(
                        pos.user_id,
                        -realized,
                        transaction_type=TransactionType.PNL,
                        narration=f"{narration} (shortfall booked to outstanding)",
                        reference_type="POSITION_SETTLEMENT",
                        reference_id=str(record.id),
                    )
                else:
                    raise

        # ── Close the old position (no margin release — margin carries) ──
        now = now_utc()
        pos.status = PositionStatus.CLOSED
        pos.closed_at = now
        pos.close_reason = "WEEKLY_SETTLEMENT"
        pos.ltp = Decimal128(str(ltp))
        # Freeze the realized slice onto the closing doc for the history view.
        pos.realized_pnl = Decimal128(
            str(quantize_money(to_decimal(pos.realized_pnl) + realized))
        )
        pos.unrealized_pnl = Decimal128("0")
        if pos.open_usd_inr_rate is not None and pos.close_usd_inr_rate is None:
            pos.close_usd_inr_rate = Decimal128(str(fx_rate))
        if pos.stop_loss is not None and pos.close_stop_loss is None:
            pos.close_stop_loss = pos.stop_loss
        if pos.target is not None and pos.close_target is None:
            pos.close_target = pos.target
        await pos.save()

        # ── Re-open an identical fresh position at the settlement price ──
        new_pos = Position(
            user_id=pos.user_id,
            instrument=pos.instrument,
            segment_type=pos.segment_type,
            product_type=pos.product_type,
            quantity=qty_signed,  # same signed qty → same side + size
            opened_side=pos.opened_side,
            opening_quantity=abs(qty_signed),
            avg_price=Decimal128(str(ltp)),
            ltp=Decimal128(str(ltp)),
            margin_used=pos.margin_used,  # carried as-is (no new block/release)
            realized_pnl=Decimal128("0"),
            unrealized_pnl=Decimal128("0"),
            stop_loss=pos.stop_loss,
            target=pos.target,
            open_usd_inr_rate=Decimal128(str(fx_rate)) if is_usd else None,
            opened_at=now,
            status=PositionStatus.OPEN,
            is_demo=bool(getattr(pos, "is_demo", False)),
        )
        await new_pos.insert()

        # Tracker is recomputed from live docs — old CLOSED + new OPEN with
        # the same qty/segment/token nets to no change, but recomputing keeps
        # it drift-immune.
        try:
            await position_service._recompute_tracker(
                user_id=pos.user_id,
                segment_type=pos.segment_type,
                token=token,
            )
        except Exception:  # noqa: BLE001
            logger.exception("weekly_settlement_tracker_recompute_failed pos=%s", pos.id)

        record.new_position_id = new_pos.id
        record.status = PositionSettlementStatus.DONE
        record.settled_at = now
        await record.save()

        # Best-effort UI refresh — never block / fail the settlement on a
        # Redis hiccup.
        try:
            from app.core.redis_client import publish

            await publish(
                f"user:{pos.user_id}:positions",
                {"type": "positions", "payload": {"reason": "weekly_settlement"}},
            )
        except Exception:  # noqa: BLE001
            pass

        return "settled"
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "weekly_settlement_position_failed pos=%s user=%s",
            pos.id,
            pos.user_id,
        )
        try:
            record.status = PositionSettlementStatus.FAILED
            record.error = str(e)[:500]
            record.settled_at = now_utc()
            await record.save()
        except Exception:  # noqa: BLE001
            pass
        return "failed"


async def run_weekly_settlement(
    *,
    week_key: str | None = None,
    scope_admin_id: PydanticObjectId | None = None,
    scope_user_ids: list[PydanticObjectId] | None = None,
    force: bool = False,
) -> dict:
    """Run (or resume) a weekly settlement batch.

    Scope:
      * ``scope_admin_id is None`` (and ``scope_user_ids is None``) → GLOBAL
        run: every OPEN position (the automatic Saturday job).
      * ``scope_admin_id`` set + ``scope_user_ids`` provided → SCOPED run:
        only that admin's user pool (a manual "Run now"). A SUPER_ADMIN /
        ADMIN settles only the users they own.

    Safe to call repeatedly and to overlap a scoped run with the global
    run: the batch is keyed by a unique ``run_key`` and each position's
    unique ``(week_key, old_position_id)`` record means a position is
    settled at most once per ISO week regardless of which run reaches it
    first. ``force`` only bypasses the enabled-flag check; it does NOT
    bypass idempotency.

    Returns a summary dict with the batch id and counts.
    """
    if not force and not await is_settlement_enabled():
        logger.info("weekly_settlement_skipped_disabled")
        return {"skipped": True, "reason": "disabled"}

    wk = week_key or iso_week_key()
    run_key = wk if scope_admin_id is None else f"{wk}:{scope_admin_id}"

    # A scoped run with an EMPTY pool has nothing to do.
    if scope_admin_id is not None and not scope_user_ids:
        logger.info("weekly_settlement_scoped_empty_pool admin=%s week=%s", scope_admin_id, wk)
        return {"skipped": True, "reason": "empty_pool", "week_key": wk, "total": 0,
                "settled": 0, "skipped": 0, "failed": 0}

    # ── Find or create the batch (unique run_key = duplicate gate) ─────
    batch = await SettlementBatch.find_one(SettlementBatch.run_key == run_key)
    if batch is not None and batch.status == SettlementBatchStatus.DONE and not force:
        logger.info("weekly_settlement_already_done run=%s", run_key)
        return {"skipped": True, "reason": "already_done", "week_key": wk,
                "batch_id": str(batch.id), "total": batch.total, "settled": batch.settled,
                "skipped": batch.skipped, "failed": batch.failed}

    fx_rate = quantize_money(to_decimal(market_data_service.get_usd_inr_rate()))

    if batch is None:
        try:
            batch = SettlementBatch(
                run_key=run_key,
                week_key=wk,
                scope_admin_id=scope_admin_id,
                status=SettlementBatchStatus.RUNNING,
                started_at=now_utc(),
                fx_rate_snapshot=Decimal128(str(fx_rate)),
            )
            await batch.insert()
        except DuplicateKeyError:
            # Another worker created it between our read and insert — load it.
            batch = await SettlementBatch.find_one(SettlementBatch.run_key == run_key)
            if batch is None:
                return {"skipped": True, "reason": "race_no_batch", "week_key": wk}
    else:
        # Resuming an interrupted batch.
        batch.status = SettlementBatchStatus.RUNNING
        await batch.save()

    logger.info(
        "weekly_settlement_started run=%s scope=%s batch=%s fx=%s",
        run_key, scope_admin_id or "global", batch.id, fx_rate,
    )

    total = settled = skipped = failed = 0
    settled_user_ids: set = set()
    try:
        # Iterate OPEN positions one at a time (sequential — see module
        # docstring on why we avoid concurrent wallet writes). Scoped runs
        # filter to the admin's user pool.
        if scope_user_ids is not None:
            from beanie.operators import In

            cursor = Position.find(
                Position.status == PositionStatus.OPEN,
                In(Position.user_id, scope_user_ids),
            )
        else:
            cursor = Position.find(Position.status == PositionStatus.OPEN)
        async for pos in cursor:
            total += 1
            outcome = await _settle_one_position(pos, batch=batch, fx_rate=fx_rate)
            if outcome == "settled":
                settled += 1
                settled_user_ids.add(pos.user_id)
            elif outcome == "skipped":
                skipped += 1
            elif outcome == "failed":
                failed += 1
            # "duplicate" → already counted in a previous run; ignore.
            # Yield to the event loop every so often so the once-weekly batch
            # never starves the rest of the process.
            if total % 50 == 0:
                await asyncio.sleep(0)

        # ── Anti-fragmentation pass ──────────────────────────────────────
        # The reopen above creates a fresh Position doc per settled position
        # and never nets same-instrument ones, so a user could accumulate the
        # same stock across many rows week over week. Fold each settled user's
        # OPEN positions back to one per (token, product). Every reopened lot
        # is at the identical settlement price, so any BUY/SELL offset nets at
        # zero P&L — this is money-neutral here. Best-effort per user: one
        # user's failure never aborts the batch.
        for _uid in settled_user_ids:
            try:
                collapsed = await position_service.consolidate_open_positions(_uid)
                if collapsed:
                    logger.info(
                        "weekly_settlement_consolidated user=%s groups=%s week=%s",
                        _uid, collapsed, wk,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("weekly_settlement_consolidate_failed user=%s", _uid)
            if len(settled_user_ids) > 50:
                await asyncio.sleep(0)

        batch.status = SettlementBatchStatus.DONE
        batch.finished_at = now_utc()
        batch.total = total
        batch.settled = settled
        batch.skipped = skipped
        batch.failed = failed
        await batch.save()
        logger.info(
            "weekly_settlement_done week=%s total=%s settled=%s skipped=%s failed=%s",
            wk,
            total,
            settled,
            skipped,
            failed,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("weekly_settlement_batch_failed week=%s", wk)
        try:
            batch.status = SettlementBatchStatus.FAILED
            batch.error = str(e)[:500]
            batch.total = total
            batch.settled = settled
            batch.skipped = skipped
            batch.failed = failed
            await batch.save()
        except Exception:  # noqa: BLE001
            pass

    return {
        "week_key": wk,
        "batch_id": str(batch.id),
        "total": total,
        "settled": settled,
        "skipped": skipped,
        "failed": failed,
    }


# ── Background loop (same pattern as intraday_to_carry_loop) ───────────
_settlement_loop_stop = False
_last_settlement_week: str | None = None


def stop_weekly_settlement_loop() -> None:
    global _settlement_loop_stop
    _settlement_loop_stop = True


async def weekly_settlement_loop(interval_sec: float = 60.0) -> None:
    """Wake every minute; when the Saturday-00:00 IST window is reached and
    this ISO week's GLOBAL run hasn't completed yet, fire it. The global run
    settles EVERY open position (it ignores admin scoping) so the whole
    platform is covered once a week; any position already settled by an
    earlier scoped "Run now" is skipped via the per-week-per-position lock.
    The unique ``run_key`` batch index makes a duplicate fire a no-op, so the
    in-process ``_last_settlement_week`` guard is just an optimisation."""
    global _settlement_loop_stop, _last_settlement_week
    _settlement_loop_stop = False

    while not _settlement_loop_stop:
        try:
            if is_saturday_settlement_window():
                wk = iso_week_key()
                if _last_settlement_week != wk and await is_settlement_enabled():
                    # Global run's run_key is the bare week_key.
                    existing = await SettlementBatch.find_one(
                        SettlementBatch.run_key == wk
                    )
                    if existing is None or existing.status != SettlementBatchStatus.DONE:
                        summary = await run_weekly_settlement(week_key=wk)
                        logger.info("weekly_settlement_loop_fired %s", summary)
                    _last_settlement_week = wk
        except Exception:  # noqa: BLE001
            logger.exception("weekly_settlement_loop_iteration_failed")
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            return
