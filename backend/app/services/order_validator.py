"""Order pre-trade validator — 12 checks per spec, in order.

Returns (ok, applied_settings_snapshot) on success or raises OrderRejectedError.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.core.config import settings
from app.core.exceptions import (
    InsufficientFundsError,
    MarketClosedError,
    OrderRejectedError,
    SegmentNotAllowedError,
)
from app.models._base import (
    OrderAction,
    OrderType,
    ProductType,
)
from app.models.holiday import TradingHoliday
from app.models.instrument import Instrument
from app.models.position import Position, PositionStatus, UserPositionTracker
from app.models.user import User, UserStatus
from app.services import market_data_service, netting_service, wallet_service
from app.services.instrument_service import infer_instrument_type_from_symbol
from app.utils.decimal_utils import to_decimal
from app.utils.time_utils import (
    is_weekend,
    is_within_open_grace,
    market_open_time_for_segment,
    now_ist,
    parse_hhmm,
    to_ist,
)


# ── Open settle-buffer (stale-first-tick protection) ──────────────────
# At the exact opening bell the exchange's FIRST packet often carries a
# FRESH exchange_timestamp but a STALE / pre-open / unsettled price — the
# real opening trade prints a few seconds later. The live-tick freshness
# gate (§11b) waves it through because the timestamp IS fresh, so a user
# order could fill at that transient price. 2026-07-08 09:00 MCX: a
# CRUDEOIL BUY booked at a stale 6767 while the session actually opened
# ~6885, gifting ~₹21.6k phantom profit when squared off a minute later.
# We therefore refuse NEW USER orders (entry AND user-initiated exit) for
# the first _OPEN_SETTLE_BUFFER_SEC after a segment's daily open so the
# opening price establishes first. SERVER force-close (risk / admin / EOD)
# is exempt — and risk_enforcer independently suppresses stop-out/SL/TP for
# the SAME window (its _OPEN_GRACE_SEC), so during the buffer NEITHER side
# acts on the unsettled tick: fully symmetric, exploit-proof both ways.
# AMO orders are exempt (they queue for the open). Forex/crypto self-exempt
# — they have no daily bell, so is_within_open_grace() returns False.
# Keep this in lockstep with risk_enforcer._OPEN_GRACE_SEC.
_OPEN_SETTLE_BUFFER_SEC = 60


@dataclass
class ValidatedOrder:
    settings: dict[str, Any]
    netting_settings: dict[str, Any]  # full netting resolved dict for matching engine
    margin_required: Decimal
    ltp: Decimal


def _money_to_float(v: Any) -> float:
    """Coerce ANY money-shaped value (Decimal128 / Decimal / str / number / None)
    into a Python float. Python's built-in ``float()`` does NOT accept
    ``bson.Decimal128`` and raises ``TypeError: float() argument must be a
    string or a real number, not 'Decimal128'`` — which is the exact error
    the user saw on SELL / reducing orders against an open Position. We
    route every money read in this validator through this helper so that
    bug can never resurface."""
    if v is None:
        return 0.0
    try:
        return float(to_decimal(v))
    except Exception:
        try:
            return float(str(v))
        except Exception:
            return 0.0


def bracket_direction_error(
    action: Any, ref_price: Any, sl: Any = None, tp: Any = None
) -> str | None:
    """Return a user-facing message when an SL/TP leg is on the WRONG side of
    ``ref_price``, else None. Shared guard for every bracket-leg writer.

    Why it matters: SL/TP legs fill at EXACTLY the leg price (``_exact_close`` —
    a deliberate operator decision). A leg on the profitable side of the market
    is already "hit" the instant it's stored — the risk enforcer fires it and
    the fill books at an untraded price → fake P&L. So an impossible leg must be
    REJECTED at write time, never accepted.

    Rules (ref = live LTP, caller falls back to entry price; ref <= 0 → None,
    fail open so a missing mark never blocks a legitimate leg):
      • BUY / long  : SL below ref, TP above ref
      • SELL / short: SL above ref, TP below ref
    """
    ref = _money_to_float(ref_price)
    if ref <= 0:
        return None
    act = str(getattr(action, "value", action) or "").upper()
    is_long = act == "BUY"
    if sl is not None:
        s = _money_to_float(sl)
        if s > 0:
            if is_long and s >= ref:
                return f"Stop Loss ₹{s:g} must be BELOW current price ₹{ref:.2f} for a BUY position."
            if not is_long and s <= ref:
                return f"Stop Loss ₹{s:g} must be ABOVE current price ₹{ref:.2f} for a SELL position."
    if tp is not None:
        t = _money_to_float(tp)
        if t > 0:
            if is_long and t <= ref:
                return f"Target ₹{t:g} must be ABOVE current price ₹{ref:.2f} for a BUY position."
            if not is_long and t >= ref:
                return f"Target ₹{t:g} must be BELOW current price ₹{ref:.2f} for a SELL position."
    return None


# Grace after the tick feed (re)connects. A deploy or a brief WS drop makes
# every instrument look quiet for a moment, and blocking trading for everyone
# at once is what got the earlier 60-second version removed in July 2026.
_FEED_WARM_GRACE_SEC = 60


def stale_feed_block_reason(
    *,
    symbol: str,
    price_age_sec: float | None,
    threshold_sec: int,
    feed_warm_age_sec: float | None,
    grace_sec: int = _FEED_WARM_GRACE_SEC,
) -> str | None:
    """Why this instrument can't be traded right now, or None to allow it.

    Allows the order when the feature is off (threshold 0), when there is no
    price age to judge by, and while the feed is still warming up after a
    reconnect.
    """
    if threshold_sec <= 0 or price_age_sec is None:
        return None
    if feed_warm_age_sec is not None and feed_warm_age_sec < grace_sec:
        return None
    if price_age_sec <= threshold_sec:
        return None
    return (
        f"{symbol}: price has not moved for {int(price_age_sec)}s "
        f"(limit {threshold_sec}s). Trading resumes on the next price update."
    )


def expiry_window_blocks_new_trade(
    *,
    expiry: Any,
    today: Any,
    days: int,
) -> bool:
    """Is `today` inside the contract's final `days` calendar days?

    Expiry day counts as one of them: 3 on a Thursday expiry covers Tue, Wed
    and Thu, so Monday is the last day a position can be opened. 0 days, no
    expiry, or an already-expired contract all mean "not this rule's business".
    """
    if days <= 0 or expiry is None or today is None:
        return False
    try:
        remaining = (expiry - today).days
    except TypeError:
        return False
    if remaining < 0:
        return False  # already expired — the contract-live guard owns that
    return remaining <= days - 1


async def validate(
    *,
    user: User,
    instrument: Instrument,
    segment_type: str,
    action: OrderAction,
    order_type: OrderType,
    product_type: ProductType,
    lots: float,
    quantity: float,
    price: Decimal,
    trigger_price: Decimal,
    is_amo: bool,
    is_squareoff: bool = False,
    placed_from: str = "WEB",
    expected_price: Decimal | None = None,
    bracket_sl: Decimal | None = None,
    bracket_tp: Decimal | None = None,
) -> ValidatedOrder:
    # 12) user status
    if user.status != UserStatus.ACTIVE:
        raise OrderRejectedError("Account is not active", code="ACCOUNT_INACTIVE")
    if not user.permissions.can_place_orders:
        raise OrderRejectedError("Order placement disabled for this account", code="PERMISSION_DENIED")

    # ── Batch independent async lookups to cut latency ────────────
    # Risk, netting settings, position tracker, and open position are all
    # independent — fire them in parallel instead of sequentially.
    is_expiry_day_now = bool(instrument.expiry and instrument.expiry == now_ist().date())

    async def _fetch_risk() -> dict[str, Any]:
        try:
            rp = await netting_service.get_effective_risk(str(user.id))
            return rp.get("settings", {}) if rp else {}
        except Exception:
            return {}

    # option_type drives whether the resolver applies the admin's per-side
    # Opt Buy / Opt Sell margin (Fixed ₹/lot, Times, %). `instrument.option_type`
    # is None for option rows whose field wasn't populated during the Zerodha
    # sync, so fall back to deriving CE/PE from the symbol suffix — exactly
    # like positions.py / matching_engine / position_service already do. Without
    # this an option SELL silently resolves the generic segment (Times) margin
    # and lets the user sell for a fraction of the configured Opt-Sell Fixed
    # margin (the mobile option-chain card "₹273 vs Fixed ₹25L" bug). Centralised
    # here so EVERY sell entry point (web terminal, mobile, option-chain card,
    # one-click) gets the same correct margin.
    _resolved_opt_type = instrument.option_type.value if instrument.option_type else None
    if _resolved_opt_type is None:
        _derived_opt_type = infer_instrument_type_from_symbol(instrument.symbol)
        if _derived_opt_type in ("CE", "PE"):
            _resolved_opt_type = _derived_opt_type

    async def _fetch_netting() -> dict[str, Any]:
        return await netting_service.get_effective_settings(
            user.id,  # type: ignore[arg-type]
            segment_type,
            action=action.value if hasattr(action, "value") else str(action),
            option_type=_resolved_opt_type,
            product_type=product_type.value if hasattr(product_type, "value") else str(product_type),
            is_expiry_day=is_expiry_day_now,
            symbol=instrument.symbol,
        )

    import logging as _logging
    import time as _time

    _vlog = _logging.getLogger(__name__)
    _t0 = _time.perf_counter()
    risk, resolved, tracker, open_position, ltp = await asyncio.gather(
        _fetch_risk(),
        _fetch_netting(),
        UserPositionTracker.find_one(
            UserPositionTracker.user_id == user.id,
            UserPositionTracker.segment_type == segment_type,
            UserPositionTracker.instrument_token == instrument.token,
        ),
        # IMPORTANT: lookup keys must match the matching_engine's
        # `existing_pos` query (services/matching_engine.py) — they
        # work as a pair (validator caps the order, matching_engine
        # merges the fill into the same position row). Previously the
        # validator filtered by `segment_type` while the engine filtered
        # by `product_type`, so a position opened under an older
        # segment-name spelling (e.g. CRYPTO_PERPETUAL renamed to
        # CRYPTO) was invisible to the validator's `signed_held` /
        # `projected_net` calc — every follow-up order saw "no existing
        # position", passed the `max_each_lot` cap on its own lots, and
        # the engine merrily pyramided into the existing row. End-user
        # symptom: lots stop at the per-order cap but the position size
        # keeps climbing past `maxLots/script` (the user-reported "size
        # badhate ja raha hai" bug).
        Position.find_one(
            Position.user_id == user.id,
            Position.instrument.token == instrument.token,
            Position.product_type == product_type,
            Position.status == PositionStatus.OPEN,
        ),
        market_data_service.get_ltp(instrument.token),
    )
    _vlog.info(
        "order_perf step=validate.gather5 ms=%.1f",
        (_time.perf_counter() - _t0) * 1000,
    )
    s: dict[str, Any] = resolved["settings"]

    # 1) Block gate — split into two flags so closing trades survive a
    # paused segment:
    #   • `is_active = false`   → segment is turned OFF entirely. The
    #     user side hides instruments from search and we reject EVERY
    #     order (including squareoff) here as a belt-and-braces check.
    #   • `trading_enabled = false` → new entries are blocked but the
    #     user can still see prices and CLOSE existing positions. The
    #     `is_reducing` / `is_squareoff` exemption fires below once
    #     those values are computed.
    if not bool(s.get("is_active", True)):
        raise SegmentNotAllowedError(
            f"Segment {segment_type} is disabled — contact your broker"
        )

    lot_size = max(1, instrument.lot_size or 1)

    # 2) lot limits — admin's segment settings are the single source of truth.
    # Squareoff orders are EXEMPT from the min-lot floor: a legacy position
    # opened before the Infoway lot-size table existed may have stored
    # `quantity = 0.99` against `lot_size = 1`; once the canonical heal
    # rewrites lot_size to 100 (XAUUSD = 100 oz/lot), the close path
    # recomputes `lots = quantity / lot_size = 0.0099` which would
    # otherwise trip the 0.01 minimum and leave the trader unable to
    # exit. Same exemption applies to admin-initiated force-closes
    # (kill switch, risk auto-flatten, EOD rollover) which all set
    # `is_squareoff=True`.
    min_lot = float(s.get("min_lot") or 1)
    order_lot = float(s.get("order_lot") or 0)  # per-order maximum (new positions only)
    if lots < min_lot and not is_squareoff:
        raise OrderRejectedError(f"Minimum {min_lot} lot(s) required", code="LOT_BELOW_MIN")

    # 3) position limits — running total per instrument + per segment
    #
    # `Position.quantity` is stored in CONTRACTS (lots × lot_size), while
    # the admin's caps and the user's `lots` field are in LOTS. Mixing
    # the two units meant a 1-lot follow-up on a NIFTY long (75 contracts
    # held) computed `projected_net = 75 + 1 = 76` and tripped the
    # 100-lot per-instrument cap as if the user already held 75 lots.
    # Convert to lots up-front so every downstream check (`is_reducing`,
    # `MAX_EACH_EXCEEDED`) compares apples to apples. We divide by the
    # POSITION's own stored lot_size (not the live instrument's) so a
    # legacy row where lot_size has since been corrected by the canonical
    # table still resolves to the same lot count it was opened with.
    held = tracker.total_lots if tracker else 0
    pos_lot_size = max(
        1,
        int(
            (getattr(open_position.instrument, "lot_size", 0) or lot_size)
            if open_position
            else lot_size
        ),
    )
    signed_held = (
        float(open_position.quantity) / pos_lot_size if open_position else 0.0
    )  # IN LOTS (signed: + long, − short, 0 flat)
    delta = float(lots) if action == OrderAction.BUY else -float(lots)
    projected_net = signed_held + delta
    is_reducing = abs(projected_net) < abs(signed_held)  # closing / partial close

    # ── Stop-trigger directional sanity (SL / SL-M) ───────────────────
    # A stop must sit on the side the market has to MOVE to reach it:
    #   • BUY stop  → trigger ABOVE the current price (fires on a rise).
    #   • SELL stop → trigger BELOW the current price (fires on a fall).
    # A wrong-side stop (e.g. a SELL SL-M with trigger 64,700 while BTCUSD is
    # 64,115) satisfies its trigger condition the instant it's parked, so the
    # poller fires it immediately and books the fill at the trigger — an
    # UNTRADED price → fake P&L (user-reported: SELL SL-M filling at 64,700 the
    # market never hit). This runs UNCONDITIONALLY — independent of
    # `limit_percentage` (which admins may set to 0, disabling the distance
    # band below) — because it's a correctness gate, not a distance policy.
    if (
        order_type in (OrderType.SL, OrderType.SL_M)
        and trigger_price is not None
        and trigger_price > 0
        and ltp is not None
        and ltp > 0
        and not is_squareoff
    ):
        if action == OrderAction.BUY and trigger_price <= ltp:
            raise OrderRejectedError(
                f"Buy stop trigger ₹{trigger_price} must be ABOVE the current price ₹{ltp:.2f}.",
                code="STOP_TRIGGER_WRONG_SIDE",
            )
        if action == OrderAction.SELL and trigger_price >= ltp:
            raise OrderRejectedError(
                f"Sell stop trigger ₹{trigger_price} must be BELOW the current price ₹{ltp:.2f}.",
                code="STOP_TRIGGER_WRONG_SIDE",
            )

    # ── Square-off / Exit must be REDUCE-ONLY ──────────────────────────
    # An Exit / square-off order exists only to FLATTEN existing exposure.
    # If there is NO open position, or the order is on the SAME side as the
    # held position (which would ADD to it), executing it opens / increases
    # a brand-new position — never what "Exit" means.
    #
    # Production incident CL66147834 (2026-06-24): user tapped Exit, the
    # long closed (SELL sq=true), then a slow-UI second Exit ~25 s later —
    # PAST the /squareoff endpoint's 10 s single-flight lock — landed as a
    # fresh SELL and opened an unwanted SHORT, which then had to be bought
    # back. Rejecting any non-closing square-off makes a duplicate / stale /
    # late Exit a safe no-op regardless of which path it arrives on.
    #
    # We deliberately gate on "no position / wrong side" rather than on
    # MAGNITUDE, so float lot-rounding on a legitimate FULL close can never
    # wrongly block a user from exiting. All system closers (risk enforcer,
    # admin force-close, EOD rollover, kill-switch) always send the correct
    # opposite side against a live open position, so they pass untouched.
    if is_squareoff:
        squareoff_adds_or_absent = (
            signed_held == 0 or (signed_held > 0) == (delta > 0)
        )
        if squareoff_adds_or_absent:
            raise OrderRejectedError(
                "No open position to close — square-off is reduce-only",
                code="SQUAREOFF_NO_POSITION",
            )

    # Per-order cap: only applies to NEW/opening orders — closing must always
    # be allowed in full so user can exit the entire position in one click.
    if not is_squareoff and not is_reducing and order_lot > 0 and lots > order_lot:
        raise OrderRejectedError(
            f"Maximum {order_lot} lot(s) per order", code="LOT_PER_ORDER_MAX"
        )

    # ── Risk: exit-only mode ───────────────────────────────────────
    # Admin freezes the account for new entries (e.g. during volatility,
    # margin warnings). Only reducing/closing trades remain allowed.
    if risk.get("exitOnlyMode") and not is_reducing:
        raise OrderRejectedError(
            "Exit-only mode is active — only closing trades are allowed",
            code="EXIT_ONLY_MODE",
        )

    # ── Expiry run-in: closing only in a contract's final days ─────
    # Segment setting: with 3 on a Thursday expiry, Tue / Wed / Thu accept
    # closes but no fresh positions and no added lots, so nobody is still
    # building exposure into settlement. Admin square-off and the risk
    # engine are exempt — they exist to flatten.
    _expiry_no_new_days = int(s.get("expiryNoNewTradeDays") or 0)
    if (
        _expiry_no_new_days > 0
        and not is_squareoff
        and not is_reducing
        and expiry_window_blocks_new_trade(
            expiry=instrument.expiry,
            today=now_ist().date(),
            days=_expiry_no_new_days,
        )
    ):
        raise OrderRejectedError(
            f"{instrument.symbol} expires on {instrument.expiry} — no new "
            f"positions in the last {_expiry_no_new_days} day(s). Closing or "
            "reducing what you hold still works.",
            code="EXPIRY_NO_NEW_TRADE",
        )

    # ── Settlement-pending gate ────────────────────────────────────
    # When `User.auto_settlement == False` and a debit has left the
    # wallet's available_balance below 0, `wallet_service` queues a
    # PENDING SettlementRequest. While that row exists the user is
    # blocked from new OPENING trades — only `is_reducing` (closing /
    # partial close) or `is_squareoff` (admin / risk auto-flatten)
    # orders pass through, mirroring the exit-only-mode exemption
    # pattern just above. Admin clears the block by approving the
    # request from Payments → Settlement Requests.
    if not is_reducing and not is_squareoff:
        try:
            from app.services import wallet_service as _ws

            if await _ws.has_pending_settlement_request(user.id):
                raise OrderRejectedError(
                    "Settlement pending — close existing positions or "
                    "wait for admin approval before opening new trades",
                    code="SETTLEMENT_PENDING",
                )
        except OrderRejectedError:
            raise
        except Exception:  # pragma: no cover
            # A Mongo hiccup on the probe must NOT block legitimate
            # orders. Fail-open is acceptable here because the
            # downstream `wallet_service.adjust` will still gate via
            # InsufficientFundsError if margin can't be sourced.
            pass

    # ── Upper / lower circuit (exchange daily price band) ───────────
    # A circuit is a DIRECTIONAL lock, not a halt: at the upper circuit only
    # SELL can execute, at the lower circuit only BUY. Blocking the scrip
    # outright would be wrong, and so would blocking the locked side
    # unconditionally — hence the exemption below.
    #
    # EXIT EXEMPTION (the whole point): a trader must ALWAYS be able to close.
    # The band stops you OPENING into a locked side; it must never become a
    # trap. `is_reducing` covers full AND partial closes (it compares
    # |projected_net| < |signed_held|, both in LOTS), and `is_squareoff`
    # covers the risk-engine / admin auto-flatten, which bypasses every gate.
    # The two cases this rescues are LONG-at-lower-circuit exiting via SELL
    # and SHORT-at-upper-circuit covering via BUY — both would otherwise be
    # rejected on the locked side, and they fail SILENTLY (nobody notices
    # until someone is trapped in a position).
    #
    # `signed_held` is read from the SAME position keys the matching engine
    # merges fills on (user_id + instrument.token + product_type + OPEN) —
    # verified identical; if they ever drift, `signed_held` reads 0 and this
    # exemption quietly stops firing.
    #
    # This is a B-book platform (fills are internal at LTP, never routed), so
    # honouring the exit is not just correct but actually executable.
    #
    # FAIL OPEN throughout: no band, dead feed, cold cache, LTP 0 → allowed.
    if settings.CIRCUIT_BANDS_ENABLED and not is_reducing and not is_squareoff:
        try:
            from app.services import circuit_service

            _lower, _upper = await circuit_service.get_circuit_band(instrument)
            if _lower is not None or _upper is not None:
                # The order's own price. LIMIT books at the user's price,
                # SL-M at its trigger; a MARKET order books at the live side,
                # which the ticket already sends as `expected_price` (the
                # exact bid/ask it displayed) — LTP is the fallback.
                if order_type == OrderType.MARKET:
                    _ref = (
                        expected_price
                        if expected_price is not None and expected_price > 0
                        else ltp
                    )
                elif order_type == OrderType.SL_M:
                    _ref = trigger_price if trigger_price and trigger_price > 0 else price
                else:
                    _ref = price
                _hit = circuit_service.evaluate(
                    action=action.value if hasattr(action, "value") else str(action),
                    order_type=(
                        order_type.value if hasattr(order_type, "value") else str(order_type)
                    ),
                    lower=_lower,
                    upper=_upper,
                    cur=to_decimal(ltp) if ltp else None,
                    ref_price=to_decimal(_ref) if _ref else None,
                )
                if _hit is not None:
                    _code, _message = _hit
                    # Logged on BOTH paths so a log-only rollout shows exactly
                    # which orders would start bouncing once ENFORCE flips on.
                    _vlog.warning(
                        "circuit_block user=%s symbol=%s action=%s code=%s "
                        "ltp=%s ref=%s lower=%s upper=%s enforce=%s",
                        user.id,
                        instrument.symbol,
                        getattr(action, "value", action),
                        _code,
                        ltp,
                        _ref,
                        _lower,
                        _upper,
                        settings.CIRCUIT_ENFORCE,
                    )
                    if settings.CIRCUIT_ENFORCE:
                        raise OrderRejectedError(_message, code=_code)
        except OrderRejectedError:
            raise
        except Exception:  # pragma: no cover
            # Band lookup / evaluation problem → treat as "no band". One
            # broker API hiccup must never halt the platform.
            _vlog.debug("circuit_gate_failed", exc_info=True)

    # ── Block gate (segment level): tradingEnabled = false ────────
    # Admin paused this segment. Existing positions can still be closed
    # (the user can exit their book) but no new entries are allowed.
    # `is_squareoff` covers admin-initiated kill-switch / risk auto-flatten
    # so those always pass through.
    if not bool(s.get("trading_enabled", True)) and not is_reducing and not is_squareoff:
        raise SegmentNotAllowedError(
            f"Segment {segment_type} is paused — only closing trades are allowed",
        )

    # ── Expiry-day hold timer ─────────────────────────────────────
    # Admin can force users to hold profitable / losing trades for a
    # minimum number of seconds on expiry day before allowing close.
    # The check fires only when:
    #   • This is a CLOSING order (`is_reducing`) and not an admin-driven
    #     squareoff (kill-switch / risk auto-flatten / EOD rollover are
    #     never gated by the user-facing hold timer).
    #   • The instrument expires today (matches the same predicate the
    #     resolver uses).
    #   • An open position exists with an `opened_at` timestamp we can
    #     compare against.
    # Zero means "no minimum" — opt out per segment.
    if (
        is_reducing
        and not is_squareoff
        and is_expiry_day_now
        and open_position is not None
        and getattr(open_position, "opened_at", None) is not None
    ):
        from app.utils.time_utils import now_utc

        try:
            held_sec = (now_utc() - open_position.opened_at).total_seconds()
            # Sign of the current position tells us whether the user is
            # closing a long (cur_qty > 0, P/L = ltp − avg) or a short
            # (cur_qty < 0, P/L = avg − ltp). Every money read uses
            # `_money_to_float` so a Decimal128 / Decimal / str payload
            # all coerce cleanly — the soft hold-timer must never crash
            # the entire order pipeline.
            avg = _money_to_float(getattr(open_position, "avg_price", 0))
            ltp_f = _money_to_float(ltp)
            qty_f = float(open_position.quantity or 0)
            if avg > 0 and ltp_f > 0:
                unrealized = (ltp_f - avg) if qty_f > 0 else (avg - ltp_f)
                min_hold = float(
                    s.get("expiry_profit_hold") if unrealized >= 0 else s.get("expiry_loss_holding")
                    or 0
                )
                if min_hold > 0 and held_sec < min_hold:
                    kind = "profit" if unrealized >= 0 else "loss"
                    raise OrderRejectedError(
                        f"Expiry-day {kind}-hold: this position must be held for "
                        f"{int(min_hold)} s before closing (held {int(held_sec)} s)",
                        code=f"EXPIRY_{kind.upper()}_HOLD",
                    )
        except OrderRejectedError:
            # Hold-timer rejection is a legitimate user-facing error —
            # bubble it through. Other exceptions are swallowed below.
            raise
        except Exception:
            _vlog.warning("expiry_hold_check_failed", exc_info=True)

    # ── Always-on hold timer (Risk Management settings) ─────────────
    # The two `*TradeHoldMinSeconds` knobs on the Risk Management page
    # apply to every user-initiated close, every day — not just expiry.
    # Admin uses these to prevent scalping (positive hold) or panic
    # closing (loss hold). Same exemptions as the expiry-day version
    # above: only fires on user-driven reductions, never on admin
    # squareoff. Zero = feature off.
    if (
        is_reducing
        and not is_squareoff
        and open_position is not None
        and getattr(open_position, "opened_at", None) is not None
    ):
        from app.utils.time_utils import now_utc

        try:
            held_sec_rs = (now_utc() - open_position.opened_at).total_seconds()
            avg_rs = _money_to_float(getattr(open_position, "avg_price", 0))
            ltp_rs = _money_to_float(ltp)
            qty_rs = float(open_position.quantity or 0)
            if avg_rs > 0 and ltp_rs > 0:
                unrealized_rs = (ltp_rs - avg_rs) if qty_rs > 0 else (avg_rs - ltp_rs)
                min_hold_rs = float(
                    (risk.get("profitTradeHoldMinSeconds") if unrealized_rs >= 0 else risk.get("lossTradeHoldMinSeconds"))
                    or 0
                )
                if min_hold_rs > 0 and held_sec_rs < min_hold_rs:
                    kind = "profit" if unrealized_rs >= 0 else "loss"
                    raise OrderRejectedError(
                        f"{kind.capitalize()} trade must be held for "
                        f"{int(min_hold_rs)} s before close (held {int(held_sec_rs)} s)",
                        code=f"{kind.upper()}_HOLD",
                    )
        except OrderRejectedError:
            raise
        except Exception:
            _vlog.warning("trade_hold_check_failed", exc_info=True)

    intra_limit = int(s.get("intraday_lot_limit") or 0)
    hold_limit = int(s.get("holding_lot_limit") or 0)
    max_each = int(s.get("max_each_lot") or 0)

    # Cap only applies when the order would INCREASE exposure on this script.
    # A risk/admin auto-flatten (`is_squareoff`) is ALWAYS an exit — it must
    # never be blocked by a position-size cap, or the stop-out can't fire and
    # the risk_enforcer hot-loops retrying it every 250 ms (observed in prod:
    # a 584-lot position stuck, "Per-instrument cap reached: would hold 584.1
    # > 10" spamming the logs + loading the backend while never flattening).
    # The exit gates above already exempt is_squareoff; these three position-
    # size caps were missing that same exemption.
    if max_each and not is_reducing and not is_squareoff and abs(projected_net) > max_each:
        raise OrderRejectedError(
            f"Per-instrument cap reached: would hold {abs(projected_net)} > {max_each}",
            code="MAX_EACH_EXCEEDED",
        )
    if (
        not is_reducing
        and not is_squareoff
        and product_type == ProductType.MIS
        and intra_limit
        and (tracker.intraday_lots if tracker else 0) + lots > intra_limit
    ):
        raise OrderRejectedError(f"Intraday lot limit {intra_limit} reached", code="INTRADAY_LIMIT")
    if (
        not is_reducing
        and not is_squareoff
        and product_type in (ProductType.NRML, ProductType.CNC)
        and hold_limit
        and (tracker.holding_lots if tracker else 0) + lots > hold_limit
    ):
        raise OrderRejectedError(f"Holding lot limit {hold_limit} reached", code="HOLDING_LIMIT")

    # 4) Limit-away check.
    #
    # Bounds every limit-style price the order carries by ±limit_pct of the
    # bid/ask of the side that will actually transact on that leg:
    #
    #   • Entry leg (LIMIT price / SL-M trigger): user is the maker — BUY
    #     orders reference the ask (the price they'd cross to fill); SELL
    #     orders reference the bid.
    #   • Bracket SL / target: the closing leg trades the opposite side,
    #     so a BUY (long) entry's SL/TP both reference the bid (close = SELL),
    #     and a SELL (short) entry's SL/TP reference the ask (close = BUY).
    #
    # Falls back to LTP when bid/ask are missing (off-hours / mock feed).
    # MARKET orders skip the entry-leg check (no user-priced field) but
    # their bracket prices are still validated.

    # ── 4a) Bracket SL / TP directional sanity ─────────────────────
    # Independent of `limit_pct` (which can be 0 in some segments). Without
    # this, a user could type TP=95 on a LONG at 100 and the risk-enforcer
    # would immediately square-off the position the moment the order fills
    # because the trigger condition `ltp >= tp` would be true on every
    # tick. Same for SL on the wrong side: trigger condition becomes
    # impossible and the bracket never fires. The check uses the entry
    # price the order is actually going to land at:
    #   • LIMIT / SL-M → user-entered price / trigger.
    #   • MARKET       → live close-side quote (ask for BUY, bid for SELL),
    #                    falling back to LTP.
    limit_pct = float(s.get("limit_percentage") or 0)
    if limit_pct > 0 and ltp > 0:
        try:
            _quote = await market_data_service.get_quote(instrument.token)
            _bid_raw = _quote.get("bid")
            _ask_raw = _quote.get("ask")
            _bid = to_decimal(_bid_raw) if _bid_raw not in (None, 0, "0") else None
            _ask = to_decimal(_ask_raw) if _ask_raw not in (None, 0, "0") else None
        except Exception:
            _bid = _ask = None

        def _market_ref(side_word: str) -> Decimal:
            # BUY-leg reference is the ask (you'd cross the spread upward
            # to fill), SELL-leg is the bid. Fall back to LTP when bid/ask
            # are missing.
            if side_word == "BUY":
                return _ask if _ask is not None and _ask > 0 else ltp
            return _bid if _bid is not None and _bid > 0 else ltp

        def _check_entry(name: str, ref: Decimal, candidate: Decimal | None) -> None:
            # limit_pct = MAX allowed distance from market (admin label: "Max % away
            # from market"). Orders placed OUTSIDE the band are rejected.
            if candidate is None or candidate <= 0:
                return
            if ref is None or ref <= 0:
                return
            upper = ref * to_decimal(1 + limit_pct / 100)
            lower = ref * to_decimal(1 - limit_pct / 100)
            if candidate < lower or candidate > upper:
                raise OrderRejectedError(
                    f"{name} ₹{candidate} is too far from market ₹{ref}. "
                    f"Must be within {limit_pct}% (between ₹{lower:.2f} and ₹{upper:.2f}).",
                    code=f"{name.upper().replace(' ', '_')}_TOO_FAR",
                )

        def _check_sl_tp(name: str, ref: Decimal, candidate: Decimal | None) -> None:
            # For SL/TP: limit_pct is a MINIMUM distance from entry.
            # SL/TP placed inside the band (too close to entry) are rejected.
            if candidate is None or candidate <= 0:
                return
            if ref is None or ref <= 0:
                return
            upper = ref * to_decimal(1 + limit_pct / 100)
            lower = ref * to_decimal(1 - limit_pct / 100)
            if lower < candidate < upper:
                raise OrderRejectedError(
                    f"{name} ₹{candidate} is too close to entry ₹{ref}. "
                    f"Must be at least {limit_pct}% away "
                    f"(≤ ₹{lower:.2f} for sell-side or ≥ ₹{upper:.2f} for buy-side).",
                    code=f"{name.upper().replace(' ', '_')}_TOO_CLOSE",
                )

        entry_side = "BUY" if action == OrderAction.BUY else "SELL"
        entry_ref = _market_ref(entry_side)

        # Entry: max distance from market
        if order_type != OrderType.MARKET:
            _check_entry("limit price", entry_ref, price)
            _check_entry("trigger price", entry_ref, trigger_price)

        # SL/TP: min distance from entry price
        bracket_ref = price if (order_type != OrderType.MARKET and price and price > 0) else (
            _market_ref("SELL" if action == OrderAction.BUY else "BUY")
        )
        _check_sl_tp("stop loss", bracket_ref, bracket_sl)
        _check_sl_tp("target", bracket_ref, bracket_tp)

    # ── Block parked/pending orders for this segment ──────────────────
    # When on, the segment accepts only MARKET entries + exits — every
    # LIMIT / SL / SL-M (parked) order is refused. Square-off is exempt so a
    # position can always be closed.
    if (
        s.get("block_pending_orders")
        and order_type != OrderType.MARKET
        and not is_squareoff
    ):
        raise OrderRejectedError(
            "Pending orders (LIMIT / SL) are blocked for this segment. "
            "Only market orders are accepted here.",
            code="PENDING_ORDERS_BLOCKED",
        )

    # ── Block a parked LIMIT/SL resting INSIDE today's traded range ────
    # When on, a parked order must sit ABOVE the day high or BELOW the day
    # low — never between (a level the instrument already traded through, so
    # it fills on the next wobble rather than a real move). Independent of
    # limitAwayPercent (a % band around the current price) — both apply.
    # Exemptions are deliberate: a MARKET order fills at the touch; a
    # square-off must always be able to exit (else the stop-out hot-loops
    # retrying a blocked order); and an unknown range (pre-open / no OHLC)
    # stands aside rather than refusing everything.
    if (
        s.get("limit_within_day_range")
        and order_type != OrderType.MARKET
        and not is_squareoff
    ):
        try:
            _dq = await market_data_service.get_quote(instrument.token)
            _hi_raw, _lo_raw = _dq.get("high"), _dq.get("low")
            _hi = to_decimal(_hi_raw) if _hi_raw not in (None, 0, "0") else None
            _lo = to_decimal(_lo_raw) if _lo_raw not in (None, 0, "0") else None
        except Exception:  # noqa: BLE001
            _hi = _lo = None
        if _hi and _lo and _hi > 0 and _lo > 0 and _hi >= _lo:
            for _name, _cand in (("Limit price", price), ("Trigger price", trigger_price)):
                if _cand is None or _cand <= 0:
                    continue
                if _lo <= _cand <= _hi:
                    raise OrderRejectedError(
                        f"{_name} ₹{_cand} is inside today's range (₹{_lo} – ₹{_hi}). "
                        f"This segment only accepts orders above the high or below the low.",
                        code="INSIDE_DAY_RANGE",
                    )

    # ── Per-segment SL / TP enable gate ───────────────────────────────
    # Admin can turn OFF SL and/or TP for a segment (Limit away → SL/TP
    # on-off). Reject an order that tries to attach the disabled leg so a
    # user can't open a bracket the segment doesn't allow. Independent
    # knobs: SL can be blocked while TP stays allowed and vice-versa.
    if bracket_sl is not None and bracket_sl > 0 and not s.get("sl_enabled", True):
        raise OrderRejectedError(
            "Stop Loss is disabled for this segment.",
            code="SL_DISABLED",
        )
    if bracket_tp is not None and bracket_tp > 0 and not s.get("tp_enabled", True):
        raise OrderRejectedError(
            "Target (Take Profit) is disabled for this segment.",
            code="TP_DISABLED",
        )

    # SL / TP directional check — simple, always-on guard.
    # BUY:  SL must be below entry, TP must be above entry.
    # SELL: SL must be above entry, TP must be below entry.
    # Reference is the user's limit price for LIMIT orders, LTP for MARKET.
    _dir_ref = price if (order_type != OrderType.MARKET and price and price > 0) else ltp
    if _dir_ref and _dir_ref > 0:
        if bracket_sl is not None and bracket_sl > 0:
            if action == OrderAction.BUY and bracket_sl >= _dir_ref:
                raise OrderRejectedError(
                    f"Stop Loss ₹{bracket_sl} must be BELOW entry ₹{_dir_ref} for a BUY order.",
                    code="SL_WRONG_SIDE",
                )
            if action == OrderAction.SELL and bracket_sl <= _dir_ref:
                raise OrderRejectedError(
                    f"Stop Loss ₹{bracket_sl} must be ABOVE entry ₹{_dir_ref} for a SELL order.",
                    code="SL_WRONG_SIDE",
                )
        if bracket_tp is not None and bracket_tp > 0:
            if action == OrderAction.BUY and bracket_tp <= _dir_ref:
                raise OrderRejectedError(
                    f"Target ₹{bracket_tp} must be ABOVE entry ₹{_dir_ref} for a BUY order.",
                    code="TP_WRONG_SIDE",
                )
            if action == OrderAction.SELL and bracket_tp >= _dir_ref:
                raise OrderRejectedError(
                    f"Target ₹{bracket_tp} must be BELOW entry ₹{_dir_ref} for a SELL order.",
                    code="TP_WRONG_SIDE",
                )

    # Hard-cap: reject LIMIT prices > 50% away from LTP regardless of
    # limit_percentage setting. Prevents phantom fills caused by typos
    # or a momentarily zero/stale LTP from triggering 90%-off orders.
    _MAX_LIMIT_DEV_PCT = to_decimal("50")
    if ltp and ltp > 0 and order_type != OrderType.MARKET:

        def _hard_cap_check(cap_name: str, candidate: Decimal | None) -> None:
            if candidate is None or candidate <= 0:
                return
            dev = abs(candidate - ltp) / ltp * 100
            if dev > _MAX_LIMIT_DEV_PCT:
                raise OrderRejectedError(
                    f"{cap_name} ₹{candidate} deviates {dev:.1f}% from current market "
                    f"₹{ltp}. Maximum allowed deviation is {_MAX_LIMIT_DEV_PCT}%.",
                    code=f"{cap_name.upper().replace(' ', '_')}_TOO_FAR",
                )

        _hard_cap_check("limit price", price)
        _hard_cap_check("trigger price", trigger_price)

    # 5) strike difference (only for option segments)
    strike_diff = int(s.get("strike_difference") or 0)
    if strike_diff > 0 and instrument.strike is not None and "OPTION" in segment_type.upper():
        underlying = await Instrument.find_one(
            Instrument.token == (instrument.underlying_token or "")
        )
        if underlying is not None:
            spot = await market_data_service.get_ltp(underlying.token)
            atm = round(float(spot) / strike_diff) * strike_diff
            strike_val = float(to_decimal(instrument.strike))
            steps = abs(strike_val - atm) // strike_diff
            max_steps = int(s.get("strike_difference") or 5)
            if steps > max_steps:
                raise OrderRejectedError(
                    f"Strike too far from ATM ({int(steps)} > {max_steps})", code="STRIKE_OUT_OF_RANGE"
                )

    # 6) OTM extra-strict cap
    # Skipped for closing / squareoff orders — those REDUCE existing
    # exposure, they don't add a new OTM bet. Risk auto-squareoff on
    # a stop-out used to silently fail in an infinite retry loop here
    # ("OTM cap 5 reached") because the user already had > otm_max
    # lots open and the enforcer kept trying to add more by routing
    # the close as a regular fill. Same exemption every other lot/qty
    # cap above already gives via `is_squareoff` / `is_reducing`.
    otm_max = int(s.get("otm_max_each_lot") or 0)
    if (
        otm_max
        and "OPTION" in segment_type.upper()
        and instrument.option_type
        and not is_squareoff
        and not is_reducing
    ):
        # Heuristic: rely on max_each_lot already handling general cap; here we tighten
        if (held + lots) > otm_max:
            raise OrderRejectedError(f"OTM cap {otm_max} reached", code="OTM_CAP_EXCEEDED")

    # 6a-i) minimum quantity check (equity segments that use qty instead of lots)
    min_qty = float(s.get("min_qty") or 0)
    if min_qty > 0 and quantity < min_qty:
        raise OrderRejectedError(
            f"Minimum quantity is {min_qty}, got {quantity}",
            code="QTY_BELOW_MIN",
        )

    # 6a) per-order quantity cap (relevant mostly for equity segments).
    # Skip for reducing/closing orders — those exit existing exposure.
    per_order_qty = float(s.get("per_order_qty") or 0)
    if not is_reducing and per_order_qty > 0 and quantity > per_order_qty:
        raise OrderRejectedError(
            f"Quantity {quantity} exceeds per-order cap of {per_order_qty}",
            code="QTY_PER_ORDER_EXCEEDED",
        )

    # 6b) running total quantity per script (running held + new ≤ cap)
    max_qty_script = float(s.get("max_qty_per_script") or 0)
    if not is_reducing and max_qty_script > 0:
        held_qty = (tracker.total_lots if tracker else 0) * lot_size
        if held_qty + quantity > max_qty_script:
            raise OrderRejectedError(
                f"Per-script quantity cap {max_qty_script} would be breached "
                f"(held {held_qty} + new {quantity})",
                code="MAX_QTY_PER_SCRIPT",
            )

    # 6c) per-order notional cap (₹ value) — skip for closing orders
    max_value = float(s.get("max_value") or 0)
    if not is_reducing and max_value > 0:
        ref_price_for_value = price if price > 0 else ltp
        notional_check = quantity * float(ref_price_for_value)
        if notional_check > max_value:
            raise OrderRejectedError(
                f"Order value ₹{notional_check:,.0f} exceeds per-order cap of ₹{max_value:,.0f}",
                code="MAX_VALUE_EXCEEDED",
            )

    # 6d) Option-leg strike-distance from spot. Single percent cap from
    # admin's Options column — same value gates both BUY and SELL legs.
    # The option-chain dialog filters the same way (strikes outside this
    # band aren't shown to the user) so anything that reaches this check
    # is the result of a deliberate token-paste, not normal click flow.
    far_pct = float(s.get("strike_far_percent") or 0)
    if (
        far_pct > 0
        and "OPTION" in segment_type.upper()
        and instrument.strike is not None
        and instrument.underlying_token
    ):
        underlying = await Instrument.find_one(Instrument.token == instrument.underlying_token)
        if underlying is not None:
            spot = float(await market_data_service.get_ltp(underlying.token))
            if spot > 0:
                strike_val = float(to_decimal(instrument.strike))
                deviation_pct = abs(strike_val - spot) / spot * 100
                if deviation_pct > far_pct:
                    raise OrderRejectedError(
                        f"Strike {strike_val:.0f} is {deviation_pct:.1f}% from spot {spot:.2f} "
                        f"— cap is {far_pct:.1f}%",
                        code="STRIKE_FAR_CAP",
                    )

    # 7) overnight selling
    if not s.get("selling_overnight", True) and action == OrderAction.SELL and product_type != ProductType.MIS:
        # Only block if user has no current long position to cover
        if not tracker or tracker.holding_lots <= 0:
            raise OrderRejectedError(
                "Overnight short selling is disabled for your account", code="NO_OVERNIGHT_SHORT"
            )

    # 8) expiry-day rules
    # Two knobs on the segment matrix drive this:
    #   • `expiry_intraday_margin` (and the OPTION-buy/sell variants
    #     picked earlier in the resolver) → the value to use today.
    #   • `expiry_margin_as_percent` → when False the value above is a
    #     flat ₹/lot (mirrors `margin_calc_mode = fixed`); when True
    #     it's % of notional / a leverage multiplier (interpretation
    #     follows the segment's `margin_calc_mode`). Lets admin run
    #     normal trading on, say, Times-leverage and still impose a
    #     punitive flat ₹ on expiry.
    # Use `effective_expiry` so instruments whose stored `expiry` is None
    # (data-quality gap from Zerodha sync) still get the expiry-day rule
    # applied based on a symbol-derived fallback date. Without this,
    # rows like CRUDEOIL26JULFUT with a null `expiry` skipped the rule
    # entirely and traded at the regular tier on their actual expiry day.
    from app.services.instrument_service import effective_expiry as _effective_expiry

    _expiry = _effective_expiry(instrument)
    is_expiry_today = bool(_expiry and _expiry == now_ist().date())
    if is_expiry_today:
        expiry_margin = float(
            s.get("expiry_intraday_margin")
            or s.get("margin_percentage")
            or s.get("leverage")
            or 100.0
        )
        expiry_as_percent = bool(s.get("expiry_margin_as_percent", True))
        seg_mode = (s.get("margin_calc_mode") or "").lower()
        if not expiry_as_percent:
            # Admin explicitly opted for flat ₹/lot on expiry — switch
            # the calc into fixed mode regardless of segment-default mode.
            s["margin_calc_mode"] = "fixed"
            s["fixed_margin_per_lot"] = expiry_margin
            s["margin_percentage"] = 0.0
            s["leverage"] = 1.0
        elif seg_mode == "times":
            # Times segment + percent expiry knob → admin's number is a
            # LEVERAGE multiplier, same units as `intradayMargin` in Times
            # mode (e.g. 500 means 500×). Previously the code force-set
            # `leverage = 1` and stuffed the multiplier into
            # `margin_percentage`, which turned a 500× setting into "500%
            # of notional × ÷ 1" — i.e. 5× the notional locked. On a
            # ₹10L crude lot that was ₹51L margin required and every
            # expiry-day order failed with InsufficientFunds. Now we
            # preserve the Times semantics so the user pays the same
            # margin tier on expiry day unless admin explicitly changed
            # `expiryDayIntradayMargin` to a stricter value.
            s["leverage"] = max(1.0, expiry_margin)
            s["margin_percentage"] = 100.0
            s["fixed_margin_per_lot"] = 0.0
        elif seg_mode == "fixed":
            # Fixed segment + percent expiry knob → ambiguous (admin
            # chose Fixed ₹/lot but didn't flip the `as_percent` flag).
            # Honour the existing fixed semantics: the expiry value is
            # ₹/lot.
            s["margin_calc_mode"] = "fixed"
            s["fixed_margin_per_lot"] = expiry_margin
            s["margin_percentage"] = 0.0
            s["leverage"] = 1.0
        else:
            # Legacy "percent" mode (or unknown) → preserve the original
            # behaviour: value is a % of notional with leverage 1.
            s["margin_percentage"] = expiry_margin
            s["leverage"] = 1.0
            s["fixed_margin_per_lot"] = 0.0

    # 9) margin check (all-Decimal arithmetic — never mix Decimal × float)
    margin_pct = to_decimal(s.get("margin_percentage") or 100.0) / to_decimal(100)
    leverage = to_decimal(s.get("leverage") or 1.0)
    if leverage <= 0:
        leverage = to_decimal(1)
    # Reference price for the notional / margin calc, in priority order:
    #   1. LIMIT / SL — user-entered price.
    #   2. `expected_price` from the order panel (the BUY/SELL price the
    #      user clicked). Same value the matching engine fills at, so the
    #      wallet locks exactly what the user will be charged.
    #   3. Live ask (BUY) or bid (SELL) — the actual fill side.
    #   4. LTP fallback for off-hours / missing depth.
    if price > 0:
        ref_price = price
    elif expected_price is not None and expected_price > 0:
        ref_price = expected_price
    else:
        try:
            quote = await market_data_service.get_quote(instrument.token)
            bid_raw = quote.get("bid")
            ask_raw = quote.get("ask")
            bid = to_decimal(bid_raw) if bid_raw not in (None, 0, "0") else None
            ask = to_decimal(ask_raw) if ask_raw not in (None, 0, "0") else None
        except Exception:
            bid = ask = None
        if action == OrderAction.BUY and ask is not None and ask > 0:
            ref_price = ask
        elif action == OrderAction.SELL and bid is not None and bid > 0:
            ref_price = bid
        else:
            ref_price = ltp
    notional = to_decimal(quantity) * ref_price
    # Fixed-margin segments skip the notional × pct ÷ leverage formula
    # entirely — the admin's configured value is a flat ₹/lot charged
    # once per lot. Anything else falls into the standard percent/times
    # path. (`margin_pct` is already 100% with the `leverage` set for
    # times mode, and is the literal percent for legacy percent mode.)
    fixed_per_lot = to_decimal(s.get("fixed_margin_per_lot") or 0)
    is_fixed_mode = (s.get("margin_calc_mode") == "fixed") and fixed_per_lot > 0
    if is_fixed_mode:
        margin_required = to_decimal(lots) * fixed_per_lot
    else:
        margin_required = notional * margin_pct / leverage

    # USD-quoted instruments (Infoway: crypto / forex / metals / energy) price
    # `ref_price` in dollars — the wallet runs in INR, so the margin we lock
    # must be in INR too. Convert the percent/times margin (computed in native
    # USD) via the live USD/INR rate; fixed-per-lot is admin-entered in INR
    # already, so it's NOT converted. We compute the rate UNCONDITIONALLY so the
    # over-leverage guard below can also put the notional in INR for a same-
    # currency ratio (native-INR segments use rate = 1).
    inst_segment = str(getattr(instrument.segment, "value", instrument.segment) or "")
    _is_usd_seg = bool(
        market_data_service.is_usd_quoted_segment(segment_type)
        or market_data_service.is_usd_quoted_segment(inst_segment)
    )
    usd_inr = to_decimal(market_data_service.get_usd_inr_rate()) if _is_usd_seg else to_decimal(1)
    if _is_usd_seg and not is_fixed_mode:
        margin_required = margin_required * usd_inr

    # ── Zero / negative-margin backstop ──────────────────────────────────────
    # 2026-07-09: the per-notional LEVERAGE CEILING was removed at the operator's
    # explicit request. The Fixed ₹/lot margin system is INTENTIONAL — admins set
    # flat per-lot margins (e.g. ₹80–₹800) that imply high effective leverage on
    # expensive instruments (BTCUSD etc.), and that high leverage is the
    # platform's deliberate B-book design, not a misconfiguration. So we no
    # longer reject an order merely because the locked margin is small relative
    # to notional. (Earlier this blocked every Fixed-mode crypto order with
    # "Margin calculation error"; operator: "fixed lot system work nahi kar raha,
    # is guard ko hata do".)
    #
    # What we STILL block: a computed margin of ZERO or NEGATIVE. That is never a
    # valid tier — it means fixed_per_lot / lots / leverage / price resolved to 0
    # (a genuinely broken config, NOT a leverage choice). A 0-margin position
    # pins the stop-out denominator (available + used_margin + credit_limit) at
    # 0, which silently DISABLES stop-out and leaves loss-making exposure with no
    # capital backing it (CL45900793). This one piece stays.
    notional_inr = (notional * usd_inr) if _is_usd_seg else notional
    if (
        not is_reducing
        and not is_squareoff
        and notional_inr > 0
        and margin_required <= to_decimal(0)
    ):
        _vlog.error(
            "order_zero_margin user=%s symbol=%s segment=%s notional_inr=%s "
            "margin=%s leverage=%s margin_pct=%s fixed=%s ref_price=%s",
            str(user.id), instrument.symbol, segment_type, str(notional_inr),
            str(margin_required), str(leverage), str(margin_pct), str(is_fixed_mode), str(ref_price),
        )
        raise OrderRejectedError(
            "Margin calculation error for this instrument — order blocked for "
            "your safety. Please contact support.",
            code="MARGIN_SANITY_FLOOR",
        )
    wallet = await wallet_service.get_or_create(user.id)  # type: ignore[arg-type]
    # Buying power = real cash + admin credit_limit + bonus credit pool. Bonus
    # credit (Bonus Management) is usable as opening margin headroom exactly
    # like credit_limit — it's never spent here, just lets the user lock margin
    # they don't have the cash for; a later loss drains real balance first, then
    # the bonus (see wallet_service loss hook). 0 when bonuses are off → inert.
    available = (
        to_decimal(wallet.available_balance)
        + to_decimal(wallet.credit_limit)
        + to_decimal(getattr(wallet, "credit", 0))
    )
    # Closing/reducing orders don't lock new margin — they free it up — so
    # skip the funds + utilisation cap checks for them.
    if is_reducing or is_squareoff:
        margin_required = to_decimal(0)
    else:
        # ── Zero-capital opening guard (root-cause companion to the
        # risk_enforcer zero-capital stop-out) ──────────────────────────
        # A user whose available_balance + credit_limit is exhausted
        # (realised losses floored the wallet to 0, overflow parked in
        # settlement_outstanding) must NOT be able to open fresh exposure.
        # Without this, an opening order whose computed margin happened to
        # be 0 (fixed_per_lot = 0, margin_percentage = 0, or a stale
        # leverage config) sailed past the `margin_required > available`
        # check below — `0 > 0` is False — and created a position with
        # margin_used = 0. That is the exact state that left CL45900793
        # holding loss-making exposure with NO capital backing it and the
        # stop-out denominator (available + used_margin + credit_limit)
        # pinned at 0, which silently disabled stop-out for the account.
        if available <= to_decimal(0):
            raise InsufficientFundsError(
                "No funds available to open a new position. "
                "Close existing positions or add funds."
            )
        if margin_required > available:
            raise InsufficientFundsError(
                f"Need ₹{margin_required:.2f}, have ₹{available:.2f}"
            )

    # 10) stop-loss mandatory
    if s.get("stop_loss_mandatory") and order_type not in (OrderType.SL, OrderType.SL_M):
        raise OrderRejectedError("Stop-loss is mandatory for this segment", code="SL_MANDATORY")

    # 11) market hours (skip for AMO and 24×7 segments)
    seg_upper = (segment_type or "").upper()
    exch_upper = str(getattr(instrument.exchange, "value", instrument.exchange) or "").upper()
    is_24x7 = "CRYPTO" in seg_upper or exch_upper == "CRYPTO"  # crypto runs 24×7
    # Forex + spot metals (XAU/XAG…) + energy (USOIL/UKOIL/NATGAS) all
    # follow the international 24×5 calendar — closed only on weekends.
    # They all sit on the virtual `CDS` exchange in our catalogue.
    is_24x5 = (
        "FOREX" in seg_upper
        or "FX" in seg_upper
        or "COMMODITIES" in seg_upper
        or "CDS" in seg_upper
        or exch_upper == "CDS"
    )
    is_mcx = "MCX" in seg_upper  # MCX has its own hours (~09:00-23:30 IST)

    # Only SERVER/admin force-closes bypass the market-hours guard — risk
    # auto-flatten (RISK_ENFORCER), admin "Close" (ADMIN / ADMIN_KILL_SWITCH),
    # and EOD rollover (OVERNIGHT_DISABLED_CLOSE / INTRADAY_ROLLOVER) must be
    # able to flatten a runaway position any time. A USER-initiated exit
    # (placed_from == "WEB") is NOT exempt: closing at a stale weekend/holiday
    # price is the same unfair "buy blocked but sell goes through" gap the
    # operator flagged (CL59347510 sold KIRLOSENG on a closed Saturday). User
    # exits now respect market hours exactly like a user BUY.
    _is_force_close = is_squareoff and (placed_from or "WEB").upper() != "WEB"
    # Admin-placed orders (Admin → Market Watch "place-orders", placed_from
    # =="ADMIN") bypass the market-hours guard entirely — the operator can
    # deliberately open OR close a trade off-hours / on a weekend / holiday,
    # at the live LTP (MARKET) or their own price (MANUAL). This is an
    # authenticated, scope-checked operator action, unlike a user's WEB order
    # which stays gated (the stale-price fairness guard below is for USERS).
    _is_admin_placed = (placed_from or "WEB").upper() == "ADMIN"
    if not is_amo and not is_24x7 and not _is_force_close and not _is_admin_placed:
        ist = now_ist()

        # 24×5 (forex / metals / energy): closed only on weekends (Sat full-day;
        # Sun close before 17:30 ET ≈ 03:00 IST Mon)
        if is_24x5:
            wd = ist.weekday()  # Mon=0 ... Sun=6
            if wd == 5 or (wd == 6 and ist.hour < 4):
                raise MarketClosedError("Forex market is closed for the weekend.")
        else:
            if is_weekend(ist.date()):
                raise MarketClosedError("Market is closed (weekend). Place AMO instead.")
            # holiday lookup (only Indian exchanges)
            h = await TradingHoliday.find_one(
                TradingHoliday.holiday_date == ist.date(),
                TradingHoliday.exchange == instrument.exchange,
            )
            if h is not None:
                if h.is_full_day:
                    raise MarketClosedError(f"Holiday: {h.description}. Place AMO instead.")
                # Partial-day holiday with a special session window — e.g. MCX
                # runs an EVENING-only session (17:00–23:30) on a day NSE/BSE
                # are fully closed. We honour the admin-set open_time/close_time
                # (stored as "HH:MM" strings on TradingHoliday) so trades are
                # blocked OUTSIDE that window — and the snapshot-fed feed gate
                # can't leak a pre-session order. A row with is_full_day=false +
                # open_time="17:00" → blocked till 17:00, open after, no manual
                # toggle. Malformed time strings fail-open (defensive parse).
                _ot = _ct = None
                try:
                    if h.open_time:
                        _ot = parse_hhmm(h.open_time)
                    if h.close_time:
                        _ct = parse_hhmm(h.close_time)
                except Exception:
                    _ot = _ct = None
                if _ot is not None and ist.time() < _ot:
                    raise MarketClosedError(
                        f"{h.description}: session opens at {h.open_time} IST. Place AMO instead."
                    )
                if _ct is not None and ist.time() > _ct:
                    raise MarketClosedError(
                        f"{h.description}: session closed at {h.close_time} IST."
                    )

            from app.core.config import settings as cfg

            if is_mcx:
                # MCX: 09:00 – 23:30 IST (winter) / 23:55 IST (summer evening session)
                # We use a generous 09:00 – 23:30 window.
                from datetime import time as _t

                if not (_t(9, 0) <= ist.time() <= _t(23, 30)):
                    raise MarketClosedError("MCX is closed. Place AMO instead.")
            else:
                # NSE / BSE equities + F&O: 09:15 – 15:30 IST
                open_t = parse_hhmm(cfg.MARKET_OPEN_TIME)
                close_t = parse_hhmm(cfg.MARKET_CLOSE_TIME)
                if not (open_t <= ist.time() <= close_t):
                    raise MarketClosedError("Market is closed. Place AMO instead.")

        # ── 11a-2) Open settle-buffer (stale-first-tick protection) ──────
        # We're confirmed in-session above (not weekend/holiday, inside the
        # clock window). But right AT the bell the first packet can carry a
        # stale/unsettled price with a FRESH timestamp, which §11b below would
        # wave through — that's exactly how the 2026-07-08 CRUDEOIL 6767 fill
        # slipped in. Refuse NEW user orders for the first
        # _OPEN_SETTLE_BUFFER_SEC after the daily open so the opening price
        # establishes first. Forex/crypto self-exempt (no daily bell →
        # is_within_open_grace returns False); AMO + SERVER force-close are
        # already excluded by the outer guard. See _OPEN_SETTLE_BUFFER_SEC.
        if is_within_open_grace(segment_type, _OPEN_SETTLE_BUFFER_SEC, ist):
            _open_t = market_open_time_for_segment(segment_type)
            _resume = ""
            if _open_t is not None:
                _resume_t = (
                    datetime.combine(ist.date(), _open_t)
                    + timedelta(seconds=_OPEN_SETTLE_BUFFER_SEC)
                ).time()
                _resume = f" Trading resumes {_resume_t.strftime('%H:%M')} IST."
            raise MarketClosedError(
                f"{instrument.symbol}: market just opened — opening prices are "
                f"still settling.{_resume} This protects you from a stale "
                f"opening tick. Place an AMO order to queue, or try again "
                f"in a few seconds."
            )

        # ── 11b) Live-tick sanity (catches late opens / mid-session halts)
        #
        # The static clock-based checks above can't catch the case where
        # the exchange schedule SAYS the market is open but the session
        # hasn't actually started (or has stopped mid-day for a halt).
        # 28-May 2026: MCX was scheduled 09:00 IST but the exchange
        # didn't release the feed until 17:00; orders went through at
        # zero/stale prices for 8 hours, costing the admin real money.
        #
        # This guard belts-and-braces the wall-clock check by requiring
        # at least one fresh tick from Zerodha for THIS specific
        # instrument within the last `_TICK_MAX_AGE_SEC` seconds. During
        # an active session Kite streams continuously, so a 60-second
        # gap is a strong "session not live" signal. The threshold is
        # generous enough to tolerate brief WS hiccups without false-
        # rejecting legitimate orders.
        #
        # This probe reads ZERODHA's last-tick age, so it ONLY applies to
        # Zerodha-fed segments (NSE / BSE / NFO / BFO / MCX). Infoway-fed
        # segments are exempt — Zerodha never ticks them, so the probe is
        # always None and would false-reject every order even with a live
        # price on the chart:
        #   • crypto (24×7) — already excluded by the outer guard.
        #   • forex / spot metals (XAUUSD…) / energy (24×5 → is_24x5).
        # 05-Jun-2026: XAUUSD BUY was blocked with "No live prices" despite
        # a live Infoway chart + bid/ask — this `not is_24x5` guard fixes it.
        # The wall-clock weekend check (11a) still gates these 24×5 markets.
        if not is_24x5:
            import time as _vt

            from app.services.market_data_service import _read_mdlive
            from app.services.zerodha_service import zerodha as _zerodha_for_tick_check

            # ── PRIMARY gate: the EXCHANGE's own packet age (snapshot-proof) ──
            # The wall-clock window above says MCX is "open" from 09:00, but on
            # a day MCX only runs the evening session (e.g. an NSE holiday) the
            # exchange sends nothing until 17:00 — EXCEPT a stale snapshot Kite
            # resends on every (re)subscribe, which keeps `received_at` fresh and
            # let orders through at last-session prices (28-May incident). The
            # exchange's OWN `exchange_timestamp` is not fooled: on a closed
            # session it's the PREVIOUS session's time (hours old). So if a fresh
            # exchange timestamp exists, the session is genuinely live; if it's
            # hours stale, the session is closed regardless of the clock. This
            # auto-handles every holiday / late-open / evening-only session with
            # zero calendar maintenance.
            #
            # 600 s tolerance: a live session streams exchange packets well
            # within this even for quiet/illiquid contracts, while a pre-open /
            # closed session is hours stale — so this never false-rejects a live
            # contract yet always catches the closed-session case.
            _SESSION_STALE_SEC = 600

            _ex_age: float | None = None
            # Same question for a symbol Zerodha never quotes: forex, metals,
            # indices and US stocks come from MetaAPI / Infoway, which send no
            # exchange packet time. Their quote carries the PROVIDER's tick
            # time as `feed_ts` instead, so the stale-price guard covers them
            # too. Kept separate from `_ex_age` on purpose — the 600 s
            # "session looks closed" check below is about Indian exchange
            # sessions and its advice (place an AMO) doesn't apply here.
            _feed_age: float | None = None
            try:
                _ex_age = _zerodha_for_tick_check.get_exchange_ts_age_sec(instrument.token)
            except Exception:
                _ex_age = None
            # Multi-worker: ticks_by_token is warm only on the feed-leader, but
            # the leader mirrors `exchange_timestamp` into mdlive:{token}, so a
            # non-leader reads the live-session signal from there.
            if _ex_age is None:
                try:
                    _snap = await _read_mdlive(instrument.token) or {}
                    _ets = float(_snap.get("exchange_timestamp") or 0)
                    if _ets > 0:
                        _ex_age = _vt.time() - _ets
                    _fts = float(_snap.get("feed_ts") or 0)
                    if _fts > 0:
                        _feed_age = _vt.time() - _fts
                except Exception:
                    _ex_age = None

            # Operator decision (2026-07-14): the 60-second "no live tick"
            # FALLBACK block was REMOVED. It false-blocked legitimate orders
            # during brief feed hiccups / post-restart WS warm-up with the
            # "No live prices in the last 60s" toast, so trades wouldn't go
            # through. Only the AUTHORITATIVE exchange-timestamp guard stays:
            # it reads the exchange's OWN packet time (600 s tolerance), so a
            # GENUINELY closed session (holiday / pre-open / evening-only —
            # where the timestamp is hours stale) is still blocked, preventing
            # trades at last-session prices (the 28-May stale-price incident,
            # and the phantom-P&L class of bug). A briefly-quiet-but-live feed
            # no longer blocks; when no exchange timestamp exists at all the
            # order simply proceeds.
            # ── Stale-price block (admin setting, default 30 s) ─────────
            # Operator request: if an instrument's price has not moved for
            # N seconds, nobody may open a trade on it until a new price
            # arrives. Uses the exchange's own packet time — the same signal
            # as the session guard below, with a tighter threshold — so a
            # frozen feed and a genuinely untraded contract both count.
            _stale_block_sec = int(risk.get("staleFeedBlockSec") or 0)
            if _stale_block_sec > 0:
                _warm_age: float | None = None
                try:
                    from app.core.redis_client import cache_get as _cache_get
                    from app.services.zerodha_service import FEED_WARM_KEY

                    _warm_at = await _cache_get(FEED_WARM_KEY)
                    if _warm_at:
                        _warm_age = _vt.time() - float(_warm_at)
                except Exception:
                    _warm_age = None
                _stale_msg = stale_feed_block_reason(
                    symbol=instrument.symbol,
                    price_age_sec=_ex_age if _ex_age is not None else _feed_age,
                    threshold_sec=_stale_block_sec,
                    feed_warm_age_sec=_warm_age,
                )
                if _stale_msg:
                    raise OrderRejectedError(_stale_msg, code="STALE_FEED")

            if _ex_age is not None and _ex_age > _SESSION_STALE_SEC:
                raise MarketClosedError(
                    f"{instrument.symbol}: no live session — the exchange feed "
                    f"is {int(_ex_age)}s stale, so the market appears closed "
                    f"(pre-open / holiday session). Try once prices resume, or "
                    f"place an AMO order."
                )

    # Build snapshot for the order document
    settings_snapshot = {
        "segment_type": segment_type,
        "margin_percentage": s.get("margin_percentage"),
        "leverage": s.get("leverage"),
        "commission_type": str(s.get("commission_type")) if s.get("commission_type") else None,
        "commission_value": s.get("commission_value"),
        "min_brokerage": s.get("min_brokerage"),
        "limit_percentage": s.get("limit_percentage"),
        "stop_loss_mandatory": s.get("stop_loss_mandatory"),
        "auto_squareoff_time": s.get("auto_squareoff_time"),
        "m2m_squareoff_percent": s.get("m2m_squareoff_percent"),
    }

    return ValidatedOrder(settings=settings_snapshot, netting_settings=resolved, margin_required=margin_required, ltp=ltp)
