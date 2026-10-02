"""Risk Management background enforcer.

Runs every 250 ms (see `risk_enforcer_loop` default). Implements the
simplified spec:

    stopOutWarningPercent  — notify when (-total_pnl) / balance × 100 ≥ this %.
                             "balance" = wallet.available + used_margin + credit_limit
                             (matches the admin UI help text).
    stopOutPercent         — force-close EVERY open position when the same
                             ratio crosses this %.
    profitTradeHoldMinSeconds / lossTradeHoldMinSeconds / exitOnlyMode are
    enforced synchronously by the order validator; they don't need a
    background loop.

Plus a built-in bracket SL / TP scan per position (LONG: SL when LTP ≤ SL,
TP when LTP ≥ TP; SHORT: mirrored). The pending-order poller handles
stand-alone LIMIT / SL-M; bracket legs attached to positions land here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from decimal import Decimal
from typing import Any

from bson import Decimal128

from app.models._base import OrderAction, OrderType
from app.models.position import Position, PositionStatus
from app.models.user import User
from app.services import (
    market_data_service,
    netting_service,
    order_service,
    position_service,
    wallet_service,
)
from app.utils.decimal_utils import to_decimal

logger = logging.getLogger(__name__)

# Per-shard run guard. Each gunicorn worker is a SEPARATE process, so this
# set lives per-process and normally holds at most one shard_id. It's a SET
# (not a single `_running` bool) so that if one process ever transiently holds
# two `leader:risk:shard:*` locks during a failover, the 2nd shard's loop is a
# clean no-op instead of being silently blocked by a shared bool (which would
# leave that shard un-enforced while its lock is held). On a single worker /
# RISK_SHARDS=1 this just holds {0} → identical to the old single-flag guard.
_running_shards: set[int] = set()


def shard_of(user_id: str, num_shards: int) -> int:
    """Deterministically map a user to a shard in [0, num_shards). Stable
    across processes (sha1 of the user_id), roughly even distribution.
    num_shards <= 1 → always 0, so single-shard mode is the identity."""
    if num_shards <= 1:
        return 0
    return int(hashlib.sha1(str(user_id).encode()).hexdigest(), 16) % num_shards


# Per-user re-arm flag for the warning notification — "once per crossing"
# means we send a single ping when loss first crosses the warning threshold
# and don't ping again until loss drops back below it (then we re-arm).
_warning_armed: dict[str, bool] = {}

# Yield cadence inside the per-user position sweep. Yielding on EVERY
# position (the old behaviour) made the sweep wall-clock balloon under
# peak load: each await let the busy leader's feed-fanout / 150 WS
# clients / HTTP steal the loop, so a 92-position sweep stretched to
# 0.4-1.4 s (occasionally ~9.8 s) → frequent risk_enforcer_tick_overrun.
# Yielding every N positions keeps the API responsive while letting the
# sweep finish in far fewer context switches.
_YIELD_EVERY = 16

# User-doc cache for the risk sweep. Risk policy / tier / status on the
# User doc change at human (admin) timescales, not per-tick, so re-running
# a Mongo `$in` for every active user on EVERY tick was pure waste
# (users_ms spikes of 100-273 ms seen in production). Each fetched User is
# cached for a few seconds; users that appear for the first time are pulled
# on the first tick they show up. Wallets are deliberately NOT cached —
# stop-out needs the live balance / settlement figure every tick.
_USER_CACHE_TTL = 10.0
_user_cache: dict[str, tuple[User, float]] = {}

# REST fallback LTP cache — avoids hitting Kite REST every 250 ms per token.
# Entries: token -> (ltp, unix_timestamp). Stale after 10 s.
_REST_LTP_TTL = 10.0
_rest_ltp_cache: dict[str, tuple[Decimal, float]] = {}
# Failure cache: tokens that returned no live price from REST (expired/delisted)
# are skipped for 60 s to prevent hammering Kite API every 10 s per user.
_REST_LTP_FAIL_TTL = 60.0
_rest_ltp_fail_ts: dict[str, float] = {}  # token → monotonic ts of last REST failure
# At most ONE Kite REST call may run at a time. asyncio is single-threaded so
# this bool is race-safe without a lock. asyncio.to_thread spawns a real OS
# thread that cannot be cancelled — 25 concurrent users each launching one
# creates thread-pool exhaustion (pool has ~12 slots) and cascades into
# 21 s overruns even with a 3 s wait_for timeout on each awaitable.
_rest_call_in_progress: bool = False

# ── Outlier / glitch-tick guard (added 2026-06-09) ──────────────────────
# A single feed tick that gaps far from a token's last-good price is almost
# always a stale/glitch tick — that booked the BEL26JUN415CE phantom STOP_OUT
# at 4.40 (real ~9.5) and ADANIPOWER at 6.46 / FEDERALBNK at 4.80. The zero-LTP
# guard only catches 0; a NON-zero-but-wrong price slips through. We don't act
# on a >35% single-tick gap until it's confirmed by 3 consecutive ticks at a
# similar level — a real move arrives as a sequence; a glitch reverts. This
# also self-corrects a bad seed: if the baseline itself is wrong, the genuine
# price (which persists) is accepted after 3 ticks (~0.75 s).
_OUTLIER_TICK_PCT = Decimal("0.35")   # >35% one-tick jump from last-good = suspect
_OUTLIER_CONFIRM_TICKS = 3            # consecutive ticks before a suspect is accepted
_last_good_ltp: dict[str, float] = {}              # token -> last accepted LTP
_outlier_pending: dict[str, tuple[float, int]] = {}  # token -> (suspect price, count)

# Grace window (seconds) after a segment's daily open during which stop-out /
# SL / TP are suppressed — the feed may still be on the stale overnight tick
# right at the bell (see is_within_open_grace + the 2026-07-01 CRUDEOIL
# phantom stop-out). 60 s is comfortably longer than the WS resubscribe/first
# -tick lag while barely delaying a genuine gap-open stop-out.
_OPEN_GRACE_SEC = 60


def _wallet_balance(wallet: Any) -> Decimal:
    """Denominator the stop-out percentages are measured against.

    Total wallet pool = available cash + currently locked margin + admin-
    extended credit. With balance ₹1000 and stop-out 80 %, a floating
    loss of ₹800 triggers stop-out — matching the broker's spec:
        loss_pct = (floating_loss + estimated_close_brokerage) / balance × 100

    Note: callers fold the close-leg brokerage estimate INTO the
    numerator (see `enforce_for_user`); this function only returns the
    denominator, kept simple and dependency-free so it stays cheap to
    call every tick.
    """
    return (
        to_decimal(wallet.available_balance)
        + to_decimal(wallet.used_margin)
        + to_decimal(wallet.credit_limit)
        # Bonus credit pool (Bonus Management) counts toward the stop-out base,
        # so a user can ride a bit further before stop-out. Always 0 when
        # BONUSES_ENABLED is off (no grants ever happen), so this is inert then.
        + to_decimal(getattr(wallet, "credit", 0))
    )


async def _send_warning(user_id: str, threshold: float, loss_pct: float) -> None:
    """Best-effort warning ping over the per-user Redis pub/sub channel.
    The user's open terminal subscribes to `user:{id}:risk` and renders a
    banner. We don't block the loop on this."""
    try:
        from app.core.redis_client import publish

        await publish(
            f"user:{user_id}:risk",
            {
                "type": "stop_out_warning",
                "threshold_pct": round(threshold, 2),
                "loss_pct": round(loss_pct, 2),
            },
        )
    except Exception:
        logger.debug("stop_out_warning_publish_failed", extra={"user_id": user_id})


def _classify_close_reason(raw: str) -> str:
    """Map the verbose internal reason string to the compact tag stored on
    Position.close_reason. The tag is what the UI renders on the Closed
    tab, so it has to be human-friendly and stable.
    """
    if "bracket_sl" in raw:
        return "SL_HIT"
    if "bracket_tp" in raw:
        return "TP_HIT"
    if "stop_out" in raw:
        return "STOP_OUT"
    return "AUTO"


async def _stamp_close_reason(position_id: Any, tag: str) -> None:
    """Refetch the position and stamp `close_reason` if it actually closed.
    Idempotent — won't overwrite an existing tag.
    """
    try:
        fresh = await Position.get(position_id)
        if (
            fresh is not None
            and fresh.status == PositionStatus.CLOSED
            and not fresh.close_reason
        ):
            fresh.close_reason = tag
            await fresh.save()
    except Exception:
        logger.warning(
            "close_reason_stamp_failed",
            extra={"position_id": str(position_id)},
        )


async def _squareoff_position(
    user: User,
    p: Position,
    reason: str,
    fill_at: Decimal | None = None,
) -> None:
    """Fire an opposite-side market order to flatten one position. Same
    pattern the kill-switch + EOD rollover use: `force_quantity` so the
    close moves exactly the open qty (legacy positions with stale
    lot_size land correctly), and `is_squareoff=True` so the validator's
    hold-time + exit-only gates pass through.

    `fill_at` (optional) is the price the close should book at. For
    SL/TP bracket fires we pass the user's trigger value (`stop_loss`
    or `target`) so the realised close price equals what the user set,
    not the live LTP at the moment the enforcer ticked — eliminates
    the 1-5 point slippage the poll-interval gap used to introduce.
    The matching engine treats this as `expected_price` and clamps it
    to ±1% of live bid/ask anyway, so an absurd value can't sneak
    through; SL/TP triggers by definition fire at the current LTP, so
    they always land well inside that cap."""
    if p.quantity == 0:
        return
    action = OrderAction.SELL if p.quantity > 0 else OrderAction.BUY
    qty = abs(p.quantity)
    lots = max(0.01, qty / max(1, p.instrument.lot_size or 1))
    payload: dict[str, Any] = {
        "token": p.instrument.token,
        "action": action.value,
        "order_type": OrderType.MARKET.value,
        "product_type": p.product_type.value,
        "lots": lots,
        "force_quantity": qty,
        "is_squareoff": True,
        "placed_from": "RISK_ENFORCER",
        # Same classified tag we stamp on the Position, now also on the Order
        # so the Orders monitor's Reason column shows SL / TP / stop-out.
        "close_reason": _classify_close_reason(reason),
    }
    if fill_at is not None and fill_at > 0:
        payload["expected_price"] = str(fill_at)
    try:
        await order_service.place_order(user=user, payload=payload)
        # The market order fills synchronously inside place_order — so by
        # the time we return here the position's status has been mutated
        # (see services/position_service.apply_fill). Stamp the
        # user-visible reason so the Closed tab on the app can show
        # "Closed by SL" / "Closed by TP" / "Stop-out".
        await _stamp_close_reason(p.id, _classify_close_reason(reason))
        logger.info(
            "risk_auto_squareoff",
            extra={
                "user_id": str(user.id),
                "position_id": str(p.id),
                "symbol": p.instrument.symbol,
                "reason": reason,
            },
        )
    except Exception:
        logger.exception(
            "risk_auto_squareoff_failed",
            extra={"user_id": str(user.id), "position_id": str(p.id)},
        )


async def _enforce_for_user(
    user: User,
    open_positions: list[Position],
    shared_ltp: dict[str, Any],
    prefetched_wallet: Any = None,
    shared_quotes: dict[str, Any] | None = None,
) -> None:
    """One sweep for one user.

    ``open_positions``, ``shared_ltp``, ``prefetched_wallet``, and
    ``shared_quotes`` are all pre-fetched ONCE per tick in ``enforce_once``
    — zero per-user MongoDB/REST calls in this hot path.
    """
    if not open_positions:
        # No open exposure → re-arm the warning for the next breach.
        _warning_armed[str(user.id)] = True
        return

    # Build per-user LTP map from the tick-level shared snapshot.
    ltp_map: dict[str, Any] = {
        p.instrument.token: shared_ltp.get(p.instrument.token)
        for p in open_positions
    }

    # Refresh LTP + run bracket SL/TP checks per position. Bracket legs on
    # open positions don't live in the pending-order book, so this is where
    # they fire.
    #
    # Market-closed gate: for Indian equity / F&O (close 15:30 IST) and MCX
    # (close 23:55 IST) we MUST NOT auto-fire SL/TP brackets after close.
    # The cached LTP is whatever the last tick before close was — comparing
    # it against the user's bracket trigger one second later (or hours
    # later) and "filling" the close at that stale price is a phantom
    # execution: there's no exchange, no counterparty, no real fill. The
    # user reported "market closed ho gaya phir bhi mere trade close ho
    # gaya" because the enforcer was happily booking these phantom closes.
    # Forex / crypto / spot commodity stay 24×5 / 24×7, so they get the
    # full bracket evaluation as before.
    from app.utils.time_utils import (
        is_after_close,
        is_before_open,
        is_weekend,
        is_within_open_grace,
        now_ist,
    )

    now_now = now_ist()
    is_weekend_now = is_weekend(now_now.date())

    def _segment_closed(seg: str | None) -> bool:
        if not seg:
            return False
        if is_weekend_now:
            # Indian exchanges + MCX are closed Sat/Sun. is_after_close
            # only handles weekday close-of-day, so OR with a prefix
            # check that covers the full weekend window. Forex (CDS_*)
            # is 24×5 — closed Sat all day, Sun till ~Mon 04:00 IST;
            # we cover that conservatively by NOT including it here so
            # 24×5 instruments still trade Sun evening if open. Crypto
            # is 24×7 and never closes.
            # Prefix WITHOUT underscore catches bare "NFO" / "NSE" / "MCX"
            # values that some instrument seeds produce, and also covers all
            # "NSE_*" / "NFO_*" variants. is_after_close now also has a
            # prefix fallback, but the weekend short-circuit here avoids
            # calling it at all when we already know it's a closed weekend.
            seg_up = seg.upper()
            if seg_up.startswith(("NSE", "BSE", "MCX", "NFO", "BFO")):
                return True
        # Out of session = past close (15:30→midnight) OR before open
        # (midnight→09:15). The second half is what plugs the weekday
        # pre-open hole that let the enforcer phantom-close positions
        # against yesterday's stale closing tick on Monday mornings.
        #
        # PLUS a short grace window RIGHT AFTER the bell: at the exact open
        # the WS feed often hasn't ticked yet, so the last cached price is
        # still the overnight/stale one. Acting on it fires phantom stop-outs
        # at a price the new session never traded (2026-07-01 09:00 MCX:
        # CRUDEOIL long stopped out at a stale 6523 while the session low was
        # 6631). Suppress stop-out / SL / TP for _OPEN_GRACE_SEC after open so
        # real prices establish first; genuine risk re-evaluates on the next
        # tick once the grace lapses.
        return (
            is_after_close(seg, now_now)
            or is_before_open(seg, now_now)
            or is_within_open_grace(seg, _OPEN_GRACE_SEC, now_now)
        )

    total_unrealised = Decimal("0")
    bracket_fired_ids: set[str] = set()
    # Snapshotted lazily the first time a bracket actually fires this tick,
    # so the post-bracket close-ordering correction nets only the phantom
    # those fires create (not pre-existing / genuine settlement).
    bracket_settle_before: Decimal | None = None
    for _i, p in enumerate(open_positions):
        # Yield to the event loop every _YIELD_EVERY positions.
        # refresh_unrealized_pnl is fully in-memory (no network await), so the
        # loop is CPU-bound; without ANY yield it starves HTTP and the feed/
        # WS-fanout (150 clients), stalling the Positions page. But yielding on
        # EVERY position made the opposite problem under peak load — each await
        # let the busy leader steal the loop, ballooning the sweep wall-clock
        # to seconds (the risk_enforcer_tick_overrun spikes). Yielding in
        # batches keeps the API responsive while finishing the sweep in far
        # fewer context switches. sleep(0) just reschedules cooperatively.
        if _i % _YIELD_EVERY == 0:
            await asyncio.sleep(0)
        # ── Market-closed skip ─────────────────────────────────────────
        # Skip the ENTIRE evaluation (LTP refresh, bracket fire, and
        # this position's contribution to the aggregate stop-out loss)
        # when its segment is past close. The cached LTP at this point
        # is whatever the last tick before close was — booking a phantom
        # close at that stale price is what caused the "market closed
        # ho gaya phir bhi mere trade close ho gaya" bug. Re-evaluation
        # resumes on the next tick once the segment reopens.
        seg_for_check = (
            getattr(p, "segment_type", None) or getattr(p.instrument, "segment", None)
        )
        if _segment_closed(str(seg_for_check) if seg_for_check else None):
            # Preserve the position's last-known unrealised P/L on the
            # aggregate so the warning re-arm logic and admin telemetry
            # still see "this user has open exposure" — but DON'T treat
            # any change as actionable.
            try:
                total_unrealised += to_decimal(p.unrealized_pnl)
            except Exception:
                pass
            continue

        ltp = ltp_map.get(p.instrument.token)
        # Reject zero / negative LTPs the same way we reject `None`. A
        # 0 LTP fed to `refresh_unrealized_pnl` would compute floating
        # loss = (0 − avg) × qty = −notional, which the aggregate then
        # mis-reads as a colossal drawdown and force-closes the
        # position even when it's in profit. 21-May 08:11 production
        # incident: COPPER 2500-lot stop-out fired with loss_pct =
        # 9124.17 % because the cached LTP was 0 at scan time.
        ltp_valid = ltp is not None
        if ltp_valid:
            try:
                if to_decimal(ltp) <= 0:
                    ltp_valid = False
            except Exception:
                ltp_valid = False
        # Outlier / glitch-tick guard — see module header. Reuses the
        # `ltp_valid=False` skip path below (preserve last-good PnL, skip
        # bracket + stop-out this tick) when an UNCONFIRMED >35% single-tick
        # gap is seen. This is what stops a stale/glitch Zerodha tick (e.g.
        # 9.5 -> 4.40) from booking a phantom STOP_OUT.
        if ltp_valid:
            try:
                tok = p.instrument.token
                new_ltp = float(str(ltp))
                last_good = _last_good_ltp.get(tok)
                if last_good and last_good > 0:
                    dev = abs(new_ltp - last_good) / last_good
                    if dev > float(_OUTLIER_TICK_PCT):
                        sus, cnt = _outlier_pending.get(tok, (None, 0))
                        if sus is not None and abs(new_ltp - sus) <= abs(sus) * 0.05:
                            cnt += 1
                        else:
                            cnt = 1
                        _outlier_pending[tok] = (new_ltp, cnt)
                        if cnt < _OUTLIER_CONFIRM_TICKS:
                            ltp_valid = False
                            logger.warning(
                                "risk_outlier_tick_skipped",
                                extra={
                                    "user_id": str(user.id),
                                    "position_id": str(p.id),
                                    "symbol": p.instrument.symbol,
                                    "last_good": last_good,
                                    "tick_ltp": new_ltp,
                                    "deviation_pct": round(dev * 100, 1),
                                    "confirm": cnt,
                                },
                            )
                        else:
                            # Confirmed across N consecutive ticks → real move.
                            _last_good_ltp[tok] = new_ltp
                            _outlier_pending.pop(tok, None)
                    else:
                        _last_good_ltp[tok] = new_ltp
                        _outlier_pending.pop(tok, None)
                else:
                    # First sighting this run — seed the baseline.
                    _last_good_ltp[tok] = new_ltp
            except Exception:
                logger.debug("risk_outlier_guard_failed", exc_info=True)
        if ltp_valid:
            try:
                # ALWAYS hand refresh a dict (never None). When a token is
                # missing from the WS-state snapshot, a None here makes
                # refresh_unrealized_pnl fall back to an awaited get_quote →
                # Zerodha REST (~2s) PER position PER tick. After the demo
                # book closed, the remaining real NSE/MCX option/future legs
                # include illiquid tokens absent from _state, so that REST
                # fallback fired for many of them every tick → sweep 2-13s,
                # stalling the Positions page. An empty dict makes refresh
                # mark against the LTP we already have (zero network); the
                # bid/ask close-side refinement is a nicety, not worth a REST
                # round-trip inside a 0.5s sweep.
                pq = (shared_quotes.get(str(p.instrument.token)) if shared_quotes else None) or {}
                await position_service.refresh_unrealized_pnl(p, ltp, prefetched_quote=pq)
            except Exception:
                logger.warning(
                    "risk_pnl_refresh_failed",
                    extra={
                        "user_id": str(user.id),
                        "position_id": str(p.id),
                        "symbol": p.instrument.symbol,
                    },
                )
        else:
            _now = time.monotonic()
            _tok = p.instrument.token
            if _now - _ltp_warn_last.get(_tok, 0) >= _LTP_WARN_INTERVAL_SEC:
                _ltp_warn_last[_tok] = _now
                logger.warning(
                    "risk_ltp_fetch_failed",
                    extra={
                        "user_id": str(user.id),
                        "position_id": str(p.id),
                        "symbol": p.instrument.symbol,
                        "token": _tok,
                        "has_sl": p.stop_loss is not None,
                        "has_tp": p.target is not None,
                        "raw_ltp": str(ltp) if ltp is not None else None,
                    },
                )
            # No usable LTP this tick — preserve the position's last
            # known unrealised P/L on the aggregate but DO NOT add a
            # bogus negative driven by a zero tick. Skip bracket SL/TP
            # checks too; they need a current price to be meaningful.
            try:
                total_unrealised += to_decimal(p.unrealized_pnl)
            except Exception:
                pass
            continue
        try:
            total_unrealised += to_decimal(p.unrealized_pnl)
        except Exception:
            pass

        if p.quantity == 0:
            continue
        try:
            # SL/TP must trigger on the CLOSE-SIDE mark — the bid for a long,
            # the ask for a short — which is the price the user would actually
            # realise and exactly what M2M / the position card display. The
            # call to refresh_unrealized_pnl just above already resolved that
            # close-side price (bid/ask from the quote, falling back to LTP
            # when no depth is published) and stored it on `p.ltp`, so we read
            # it back here instead of the raw last-traded `ltp`.
            #
            # Using the raw last-traded `ltp` (the old behaviour) made brackets
            # MISS on thin/illiquid options whose last trade lags the live book:
            # the bid fell through the stop (user saw e.g. "280.60 BID" under an
            # SL of 283) but the stale last_price stayed above it, so the SL
            # never fired even though the position was marked at a loss
            # (CL-reported NIFTY26JUN23800CE + SENSEX weekly options, 2026-06-24).
            ltp_dec = to_decimal(p.ltp) if p.ltp is not None else to_decimal(ltp)
            if ltp_dec <= 0:
                ltp_dec = to_decimal(ltp)
            sl = to_decimal(p.stop_loss) if p.stop_loss is not None else None
            tp = to_decimal(p.target) if p.target is not None else None
        except Exception:
            continue

        # ── Per-segment bracket toggle ─────────────────────────────────
        # Admin can disable SL and/or TP auto-fire for a whole segment
        # (admin panel → Limit away → SL/TP on-off). When a leg is
        # disabled for this position's segment we SKIP the auto-fire for
        # that leg only — the stored leg value is left untouched so it
        # re-arms automatically if the admin turns it back on. Flags are
        # served from a 30 s in-process cache, so this is a dict lookup in
        # the hot loop (no per-tick DB round-trip). Only pay the call when
        # the position actually carries a bracket leg.
        if sl is not None or tp is not None:
            try:
                _sl_on, _tp_on = await netting_service.get_segment_bracket_flags(
                    str(seg_for_check) if seg_for_check else ""
                )
                if not _sl_on:
                    sl = None
                if not _tp_on:
                    tp = None
            except Exception:
                logger.debug("segment_bracket_flags_lookup_failed", exc_info=True)

        # NOTE (2026-06-03): the old avg-based "wrong-side SL/TP self-heal"
        # was REMOVED. It compared SL/TP against the ENTRY price and so
        # silently wiped valid profit-lock / trailing stops the moment a
        # position moved into profit — e.g. a long in profit with SL set
        # above entry but below LTP, or a short in profit with SL below
        # entry but above LTP. That was the "SL lagao to turant auto-remove
        # ho jaata hai" bug (CL62329114 / PANKAJ).
        #
        # Set-time validation now checks SL/TP against the LIVE price
        # (positions.update_sl_tp via `_validate_sl_tp_direction`, and the
        # order_validator bracket check), so a leg can only ever be set on
        # the correct side of the current price. Anything the price later
        # crosses simply FIRES via the bracket check below — which is
        # exactly what a stop / target is supposed to do. There is no
        # longer any "clear instead of fire" case to handle here.

        # Identify the trigger that fired and remember WHICH price the
        # close should book at. The user set `stop_loss` / `target` as
        # an explicit price barrier — they expect the trade to record
        # at THAT price, not at whatever LTP the next risk-enforcer
        # tick happened to read (which can drift several ticks past
        # the trigger between sweeps). Passing the trigger as
        # `fill_at` makes the matching engine use it directly.
        hit_reason: str | None = None
        fill_at: Decimal | None = None
        if p.quantity > 0:  # LONG
            if sl is not None and sl > 0 and ltp_dec <= sl:
                hit_reason = f"bracket_sl_long@{ltp_dec}"
                fill_at = sl
            elif tp is not None and tp > 0 and ltp_dec >= tp:
                hit_reason = f"bracket_tp_long@{ltp_dec}"
                fill_at = tp
        else:  # SHORT
            if sl is not None and sl > 0 and ltp_dec >= sl:
                hit_reason = f"bracket_sl_short@{ltp_dec}"
                fill_at = sl
            elif tp is not None and tp > 0 and ltp_dec <= tp:
                hit_reason = f"bracket_tp_short@{ltp_dec}"
                fill_at = tp

        if hit_reason is not None:
            # ── Cross-worker dedup via atomic Mongo claim ───────────
            # The risk_enforcer_loop runs in every uvicorn worker/instance,
            # and two of them can read the same OPEN position with target
            # set in the same 5 s tick. Without a distributed lock both
            # called _squareoff_position and TWO opposite-side SELL orders
            # landed in the History tab for the same close (the user-
            # reported "limit order me 2 baar execute hua" bug).
            #
            # Fix: race the workers on a `findOneAndUpdate` that clears the
            # bracket leg BEFORE placing the squareoff. Whichever worker's
            # update has a `modified_count > 0` legitimately claimed the
            # fire; the rest will see the leg already cleared and skip.
            # Idempotent: if the fire fails downstream we restore the leg
            # in the except block so the next tick can retry.
            is_sl_fire = "bracket_sl" in hit_reason
            leg_field = "stop_loss" if is_sl_fire else "target"
            # Preserve WHICH price fired onto the close-snapshot field in the
            # SAME atomic claim. The leg is cleared to None before the fire, so
            # the close-time snapshot (`if stop_loss is not None: close_stop_loss
            # = stop_loss`) would otherwise see None and lose the trigger — the
            # Closed tab then can't show "SL @ X" and there's no record to audit
            # the fill against. `fill_at` (the same trigger) is what the fill
            # books at, so close_stop_loss/close_target == close price now.
            snap_field = "close_stop_loss" if is_sl_fire else "close_target"
            _trigger = sl if is_sl_fire else tp
            try:
                claim_result = await Position.get_motor_collection().update_one(
                    {
                        "_id": p.id,
                        "status": PositionStatus.OPEN.value,
                        leg_field: {"$ne": None},
                    },
                    {"$set": {leg_field: None, snap_field: Decimal128(str(_trigger))}},
                )
            except Exception:
                logger.exception("bracket_claim_query_failed", extra={"position_id": str(p.id)})
                continue
            if not claim_result.modified_count:
                # Another worker won the race; nothing to do here.
                logger.info(
                    "bracket_skip_already_claimed",
                    extra={"position_id": str(p.id), "reason": hit_reason},
                )
                continue

            # Snapshot the leg value we just cleared so we can restore it
            # if the squareoff itself blows up.
            restore_value = sl if is_sl_fire else tp
            # Capture settlement once, before the first bracket close, for
            # the post-loop phantom-settlement correction.
            if bracket_settle_before is None:
                try:
                    _bw = await wallet_service.get_or_create(user.id)
                    bracket_settle_before = to_decimal(_bw.settlement_outstanding)
                except Exception:
                    bracket_settle_before = None
            try:
                await _squareoff_position(user, p, hit_reason, fill_at=fill_at)
                bracket_fired_ids.add(str(p.id))
            except Exception:
                logger.exception(
                    "bracket_squareoff_failed_restoring_leg",
                    extra={"position_id": str(p.id), "reason": hit_reason},
                )
                if restore_value is not None:
                    try:
                        await Position.get_motor_collection().update_one(
                            {"_id": p.id},
                            {"$set": {leg_field: Decimal128(str(restore_value))}},
                        )
                    except Exception:
                        logger.exception(
                            "bracket_leg_restore_failed",
                            extra={"position_id": str(p.id)},
                        )

    # Drop bracket-flattened positions before the stop-out check so we
    # don't double-close them.
    if bracket_fired_ids:
        # Net any close-ordering phantom settlement the bracket fires booked
        # (a loss-leg closing before a sibling's margin was freed). Same
        # self-correcting net the stop-out path uses; no-op when there's no
        # phantom, and genuine shortfall is left intact.
        if bracket_settle_before is not None:
            try:
                await wallet_service.net_phantom_settlement(user.id, bracket_settle_before)
            except Exception:
                logger.exception(
                    "bracket_net_phantom_failed", extra={"user_id": str(user.id)}
                )
        open_positions = [p for p in open_positions if str(p.id) not in bracket_fired_ids]
        if not open_positions:
            return

    # Risk policy snapshot. `get_effective_risk` walks global → per-user
    # override and returns a flat dict the same way segment-settings does.
    risk = (await netting_service.get_effective_risk(str(user.id)))["settings"]
    warning_pct = float(risk.get("stopOutWarningPercent") or 0)
    stop_pct = float(risk.get("stopOutPercent") or 0)
    if warning_pct <= 0 and stop_pct <= 0:
        # Both knobs off — nothing to enforce, just keep the warning re-armed.
        _warning_armed[str(user.id)] = True
        return

    user_id_str = str(user.id)
    wallet = prefetched_wallet or await wallet_service.get_or_create(user.id)  # type: ignore[arg-type]
    balance = _wallet_balance(wallet)

    # Floating loss as a positive magnitude (0 when the book is flat / in
    # profit). Computed once here so the zero-capital guard below AND the
    # percentage check further down both reuse it.
    floating_loss = (-total_unrealised) if total_unrealised < 0 else Decimal("0")

    # A force-close on a position whose feed is 0/stale (e.g. Infoway crypto
    # dropped after a restart, or a Zerodha tick gap) can NEVER fill — the
    # matching-engine zero-price guard rejects it as STALE_FEED. Both stop-out
    # loops below would then re-issue that squareoff every 250 ms, spamming
    # hundreds of failed orders while never flattening (observed 2026-06-09:
    # CL84388017 BTCUSD ~1 squareoff/sec for minutes with the feed at 0). Skip
    # such positions — they flatten on the first tick the feed returns.
    # `ltp_map` was fetched once at the top of this tick.
    def _ltp_ok(tok: str) -> bool:
        v = ltp_map.get(tok)
        if v is None:
            return False
        try:
            return float(str(v)) > 0
        except Exception:
            return False

    # ── Zero / negative-capital stop-out (was: `if balance <= 0: return`) ─
    # The whole-pool denominator (available + used_margin + credit_limit)
    # can legitimately reach 0: realised losses floored the wallet to 0 with
    # the overflow parked in settlement_outstanding, or a position opened
    # with margin_used = 0. The OLD `return` here SILENTLY DISABLED stop-out
    # for exactly those accounts — CL45900793 ran to -₹2.3L realised because
    # every 250 ms tick bailed at this single line.
    #
    # There is no percentage to divide against a 0 balance, but the intent
    # is unambiguous: zero capital + ANY floating loss means equity is
    # already <= 0 and the broker is carrying the loss → force-close every
    # open position immediately. Flat / in-profit books are left untouched.
    if balance <= 0:
        if stop_pct > 0 and floating_loss > 0:
            logger.warning(
                "stop_out_zero_capital",
                extra={
                    "user_id": user_id_str,
                    "floating_loss": float(floating_loss),
                    "balance": float(balance),
                    "open_positions": len(open_positions),
                },
            )
            _zc_reason = f"stop_out_zero_capital_loss={floating_loss:.2f}"
            _zc_tasks = []
            for p in open_positions:
                seg = getattr(p, "segment_type", None) or getattr(
                    p.instrument, "segment", None
                )
                if _segment_closed(str(seg) if seg else None):
                    continue
                if not _ltp_ok(p.instrument.token):
                    continue
                # Same trigger-price pin as the main stop-out below — book
                # at the close-side mark the breach saw, not a later tick.
                _zc_tasks.append(
                    _squareoff_position(user, p, _zc_reason, fill_at=to_decimal(p.ltp))
                )
            if _zc_tasks:
                await asyncio.gather(*_zc_tasks, return_exceptions=True)
            _warning_armed[user_id_str] = True
        return

    # ── Fast exit when comfortably safe ────────────────────────────────
    # The close-brokerage estimate below is the most expensive part of the
    # tick — one netting resolve + one brokerage calc PER open position.
    # Brokerage only ever makes the projected loss LARGER, so if the loss
    # WITHOUT brokerage is already below the lowest active threshold there
    # is nothing either branch could fire. Skip the estimate and keep the
    # 250 ms sweep cheap on the 99 % of ticks where books are healthy.
    # (Worst case defers a borderline WARNING by one tick; it can never
    # delay a real stop-out, which sits far above this floor.)
    raw_loss_pct = (
        float(floating_loss / balance * Decimal(100)) if floating_loss > 0 else 0.0
    )
    arm_floor = min(p for p in (warning_pct, stop_pct) if p > 0)
    if raw_loss_pct < arm_floor:
        if raw_loss_pct < warning_pct:
            _warning_armed[user_id_str] = True
        return

    # Estimate the closing-leg brokerage that would be charged if every
    # open position were force-closed right now. Per broker spec the
    # stop-out check looks at floating P&L AFTER deducting close
    # brokerage — so a position that's a hair from break-even still
    # trips stop-out once round-trip costs are folded in. Uses the same
    # netting + brokerage_calculator stack the matching engine runs at
    # fill time, so the estimate matches what will actually be billed.
    from app.models._base import OrderAction as _OA
    from app.services import brokerage_calculator as _bc

    async def _estimate_one(p: Any) -> Decimal:
        if not p.quantity:
            return Decimal("0")
        try:
            close_action = _OA.SELL if p.quantity > 0 else _OA.BUY
            netting = await netting_service.get_effective_settings(
                user.id,
                p.instrument.segment,
                action=close_action.value,
                product_type=p.product_type.value,
                symbol=p.instrument.symbol,
            )
            charges = await _bc.calculate(
                segment_type=p.instrument.segment,
                action=close_action,
                product_type=p.product_type,
                qty=abs(float(p.quantity)),
                price=to_decimal(p.ltp) if p.ltp is not None else to_decimal(p.avg_price),
                lot_size=int(p.instrument.lot_size or 1),
                netting_override=netting.get("settings"),
                is_closing=True,
                charge_on=netting.get("settings", {}).get("charge_on"),
            )
            return to_decimal(charges.total)
        except Exception:
            logger.warning(
                "risk_close_brokerage_estimate_failed",
                extra={"user_id": str(user.id), "position_id": str(p.id)},
            )
            return Decimal("0")

    # The per-position close-brokerage estimate is the HEAVIEST part of the
    # sweep — a netting resolve + brokerage_calculator run for EVERY open
    # position, EVERY tick. At 200+ positions it pegged one CPU core and
    # blocked the HTTP event loop for SECONDS (sweep_ms 7600), so users saw
    # the Positions tab stuck on "Loading…".
    #
    # Brokerage is a tiny fraction of the loss and only sways the stop-out
    # decision right at the margin. So compute the rough loss % from the
    # raw floating loss first, and ONLY pay for the precise per-position
    # brokerage when the user is already within striking distance of the
    # stop-out line. For the vast majority of users each tick (loss far
    # below threshold) we skip 200+ netting+brokerage lookups entirely.
    # Brokerage estimate REMOVED from the per-tick sweep entirely. It was a
    # netting + brokerage_calculator run for EVERY open position EVERY tick —
    # the single heaviest thing in the sweep. The earlier "only when near the
    # stop-out line" gate stopped helping once the demo book was closed: the
    # remaining real positions are all heavily leveraged (≈always near the
    # line), so the gate opened for everyone and the sweep ballooned to 2-8s
    # for ~90 positions, stalling the event loop so the Positions page would
    # not load.
    #
    # Brokerage is a tiny fraction of the loss that triggers a stop-out, so we
    # now decide stop-out on the raw floating loss alone. Effect: a stop-out
    # fires a hair LATER (by brokerage's share of balance) — conservative and
    # safe; the close still charges full brokerage when it actually fires.
    estimated_close_brokerage = Decimal("0")

    # Stop-out: floating loss as % of total balance.
    #
    #   loss_pct = (floating_loss + close_brokerage) / balance × 100
    #   balance  = available + used_margin + credit_limit
    #
    # Admin sets 90% → stop-out fires when floating loss reaches 90%
    # of the user's total balance. Example: balance ₹48K, 90% = stop
    # when loss reaches ₹43.3K.
    projected_loss = floating_loss + estimated_close_brokerage
    loss_pct = (
        float(projected_loss / balance * Decimal(100)) if balance > 0 and projected_loss > 0 else 0.0
    )

    # 1) Stop-out — force-close EVERYTHING when loss % crosses threshold.
    if stop_pct > 0 and loss_pct >= stop_pct:
        logger.warning(
            "stop_out_triggered",
            extra={
                "user_id": user_id_str,
                "loss_pct": round(loss_pct, 2),
                "threshold_pct": stop_pct,
                "floating_loss": float(floating_loss),
                "balance": float(balance),
                "unrealised": float(total_unrealised),
            },
        )
        # Snapshot settlement BEFORE the batch so we can tell apart any
        # settlement that's a pure close-ordering artifact of THIS stop-out
        # (booked while a loss-maker closed ahead of a margin-heavy sibling)
        # from pre-existing / genuine shortfall.
        settlement_before = to_decimal(wallet.settlement_outstanding)
        _so_reason = f"stop_out_loss_{loss_pct:.2f}>={stop_pct}"
        _so_tasks = []
        for p in open_positions:
            seg = getattr(p, "segment_type", None) or getattr(
                p.instrument, "segment", None
            )
            if _segment_closed(str(seg) if seg else None):
                continue
            if not _ltp_ok(p.instrument.token):
                continue
            # Book the stop-out close at the SAME close-side mark the breach
            # was computed against (`p.ltp` was just set by
            # refresh_unrealized_pnl to the bid-for-long / ask-for-short
            # price this tick). Without this the squareoff filled at the
            # LIVE price a fraction of a second later — which, if the tick
            # had bounced back, booked a PROFIT on a position that was
            # force-closed FOR A LOSS. Operators (and users) read that as
            # "profit trade me stop-out kyun?". Pinning the fill to the
            # trigger mark makes the realised P&L equal the floating loss
            # that fired the stop-out — same approach SL/TP brackets already
            # use. The matching engine still clamps it to ±1% of live
            # bid/ask, so a stale mark can't book an absurd price.
            _so_tasks.append(
                _squareoff_position(user, p, _so_reason, fill_at=to_decimal(p.ltp))
            )
        # Fire all stop-out closes in parallel — was sequential (N awaits),
        # now all matching-engine runs happen concurrently.
        if _so_tasks:
            await asyncio.gather(*_so_tasks, return_exceptions=True)
        # Close-ordering correction: net any settlement booked during this
        # batch back against the margin the same batch just freed, so the
        # wallet never lingers in the contradictory "high available_balance
        # + phantom settlement_outstanding" state an admin can misread as
        # "the user kept too much" (CL15362105 / MITESH — an admin clawed
        # ₹2,000 after seeing an inflated balance). Net-neutral on equity;
        # genuine capital-exhausted shortfall is left intact.
        try:
            await wallet_service.net_phantom_settlement(user.id, settlement_before)
        except Exception:
            logger.exception(
                "net_phantom_settlement_failed", extra={"user_id": user_id_str}
            )
        # Forfeit any bonus the stop-out left behind. All positions are now
        # closed → bonus_locked has been released back into `credit`, and the
        # loss absorbed only the overflow past real cash, so a residual bonus
        # can linger. Operator policy: a stop-out (account wipe) forfeits the
        # whole remaining bonus — it must not survive. No-op when bonuses off.
        try:
            from app.core.config import settings as _bonus_settings

            if _bonus_settings.BONUSES_ENABLED:
                from app.services import bonus_service as _bonus_svc

                _forfeited = await _bonus_svc.forfeit_on_stopout(user, reason=_so_reason)
                if _forfeited > 0:
                    logger.info(
                        "stopout_bonus_forfeited",
                        extra={"user_id": user_id_str, "amount": float(_forfeited)},
                    )
        except Exception:  # bonus forfeit must never block / fail the stop-out
            logger.exception(
                "stopout_bonus_forfeit_failed", extra={"user_id": user_id_str}
            )
        _warning_armed[user_id_str] = True
        return

    # 2) Warning — fire once per crossing FROM BELOW. Armed = ready to
    # fire. Once fired we disarm; reset to armed when loss drops back
    # below the warning threshold. `stopOutWarningPercent` is in the
    # same units as `stopOutPercent` (loss % of bal). Set it LOWER than
    # the stop-out value to get an early heads-up (e.g. warning 60%,
    # stop-out 80%).
    armed = _warning_armed.get(user_id_str, True)
    if warning_pct > 0 and loss_pct >= warning_pct:
        if armed:
            await _send_warning(user_id_str, warning_pct, loss_pct)
            _warning_armed[user_id_str] = False
            logger.info(
                "stop_out_warning_sent",
                extra={
                    "user_id": user_id_str,
                    "loss_pct": round(loss_pct, 2),
                    "threshold_pct": warning_pct,
                },
            )
    elif loss_pct < warning_pct:
        _warning_armed[user_id_str] = True


# How many users to enforce concurrently in one batch. Sized so the
# concurrent Mongo fan-out fits comfortably inside `MONGODB_MAX_POOL_SIZE`
# After caching fixes: wallet pre-fetched, LTP via _state (zero DB),
# quote cache warmed, netting TTL staggered → <5 DB ops per user per
# tick on average. One large batch: sweep = max(slowest_user) instead
# of sum(max_per_batch × N_batches). Pool handles 200 users easily.
_RISK_ENFORCE_BATCH_SIZE = 200

# Throttle null-LTP warnings: log at most once per token per 5 minutes.
# Without this, 40-50 positions with stale/missing LTP emit WARNING logs
# at 4 sweeps/second = 160-200 log writes/second → measurable CPU spike.
_ltp_warn_last: dict[str, float] = {}
_LTP_WARN_INTERVAL_SEC = 300


async def _enforce_one_user_safe(
    user: User | None,
    open_positions: list[Position],
    shared_ltp: dict[str, Any],
    prefetched_wallet: Any = None,
    shared_quotes: dict[str, Any] | None = None,
) -> bool:
    """Run one user's risk sweep with full per-user error isolation."""
    try:
        if user is None:
            return False
        await _enforce_for_user(user, open_positions, shared_ltp, prefetched_wallet, shared_quotes)
        return True
    except Exception:
        logger.exception(
            "risk_enforcer_user_failed",
            extra={"user_id": str(user.id) if user else "?"},
        )
        return False


# Demo positions are fake money, so they don't need 0.5s real-time risk
# enforcement — a ~2.5s stop-out / SL-TP latency is invisible in practice.
# They were ~60% of the open book (114 of 187 on prod), so risk-checking
# them only every Nth tick — while real-money positions run EVERY tick —
# roughly halves the average per-tick sweep cost and leaves the event loop
# free for HTTP / WS on the 4-of-5 "light" ticks. Real positions are NEVER
# deferred. At the 0.5s loop interval, N=5 ⇒ demo checked ~every 2.5s.
DEMO_SWEEP_EVERY_N = 5
_sweep_tick = 0


# ── Per-process cache pruning (memory-leak guard) ─────────────────────
# Every module-level dict the sweep keeps (_warning_armed, _user_cache,
# _last_good_ltp, _outlier_pending, _ltp_warn_last, _rest_ltp_cache,
# _rest_ltp_fail_ts) is keyed by user_id or token and was NEVER evicted.
# Across a trading day options/futures expire and users open/close
# positions, so these grew monotonically — the process RSS crept 4→7 GB
# and the GC pressure made the risk sweep's wall-clock worsen steadily
# through the session (2026-07-07 incident: elapsed 3.8s→10s→18.6s until
# the feed-leader event loop starved and prices froze; a restart "fixed"
# it only because a fresh process starts with empty dicts).
#
# Fix: once per full-book tick, drop every entry whose key is no longer in
# the live open book. That caps each dict at the live position/user count,
# so memory + GC stay flat all day and the latency no longer creeps up.
# Only triggers once a dict has grown past 2× the live set (or a small
# floor) so the steady-state common case stays allocation-free, and the
# transient "two shards briefly on one process" case can't churn a small
# dict every tick.
_STATE_PRUNE_FLOOR = 512


def _prune_stale_state(active_users: set[str], active_tokens: set[str]) -> None:
    def _prune(d: dict, active: set[str]) -> None:
        if len(d) <= max(_STATE_PRUNE_FLOOR, len(active) * 2):
            return
        for k in [k for k in d if k not in active]:
            d.pop(k, None)

    _prune(_warning_armed, active_users)
    _prune(_user_cache, active_users)
    _prune(_last_good_ltp, active_tokens)
    _prune(_outlier_pending, active_tokens)
    _prune(_ltp_warn_last, active_tokens)
    _prune(_rest_ltp_cache, active_tokens)
    _prune(_rest_ltp_fail_ts, active_tokens)


async def enforce_once(shard_id: int = 0, num_shards: int = 1) -> int:
    """One sweep across the users with open positions OWNED BY THIS SHARD.

    DEFAULT (shard_id=0, num_shards=1): every user — byte-for-byte the original
    single-worker behaviour (prices read in-process, no shard filter). With
    num_shards > 1 the user set is partitioned by `shard_of(user_id)` and prices
    are read from the leader's Redis `mdlive` snapshot instead of in-process.

    FLAT 2-QUERY DESIGN regardless of user/position count:
      1. ``Position.find(OPEN)``    — fetch every open position in one scan.
      2. ``User.find({_id: $in})``  — batch-load every affected user in one query.

    Positions are grouped by user_id in Python (zero extra DB round-trips),
    unique tokens are deduced from the same set (replaces the old
    ``distinct("instrument.token")`` second query), and LTPs are fetched from
    Redis in one parallel fan-out over those unique tokens.

    Before: 2 distinct() + N×User.get() + N×Position.find() = 2+2N queries.
    After:  2 queries total, N irrelevant.
    """
    from collections import defaultdict

    from bson import ObjectId

    _t0 = asyncio.get_event_loop().time()

    # ── 1. Fetch this shard's open positions ────────────────────────────
    # SINGLE-SHARD (num_shards<=1): pull the whole open book — byte-for-byte
    # the original behaviour.
    #
    # SHARDED (num_shards>1): each shard previously pulled the ENTIRE open
    # book and discarded the ~3/4 it doesn't own. With 4 shards that meant
    # 4 workers each deserialising all ~115 Position docs every 0.5 s — the
    # CPU-heavy Pydantic parse blocked each worker's event loop for 0.6-0.7 s
    # (the `positions_ms` spikes), inflating `ltp_ms` too and overrunning the
    # tick (626 overruns on the skewed shard). Instead we first read the cheap
    # `distinct(user_id)` of open positions (server-side, no doc parse), keep
    # only the user_ids THIS shard owns, then fetch ONLY those users' open
    # positions — so each shard parses ~1/num_shards of the book. Falls back
    # to the full scan on ANY error so a shard is NEVER left un-enforced.
    if num_shards > 1:
        try:
            _open_uids = await Position.get_motor_collection().distinct(
                "user_id", {"status": PositionStatus.OPEN.value}
            )
            _shard_oids = [
                uid for uid in _open_uids
                if shard_of(str(uid), num_shards) == shard_id
            ]
            if not _shard_oids:
                return 0
            all_open = await Position.find(
                Position.status == PositionStatus.OPEN,
                {"user_id": {"$in": _shard_oids}},
            ).to_list()
        except Exception:
            logger.exception("risk_shard_prefilter_failed_full_scan")
            all_open = await Position.find(
                Position.status == PositionStatus.OPEN
            ).to_list()
    else:
        all_open = await Position.find(
            Position.status == PositionStatus.OPEN
        ).to_list()
    if not all_open:
        return 0

    # Slow-lane the demo book: on 4 of every 5 ticks, drop demo positions so
    # only real-money positions are risk-enforced. Demo still gets stop-out /
    # SL-TP, just every ~2.5s instead of 0.5s — fine for fake money, and it
    # cuts the per-tick CPU that was starving the event loop (spikes + feed
    # reconnects). Real positions are processed on EVERY tick.
    global _sweep_tick
    _sweep_tick += 1
    # A "full" tick includes the demo book; the other 4-of-5 ticks drop it
    # (slow-lane). We only prune the per-process caches on a full tick so we
    # never transiently evict live demo-position state.
    _is_full_tick = (_sweep_tick % DEMO_SWEEP_EVERY_N == 0)
    if not _is_full_tick:
        all_open = [p for p in all_open if not p.is_demo]
        if not all_open:
            return 0

    _t1 = asyncio.get_event_loop().time()

    # ── 2. Group by user_id + collect unique tokens — pure Python ───────
    positions_by_user: dict[str, list[Position]] = defaultdict(list)
    all_token_set: set[str] = set()
    for p in all_open:
        positions_by_user[str(p.user_id)].append(p)
        all_token_set.add(str(p.instrument.token))

    # ── 2b. Shard filter — keep only the users THIS shard owns ──────────
    # Partition by USER (not position): a user's stop-out is computed over
    # ALL their positions, so every position of a user must stay together.
    # num_shards == 1 → shard_of is always 0 → no-op (today's exact set).
    if num_shards > 1:
        positions_by_user = {
            uid: ps
            for uid, ps in positions_by_user.items()
            if shard_of(uid, num_shards) == shard_id
        }
        if not positions_by_user:
            return 0
        all_token_set = {
            str(p.instrument.token)
            for ps in positions_by_user.values()
            for p in ps
        }

    user_id_strs = list(positions_by_user.keys())
    all_tokens = list(all_token_set)

    # Cap the per-process cache dicts to the live open book so memory + GC
    # stay flat all day (see _prune_stale_state). Only on a full-book tick
    # so demo-position state is never transiently evicted.
    if _is_full_tick:
        _prune_stale_state(set(user_id_strs), set(all_token_set))

    # ── 3. Batch LTP + quote snapshot from in-memory WS state ───────────
    # Both functions read `_state[token]` directly (pure dict lookup,
    # synchronous, zero network). `shared_quotes` is passed down to
    # `refresh_unrealized_pnl` via `prefetched_quote` so that it never
    # calls `get_quote` / `_overlay_all` / Zerodha REST — completely
    # eliminating the mid-sweep cache-expiry cascade that caused 500 ms–
    # 5000 ms sweep_ms spikes when the 700 ms _quote_cache TTL ran out
    # partway through a slow sweep.
    # ── 3-4. Prefetch LTP/quotes + users + wallets — CONCURRENTLY ───────
    # These three phases each depend ONLY on the positions-derived lists
    # (`all_tokens` / `user_id_strs`), never on each other, so they run under
    # ONE asyncio.gather instead of three serial awaits. Previously their
    # latencies ADDED (~ltp 438 + users 117 + wallets 283 ms in prod) BEFORE
    # the sweep even started, so on a contended tick the sweep overran 1 s and
    # a stop-out could fire LATE — a position ran well past its threshold
    # between checks (a ₹116 account seen stopped out at 193% vs 80%).
    # Concurrent, the phase collapses toward the slowest single round-trip.
    # WHICH data is fetched and the stop-out math are UNCHANGED — this only
    # removes serial waits so stop-outs fire sooner under load.
    #
    # `shared_quotes` is handed to refresh_unrealized_pnl as prefetched_quote
    # so it never calls get_quote/_overlay_all/Kite REST mid-sweep. Sharded
    # workers read the leader's execution-safe `mdlive` snapshot in ONE MGET
    # (both ltp + quote maps); single-shard is pure in-process dict lookups
    # (zero network). A token absent from mdlive → None/{} → the zero-LTP
    # guard SKIPS that position (never a wrong close). Users come from a
    # short-lived cache (only cache-misses hit Mongo); wallets are one $in.
    from app.models.wallet import Wallet

    oid_list = [ObjectId(uid) for uid in user_id_strs]

    # Users: synchronous cache check first — only cache-misses hit Mongo.
    users_by_id: dict[str, User] = {}
    _now_mono = time.monotonic()
    stale_ids: list[str] = []
    for uid in user_id_strs:
        ent = _user_cache.get(uid)
        if ent is not None and (_now_mono - ent[1]) < _USER_CACHE_TTL:
            users_by_id[uid] = ent[0]
        else:
            stale_ids.append(uid)
    stale_oids = [ObjectId(uid) for uid in stale_ids]

    async def _fetch_ltp_quotes() -> tuple[dict[str, Any], dict[str, Any]]:
        if num_shards > 1:
            return await market_data_service.get_ltp_quote_batch_mdlive(all_tokens)
        return (
            {tok: market_data_service.get_ltp_instant(tok) for tok in all_tokens},
            {tok: market_data_service.get_quote_instant(tok) for tok in all_tokens},
        )

    async def _fetch_stale_users() -> list:
        if not stale_oids:
            return []
        try:
            return await User.find({"_id": {"$in": stale_oids}}).to_list()
        except Exception:
            logger.exception("risk_enforcer_user_batch_fetch_failed")
            return []

    async def _fetch_wallets() -> list:
        try:
            return await Wallet.find({"user_id": {"$in": oid_list}}).to_list()
        except Exception:
            logger.exception("risk_enforcer_wallet_batch_fetch_failed")
            return []

    (shared_ltp, shared_quotes), _stale_users, _wallet_docs = await asyncio.gather(
        _fetch_ltp_quotes(), _fetch_stale_users(), _fetch_wallets()
    )

    # Fold cache-miss users into the cache + map.
    for u in _stale_users:
        _uid = str(u.id)
        _user_cache[_uid] = (u, _now_mono)
        users_by_id[_uid] = u

    wallets_by_user: dict[str, Any] = {str(w.user_id): w for w in _wallet_docs}

    _t2 = asyncio.get_event_loop().time()

    # Trigger WS subscription for zero-LTP numeric tokens so next tick has a price.
    zero_numeric = [
        t for t, v in shared_ltp.items()
        if (v is None or float(str(v or 0)) <= 0) and str(t).lstrip("-").isdigit()
    ]
    if zero_numeric:
        try:
            if num_shards > 1:
                # The Zerodha WS lives on the feed leader, not here. Announce
                # the tokens on the cross-worker `feed:subscribe` channel
                # (market_data_service.subscribe publishes it when this worker
                # isn't the feed leader) so the leader subscribes them.
                market_data_service.subscribe(zero_numeric)
            else:
                from app.services.zerodha_service import zerodha as _zs
                int_toks = [int(t) for t in zero_numeric]
                if int_toks and _zs.is_connected:
                    asyncio.create_task(
                        _zs.subscribe_tokens_on_demand(int_toks),
                        name="risk_ws_subscribe_batch",
                    )
        except Exception:
            pass

    # Timing points retained for the perf log. The old per-phase split
    # (ltp/users/wallets) is now a single concurrent prefetch, so `ltp_ms`
    # captures the whole gather and `users_ms`/`wallets_ms` read ~0.
    _t3 = asyncio.get_event_loop().time()
    _t3b = _t3

    # ── 5. Sweep each user using pre-fetched data ────────────────────────
    count = 0
    batch_size = max(1, _RISK_ENFORCE_BATCH_SIZE)
    for i in range(0, len(user_id_strs), batch_size):
        chunk = user_id_strs[i : i + batch_size]
        results = await asyncio.gather(
            *(
                _enforce_one_user_safe(
                    users_by_id.get(uid),
                    positions_by_user[uid],
                    shared_ltp,
                    wallets_by_user.get(uid),
                    shared_quotes,
                )
                for uid in chunk
            ),
            return_exceptions=True,
        )
        for r in results:
            if r is True:
                count += 1

    _t4 = asyncio.get_event_loop().time()
    _total_ms = round((_t4 - _t0) * 1000, 1)
    _sweep_ms = round((_t4 - _t3b) * 1000, 1)
    # Log at WARNING only when a tick overruns 400 ms (indicates a problem);
    # normal ticks go to DEBUG so production logs stay quiet.
    _log = logger.warning if (_total_ms > 400 or _sweep_ms > 300) else logger.debug
    _log(
        "risk_enforcer_perf",
        extra={
            "positions_ms": round((_t1 - _t0) * 1000, 1),
            "ltp_ms": round((_t2 - _t1) * 1000, 1),
            "users_ms": round((_t3 - _t2) * 1000, 1),
            "wallets_ms": round((_t3b - _t3) * 1000, 1),
            "sweep_ms": _sweep_ms,
            "total_ms": _total_ms,
            "n_positions": len(all_open),
            "n_users": len(user_id_strs),
            "n_tokens": len(all_tokens),
            "shard_id": shard_id,
            "num_shards": num_shards,
        },
    )
    return count


# Hard lower bound on inter-tick sleep. Even if a single tick somehow
# takes longer than `interval_sec`, we still yield this much so we
# never hot-spin and starve the event loop. 50 ms is short enough that
# the 250 ms cadence is preserved when ticks run on time, but long
# enough to keep the worker responsive to other coroutines if
# something goes wrong (network blip on Mongo, Redis hiccup, etc).
_MIN_TICK_GAP_SEC = 0.05


async def risk_enforcer_loop(
    interval_sec: float = 0.25, shard_id: int = 0, num_shards: int = 1
) -> None:
    """Background loop launched from the FastAPI lifespan.

    DEFAULT (shard_id=0, num_shards=1) = the original single-loop behaviour.
    When num_shards > 1 this loop only enforces the users this shard owns
    (`enforce_once(shard_id, num_shards)`); main.py starts one such loop per
    shard, each under its own `leader:risk:shard:{k}` lock on a different
    worker.

    250 ms cadence — four sweeps per second — so an SL/TP bracket or
    a stop-out threshold breach is caught within ~quarter of a second
    of the price crossing it, instead of the old 5-s gap that let
    LTP drift several ticks past the trigger before the close booked.

    Per-tick cost is small by design:
      • `enforce_once` issues ONE indexed Mongo `distinct` to find
        users with open positions.
      • `_enforce_for_user` parallelises every LTP lookup, and reads
        wallet + risk policy from Redis-backed cache.
      • `refresh_unrealized_pnl` is in-memory — no Mongo write per
        tick. Saves only happen when something actually fires
        (close, self-heal).
      • Bracket fires are idempotent (closed positions filter out of
        the next sweep) and the squareoff path uses an atomic Mongo
        claim to dedup across workers / leader handoffs.

    Drift-corrected sleep: we measure how long the tick took and
    sleep for the REMAINDER of the interval, not a fixed slice. So
    an 80-ms tick is followed by a 170-ms sleep — keeping the real
    cadence locked at `interval_sec` regardless of load, instead of
    compounding to 330 ms per tick the way a fixed sleep would.

    Overrun warning: if a tick exceeds the interval we log it once
    at WARNING (`risk_enforcer_tick_overrun`). That's the operator
    signal to either bump the interval or scale workers — we still
    complete the tick (no skip), but the user is told the risk loop
    is falling behind so they can act on it.
    """
    if shard_id in _running_shards:
        return
    _running_shards.add(shard_id)
    logger.info(
        "risk_enforcer_started",
        extra={"interval_sec": interval_sec, "shard_id": shard_id, "num_shards": num_shards},
    )
    try:
        loop = asyncio.get_event_loop()
        while shard_id in _running_shards:
            t0 = loop.time()
            try:
                await enforce_once(shard_id, num_shards)
            except Exception:
                logger.exception("risk_enforcer_tick_failed")
            elapsed = loop.time() - t0
            if elapsed > interval_sec:
                logger.warning(
                    "risk_enforcer_tick_overrun",
                    extra={
                        "elapsed_sec": round(elapsed, 3),
                        "interval_sec": interval_sec,
                        "shard_id": shard_id,
                        "num_shards": num_shards,
                    },
                )
            sleep_for = max(_MIN_TICK_GAP_SEC, interval_sec - elapsed)
            await asyncio.sleep(sleep_for)
    finally:
        _running_shards.discard(shard_id)
        logger.info("risk_enforcer_stopped", extra={"shard_id": shard_id})


def stop_risk_enforcer() -> None:
    _running_shards.clear()
