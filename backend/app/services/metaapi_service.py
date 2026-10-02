"""MetaAPI (MetaTrader 4/5) feed for FOREX / METALS / INDICES / COMMODITIES.

Streams real-time bid/ask from a connected MT4/MT5 account via metaapi.cloud.
Wired in as the price source for those segments (in place of Infoway) when
``METAAPI_FEED=true``. Crypto stays on Binance; Infoway remains the automatic
FALLBACK if MetaAPI has no tick for a symbol, so nothing breaks during rollout.

Same in-memory-cache shape as ``infoway_service`` / ``binance_service``:
``get_tick(symbol)`` returns ``{ltp, bid, ask, change, change_pct, volume, open,
high, low, close_24h}`` so ``market_data_service._infoway_overlay`` merges it
identically. LTP = mid (bid+ask)/2. Rolling intraday OHLC + session change are
synthesised from the streamed quotes (MT quotes carry no 24h stats), exactly
like the Infoway path.

The streaming SDK keeps ``connection.terminal_state`` fresh in real time; the
0.1 s market tick_loop samples ``get_tick`` off it, so movement is smooth/fast
(well under the requested 0.25 s cadence) without any extra loop here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# Reject a lone tick that jumps more than this fraction vs the last price —
# mirrors infoway_service / binance_service spike guards.
_MAX_TICK_SPIKE_PCT = 0.5

# Cross-process control. The admin API answers on any HTTP worker, but the
# feed only exists in the feed process: commands travel one way on this
# channel, the live status snapshot comes back through the Redis key.
METAAPI_CMD_CHANNEL = "metaapi:cmd"
METAAPI_STATUS_KEY = "metaapi:status"

# An MT account that is deployed but not logged in to the broker makes the SDK
# wait forever, so every connect stage is bounded — a stuck account must show
# up in the admin panel as an error, not as silence.
_CONNECT_TIMEOUT_SEC = 90
# The initial synchronize is in a different league from the connect stages:
# measured on this account it takes ~190s, and MetaApi's SDK resynchronizes
# internally when its first attempt "did not finish in time". Their own
# default is 300s. We used to allow 60s, which this account could never meet
# — every attempt was killed mid-sync and retried, forever.
_SYNC_TIMEOUT_SEC = 300
# Heartbeat so the panel can tell "connecting" from "feed process is down".
_STATUS_HEARTBEAT_SEC = 10

# MetaApi rate-limits subscribe/synchronize per account. A failed sync gets a
# much slower ladder than an ordinary connection error, because hammering it
# every minute is what keeps the account throttled.
_SYNC_BACKOFF_BASE_SEC = 60
_SYNC_BACKOFF_MAX_SEC = 900

def sync_backoff_sec(consecutive_failures: int) -> float:
    """Seconds to wait before retrying after `n` failed synchronizes."""
    if consecutive_failures < 1:
        return float(_SYNC_BACKOFF_BASE_SEC)
    return float(
        min(
            _SYNC_BACKOFF_BASE_SEC * (2 ** (consecutive_failures - 1)),
            _SYNC_BACKOFF_MAX_SEC,
        )
    )


# The broker's full symbol universe, published by the feed process so the API
# workers can search it without opening their own MetaAPI connection.
METAAPI_BROKER_SYMBOLS_KEY = "metaapi:broker_symbols"
_BROKER_SYMBOLS_TTL_SEC = 3600

# Brokers decorate the same instrument in ways that are pure noise to us:
# AAPL.US, EURUSD.r, US500-F, TSLA.US-PERP. Strip the venue/variant so one
# catalogue row covers them, and remember the broker's exact spelling for the
# subscribe call.
_VARIANT_SUFFIXES = ("-PERP", "-24", "-F", "-CASH", "CASH", "PRO", "RAW", "ECN", "STP")

# Genuine renames, tried only when the platform name isn't offered verbatim.
_SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "SPX500": ("US500", "SP500", "SPX"),
    "DE40": ("GER40", "DAX40", "GER30"),
    "UK100": ("FTSE100", "UK100"),
    "NAS100": ("USTEC", "NDX100", "TECH100"),
    "US30": ("DJ30", "WS30", "DOW30"),
    "JPN225": ("JP225", "NIKKEI225"),
    "HK50": ("HSI50", "HK50"),
    "USOIL": ("SPOTCRUDE", "XTIUSD", "WTI", "CRUDEOIL", "USCRUDE"),
    "UKOIL": ("SPOTBRENT", "XBRUSD", "BRENT", "BRENTOIL"),
    "NATGAS": ("XNGUSD", "NGAS", "NATURALGAS"),
    "XAUUSD": ("GOLD", "GOLDUSD"),
    "XAGUSD": ("SILVER", "SILVERUSD"),
}


def clean_broker_symbol(raw: str) -> str:
    """Broker spelling → our catalogue symbol: AAPL.US-24 → AAPL, US500-F →
    US500, EURUSD.r → EURUSD."""
    s = (raw or "").strip().upper()
    if not s:
        return ""
    s = s.split(".", 1)[0]
    changed = True
    while changed:
        changed = False
        for suffix in _VARIANT_SUFFIXES:
            if s.endswith(suffix) and len(s) > len(suffix) + 2:
                s = s[: -len(suffix)]
                changed = True
    return s.strip("-_")


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _silence_sdk_loggers() -> None:
    """The metaapi-cloud-sdk (socket.io / engine.io transport) logs EVERY
    websocket packet at INFO — including the full symbol-specifications dump
    (thousands of instruments). Left alone it floods journalctl + burns disk.
    Pin those transport loggers to WARNING so only real problems surface."""
    for name in (
        "engineio",
        "engineio.client",
        "socketio",
        "socketio.client",
        "metaapi",
        "aiohttp.access",
    ):
        try:
            logging.getLogger(name).setLevel(logging.WARNING)
        except Exception:
            pass


class MetaApiFeed:
    """Singleton MT feed. Lives in the running backend process (leader)."""

    def __init__(self) -> None:
        self._api: Any = None
        self._account: Any = None
        self._conn: Any = None
        self._connected = False
        self._symbols: list[str] = []
        # Per-symbol rolling state for OHLC + change synthesis.
        self._state: dict[str, dict[str, float]] = {}
        self._alias: dict[str, str] = {}   # platform symbol -> MT broker symbol
        # Broker symbols this MT account does NOT offer (returned "symbol does
        # not exist"). Memoised so we never re-attempt them — otherwise every
        # WS subscribe of a crypto/forex/stock token the broker lacks (e.g.
        # CRYPTO_BTCUSD, BTCUSDT, FX_EURUSD, NSE_EQ_*) re-fires a doomed
        # subscribe on every tick and floods the (isolated) feed process CPU
        # + journal with metaapi_on_demand_subscribe_failed.
        self._unavailable: set[str] = set()
        # MetaAPI accounts have a HARD per-account cap on concurrent symbol
        # subscriptions (plan-dependent; the current plan = 25). Exceeding it
        # makes the SDK's subscription_manager retry the rejected symbol every
        # ~10s forever with a TooManyRequestsException(429) flood, and the
        # excess symbols get no MetaAPI tick. We therefore cap our own list a
        # touch below the account limit; symbols beyond the cap simply fall
        # back to the Infoway feed (market_data_service._infoway_overlay already
        # does MetaAPI-first-then-Infoway), so nothing freezes.
        self._max_symbols: int = int(getattr(settings, "METAAPI_MAX_SYMBOLS", 24) or 24)
        self._cap_log_ts = 0.0
        self._task: asyncio.Task[Any] | None = None
        self._status_task: asyncio.Task[Any] | None = None
        self._stop = False
        self._last_rx = 0.0
        self._last_error = ""
        # Resolved config — admin row over .env. See load_config().
        self._cfg: dict[str, Any] | None = None
        # The broker's own symbol universe, kept in the broker's exact
        # spelling (suffix case matters when subscribing: "EURUSD.r").
        self._broker_symbols: set[str] = set()
        self._broker_by_upper: dict[str, str] = {}   # EURUSD.R  -> EURUSD.r
        self._broker_by_clean: dict[str, str] = {}   # EURUSD    -> EURUSD.r

    # ── config: admin panel first, .env fallback ──────────────────────
    async def _fetch_doc(self) -> Any:
        """The single MetaApiSettings row, or None when it (or Mongo) is absent."""
        try:
            from app.models.metaapi_settings import MetaApiSettings

            return await MetaApiSettings.find_one()
        except Exception:
            logger.debug("metaapi_settings_fetch_failed", exc_info=True)
            return None

    async def load_config(self) -> dict[str, Any]:
        """Resolve the live config and cache it on the instance.

        The admin row wins field by field; every field it leaves empty falls
        back to the matching METAAPI_* env var, so a deployment that hasn't
        been switched over to the admin panel keeps running unchanged.
        """
        doc = await self._fetch_doc()

        token = ""
        if doc is not None and getattr(doc, "encrypted_token", ""):
            try:
                from app.utils.crypto import decrypt

                token = decrypt(doc.encrypted_token, doc.encrypted_token_iv)
            except Exception:
                logger.warning(
                    "metaapi_token_decrypt_failed — ZERODHA_CREDS_KEY rotated? "
                    "Re-save the token in the admin panel."
                )
        if not token:
            try:
                token = settings.METAAPI_TOKEN.get_secret_value()
            except Exception:
                token = ""

        account_id = (getattr(doc, "account_id", "") or "") or (
            getattr(settings, "METAAPI_ACCOUNT_ID", "") or ""
        )
        region = (getattr(doc, "region", "") or "") or (
            getattr(settings, "METAAPI_REGION", "") or ""
        )
        enabled = (
            bool(doc.enabled) if doc is not None
            else bool(getattr(settings, "METAAPI_FEED", False))
        )
        max_symbols = int(getattr(doc, "max_symbols", 0) or 0) or int(
            getattr(settings, "METAAPI_MAX_SYMBOLS", 24) or 24
        )
        alias = dict(getattr(doc, "symbol_map", {}) or {}) or self._resolve_alias()

        symbols: list[str] = []
        seen: set[str] = set()
        raw_symbols = [
            str(s).strip().upper() for s in (getattr(doc, "symbols", []) or []) if str(s).strip()
        ] or self._resolve_symbols_from_env()
        for s in raw_symbols:
            if s not in seen:
                seen.add(s)
                symbols.append(s)
        # Past the account cap MetaAPI rejects subscriptions and the SDK retries
        # them forever (429 flood); the extras use the Infoway fallback instead.
        if len(symbols) > max_symbols:
            logger.warning(
                "metaapi_symbols_capped from=%d to=%d", len(symbols), max_symbols
            )
            symbols = symbols[:max_symbols]

        configured = bool(token) and bool(account_id)
        self._cfg = {
            "enabled": enabled and configured,
            "configured": configured,
            "token": token,
            "account_id": account_id,
            "region": region,
            "symbols": symbols,
            "alias": alias,
            "max_symbols": max_symbols,
            "source": "admin" if doc is not None else "env",
        }
        self._max_symbols = max_symbols
        return self._cfg

    # ── public accessors ──────────────────────────────────────────────
    def is_enabled(self) -> bool:
        """Cached view for boot checks and status; load_config() is the source."""
        if self._cfg is not None:
            return bool(self._cfg.get("enabled"))
        try:
            has_token = bool(settings.METAAPI_TOKEN.get_secret_value())
        except Exception:
            has_token = False
        return bool(
            getattr(settings, "METAAPI_FEED", False)
            and has_token
            and getattr(settings, "METAAPI_ACCOUNT_ID", "")
        )

    @property
    def is_connected(self) -> bool:
        return self._connected

    def status(self) -> dict[str, Any]:
        now = time.time()
        cfg = self._cfg or {}
        return {
            "enabled": self.is_enabled(),
            "configured": bool(cfg.get("configured")),
            "connected": self._connected,
            "account_id": cfg.get("account_id", ""),
            "region": cfg.get("region", ""),
            "source": cfg.get("source", ""),
            "symbols": self._symbols,
            "max_symbols": self._max_symbols,
            "tick_count": len(self._state),
            "last_error": self._last_error,
            "last_rx_age_sec": round(now - self._last_rx, 1) if self._last_rx else None,
        }

    async def _publish_status(self) -> None:
        """Mirror the status into Redis so the admin API can read it from any
        worker — the feed itself only exists in this process."""
        try:
            from app.core.redis_client import cache_set

            await cache_set(METAAPI_STATUS_KEY, self.status(), ttl_sec=20)
        except Exception:
            logger.debug("metaapi_status_publish_failed", exc_info=True)

    def _mt_symbol(self, sym: str) -> str:
        """Map a platform symbol to this broker's spelling.

        Order: the admin's explicit alias map (always wins), then the broker's
        own symbol list — exact match, then the cleaned-name index (so AAPL
        finds AAPL.US and US500 finds US500-F), then the rename table. Falls
        back to the symbol itself when the broker list isn't loaded yet.
        """
        s = (sym or "").upper()
        explicit = self._alias.get(s)
        if explicit:
            return explicit
        if not self._broker_symbols:
            return sym
        hit = self._broker_by_upper.get(s) or self._broker_by_clean.get(s)
        if hit:
            return hit
        for candidate in _SYMBOL_ALIASES.get(s, ()):  # genuine renames
            hit = self._broker_by_upper.get(candidate) or self._broker_by_clean.get(candidate)
            if hit:
                return hit
        return sym

    # ── Broker symbol universe ────────────────────────────────────────
    def _index_broker_symbols(self, symbols: list[str]) -> None:
        """Index the universe for lookup while preserving the broker's exact
        spelling — subscribing with "EURUSD.R" when the broker means
        "EURUSD.r" gets the symbol rejected. When several variants clean to
        the same name (AAPL.US, AAPL.US-24) the shortest wins: the plain spot
        contract."""
        self._broker_symbols = {s for s in symbols if s}
        by_upper: dict[str, str] = {}
        by_clean: dict[str, str] = {}
        for exact in sorted(self._broker_symbols, key=len):
            by_upper.setdefault(exact.upper(), exact)
            clean = clean_broker_symbol(exact)
            if clean:
                by_clean.setdefault(clean, exact)
        self._broker_by_upper = by_upper
        self._broker_by_clean = by_clean

    async def _refresh_broker_symbols(self) -> None:
        """Read the universe off the synchronised terminal (free — no extra
        connection) and publish it for the API workers' instrument search."""
        symbols: list[str] = []
        try:
            specs = getattr(self._conn.terminal_state, "specifications", None) or []
            for spec in specs:
                name = spec.get("symbol") if isinstance(spec, dict) else getattr(spec, "symbol", None)
                if name:
                    symbols.append(str(name).strip())
        except Exception:
            logger.debug("metaapi_specifications_read_failed", exc_info=True)
        if not symbols:
            return
        self._index_broker_symbols(symbols)
        try:
            from app.core.redis_client import cache_set

            await cache_set(
                METAAPI_BROKER_SYMBOLS_KEY, sorted(self._broker_symbols), ttl_sec=_BROKER_SYMBOLS_TTL_SEC
            )
        except Exception:
            logger.debug("metaapi_broker_symbols_publish_failed", exc_info=True)
        logger.info("metaapi_broker_symbols_indexed count=%d", len(self._broker_symbols))

    async def list_broker_symbols(self, refresh: bool = False) -> list[str]:
        """Every symbol this MT account offers. Served from the feed process's
        published snapshot; an API worker only opens its own RPC connection
        when that snapshot is missing (or the caller forces a refresh)."""
        from app.core.redis_client import cache_get, cache_set

        if not refresh:
            if self._broker_symbols:
                return sorted(self._broker_symbols)
            cached = await cache_get(METAAPI_BROKER_SYMBOLS_KEY)
            if cached:
                self._index_broker_symbols(list(cached))
                return sorted(self._broker_symbols)

        cfg = self._cfg or await self.load_config()
        if not cfg["configured"]:
            return []
        from metaapi_cloud_sdk import MetaApi

        api = (
            MetaApi(cfg["token"], {"region": cfg["region"]})
            if cfg["region"]
            else MetaApi(cfg["token"])
        )
        account = await api.metatrader_account_api.get_account(cfg["account_id"])
        conn = account.get_rpc_connection()
        try:
            await conn.connect()
            await conn.wait_synchronized(60)
            raw = await conn.get_symbols()
        finally:
            try:
                await conn.close()
            except Exception:
                pass
        symbols = [str(s).strip() for s in (raw or []) if str(s).strip()]
        self._index_broker_symbols(symbols)
        await cache_set(
            METAAPI_BROKER_SYMBOLS_KEY, sorted(self._broker_symbols), ttl_sec=_BROKER_SYMBOLS_TTL_SEC
        )
        return sorted(self._broker_symbols)

    async def offers(self, platform_symbol: str) -> bool:
        """True when the broker carries this symbol under any spelling."""
        s = (platform_symbol or "").strip().upper()
        if not s:
            return False
        if not self._broker_by_clean:
            await self.list_broker_symbols()
        if s in self._broker_by_upper or s in self._broker_by_clean:
            return True
        return any(
            c in self._broker_by_upper or c in self._broker_by_clean
            for c in _SYMBOL_ALIASES.get(s, ())
        )

    async def search_symbols(self, q: str, limit: int = 30) -> list[str]:
        """Catalogue-shaped names matching `q`, de-duplicated across broker
        variants (AAPL.US and AAPL.US-24 both surface once, as AAPL).
        Prefix matches rank above substring matches."""
        needle = (q or "").strip().upper()
        if not needle:
            return []
        if not self._broker_by_clean:
            await self.list_broker_symbols()
        names = sorted(self._broker_by_clean.keys())
        starts = [n for n in names if n.startswith(needle)]
        contains = [n for n in names if needle in n and not n.startswith(needle)]
        return (starts + contains)[:limit]

    def _looks_non_metaapi(self, sym: str) -> bool:
        """Cheap pre-filter for PLATFORM tokens that clearly aren't MT symbols,
        so we never even attempt a subscribe the broker will always reject:
          - namespaced platform tokens carry an underscore (CRYPTO_BTCUSD,
            FX_EURUSD, NSE_EQ_TATAMOTORS) — real MT symbols don't (EURUSD,
            XAUUSD, USOIL, SPX500, AAPL.US).
          - Binance-style crypto pairs end in USDT (BTCUSDT, ETHUSDT).
        MetaAPI only carries forex / metals / energy / indices / US-stocks."""
        s = (sym or "").upper()
        return ("_" in s) or s.endswith("USDT")

    def _note_sub_error(self, broker: str, err: Exception, log_msg: str) -> None:
        """Classify a subscribe failure. A "symbol does not exist" is EXPECTED
        for non-MetaAPI tokens — memoise it (never retry) and log at debug so
        it doesn't flood. Anything else is a real problem → warning."""
        text = str(err).lower()
        if "does not exist" in text or "not found" in text:
            self._unavailable.add(broker)
            logger.debug("%s %s: not offered by broker (memoised)", log_msg, broker)
        else:
            logger.warning("%s %s: %s", log_msg, broker, err)

    def get_tick(self, symbol: str | None) -> dict[str, Any] | None:
        if not symbol or self._conn is None:
            return None
        sym = symbol.upper()
        mt = self._mt_symbol(sym)
        price = None
        try:
            ts = self._conn.terminal_state
            if ts is not None:
                price = ts.price(symbol=mt)
        except Exception:
            price = None
        if not price:
            return None
        bid = _f(price.get("bid"))
        ask = _f(price.get("ask"))
        if bid <= 0 and ask <= 0:
            return None
        ltp = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (bid or ask)
        if ltp <= 0:
            return None

        st = self._state.get(sym)
        if st is None:
            st = {"open": ltp, "high": ltp, "low": ltp, "last": ltp}
            self._state[sym] = st
        else:
            prev = st.get("last") or ltp
            # Bad-tick spike guard — ignore a lone >50% jump, keep last-known.
            if prev > 0 and abs(ltp - prev) / prev > _MAX_TICK_SPIKE_PCT:
                ltp = prev
                bid = st.get("bid", bid)
                ask = st.get("ask", ask)
            else:
                if ltp > st["high"]:
                    st["high"] = ltp
                if ltp < st["low"]:
                    st["low"] = ltp
        st["last"] = ltp
        st["bid"] = bid
        st["ask"] = ask
        self._last_rx = time.time()
        # The BROKER's own tick time, not our read time. terminal_state keeps
        # the last price it received, so when the stream freezes this stops
        # advancing while `ts` below keeps ticking — which is the whole point:
        # the stale-price guard needs a signal our own loop can't fake.
        feed_ts = None
        _t = price.get("time")
        if hasattr(_t, "timestamp"):
            try:
                feed_ts = _t.timestamp()
            except Exception:
                feed_ts = None
        change = ltp - st["open"]
        return {
            "ltp": ltp,
            "bid": bid,
            "ask": ask,
            "change": change,
            "change_pct": (change / st["open"] * 100.0) if st["open"] else 0.0,
            "volume": 0.0,
            "open": st["open"],
            "high": st["high"],
            "low": st["low"],
            "close_24h": st["open"],  # no true prev-close from MT → session open
            "ts": self._last_rx,
            "feed_ts": feed_ts,
        }

    # ── config ────────────────────────────────────────────────────────
    def _resolve_symbols_from_env(self) -> list[str]:
        raw = (getattr(settings, "METAAPI_SYMBOLS", "") or "").strip()
        if not raw:
            # Default: reuse the Infoway forex / metals / energy / indices lists
            # (same standard MT symbol names — EURUSD, XAUUSD, USOIL, SPX500…).
            parts = [
                getattr(settings, "INFOWAY_DEFAULT_FOREX", "") or "",
                getattr(settings, "INFOWAY_DEFAULT_METALS", "") or "",
                getattr(settings, "INFOWAY_DEFAULT_ENERGY", "") or "",
                getattr(settings, "INFOWAY_DEFAULT_INDICES", "") or "",
            ]
            raw = ",".join(p for p in parts if p)
        seen: set[str] = set()
        out: list[str] = []
        for s in (x.strip().upper() for x in raw.split(",")):
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def _resolve_alias(self) -> dict[str, str]:
        raw = (getattr(settings, "METAAPI_SYMBOL_MAP", "") or "").strip()
        out: dict[str, str] = {}
        for pair in raw.split(","):
            if ":" in pair:
                p, m = pair.split(":", 1)
                p = p.strip().upper()
                m = m.strip()
                if p and m:
                    out[p] = m
        return out

    # ── lifecycle ─────────────────────────────────────────────────────
    async def start(self) -> None:
        cfg = await self.load_config()
        if not cfg["enabled"]:
            logger.info(
                "metaapi_feed_skipped: disabled or token/account missing (source=%s)",
                cfg["source"],
            )
            await self._publish_status()
            return
        _silence_sdk_loggers()
        self._symbols = list(cfg["symbols"])
        self._alias = dict(cfg["alias"])
        self._stop = False
        self._last_error = ""
        if self._task is not None and not self._task.done():
            logger.info("metaapi_feed_already_running")
            return
        self._task = asyncio.create_task(self._run_loop(), name="metaapi_feed")
        if self._status_task is None or self._status_task.done():
            self._status_task = asyncio.create_task(
                self._status_loop(), name="metaapi_status"
            )
        logger.info(
            "metaapi_feed_started symbols=%d aliases=%d source=%s",
            len(self._symbols),
            len(self._alias),
            cfg["source"],
        )

    async def restart(self) -> None:
        """Apply freshly saved settings: stop, forget the cached config, start."""
        await self.stop()
        self._cfg = None
        await self.start()

    async def subscribe(self, symbols: list[str]) -> None:
        """On-demand: ensure `symbols` are streamed by the MT terminal.

        The startup list (`_resolve_symbols`) covers the default forex / metals
        / energy / indices, but a watchlist symbol outside that list would
        never be subscribed upstream → `terminal_state.price` returns None →
        `get_tick` None → frozen 0.0000. Callers pass PLATFORM symbols; we map
        each through the alias to the broker name before subscribing. Newly
        seen symbols are also remembered so a reconnect re-subscribes them.
        Best-effort + idempotent (the SDK no-ops an already-subscribed symbol).
        """
        if not symbols:
            return
        for raw in symbols:
            sym = (raw or "").strip().upper()
            if not sym:
                continue
            broker = self._mt_symbol(sym)
            # Skip tokens the broker doesn't carry (already-known-unavailable or
            # an obviously-non-MetaAPI namespace) so we don't pollute the
            # persistent symbol list or spam doomed subscribes every tick.
            if broker in self._unavailable or self._looks_non_metaapi(sym):
                continue
            if sym not in self._symbols:
                # Respect the account subscription cap — a new symbol beyond it
                # would 429-flood; it falls back to Infoway instead (no freeze).
                if len(self._symbols) >= self._max_symbols:
                    now = time.time()
                    if now - self._cap_log_ts > 60:
                        self._cap_log_ts = now
                        logger.info(
                            "metaapi_subscription_cap_reached max=%d skipping=%s "
                            "(uses Infoway fallback)", self._max_symbols, sym
                        )
                    continue
                self._symbols.append(sym)
            if self._conn is None or not self._connected:
                continue  # will be picked up by the startup subscribe on connect
            try:
                await self._conn.subscribe_to_market_data(broker)
            except Exception as e:  # noqa: BLE001
                self._note_sub_error(broker, e, "metaapi_on_demand_subscribe_failed")

    async def _status_loop(self) -> None:
        """Publish the status every few seconds while the feed is up. Without
        it a connect that hangs upstream looks identical to a dead feed
        process, because status is only written on connect / error / stop."""
        try:
            while not self._stop:
                await self._publish_status()
                await asyncio.sleep(_STATUS_HEARTBEAT_SEC)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("metaapi_status_loop_failed", exc_info=True)

    async def _teardown_client(self) -> None:
        """Close the streaming connection AND the MetaApi client.

        Closing only the connection leaves the client's websocket and its
        subscription jobs running. Each reconnect then built another one, and
        after an hour of retries the account had dozens of clients all trying
        to subscribe — MetaApi answered every sync with a timeout even though
        the terminal itself was CONNECTED.
        """
        try:
            if self._conn is not None:
                await self._conn.close()
        except Exception:
            logger.debug("metaapi_conn_close_failed", exc_info=True)
        self._conn = None
        try:
            if self._api is not None:
                # Sync method: schedules the socket close and stops the jobs.
                self._api.close()
        except Exception:
            logger.debug("metaapi_client_close_failed", exc_info=True)
        self._api = None

    async def stop(self) -> None:
        self._stop = True
        self._connected = False
        if self._status_task is not None:
            self._status_task.cancel()
            self._status_task = None
        await self._teardown_client()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._publish_status()

    async def _run_loop(self) -> None:
        # Two ladders. An ordinary connection error doubles from 2s to a
        # minute; a failed SYNC backs off much harder, because MetaApi
        # rate-limits subscribe/synchronize per account and a fresh attempt
        # every minute competes with the subscription their server is still
        # holding from the last one.
        delay = 2.0
        sync_failures = 0
        while not self._stop:
            try:
                await self._connect_once()
                delay = 2.0
                sync_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                self._last_error = str(e)[:300]
                logger.warning("metaapi_error: %s", e)
                if "synchroniz" in str(e).lower() or "timed out" in str(e).lower():
                    sync_failures += 1
                    delay = sync_backoff_sec(sync_failures)
                    logger.warning(
                        "metaapi_sync_backoff next_try_in_sec=%d consecutive=%d",
                        delay,
                        sync_failures,
                    )
                else:
                    delay = min(delay * 2, 60.0)
            finally:
                self._connected = False
                await self._publish_status()
                await self._teardown_client()
            if self._stop:
                break
            # NOTE: sleep exactly what was decided above. An earlier
            # `min(delay, 60)` here silently capped the sync ladder, so the
            # log said "next_try_in_sec=900" and the loop retried in 60.
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        from metaapi_cloud_sdk import MetaApi  # imported lazily so the dep is optional

        cfg = self._cfg or await self.load_config()
        token = cfg["token"]
        acc_id = cfg["account_id"]
        region = (cfg["region"] or "").strip()

        self._api = MetaApi(token, {"region": region}) if region else MetaApi(token)
        self._account = await self._api.metatrader_account_api.get_account(acc_id)

        # Ensure the account is deployed + connected to the broker before we
        # open the market-data stream. `wait_connected()` never returns while
        # the broker login fails (wrong password, dead demo server), so bound
        # it and report what MetaAPI says about the account.
        state = getattr(self._account, "state", None)
        conn_status = getattr(self._account, "connection_status", None)
        logger.info(
            "metaapi_account_state state=%s connection=%s", state, conn_status
        )
        try:
            if state and state not in ("DEPLOYED",):
                await asyncio.wait_for(self._account.deploy(), _CONNECT_TIMEOUT_SEC)
            await asyncio.wait_for(
                self._account.wait_connected(), _CONNECT_TIMEOUT_SEC
            )
        except TimeoutError as e:
            raise RuntimeError(
                f"MT account did not connect within {_CONNECT_TIMEOUT_SEC}s "
                f"(MetaAPI reports state={state}, connection={conn_status}). "
                "Check the account on metaapi.cloud — it must show Connected, "
                "not just Deployed."
            ) from e

        self._conn = self._account.get_streaming_connection()
        await asyncio.wait_for(self._conn.connect(), _CONNECT_TIMEOUT_SEC)
        await asyncio.wait_for(
            self._conn.wait_synchronized({"timeoutInSeconds": _SYNC_TIMEOUT_SEC}),
            # Outer guard sits ABOVE the SDK's own timeout so the SDK gets to
            # report why it gave up instead of us cutting it off first.
            _SYNC_TIMEOUT_SEC + 30,
        )
        await self._refresh_broker_symbols()
        self._connected = True
        self._last_error = ""
        self._last_rx = time.monotonic()
        logger.info("metaapi_connected account=%s", acc_id)
        await self._publish_status()

        _sub_count = 0
        for s in self._symbols:
            broker = self._mt_symbol(s)
            if broker in self._unavailable or self._looks_non_metaapi(s):
                continue
            try:
                await self._conn.subscribe_to_market_data(broker)
                _sub_count += 1
            except Exception as e:  # noqa: BLE001
                self._note_sub_error(broker, e, "metaapi_subscribe_failed")

        logger.info("metaapi_subscribed symbols=%d", _sub_count)

        # Keep the connection alive; the SDK streams into terminal_state in the
        # background. Bail out (→ reconnect) if it desynchronises.
        while not self._stop:
            await asyncio.sleep(5)
            await self._publish_status()
            try:
                if not getattr(self._conn, "synchronized", True):
                    logger.warning("metaapi_desynchronised_forcing_reconnect")
                    return
            except Exception:
                return


    async def cmd_listener(self) -> None:
        """Run admin commands (connect / disconnect / resubscribe) published by
        the API workers on `metaapi:cmd`. Started in the feed process only."""
        import json

        from app.core.redis_client import pubsub

        backoff = 1.0
        ps: Any = None
        try:
            while True:
                try:
                    if ps is None:
                        ps = pubsub()
                        await ps.subscribe(METAAPI_CMD_CHANNEL)
                        logger.info("metaapi_cmd_listener_started")
                    async for msg in ps.listen():
                        if msg.get("type") != "message":
                            continue
                        raw = msg.get("data")
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "ignore")
                        try:
                            action = (json.loads(raw) or {}).get("action")
                        except Exception:
                            continue
                        logger.info("metaapi_cmd_received action=%s", action)
                        try:
                            if action == "disconnect":
                                await self.stop()
                            elif action in ("connect", "resubscribe"):
                                await self.restart()
                        except Exception:
                            logger.exception("metaapi_cmd_failed action=%s", action)
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("metaapi_cmd_listener_error", exc_info=True)
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
                    await ps.unsubscribe(METAAPI_CMD_CHANNEL)
                except Exception:
                    pass


metaapi = MetaApiFeed()
