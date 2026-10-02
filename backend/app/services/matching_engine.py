"""Internal B-Book matching engine.

For market orders we fill immediately at the current LTP.
For limit / SL / SL-M orders, the order is parked OPEN — a background
poller (Phase 4 Celery) walks pending orders and fills any whose conditions
are met. This file ships the **immediate-fill** path used by `order_service`.

CRITICAL: orders are NEVER routed to an external exchange.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import timezone
from decimal import Decimal
from typing import Any

from beanie import PydanticObjectId
from bson import Decimal128

from app.core.exceptions import InsufficientFundsError, OrderRejectedError
from app.models._base import OrderAction, OrderType
from app.models.order import Order, OrderStatus
from app.models.trade import Trade
from app.services import (
    brokerage_calculator,
    market_data_service,
    netting_service,
    position_service,
    wallet_service,
)
from app.utils.decimal_utils import quantize_money, to_decimal
from app.utils.time_utils import now_utc

logger = logging.getLogger(__name__)

# Open settle-buffer (seconds after a segment's daily open) during which the
# pending-order poller HOLDS parked LIMIT / SL-M fills — right at the bell the
# first cached tick is often the stale overnight price, and firing a parked
# order on it books a phantom fill (e.g. a protective SL-M stopping out at a
# price the new session never traded). Kept in lockstep with
# order_validator._OPEN_SETTLE_BUFFER_SEC (blocks new user orders) and
# risk_enforcer._OPEN_GRACE_SEC (suppresses stop-out/SL/TP) so ALL three sides
# wait the same window for opening prices to settle — symmetric, no exploit.
_OPEN_SETTLE_BUFFER_SEC = 60


def execution_snapshot(
    *,
    quote: dict[str, Any],
    order_created_at: Any,
    reference_ltp: Decimal | None,
    fill_price: Decimal | None,
    immediate: bool = True,
) -> dict[str, Any]:
    """What the market looked like at a fill, for the slippage / latency reports.

    Pure and defensive on purpose: this runs inside the fill path, so a missing
    or malformed quote must yield None rather than raise.

    ``tick_age_ms`` measures the EXCHANGE's own packet time, not our own: the
    quote's `ts` is stamped when we write the snapshot, so it always reads
    ~0 ms old and would claim every fill was on a fresh price.

    ``fill_latency_ms`` is only meaningful for an immediate fill. A parked
    LIMIT / SL-M sits until its trigger hits, so the gap since it was placed
    is waiting time, not execution speed — those get None instead of a
    ten-minute "latency".
    """
    tick_age_ms: int | None = None
    try:
        raw_ts = (quote or {}).get("exchange_timestamp")
        feed_ms: float | None = None
        if raw_ts:
            feed_ms = float(raw_ts)
            # The exchange packet time arrives in seconds; our own stamps are ms.
            if feed_ms < 1e11:
                feed_ms *= 1000
        if feed_ms and feed_ms > 0:
            tick_age_ms = max(0, int(time.time() * 1000 - feed_ms))
    except (TypeError, ValueError):
        tick_age_ms = None

    fill_latency_ms: int | None = None
    if order_created_at is not None and immediate:
        try:
            created = order_created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            fill_latency_ms = max(0, int((now_utc() - created).total_seconds() * 1000))
        except Exception:  # noqa: BLE001 — telemetry must never break a fill
            fill_latency_ms = None

    markup: Decimal | None = None
    try:
        if reference_ltp is not None and reference_ltp > 0 and fill_price is not None:
            markup = quantize_money(to_decimal(fill_price) - to_decimal(reference_ltp))
    except Exception:  # noqa: BLE001
        markup = None

    return {
        "tick_age_ms": tick_age_ms,
        "fill_latency_ms": fill_latency_ms,
        "markup": markup,
    }


def _trade_number() -> str:
    return f"T{now_utc().strftime('%y%m%d')}{secrets.token_hex(4).upper()}"


async def execute_market_order(
    order: Order,
    *,
    cached_ltp: Decimal | None = None,
    cached_netting: dict[str, Any] | None = None,
    expected_price: Decimal | None = None,
    force_fill_price: Decimal | None = None,
) -> Trade:
    """Immediately fill a MARKET order, generate a Trade, update positions/
    holdings, debit charges + (settle PnL if closing).

    Fill-price selection (in priority order):

      1. ``expected_price`` — the BUY (ask) or SELL (bid) value the user saw
         on the order panel when they clicked. This is what makes ENTRY
         match the displayed price exactly. Capped at ±1% from the current
         bid/ask to prevent a tampered client from booking off-market.
      2. Live ask (for BUY) / bid (for SELL) — the broker's bid-ask spread.
      3. LTP — last resort fallback, used when bid/ask are missing (mock
         feed, off-hours).

    Performance: accepts ``cached_ltp`` and ``cached_netting`` from the
    validator to eliminate duplicate fetches. All independent DB writes
    are batched with ``asyncio.gather`` to minimise round-trips.
    """
    from app.models.position import Position, PositionStatus
    from app.models.transaction import TransactionType

    ltp = cached_ltp if cached_ltp is not None else await market_data_service.get_ltp(order.instrument.token)

    # Snapshot the raw mid/last price BEFORE fill-price selection (and the
    # per-pool broker spread further down) overwrite `ltp`. The spread step
    # needs the true LTP as its midpoint, not the chosen fill price.
    raw_ltp = ltp

    fill_price = ltp
    bid: Decimal | None = None
    ask: Decimal | None = None
    quote: dict[str, Any] = {}
    try:
        quote = await market_data_service.get_quote(order.instrument.token)
        bid_raw = quote.get("bid")
        ask_raw = quote.get("ask")
        bid = to_decimal(bid_raw) if bid_raw not in (None, 0, "0") else None
        ask = to_decimal(ask_raw) if ask_raw not in (None, 0, "0") else None
    except Exception:
        logger.exception("matching_engine_quote_fetch_failed")

    # ── Crossed / locked-quote guard (anti-arbitrage) ───────────────────
    # A valid market ALWAYS has ask >= bid. A stale or bad tick — common in the
    # last seconds before an exchange close / auction — can invert them
    # (bid > ask). Because BUY fills at the ask and SELL at the bid, a crossed
    # quote lets a user BUY at the LOW ask and SELL at the HIGH bid — a
    # risk-free round-trip. That is exactly what happened on RELIANCE
    # (20-Aug ~15:28–15:29): the feed froze at bid ₹1352 / ask ₹1273.40 around
    # the real ₹1313, and one user farmed ~₹99k in ~2 minutes of 100/500-qty
    # round-trips. Discard a crossed quote entirely so BOTH sides fall back to
    # the single (non-arbitrageable) LTP — a round-trip then nets ~0. The
    # expected_price anti-tamper check below also references LTP once bid/ask
    # are gone, so a tampered client price outside 1% of LTP is rejected too.
    if bid is not None and ask is not None and bid > ask:
        logger.warning(
            "matching_engine_crossed_quote_discarded",
            extra={
                "token": order.instrument.token,
                "symbol": order.instrument.symbol,
                "bid": str(bid),
                "ask": str(ask),
                "ltp": str(ltp),
                "user_id": str(order.user_id),
            },
        )
        bid = ask = None

    # Choose the live close-side price for this action.
    live_side: Decimal | None
    if order.action == OrderAction.BUY:
        live_side = ask if (ask is not None and ask > 0) else None
    else:
        live_side = bid if (bid is not None and bid > 0) else None

    # Admin "Manual" entry — an operator-supplied fill price that must be
    # honoured EXACTLY. No slippage cap, no live-side override: the admin
    # Market-Watch "Manual" order books the position at this negotiated
    # price and PnL accrues from it. Still subject to the zero/negative
    # sanity guard below.
    SLIPPAGE_CAP = Decimal("0.01")  # 1 %
    if force_fill_price is not None and force_fill_price > 0:
        fill_price = force_fill_price
    # A SERVER-initiated squareoff carrying an expected_price is a bracket
    # SL/TP fire — the risk enforcer passes the user's OWN stored trigger as
    # `expected_price`. That is not a browser value, so honour it EXACTLY: the
    # user set the barrier at that price and expects the close booked THERE,
    # not at whatever LTP the (1–2 s) risk tick drifted to. The 1% anti-tamper
    # cap below is for ENTRY orders only — applying it to bracket fires is what
    # filled "SL 260 set → closed at 255" when the tick lagged >1% past the
    # trigger. (EOD-rollover / kill-switch / zero-price squareoffs pass
    # expected_price == ltp, so this books at ltp for them — no change.)
    elif order.is_squareoff and expected_price is not None and expected_price > 0:
        fill_price = expected_price
    # Prefer the client-supplied expected price when it's within 1% of the
    # current live side — that keeps ENTRY identical to what the order
    # panel was showing, and the cap blocks any browser-side tampering.
    elif expected_price is not None and expected_price > 0:
        reference = live_side or ltp
        if reference and reference > 0:
            deviation = abs(expected_price - reference) / reference
            if deviation <= SLIPPAGE_CAP:
                fill_price = expected_price
            else:
                fill_price = live_side or ltp
                logger.warning(
                    "matching_engine_expected_price_outside_cap",
                    extra={
                        "expected": str(expected_price),
                        "reference": str(reference),
                        "deviation_pct": float(deviation) * 100,
                    },
                )
        else:
            fill_price = expected_price
    elif live_side is not None:
        fill_price = live_side
    # else: keep ltp fallback

    fill_price = quantize_money(fill_price)

    # ── Sanity guard: NEVER fill at zero / negative ──────────────────
    # If the upstream feed is down (Zerodha WS dropped + REST timing
    # out, Infoway tick stale) get_ltp can return 0 and bid/ask are
    # filtered out above when they're 0. Without this guard a market
    # SELL would execute at ₹0.00 — operator-reported: an MCX position
    # avg ₹8631 closed at ₹0 booked −₹17 lakh realised loss from a
    # single 100-lot exit, purely because the LTP feed flatlined for
    # ~3 minutes during a Zerodha auto-login window.
    #
    # SQUAREOFF EXCEPTION: for closing / squareoff orders, try the Redis
    # last_ltp before hard-blocking. A momentary Infoway feed gap on CDS
    # instruments (XAUUSD, XAGUSD) can last 30–90 s while the WS
    # reconnects. Blocking the close traps the user in the position and
    # leaves the PENDING order stuck in the Orders tab. last_ltp is capped
    # at a week TTL so it's always a real recent price, never fabricated.
    if fill_price is None or fill_price <= Decimal("0"):
        _used_last_ltp = False
        if order.is_squareoff:
            try:
                from app.core.redis_client import cache_get as _cg
                _last = await _cg(f"mdlast:{order.instrument.token}")
                _last_val = to_decimal(_last.get("ltp") or 0) if _last else Decimal(0)
                if _last_val > 0:
                    fill_price = quantize_money(_last_val)
                    ltp = fill_price
                    _used_last_ltp = True
                    logger.warning(
                        "matching_engine_squareoff_used_last_ltp",
                        extra={
                            "order_id": str(order.id),
                            "symbol": order.instrument.symbol,
                            "last_ltp": str(fill_price),
                        },
                    )
            except Exception:
                pass
        if not _used_last_ltp and (fill_price is None or fill_price <= Decimal("0")):
            logger.error(
                "matching_engine_zero_price_blocked",
                extra={
                    "order_id": str(order.id),
                    "user_id": str(order.user_id),
                    "token": order.instrument.token,
                    "symbol": order.instrument.symbol,
                    "ltp_raw": str(ltp),
                    "bid": str(bid) if bid is not None else None,
                    "ask": str(ask) if ask is not None else None,
                },
            )
            raise OrderRejectedError(
                "Market data feed is stale (price unavailable). "
                "Please retry in a few seconds.",
                code="STALE_FEED",
            )

    ltp = fill_price  # downstream uses `ltp` as the executed price

    # ── Netting settings (reuse from validator when available) ────────
    if cached_netting is not None:
        netting_resolved = cached_netting
    else:
        instr_ref = order.instrument
        option_type = None
        if "OPTION" in (instr_ref.segment or "").upper():
            sym = (instr_ref.symbol or "").upper()
            if sym.endswith("CE"):
                option_type = "CE"
            elif sym.endswith("PE"):
                option_type = "PE"
        netting_resolved = await netting_service.get_effective_settings(
            order.user_id,
            instr_ref.segment,
            action=order.action.value if hasattr(order.action, "value") else str(order.action),
            option_type=option_type,
            product_type=order.product_type.value if hasattr(order.product_type, "value") else str(order.product_type),
            symbol=instr_ref.symbol,
        )

    # ── Per-pool broker spread (execution markup) ─────────────────────
    # The live market-data feed (`/ws/marketdata`) is a single PUBLIC
    # broadcast, so it can't carry a per-admin spread — a spread an admin
    # sets on Segment Settings never reached their pool's fills (only the
    # platform/super-admin spread, applied globally in
    # market_data_service._apply_admin_spread, ever took effect). Apply it
    # HERE instead: `netting_resolved` came from get_effective_settings,
    # which already walks the USER → SUB-ADMIN → BROKER → SUPER-ADMIN →
    # base cascade, so `spread_pips` here is THIS user's pool's spread.
    # Mark the fill up by half-spread on the taken side (mirrors the
    # fixed/floating math in _apply_admin_spread). Skipped for non-MARKET
    # fills (LIMIT / SL-M book at the user's own price) and admin Manual
    # orders (force_fill_price is the exact negotiated price).
    #
    # ALSO skipped for SL_HIT / TP_HIT bracket fires. The risk-enforcer
    # passes the user's exact stop/target as `expected_price`, so `fill_price`
    # above already equals the trigger the user set. Without this guard the
    # spread step below would overwrite it with raw_ltp±half, re-introducing
    # the slippage the bracket is meant to eliminate — the user's complaint
    # that "SL set kiya wahan close nahi hua, upar/niche ho gaya". Operator
    # decision (2026-06-23): SL/TP must exit at EXACTLY the set price even
    # though the broker forgoes its half-spread markup on these closes. Stop-
    # out / manual / ordinary market fills are unaffected and still get spread.
    _exact_close = str(getattr(order, "close_reason", "") or "") in ("SL_HIT", "TP_HIT")
    if (
        order.order_type == OrderType.MARKET
        and not (force_fill_price is not None and force_fill_price > 0)
        and not _exact_close
    ):
        seg_settings = netting_resolved.get("settings", {}) if netting_resolved else {}
        try:
            spread_pips = to_decimal(seg_settings.get("spread_pips") or 0)
        except Exception:
            spread_pips = Decimal(0)
        spread_mode = str(seg_settings.get("spread_type") or "fixed").lower()
        if spread_pips > 0 and raw_ltp is not None and raw_ltp > 0:
            half = spread_pips / Decimal(2)
            has_book = bid is not None and ask is not None and bid > 0 and ask > 0
            live_spread = (ask - bid) if has_book else Decimal(0)
            if spread_mode != "floating" or live_spread < spread_pips:
                # Fixed: ignore the exchange spread entirely (broker markup).
                # Floating: widen to the configured minimum when the live
                # book is tighter than that minimum.
                spread_side = (
                    raw_ltp + half if order.action == OrderAction.BUY else raw_ltp - half
                )
            else:
                # Floating with a wide-enough live book: keep the real side.
                spread_side = ask if order.action == OrderAction.BUY else bid
            spread_side = quantize_money(spread_side)
            # Safety cap: a mis-keyed spread (e.g. a fat-finger 50000) must
            # NEVER book a wildly off-market fill — that is exactly how the
            # GOLD ₹1.38 cr phantom profit happened. Beyond 20 % of LTP,
            # treat the spread as misconfigured and leave the fill untouched.
            if spread_side > 0 and abs(spread_side - raw_ltp) <= raw_ltp * Decimal("0.20"):
                fill_price = spread_side
                ltp = fill_price  # keep charges / notional / trade row in sync
            else:
                logger.warning(
                    "matching_engine_spread_out_of_band_skipped",
                    extra={
                        "order_id": str(order.id),
                        "raw_ltp": str(raw_ltp),
                        "spread_pips": str(spread_pips),
                        "side_price": str(spread_side),
                    },
                )

    # ── Existing-position lookup first (needed to classify the fill as
    #    opening vs closing — `charge_on` gates brokerage on one or both).
    existing_pos = await Position.find_one(
        Position.user_id == order.user_id,
        Position.instrument.token == order.instrument.token,
        Position.product_type == order.product_type,
        Position.status == PositionStatus.OPEN,
    )
    old_pos_margin = to_decimal(existing_pos.margin_used) if existing_pos else Decimal(0)

    # Classify: this fill is "closing" if it pushes the position toward 0
    # (BUY against a short, SELL against a long). A fresh open or same-side
    # pyramid is "opening". Partial-close / flip cases still count as
    # closing for brokerage gating — the position service realizes the
    # closed portion separately, and the admin's `charge_on` is per-leg
    # not per-share. Without an existing position the fill is always
    # opening (you can't close what you don't have).
    is_closing = False
    if existing_pos is not None:
        cur_qty = to_decimal(existing_pos.quantity)
        if cur_qty > 0 and order.action == OrderAction.SELL:
            is_closing = True
        elif cur_qty < 0 and order.action == OrderAction.BUY:
            is_closing = True

    charge_on = (
        netting_resolved.get("settings", {}).get("charge_on")
        if netting_resolved
        else None
    )
    charges = await brokerage_calculator.calculate(
        segment_type=order.instrument.segment,
        action=order.action,
        product_type=order.product_type,
        qty=order.quantity,
        price=ltp,
        lot_size=order.instrument.lot_size,
        netting_override=netting_resolved.get("settings"),
        is_closing=is_closing,
        charge_on=charge_on,
    )

    # ── Build Trade + update Order (CPU, no I/O) ─────────────────────
    qty_dec = to_decimal(order.quantity)
    notional = quantize_money(ltp * qty_dec)

    # Compute realized P&L in INR for closing legs and freeze it on the
    # trade row. Uses the existing position's avg_price, the fill price,
    # and the USD/INR rate as of NOW (snapshotted — never recomputed).
    # Closing-leg brokerage is folded into `pnl_inr_dec` so the History
    # tab's P&L column shows the user's true net cost (raw P&L − close
    # brokerage), matching the user's mental model "close brokerage 20 +
    # P&L −20 → total loss −40". `raw_pnl_inr_dec` keeps the un-folded
    # raw realized PnL for the WALLET adjustment, because the closing-
    # leg brokerage is already debited separately via the CHARGES line
    # below — using `pnl_inr_dec` there would double-charge it (debiting
    # the close brokerage once via CHARGES and again folded into PNL).
    # Opening fills leave both = None.
    pnl_inr_dec: Decimal | None = None
    raw_pnl_inr_dec: Decimal | None = None
    if is_closing and existing_pos is not None:
        cur_qty = to_decimal(existing_pos.quantity)
        avg = to_decimal(existing_pos.avg_price)
        # Specific-lot close: realize against the tapped fill's entry price
        # instead of the position average. Opt-in — set ONLY by the Active-tab
        # per-fill Exit; every other close leaves _cbo None → avg-price as before.
        _cbo = getattr(order, "cost_basis_override", None)
        if _cbo is not None:
            avg = to_decimal(_cbo)
        closed_qty = min(abs(cur_qty), qty_dec)
        sign = Decimal(1) if cur_qty > 0 else Decimal(-1)
        raw_realized = (ltp - avg) * closed_qty * sign
        if market_data_service.is_usd_quoted_segment(order.instrument.segment):
            fx = to_decimal(market_data_service.get_usd_inr_rate())
            raw_realized = raw_realized * fx
        raw_pnl_inr_dec = quantize_money(raw_realized)
        pnl_inr_dec = quantize_money(raw_realized - to_decimal(charges.brokerage))

    snapshot = execution_snapshot(
        quote=quote,
        order_created_at=getattr(order, "created_at", None),
        reference_ltp=raw_ltp,
        fill_price=to_decimal(ltp),
        # A parked LIMIT / SL-M reaches this function when its trigger fires;
        # the wait since it was placed is not our execution speed.
        immediate=order.order_type == OrderType.MARKET,
    )

    def _money(v: Decimal | None) -> Decimal128 | None:
        return Decimal128(str(v)) if v is not None else None

    trade = Trade(
        trade_number=_trade_number(),
        order_id=order.id,  # type: ignore[arg-type]
        user_id=order.user_id,
        instrument=order.instrument,
        action=order.action,
        product_type=order.product_type,
        quantity=order.quantity,
        price=Decimal128(str(ltp)),
        value=Decimal128(str(notional)),
        brokerage=Decimal128(str(charges.brokerage)),
        total_charges=Decimal128(str(charges.total)),
        net_amount=Decimal128(
            str(quantize_money(notional + (charges.total if order.action == OrderAction.SELL else -charges.total)))
        ),
        pnl_inr=Decimal128(str(pnl_inr_dec)) if pnl_inr_dec is not None else None,
        # Carry the specific-lot basis onto the Trade so the FIFO closed-blotter
        # pairs this close with the SAME lot the P&L booked against. None for
        # every non-Active-tab-Exit close → unchanged FIFO display.
        cost_basis_override=getattr(order, "cost_basis_override", None) if is_closing else None,
        expected_price=_money(to_decimal(expected_price) if expected_price else None),
        reference_ltp=_money(raw_ltp),
        bid_at_fill=_money(bid),
        ask_at_fill=_money(ask),
        markup=_money(snapshot["markup"]),
        tick_age_ms=snapshot["tick_age_ms"],
        fill_latency_ms=snapshot["fill_latency_ms"],
    )
    order.filled_quantity += order.quantity
    order.pending_quantity = max(0, order.quantity - order.filled_quantity)
    order.average_price = Decimal128(str(ltp))
    order.brokerage = Decimal128(str(charges.brokerage))
    order.other_charges = Decimal128(
        str(quantize_money(charges.total - charges.brokerage))
    )
    order.status = OrderStatus.EXECUTED
    order.executed_at = now_utc()

    # ── Persist trade + order in parallel (independent writes) ────────
    await asyncio.gather(trade.insert(), order.save())

    # ── Bonus wager progress (Bonus Management) ───────────────────────
    # Every fill's notional counts toward any ACTIVE bonus's wager target;
    # gated + best-effort so it can NEVER block or fail a fill.
    try:
        from app.core.config import settings as _bset

        if _bset.BONUSES_ENABLED:
            from app.services import bonus_service as _bonus

            await _bonus.increment_wager(
                order.user_id, to_decimal(trade.value), trade_id=trade.id
            )
    except Exception:  # pragma: no cover — bonus must never affect a fill
        logger.exception("bonus_wager_increment_failed order=%s", getattr(order, "id", None))

    # ── Update position ──────────────────────────────────────────────
    sl_dec = to_decimal(order.bracket_stop_loss) if order.bracket_stop_loss is not None else None
    tp_dec = to_decimal(order.bracket_target) if order.bracket_target is not None else None
    pos = await position_service.apply_fill(
        user_id=order.user_id,
        instrument=order.instrument,
        segment_type=order.instrument.segment,
        action=order.action,
        product_type=order.product_type,
        quantity=order.quantity,
        price=ltp,
        margin_used=to_decimal(order.margin_blocked),
        stop_loss=sl_dec,
        target=tp_dec,
        is_demo=bool(getattr(order, "is_demo", False)),
        cost_basis_override=(
            to_decimal(getattr(order, "cost_basis_override", None))
            if getattr(order, "cost_basis_override", None) is not None
            else None
        ),
    )

    # ── P&L sharing WS notify on Position close ──────────────────────
    # When this fill flattened the position (FIFO match on opposite side
    # cleared quantity to 0), `apply_fill` flipped status to CLOSED and
    # already persisted. Best-effort WS notify so admin P&L sharing
    # dashboards refresh in real-time. Fire-and-forget — never block
    # trade execution on a Redis hiccup or missing-broker lookup.
    if pos is not None and pos.status == PositionStatus.CLOSED and pos.user_id is not None:
        # Best-effort WS notify so admin P&L-sharing dashboards refresh. The
        # ENTIRE block is wrapped so a hiccup here can NEVER break the close
        # (a missing import / Redis blip must not fail the trade). Scheduled
        # as a background task so it doesn't add a DB read + publish loop to
        # the close hot path — but if scheduling itself fails we just skip it.
        try:
            _closed_user_id = pos.user_id

            async def _notify_pnl_sharing() -> None:
                from app.models.user import User
                from app.services.pnl_sharing_service import publish_pnl_sharing_update

                _u = await User.get(_closed_user_id)
                if _u is None:
                    return
                ancestors = list(_u.broker_ancestry or [])
                if (
                    _u.assigned_broker_id is not None
                    and _u.assigned_broker_id not in ancestors
                ):
                    ancestors.append(_u.assigned_broker_id)
                await asyncio.gather(
                    *[publish_pnl_sharing_update(a) for a in ancestors],
                    return_exceptions=True,
                )

            try:
                from app.utils.background import fire_and_forget

                fire_and_forget(_notify_pnl_sharing(), label="pnl_sharing_ws")
            except Exception:
                # No background helper available / scheduling failed — fall
                # back to a bare task so the notify still fires; never raise.
                asyncio.create_task(_notify_pnl_sharing())
        except Exception:
            logger.exception("pnl_sharing_ws_publish_failed")

    # ── Wallet adjustments — B-book / CFD model ──────────────────────
    # In a B-book broker the user never actually receives the notional
    # value of the underlying asset on a SELL — they only realize the
    # price-difference P&L on close. So the wallet only moves by:
    #   • margin block on open (handled inside position_service.apply_fill
    #     via wallet_service.block_margin when margin_used grows)
    #   • margin release on close (when margin_used shrinks)
    #   • charges (brokerage + taxes, always a debit)
    #   • realized P&L (signed: + on profit, − on loss; ONLY on closing legs)
    #
    # The previous version unconditionally credited `ltp × quantity` on
    # every SELL order, which (a) was the wrong economic model for a
    # B-book broker and (b) credited USD notional as INR on USD-quoted
    # instruments like BTCUSD/XAUUSD — that's the bug that ballooned
    # wallets by the underlying's notional on every open-SELL.
    new_pos_margin = to_decimal(pos.margin_used)
    freed_margin = old_pos_margin - new_pos_margin
    if freed_margin > 0:
        await wallet_service.release_margin(order.user_id, freed_margin)

    await wallet_service.adjust(
        order.user_id,
        -charges.total,
        transaction_type=TransactionType.CHARGES,
        narration=f"Charges for {order.action.value} {order.instrument.symbol} x{order.quantity}",
        reference_type="ORDER",
        reference_id=str(order.id),
    )

    # Realized P&L (signed, INR, already FX-converted for USD segments
    # at line ~200) — credited on closing fills only. Uses `raw_pnl_inr_dec`
    # (NOT the brokerage-folded `pnl_inr_dec`) because the closing-leg
    # brokerage is already debited separately via the CHARGES line above —
    # using `pnl_inr_dec` here would double-debit it. `pnl_inr_dec` is
    # kept only for `Trade.pnl_inr` (History tab display).
    if raw_pnl_inr_dec is not None and raw_pnl_inr_dec != 0:
        pnl_narration = (
            f"Realized {'profit' if raw_pnl_inr_dec > 0 else 'loss'} "
            f"on {order.instrument.symbol} close"
        )
        try:
            await wallet_service.adjust(
                order.user_id,
                raw_pnl_inr_dec,
                transaction_type=TransactionType.PNL,
                narration=pnl_narration,
                reference_type="ORDER",
                reference_id=str(order.id),
            )
        except InsufficientFundsError:
            # Forced-close path (risk_enforcer stop-out, auto-squareoff): the
            # position MUST close even if the realized loss exceeds available
            # balance + credit_limit. Fall back to `force_debit` which debits
            # what we can and books the overflow to `settlement_outstanding`.
            # Voluntary user closes (`is_squareoff` falsy) keep raising so the
            # user can't silently rack up dues from their own actions.
            if getattr(order, "is_squareoff", False) and raw_pnl_inr_dec < 0:
                await wallet_service.force_debit(
                    order.user_id,
                    -raw_pnl_inr_dec,
                    transaction_type=TransactionType.PNL,
                    narration=f"{pnl_narration} (stop-out: shortfall booked to outstanding)",
                    reference_type="ORDER",
                    reference_id=str(order.id),
                )
            else:
                raise

    return trade


async def cancel_order(order: Order, *, reason: str | None = None) -> Order:
    if order.status not in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIAL):
        return order
    order.status = OrderStatus.CANCELLED
    order.cancelled_at = now_utc()
    order.rejection_reason = reason
    await order.save()
    # Release any margin that had been blocked
    if to_decimal(order.margin_blocked) > 0:
        await wallet_service.release_margin(order.user_id, to_decimal(order.margin_blocked))
    return order


# ── Pending-order poller ─────────────────────────────────────────────
# Walks every parked LIMIT / SL-M order every tick and fires the ones whose
# trigger condition is met. Started once from the FastAPI lifespan.

_poller_running: bool = False


def _should_fill(order_type: OrderType, action: OrderAction, ltp: Decimal,
                 limit_price: Decimal, trigger_price: Decimal,
                 limit_wait_up: bool | None = None) -> bool:
    """LIMIT: a RESTING price-touch order — fills ONLY once the price
       REACHES the limit level, regardless of BUY/SELL side (operator
       spec: "jab tak price waha pe nahi jaye, execute mat ho"). The side
       the price must approach from is captured at placement:
         • limit_wait_up=True  → limit was ABOVE market → fill when LTP ≥ limit
         • limit_wait_up=False → limit was BELOW market → fill when LTP ≤ limit
       Legacy rows (limit_wait_up=None) fall back to the old action-based
       rule (BUY fills ≤ limit, SELL fills ≥ limit).

       SL-M  BUY  fills when LTP ≥ trigger (stop-buy / break-out)
       SL-M  SELL fills when LTP ≤ trigger (stop-loss exit)"""
    if order_type == OrderType.LIMIT:
        if limit_price <= 0:
            return False
        if limit_wait_up is None:
            # Legacy / no captured direction — old action-based behaviour.
            if action == OrderAction.BUY:
                return ltp <= limit_price
            return ltp >= limit_price
        # Touch semantics: wait until price reaches the level from the side
        # it was placed on.
        if limit_wait_up:
            return ltp >= limit_price
        return ltp <= limit_price
    if order_type == OrderType.SL_M:
        if trigger_price <= 0:
            return False
        if action == OrderAction.BUY:
            return ltp >= trigger_price
        return ltp <= trigger_price
    if order_type == OrderType.SL:
        # Stop-LIMIT: arms when LTP crosses the trigger (same side as SL-M),
        # then behaves like a LIMIT at `limit_price` — it fills only while the
        # price is still on the fillable side of the limit, so a fast gap PAST
        # the limit protects the user (unlike SL-M which always fills). BUY SL
        # (stop-buy): armed when ltp ≥ trigger, fills up to the limit
        # (ltp ≤ limit). SELL SL (stop-loss exit): armed when ltp ≤ trigger,
        # fills down to the limit (ltp ≥ limit). Both trigger + limit must be
        # set. This is why plain SL orders sat inert before — there was no
        # branch here and the poller query below never selected them.
        if trigger_price <= 0 or limit_price <= 0:
            return False
        if action == OrderAction.BUY:
            return ltp >= trigger_price and ltp <= limit_price
        return ltp <= trigger_price and ltp >= limit_price
    return False


async def _exit_order_fill_open_qty(order) -> float:
    """For a per-fill SL/TP exit order, the still-open qty of the opening fill it
    protects (FIFO over the token's trades). Returns -1 when `order` isn't such an
    exit order (skip the guard), 0 when the fill is gone/already consumed."""
    tid = getattr(order, "sl_tp_source_trade_id", None)
    if tid is None:
        return -1.0
    from datetime import datetime as _d, timezone as _tz

    from app.models.trade import Trade as _Trade

    src = await _Trade.get(tid)
    if src is None:
        return 0.0
    trades = await _Trade.find(
        _Trade.user_id == order.user_id,
        _Trade.instrument.token == order.instrument.token,
    ).to_list()
    is_long = str(getattr(src.action, "value", src.action)).upper() == "BUY"

    def _aw(dt):
        if dt is None:
            return _d.min.replace(tzinfo=_tz.utc)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=_tz.utc)

    trades.sort(key=lambda tr: _aw(tr.executed_at))
    fifo: list[list] = []
    for tr in trades:
        act = str(getattr(tr.action, "value", tr.action)).upper()
        same = (is_long and act == "BUY") or (not is_long and act == "SELL")
        if same:
            fifo.append([str(tr.id), float(tr.quantity)])
        else:
            remain = float(tr.quantity)
            for pair in fifo:
                if remain <= 0:
                    break
                c = min(pair[1], remain)
                pair[1] -= c
                remain -= c
    for _tid, lo in fifo:
        if _tid == str(tid):
            return max(0.0, lo)
    return 0.0


async def _cancel_exit_order(o, reason: str) -> None:
    """Cancel a parked per-fill exit order in place. These are is_squareoff
    closing orders with no blocked margin, so there's nothing to release."""
    try:
        o.status = OrderStatus.CANCELLED
        if hasattr(o, "cancelled_at"):
            o.cancelled_at = now_utc()
        if hasattr(o, "close_reason") and not o.close_reason:
            o.close_reason = reason
        await o.save()
    except Exception:  # noqa: BLE001
        logger.exception("exit_order_cancel_failed", extra={"order_id": str(getattr(o, "id", None))})


async def _cancel_oco_siblings(fired_order) -> None:
    """After a per-fill SL/TP exit fires, cancel the OTHER leg (and any other
    still-open orders) in its OCO group so the stop can't later fire on the
    remaining book and close a DIFFERENT fill."""
    grp = getattr(fired_order, "oco_group_id", None)
    if not grp:
        return
    try:
        sibs = await Order.find(
            {
                "oco_group_id": grp,
                "status": {"$in": [OrderStatus.OPEN.value, OrderStatus.PARTIAL.value, OrderStatus.PENDING.value]},
            }
        ).to_list()
    except Exception:  # noqa: BLE001
        return
    for s in sibs:
        if str(s.id) == str(fired_order.id):
            continue
        await _cancel_exit_order(s, "OCO_CANCELLED")


async def trigger_pending_orders() -> int:
    """One pass over all OPEN/PARTIAL non-MARKET orders. Returns how many
    orders fired this pass. Logs but never raises — a single bad order
    must not stop the others.

    Fill-price contract: when a LIMIT order's trigger is met the trade
    books at the LIMIT price (what the user typed), not at the LTP that
    the poller happened to read at fire time. Same for SL-M: fills at
    the user's TRIGGER price. Previously this path called
    `execute_market_order(o)` with no `expected_price`, so the engine
    picked up bid/ask/LTP and the realised fill drifted away from the
    user's order — e.g. a BUY LIMIT at 79222 used to record at the
    LTP-of-the-moment (which after a fast tick down could be 79215, a
    7-rupee discrepancy the user noticed in the Orders tab). Passing
    `expected_price = limit_or_trigger` makes the engine use that value
    directly. The engine still clamps to ±1% of live bid/ask as an
    anti-tamper guard, but `_should_fill` only allows fires once LTP
    has crossed the user's price, so the limit/trigger is always well
    inside that cap by definition.
    """
    triggered = 0
    try:
        rows = await Order.find(
            {
                "status": {"$in": [OrderStatus.OPEN.value, OrderStatus.PARTIAL.value]},
                "order_type": {"$in": [OrderType.LIMIT.value, OrderType.SL_M.value, OrderType.SL.value]},
            }
        ).to_list()
    except Exception:
        logger.exception("pending_order_scan_failed")
        return 0

    if not rows:
        return 0

    # Build LTP snapshot from _state directly (same price the user's chart shows).
    # Previously used get_ltp() which goes through a 700ms cache → on cache miss
    # hits _zerodha_overlay → reads zerodha.ticks_by_token (updated immediately on
    # every WS tick). That diverges from _state (updated every ~1s by market_tick
    # loop): a momentary WS tick that hadn't appeared on the chart yet could still
    # cross a limit and fire the order — user sees price at 102 on chart, order
    # fires because get_ltp() caught a 99.8 tick still invisible to the chart.
    # get_ltp_instant() reads _state directly: O(1) sync, no cache, no REST calls,
    # consistent with what the chart shows.
    unique_tokens = list({o.instrument.token for o in rows})
    ltp_map: dict[str, Decimal | None] = {
        tok: market_data_service.get_ltp_instant(tok) for tok in unique_tokens
    }

    # Cross-worker dedup. The poller runs in every uvicorn worker — without
    # a distributed claim, two workers reading the same OPEN limit order in
    # the same 1.5 s tick both called `execute_market_order` and TWO trades
    # landed in History for the same fire (the user-reported "limit order
    # 2 baar execute hua" bug). Redis SETNX with a 10 s TTL is enough: only
    # the first worker to claim the order_id key proceeds; the rest skip.
    from app.core.redis_client import idempotency_check_and_set

    # Market-closed gate — mirror of the risk_enforcer one. The poller
    # ticks even when the exchange is shut, but the cached LTP is the
    # last open-market tick. Firing a LIMIT / SL-M against that stale
    # value books a phantom fill at a price nobody traded at — the same
    # class of bug as auto-firing brackets after close. Skip orders whose
    # segment is past close; let them sit until reopen.
    from app.utils.time_utils import (
        is_after_close,
        is_before_open,
        is_weekend,
        is_within_open_grace,
        now_ist as _now_ist_pend,
    )

    _now_pend = _now_ist_pend()
    _is_weekend_pend = is_weekend(_now_pend.date())

    def _order_segment_closed(seg: str | None) -> bool:
        if not seg:
            return False
        if _is_weekend_pend and seg.upper().startswith(("NSE", "BSE", "MCX", "NFO", "BFO")):
            return True
        # Out of session = past close (15:30→midnight) OR before open
        # (midnight→09:15). Without the pre-open half a weekday-morning
        # tick of yesterday's stale LTP would cross a parked LIMIT / SL-M
        # and fire a phantom fill at a price nobody traded at — the
        # "limit order sahi nahi chal raha" symptom. Crypto / forex
        # (24×7 / 24×5) return None from both helpers and stay live.
        return is_after_close(seg, _now_pend) or is_before_open(seg, _now_pend)

    for o in rows:
        try:
            seg = getattr(o.instrument, "segment", None)
            _seg_str = str(seg) if seg else None
            if _order_segment_closed(_seg_str):
                continue
            # Open settle-buffer: hold parked fills for the first
            # _OPEN_SETTLE_BUFFER_SEC after the bell — the first cached tick can
            # be the stale overnight price and firing on it books a phantom fill
            # (e.g. the 2026-07-08 CRUDEOIL 6767 class). Mirrors the validator's
            # new-order block + risk_enforcer's stop-out suppression for the SAME
            # window. Forex/crypto self-exempt (is_within_open_grace -> False).
            if _seg_str and is_within_open_grace(_seg_str, _OPEN_SETTLE_BUFFER_SEC, _now_pend):
                continue
            ltp = ltp_map.get(o.instrument.token)
            # ltp=0 is as bad as ltp=None — BUY LIMIT would falsely fire
            # because (0 <= any_positive_price) is always True.
            if ltp is None or ltp <= 0:
                continue
            limit_price = to_decimal(o.price)
            trigger_price = to_decimal(o.trigger_price)
            if not _should_fill(
                o.order_type, o.action, ltp, limit_price, trigger_price,
                getattr(o, "limit_wait_up", None),
            ):
                continue
            # Lock the fill at the user's specified price. LIMIT and SL
            # (stop-LIMIT) book at `o.price` (the limit); SL-M books at
            # `o.trigger_price` (market-on-trigger).
            if o.order_type in (OrderType.LIMIT, OrderType.SL) and limit_price > 0:
                fill_at = limit_price
            elif o.order_type == OrderType.SL_M and trigger_price > 0:
                fill_at = trigger_price
            else:
                fill_at = None

            # Execution-time sanity: if the fill price deviates more than 50%
            # from current LTP the price feed is suspect — skip this tick
            # and let the order sit until the feed recovers.
            if fill_at is not None and fill_at > 0:
                _exec_dev = abs(fill_at - ltp) / ltp * 100
                if _exec_dev > Decimal("50"):
                    logger.warning(
                        "pending_order_fill_price_deviation_too_large",
                        extra={
                            "order_id": str(o.id),
                            "symbol": o.instrument.symbol,
                            "fill_at": str(fill_at),
                            "ltp": str(ltp),
                            "deviation_pct": float(_exec_dev),
                        },
                    )
                    continue

            # Atomic claim. TTL is generously sized vs the expected
            # execute_market_order latency (~50-200 ms) so the key only
            # outlives a real fire long enough to swallow a duplicate from
            # a concurrent worker — never long enough to block a legitimate
            # retry after a crash.
            claim_key = f"pending_fire:{o.id}"
            try:
                claimed = await idempotency_check_and_set(claim_key, ttl_sec=10)
            except Exception:
                logger.exception("pending_fire_claim_failed", extra={"order_id": str(o.id)})
                claimed = False
            if not claimed:
                logger.info(
                    "pending_order_skip_already_claimed",
                    extra={"order_id": str(o.id), "symbol": o.instrument.symbol},
                )
                continue

            logger.info(
                "pending_order_firing",
                extra={
                    "order_id": str(o.id),
                    "symbol": o.instrument.symbol,
                    "action": o.action.value if hasattr(o.action, "value") else str(o.action),
                    "order_type": o.order_type.value if hasattr(o.order_type, "value") else str(o.order_type),
                    "limit_price": str(limit_price),
                    "trigger_price": str(trigger_price),
                    "ltp_at_fire": str(ltp),
                    "fill_at": str(fill_at),
                },
            )
            # Per-fill SL/TP exit guard: if the opening fill this exit protects
            # is already gone (manually closed, or consumed by its sibling),
            # firing would close a DIFFERENT fill's qty on the netted book —
            # cancel it instead. -1 ⇒ not an exit order (normal fire). Fail-open
            # on any error so a genuine stop still fires.
            try:
                _open_qty = await _exit_order_fill_open_qty(o)
            except Exception:  # noqa: BLE001
                _open_qty = -1.0
            if _open_qty == 0.0:
                await _cancel_exit_order(o, "SL_TP_FILL_GONE")
                logger.info(
                    "exit_order_cancelled_fill_gone",
                    extra={"order_id": str(o.id), "symbol": o.instrument.symbol},
                )
                continue

            await execute_market_order(o, cached_ltp=ltp, expected_price=fill_at)
            triggered += 1
            # OCO: this per-fill exit fired → cancel its paired stop/target so it
            # can't later fire on the remaining book.
            try:
                await _cancel_oco_siblings(o)
            except Exception:  # noqa: BLE001
                logger.exception("oco_cancel_failed", extra={"order_id": str(o.id)})
        except Exception:
            logger.exception(
                "pending_order_trigger_failed",
                extra={"order_id": str(o.id), "symbol": o.instrument.symbol},
            )
    return triggered


async def pending_order_poller(interval_sec: float = 1.5) -> None:
    """Background loop launched from the lifespan. Idempotent — second call
    returns immediately."""
    global _poller_running
    if _poller_running:
        return
    _poller_running = True
    logger.info("pending_order_poller_started", extra={"interval_sec": interval_sec})
    try:
        import asyncio as _asyncio
        while _poller_running:
            n = await trigger_pending_orders()
            if n:
                logger.info("pending_orders_triggered", extra={"count": n})
            await _asyncio.sleep(interval_sec)
    finally:
        _poller_running = False
        logger.info("pending_order_poller_stopped")


def stop_pending_order_poller() -> None:
    global _poller_running
    _poller_running = False
