"""Real market-data service — Zerodha (Indian) + Infoway (forex/crypto/etc).

NO mock/random-walk price generation. When no real feed is connected for
a token (Zerodha WS not subscribed AND Infoway not subscribed AND REST
snapshot unavailable), this service returns a zero-valued quote so the
UI clearly shows "—" for bid/ask/LTP instead of inventing fake prices.

Background tick loop publishes ticks ONLY when a real overlay updates
the cached quote — the loop no longer steps prices itself.

The service exposes:
    • get_ltp(token) → current price (Decimal); returns 0 if no feed
    • get_quote(token) → full quote shape (zeros when no feed)
    • subscribe(tokens) / unsubscribe(tokens) — in-memory tracking
    • tick_loop — publishes per-token ticks to Redis pub/sub for WS fanout.
"""

from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

from app.core.redis_client import cache_get, cache_set, publish, publish_batch
from app.models.instrument import Instrument  # noqa: F401 — kept for downstream imports
from app.utils.decimal_utils import quantize_money, to_decimal

logger = logging.getLogger(__name__)

# In-memory state: token → quote dict
_state: dict[str, dict[str, Any]] = {}
_subscribed: set[str] = set()
_running: bool = False

# Last (ltp, bid, ask) actually PUBLISHED per token — the tick_loop only
# re-broadcasts a token whose price MOVED. Illiquid option strikes don't tick
# every second; re-publishing an identical quote was pure CPU (JSON serialise)
# + Redis fanout on the single-threaded feed-leader worker, which is what kept
# it at ~80% of one core and starved the co-located risk shard. mdlive is still
# refreshed every tick (freshness for execution), only the WS fanout is gated.
_last_pub: dict[str, tuple[Any, Any, Any]] = {}

# Multi-worker: True only on the worker that owns the upstream feed (set by
# main.py's feed leader-gate). Non-leader workers leave this False, which is
# what makes them PUBLISH cross-worker `feed:subscribe` requests instead of
# trying to subscribe a feed they don't run. On a single worker this flips
# True at boot, so subscribe() never publishes → behaviour identical to the
# pre-multi-worker code.
_is_feed_leader: bool = False


def set_feed_leader(is_leader: bool) -> None:
    """Called by main.py when this worker (de)acquires the feed leader lock."""
    global _is_feed_leader
    _is_feed_leader = bool(is_leader)


def is_feed_leader() -> bool:
    """True on the worker currently holding the `leader:feed` lock (runs the
    upstream feed + tick fanout). Used by the risk-shard admission gate to keep
    risk shards OFF the already-saturated feed-leader worker."""
    return _is_feed_leader


# ── Feed process split (FEED_GROUP) ──────────────────────────────────
# "all" (default) = one process feeds everything. "main" = Zerodha only
# (numeric instrument tokens). "global" = crypto/forex/metals only (symbol
# tokens). Set once at boot by main.py. Used to keep the global feed process
# from ever touching Zerodha (and vice-versa) when the feed is split across
# two cores — every subscription entry point filters to the process's group.
_feed_group: str = "all"


def set_feed_group(group: str) -> None:
    global _feed_group
    _feed_group = (group or "all").strip().lower()


def _token_is_numeric(token: Any) -> bool:
    """Zerodha instrument tokens are pure integers (e.g. 256265); Infoway /
    Binance / MetaAPI tokens are symbol strings (BTCUSDT, EURUSD, XAUUSD)."""
    return str(token).lstrip("-").isdigit()


def _in_feed_group(token: Any) -> bool:
    """True when a token belongs to THIS process's feed group."""
    if _feed_group == "all":
        return True
    numeric = _token_is_numeric(token)
    return numeric if _feed_group == "main" else (not numeric)


def _group_filter(tokens: list) -> list:
    """Drop tokens that belong to the OTHER feed group — so e.g. the global
    process never forwards a numeric token to Zerodha (which it doesn't run)."""
    if _feed_group == "all":
        return list(tokens)
    return [t for t in tokens if _in_feed_group(t)]


def prune_subscribed_numeric(keep_tokens: set) -> int:
    """Drop numeric (Zerodha) tokens from ``_subscribed`` / ``_state`` that are
    NOT in ``keep_tokens``. Called by the Zerodha LRU trim.

    THE FIX for the "option chain is 10 s behind live" incident: the trim
    already caps the Zerodha WS subscription at ``keep_count`` (~1200), but it
    never touched ``_subscribed`` / ``_state`` — the sets the ``tick_loop``
    actually iterates (``_state ∩ _subscribed``). So as a trader browsed option
    chains all day those two sets grew UNBOUNDED (7,171 tokens observed) while
    the WS stayed at 1200. The tick_loop then overlaid 7,171 tokens per pass on
    a single-core feed, stretching one pass to ~10 s → every published price
    (option chain, positions, risk enforcer's `mdlive`) was ~10 s stale.

    Bounding these two sets to the SAME keep-set as the WS keeps the tick_loop's
    per-pass work fixed no matter how long the session runs. Symbol-style
    (crypto/forex) tokens are left alone — they're the global feed's concern.
    mdlive keys for dropped tokens are left to expire on their own 30 s TTL.
    """
    keep_str = {str(int(t)) for t in keep_tokens if str(t).lstrip("-").isdigit()}
    dropped = 0
    for t in list(_subscribed):
        ts = str(t)
        if ts.lstrip("-").isdigit() and ts not in keep_str:
            _subscribed.discard(t)
            _state.pop(t, None)
            dropped += 1
    return dropped


# In-memory token → symbol cache (avoids MongoDB lookup on every tick)
_token_symbol_cache: dict[str, str | None] = {}
_TOKEN_CACHE_WARM = False


async def _warm_token_symbol_cache() -> None:
    """Pre-load token→symbol mapping from Instrument collection once."""
    global _TOKEN_CACHE_WARM
    if _TOKEN_CACHE_WARM:
        return
    try:
        instruments = await Instrument.find_all().to_list()
        for instr in instruments:
            if instr.token and instr.symbol:
                _token_symbol_cache[str(instr.token)] = instr.symbol.upper()
        _TOKEN_CACHE_WARM = True
        logger.info("token_symbol_cache_warmed count=%d", len(_token_symbol_cache))
    except Exception:
        logger.exception("token_symbol_cache_warm_failed")


def _get_cached_symbol(token: str) -> str | None:
    """O(1) in-memory lookup. Returns None if not cached."""
    return _token_symbol_cache.get(token)


async def _resolve_symbol(token: str) -> str | None:
    """Resolve token to Infoway symbol. Uses in-memory cache first, falls back to MongoDB."""
    cached = _token_symbol_cache.get(token)
    if cached is not None:
        return cached
    instr = await Instrument.find_one(Instrument.token == token)
    if instr is None or not instr.symbol:
        _token_symbol_cache[token] = None  # type: ignore[assignment]
        return None
    sym = instr.symbol.upper()
    _token_symbol_cache[token] = sym
    return sym


def _empty_quote(token: str) -> dict[str, Any]:
    """Zero-valued quote skeleton. Overlays (Zerodha / Infoway) fill in
    real numbers if the instrument is subscribed; otherwise the UI sees
    zeros and renders "—" placeholders instead of fake prices.
    """
    return {
        "token": token,
        "ltp": 0.0,
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "prev_close": 0.0,
        "change": 0.0,
        "change_pct": 0.0,
        "volume": 0,
        "bid": 0.0,
        "ask": 0.0,
        "depth": {"bids": [], "asks": []},
        "ts": 0,
    }


async def _ensure_quote(token: str) -> dict[str, Any]:
    """Return the in-memory quote slot for `token`, creating an empty one
    if needed. NEVER fabricates a price — the slot stays at zero until a
    real overlay (Zerodha tick / Infoway tick / Kite REST snapshot)
    fills it in."""
    if token in _state:
        return _state[token]
    _state[token] = _empty_quote(token)
    return _state[token]


async def _zerodha_overlay(
    token: str, base_quote: dict[str, Any], *, allow_rest: bool = True
) -> dict[str, Any]:
    """If Zerodha is streaming this instrument, replace the mock LTP / bid /
    ask / OHLC / volume / depth with live exchange data. Falls through silently
    on any error so the UI stays usable in dev."""
    try:
        # Kite instrument tokens are always pure integers (e.g. 738561 for RELIANCE).
        # Non-integer tokens like "CRYPTO_BTCUSD" or "FOREX_EURUSD" belong to the
        # Infoway feed — sending them to Kite REST results in an "Invalid exchange"
        # error that takes up to 2 s to surface, causing zerodha_overlay_timeout and
        # risk_enforcer_tick_overrun on every tick where a crypto position is open.
        try:
            int(token)
        except (TypeError, ValueError):
            return base_quote

        from app.services.zerodha_service import zerodha

        # Circuit breaker: if no Zerodha WS connection is alive, skip the
        # entire overlay.  Without this, every quote request during a WS
        # outage (daily token rotation, Kite slot release delay) floods
        # the event loop with 2-second timeouts from DB lookups and REST
        # fallbacks, starving the self-heal loop and locking the WS in
        # CONNECTING forever.
        with zerodha._ticker_lock:
            _any_connected = any(e.get("connected") for e in zerodha._tickers)
        if not _any_connected and not zerodha.ticks_by_token:
            return base_quote

        # 1) Direct token lookup — mirrored Zerodha subscriptions store the
        #    Kite instrument_token as Instrument.token, so this is the fast path.
        live: dict[str, Any] | None = None
        try:
            live = zerodha.ticks_by_token.get(int(token))
        except (TypeError, ValueError):
            live = None

        # 2) Resolve symbol + exchange so we can hit symbol cache and REST fallback.
        instr = None
        sym: str | None = None
        ex_str: str | None = None
        if live is None:
            instr = await Instrument.find_one(Instrument.token == token)
            if instr is not None and instr.symbol:
                sym = instr.symbol
                ex_attr = getattr(instr, "exchange", None)
                if ex_attr is not None:
                    ex_str = ex_attr.value if hasattr(ex_attr, "value") else str(ex_attr)

            # If the local Instrument is missing (option contracts subscribed
            # before mirror existed, etc.), look the same token up in the
            # Zerodha subscribed list — it carries symbol + exchange too.
            if sym is None:
                from app.models.zerodha_settings import ZerodhaSettings

                zsettings = await ZerodhaSettings.find_one()
                if zsettings is not None:
                    try:
                        token_int = int(token)
                    except (TypeError, ValueError):
                        token_int = None
                    sub = None
                    if token_int is not None:
                        sub = next((i for i in zsettings.subscribedInstruments if i.token == token_int), None)
                    if sub is None:
                        sub = next(
                            (i for i in zsettings.subscribedInstruments if str(i.token) == token),
                            None,
                        )
                    if sub is not None:
                        sym = sub.symbol
                        ex_str = sub.exchange

            # Last-resort lookup: scan the Zerodha in-memory instruments
            # cache itself. This unlocks live data for EVERY Kite-listed
            # symbol the user can see in the instruments panel — without
            # needing an explicit admin subscribe or a MongoDB mirror. The
            # cache is keyed per exchange and is warmed at startup, so the
            # scan is in-process and fast. With sym + ex_str resolved here,
            # `get_quote_snapshot` (REST) and `subscribe_tokens_on_demand`
            # (WS) downstream do the rest.
            if sym is None:
                try:
                    token_int = int(token)
                except (TypeError, ValueError):
                    token_int = None
                if token_int is not None:
                    for ex_key, cache in zerodha._instruments_cache.items():
                        match = next(
                            (r for r in cache if int(r.get("token") or 0) == token_int),
                            None,
                        )
                        if match is not None:
                            sym = match.get("symbol")
                            ex_str = (match.get("exchange") or ex_key).upper()
                            break

        # 3) Symbol-keyed live tick (covers seeded NSE_EQ_RELIANCE-style tokens
        #    where the local token is text but the live tick is keyed by symbol).
        if live is None and sym:
            live = zerodha.ticks_by_symbol.get(sym)

        # 4) REST `/quote` fallback — when the ticker has no recent push for
        #    this instrument (weekends, pre-open, fresh subscribe before the
        #    first tick arrives) we still want real exchange data, not mock.
        if not live and sym and ex_str and allow_rest:
            snap = await zerodha.get_quote_snapshot(ex_str, sym)
            if snap:
                live = snap

        if not live:
            return base_quote

        merged = dict(base_quote)
        merged["ltp"] = live.get("ltp", merged["ltp"])
        merged["open"] = live.get("open", merged["open"])
        merged["high"] = live.get("high", merged["high"])
        merged["low"] = live.get("low", merged["low"])
        merged["prev_close"] = live.get("close", merged["prev_close"])
        merged["volume"] = live.get("volume", merged["volume"])
        # Carry the exchange's own packet time through to `_state` → mdlive →
        # the published tick, so EVERY worker (esp. non-leaders that only see
        # mdlive) can gate order freshness on the live-session signal rather
        # than the snapshot-fooled frame-arrival time.
        if live.get("exchange_timestamp"):
            merged["exchange_timestamp"] = live.get("exchange_timestamp")

        # 5-level depth — Kite ticks expose depth.buy/depth.sell when
        # subscribed in MODE_FULL. Translate to our schema (bids/asks) and
        # ALSO derive top-of-book bid/ask from it.
        depth = live.get("depth")
        best_bid_from_depth: float | None = None
        best_ask_from_depth: float | None = None
        if isinstance(depth, dict):
            bids = depth.get("buy") or []
            asks = depth.get("sell") or []
            if bids or asks:
                merged["depth"] = {
                    "bids": [
                        {
                            "price": float(b.get("price") or 0),
                            "qty": int(b.get("quantity") or 0),
                            "orders": int(b.get("orders") or 0),
                        }
                        for b in bids[:5]
                    ],
                    "asks": [
                        {
                            "price": float(a.get("price") or 0),
                            "qty": int(a.get("quantity") or 0),
                            "orders": int(a.get("orders") or 0),
                        }
                        for a in asks[:5]
                    ],
                }
                try:
                    if bids and float(bids[0].get("price") or 0) > 0:
                        best_bid_from_depth = float(bids[0]["price"])
                    if asks and float(asks[0].get("price") or 0) > 0:
                        best_ask_from_depth = float(asks[0]["price"])
                except (TypeError, ValueError, KeyError):
                    pass

        # Bid / ask resolution — ONLY real exchange data:
        #   1. Explicit `live.bid` / `live.ask` (set by REST snapshot
        #      or MODE_FULL pushes)
        #   2. Top of Kite depth book (MODE_FULL ticks)
        #   No synthesised fallback — when no real bid/ask is available,
        #   leave the field at 0 so downstream code (order panel, limit-
        #   away validator) can detect "no live quote" and refuse to
        #   accept opening trades on illiquid contracts. The admin
        #   segment-spread overlay that runs LATER in this chain still
        #   gets to synthesise bid/ask from LTP ± half-spread for
        #   segments with `spread_pips > 0` — only instruments with
        #   neither real depth NOR an admin spread end up at 0.
        #
        # Operator-flagged 22-May: deep-OTM options like GOLD150000CE
        # had no real depth and no admin spread, so bid/ask were quietly
        # mirrored to the (stale) LTP and the order panel showed a
        # "tradeable" price on contracts that physically can't fill.
        # Trade would either reject mid-flight or fill at a junk price.
        live_bid = float(live.get("bid") or 0)
        live_ask = float(live.get("ask") or 0)
        if live_bid > 0:
            merged["bid"] = live_bid
        elif best_bid_from_depth and best_bid_from_depth > 0:
            merged["bid"] = best_bid_from_depth
        else:
            merged["bid"] = 0.0
        if live_ask > 0:
            merged["ask"] = live_ask
        elif best_ask_from_depth and best_ask_from_depth > 0:
            merged["ask"] = best_ask_from_depth
        else:
            merged["ask"] = 0.0

        if merged["prev_close"]:
            merged["change"] = round(merged["ltp"] - merged["prev_close"], 2)
            merged["change_pct"] = round((merged["change"] / merged["prev_close"]) * 100, 2)
        merged["source"] = "zerodha"
        return merged
    except Exception:
        return base_quote


async def _infoway_overlay(token: str, base_quote: dict[str, Any]) -> dict[str, Any]:
    """Overlay live Infoway tick (forex / crypto / metals / energy). Infoway
    is keyed by SYMBOL (BTCUSDT, EURUSD, XAUUSD…); we resolve via the
    Instrument doc.

    Don't gate on `infoway.is_connected` — that property reaches into the
    websockets client object whose API changed between library versions and
    can throw / return False even while ticks are still in the cache. The
    cache itself is the source of truth: if there's a fresh tick keyed by
    this symbol, use it.
    """
    try:
        from app.services.infoway_service import infoway

        sym = await _resolve_symbol(token)
        if not sym:
            return base_quote
        # CRYPTO-first source: when the Binance feed is enabled, crypto symbols
        # get their tick from Binance (matches the Binance chart users see,
        # smoother/faster). ONLY subscribed crypto symbols resolve in Binance —
        # forex / metals / energy return None here and fall through to Infoway
        # below, completely unchanged.
        live: dict[str, Any] | None = None
        source = "infoway"
        try:
            from app.services.binance_service import binance

            if binance.is_enabled():
                b = binance.get_tick(sym) or binance.get_tick(sym + "T")
                if b and float(b.get("ltp") or 0) > 0:
                    live = b
                    source = "binance"
        except Exception:
            live = None
        # FOREX / METALS / INDICES / COMMODITIES → MetaAPI when enabled. Only the
        # symbols the MT account actually streams resolve here; crypto / NSE
        # return None and fall through. Infoway stays the automatic fallback.
        if live is None:
            try:
                from app.services.metaapi_service import metaapi

                if metaapi.is_enabled():
                    m = metaapi.get_tick(sym)
                    if m and float(m.get("ltp") or 0) > 0:
                        live = m
                        source = "metaapi"
            except Exception:
                live = None
        if live is None:
            live = infoway.get_tick(sym) or infoway.get_tick(sym + "T")
        if not live:
            return base_quote
        ltp = float(live.get("ltp") or 0)
        if ltp <= 0:
            return base_quote
        merged = dict(base_quote)
        merged["ltp"] = ltp
        # Real best-bid / best-ask from Infoway depth book. When the feed
        # publishes no bid/ask (dead / illiquid symbol), leave the side at
        # 0 instead of collapsing to LTP — the order panel reads a 0 here
        # as "no live quote" and disables that side of the trade. The
        # admin segment-spread overlay still runs AFTER this; segments
        # with `spread_pips > 0` will synthesise bid/ask from LTP, so
        # only symbols with neither real depth NOR an admin spread end
        # up at 0.
        live_bid = float(live.get("bid") or 0)
        live_ask = float(live.get("ask") or 0)
        merged["bid"] = live_bid if live_bid > 0 else 0.0
        merged["ask"] = live_ask if live_ask > 0 else 0.0
        merged["volume"] = float(live.get("volume") or merged.get("volume") or 0)
        if live.get("close_24h"):
            merged["prev_close"] = float(live["close_24h"])
        merged["change"] = float(live.get("change") or merged.get("change") or 0)
        merged["change_pct"] = float(live.get("change_pct") or merged.get("change_pct") or 0)
        # Running intraday OHLC synthesised from tick stream — overlay only
        # when the Infoway service has seen at least one tick for this symbol.
        if live.get("open"):
            merged["open"] = float(live["open"])
        if live.get("high"):
            merged["high"] = float(live["high"])
        if live.get("low"):
            merged["low"] = float(live["low"])
        # Overlay real depth too if Infoway has a book for this symbol
        # Depth book only exists for Infoway symbols (Binance @ticker carries
        # best bid/ask only — already merged above).
        if source == "infoway":
            depth = infoway.depth.get(sym) or infoway.depth.get(sym + "T")
            if depth and depth.get("bids") and depth.get("asks"):
                merged["depth"] = {"bids": depth["bids"], "asks": depth["asks"]}
        merged["source"] = source
        # Provider's own tick time, for the stale-price guard. Set it even
        # when absent (None) so a fresh quote can never inherit the previous
        # tick's timestamp from `base_quote` and look older than it is.
        _fts = live.get("feed_ts")
        merged["feed_ts"] = float(_fts) if _fts else None
        # USD/INR snapshot so the frontend can show margin in real INR
        # rather than displaying the USD number with a ₹ symbol (which is
        # how users end up trying to place orders worth 80× their wallet).
        merged["fx_rate"] = get_usd_inr_rate()
        return merged
    except Exception:
        logger.exception("infoway_overlay_failed token=%s", token)
        return base_quote


async def _overlay_all(
    token: str, base: dict[str, Any], *, allow_rest: bool = True
) -> dict[str, Any]:
    """Apply Infoway first (forex/crypto/metals/energy), then Zerodha (Indian).
    Whichever provider has live data wins.

    Both overlays may hit external services — Infoway reads from a local
    in-memory tick map (fast) but the Zerodha fallback issues a Kite REST
    `/quote` call that can stall when Kite is slow / TCP RST'd. We hard-cap
    each overlay at 2 seconds so a hung external service can NEVER freeze
    callers like `order_service.place_order` → `matching_engine.execute_market_order`
    → `get_ltp`. On timeout the overlay falls back to the cached base quote
    (last-known real value or zero) — never a fabricated price.

    After both feed overlays run, the admin's per-segment spread (Fixed /
    Floating + spread_pips) is applied as the final pass. This is the
    "money changer" markup — bid moves down half-spread, ask moves up
    half-spread, around the live LTP. Cached resolution per
    `(segment, symbol)` for 30 s so the 250 ms WS pump doesn't go to
    Mongo on every tick.
    """
    try:
        # ── Fast path: already-ticking Zerodha (numeric) token ──────────────
        # The dominant tick_loop case. A numeric token that already has a live
        # in-memory KiteTicker tick resolves ENTIRELY from `ticks_by_token`
        # (no DB / no REST await can fire inside `_zerodha_overlay`), so we
        # skip the two `asyncio.wait_for(...)` wrappers below. Each wrapper
        # allocates a Task + timer callback even when the wrapped coroutine
        # never suspends; at ~1350 subscribed tokens × 10 Hz that was ~27k
        # throwaway tasks/sec pinning the single-core feed leader near 100% CPU
        # and making `tick_loop` fall behind its 100 ms cadence (=> the ~2 s
        # option-chain price lag). Numeric tokens are never Infoway symbols
        # (that feed is non-numeric), so skipping the Infoway overlay here is
        # also correct — not just faster. Cold tokens (no live tick yet) and
        # Infoway symbols fall through to the timeout-protected path unchanged.
        if not allow_rest:
            _tok_int: int | None
            try:
                _tok_int = int(token)
            except (TypeError, ValueError):
                _tok_int = None
            if _tok_int is not None:
                from app.services.zerodha_service import zerodha

                if zerodha.ticks_by_token.get(_tok_int) is not None:
                    try:
                        q = await _zerodha_overlay(token, base, allow_rest=False)
                    except Exception:
                        logger.debug(
                            "zerodha_overlay_fastpath_failed token=%s", token, exc_info=True
                        )
                        q = base
                    return await _apply_admin_spread(token, q)

        after_infoway = await asyncio.wait_for(_infoway_overlay(token, base), timeout=2.0)
    except asyncio.TimeoutError:
        logger.warning("infoway_overlay_timeout", extra={"token": token})
        after_infoway = base
    except Exception:
        logger.exception("infoway_overlay_failed", extra={"token": token})
        after_infoway = base
    if after_infoway.get("source") in ("infoway", "binance", "metaapi"):
        return await _apply_admin_spread(token, after_infoway)
    # Per-token negative cache: a token with no live WS tick whose Kite REST
    # `/quote` snapshot just timed out is almost certainly illiquid / beyond
    # the WS cap / not pushing. Re-issuing the 2 s REST on every 5 s pump tick
    # froze the event loop for nothing (the timeout already falls back to
    # `base`). Skip ONLY the REST fallback for a short window; the fast
    # in-memory `ticks_by_token` / `ticks_by_symbol` checks inside
    # `_zerodha_overlay` STILL run, so the instant a token starts ticking
    # again it is served live (the warm path is reached before this gate).
    _now = _t.time()
    # `allow_rest=False` forces WS-only (no Kite REST fallback). The background
    # tick_loop passes it so the 1 s pump NEVER blocks on a 2 s Kite REST for a
    # cold token — with many un-ticked tokens that flooded zerodha_overlay_timeout
    # and (because the feed leader also runs a risk shard) starved the risk sweep
    # into 3-6 s tick_overruns. On-demand get_quote() (order panel) keeps REST on.
    _allow_rest = allow_rest and (_now >= _zerodha_rest_skip.get(token, 0.0))
    try:
        zerodha_quote = await asyncio.wait_for(
            _zerodha_overlay(token, base, allow_rest=_allow_rest), timeout=2.0
        )
    except asyncio.TimeoutError:
        logger.warning("zerodha_overlay_timeout", extra={"token": token})
        _zerodha_rest_skip[token] = _now + _ZERODHA_REST_SKIP_TTL_SEC
        zerodha_quote = base
    except Exception:
        logger.exception("zerodha_overlay_failed", extra={"token": token})
        zerodha_quote = base
    return await _apply_admin_spread(token, zerodha_quote)


# Token → segment cache so the spread step doesn't re-fetch the Instrument
# doc on every tick. Segment is essentially immutable for a token (changes
# only via admin edit), so a 5-min TTL is plenty. Misses fall through to
# Mongo and re-cache. Falsy values aren't cached (an instrument that doesn't
# exist yet might be mirrored on the next call).
_SEGMENT_FOR_TOKEN_TTL = 300
_SEGMENT_FOR_TOKEN_PREFIX = "spread_seg:"


async def _segment_for_token(token: str) -> tuple[str, str] | None:
    """Return `(segment_type, symbol_upper)` for a token, or None if the
    instrument isn't in our collection."""
    cache_key = f"{_SEGMENT_FOR_TOKEN_PREFIX}{token}"
    try:
        from app.core.redis_client import cache_get, cache_set

        cached = await cache_get(cache_key)
        if cached is not None:
            return (cached.get("seg") or "", cached.get("sym") or "")
    except Exception:
        cache_set = None  # type: ignore[assignment]

    instr = await Instrument.find_one(Instrument.token == token)
    if instr is None:
        return None
    seg_value = getattr(instr.segment, "value", instr.segment)
    sym = (instr.symbol or "").upper()
    payload = {"seg": str(seg_value), "sym": sym}
    try:
        if cache_set is not None:
            await cache_set(cache_key, payload, ttl_sec=_SEGMENT_FOR_TOKEN_TTL)
    except Exception:
        pass
    return (str(seg_value), sym)


async def get_segment_for_token(token: str) -> tuple[str, str] | None:
    """Public wrapper — return ``(segment_type, symbol_upper)`` for an
    instrument token, or ``None`` when not found. Results are cached in
    Redis for ~5 min so repeated calls during subscribe are cheap."""
    return await _segment_for_token(token)


async def _apply_admin_spread(token: str, quote: dict[str, Any]) -> dict[str, Any]:
    """Final overlay: apply the admin-configured spread to the live quote.

    Fixed mode  → bid = ltp − pips/2, ask = ltp + pips/2 every tick. The
                  exchange spread is ignored entirely (broker-set markup).
    Floating    → keep the live (ask − bid), but widen symmetrically around
                  ltp when it falls below `spread_pips`. Implements the
                  "real spread, but never less than minimum" rule.

    `spread_pips` is interpreted as PRICE UNITS for that instrument (admin
    sees the same units they'd see on the chart — 0.0002 for EURUSD,
    0.50 for XAUUSD, 5 for NIFTY). Zero or negative → no spread mod.

    Skipped when `spread_pips <= 0` so admin can opt out by leaving the
    field blank.
    """
    try:
        ltp = float(quote.get("ltp") or 0)
        if ltp <= 0:
            return quote

        seg_sym = await _segment_for_token(token)
        if seg_sym is None:
            return quote
        seg_type, symbol = seg_sym

        # Translate instrument segment → admin row name (NSE_EQ / FOREX /
        # CRYPTO / …) the way the rest of the resolver stack does.
        from app.services.netting_service import resolve_admin_row, resolve_spread

        admin_row = resolve_admin_row(seg_type, symbol)
        cfg = await resolve_spread(admin_row, symbol)
        pips = float(cfg.get("spread_pips") or 0)
        if pips <= 0:
            return quote
        mode = str(cfg.get("spread_type") or "fixed").lower()

        half = pips / 2.0
        live_bid = float(quote.get("bid") or 0)
        live_ask = float(quote.get("ask") or 0)
        live_spread = (live_ask - live_bid) if (live_bid > 0 and live_ask > 0) else 0.0

        if mode == "fixed":
            merged = dict(quote)
            merged["bid"] = ltp - half
            merged["ask"] = ltp + half
            return merged

        # Floating: keep market spread until it's tighter than the minimum,
        # then widen to the minimum around the LTP midpoint.
        if live_spread < pips:
            merged = dict(quote)
            merged["bid"] = ltp - half
            merged["ask"] = ltp + half
            return merged
        return quote
    except Exception:
        logger.exception("admin_spread_overlay_failed", extra={"token": token})
        return quote


# ── USD → INR conversion (forex / crypto P&L is reported in INR) ─────
USD_INR_FALLBACK = 83.0  # used only if Infoway hasn't pushed a USDINR tick yet


def get_usd_inr_rate() -> float:
    """Live USD/INR conversion rate. Infoway subscribes to USDINR by default,
    so this is the rate at which crypto / forex P&L gets translated for
    Indian wallets. Falls back to a sensible constant on a cold start."""
    try:
        from app.services.infoway_service import infoway

        for sym in ("USDINR", "USDINR=X", "USD/INR"):
            tick = infoway.get_tick(sym)
            if tick:
                ltp = float(tick.get("ltp") or 0)
                if ltp > 0:
                    return ltp
    except Exception:  # noqa: BLE001
        pass
    return USD_INR_FALLBACK


def is_infoway_lot_segment(segment: str | None) -> bool:
    """Whether the segment is sourced from the Infoway feed for lot-table
    resolution purposes. STOCKS / INDICES / CRYPTO / FOREX / COMMODITIES /
    CDS — every segment that does NOT come from the Zerodha-fed NSE / BSE
    / MCX / NFO / BFO lot tables.

    This is the segment classifier the ORDER PLACEMENT path uses to pick
    between `infoway_lots.get_infoway_lot_size` and the canonical Indian
    lot tables. It is intentionally separate from `is_usd_quoted_segment`
    below: that flag now governs *FX conversion* of P&L / margin only, and
    has been disabled per the broker's spec ("treat the Infoway feed price
    as INR directly"). Without this split, disabling FX conversion would
    also break lot-size resolution for forex/crypto/etc.
    """
    s = (segment or "").upper()
    if s in ("STOCKS", "INDICES"):
        return True
    return (
        "CRYPTO" in s
        or "FOREX" in s
        or "FX" in s
        or "CDS" in s
        or "COMMODITIES" in s
    )


def is_usd_quoted_segment(segment: str | None) -> bool:
    """Whether the segment's P&L / margin needs USD→INR conversion.

    Per the broker's spec, Infoway-feed prices (forex / crypto / spot
    metals / energy / international stocks & indices) are now treated as
    INR directly — there is no live USD→INR multiplication on P&L, no
    margin scaling, and the UI renders these prices with ₹ rather than $.
    So this helper always returns False; every legacy call site that gated
    FX conversion on it becomes a no-op without further changes.

    For Infoway lot-table selection use ``is_infoway_lot_segment`` above.
    """
    # Reference the parameter so linters don't flag it unused — the
    # signature is kept stable for every existing caller.
    _ = segment
    return False


# Short in-process overlay cache (token → (timestamp_ms, payload)). The
# overlay pipeline blocks on Zerodha + Infoway sequentially with a 2 s
# timeout each — when the chart datafeed, the OrderPanel, the
# MobileQuickTradeBar, and the positions overlay all call get_quote for
# the same token within ~500 ms, the user paid for that overlay 4 times.
# A 700 ms TTL is short enough that a stale price never lingers visibly
# (the WS pump runs every 1 s anyway) but kills the duplicate-fanout cost.
import time as _t

_QUOTE_CACHE_TTL_MS = 700
_quote_cache: dict[str, tuple[int, dict[str, Any]]] = {}

# Per-token negative cache for the Zerodha REST `/quote` fallback. A token
# that has no live WS tick AND whose REST snapshot times out is marked here
# so subsequent get_quote calls skip the (useless, 2 s) REST for this window
# instead of re-freezing the pump every tick. Short TTL + the fact that a
# revived token is served by the in-memory tick path BEFORE the REST gate
# means a recovering symbol is never held back. Bounded by the count of
# distinct dead tokens (a few hundred at most); entries self-expire by time.
_ZERODHA_REST_SKIP_TTL_SEC = 30.0
_zerodha_rest_skip: dict[str, float] = {}


# ── Last-known price (DISPLAY-ONLY fallback) ─────────────────────────
# When a market is closed (e.g. spot gold XAUUSD over the weekend) or the
# Infoway/Zerodha feed has no live tick, the overlay yields ltp = 0 and the
# UI shows a dead "0.00 / feed unavailable" panel. To still show the trader
# a reference, we persist the LAST good quote per token to Redis and, when
# the live overlay produces no price, attach it as a SEPARATE `last_ltp`
# field (plus the last OHLC for the header).
#
# SAFETY: we deliberately do NOT revive the live `ltp` / `bid` / `ask`.
# Those stay 0 so (a) the matching engine keeps rejecting fills on a dead
# feed (`get_ltp` returns 0 → STALE_FEED guard) and (b) the order panel's
# BUY/SELL stay disabled. `last_ltp` is for display only — never execution.
_LAST_QUOTE_KEY = "mdlast:{token}"
_LAST_QUOTE_TTL_SEC = 7 * 86400  # a week — covers weekend / holiday feed gaps


async def _persist_last_quote(token: str, q: dict[str, Any]) -> None:
    try:
        if float(q.get("ltp") or 0) > 0:
            await cache_set(
                _LAST_QUOTE_KEY.format(token=token),
                {
                    "ltp": float(q["ltp"]),
                    "open": float(q.get("open") or 0),
                    "high": float(q.get("high") or 0),
                    "low": float(q.get("low") or 0),
                    "prev_close": float(q.get("prev_close") or 0),
                    "source": q.get("source"),
                    "ts": int(_t.time() * 1000),
                },
                ttl_sec=_LAST_QUOTE_TTL_SEC,
            )
    except Exception:  # pragma: no cover - cache write must never break a quote
        pass


async def _attach_last_quote(token: str, q: dict[str, Any]) -> dict[str, Any]:
    # Live price present → nothing to do.
    if float(q.get("ltp") or 0) > 0:
        return q
    try:
        last = await cache_get(_LAST_QUOTE_KEY.format(token=token))
    except Exception:  # pragma: no cover
        last = None
    if not last or float(last.get("ltp") or 0) <= 0:
        return q
    out = dict(q)
    out["last_ltp"] = float(last["ltp"])
    out["last_ts"] = last.get("ts")
    out["stale"] = True
    # Fill the header OHLC so the chart strip shows the last session's
    # numbers instead of "—". Does NOT touch ltp/bid/ask (kept at 0).
    for k in ("open", "high", "low", "prev_close"):
        if not out.get(k) and last.get(k):
            out[k] = float(last[k])
    return out


# ── Live cross-worker price snapshot (multi-worker, execution-safe) ──
# Only the LEADER worker runs the upstream feed + tick_loop, so non-leader
# workers have a permanently-cold `_state`. The leader mirrors each live
# overlaid quote into Redis (`mdlive:{token}`) every tick; a non-leader's
# `get_quote` reads it BEFORE `_overlay_all` (which would otherwise fire a
# ~2 s Kite REST per cold quote). Unlike the display-only `mdlast`, `mdlive`
# carries live ltp/bid/ask exactly as the leader served it and IS safe for
# execution — short TTL so a dead leader can't feed stale fills. On a single
# warm worker `_state` is always warm, so the cold-path read never runs and
# behaviour is unchanged.
_MDLIVE_KEY = "mdlive:{token}"
_MDLIVE_TTL_SEC = 30

# Cross-worker feed-subscription channel (see subscribe / feed_subscribe_listener).
FEED_SUBSCRIBE_CHANNEL = "feed:subscribe"


async def _write_mdlive_batch(items: list[tuple[str, dict[str, Any]]]) -> None:
    """Leader-only: mirror this tick's live quotes to Redis in ONE pipeline.
    Best-effort — a cache write must never break the tick loop."""
    if not items:
        return
    try:
        from app.core.redis_client import get_redis

        pipe = get_redis().pipeline(transaction=False)
        for token, payload in items:
            pipe.set(
                _MDLIVE_KEY.format(token=token),
                json.dumps(payload, default=str),
                ex=_MDLIVE_TTL_SEC,
            )
        await pipe.execute()
    except Exception:  # pragma: no cover - never break the tick on a cache hiccup
        logger.debug("mdlive_write_failed", exc_info=True)


async def _read_mdlive(token: str) -> dict[str, Any] | None:
    """Read the leader's live snapshot. Returns None when absent / non-positive
    so the caller falls back to the normal overlay path."""
    try:
        data = await cache_get(_MDLIVE_KEY.format(token=token))
    except Exception:  # pragma: no cover
        return None
    if not isinstance(data, dict):
        return None
    try:
        if float(data.get("ltp") or 0) <= 0:
            return None
    except Exception:
        return None
    return data


async def get_ltp_batch_mdlive(tokens: list[str]) -> dict[str, Decimal | None]:
    """Batch LTP read from the leader's `mdlive` snapshot — ONE Redis MGET.

    Used by the risk_enforcer in SHARDED mode (RISK_SHARDS > 1): a shard runs
    on a NON-feed-leader worker whose in-process `_state` is cold, so it can't
    use `get_ltp_instant`. The feed leader mirrors every live quote to
    `mdlive:{token}` (30 s TTL, execution-safe); this reads them back in bulk.
    Mirrors `_read_mdlive`'s guard: ltp <= 0 / missing → None (the risk loop's
    zero-LTP guard then SKIPS that position safely — never a wrong close).
    """
    out: dict[str, Decimal | None] = {t: None for t in tokens}
    if not tokens:
        return out
    try:
        from app.core.redis_client import get_redis

        raw = await get_redis().mget([_MDLIVE_KEY.format(token=t) for t in tokens])
    except Exception:  # pragma: no cover - never break the sweep on a cache hiccup
        return out
    for t, val in zip(tokens, raw):
        if not val:
            continue
        try:
            ltp = float(json.loads(val).get("ltp") or 0)
            if ltp > 0:
                out[t] = quantize_money(to_decimal(ltp))
        except Exception:
            continue
    return out


async def get_quote_batch_mdlive(tokens: list[str]) -> dict[str, dict[str, Any]]:
    """Batch full-quote read from the leader's `mdlive` snapshot — ONE Redis
    MGET. Sharded-mode counterpart of `get_quote_instant`. Returns the same
    execution-safe quote dict the feed leader wrote (ltp / bid / ask / OHLC /
    `exchange_timestamp`). Missing / non-positive-ltp tokens map to ``{}`` so
    callers (refresh_unrealized_pnl) fall back to the LTP gracefully, exactly
    like `get_quote_instant`.
    """
    out: dict[str, dict[str, Any]] = {t: {} for t in tokens}
    if not tokens:
        return out
    try:
        from app.core.redis_client import get_redis

        raw = await get_redis().mget([_MDLIVE_KEY.format(token=t) for t in tokens])
    except Exception:  # pragma: no cover
        return out
    for t, val in zip(tokens, raw):
        if not val:
            continue
        try:
            data = json.loads(val)
            if float(data.get("ltp") or 0) > 0:
                out[t] = data
        except Exception:
            continue
    return out


async def get_ltp_quote_batch_mdlive(
    tokens: list[str],
) -> tuple[dict[str, Decimal | None], dict[str, dict[str, Any]]]:
    """ONE Redis MGET → BOTH the LTP map and the full-quote map.

    The sharded risk loop needs both every tick. Fetching them via the two
    SEPARATE helpers (`get_ltp_batch_mdlive` + `get_quote_batch_mdlive`) did
    TWO MGETs over the SAME `mdlive:{token}` keys and parsed each JSON value
    TWICE every tick — on 4 shards that doubled the Redis round-trips + parse
    work and showed up as correlated `ltp_ms` spikes (1-2 s) when all shards
    hit Redis at once. This does a single MGET and one parse per value, then
    derives the LTP from the same dict. Guards are identical: ltp <= 0 /
    missing → None / {} so the risk loop's zero-LTP guard still skips safely.
    """
    ltp_out: dict[str, Decimal | None] = {t: None for t in tokens}
    quote_out: dict[str, dict[str, Any]] = {t: {} for t in tokens}
    if not tokens:
        return ltp_out, quote_out
    try:
        from app.core.redis_client import get_redis

        raw = await get_redis().mget([_MDLIVE_KEY.format(token=t) for t in tokens])
    except Exception:  # pragma: no cover - never break the sweep on a cache hiccup
        return ltp_out, quote_out
    for t, val in zip(tokens, raw):
        if not val:
            continue
        try:
            data = json.loads(val)
            ltp = float(data.get("ltp") or 0)
            if ltp > 0:
                quote_out[t] = data
                ltp_out[t] = quantize_money(to_decimal(ltp))
        except Exception:
            continue
    return ltp_out, quote_out


async def _forward_feed_subscription(tokens: list[str]) -> None:
    """Leader-side: subscribe cross-worker-requested tokens on the real feeds.
    Numeric tokens → Zerodha ticker; symbol-style tokens → Infoway. Adds them
    to `_subscribed` so the tick_loop mirrors them into `mdlive` + pub/sub."""
    # Feed-split: forward only this process's group (global never talks to
    # Zerodha; main never talks to Infoway). No-op when FEED_GROUP="all".
    tokens = _group_filter(tokens)
    if not tokens:
        return
    _subscribed.update(tokens)
    # Seed `_state` on the leader (see `subscribe`): symbol-style MetaAPI /
    # Infoway tokens have no upstream WS to populate `_state`, so without this
    # they never enter the leader's `tick_loop` publish set (`_state ∩
    # _subscribed`) → frozen 0.0000. `_overlay_all` fills the live price next tick.
    for t in tokens:
        if t and t not in _state:
            _state[t] = _empty_quote(t)
    infoway_codes = [t for t in tokens if t and not str(t).lstrip("-").isdigit()]
    numeric: list[int] = []
    for t in tokens:
        try:
            numeric.append(int(t))
        except (TypeError, ValueError):
            pass
    if infoway_codes:
        try:
            from app.services.infoway_service import infoway

            await infoway.subscribe(infoway_codes)
        except Exception:  # pragma: no cover
            logger.debug("feed_forward_infoway_failed", exc_info=True)
        # MetaAPI is the primary feed for forex/metals/indices/commodities.
        # Subscribe the same symbol-style tokens upstream so a watchlist symbol
        # outside the startup list gets a live terminal_state price instead of 0.
        try:
            from app.services.metaapi_service import metaapi

            if metaapi.is_enabled():
                await metaapi.subscribe(infoway_codes)
        except Exception:  # pragma: no cover
            logger.debug("feed_forward_metaapi_failed", exc_info=True)
    if numeric:
        try:
            from app.services.zerodha_service import zerodha

            await zerodha.subscribe_tokens_on_demand(numeric)
        except Exception:  # pragma: no cover
            logger.debug("feed_forward_zerodha_failed", exc_info=True)


async def feed_subscribe_listener() -> None:
    """LEADER-ONLY loop: consume cross-worker `feed:subscribe` requests and
    forward them to the upstream feeds. Started under the feed leader-gate in
    main.py (same gate as the feed + tick_loop, so it co-locates with them).
    Reconnects on a dropped Redis connection; exits cleanly on cancel."""
    from app.core.redis_client import pubsub

    backoff = 1.0
    ps: Any = None
    try:
        while True:
            try:
                if ps is None:
                    ps = pubsub()
                    await ps.subscribe(FEED_SUBSCRIBE_CHANNEL)
                    logger.info("feed_subscribe_listener_started")
                async for msg in ps.listen():
                    if msg.get("type") != "message":
                        continue
                    raw = msg.get("data")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "ignore")
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    toks = [str(t) for t in (data.get("tokens") or []) if t]
                    if toks:
                        await _forward_feed_subscription(toks)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("feed_subscribe_listener_error", exc_info=True)
                if ps is not None:
                    try:
                        await ps.close()
                    except Exception:
                        pass
                    ps = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
    finally:
        if ps is not None:
            try:
                await ps.unsubscribe(FEED_SUBSCRIBE_CHANNEL)
                await ps.close()
            except Exception:
                pass


async def get_quote(token: str) -> dict[str, Any]:
    now_ms = int(_t.time() * 1000)
    cached = _quote_cache.get(token)
    if cached and (now_ms - cached[0]) < _QUOTE_CACHE_TTL_MS:
        return cached[1]
    # Cold-path (non-leader) short-circuit: when THIS worker has no live
    # in-process price for the token, serve the leader's `mdlive` snapshot
    # BEFORE `_overlay_all` (which would otherwise issue a ~2 s Kite REST on
    # every cold quote). On a warm worker (leader / single-process) `_state`
    # always carries the price, so this branch never runs and the original
    # path below is taken unchanged.
    st = _state.get(str(token))
    st_cold = True
    if st:
        try:
            st_cold = float(st.get("ltp") or 0) <= 0
        except Exception:
            st_cold = True
    if st_cold:
        live = await _read_mdlive(token)
        if live is not None:
            out = dict(live)
            out["ts"] = now_ms
            _quote_cache[token] = (now_ms, out)
            return out
        # Cold, and the leader has no snapshot either: nothing has ever asked
        # the feed for this token. Ask now — subscribe() seeds `_state` and,
        # on a non-leader, forwards to the leader over `feed:subscribe` — so
        # the next poll has a real price. Without this a symbol-keyed
        # instrument (crypto / forex / metals) read 0.00 from REST forever
        # unless a WebSocket happened to subscribe it first, which is why
        # opening the crypto list showed no price until a tick arrived.
        # Numeric (Zerodha) tokens are left alone: they have a REST snapshot
        # fallback below and their own on-demand subscribe path.
        if not str(token).lstrip("-").isdigit():
            try:
                subscribe([token])
            except Exception:  # pragma: no cover - never break a quote
                logger.debug("quote_cold_subscribe_failed", exc_info=True)
    q = await _ensure_quote(token)
    out = await _overlay_all(token, q)
    await _persist_last_quote(token, out)
    out = await _attach_last_quote(token, out)
    _quote_cache[token] = (now_ms, out)
    return out


async def get_ltp(token: str) -> Decimal:
    q = await get_quote(token)
    return quantize_money(to_decimal(q["ltp"]))


async def get_display_ltp(token: str) -> Decimal:
    """LTP for a BLOTTER — the live price, else the last one we saw.

    `get_ltp` is deliberately strict: no live price means 0, and every
    execution path leans on that (a fill must never use a stale quote).
    A blotter is not an execution path, and 0 there is read as a fact —
    it zeroes the M2M and the net P&L of a perfectly good open position.

    Measured 2026-09-25, MCX open: far-month contracts (SILVER26DECFUT,
    CRUDEOIL26OCTFUT) trade a few times an hour, while the live snapshot
    every non-leader worker reads lives for 30 s. Between two trades the
    snapshot expires, so the admin Positions page swung between a real
    M2M and zero every few seconds — while `mdlast` held the right price
    the whole time.

    Never use this to price an order.
    """
    q = await get_quote(token)
    live = to_decimal(q.get("ltp") or 0)
    if live > 0:
        return quantize_money(live)
    return quantize_money(to_decimal(q.get("last_ltp") or 0))


def get_ltp_instant(token: str) -> Decimal | None:
    """Read LTP from the in-memory WS state with ZERO network calls.

    Used by the risk enforcer's per-tick LTP batch so that the 700 ms
    ``_quote_cache`` expiry no longer triggers concurrent Zerodha REST
    calls every 3rd tick (which caused 125-460 ms ltp_ms spikes).

    Returns ``None`` when the token has no WS price yet — the risk
    enforcer's existing zero-LTP guard skips those positions safely.
    """
    q = _state.get(str(token))
    if q:
        try:
            v = to_decimal(q.get("ltp") or 0)
            if v > 0:
                return v
        except Exception:
            pass
    # _state-miss fallback → raw Zerodha WS tick cache. `_state` is only
    # seeded for tokens someone called get_quote() on (watchlist / order
    # panel / positions view). An open-position token nobody is actively
    # viewing never enters `_state`, so the risk enforcer got None and
    # SKIPPED its SL/TP/stop-out EVERY tick (raw_ltp=null in
    # risk_ltp_fetch_failed — observed for dozens of FUT/CE/equity legs,
    # 2026-06-24). The tick handler keeps `ticks_by_token` fresh for every
    # subscribed token on the feed-leader worker, where the enforcer
    # co-locates (see main.py), so this restores a live price with zero
    # network. Empty on a cold/non-leader worker → None (no worse than before).
    try:
        from app.services.zerodha_service import zerodha as _zs
        live = _zs.ticks_by_token.get(int(token))
        if live:
            v = to_decimal(live.get("ltp") or 0)
            return v if v > 0 else None
    except Exception:
        pass
    return None


def get_quote_instant(token: str) -> dict[str, Any]:
    """Return a shallow copy of the in-memory WS state for *token*.

    Used by the risk enforcer to pass a pre-fetched quote dict directly to
    ``refresh_unrealized_pnl``, bypassing ``get_quote`` / ``_overlay_all`` /
    ``_quote_cache`` entirely. This eliminates the REST fallback that caused
    sweep_ms to cascade when the 700 ms _quote_cache TTL expired mid-sweep
    (position checked at t=0 got the cached quote; position checked at t=750 ms
    saw an expired cache and triggered a Zerodha REST, extending the sweep past
    the next TTL boundary and cascading further).

    Returns an empty dict when the token has no WS state yet — callers must
    fall back gracefully (use LTP as-is) when bid/ask are absent.
    """
    q = _state.get(str(token))
    if q:
        return dict(q)
    # Same _state-miss fallback as get_ltp_instant: read the live WS tick
    # cache so the risk enforcer's close-side (bid/ask) mark works for
    # open-position tokens that were never get_quote()'d into `_state`.
    try:
        from app.services.zerodha_service import zerodha as _zs
        live = _zs.ticks_by_token.get(int(token))
        if live:
            return dict(live)
    except Exception:
        pass
    return {}


async def get_quotes(tokens: list[str]) -> list[dict[str, Any]]:
    # Previously this looped serially — each token's `_overlay_all` blocked
    # on Zerodha + Infoway timeouts (~2 s each) BEFORE moving to the next
    # token. With 5 instruments on the OrderPanel that was a ~10 s worst-
    # case for a single batch request. asyncio.gather fans them out in
    # parallel so the total wait drops to the slowest single overlay.
    # Delegate to get_quote() rather than repeating its body: this path was
    # missing the cold-path `mdlive` short-circuit, so on a multi-worker
    # deploy a NON-leader worker served 0.0 for every symbol-keyed feed
    # (crypto / forex / metals) — those have no Zerodha REST to fall back
    # on, so `_overlay_all` returned an empty quote. Single quotes were fine
    # because get_quote() reads the leader's snapshot; the batch endpoint
    # behind Market Watch and the order panel did not, so roughly half the
    # requests showed a frozen 0.00. get_quote() is a strict superset of the
    # old body (cache + mdlive, then the same ensure/overlay/persist/attach).
    async def _one(t: str) -> dict[str, Any]:
        return await get_quote(t)

    return list(await asyncio.gather(*[_one(t) for t in tokens]))


def subscribe(tokens: list[str]) -> None:
    # Feed-split: keep only tokens for THIS process's group so the global feed
    # never seeds/forwards a Zerodha token (and vice-versa). No-op when "all".
    tokens = _group_filter(tokens)
    _subscribed.update(tokens)

    # Seed an empty `_state` slot for every subscribed token so the leader's
    # `tick_loop` starts overlaying it immediately. Zerodha (numeric) tokens are
    # seeded by their WS ticks, but MetaAPI / Infoway symbol-style tokens have
    # NO upstream WS that writes `_state` — without this seed they sit in
    # `_subscribed` yet never enter the `tick_loop` publish set (which is
    # `_state ∩ _subscribed`), so they never reach `mdlive` / the WS fanout and
    # show a frozen 0.0000 on the frontend. `_overlay_all` fills the real price
    # (MetaAPI `get_tick`) on the next tick; the slot is never a fabricated price.
    for t in tokens:
        if t and t not in _state:
            _state[t] = _empty_quote(t)

    # Multi-worker cross-worker propagation: when THIS worker doesn't own the
    # upstream feed (non-leader), announce the tokens on `feed:subscribe` so
    # the leader's `feed_subscribe_listener` subscribes them on the real
    # Zerodha/Infoway feeds. Gated on `not _is_feed_leader` so a single worker
    # (always its own leader) never publishes → behaviour identical to before.
    if not _is_feed_leader and tokens:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                publish(FEED_SUBSCRIBE_CHANNEL, {"tokens": [str(t) for t in tokens]}),
                name="feed_subscribe_publish",
            )
        except RuntimeError:
            pass
        except Exception:  # pragma: no cover
            logger.debug("feed_subscribe_publish_failed", exc_info=True)

    # Propagate Infoway-class tokens (forex / crypto / metals / energy) to
    # the Infoway WS ON DEMAND. Without this, only the symbols listed in
    # INFOWAY_DEFAULT_* (subscribed once at startup) ever received ticks —
    # opening ANY other forex chart showed a perpetual "loading" because
    # Infoway was never told to stream that symbol, and (unlike Zerodha)
    # Infoway has no REST snapshot fallback to bridge the gap.
    #
    # Infoway instrument tokens ARE the symbol string (token == symbol, see
    # infoway_service.mirror_subscribed_to_instruments). Zerodha tokens are
    # always pure integers and are handled by the Kite on-demand path
    # elsewhere — so we forward only the non-numeric (symbol-style) tokens.
    infoway_codes = [
        t for t in tokens if t and not str(t).lstrip("-").isdigit()
    ]
    if infoway_codes:
        try:
            from app.services.infoway_service import infoway

            loop = asyncio.get_running_loop()
            loop.create_task(
                infoway.subscribe(infoway_codes),
                name="infoway_on_demand_subscribe",
            )
        except RuntimeError:
            # No running event loop (called from a sync context) — skip;
            # the WS subscribe path always runs inside the loop so this
            # only guards rare non-async callers.
            pass
        except Exception:
            logger.debug("infoway_on_demand_subscribe_failed", exc_info=True)

        # MetaAPI is the PRIMARY feed for forex/metals/indices/commodities. On
        # the feed leader, ensure the symbol is streamed by the MT terminal so
        # `get_tick` returns a live price (no-ops on non-leaders where MetaAPI
        # isn't connected — those forward via `feed:subscribe` above).
        try:
            from app.services.metaapi_service import metaapi

            if metaapi.is_enabled():
                loop = asyncio.get_running_loop()
                loop.create_task(
                    metaapi.subscribe(infoway_codes),
                    name="metaapi_on_demand_subscribe",
                )
        except RuntimeError:
            pass
        except Exception:
            logger.debug("metaapi_on_demand_subscribe_failed", exc_info=True)


def unsubscribe(tokens: list[str]) -> None:
    for t in tokens:
        _subscribed.discard(t)

    # NOTE: we deliberately do NOT propagate unsubscribe to Infoway. There
    # is no cross-connection ref-count here, so if user A closes a EURUSD
    # chart while user B still has it open, unsubscribing from Infoway would
    # kill B's live ticks too. An idle Infoway subscription is cheap (one
    # extra symbol in the feed), so we leave it streaming. A periodic
    # reaper can prune genuinely-unwatched symbols later if needed.


async def ensure_open_position_subscriptions() -> dict[str, int]:
    """Guarantee every OPEN-position token is on the live feed AND in
    ``_subscribed`` — the gate ``tick_loop`` uses to mirror quotes into
    ``mdlive:{token}``.

    Why this exists: the boot/reconnect warm in ``zerodha_service`` subscribed
    held tokens on the Kite WS (so ``_state`` warms) but never added them to
    ``_subscribed``, so ``tick_loop`` skipped mirroring them into ``mdlive``.
    The sharded risk loop reads ``mdlive`` — so any held token nobody was
    independently watching (no watchlist entry / open chart) had NO live price
    and the risk enforcer silently SKIPPED SL/TP/stop-out for it every tick
    (the ``risk_ltp_fetch_failed`` flood). This reconciles BOTH registries.

    Idempotent: ``subscribe_tokens_on_demand`` skips already-subscribed Kite
    tokens and ``_subscribed`` is a set. Numeric tokens → Kite WS (warms
    ``_state``); symbol-style synthetic (Infoway forex/crypto) tokens are
    forwarded to the Infoway WS by ``subscribe`` below.
    """
    from app.models.position import Position, PositionStatus

    open_positions = await Position.find(
        Position.status == PositionStatus.OPEN
    ).to_list()

    all_tokens: list[str] = []
    numeric: list[int] = []
    sym_map: dict[int, dict[str, str]] = {}
    for p in open_positions:
        tok_raw = getattr(getattr(p, "instrument", None), "token", None)
        if tok_raw is None:
            continue
        all_tokens.append(str(tok_raw))
        try:
            tok = int(tok_raw)
        except (TypeError, ValueError):
            continue  # synthetic Infoway token — handled via subscribe() below
        ex = getattr(p.instrument, "exchange", None)
        ex_str = ex.value if hasattr(ex, "value") else str(ex or "NSE")
        numeric.append(tok)
        sym_map[tok] = {
            "symbol": getattr(p.instrument, "symbol", "") or str(tok),
            "exchange": ex_str,
        }

    if not all_tokens:
        return {"positions": 0, "kite": 0, "newly_subscribed": 0}

    # 1) Kite WS — warm `_state` for numeric tokens (no-op if already subbed).
    #    Feed-split: skip on the global process — it doesn't run Zerodha.
    if numeric and _feed_group in ("all", "main"):
        try:
            from app.services.zerodha_service import zerodha

            await zerodha.subscribe_tokens_on_demand(numeric, sym_map)
        except Exception:  # noqa: BLE001
            logger.exception("ensure_open_position_kite_subscribe_failed")

    # 2) `_subscribed` set — so `tick_loop` mirrors them into `mdlive`. Also
    #    forwards synthetic (Infoway) tokens to the Infoway WS on demand.
    before = len(_subscribed)
    subscribe(all_tokens)
    added = len(_subscribed) - before

    logger.info(
        "open_position_subscriptions_reconciled",
        extra={
            "positions": len(open_positions),
            "kite_tokens": len(numeric),
            "newly_added_to_subscribed": added,
            "total_subscribed": len(_subscribed),
        },
    )
    return {
        "positions": len(open_positions),
        "kite": len(numeric),
        "newly_subscribed": added,
    }


# Segments quoted by MetaAPI / Infoway rather than Zerodha. Their instrument
# tokens are symbol-style ("EURUSD", "CRYPTO_BTCUSD"), not numeric.
_INTERNATIONAL_SEGMENTS = ("FOREX", "COMMODITIES", "INDICES", "STOCKS", "CRYPTO_SPOT")


async def ensure_international_subscriptions() -> int:
    """Keep every symbol-style instrument in the publish set. Returns how
    many were newly added.

    `_subscribed` lives in memory, so a restart empties it — and a
    symbol-style token only re-enters it when some client happens to ask
    for that instrument. The MetaAPI / Infoway feeds stream these symbols
    upstream from boot regardless, so the price sits in the process and is
    simply never mirrored into `mdlive`: the terminal then shows a frozen
    quote for the whole FOREX / INDICES / STOCKS list, and every restart
    brings it back. Measured on production: 23 of 23 such instruments had
    no `mdlive` key at all, and one `feed:subscribe` lit 22 of them up
    within seconds (USDINR isn't offered by the broker).

    Already-subscribed tokens are filtered out before forwarding, so the
    periodic pass costs one Mongo read and sends nothing upstream —
    re-subscribing 40 symbols at MetaAPI every two minutes is exactly the
    traffic their per-account rate limit punishes.
    """
    from app.models.instrument import Instrument

    rows = await Instrument.find(
        {"is_active": True, "segment": {"$in": list(_INTERNATIONAL_SEGMENTS)}}
    ).to_list()
    tokens = [
        str(i.token)
        for i in rows
        if i.token
        and not str(i.token).lstrip("-").isdigit()
        and str(i.token) not in _subscribed
    ]
    if tokens:
        await _forward_feed_subscription(tokens)
        logger.info("international_subscriptions_added count=%d", len(tokens))
    return len(tokens)


async def open_position_subscription_loop(interval_sec: float = 120.0) -> None:
    """Leader-only: periodically reconcile OPEN-position subscriptions so a
    held token can never silently fall off the live feed mid-session — which
    would make the risk enforcer skip its SL/TP/stop-out. A 2-min cadence
    self-heals within a couple of risk ticks; the call is cheap + idempotent.
    First pass runs immediately on start."""
    import asyncio as _asyncio

    logger.info("open_position_subscription_loop_started interval_sec=%s", interval_sec)
    while True:
        try:
            await ensure_open_position_subscriptions()
        except Exception:  # noqa: BLE001
            logger.exception("open_position_subscription_loop_iter_failed")
        # Same idea, other feed: forex / indices / international stocks are
        # streamed upstream from boot but only reach `mdlive` once something
        # subscribes them.
        try:
            await ensure_international_subscriptions()
        except Exception:  # noqa: BLE001
            logger.exception("international_subscription_loop_iter_failed")
        await _asyncio.sleep(interval_sec)


# ── Background tick loop ────────────────────────────────────────────
async def tick_loop(interval_sec: float = 1.0) -> None:
    """Fan out subscribed instrument ticks to Redis pub/sub.

    No price generation here — the loop only mirrors whatever the real
    overlays (Zerodha WS / Infoway WS / Kite REST snapshot) have already
    written into `_state`. Tokens with LTP = 0 are skipped so we don't
    spam consumers with zero-priced ticks for instruments that have no
    real feed yet.
    """
    global _running
    if _running:
        return
    _running = True
    logger.info("market_tick_loop_started")
    await _warm_token_symbol_cache()
    try:
        import time

        while _running:
            try:
                now_ms = int(time.time() * 1000)
                # Collect subscribed (token, base) pairs first so we can
                # fan the overlays out in parallel. The old code awaited
                # `_overlay_all` per-token in a serial for-loop — with
                # N subscribed instruments each taking the worst-case
                # Zerodha REST overlay (~200-2000 ms), one iteration
                # could stretch into multiple seconds and starve the
                # 250 ms tick-loop cadence.
                pending = [
                    (token, q)
                    for token, q in list(_state.items())
                    if token in _subscribed
                ]
                if pending:
                    # WS-only overlays in the pump: allow_rest=False so a cold
                    # token never fires a 2 s Kite REST here (that flooded
                    # zerodha_overlay_timeout + starved the co-located risk
                    # shard). Live prices come from the Zerodha/Infoway WS maps;
                    # on-demand get_quote() still RESTs a single token for the
                    # order panel. The risk enforcer already WS-subscribes any
                    # zero-LTP open-position token, so cold tokens self-heal to
                    # a live tick within a few seconds.
                    results = await asyncio.gather(
                        *(
                            _overlay_all(token, base, allow_rest=False)
                            for token, base in pending
                        ),
                        return_exceptions=True,
                    )
                    # Multi-worker: mirror each live quote to Redis so the
                    # non-leader workers (cold `_state`) can serve quotes +
                    # fills from `mdlive:{token}` without running their own feed.
                    mdlive_items: list[tuple[str, dict[str, Any]]] = []
                    # Collect the per-token tick fanout and PUBLISH it all in ONE
                    # pipeline round-trip (publish_batch) instead of N serial
                    # awaits — N serial publishes held the pool + contended the
                    # co-located risk shard's mdlive MGET, spiking tick_overruns.
                    publish_items: list[tuple[str, dict[str, Any]]] = []
                    for (token, base), overlaid in zip(pending, results):
                        if isinstance(overlaid, Exception):
                            q = base
                        else:
                            q = overlaid
                            _state[token] = q
                        q["ts"] = now_ms
                        # Skip tokens that still have no real feed — don't
                        # broadcast zero-priced ticks.
                        if float(q.get("ltp") or 0) <= 0:
                            continue
                        # mdlive is refreshed EVERY tick so the risk enforcer +
                        # execution path always see a fresh price (30 s TTL).
                        mdlive_items.append((token, q))
                        # But only PUBLISH to the WS fanout when the price moved
                        # — skip the (common) unchanged illiquid-strike ticks so
                        # the single-threaded leader stops burning CPU + Redis on
                        # identical re-broadcasts.
                        _sig = (q.get("ltp"), q.get("bid"), q.get("ask"))
                        if _last_pub.get(token) == _sig:
                            continue
                        _last_pub[token] = _sig
                        publish_items.append((
                            f"market:tick:{token}",
                            {
                                "token": token,
                                "ltp": q["ltp"],
                                "change": q["change"],
                                "change_pct": q["change_pct"],
                                "volume": q["volume"],
                                "bid": q["bid"],
                                "ask": q["ask"],
                                "ts": q["ts"],
                            },
                        ))
                    await publish_batch(publish_items)
                    await _write_mdlive_batch(mdlive_items)
                    # Fold the SAME leader-only live quotes into per-minute
                    # bid/ask high-low buckets for the admin "Rate History"
                    # feature. Pure in-memory + synchronous, so it can't slow
                    # the tick; the leader-gated flush loop persists completed
                    # minutes. Best-effort — a bad tick must never break fanout.
                    try:
                        from app.services import tick_aggregator

                        tick_aggregator.record(mdlive_items, now_ms)
                    except Exception:  # pragma: no cover
                        logger.debug("tick_aggregator_record_failed", exc_info=True)
                await asyncio.sleep(interval_sec)
            except Exception as e:  # pragma: no cover
                logger.exception("market_tick_loop_iter_failed", extra={"error": str(e)})
                await asyncio.sleep(2.0)
    finally:
        _running = False
        logger.info("market_tick_loop_stopped")


def stop_tick_loop() -> None:
    global _running
    _running = False
