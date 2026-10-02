"""Infoway (infoway.io) WebSocket integration.

Infoway covers global markets that Kite Connect does not — forex pairs
(EURUSD, USDJPY…), crypto pairs (BTCUSDT, ETHUSDT…), spot metals
(XAUUSD, XAGUSD…) and energy (USOIL, UKOIL, NATGAS).

Protocol (code-based JSON over WS — different from AllTick):
    Subscribe          → {code: 10003, trace, data: {codes: "BTC,ETH,..."}}
    Unsubscribe        → {code: 10004, trace, data: {codes: "BTC,..."}}
    Server depth push  ← {code: 10005, data: {s, t,
                            a: [[prices], [volumes]],   # asks
                            b: [[prices], [volumes]]}}  # bids

Endpoints — Infoway splits markets onto separate WS channels:
    Crypto                  wss://data.infoway.io/ws?business=crypto&apikey=...
    Forex / metals / energy wss://data.infoway.io/ws?business=common&apikey=...

We hold both connections in parallel and fan-out subscriptions to whichever
channel matches the symbol's class. The public surface (`infoway.ticks`,
`infoway.subscribe`, `infoway.status`, …) is identical to the AllTick
service this replaces so callers don't change.

Every numeric field exposed downstream is `float`. Decimal coercion only
happens at the order / wallet boundary; the live tick path stays float so
it is cheap and JSON-serialisable for Redis fanout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from app.core.config import settings
from app.core.redis_client import publish

logger = logging.getLogger(__name__)

# Bad-tick spike filter (see _handle price ingestion): reject a tick whose price
# jumps more than this fraction from the last-good tick unless that tick is
# older than _SPIKE_STALE_MS. 50% tick-to-tick is impossible for a real
# instrument — the garbage ticks that phantom-closed positions were 68%+ jumps.
# 30 s (not 5): the crypto/forex feed reconnects ~every 65 s and illiquid pairs
# tick sparsely, so a 5 s window let a garbage tick right after a gap through as
# "a real move" (XRPUSDT filled a BUY at $26 vs its real $1.14, CL29840025).
# 30 s keeps the last-good price trusted across the reconnect gap.
_MAX_TICK_SPIKE_PCT = 0.5
_SPIKE_STALE_MS = 30000.0

WS_BASE = "wss://data.infoway.io/ws"

# Protocol codes
CMD_SUBSCRIBE = 10003
CMD_UNSUBSCRIBE = 10004
CMD_DEPTH_PUSH = 10005
CMD_HEARTBEAT = 22000

# Server drops the socket if it doesn't receive any frame from us for ~60s,
# and protocol-level WS pings don't count — only the app-level code 22000
# heartbeat keeps the channel alive. Send well inside the window.
#
# 15 s (not 25 s) on purpose: the single backend event loop occasionally
# stalls for several seconds under a heavy risk-enforcer sweep, which DELAYS
# this heartbeat. At 25 s a couple of stalls could push the actual gap past
# the server's 60 s cutoff → drop → the ~60 s reconnect churn we saw. 15 s
# leaves room for ~3 missed beats before the cutoff, so the link stays up.
HEARTBEAT_INTERVAL_SEC = 15

# Stale-data watchdog. A WS can wedge "half-open": the TCP socket stays up
# (so `async for raw in ws` never raises and the existing reconnect never
# fires) but the server has silently stopped pushing frames — the watchlist
# then freezes at 0.00 forever (operator/user report: "price bar-bar ruk
# jata hai"). If we go this long without ANY inbound frame (a tick OR any
# server ack — `_dispatch` sees every frame), force-close the socket so the
# receive loop unwinds and `_run_loop` reconnects + re-subscribes. 45 s = 3
# heartbeat cycles, long enough not to trip on a brief gap, short enough that
# a real stall self-heals in well under a minute instead of never.
STALE_RX_TIMEOUT_SEC = 45

# Reconnect backoff caps. A normal drop (network blip, half-open heal) retries
# fast — exponential 1→2→… capped at 30 s.
RECONNECT_BACKOFF_CAP_SEC = 30
# Infoway answers HTTP 429 when too many connections exist for the API key.
# In practice this happens right after a backend restart: the PREVIOUS
# process's sockets are still lingering server-side and occupying the slot,
# so the new process is rejected. Backing off only 30 s keeps us rejected AND
# keeps the slot contended, so on a 429 we wait much longer (up to ~2 min) to
# let the stale session expire server-side before retrying.
RATE_LIMIT_BACKOFF_CAP_SEC = 120

# Only treat a connection as "recovered" (and reset the backoff to a fast
# 1 s retry) once it has stayed up at least this long. A socket that drops
# within a few seconds — the server closing us with code 1000, a half-open
# heal, or a 429 that briefly slipped through — must KEEP ramping the backoff
# instead of resetting to 1 s. Without this, every quick drop reset us to a
# 1 s retry and we hammered Infoway straight back into an HTTP 429 storm, the
# reconnect churn behind the recurring "stale feed events" spikes. 30 s cleanly
# separates that churn (drops in <5 s) from a healthy connection that has done
# real work (the feed's normal server-side recycle is ~65 s).
STABLE_CONNECTION_SEC = 30

# Market channels — Infoway requires a separate WS per business class.
# Per the Infoway docs at /websocket-api/endpoints.md the supported business
# values are: stock (US + HK + A-shares), japan, india, crypto, common
# (forex / futures / metals / energy / indices). We only wire the three
# this platform actually surfaces today: crypto, stock, and common.
CHANNEL_CRYPTO = "crypto"
CHANNEL_STOCK = "stock"  # US / HK / A-share equities
CHANNEL_COMMON = "common"  # forex / metals / energy / indices / futures


# Inbound symbol aliases: Infoway feeds a few indices under a different code
# than our canonical platform name. Remap the incoming tick `s` so it lands on
# the symbol the watchlist/instruments use. (Same names the MetaAPI broker uses:
# S&P 500 = US500, DAX = GER40.) Subscribe the Infoway names via
# INFOWAY_DEFAULT_INDICES; they get stored/displayed under the mapped name.
_INBOUND_ALIAS: dict[str, str] = {"US500": "SPX500", "GER40": "DE40"}
# Outbound (subscribe wire) alias — the inverse. We keep SPX500 / DE40 as the
# canonical instrument + watchlist names, but ASK Infoway using its own codes
# (US500 / GER40). Ticks then come back under those and _INBOUND_ALIAS maps them
# home. Keeps a single canonical instrument per index (no stray US500 / GER40).
_OUTBOUND_ALIAS: dict[str, str] = {v: k for k, v in _INBOUND_ALIAS.items()}


# Crypto bases we list as USD pairs (BTCUSD, ETHUSD…). Used both to map them
# onto Infoway's USDT feed symbols and to classify them as crypto (NOT forex)
# — a plain `BTCUSD` code must never fall through to the FOREX bucket.
_CRYPTO_BASES = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "MATIC", "LINK", "AVAX", "LTC", "DOT", "TRX", "SHIB"}


def _normalise_symbol(code: str) -> str:
    """`BTCUSD` ↔ `BTCUSDT` map (Infoway lists most cryptos as USDT pairs).
    All other symbols (EURUSD, XAUUSD, USOIL…) pass through unchanged."""
    s = (code or "").strip().upper()
    if not s:
        return s
    if s.endswith("USDT"):
        return s
    base = s[:-3] if s.endswith("USD") else s
    if s.endswith("USD") and base in _CRYPTO_BASES:
        return base + "USDT"
    return s


def _channel_for(code: str) -> str:
    """Decide which Infoway business channel owns this symbol.

    Routing (in priority order):
        1. Codes ending in USDT / USDC / BUSD → `crypto` channel.
        2. Codes listed in INFOWAY_DEFAULT_STOCKS → `stock` channel.
           Indices go to `common`, not `stock` (per Infoway docs:
           `common` covers forex/futures/indices, `stock` is equities only).
        3. Everything else → `common`.

    A wrong channel = a silent subscription that never delivers ticks
    (Infoway won't error, the symbol just never appears in the feed),
    so this routing matters more than the segment label.
    """
    c = (code or "").strip().upper()
    if c.endswith("USDT") or c.endswith("USDC") or c.endswith("BUSD"):
        return CHANNEL_CRYPTO
    if c in _stock_codes():
        return CHANNEL_STOCK
    return CHANNEL_COMMON


def _safe_float(v: Any, default: float = 0.0) -> float:
    """Coerce any incoming numeric (str / int / float / None) to a finite
    float without raising. Defends the tick path against malformed feed
    payloads that would otherwise break parsing for a whole frame."""
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return f


class _Channel:
    """One Infoway WS connection (either `crypto` or `common`).

    The parent `InfowayService` owns two of these and routes symbols by
    `_channel_for(code)`. Each channel handles its own connect / reconnect /
    re-subscribe loop independently — a hiccup on the crypto stream doesn't
    pause forex ticks."""

    def __init__(self, business: str, parent: "InfowayService") -> None:
        self.business = business
        self.parent = parent
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._task: asyncio.Task | None = None
        self._subscribed: set[str] = set()
        self._connected: bool = False
        self._stop_requested: bool = False
        # Monotonic ts of the last inbound frame — drives the stale-data
        # watchdog in `_heartbeat_loop`. 0.0 until the first connect.
        self._last_rx: float = 0.0

    @property
    def is_connected(self) -> bool:
        if not self._connected or self._ws is None:
            return False
        ws = self._ws
        # websockets ≥ 14 uses ClientConnection.open; older versions used .closed
        if hasattr(ws, "open"):
            return bool(ws.open)
        return not bool(getattr(ws, "closed", True))  # type: ignore[union-attr]

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_requested = False
        self._task = asyncio.create_task(self._run_loop(), name=f"infoway_ws_{self.business}")

    async def stop(self) -> None:
        self._stop_requested = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        self._connected = False

    async def _run_loop(self) -> None:
        backoff = 1
        rate_limited = False
        while not self._stop_requested:
            connect_started = time.monotonic()
            try:
                await self._connect_once()
                # Clean return: server closed the socket (or stop requested).
                # Not a rate-limit — clear the flag so the backoff cap below
                # uses the fast (30 s) cap, not the 429 (120 s) one.
                rate_limited = False
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.parent._last_error = str(e)[:300]
                # "server rejected WebSocket connection: HTTP 429" — detect via
                # the message so we stay robust across websockets versions
                # (InvalidStatus vs InvalidStatusCode have different attrs).
                rate_limited = "429" in str(e)
                logger.warning(
                    "infoway_reconnect [%s]%s: %s",
                    self.business,
                    " rate-limited" if rate_limited else "",
                    str(e)[:200],
                )
            self._connected = False
            if self._stop_requested:
                break
            # Reset the backoff to a fast 1 s retry ONLY if the connection just
            # closed after doing real work (stayed up ≥ STABLE_CONNECTION_SEC).
            # A drop within seconds — a 429 handshake reject, the server closing
            # us with code 1000, or a half-open heal — must KEEP ramping the
            # backoff so we don't hammer Infoway straight back into a 429 storm.
            # This is the fix for the recurring "stale feed events" churn: the
            # old code reset to 1 s on EVERY clean return, so a briefly-successful
            # connect wiped the 429 backoff and the fast-retry storm restarted.
            uptime = time.monotonic() - connect_started
            if uptime >= STABLE_CONNECTION_SEC and not rate_limited:
                backoff = 1
            cap = RATE_LIMIT_BACKOFF_CAP_SEC if rate_limited else RECONNECT_BACKOFF_CAP_SEC
            ceiling = min(backoff, cap)
            # Full jitter (AWS-style): sleep a random duration in
            # [ceiling/2, ceiling]. De-syncs the crypto/common/stock channels
            # so they never retry on the same tick and re-trigger Infoway's
            # concurrent-connection burst limit.
            wait = ceiling / 2 + random.uniform(0, ceiling / 2)
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, cap)

    async def _connect_once(self) -> None:
        # The `common` (forex/metals) channel uses its own key when configured,
        # so it gets a dedicated connection slot instead of contending with
        # crypto on one key (each Infoway key allows only ~1-2 concurrent WS).
        token = settings.INFOWAY_API_KEY.get_secret_value()
        if self.business == CHANNEL_COMMON:
            common_key = settings.INFOWAY_API_KEY_COMMON.get_secret_value()
            if common_key:
                token = common_key
        url = f"{WS_BASE}?business={self.business}&apikey={token}"
        async with websockets.connect(
            url,
            ping_interval=None,  # Infoway ignores protocol PING frames; app-level
            ping_timeout=None,   # heartbeat (code 22000 every 25 s) keeps link alive
            close_timeout=5,
            max_size=2**20,
        ) as ws:
            self._ws = ws
            self._connected = True
            self.parent._last_error = None
            # Seed the watchdog clock at connect so a slow first tick (or a
            # market that's quiet right after we attach) doesn't instantly
            # look "stale".
            self._last_rx = time.monotonic()
            logger.info("infoway_ws_connected", extra={"channel": self.business})

            # Re-subscribe to anything we had before disconnect
            if self._subscribed:
                await self._send_subscribe(list(self._subscribed))

            hb_task = asyncio.create_task(
                self._heartbeat_loop(ws), name=f"infoway_hb_{self.business}"
            )
            try:
                async for raw in ws:
                    # ANY frame — tick or server ack — means the link is live.
                    self._last_rx = time.monotonic()
                    if self._stop_requested:
                        break
                    try:
                        msg = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
                    except Exception:
                        continue
                    self.parent._dispatch(msg)
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        # Send-first, then sleep: pushes the first keep-alive out immediately
        # after connect instead of 15 s later, so a slow first sweep can't
        # starve the very first beat.
        while True:
            try:
                await ws.send(json.dumps({"code": CMD_HEARTBEAT, "trace": uuid.uuid4().hex}))
            except Exception:
                return
            # Stale-data watchdog: socket is up but the server stopped pushing
            # frames for too long → it's wedged half-open. Close it so the
            # receive loop's `async for` raises ConnectionClosed and
            # `_run_loop` reconnects + re-subscribes (≈1 s, backoff was reset
            # on the last good connect). This is what unfreezes a 0.00
            # watchlist instead of waiting on a drop that never comes.
            gap = time.monotonic() - self._last_rx
            if gap > STALE_RX_TIMEOUT_SEC:
                logger.warning(
                    "infoway_stale_rx_forcing_reconnect [%s] no frame for %.0fs",
                    self.business,
                    gap,
                )
                try:
                    await ws.close()
                except Exception:
                    pass
                return
            await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)

    async def subscribe(self, codes: list[str]) -> int:
        new = [c for c in codes if c and c not in self._subscribed]
        if not new:
            return 0
        for c in new:
            self._subscribed.add(c)
        if self.is_connected:
            await self._send_subscribe(new)
        return len(new)

    async def unsubscribe(self, codes: list[str]) -> int:
        gone = [c for c in codes if c in self._subscribed]
        for c in gone:
            self._subscribed.discard(c)
        if self.is_connected and gone:
            await self._send_unsubscribe(gone)
        return len(gone)

    async def _send_subscribe(self, codes: list[str]) -> None:
        if self._ws is None:
            return
        wire = [_OUTBOUND_ALIAS.get(c, c) for c in codes]
        payload = {
            "code": CMD_SUBSCRIBE,
            "trace": uuid.uuid4().hex,
            "data": {"codes": ",".join(wire), "includeTy": False},
        }
        try:
            await self._ws.send(json.dumps(payload))
            logger.info(
                "infoway_subscribed",
                extra={"channel": self.business, "count": len(codes)},
            )
        except (ConnectionClosed, Exception) as e:
            logger.warning(
                "infoway_subscribe_failed [%s]: %s",
                self.business,
                str(e)[:200],
            )

    async def _send_unsubscribe(self, codes: list[str]) -> None:
        if self._ws is None:
            return
        wire = [_OUTBOUND_ALIAS.get(c, c) for c in codes]
        payload = {
            "code": CMD_UNSUBSCRIBE,
            "trace": uuid.uuid4().hex,
            "data": {"codes": ",".join(wire)},
        }
        try:
            await self._ws.send(json.dumps(payload))
        except Exception:
            pass


class InfowayService:
    """Public surface kept compatible with the old AllTickService so
    `get_tick / get_all_ticks / subscribe / status / depth` consumers
    don't change."""

    def __init__(self) -> None:
        self.ticks: dict[str, dict[str, Any]] = {}
        self.depth: dict[str, dict[str, Any]] = {}
        self._channels: dict[str, _Channel] = {
            CHANNEL_CRYPTO: _Channel(CHANNEL_CRYPTO, self),
            CHANNEL_COMMON: _Channel(CHANNEL_COMMON, self),
            # International equities (US / HK / A-shares) — see `start()`: this
            # channel is only actually CONNECTED when INFOWAY_DEFAULT_STOCKS is
            # populated. Registered here so subscribe()'s bucket seeding always
            # has the key, but left dormant otherwise so it doesn't burn one of
            # the key's few concurrent WS slots and starve `common` (gold /
            # forex) into an HTTP 429 lockout.
            CHANNEL_STOCK: _Channel(CHANNEL_STOCK, self),
        }
        self._last_error: str | None = None
        self._main_loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_configured(self) -> bool:
        return bool(settings.INFOWAY_API_KEY.get_secret_value())

    @property
    def is_connected(self) -> bool:
        return any(c.is_connected for c in self._channels.values())

    def status(self) -> dict[str, Any]:
        subs: set[str] = set()
        for ch in self._channels.values():
            subs |= ch._subscribed
        return {
            "configured": self.is_configured,
            "connected": self.is_connected,
            "channels": {
                name: {"connected": ch.is_connected, "subscribed": sorted(ch._subscribed)}
                for name, ch in self._channels.items()
            },
            "subscribed_count": len(subs),
            "subscribed": sorted(subs),
            "lastError": self._last_error,
            "tickCount": len(self.ticks),
        }

    def get_tick(self, symbol: str) -> dict[str, Any] | None:
        return self.ticks.get(_normalise_symbol(symbol))

    def get_all_ticks(self) -> dict[str, dict[str, Any]]:
        return dict(self.ticks)

    # ── Lifecycle ────────────────────────────────────────────────────
    async def start(self) -> None:
        if not self.is_configured:
            logger.info("infoway_skipped: INFOWAY_API_KEY not set")
            return
        self._main_loop = asyncio.get_running_loop()
        # Stagger channel startups by 3 s each so we don't fire 3 simultaneous
        # WS handshakes at the same API key — Infoway rate-limits concurrent
        # connection bursts with HTTP 429.
        #
        # Skip the `stock` channel entirely when no US equities are configured:
        # each key allows only ~2 concurrent WS connections, and an idle stock
        # channel still opens + reconnects, permanently starving `common`
        # (gold / forex / metals) into an HTTP 429 lockout. `_channel_for` never
        # routes a symbol to `stock` while `_stock_codes()` is empty, so the
        # dormant channel receives no subscriptions.
        stock_on = bool(_stock_codes())
        # Skip the `crypto` channel when Binance already supplies crypto
        # (BINANCE_CRYPTO_FEED=true). Each Infoway key allows only ~2 concurrent
        # WS, so a redundant crypto channel permanently starves `common` +
        # `stock` (429 thrashing). Binance is the crypto source in that case, so
        # dropping Infoway's crypto slot lets common (metals/forex/indices) and
        # stock both stay up on one key.
        crypto_on = not bool(getattr(settings, "BINANCE_CRYPTO_FEED", False))
        for name, ch in self._channels.items():
            if name == CHANNEL_STOCK and not stock_on:
                logger.info("infoway_stock_channel_skipped: INFOWAY_DEFAULT_STOCKS empty")
                continue
            if name == CHANNEL_CRYPTO and not crypto_on:
                logger.info("infoway_crypto_channel_skipped: BINANCE_CRYPTO_FEED serves crypto")
                continue
            await ch.start()
            await asyncio.sleep(3)
        logger.info("infoway_started")

    async def stop(self) -> None:
        # Close every channel CONCURRENTLY (not one-by-one) so the whole feed
        # tears down inside the restart grace window. A slow sequential close
        # risked SIGKILL arriving before the last socket shut, leaving a
        # session lingering on Infoway's side → the next process gets 429.
        await asyncio.gather(
            *(ch.stop() for ch in self._channels.values()),
            return_exceptions=True,
        )

    # ── Subscribe / unsubscribe (routes to the right channel) ────────
    async def subscribe(self, codes: list[str]) -> int:
        # Bucket symbols by channel, then forward. Buckets are seeded for
        # every registered channel so a new business class (e.g. `stock`)
        # doesn't trip a KeyError before its first symbol arrives.
        buckets: dict[str, list[str]] = {name: [] for name in self._channels}
        for raw in codes:
            c = _normalise_symbol(raw)
            if not c:
                continue
            buckets[_channel_for(c)].append(c)
        total = 0
        for channel, symbols in buckets.items():
            if symbols:
                total += await self._channels[channel].subscribe(symbols)
        return total

    async def unsubscribe(self, codes: list[str]) -> int:
        buckets: dict[str, list[str]] = {name: [] for name in self._channels}
        for raw in codes:
            c = _normalise_symbol(raw)
            if not c:
                continue
            buckets[_channel_for(c)].append(c)
        total = 0
        for channel, symbols in buckets.items():
            if symbols:
                total += await self._channels[channel].unsubscribe(symbols)
                for s in symbols:
                    self.ticks.pop(s, None)
                    self.depth.pop(s, None)
        return total

    # ── Frame dispatch (shared across channels) ──────────────────────
    def _dispatch(self, msg: dict[str, Any]) -> None:
        code = int(msg.get("code") or 0)
        if code != CMD_DEPTH_PUSH:
            # Login/heartbeat/ack frames — Infoway doesn't require explicit
            # auth past the URL apikey, so we just ignore non-tick frames.
            return

        data = msg.get("data") or {}
        sym = (data.get("s") or "").upper()
        if not sym:
            return
        sym = _INBOUND_ALIAS.get(sym, sym)

        # Infoway depth shape:
        #   a: [[ask_prices...], [ask_volumes...]]
        #   b: [[bid_prices...], [bid_volumes...]]
        # Each top-level array has exactly 2 inner arrays. Top of book is index 0.
        a = data.get("a") or [[], []]
        b = data.get("b") or [[], []]
        ask_prices = a[0] if len(a) > 0 else []
        ask_vols = a[1] if len(a) > 1 else []
        bid_prices = b[0] if len(b) > 0 else []
        bid_vols = b[1] if len(b) > 1 else []

        best_ask = _safe_float(ask_prices[0] if ask_prices else 0.0)
        best_bid = _safe_float(bid_prices[0] if bid_prices else 0.0)

        if best_bid > 0 and best_ask > 0:
            price = (best_bid + best_ask) / 2.0
        else:
            price = best_bid or best_ask
        if price <= 0:
            return  # malformed frame — don't pollute the cache

        # Rough running volume = sum of top-5 levels on both sides.
        book_vol = 0.0
        for v in list(ask_vols)[:5] + list(bid_vols)[:5]:
            book_vol += _safe_float(v)

        tick_time = int(_safe_float(data.get("t"), int(time.time() * 1000)))

        prev = self.ticks.get(sym) or {}

        # ── Bad-tick spike filter (mirror of zerodha_service._on_ticks) ──────
        # A CDS/forex/commodity feed glitch occasionally pushes a garbage price
        # (UKOIL $120 when it's $71). Acting on it phantom-closed positions at
        # junk prices (CL20412429 fake ₹53k profit). Reject any tick whose price
        # jumps >50% from the LAST-GOOD tick within the last few seconds — no
        # real instrument moves that fast tick-to-tick. Accept if the last-good
        # tick is stale (a genuine fast move / fresh session), so the price can
        # never get permanently stuck.
        _prev_ltp = _safe_float(prev.get("ltp") or 0)
        _prev_ts = _safe_float(prev.get("ts") or 0)
        if (
            _prev_ltp > 0
            and abs(price - _prev_ltp) / _prev_ltp > _MAX_TICK_SPIKE_PCT
            and _prev_ts > 0
            and (tick_time - _prev_ts) < _SPIKE_STALE_MS
        ):
            logger.warning(
                "infoway_tick_spike_rejected",
                extra={
                    "symbol": sym,
                    "price": price,
                    "prev_ltp": _prev_ltp,
                    "dev_pct": round(abs(price - _prev_ltp) / _prev_ltp * 100, 1),
                },
            )
            return

        prev_close = _safe_float(prev.get("close_24h") or prev.get("ltp") or price)
        change = price - prev_close
        change_pct = (change / prev_close * 100.0) if prev_close else 0.0

        # Running intraday open / high / low — Infoway sends only tick depth,
        # not OHLC bars, so we synthesise them from the tick stream:
        #   open  = first ltp seen for this symbol (kept forever until restart)
        #   high  = rolling max ltp since first tick
        #   low   = rolling min ltp since first tick
        # These give a "since server start" OHLC rather than a calendar-day
        # one, but they're far more useful than showing 0.
        prev_open = _safe_float(prev.get("open") or 0)
        running_open = prev_open if prev_open > 0 else price
        prev_high = _safe_float(prev.get("high") or 0)
        running_high = max(prev_high, price) if prev_high > 0 else price
        prev_low = _safe_float(prev.get("low") or 0)
        running_low = min(prev_low, price) if prev_low > 0 else price

        self.ticks[sym] = {
            "symbol": sym,
            "ltp": price,
            "bid": best_bid,
            "ask": best_ask,
            "volume": book_vol or _safe_float(prev.get("volume")),
            "ts": tick_time,
            "close_24h": prev_close,
            "change": round(change, 6),
            "change_pct": round(change_pct, 4),
            "open": running_open,
            "high": running_high,
            "low": running_low,
        }

        # Stash the book too — at most top-10 levels per side.
        bids_book = [
            {"price": _safe_float(p), "qty": _safe_float(v)}
            for p, v in zip(list(bid_prices)[:10], list(bid_vols)[:10])
        ]
        asks_book = [
            {"price": _safe_float(p), "qty": _safe_float(v)}
            for p, v in zip(list(ask_prices)[:10], list(ask_vols)[:10])
        ]
        if bids_book or asks_book:
            self.depth[sym] = {"bids": bids_book, "asks": asks_book, "ts": tick_time}

        # Publish to Redis so user-side WS clients can fan out.
        if self._main_loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    publish(f"infoway:tick:{sym}", self.ticks[sym]),
                    self._main_loop,
                )
            except Exception:
                pass


# Singleton
infoway = InfowayService()


def default_symbols() -> list[str]:
    """Merged set of crypto + forex + metals + energy + stocks + indices
    defaults from settings, deduplicated and uppercased."""
    sources = [
        settings.INFOWAY_DEFAULT_CRYPTO or "",
        settings.INFOWAY_DEFAULT_FOREX or "",
        getattr(settings, "INFOWAY_DEFAULT_METALS", "") or "",
        getattr(settings, "INFOWAY_DEFAULT_ENERGY", "") or "",
        getattr(settings, "INFOWAY_DEFAULT_STOCKS", "") or "",
        getattr(settings, "INFOWAY_DEFAULT_INDICES", "") or "",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for src in sources:
        for s in src.split(","):
            t = s.strip().upper()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _split_csv_upper(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


def _stock_codes() -> set[str]:
    return _split_csv_upper(getattr(settings, "INFOWAY_DEFAULT_STOCKS", ""))


def _index_codes() -> set[str]:
    return _split_csv_upper(getattr(settings, "INFOWAY_DEFAULT_INDICES", ""))


_ENERGY_NAMES = {
    "USOIL": "WTI Crude / USD",
    "UKOIL": "Brent Crude / USD",
    "BRENT": "Brent Crude / USD",
    "NATGAS": "Natural Gas / USD",
    "XBRUSD": "Brent Crude / USD",
    "XTIUSD": "WTI Crude / USD",
    "XNGUSD": "Natural Gas / USD",
}


# Friendly display/search names for spot-metal prefixes — so "GOLD" etc.
# match the XAU/XAG/XPT/XPD instruments in /instruments/search.
_METAL_NAMES = {"XAU": "Gold", "XAG": "Silver", "XPT": "Platinum", "XPD": "Palladium"}


def _classify_infoway_code(code: str) -> dict[str, Any]:
    """Map an Infoway code to local catalogue fields (segment, exchange,
    instrument_type, name).

    Resolution order:
        1. Admin's explicit stock allowlist (INFOWAY_DEFAULT_STOCKS) →
           segment=STOCKS, type=SPOT. Highest precedence because a code
           like ``META`` would otherwise look like an unrecognised forex
           pair to the heuristics below.
        2. Admin's explicit index allowlist (INFOWAY_DEFAULT_INDICES) →
           segment=INDICES, type=INDEX.
        3. Crypto suffixes (USDT / USDC / BUSD) → CRYPTO_PERPETUAL.
        4. Metals prefixes (XAU / XAG / XPT / XPD) → COMMODITIES.
        5. Energy lookup table (USOIL / UKOIL / NATGAS / …) → COMMODITIES.
        6. Fallback → FOREX (6-char major/minor crosses land here).
    """
    c = (code or "").strip().upper()
    if c in _stock_codes():
        return {
            "segment": "STOCKS",
            "exchange": "CDS",
            "instrument_type": "SPOT",
            "name": c,
        }
    if c in _index_codes():
        return {
            "segment": "INDICES",
            "exchange": "CDS",
            "instrument_type": "INDEX",
            "name": c,
        }
    # A plain `BTCUSD` / `ETHUSD` (no T suffix) is crypto, not a forex cross —
    # without this it slips past the USDT/USDC/BUSD test and the FOREX
    # fallback below mislabels it, polluting the user app's Forex tab.
    is_crypto = (
        c.endswith("USDT")
        or c.endswith("USDC")
        or c.endswith("BUSD")
        or (c.endswith("USD") and c[:-3] in _CRYPTO_BASES)
    )
    is_metal = c.startswith(("XAU", "XAG", "XPT", "XPD"))
    is_energy = c in _ENERGY_NAMES
    if is_crypto:
        return {
            "segment": "CRYPTO_PERPETUAL",
            "exchange": "CRYPTO",
            "instrument_type": "PERP",
            "name": (
                c.replace("USDT", "/USDT").replace("USDC", "/USDC").replace("BUSD", "/BUSD")
                if c.endswith(("USDT", "USDC", "BUSD"))
                else f"{c[:-3]}/USD"
            ),
        }
    if is_metal:
        # Friendly commodity name so a user searching "GOLD" / "SILVER" finds
        # the XAU/XAG spot instrument — the raw "XAU/USD" name never matched
        # the word the user actually types. Keeps the pair in parens for the
        # FX-literate ("Gold (XAU/USD)").
        base = c[:3]
        quote = c[3:] or "USD"
        friendly = _METAL_NAMES.get(base)
        return {
            "segment": "COMMODITIES",
            "exchange": "CDS",
            "instrument_type": "SPOT",
            "name": f"{friendly} ({base}/{quote})" if friendly else f"{base}/{quote}",
        }
    if is_energy:
        return {
            "segment": "COMMODITIES",
            "exchange": "CDS",
            "instrument_type": "SPOT",
            "name": _ENERGY_NAMES[c],
        }
    return {
        "segment": "FOREX",
        "exchange": "CDS",
        "instrument_type": "SPOT",
        "name": f"{c[:3]}/{c[3:]}" if len(c) >= 6 else c,
    }


async def upsert_instrument_for_code(code: str) -> bool:
    """Insert or heal the catalogue row for one global symbol. Returns True
    when a row was created. Shared by the Infoway mirror (symbols we happen to
    be subscribed to) and the boot seed (symbols we always want listed)."""
    from bson import Decimal128

    from app.models._base import Exchange, InstrumentType
    from app.models.instrument import Instrument
    from app.services.infoway_lots import get_infoway_lot_size

    # Never mirror internal seed tokens (NSE_EQ_*, NSE_IDX_*, BSE_IDX_*,
    # MCX_FUT_*, CRYPTO_*, FX_*) or junk codes ("UNDEFINED"). Real Infoway
    # symbols are clean tickers (EURUSD, BTCUSDT, XAUUSD, AAPL) with no
    # underscores. If such an internal token ever sneaks into a channel's
    # subscribe set, mirroring it here would overwrite the legit seed row's
    # segment with the FOREX fallback — exactly the corruption that filled
    # the Forex tab with crypto / Indian stocks / indices.
    if "_" in code or code == "UNDEFINED" or not code.strip():
        return False
    meta = _classify_infoway_code(code)
    try:
        ex = Exchange(meta["exchange"])
    except ValueError:
        return False
    try:
        it = InstrumentType(meta["instrument_type"])
    except ValueError:
        it = InstrumentType.SPOT

    # Retail-CFD contract size by symbol. Forex → 100,000 base units
    # per lot, spot gold → 100 troy oz, USOIL → 1,000 barrels, etc.
    # Falls back to a per-segment default for unlisted symbols.
    lot = get_infoway_lot_size(code, meta.get("segment"))

    existing = await Instrument.find_one(Instrument.token == code)
    if existing is None:
        await Instrument(
            token=code,
            symbol=code,
            trading_symbol=code,
            name=meta["name"],
            exchange=ex,
            segment=meta["segment"],
            instrument_type=it,
            lot_size=lot,
            tick_size=Decimal128("0.0001"),
            is_active=True,
            is_tradable=True,
        ).insert()
        return True
    else:
        existing.exchange = ex
        existing.segment = meta["segment"]
        existing.instrument_type = it
        # Heal the display name too so rows mirrored before the friendly
        # commodity names existed (e.g. "XAU/USD" → "Gold (XAU/USD)")
        # become searchable by "GOLD" on the next mirror pass.
        existing.name = meta["name"]
        # Heal stored lot_size when it disagrees with the canonical
        # value — legacy rows seeded before this table existed had
        # `lot_size = 1` which silently understated notional /
        # margin by 100,000× for a 1-lot forex order.
        if int(existing.lot_size or 0) != lot:
            existing.lot_size = lot
        existing.is_active = True
        existing.is_tradable = True
        await existing.save()
    return False


async def mirror_subscribed_to_instruments() -> int:
    """Idempotent: insert/update an `Instrument` row for every Infoway code
    we have subscribed to. User-side `/instruments/search` then finds
    forex/crypto/metals/energy symbols just like Indian instruments."""
    subs: set[str] = set()
    for ch in infoway._channels.values():
        subs |= ch._subscribed

    mirrored = 0
    for code in sorted(subs):
        if await upsert_instrument_for_code(code):
            mirrored += 1
    if mirrored:
        logger.info("infoway_mirror_done", extra={"count": mirrored})
    return mirrored


async def seed_default_instruments() -> int:
    """Catalogue rows for the configured default forex / metals / energy /
    stocks / indices symbols, whether or not Infoway is connected.

    The terminal's Stocks and Indices tabs read the catalogue, and on a
    MetaAPI-fed deployment Infoway never subscribes (so never mirrors) those
    symbols — which left both tabs empty. Crypto is deliberately excluded: the
    Indian seed already ships CRYPTO_SPOT rows (BTCUSD…), and adding Infoway's
    USDT pairs on top would show the same coin twice.
    """
    sources = [
        settings.INFOWAY_DEFAULT_FOREX or "",
        getattr(settings, "INFOWAY_DEFAULT_METALS", "") or "",
        getattr(settings, "INFOWAY_DEFAULT_ENERGY", "") or "",
        getattr(settings, "INFOWAY_DEFAULT_STOCKS", "") or "",
        getattr(settings, "INFOWAY_DEFAULT_INDICES", "") or "",
    ]
    created = 0
    seen: set[str] = set()
    for src in sources:
        for raw in src.split(","):
            code = raw.strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            if await upsert_instrument_for_code(code):
                created += 1
    # NB: not `extra={"created": ...}` — `created` is a reserved LogRecord
    # attribute and logging raises KeyError on the overwrite, which aborted
    # the whole boot seed in production.
    logger.info(
        "default_instruments_seeded", extra={"inserted": created, "symbols": len(seen)}
    )
    return created
