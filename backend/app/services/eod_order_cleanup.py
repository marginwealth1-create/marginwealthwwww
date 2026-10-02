"""End-of-day auto-cancel for parked DAY orders, per segment.

WHY THIS EXISTS
---------------
A LIMIT / SL-M order the user parks sits as ``OPEN`` and the
``pending_order_poller`` only ever *fires* it when its trigger is hit —
nothing ever *expires* it. So an unfilled order carried into the NEXT
trading day and fired on the next day's prices: the operator-reported
"pending order dusre din carry forward ho jaata hai" bug.

WHEN IT SWEEPS
--------------
Each settings row (Segment Settings) carries its own **pending order expiry
time** in IST, because a forex session ends nowhere near an NSE one. At that
time the row is swept once per IST day:

  • unfilled LIMIT / SL-M orders on its instruments are marked ``EXPIRED``
    and their blocked margin released;
  • positions carrying overnight lose their SL / TP (operator decision —
    brackets are day-scoped too). A carried position is therefore
    unprotected until the user sets them again.

A row with **no time set is never swept**, so its orders carry exactly as
they did before this was configurable. ``00:00`` reproduces the original
midnight sweep.

AMO HANDLING
------------
An AMO is placed in the evening FOR THE NEXT session, so it must survive the
sweep that immediately follows its placement. It is given exactly one
session: expired only once it is older than the START of the previous IST
day.

SAFETY / PLACEMENT
------------------
Mongo-only (no in-process price state), so it is safe on ANY worker and runs
under its OWN ``leader:eod_order_cleanup`` lock (NOT the ``leader:feed``
gate). Idempotent — a second run finds nothing left to expire.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as _dtime, timedelta, timezone

from app.models._base import OrderType
from app.models.order import Order, OrderStatus
from app.services import wallet_service
from app.utils.decimal_utils import to_decimal
from app.utils.time_utils import now_ist, now_utc, start_of_day_ist, to_utc

logger = logging.getLogger(__name__)

_UTC = timezone.utc

# Module-level kill switch + per-day run guard (same pattern as the
# intraday→carry rollover loop). `_last_run_day` is the IST YYYYMMDD the
# sweep last completed, so we fire exactly once per calendar day.
_stop = False
# Per settings row: the IST YYYYMMDD its cutoff last swept on.
_last_run_day: dict[str, str] = {}


def stop_eod_order_cleanup() -> None:
    global _stop
    _stop = True


def parse_cutoff(value: str | None) -> _dtime | None:
    """"HH:MM" (IST) → a time, or None when unset or unparseable.

    None means "this segment never expires parked orders" — the feature is
    off until an admin types a time.
    """
    s = (value or "").strip()
    if not s:
        return None
    try:
        hh, mm = s.split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return _dtime(hour=h, minute=m)


def cutoff_reached(
    cutoff: _dtime | None, now: datetime, last_run_day: str | None
) -> bool:
    """Is this segment due for its once-a-day sweep?

    `00:00` behaves exactly like the old midnight sweep: the first tick of a
    new IST day is already past it.
    """
    if cutoff is None:
        return False
    if last_run_day == now.strftime("%Y%m%d"):
        return False
    return now.time() >= cutoff


def admin_row_for_segment(segment: str | None) -> str | None:
    """Instrument segment (NSE_FUTURE, FOREX…) → the settings row that owns
    it. Unmapped segments own themselves, which is how FOREX / CRYPTO /
    STOCKS already resolve."""
    if not segment:
        return None
    from app.services.netting_service import _SEGMENT_NAME_MAP

    seg = str(segment).upper()
    return _SEGMENT_NAME_MAP.get(seg, seg)


def apply_cutoff_override(
    out: dict[str, _dtime], segment_name: str, raw: str | None
) -> None:
    """Layer one override row's value over the seed cutoffs, in place.

    `None` is the cascade's "inherit" everywhere else in netting, so it
    means inherit here too: the seed's cutoff stands. A value that is set
    but unparseable ("" can't reach us from the panel, but a hand-edited
    doc can) turns the sweep OFF for that row.
    """
    if raw is None:
        return
    name = str(segment_name).upper()
    cut = parse_cutoff(raw)
    if cut is None:
        out.pop(name, None)
    else:
        out[name] = cut


async def segment_cutoffs() -> dict[str, _dtime]:
    """Configured cutoff per settings row. Only rows with a time appear.

    The panel does NOT write the `NettingSegment` seeds — a super-admin's
    edit lands in `SuperAdminSegmentOverride` (the seeds are immutable
    defaults). Reading only the seeds would mean a time typed into Segment
    Settings silently never sweeps.

    ponytail: the cutoff is platform-wide, so sub-admin / broker overrides
    of this one field are ignored — honouring them means grouping every
    parked order by its owner's pool. Split it if a broker ever needs a
    session end of their own.
    """
    from app.models.netting import NettingSegment, SuperAdminSegmentOverride

    out: dict[str, _dtime] = {}
    try:
        for seg in await NettingSegment.find_all().to_list():
            cut = parse_cutoff(getattr(seg, "pendingOrderExpiryTime", None))
            if cut is not None:
                out[str(seg.name).upper()] = cut
        for over in await SuperAdminSegmentOverride.find_all().to_list():
            apply_cutoff_override(
                out,
                over.segment_name,
                getattr(over, "pendingOrderExpiryTime", None),
            )
    except Exception:
        logger.exception("eod_segment_cutoffs_failed")
    return out


async def clear_carry_brackets(seg_names: set[str]) -> int:
    """Wipe SL / TP off positions carrying overnight in these segments.

    Operator decision: brackets are day-scoped like the orders, so a carried
    position starts the next session without them. NOTE this leaves the
    position unprotected overnight — it is the reason the setting is per
    segment rather than global.
    """
    from app.models.position import Position, PositionStatus

    cleared = 0
    try:
        rows = await Position.find(
            {
                "status": PositionStatus.OPEN.value,
                "$or": [
                    {"stop_loss": {"$nin": [None, 0]}},
                    {"target": {"$nin": [None, 0]}},
                ],
            }
        ).to_list()
    except Exception:
        logger.exception("eod_bracket_scan_failed")
        return 0

    for pos in rows:
        try:
            if admin_row_for_segment(getattr(pos, "segment_type", None)) not in seg_names:
                continue
            pos.stop_loss = None
            pos.target = None
            await pos.save()
            cleared += 1
            logger.info(
                "eod_carry_brackets_cleared",
                extra={
                    "position_id": str(pos.id),
                    "user_id": str(pos.user_id),
                    "symbol": pos.instrument.symbol,
                },
            )
        except Exception:
            logger.exception("eod_bracket_clear_failed", extra={"position_id": str(pos.id)})
    return cleared


async def expire_stale_day_orders() -> dict[str, int]:
    """One EOD sweep. Expires still-parked NSE/MCX LIMIT/SL-M orders whose
    session is over, releasing their blocked margin. Never raises — one bad
    order must not stop the rest. Returns ``{scanned, expired}``.
    """
    global _last_run_day

    now = now_ist()
    cutoffs = await segment_cutoffs()
    due = {
        name
        for name, cut in cutoffs.items()
        if cutoff_reached(cut, now, _last_run_day.get(name))
    }
    if not due:
        return {"scanned": 0, "expired": 0, "brackets_cleared": 0}

    try:
        rows = await Order.find(
            {
                "status": {"$in": [OrderStatus.OPEN.value, OrderStatus.PARTIAL.value]},
                "order_type": {"$in": [OrderType.LIMIT.value, OrderType.SL_M.value]},
            }
        ).to_list()
    except Exception:
        logger.exception("eod_order_scan_failed")
        return {"scanned": 0, "expired": 0}

    if not rows:
        return {"scanned": 0, "expired": 0}

    today_start_utc = to_utc(start_of_day_ist(now.date()))
    # AMO gets one full session — only expired once older than the START of
    # the PREVIOUS IST day (it already had its session and didn't fill).
    amo_cutoff_utc = to_utc(start_of_day_ist(now.date() - timedelta(days=1)))

    expired = 0
    affected_users: set[str] = set()

    for o in rows:
        try:
            seg = getattr(o.instrument, "segment", None)
            if admin_row_for_segment(str(seg) if seg else None) not in due:
                continue

            created = o.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=_UTC)
            cutoff = amo_cutoff_utc if o.is_amo else today_start_utc
            if created is None or created >= cutoff:
                continue

            o.status = OrderStatus.EXPIRED
            o.cancelled_at = now_utc()
            o.rejection_reason = "DAY order expired at end of day (auto-cancelled)"
            o.rejection_code = "DAY_ORDER_EOD_EXPIRED"
            o.pending_quantity = 0
            await o.save()

            margin = to_decimal(o.margin_blocked)
            if margin > 0:
                try:
                    await wallet_service.release_margin(o.user_id, margin)
                except Exception:
                    logger.exception(
                        "eod_order_margin_release_failed",
                        extra={"order_id": str(o.id), "user_id": str(o.user_id)},
                    )

            expired += 1
            affected_users.add(str(o.user_id))
            logger.info(
                "eod_order_expired",
                extra={
                    "order_id": str(o.id),
                    "user_id": str(o.user_id),
                    "symbol": o.instrument.symbol,
                    "segment": str(seg),
                    "order_type": o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type),
                    "is_amo": bool(o.is_amo),
                },
            )
        except Exception:
            logger.exception(
                "eod_order_expire_failed",
                extra={"order_id": str(getattr(o, "id", None))},
            )

    # Refresh the admin dashboard + affected users' Orders/wallet views so
    # the cancellations show without an F5. Fire-and-forget — pure WS pings,
    # they must never fail the sweep.
    if affected_users:
        try:
            from app.services.admin_events import publish_admin_event
            from app.utils.background import fire_and_forget

            for uid in affected_users:
                fire_and_forget(
                    publish_admin_event(
                        "order_update",
                        {"event": "eod_expired", "user_id": uid},
                    ),
                    label="eod_order_update",
                )
                fire_and_forget(
                    publish_admin_event("wallet_update", {"user_id": uid}),
                    label="eod_wallet_update",
                )
        except Exception:
            logger.exception("eod_order_admin_event_failed")

    # Brackets on positions carrying overnight go the same way as the parked
    # orders — operator decision; see clear_carry_brackets.
    brackets = await clear_carry_brackets(due)

    day_key = now.strftime("%Y%m%d")
    for name in due:
        _last_run_day[name] = day_key

    return {
        "scanned": len(rows),
        "expired": expired,
        "brackets_cleared": brackets,
        "segments": ",".join(sorted(due)),
    }


async def eod_order_cleanup_loop(interval_sec: float = 60.0) -> None:
    """Wake every minute and sweep any segment whose cutoff has arrived.

    Each settings row carries its own "pending order expiry time" (IST),
    because a forex session ends nowhere near an NSE one. A row with no time
    set is never swept — its parked orders carry, as they did before this
    was configurable. Each row sweeps once per IST day.
    """
    global _stop
    _stop = False
    logger.info("eod_order_cleanup_started", extra={"interval_sec": interval_sec})
    while not _stop:
        try:
            summary = await expire_stale_day_orders()
            if summary.get("expired") or summary.get("brackets_cleared"):
                logger.info("eod_order_cleanup_swept", extra=summary)
        except Exception:
            logger.exception("eod_order_cleanup_loop_failed")
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            return
