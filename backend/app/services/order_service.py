"""Order service — accepts a request, runs the validator, blocks margin,
persists the order document, and (for MARKET) executes immediately."""

from __future__ import annotations

import logging
import secrets
from decimal import Decimal
from typing import Any

from beanie import PydanticObjectId
from bson import Decimal128

from app.core.exceptions import (
    AppError,
    InsufficientFundsError,
    NotFoundError,
    OrderRejectedError,
    ValidationFailedError,
)
from app.models._base import (
    OrderAction,
    OrderType,
    ProductType,
    Validity,
)
from app.models.order import (
    AppliedSettings,
    InstrumentRef,
    Order,
    OrderStatus,
)
from app.models.user import User
from app.services import (
    instrument_service,
    matching_engine,
    order_validator,
    wallet_service,
)
from app.utils.decimal_utils import to_decimal
from app.utils.time_utils import now_utc

logger = logging.getLogger(__name__)


def _order_number() -> str:
    return f"O{now_utc().strftime('%y%m%d')}{secrets.token_hex(4).upper()}"


async def place_order(
    *,
    user: User,
    payload: dict[str, Any],
) -> Order:
    """Place a new order. Validates, blocks margin, persists, and (for MARKET) executes.

    Each major step is timed and the breakdown logged at INFO so we can see
    where latency comes from in production logs without attaching a profiler.
    Format: `order_perf step=<name> ms=<float>` — easy to grep + plot.
    """
    import time as _time

    t_start = _time.perf_counter()

    def _mark(name: str, since: float) -> float:
        elapsed_ms = (_time.perf_counter() - since) * 1000
        logger.info("order_perf step=%s ms=%.1f", name, elapsed_ms)
        return _time.perf_counter()

    # Inputs
    token = str(payload.get("token") or "").strip()
    if not token:
        raise ValidationFailedError("instrument token is required")

    t = _time.perf_counter()
    instrument = await instrument_service.get_by_token(token)
    t = _mark("get_instrument", t)
    if not instrument.is_tradable or instrument.is_halted or not instrument.is_active:
        raise ValidationFailedError("Instrument is not tradable")

    action = OrderAction(payload["action"])
    order_type = OrderType(payload["order_type"])
    product_type = ProductType(payload["product_type"])
    validity = Validity(payload.get("validity") or "DAY")
    lots = float(payload.get("lots") or 1)  # fractional for crypto/forex
    # For Infoway-quoted instruments (forex / metals / energy / indices /
    # stocks / crypto) the retail-CFD contract size table gives the right
    # `quantity = lots × contract_size`. Forex majors are 100,000 base
    # units / lot, spot gold is 100 troy oz / lot, USOIL is 1,000
    # barrels / lot — getting this right is the difference between a
    # ₹1,000 margin lock and a ₹10,000,000 one.
    # Use the dedicated lot-segment classifier rather than `is_usd_quoted_segment`.
    # The latter now always returns False (FX conversion disabled per broker
    # spec); for lot-table selection we still need to know whether the row
    # came from the Infoway feed, which is what `is_infoway_lot_segment`
    # answers.
    from app.services.market_data_service import is_infoway_lot_segment

    if is_infoway_lot_segment(instrument.segment):
        from app.services.infoway_lots import get_infoway_lot_size

        canonical_infoway = get_infoway_lot_size(
            instrument.symbol, instrument.segment
        )
        stored = max(1, int(instrument.lot_size or 1))
        lot_size = canonical_infoway or stored
        # Heal stored row inline if it disagrees so positions/segment-
        # settings responses surface the same number.
        if canonical_infoway and int(instrument.lot_size or 0) != canonical_infoway:
            instrument.lot_size = canonical_infoway
            try:
                await instrument.save()
            except Exception:
                pass
    else:
        # For Indian index F&O the canonical exchange lot (NIFTY=75,
        # BANKNIFTY=35, SENSEX=20…) wins over whatever's stored on the
        # Instrument row. The DB value can be stale (auto-created from a
        # half-warm Zerodha CSV cache → 1) or outdated (old NIFTY=50
        # contracts) — using it would silently undercount quantity and
        # break the user's position size. The canonical helper is the
        # single source of truth here, AND we persist the corrected value
        # back to the Instrument row so segment-settings / positions
        # responses see the same number without depending on the startup
        # backfill having run.
        from app.models._base import InstrumentType
        from app.services.index_lots import get_canonical_lot_size

        is_fno = instrument.instrument_type in (
            InstrumentType.CE,
            InstrumentType.PE,
            InstrumentType.FUT,
        )
        if is_fno:
            ex_val = (
                instrument.exchange.value
                if hasattr(instrument.exchange, "value")
                else str(instrument.exchange)
            )
            stored_lot = max(1, int(instrument.lot_size or 1))
            # Source of truth depends on exchange:
            #   • MCX → canonical MCX_LOT_SIZES table.
            #   • NSE / BSE F&O → live Zerodha CSV. We look up by token
            #     in the in-memory instruments cache (refreshed on every
            #     boot) so a stale DB row can't trade against the wrong
            #     lot the day after an exchange revision.
            authoritative_lot: int | None = None
            if ex_val == "MCX":
                authoritative_lot = get_canonical_lot_size(
                    instrument.symbol,
                    instrument.name,
                    exchange=ex_val,
                    instrument_type=instrument.instrument_type.value,
                )
            else:
                from app.services.zerodha_service import zerodha as _zerodha

                kite_ex = {"NSE": "NFO", "BSE": "BFO"}.get(ex_val, ex_val)
                csv_cache = _zerodha._instruments_cache.get(kite_ex, [])
                try:
                    tok_int = int(instrument.token)
                except (TypeError, ValueError):
                    tok_int = None
                if tok_int is not None and csv_cache:
                    match = next(
                        (r for r in csv_cache if int(r.get("token") or 0) == tok_int),
                        None,
                    )
                    if match is not None:
                        csv_lot = int(match.get("lotSize") or 0)
                        if csv_lot > 0:
                            authoritative_lot = csv_lot
            lot_size = authoritative_lot or stored_lot
            # Heal stored row inline (idempotent) so subsequent reads —
            # /instruments/{token}, /segment-settings/effective, position
            # enrichment — return the same lot without waiting for the
            # next boot-time backfill.
            if authoritative_lot and int(instrument.lot_size or 0) != authoritative_lot:
                instrument.lot_size = authoritative_lot
                try:
                    await instrument.save()
                except Exception:
                    pass
        else:
            # Equity / index spot — always 1 share = 1 lot. If a stale
            # row stored lot_size > 1 (e.g. from a bad ETF marketlot
            # import), heal it inline so order math agrees with display.
            lot_size = 1
            if int(instrument.lot_size or 0) != 1:
                instrument.lot_size = 1
                try:
                    await instrument.save()
                except Exception:
                    pass

    # Squareoff override: when the caller (manual close / risk-auto-flatten /
    # SL-TP trigger) sends an explicit `force_quantity`, that wins over the
    # lots × lot_size math. Without this, closing a legacy position whose
    # stored quantity is `lots × 1 = 1` against the new canonical lot (75)
    # would try to SELL 75 of a 1-qty position — leaving a phantom -74
    # short, or rejecting outright. We always want squareoff to flatten
    # exactly what's open.
    force_qty_raw = payload.get("force_quantity")
    if force_qty_raw is not None:
        try:
            force_quantity = float(force_qty_raw)
        except (TypeError, ValueError):
            force_quantity = 0.0
    else:
        force_quantity = 0.0

    if force_quantity > 0:
        quantity = force_quantity
        # Recompute `lots` so downstream brokerage / margin math uses a
        # consistent pair (lots × lot_size ≈ quantity). Falls back to
        # quantity when lot_size is 1 to avoid divide-by-anything weirdness.
        lots = quantity / lot_size if lot_size > 0 else quantity
    else:
        quantity = lots * lot_size
    logger.info(
        "order_lot_resolved symbol=%s instrument_type=%s segment=%s stored_lot=%s resolved_lot=%s lots=%s qty=%s",
        instrument.symbol,
        getattr(instrument.instrument_type, "value", instrument.instrument_type),
        instrument.segment,
        instrument.lot_size,
        lot_size,
        lots,
        quantity,
    )
    price = to_decimal(payload.get("price") or 0)
    trigger = to_decimal(payload.get("trigger_price") or 0)
    is_amo = bool(payload.get("is_amo") or False)
    is_squareoff = bool(payload.get("is_squareoff") or False)

    # ── Same-instrument product netting (one product book per stock) ──
    # A user must hold at most ONE product book per instrument. If an OPEN
    # position already exists for this (user, token) — e.g. a lot that was
    # auto-carried MIS→NRML at yesterday's EOD (`convert_intraday_to_carry`)
    # — a fresh order on the same stock must ADOPT that position's
    # product_type so it NETS into the existing position (weighted-avg)
    # instead of opening a second row under a different product. Netting
    # keys on (user, token, product_type), so without this the same stock
    # shows twice (one MIS, one NRML). Margin then resolves against the
    # adopted (carry) product — the safe/conservative side.
    #
    # Skipped for squareoff / close orders (those already target a specific
    # position's product_type) and when the token has MULTIPLE distinct open
    # product types (legacy fragmentation — don't guess which to adopt;
    # leave those for the merge_fragmented_positions script).
    if not is_squareoff:
        from app.models.position import Position, PositionStatus

        _open_here = await Position.find(
            Position.user_id == user.id,
            Position.instrument.token == token,
            Position.status == PositionStatus.OPEN,
        ).to_list()
        _distinct_pt = {p.product_type for p in _open_here}
        if len(_distinct_pt) == 1:
            _existing_pt = next(iter(_distinct_pt))
            if _existing_pt != product_type:
                logger.info(
                    "order_product_type_adopted_from_open_position "
                    "token=%s requested=%s adopted=%s",
                    token,
                    product_type.value,
                    _existing_pt.value,
                )
                product_type = _existing_pt

    # Optional system close tag (risk_enforcer SL/TP/stop-out, admin force-
    # close) — stamped on the Order so the Orders monitor can show WHY it
    # closed. None for ordinary opens; order_reason_code derives those.
    close_reason = payload.get("close_reason") or None
    if close_reason:
        close_reason = str(close_reason).upper()
    segment_type = instrument.segment

    # Optional bracket-order legs (auto SL + target after entry fills)
    raw_sl = payload.get("stop_loss")
    raw_tp = payload.get("target")
    bracket_sl = to_decimal(raw_sl) if raw_sl not in (None, "", 0) else None
    bracket_tp = to_decimal(raw_tp) if raw_tp not in (None, "", 0) else None

    # Client-supplied bid/ask snapshot (see schemas.PlaceOrderRequest). Used
    # by the validator's margin calc and by the matching engine's fill so
    # both agree on a single price for this trade.
    expected_raw = payload.get("expected_price")
    expected_price = (
        to_decimal(expected_raw) if expected_raw not in (None, "", 0, 0.0) else None
    )

    # ── Wrong-side bracket guard (shared with the SL/TP endpoints) ────────
    # An SL/TP leg on the profitable side of the market is "already hit": the
    # risk enforcer fires it instantly and it fills at EXACTLY the (untraded)
    # leg price → fake P&L booked to the wallet. Reject an impossible leg at
    # write time. ref = client's live snapshot, else the live LTP; fails open
    # when neither exists.
    if (bracket_sl is not None or bracket_tp is not None) and not is_squareoff:
        _bref = expected_price
        if _bref is None:
            try:
                from app.services import market_data_service as _mds

                _bref = await _mds.get_ltp(token)
            except Exception:
                _bref = None
        _berr = order_validator.bracket_direction_error(
            action, _bref, sl=bracket_sl, tp=bracket_tp
        )
        if _berr:
            raise OrderRejectedError(_berr, code="BRACKET_WRONG_SIDE")

    # Operator-forced fill price (admin Market-Watch "Manual" order). When
    # present the matching engine fills at EXACTLY this price, bypassing the
    # slippage cap — see matching_engine.execute_market_order. Admin-only
    # path (set on the server in admin/marketwatch.py), never user-supplied.
    force_fill_raw = payload.get("force_fill_price")
    force_fill_price = (
        to_decimal(force_fill_raw) if force_fill_raw not in (None, "", 0, 0.0) else None
    )

    # Specific-lot close basis — the tapped fill's entry price. Set only by the
    # Active-tab per-fill Exit so its realized P&L books against that exact
    # entry, not the position average. None everywhere else (avg-price stays).
    cbo_raw = payload.get("cost_basis_override")
    cost_basis_override = (
        to_decimal(cbo_raw) if cbo_raw not in (None, "", 0, 0.0) else None
    )

    # Validate. When validation fails we still want a paper trail —
    # the admin Orders monitor's "Rejected Orders" tab is empty by
    # design without this, because the validator throws BEFORE any
    # Order doc is created. Operator-flagged 21-May: "rejected ka
    # reason ke sath aaye kyu nahi aa raha". Persist a stub Order
    # with status=REJECTED + the exception's code/message so the tab
    # has rows to show, then re-raise so the API surface still returns
    # the same 400 + machine-readable code the client expects.
    try:
        validated = await order_validator.validate(
            user=user,
            instrument=instrument,
            segment_type=segment_type,
            action=action,
            order_type=order_type,
            product_type=product_type,
            lots=lots,
            quantity=quantity,
            price=price,
            trigger_price=trigger,
            is_amo=is_amo,
            is_squareoff=is_squareoff,
            placed_from=str(payload.get("placed_from") or "WEB"),
            expected_price=expected_price,
            bracket_sl=bracket_sl,
            bracket_tp=bracket_tp,
        )
    except (OrderRejectedError, InsufficientFundsError, ValidationFailedError) as ve:
        # Best-effort persistence — never let the audit row blow up the
        # original 400 response the caller is waiting for.
        try:
            instr_ref = InstrumentRef(
                token=instrument.token,
                symbol=instrument.symbol,
                trading_symbol=instrument.trading_symbol,
                exchange=instrument.exchange,
                segment=instrument.segment,
                lot_size=lot_size,
                tick_size=instrument.tick_size,
            )
            rejected = Order(
                order_number=_order_number(),
                user_id=user.id,  # type: ignore[arg-type]
                instrument=instr_ref,
                action=action,
                order_type=order_type,
                product_type=product_type,
                validity=validity,
                lots=lots,
                quantity=quantity,
                pending_quantity=0,
                price=Decimal128(str(price)),
                trigger_price=Decimal128(str(trigger)),
                margin_blocked=Decimal128("0"),
                status=OrderStatus.REJECTED,
                rejection_reason=str(ve.message),
                rejection_code=str(getattr(ve, "code", None) or "ORDER_REJECTED"),
                is_amo=is_amo,
                is_squareoff=is_squareoff,
                is_demo=bool(getattr(user, "is_demo", False)),
                placed_by=user.id,  # type: ignore[arg-type]
                placed_from=str(payload.get("placed_from") or "WEB"),
                bracket_stop_loss=Decimal128(str(bracket_sl)) if bracket_sl is not None else None,
                bracket_target=Decimal128(str(bracket_tp)) if bracket_tp is not None else None,
            )
            await rejected.insert()
            logger.info(
                "order_rejected_persisted user=%s symbol=%s code=%s",
                user.id,
                instrument.symbol,
                rejected.rejection_code,
            )
        except Exception:
            logger.exception("order_rejected_persist_failed user=%s symbol=%s", user.id, instrument.symbol)
        raise
    t = _mark("validate", t)

    # Block margin for any order that OPENS / increases exposure — a new
    # long (BUY) OR a new short (SELL-to-open, e.g. option writing / short
    # futures). The validator already returns margin_required = 0 for
    # reducing / closing / squareoff legs, so gating purely on `margin > 0`
    # locks opens of EITHER side while leaving closes untouched.
    #
    # The earlier `action == OrderAction.BUY` gate NEVER locked margin on a
    # SELL-to-open, so available_balance was never decremented on shorts —
    # every subsequent short's affordability check saw the full balance and
    # passed, letting a user open unlimited short lots with no funds. The
    # wallet only went negative LATER when the periodic used_margin reconcile
    # summed the positions' margin_used. Locking here (via the atomic
    # block_margin) makes the affordability check actually gate short opens.
    margin = validated.margin_required
    if margin > 0:
        await wallet_service.block_margin(user.id, margin)  # type: ignore[arg-type]
        t = _mark("block_margin", t)

    # Persist
    instr_ref = InstrumentRef(
        token=instrument.token,
        symbol=instrument.symbol,
        trading_symbol=instrument.trading_symbol,
        exchange=instrument.exchange,
        segment=instrument.segment,
        lot_size=lot_size,  # canonical (resolved above) — never the raw DB value
        tick_size=instrument.tick_size,
    )
    applied = AppliedSettings(**validated.settings)

    # LIMIT "touch" direction — capture which side the limit sits on
    # RELATIVE to the market at placement, so the poller only fires once
    # the price actually REACHES the level (not immediately for a
    # "marketable" limit). True = limit above market → wait for price to
    # rise to it; False = limit below market → wait for price to fall to
    # it. Independent of BUY / SELL: a limit is a resting price-touch
    # order per operator spec. None for non-LIMIT / no-LTP (poller then
    # uses the legacy action-based rule).
    _limit_wait_up: bool | None = None
    if order_type == OrderType.LIMIT and price and price > 0:
        _ref_ltp = validated.ltp
        if _ref_ltp is not None and _ref_ltp > 0:
            _limit_wait_up = price >= _ref_ltp

    order = Order(
        order_number=_order_number(),
        user_id=user.id,  # type: ignore[arg-type]
        instrument=instr_ref,
        action=action,
        order_type=order_type,
        product_type=product_type,
        validity=validity,
        lots=lots,
        quantity=quantity,
        pending_quantity=quantity,
        price=Decimal128(str(price)),
        trigger_price=Decimal128(str(trigger)),
        limit_wait_up=_limit_wait_up,
        margin_blocked=Decimal128(str(margin)),
        status=OrderStatus.PENDING,
        is_amo=is_amo,
        is_squareoff=is_squareoff,
        is_demo=bool(getattr(user, "is_demo", False)),
        close_reason=close_reason,
        applied_settings=applied,
        placed_by=user.id,  # type: ignore[arg-type]
        placed_from=str(payload.get("placed_from") or "WEB"),
        bracket_stop_loss=Decimal128(str(bracket_sl)) if bracket_sl is not None else None,
        bracket_target=Decimal128(str(bracket_tp)) if bracket_tp is not None else None,
        cost_basis_override=(
            Decimal128(str(cost_basis_override)) if cost_basis_override is not None else None
        ),
        sl_tp_source_trade_id=(
            PydanticObjectId(str(payload["sl_tp_source_trade_id"]))
            if payload.get("sl_tp_source_trade_id")
            else None
        ),
        oco_group_id=(str(payload.get("oco_group_id")) if payload.get("oco_group_id") else None),
    )
    await order.insert()
    t = _mark("insert_order", t)

    # Execute or park
    if order_type == OrderType.MARKET and not is_amo:
        try:
            await matching_engine.execute_market_order(
                order,
                cached_ltp=validated.ltp,
                cached_netting=validated.netting_settings,
                expected_price=expected_price,
                force_fill_price=force_fill_price,
            )
        except (OrderRejectedError, InsufficientFundsError, ValidationFailedError) as _exec_err:
            # Matching engine failed AFTER the Order was already persisted as
            # PENDING. Mark it REJECTED so the Orders tab shows a reason and
            # the order doesn't stay permanently stuck in PENDING state.
            try:
                order.status = OrderStatus.REJECTED
                order.rejection_reason = str(_exec_err.message)
                order.rejection_code = str(getattr(_exec_err, "code", None) or "EXECUTION_FAILED")
                order.pending_quantity = 0
                await order.save()
            except Exception:
                logger.exception("order_exec_failed_status_update_failed order=%s", order.id)
            # Release margin if it was blocked for this order
            if margin > 0:
                try:
                    await wallet_service.release_margin(user.id, margin)  # type: ignore[arg-type]
                except Exception:
                    logger.exception("order_exec_failed_unblock_margin_failed order=%s", order.id)
            raise
        _mark("execute_market", t)
    else:
        order.status = OrderStatus.OPEN
        await order.save()
        _mark("park_pending", t)

    total_ms = (_time.perf_counter() - t_start) * 1000
    logger.info(
        "order_perf step=TOTAL ms=%.1f action=%s symbol=%s qty=%s",
        total_ms, action.value, instrument.symbol, quantity,
    )

    # Fan out to the admin dashboard so Orders + Positions tabs refresh
    # without F5 the moment any user places a trade. These are pure WS
    # side-effects — FIRE-AND-FORGET so the trader's place-order response
    # isn't gated on 1-3 sequential Redis publishes (each ~tens of ms when
    # Redis is warm, more when it's not). The admin dashboard still updates
    # a few ms later; the user's fill is already returned.
    # Wrapped so a missing/broken background helper can NEVER fail the
    # order placement itself — these are dashboard-refresh pings only.
    try:
        from app.services.admin_events import publish_admin_event
        from app.utils.background import fire_and_forget

        fire_and_forget(
            publish_admin_event(
                "order_update",
                {
                    "event": "placed",
                    "user_id": str(user.id),
                    "order_id": str(order.id),
                    "status": order.status.value,
                },
            ),
            label="order_update",
        )
        # An immediate-market order also produces fills, which mutate
        # positions + wallet — surface those so the admin's Positions and
        # P&L cards update in the same tick as the Orders table.
        if order_type == OrderType.MARKET and not is_amo:
            fire_and_forget(
                publish_admin_event(
                    "position_update",
                    {"event": "fill", "user_id": str(user.id), "order_id": str(order.id)},
                ),
                label="position_update",
            )
            fire_and_forget(
                publish_admin_event(
                    "wallet_update",
                    {"user_id": str(user.id)},
                ),
                label="wallet_update",
            )
    except Exception:
        logger.exception("admin_event_publish_schedule_failed")

    return order


async def cancel(user_id: PydanticObjectId, order_id: str) -> Order:
    o = await Order.get(PydanticObjectId(order_id))
    if o is None or o.user_id != user_id:
        raise NotFoundError("Order not found")
    return await matching_engine.cancel_order(o, reason="USER_CANCELLED")


async def admin_force_cancel(order_id: str, *, reason: str = "ADMIN_FORCE_CANCEL") -> Order:
    o = await Order.get(PydanticObjectId(order_id))
    if o is None:
        raise NotFoundError("Order not found")
    return await matching_engine.cancel_order(o, reason=reason)


async def list_for_user(
    user_id: PydanticObjectId, *, status: str | None = None, limit: int = 100, skip: int = 0
) -> list[Order]:
    q: dict[str, Any] = {"user_id": user_id}
    if status:
        q["status"] = status
    return (
        await Order.find(q).sort("-created_at").skip(skip).limit(limit).to_list()
    )


async def list_all(*, status: str | None = None, limit: int = 100, skip: int = 0) -> list[Order]:
    q: dict[str, Any] = {}
    if status:
        q["status"] = status
    return await Order.find(q).sort("-created_at").skip(skip).limit(limit).to_list()
