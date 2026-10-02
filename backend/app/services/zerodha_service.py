"""Zerodha Kite Connect integration.

Wraps the official `kiteconnect` Python SDK:
  • REST: login URL, generate session, instruments CSV, quotes, historical data
  • WebSocket (KiteTicker): live binary tick stream for subscribed instruments

The service runs as a singleton (`zerodha`). Live ticks are kept in an
in-memory `ticks_cache` dict keyed by symbol AND token, and pushed to Redis
pub/sub channels for WS fanout to user browsers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.core.redis_client import (
    cache_delete_pattern,
    cache_get,
    cache_set,
    idempotency_check_and_set,
    publish,
)
from app.models.zerodha_settings import (
    SubscribedInstrument,
    WsStatus,
    ZerodhaSettings,
)
from app.utils.time_utils import now_utc

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


def _next_kite_expiry_utc() -> datetime:
    """When the app treats a fresh Kite token as expired: TOMORROW 07:00 IST.

    Kite itself flushes access tokens at ~06:00 IST (regulatory). At 07:00
    self-heal clears the token and triggers auto-login, so a scheduled
    auto-login (e.g. 07:15) is the backup before the 09:15 open. Always
    tomorrow regardless of when generated — returning TODAY for pre-07:00
    logins caused an immediate "token expired" status.
    """
    now_ist = datetime.now(IST)
    tomorrow = now_ist + timedelta(days=1)
    target = tomorrow.replace(hour=7, minute=0, second=0, microsecond=0)
    return target.astimezone(timezone.utc)


def _ensure_aware_utc(dt: datetime | None) -> datetime | None:
    """Coerce a naive datetime to UTC-aware. MongoDB/Beanie occasionally hand
    back naive datetimes for fields stored as aware originally — comparing
    those against `now_utc()` (which is always aware) raises ``TypeError:
    can't compare offset-naive and offset-aware datetimes``. Treat any naive
    expiry as UTC to keep the comparison safe."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# Cross-process failover bridge: the failover engine + WS pool live in the
# FEED process, but the admin API is served by BACKEND workers (different
# process, no shared memory). The feed publishes its live status to this Redis
# key and listens for admin commands on this channel, so the admin panel can
# view live health + trigger off-market tests across processes.
FAILOVER_STATUS_KEY = "zerodha:failover:status"
# When the tick feed last (re)connected — read by the stale-feed order block.
FEED_WARM_KEY = "zerodha:feed_warm_at"
FAILOVER_CMD_CHANNEL = "zerodha:failover:cmd"
# WS pool snapshot the feed publishes so the admin Status panel (backend
# workers) shows the real per-account ticker state + subscribed count.
WS_POOL_KEY = "zerodha:ws_pool"


class ZerodhaService:
    """Kite Connect REST + WebSocket wrapper.

    Multi-WebSocket architecture:
    • Zerodha allows max 3000 tokens per single WebSocket connection.
    • We maintain a POOL of KiteTicker instances (self._tickers).
    • Subscriptions are tracked IN-MEMORY only (self._ws_subscriptions) —
      NOT saved to MongoDB to avoid DB bloat. Instruments are subscribed
      on-demand when users request quotes/charts.
    • When a new token needs subscribing, it's assigned to the least-loaded
      connection. If all connections are at capacity, a new one is spawned.
    """

    # Zerodha WebSocket limits — empirically validated, do not raise:
    #   • 3000 tokens per WebSocket connection (hard ceiling).
    #   • Only ONE active WS per access_token. Kite's docs say
    #     "3 subscribers per API key" but the kiteconnect Python SDK
    #     spawns each KiteTicker on the same Twisted reactor in-process
    #     and Kite treats same-token sockets as duplicates — the second
    #     and third opens fail with mixed symptoms:
    #         1006 SSL error: passed invalid argument
    #         1002 RSV3 set, bad opcode 12
    #         403 Forbidden on the WS upgrade
    #     Production proof (21-May): after MAX_WS_CONNECTIONS was
    #     bumped 1→3 by a recent commit, market data stopped flowing
    #     entirely within ~30s of every boot — every restart logged a
    #     cascade of WS-1/WS-2/WS-3 close-with-1006 events as they
    #     clashed. Rolled back to 1.
    #   • To get >3000 live tokens you need to provision additional
    #     Kite Connect API keys (each with its own access_token) and
    #     shard subscriptions across them. That is the supported path.
    MAX_TOKENS_PER_WS = 3000
    MAX_WS_CONNECTIONS = 1

    # Bad-tick spike filter (see _on_ticks): reject a tick whose LTP jumps more
    # than this fraction from the last-good tick, UNLESS the last-good tick is
    # older than _SPIKE_STALE_SEC (then it's a real move / fresh session, not a
    # glitch). 50% tick-to-tick is impossible for any real instrument — the
    # garbage ticks that phantom-closed positions were 68%–99% jumps.
    # _SPIKE_STALE_SEC is 30 s (not 5): the feed reconnects periodically and
    # ILLIQUID instruments tick sparsely, so a 5 s window let a garbage tick
    # right after a reconnect/gap slip through as "a real move" (XRPUSDT filled
    # a BUY at $26 vs its real $1.14, CL29840025). 30 s keeps the last-good
    # price trusted across a reconnect gap so the junk is rejected; a genuine
    # move that persists past 30 s is still accepted.
    _MAX_TICK_SPIKE_PCT = 0.5
    _SPIKE_STALE_SEC = 30.0

    def __init__(self) -> None:
        # Live tick state (populated by KiteTicker callbacks)
        self.ticks_by_token: dict[int, dict[str, Any]] = {}
        self.ticks_by_symbol: dict[str, dict[str, Any]] = {}

        # Per-exchange instrument cache (CSV is huge: ~1MB+)
        self._instruments_cache: dict[str, list[dict[str, Any]]] = {}
        self._instruments_cache_at: float = 0.0
        # 4 days (not 24h): the catalog is the source for instrument SEARCH +
        # the dashboard Market Overview. Zerodha's instruments endpoint can't
        # be fetched when the market is closed, so a 24h TTL let the Redis
        # catalog EXPIRE over a weekend / long holiday — a Saturday restart
        # then found no catalog and search returned empty ("Market data
        # unavailable", prices gone on tradeox.in). 4 days spans Fri→Mon plus a
        # holiday so Friday's catalog survives the gap. Weekday freshness is
        # still fine — admin "Sync instruments" force-refreshes on demand, and
        # expired F&O rows are dropped by the expiry_cleanup loop regardless.
        self._INSTRUMENTS_TTL_SEC = 4 * 24 * 60 * 60  # 4 days
        # After a FAILED instruments fetch (Kite 429 "Too many requests"), don't
        # re-hit Kite for this long. Without it every caller retried on each
        # failure and kept Kite rate-limiting us — the "No options available"
        # storm that pinned the whole option chain empty across all workers.
        self._instruments_fail_at: dict[str, float] = {}
        # 5 min (not 60s): with N workers each retrying, a short cooldown still
        # sent enough traffic to keep Kite's instruments endpoint permanently
        # 429-ing so it never reset. A long cooldown backs ALL workers off long
        # enough for Kite's limit to clear; the DB fallback serves the chain in
        # the meantime, and once one fetch succeeds it's cached 24h.
        self._INSTRUMENTS_FAIL_COOLDOWN_SEC = 300.0

        # ── Redis-shared instrument catalog (multi-worker) ───────────────
        # The Kite `instruments` dump is per-process in `_instruments_cache`,
        # so with N gunicorn workers each worker fetched NSE+NFO+MCX on boot
        # (and on TTL expiry) INDEPENDENTLY. 4 workers × 3 exchanges = 12
        # concurrent Kite calls → 429 "Too many requests" → the catalog stayed
        # EMPTY on the throttled workers → the option chain served last-close
        # instead of live prices. Fix: ONE worker (whoever wins a short Redis
        # NX lock) actually calls Kite and mirrors the normalised catalog into
        # Redis; every other worker reads it from Redis and NEVER touches Kite.
        # On a single worker this collapses to today's behaviour (it always
        # wins the lock). See fetch_instruments().
        self._REDIS_INSTR_KEY = "zerodha:instruments:{ex}"
        self._REDIS_INSTR_LOCK = "lock:zerodha:instruments:{ex}"
        self._REDIS_INSTR_LOCK_TTL_SEC = 60
        # Per-process single-flight: collapse concurrent callers on the SAME
        # worker so they don't each try to grab the Redis lock / re-fetch.
        self._instr_fetch_locks: dict[str, asyncio.Lock] = {}

        # REST `/quote` snapshot cache (used as a fallback when the WebSocket
        # has no live tick for an instrument — e.g. on weekends / pre-market).
        # Keyed by Kite "EXCH:TRADINGSYMBOL", value = (snapshot_dict, fetched_at).
        self._rest_quote_cache: dict[str, tuple[dict[str, Any], float]] = {}
        self._REST_QUOTE_TTL_SEC = 10.0

        # ─── Multi-WebSocket Pool ───────────────────────────────────
        # Each entry: {"ws": websockets conn, "task": asyncio.Task,
        #              "tokens": set[int], "connected": bool, ...}
        # Raw `websockets` clients (NOT kiteconnect.KiteTicker) so the pool
        # can be torn down + rebuilt any number of times within one process
        # — see _spawn_ws_connection for the Twisted-reactor history.
        self._tickers: list[dict[str, Any]] = []
        self._ticker_lock = threading.Lock()
        # Serialises every connect_ws() entry. Without this, the scheduler's
        # Layer-2/3 paths can race the post-login WS kickoff (both call
        # connect_ws within 1 s of each other), causing _stop_ticker() to
        # kill a ticker that's mid-handshake and producing the perpetual
        # "WS upgrade did not complete" loop.
        self._ws_connect_lock: asyncio.Lock | None = None
        # Tracks back-to-back self-heal failures so we can ramp the retry
        # interval (Kite throttles a key that's hammering the WS endpoint).
        self._ws_consecutive_heal_failures: int = 0

        # Reverse lookup: token → ticker index
        self._token_to_ws: dict[int, int] = {}
        # LRU eviction support — when MAX_TOKENS_PER_WS × MAX_WS_CONNECTIONS
        # cap is hit, the oldest entries here are evicted to make room for
        # new subscriptions. Every subscribe_tokens_on_demand call refreshes
        # the timestamp for every token in the request so an actively-viewed
        # leg keeps moving back to the top. Without this, the pool fills
        # over the day with stale tokens from closed positions / dismissed
        # option-chain dialogs and the option-chain picker silently shows
        # blank prices for any NEW strike beyond the cap — operator-flagged
        # 22-May: "kuch strike ka data nahi aa raha".
        self._token_last_used: dict[int, float] = {}
        # Tokens we MUST keep subscribed regardless of LRU age — admin-pinned
        # instruments, anything with an open position, etc. Populated by
        # callers via mark_token_protected / unmark_token_protected.
        self._token_protected: set[int] = set()

        # Symbol lookup for tick callbacks
        self._symbol_by_token: dict[int, dict[str, str]] = {}

        # ─── Dual-account HA failover state ─────────────────────────────
        # Per-WS-entry last-tick monotonic timestamp, keyed by api_key, so
        # health is TICK-based (a connected-but-silent socket is unhealthy).
        self._last_tick_at_by_api_key: dict[str, float] = {}
        # Caches maintained by feed_failover_loop and read by the SYNC
        # subscribe path (_ws_subscribe cannot await a DB call):
        self._account_api_key: dict[int, str] = {}          # 0=A key, 1=B key
        self._routing_map: dict[str, int] = {}              # exchange -> desired acct
        self._account_healthy_cache: dict[int, bool] = {0: False, 1: False}
        self._effective_route: dict[str, int] = {}          # exchange -> EFFECTIVE acct
        self._failover_enabled_cache: bool = True
        self._health_stale_sec: float = 15.0
        self._failover_confirm_down_sec: float = 5.0
        self._failback_confirm_up_sec: float = 25.0
        # Anti-flap debounce: presence => currently in that state; value = when
        # it started (monotonic). Exactly one of the two holds per account.
        self._account_up_since: dict[int, float] = {}
        self._account_down_since: dict[int, float] = {}
        self._failover_running: bool = False

        # Legacy compat
        self._ticker: Any = None

        # Loop reference for cross-thread Redis publishes
        self._main_loop: asyncio.AbstractEventLoop | None = None

    # ── Settings helpers ─────────────────────────────────────────────
    async def _get_settings(self, account_index: int = 0) -> ZerodhaSettings:
        s = await ZerodhaSettings.find_one(
            ZerodhaSettings.account_index == account_index
        )
        if s is None and account_index == 0:
            # Legacy documents (before dual-account) have no account_index field.
            # Find the first document without the filter and stamp it as account 0.
            s = await ZerodhaSettings.find_one()
            if s is not None:
                s.account_index = 0
                await s.save()
        if s is None:
            s = ZerodhaSettings(account_index=account_index)
            await s.insert()
        # The model's redirectUrl default is a hardcoded localhost URL, so every
        # settings document created on a real deployment stored it — the admin
        # page then told the operator to paste localhost into their Kite app,
        # and Kite would send the browser there after login. Repair that exact
        # legacy value to the canonical callback derived from BACKEND_PUBLIC_URL.
        # A URL an admin deliberately set to something else is left alone, and
        # local dev (where the two are identical) is unaffected.
        from app.core.config import settings as _cfg

        legacy = "http://localhost:8000/api/v1/admin/zerodha/callback"
        canonical = _cfg.zerodha_redirect_url
        if s.redirectUrl == legacy and canonical != legacy:
            s.redirectUrl = canonical
            await s.save()
        return s

    async def _pool_info_xproc(self) -> dict[str, Any]:
        """WS pool info that works across processes. Use the in-process pool if
        this worker owns one (the feed leader); otherwise read the snapshot the
        feed publishes to Redis — backend workers have no pool of their own, so
        without this the admin Status panel always showed DISCONNECTED / 0."""
        pool = self.get_ws_pool_info()
        if pool.get("total_connections", 0) > 0:
            return pool
        try:
            from app.core.redis_client import cache_get

            rp = await cache_get(WS_POOL_KEY)
            if isinstance(rp, dict) and rp.get("connections") is not None:
                return rp
        except Exception:
            pass
        return pool

    def _pool_entry_for(self, pool: dict[str, Any], api_key: str) -> dict[str, Any] | None:
        if not api_key:
            return None
        return next(
            (c for c in pool.get("connections", []) if c.get("api_key") == api_key),
            None,
        )

    async def get_status(self, account_index: int = 0) -> dict[str, Any]:
        s = await self._get_settings(account_index)
        pool = await self._pool_info_xproc()
        # Derive THIS account's live ticker state from the pool by api_key. The
        # pool comes from the FEED process (via Redis), so the admin panel
        # (backend workers) reflects the real socket + this account's own
        # subscribed count (e.g. MCX on Account B) — not a stale/empty pool.
        entry = self._pool_entry_for(pool, s.apiKey)
        connected = bool(entry and entry.get("connected"))
        acct_tokens = int(entry.get("tokens_count", 0)) if entry else 0
        ws_status_str = WsStatus.CONNECTED.value if connected else WsStatus.DISCONNECTED.value
        return {
            "isConfigured": bool(s.apiKey and s.apiSecret),
            "isConnected": connected or s.isConnected,
            "wsStatus": ws_status_str,
            "wsLastError": (entry or {}).get("last_close_reason", "") or s.wsLastError,
            "lastConnected": s.lastConnected,
            "tokenExpiry": s.tokenExpiry,
            "subscribedCount": acct_tokens,
            "dbSubscribedCount": len(s.subscribedInstruments),
            "wsConnections": pool.get("total_connections", 0),
            "wsPool": pool,
            "enabledSegments": s.enabledSegments.model_dump(),
            "redirectUrl": s.redirectUrl,
        }

    async def get_settings_full(self, account_index: int = 0) -> dict[str, Any]:
        from app.core.config import settings as app_settings

        s = await self._get_settings(account_index)
        token_expiry = _ensure_aware_utc(s.tokenExpiry)
        is_token_expired = bool(token_expiry and now_utc() >= token_expiry)
        default_redirect = app_settings.zerodha_redirect_url
        # Derive live ticker state from the cross-process pool (feed → Redis),
        # by this account's api_key — so BOTH accounts show the real socket
        # state on the admin panel served by backend workers.
        pool = await self._pool_info_xproc()
        entry = self._pool_entry_for(pool, s.apiKey)
        live = bool(entry and entry.get("connected"))
        ws_status_str = WsStatus.CONNECTED.value if live else WsStatus.DISCONNECTED.value
        ws_last_error = (entry or {}).get("last_close_reason", "") or s.wsLastError

        return {
            "apiKey": s.apiKey,
            "apiSecret": "***" if s.apiSecret else "",
            "apiSecretConfigured": bool(s.apiSecret),
            "isConnected": s.isConnected,
            "isTokenExpired": is_token_expired,
            "lastConnected": s.lastConnected,
            "tokenExpiry": s.tokenExpiry,
            "wsStatus": ws_status_str,
            "wsLastError": ws_last_error,
            "enabledSegments": s.enabledSegments.model_dump(),
            "subscribedInstruments": [i.model_dump() for i in s.subscribedInstruments],
            "redirectUrl": s.redirectUrl,
            # Canonical backend callback — admin UI uses this to detect/repair
            # mismatched configurations (e.g. someone pasting the frontend URL).
            "defaultRedirectUrl": default_redirect,
            "redirectUrlMismatch": s.redirectUrl != default_redirect,
        }

    async def update_settings(self, payload: dict[str, Any], account_index: int = 0) -> ZerodhaSettings:
        s = await self._get_settings(account_index)
        if "apiKey" in payload:
            s.apiKey = (payload["apiKey"] or "").strip()
        if "apiSecret" in payload and payload["apiSecret"]:
            # Don't overwrite when the UI sends the masked placeholder
            if payload["apiSecret"] != "***":
                s.apiSecret = payload["apiSecret"].strip()
        if "redirectUrl" in payload and payload["redirectUrl"]:
            url = str(payload["redirectUrl"]).strip()
            self._validate_redirect_url(url)
            s.redirectUrl = url
        if "enabledSegments" in payload and isinstance(payload["enabledSegments"], dict):
            for k, v in payload["enabledSegments"].items():
                if hasattr(s.enabledSegments, k):
                    setattr(s.enabledSegments, k, bool(v))
        await s.save()
        return s

    @staticmethod
    def _validate_redirect_url(url: str) -> None:
        """Sanity-check the redirect URL. Both frontends ship a `/api/v1/admin
        /zerodha/callback` proxy that forwards to the backend, so any of
        backend (8000), user-frontend (3000), or admin-frontend (3001) hosts
        are acceptable — but the path must be the right one."""
        from urllib.parse import urlparse

        try:
            parsed = urlparse(url)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Invalid redirect URL: {e}") from e
        if parsed.scheme not in ("http", "https"):
            raise RuntimeError("Redirect URL must start with http:// or https://")
        if not parsed.netloc:
            raise RuntimeError("Redirect URL is missing a host")
        if not parsed.path.endswith("/admin/zerodha/callback"):
            raise RuntimeError(
                "Redirect URL must end with /admin/zerodha/callback — that's the route Kite hits."
            )

    # ── KiteConnect REST client (lazy, recreated each call) ─────────
    def _kite(self, api_key: str, access_token: str | None = None):
        from kiteconnect import KiteConnect  # imported lazily to keep startup fast

        kc = KiteConnect(api_key=api_key)
        if access_token:
            kc.set_access_token(access_token)
        return kc

    async def _kite_with_token(self):
        s = await self._get_settings()
        if not s.apiKey or not s.accessToken:
            raise RuntimeError("Zerodha is not authenticated. Connect from admin panel.")
        expiry = _ensure_aware_utc(s.tokenExpiry)
        if expiry and now_utc() >= expiry:
            s.isConnected = False
            s.wsStatus = WsStatus.DISCONNECTED
            await s.save()
            raise RuntimeError("Zerodha token has expired (07:00 IST daily). Re-authenticate.")
        return self._kite(s.apiKey, s.accessToken), s

    async def probe_and_clear_invalid_token(self) -> bool:
        """REST-probe the stored access token via ``kite.profile()``.

        Returns True if the token is alive. Returns False (and CLEARS the
        token from DB so self-heal / WS-connect stop hammering it) when
        Kite responds with TokenException / 403 / 401.

        Why this exists
        ---------------
        Operator reported a recurring issue: "key DB me save rehta hai
        backend restart se hi hatti hai". Kite can invalidate a token
        BEFORE its nominal 08:00 IST expiry (e.g. login from another
        device, Kite-side rotation, IP block). Our `tokenExpiry` says
        future, but every WS upgrade now returns 403. The self-heal
        loop then loops endlessly with the dead token, holding the
        Kite-side WS slot warm and preventing fresh logins from
        succeeding cleanly.

        With this probe + auto-clear, the moment Kite says "this token
        is dead" we wipe accessToken from DB. The next loop iteration
        sees `not s.accessToken`, skips, and the auto-login scheduler
        (or admin click) gets a clean slate. No restart needed.
        """
        s = await self._get_settings()
        if not s.apiKey or not s.accessToken:
            return False
        try:
            kc = self._kite(s.apiKey, s.accessToken)
            await asyncio.to_thread(kc.profile)
            return True
        except Exception as exc:
            msg = str(exc).lower()
            # Kite's TokenException / "incorrect api_key or access_token" /
            # bare 403 / 401 all mean: this token will never work again.
            # Anything else (network blip, 5xx) we DO NOT clear — those
            # are transient and clearing would force an unnecessary
            # re-login.
            looks_invalid = (
                "tokenexception" in msg
                or "token" in msg and "expired" in msg
                or "incorrect" in msg and ("token" in msg or "api_key" in msg)
                or "403" in msg
                or "401" in msg
                or "invalid" in msg and "token" in msg
            )
            if not looks_invalid:
                logger.warning(
                    "zerodha_token_probe_transient_error",
                    extra={"error": str(exc)[:200]},
                )
                return True  # assume alive — don't punish transient errors
            logger.warning(
                "zerodha_token_probe_invalid_clearing_db",
                extra={"error": str(exc)[:200]},
            )
            try:
                s.accessToken = None
                s.refreshToken = None
                s.tokenExpiry = None
                s.isConnected = False
                s.wsStatus = WsStatus.DISCONNECTED
                s.wsLastError = "Token invalidated by Kite — cleared, awaiting re-login."
                await s.save()
            except Exception:
                logger.exception("zerodha_token_probe_db_clear_failed")
            # Also drop any in-memory tickers that were trying to use it.
            self._stop_ticker()
            return False

    # ── OAuth login flow ─────────────────────────────────────────────
    async def get_login_url(self, account_index: int = 0) -> str:
        s = await self._get_settings(account_index)
        if not s.apiKey:
            label = "Account B" if account_index == 1 else "primary account"
            raise RuntimeError(f"Zerodha API key not configured for {label}")
        # Both accounts share one redirect URL, so Kite must echo the account
        # back — otherwise /callback defaults to account 0 and exchanges
        # Account B's request_token with Account A's secret.
        return (
            f"https://kite.zerodha.com/connect/login?v=3&api_key={s.apiKey}"
            f"&redirect_params=account%3D{account_index}"
        )

    async def generate_session(self, request_token: str, account_index: int = 0) -> dict[str, Any]:
        """Exchange request_token for access_token (called by /callback)."""
        s = await self._get_settings(account_index)
        if not s.apiKey or not s.apiSecret:
            raise RuntimeError("API credentials are not configured")

        kc = self._kite(s.apiKey)
        try:
            data = await asyncio.to_thread(kc.generate_session, request_token, s.apiSecret)
        except Exception as e:
            raise RuntimeError(f"Kite session generation failed: {e}") from e

        access = data.get("access_token") if isinstance(data, dict) else None
        refresh = data.get("refresh_token") if isinstance(data, dict) else None
        if not access:
            raise RuntimeError("Kite did not return an access_token")

        s.accessToken = access
        s.refreshToken = refresh
        s.tokenExpiry = _next_kite_expiry_utc()
        s.isConnected = True
        s.lastConnected = now_utc()
        await s.save()

        # Pre-warm the instrument cache so on-demand search/subscribe is instant.
        # No bulk DB save — instruments are subscribed on-demand via WebSocket
        # when users open charts or search.
        try:
            for ex in ("NSE", "NFO", "MCX"):
                instruments = await self.fetch_instruments(ex)
                logger.info("zerodha_cache_warmed", extra={"exchange": ex, "count": len(instruments)})
        except Exception:
            logger.exception("zerodha_cache_warm_failed")

        # Start the WebSocket pool via the retry-aware path. Previously
        # we called `_start_ws_pool()` directly — a single shot that
        # silently failed when Kite's gateway held the previous slot for
        # 30-90 s (typical after a deploy or daily token rotation). The
        # admin then saw "Ticker: connecting" forever and had to click
        # the manual "Start ticker" button. `connect_ws(force=True)`
        # gives us the 5-attempt back-off ladder so the post-login WS
        # connect succeeds on its own. Fire-and-forget so the OAuth
        # callback returns instantly even when the WS connect needs the
        # full ~3 minute back-off (it converges in the background, and
        # the self-heal loop below keeps trying after that).
        #
        # The WS pool lives only on the feed leader. /callback lands on
        # whichever HTTP worker nginx picked, and a socket opened there has no
        # self-heal and double-publishes ticks — so a non-leader hands the
        # reconnect to the leader's failover command listener instead.
        from app.services import market_data_service as _mds

        if _mds.is_feed_leader():
            self._kick_ws_after_login(account_index, s.apiKey, access)
        else:
            try:
                await publish(
                    FAILOVER_CMD_CHANNEL,
                    {"action": "reconnect", "account": account_index},
                )
            except Exception:
                logger.exception("zerodha_post_login_reconnect_publish_failed")

        return {"accessToken": access, "tokenExpiry": s.tokenExpiry}

    def _kick_ws_after_login(self, account_index: int, api_key: str, access_token: str) -> None:
        """Fire-and-forget WS (re)connect for the account that just logged in."""
        try:
            if account_index == 0:
                asyncio.create_task(self._login_auto_connect())
            else:
                # Account B gets its own slot — _login_auto_connect() only
                # rebuilds Account A's pool.
                asyncio.create_task(self._account_b_ws_connect(api_key, access_token))
        except Exception:
            logger.exception("zerodha_post_login_ws_kickoff_failed")

    async def _login_auto_connect(self) -> None:
        """Kicks off the retry-aware WS connect after a fresh login. Runs
        in the background so the OAuth callback doesn't block.

        Resets the self-heal failure counter to 0 BEFORE attempting the
        connect — a fresh manual login is a clean slate, the previous
        token's failure history is irrelevant. If we didn't reset here,
        the post-login self-heal cycle would still respect the carried-
        over counter and sleep up to 5 min before its first attempt,
        which is what made the operator restart the backend every
        morning for 15 days running.
        """
        # Fresh slate: zero the counter and un-pause + nudge the
        # self-heal loop so any current 5-min sleep aborts immediately.
        self._ws_consecutive_heal_failures = 0
        self._self_heal_paused = False
        self._wake_self_heal()
        try:
            await self.connect_ws(force=True)
        except Exception:
            logger.warning(
                "zerodha_post_login_ws_connect_giving_up_to_heal_loop",
                exc_info=True,
            )
            # Already un-paused + reset above. Self-heal will retry on
            # its base 30 s cadence now, not the carried-over backoff.

    async def _account_b_ws_connect(self, api_key: str, access_token: str) -> None:
        """Spawn (or reconnect) Account B's dedicated WebSocket slot.

        Called fire-and-forget after a fresh Account B login. Any stale
        connection for the same api_key is cancelled first so we don't
        accumulate duplicate slots in the pool.
        """
        stale_tasks: list[asyncio.Task] = []
        with self._ticker_lock:
            keep, drop = [], []
            for e in self._tickers:
                (drop if e.get("api_key") == api_key else keep).append(e)
            self._tickers[:] = keep
            stale_tasks = [e["task"] for e in drop if e.get("task") and not e["task"].done()]
        for t in stale_tasks:
            t.cancel()
        try:
            await self._spawn_ws_connection(api_key, access_token)
            logger.info("zerodha_account_b_ws_spawned")
        except Exception:
            logger.exception("zerodha_account_b_ws_spawn_failed")

    async def _auto_load_default_subscriptions(self) -> int:
        """Resolve the curated default set against the live Zerodha instruments
        CSV and bulk-subscribe in one call. Fetches NSE/BSE/NFO catalogs
        directly from Kite API so the local DB can be empty. Returns count added."""
        from app.seed.zerodha_defaults import build_default_subscriptions

        defaults = await build_default_subscriptions(fetcher=self.fetch_instruments)
        if not defaults:
            return 0
        return await self.add_subscriptions_bulk(defaults)

    async def subscribe_all_instruments(
        self, exchanges: list[str] | None = None
    ) -> dict[str, Any]:
        """Fetch ALL instruments from Zerodha for specified exchanges and
        subscribe them via the multi-WebSocket pool. IN-MEMORY ONLY —
        nothing is saved to MongoDB.

        Multiple WS connections are spawned automatically (3000 tokens each).
        Default exchanges: NSE, NFO, BSE, MCX, BFO."""
        if exchanges is None:
            exchanges = ["NSE", "NFO", "BSE", "MCX", "BFO"]

        all_tokens: list[int] = []
        sym_map: dict[int, dict[str, str]] = {}
        per_exchange: dict[str, int] = {}

        for exchange in exchanges:
            try:
                instruments = await self.fetch_instruments(exchange)
                count = 0
                for inst in instruments:
                    token = int(inst.get("token") or 0)
                    if not token or token in sym_map:
                        continue
                    all_tokens.append(token)
                    sym_map[token] = {
                        "symbol": inst.get("symbol") or "",
                        "exchange": inst.get("exchange") or exchange,
                    }
                    count += 1
                per_exchange[exchange] = count
                logger.info(
                    "zerodha_subscribe_all_fetched",
                    extra={"exchange": exchange, "count": count},
                )
            except Exception:
                logger.exception("zerodha_subscribe_all_fetch_failed", extra={"exchange": exchange})
                per_exchange[exchange] = 0

        if not all_tokens:
            return {"total": 0, "added": 0, "per_exchange": per_exchange, "connections": 0}

        # Ensure WS pool is started
        s = await self._get_settings()
        if s.apiKey and s.accessToken:
            with self._ticker_lock:
                if not self._tickers:
                    await self._start_ws_pool()

        # Subscribe via multi-WS pool (spawns new connections as needed)
        added = await self.subscribe_tokens_on_demand(all_tokens, sym_map)

        pool_info = self.get_ws_pool_info()
        logger.info(
            "zerodha_subscribe_all_done",
            extra={
                "fetched": len(all_tokens),
                "added": added,
                "total_ws_subscribed": pool_info["total_tokens_subscribed"],
                "connections": pool_info["total_connections"],
            },
        )

        return {
            "fetched": len(all_tokens),
            "added": added,
            "total_ws_subscribed": pool_info["total_tokens_subscribed"],
            "connections": pool_info["total_connections"],
            "per_exchange": per_exchange,
        }

    async def disconnect(self, account_index: int = 0) -> None:
        s = await self._get_settings(account_index)
        s.accessToken = None
        s.refreshToken = None
        s.tokenExpiry = None
        s.isConnected = False
        s.wsStatus = WsStatus.DISCONNECTED
        await s.save()
        self._stop_ticker()

    # ── Instruments ─────────────────────────────────────────────────
    def _instr_lock(self, cache_key: str) -> asyncio.Lock:
        """Lazily-created per-(worker, exchange) single-flight lock so two
        concurrent callers on the SAME worker don't both try to fetch."""
        lk = self._instr_fetch_locks.get(cache_key)
        if lk is None:
            lk = asyncio.Lock()
            self._instr_fetch_locks[cache_key] = lk
        return lk

    def _kite_meta_for(self, token: int) -> dict[str, Any] | None:
        """Catalog row (symbol / exchange / lot / tick) for a Kite token.

        Indexed lazily off `_instruments_cache` and rebuilt whenever that cache
        is replaced, so an 80k-row scan happens once per catalog refresh rather
        than per subscribe.
        """
        try:
            idx = getattr(self, "_kite_token_idx", None)
            if idx is None or getattr(self, "_kite_token_idx_at", None) != self._instruments_cache_at:
                idx = {}
                for rows in self._instruments_cache.values():
                    for r in rows:
                        tok = r.get("token")
                        if tok:
                            idx[int(tok)] = r
                self._kite_token_idx = idx
                self._kite_token_idx_at = self._instruments_cache_at
            return idx.get(int(token))
        except Exception:  # pragma: no cover - lookup must never block a subscribe
            return None

    def _apply_instruments_cache(self, cache_key: str, catalog: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Populate the in-process L1 cache and return the catalog."""
        import time as _time

        self._instruments_cache[cache_key] = catalog
        self._instruments_cache_at = _time.time()
        return catalog

    async def fetch_instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        """Pulls and caches the instruments CSV for a given exchange (NSE / BSE / NFO / MCX / BFO).

        Multi-worker safe. The Kite `instruments` dump is fetched by exactly
        ONE worker across the cluster (whoever wins a short Redis NX lock) and
        mirrored into Redis; every other worker reads the catalog from Redis
        and never touches Kite. Without this, N gunicorn workers each fetched
        NSE+NFO+MCX independently on boot → Kite 429 "Too many requests" → the
        catalog stayed empty and the option chain fell back to last-close. On a
        single worker this is byte-for-byte the old path (it always wins).
        """
        cache_key = exchange or "ALL"
        import time as _time

        # ── L1: in-process cache (fast path, no I/O) ─────────────────────
        if (
            cache_key in self._instruments_cache
            and (_time.time() - self._instruments_cache_at) < self._INSTRUMENTS_TTL_SEC
        ):
            return self._instruments_cache[cache_key]

        redis_key = self._REDIS_INSTR_KEY.format(ex=cache_key)

        # ── L2: Redis shared cache — another worker already fetched it ───
        # Checked BEFORE the fail-cooldown so a worker that never fetched
        # itself still serves the catalog a sibling put in Redis.
        try:
            shared = await cache_get(redis_key)
        except Exception:
            shared = None
        if shared:
            return self._apply_instruments_cache(cache_key, shared)

        # Cluster-wide "Kite can't serve this right now" marker. The local
        # cooldown below only protects the worker that failed: every OTHER
        # worker still lost the Redis lock and polled for 5 s per exchange.
        # User search walks NSE, NFO and MCX in turn, so on those workers each
        # search took 15 s — on every call, for as long as Zerodha had no token,
        # which on a deployment that never connected it meant forever. Checked
        # before the lock so every worker stands down together.
        unavailable_key = f"zerodha:instruments:unavailable:{cache_key}"

        # Cooldown after a recent LOCAL failure — serve last-known (maybe empty).
        _now = _time.time()
        if _now - self._instruments_fail_at.get(cache_key, 0.0) < self._INSTRUMENTS_FAIL_COOLDOWN_SEC:
            return self._instruments_cache.get(cache_key, [])
        try:
            if await cache_get(unavailable_key):
                return self._instruments_cache.get(cache_key, [])
        except Exception:
            pass

        # ── Elect a single fetcher: per-process lock, then cross-worker ──
        async with self._instr_lock(cache_key):
            # Re-check L1/L2 — a concurrent caller (this worker or a sibling)
            # may have populated them while we waited on the lock.
            if (
                cache_key in self._instruments_cache
                and (_time.time() - self._instruments_cache_at) < self._INSTRUMENTS_TTL_SEC
            ):
                return self._instruments_cache[cache_key]
            try:
                shared = await cache_get(redis_key)
            except Exception:
                shared = None
            if shared:
                return self._apply_instruments_cache(cache_key, shared)

            try:
                got_lock = await idempotency_check_and_set(
                    self._REDIS_INSTR_LOCK.format(ex=cache_key),
                    ttl_sec=self._REDIS_INSTR_LOCK_TTL_SEC,
                )
            except Exception:
                # Redis down → behave like a single worker (fetch directly).
                got_lock = True

            if not got_lock:
                # A sibling worker is fetching from Kite right now. Poll Redis
                # for its result instead of hitting Kite ourselves — that is
                # exactly the 429 storm this method removes.
                for _ in range(10):  # ~5 s
                    await asyncio.sleep(0.5)
                    try:
                        shared = await cache_get(redis_key)
                        down = await cache_get(unavailable_key)
                    except Exception:
                        shared, down = None, None
                    if shared:
                        return self._apply_instruments_cache(cache_key, shared)
                    if down:
                        # The lock holder just found Kite unreachable — no
                        # catalog is coming, so stop waiting for one.
                        return self._instruments_cache.get(cache_key, [])
                # Still nothing — serve stale/empty. The option chain has a DB
                # fallback and the next poll reads Redis once it's populated.
                return self._instruments_cache.get(cache_key, [])

            # We hold the cluster-wide lock — do the real Kite fetch.
            try:
                # Inside the try on purpose: with no token (never connected, or
                # expired) this raises, and outside the try that skipped the
                # cooldown entirely, so the very next caller went round again.
                kc, s = await self._kite_with_token()
                data = await asyncio.to_thread(kc.instruments, exchange) if exchange else await asyncio.to_thread(kc.instruments)
            except Exception as e:
                # Start the cooldown so we don't hammer Kite through the 429 window.
                self._instruments_fail_at[cache_key] = _now
                try:
                    await cache_set(
                        unavailable_key, 1, ttl_sec=int(self._INSTRUMENTS_FAIL_COOLDOWN_SEC)
                    )
                except Exception:
                    pass
                raise RuntimeError(f"Kite instruments fetch failed: {e}") from e

            # SDK already returns parsed dicts. Normalise field names so the rest
            # of the code can treat Zerodha and our domain identically.
            normalised: list[dict[str, Any]] = []
            for it in data:
                normalised.append(
                    {
                        "token": int(it.get("instrument_token") or 0),
                        "symbol": (it.get("tradingsymbol") or "").strip(),
                        "exchange": (it.get("exchange") or "").strip(),
                        "segment": (it.get("segment") or "").strip(),
                        "name": (it.get("name") or "").strip(),
                        "lotSize": int(it.get("lot_size") or 1),
                        "tickSize": float(it.get("tick_size") or 0.05),
                        "expiry": it.get("expiry").isoformat() if it.get("expiry") else None,
                        "strike": float(it.get("strike")) if it.get("strike") not in (None, "") else None,
                        "instrumentType": (it.get("instrument_type") or "").strip(),
                    }
                )
            self._apply_instruments_cache(cache_key, normalised)
            # Mirror to Redis so sibling workers read it WITHOUT touching Kite.
            try:
                await cache_set(redis_key, normalised, ttl_sec=int(self._INSTRUMENTS_TTL_SEC))
            except Exception:
                logger.warning(
                    "zerodha_instruments_redis_mirror_failed",
                    extra={"exchange": cache_key},
                )
            s.instrumentsLastFetched = now_utc()
            await s.save()
            logger.info("zerodha_instruments_fetched", extra={"exchange": cache_key, "count": len(normalised)})
            return normalised

    @staticmethod
    def _segment_to_exchange(segment: str | None) -> str | None:
        return {
            "nseEq": "NSE",
            "bseEq": "BSE",
            "nseFut": "NFO",
            "nseOpt": "NFO",
            "mcxFut": "MCX",
            "mcxOpt": "MCX",
            "bseFut": "BFO",
            "bseOpt": "BFO",
        }.get(segment or "")

    @staticmethod
    def _matches_segment(inst: dict[str, Any], segment: str | None) -> bool:
        if not segment:
            return True
        seg = inst.get("segment") or ""
        ex = inst.get("exchange") or ""
        it = inst.get("instrumentType") or ""
        if segment == "nseEq":
            return seg == "NSE" and (it == "EQ" or not it)
        if segment == "bseEq":
            return seg in ("BSE", "BSE-EQ") and (it == "EQ" or not it)
        if segment == "nseFut":
            return seg == "NFO-FUT" or (ex == "NFO" and it == "FUT")
        if segment == "nseOpt":
            return seg == "NFO-OPT" or (ex == "NFO" and it in ("CE", "PE"))
        if segment == "mcxFut":
            return seg in ("MCX-FUT", "MCX") and it != "OPT" and it not in ("CE", "PE")
        if segment == "mcxOpt":
            return seg == "MCX-OPT" or (ex == "MCX" and it in ("CE", "PE"))
        if segment == "bseFut":
            return seg == "BFO-FUT" or (ex == "BFO" and it == "FUT")
        if segment == "bseOpt":
            return seg == "BFO-OPT" or (ex == "BFO" and it in ("CE", "PE"))
        return True

    async def search_instruments(
        self, query: str, segment: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if not query or len(query.strip()) < 2:
            return []
        ex = self._segment_to_exchange(segment)
        try:
            instruments = await self.fetch_instruments(ex) if ex else await self.fetch_instruments()
        except Exception as e:
            logger.warning("zerodha_search_fallback_to_subscribed", extra={"error": str(e)})
            s = await self._get_settings()
            instruments = [i.model_dump() for i in s.subscribedInstruments]

        q = query.strip().lower()
        results = [
            i
            for i in instruments
            if (q in (i.get("symbol") or "").lower() or q in (i.get("name") or "").lower())
            and self._matches_segment(i, segment)
        ]

        # Filter past expiries (IST date)
        today_ist = datetime.now(IST).date()
        results = [
            i
            for i in results
            if not i.get("expiry") or datetime.fromisoformat(i["expiry"]).date() >= today_ist
        ]
        return results[:limit]

    # ── Subscriptions ───────────────────────────────────────────────
    async def add_subscription(self, instrument: dict[str, Any]) -> bool:
        s = await self._get_settings()
        token = int(instrument.get("token") or 0)
        if not token:
            raise ValueError("instrument.token is required")
        if any(i.token == token for i in s.subscribedInstruments):
            return False
        sub = SubscribedInstrument(**instrument)
        s.subscribedInstruments.append(sub)
        await s.save()

        # Mirror into the local Instrument collection so user search / quote /
        # history hooks light up automatically (they all key off Instrument.token).
        try:
            await self._mirror_subscription_to_instrument(sub)
        except Exception:  # noqa: BLE001
            logger.exception("zerodha_mirror_failed", extra={"token": sub.token})

        # Subscribe on the live ticker (or start it if it isn't running yet —
        # admin shouldn't have to click "Start ticker" before data flows).
        try:
            self._ws_subscribe([token])
        except Exception:
            pass
        await self._ensure_ticker_running()
        return True

    async def add_subscriptions_bulk(self, instruments: list[dict[str, Any]]) -> int:
        s = await self._get_settings()
        existing = {i.token for i in s.subscribedInstruments}
        added_subs: list[SubscribedInstrument] = []
        new_tokens: list[int] = []
        for inst in instruments:
            token = int(inst.get("token") or 0)
            if not token or token in existing:
                continue
            sub = SubscribedInstrument(**inst)
            s.subscribedInstruments.append(sub)
            added_subs.append(sub)
            existing.add(token)
            new_tokens.append(token)
        if added_subs:
            await s.save()
            for sub in added_subs:
                try:
                    await self._mirror_subscription_to_instrument(sub)
                except Exception:  # noqa: BLE001
                    logger.exception("zerodha_mirror_failed", extra={"token": sub.token})
            try:
                self._ws_subscribe(new_tokens)
            except Exception:
                pass
            await self._ensure_ticker_running()
        return len(added_subs)

    # ── Mirror Zerodha subscription → local Instrument ──────────────
    async def _mirror_subscription_to_instrument(self, sub: SubscribedInstrument) -> None:
        """Upsert each Zerodha-subscribed instrument into the local
        ``Instrument`` collection so user-side search, quotes, history and
        positions all flow through normal code paths. The Zerodha
        ``instrument_token`` becomes the local ``Instrument.token`` so the
        market-data overlay can look up live ticks by token directly."""
        from datetime import datetime as _dt

        from bson import Decimal128

        from app.models._base import Exchange, InstrumentType, OptionType
        from app.models.instrument import Instrument

        # Exchange — fall back to NSE if Zerodha sends a value we don't model
        try:
            exchange = Exchange((sub.exchange or "").upper())
        except ValueError:
            return  # silently skip unsupported exchange

        # Instrument type. Zerodha subscribe payloads sometimes omit
        # `instrumentType` (or default it to "EQ") for derivative contracts,
        # leaving rows stored as EQ even though the tradingsymbol clearly
        # says FUT / <strike>CE / <strike>PE. When that happens the canonical
        # lot lookup is skipped and the order panel renders "1 lot = 1
        # units" for things like GOLD26JUNFUT. Fall back to symbol-suffix
        # inference whenever the explicit type is missing or contradicts
        # the symbol.
        from app.services.instrument_service import infer_instrument_type_from_symbol

        it_raw = (sub.instrumentType or "").upper()
        inferred = infer_instrument_type_from_symbol(sub.symbol)
        if it_raw not in ("EQ", "FUT", "CE", "PE", "INDEX"):
            it_raw = inferred or "EQ"
        elif it_raw == "EQ" and inferred:
            it_raw = inferred
        try:
            instrument_type = InstrumentType(it_raw)
        except ValueError:
            instrument_type = InstrumentType.EQ

        # Segment — best-effort string for downstream segment-aware logic
        if instrument_type == InstrumentType.EQ:
            segment = f"{exchange.value}_EQUITY"
        elif instrument_type == InstrumentType.FUT:
            segment = f"{exchange.value}_FUTURE"
        elif instrument_type in (InstrumentType.CE, InstrumentType.PE):
            # Option segment: NFO_OPT, BFO_OPT, MCX_OPT
            segment = f"{exchange.value}_OPTION"
        else:
            segment = f"{exchange.value}_EQUITY"

        # Expiry / strike / option_type
        expiry_date = None
        if sub.expiry:
            try:
                expiry_date = _dt.fromisoformat(sub.expiry).date()
            except Exception:
                expiry_date = None

        option_type: OptionType | None = None
        if instrument_type == InstrumentType.CE:
            option_type = OptionType.CE
        elif instrument_type == InstrumentType.PE:
            option_type = OptionType.PE

        token_str = str(sub.token)
        existing = await Instrument.find_one(Instrument.token == token_str)

        tick_size_d = Decimal128(str(sub.tickSize or "0.05"))
        strike_d = Decimal128(str(sub.strike)) if sub.strike else None

        # Resolve lot size: canonical table wins for FUT/CE/PE so MCX rows
        # don't end up at 1 from an empty `sub.lotSize`, and stale Zerodha
        # values for fresh NIFTY/BANKNIFTY contracts get healed.
        from app.services.index_lots import get_canonical_lot_size

        canonical_lot = (
            get_canonical_lot_size(sub.symbol, sub.name, exchange=exchange.value)
            if instrument_type in (InstrumentType.CE, InstrumentType.PE, InstrumentType.FUT)
            else None
        )
        resolved_lot = canonical_lot or (sub.lotSize if sub.lotSize else None) or 1

        # Friendly display name for derivatives — same composition rule as
        # the auto-create path. Stored on the row so search results / order
        # panel headers don't show the bare underlying.
        from app.services.instrument_service import display_name as _display_name

        friendly_name = _display_name(
            instrument_type=instrument_type,
            underlying=sub.name or sub.symbol,
            expiry=expiry_date,
            strike=sub.strike,
        )

        if existing is None:
            inst = Instrument(
                token=token_str,
                symbol=sub.symbol,
                trading_symbol=sub.symbol,
                name=friendly_name,
                exchange=exchange,
                segment=segment,
                instrument_type=instrument_type,
                lot_size=resolved_lot,
                tick_size=tick_size_d,
                expiry=expiry_date,
                strike=strike_d,
                option_type=option_type,
                is_active=True,
                is_tradable=True,
            )
            try:
                await inst.insert()
            except Exception as _dup_exc:
                if "E11000" in str(_dup_exc):
                    # Concurrent insert won the race — row exists, next
                    # subscribe cycle will update it via the `else` branch.
                    pass
                else:
                    raise
        else:
            existing.symbol = sub.symbol
            existing.trading_symbol = sub.symbol
            existing.name = friendly_name
            existing.exchange = exchange
            existing.segment = segment
            existing.instrument_type = instrument_type
            existing.lot_size = resolved_lot
            existing.tick_size = tick_size_d
            if expiry_date is not None:
                existing.expiry = expiry_date
            if strike_d is not None:
                existing.strike = strike_d
            existing.option_type = option_type
            existing.is_active = True
            existing.is_tradable = True
            await existing.save()

    async def _ensure_ticker_running(self) -> None:
        """Start the KiteTicker pool if no connections are alive; safe no-op otherwise."""
        with self._ticker_lock:
            if any(e.get("connected") for e in self._tickers):
                return
        try:
            await self.connect_ws()
        except Exception:  # noqa: BLE001
            logger.exception("zerodha_auto_start_ticker_failed")

    async def backfill_local_instruments(self) -> int:
        """Idempotent: mirror every existing Zerodha-subscribed instrument into
        the local ``Instrument`` collection. Run on app startup so subscriptions
        made before the mirror feature existed (or in a previous deploy) start
        flowing live data immediately, without admin having to re-subscribe."""
        s = await self._get_settings()
        if not s.subscribedInstruments:
            return 0
        mirrored = 0
        for sub in s.subscribedInstruments:
            try:
                await self._mirror_subscription_to_instrument(sub)
                mirrored += 1
            except Exception:  # noqa: BLE001
                logger.exception("zerodha_backfill_failed", extra={"token": sub.token})
        if mirrored:
            logger.info("zerodha_backfill_done", extra={"count": mirrored})
        return mirrored

    async def remove_subscription(self, token: int) -> bool:
        s = await self._get_settings()
        before = len(s.subscribedInstruments)
        s.subscribedInstruments = [i for i in s.subscribedInstruments if i.token != token]
        if len(s.subscribedInstruments) == before:
            return False
        await s.save()
        try:
            self._ws_unsubscribe([token])
        except Exception:
            pass
        # Forget the cached tick
        self.ticks_by_token.pop(token, None)
        return True

    async def get_subscribed(self) -> list[dict[str, Any]]:
        s = await self._get_settings()
        return [i.model_dump() for i in s.subscribedInstruments]

    async def get_all_cached_instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        """Returns previously-fetched instruments (no Kite call). Useful when admin
        wants to see what's loaded in memory without triggering a refresh."""
        if exchange:
            return list(self._instruments_cache.get(exchange, []))
        out: list[dict[str, Any]] = []
        for v in self._instruments_cache.values():
            out.extend(v)
        return out

    async def remove_expired_subscriptions(self) -> int:
        """Drop instruments whose IST expiry is strictly before today (auto-cleanup)."""
        s = await self._get_settings()
        if not s.autoRemoveExpired:
            return 0
        today_ist = datetime.now(IST).date()
        before = len(s.subscribedInstruments)
        kept: list[SubscribedInstrument] = []
        for i in s.subscribedInstruments:
            if not i.expiry:
                kept.append(i)
                continue
            try:
                exp_d = datetime.fromisoformat(i.expiry).date()
            except Exception:
                kept.append(i)
                continue
            if exp_d >= today_ist:
                kept.append(i)
        removed = before - len(kept)
        if removed:
            s.subscribedInstruments = kept
            await s.save()
        return removed

    async def _clear_shared_instruments(self) -> None:
        """Drop the Redis-shared catalog + fetch locks so a manual refresh
        actually re-pulls from Kite instead of re-reading the stale mirror.
        Best-effort — a Redis hiccup must not fail the admin action."""
        try:
            await cache_delete_pattern("zerodha:instruments:*")
            await cache_delete_pattern("lock:zerodha:instruments:*")
        except Exception:
            logger.warning("zerodha_instruments_redis_clear_failed")

    async def sync_instrument_cache(self) -> dict[str, Any]:
        """Drop the in-memory CSV cache and remove expired subscriptions. The
        next search will trigger a fresh Kite fetch (matches reference behaviour)."""
        self._instruments_cache.clear()
        self._instruments_cache_at = 0.0
        await self._clear_shared_instruments()
        removed = await self.remove_expired_subscriptions()
        return {"cleared_cache": True, "expired_removed": removed}

    async def clear_subscriptions_and_cache(self) -> int:
        """Reset subscribed instruments + drop cache. The ticker is also unsubscribed."""
        s = await self._get_settings()
        tokens = [i.token for i in s.subscribedInstruments]
        s.subscribedInstruments = []
        await s.save()
        self._instruments_cache.clear()
        self._instruments_cache_at = 0.0
        await self._clear_shared_instruments()
        self.ticks_by_token.clear()
        self.ticks_by_symbol.clear()
        if tokens:
            try:
                self._ws_unsubscribe(tokens)
            except Exception:
                pass
        return len(tokens)

    async def trim_subscriptions_lru(self, keep_count: int = 700) -> dict[str, int]:
        """Keep only the ``keep_count`` most-recently-used subscriptions plus
        any tokens currently held in open positions / watchlists / LRU-exempt
        set.  Removes the rest from both the WS pool and the DB-persisted
        list so they don't grow unbounded as users browse option chains.

        Returns: ``{"kept": int, "removed": int, "must_keep_added": int}``.
        """
        s = await self._get_settings()
        all_subs = list(s.subscribedInstruments)
        if len(all_subs) <= keep_count:
            # WS/DB already within budget — but _subscribed/_state can still be
            # bloated (a prior trim shrank the DB, not those sets). Bound them to
            # the current DB tokens so they never outgrow the WS pool.
            try:
                from app.services import market_data_service as _mds

                _mds.prune_subscribed_numeric({i.token for i in all_subs})
            except Exception:
                logger.debug("zerodha_trim_prune_ticksets_failed", exc_info=True)
            return {"kept": len(all_subs), "removed": 0, "must_keep_added": 0}

        # Build the must-keep set: open positions + active watchlist items +
        # LRU-exempt tokens (admin-pinned defaults like NIFTY/BANKNIFTY).
        must_keep: set[int] = set(self._token_protected)
        must_keep_added_from_positions = 0
        try:
            from app.models.position import Position, PositionStatus

            open_positions = await Position.find(
                Position.status == PositionStatus.OPEN
            ).to_list()
            for pos in open_positions:
                try:
                    # Token lives at pos.instrument.token (InstrumentRef), NOT
                    # a flat pos.instrument_token — the old getattr always read
                    # the missing attr → 0 → NO open-position token was ever
                    # protected, so the LRU trim happily evicted held FUT/option
                    # legs nobody was actively viewing. Once evicted they got no
                    # WS ticks → risk_enforcer saw raw_ltp=None → SKIPPED SL/TP/
                    # stop-out for those positions every tick (risk_ltp_fetch_failed
                    # flood). Reading the real token (like the boot warm at
                    # _warm... does) keeps held tokens subscribed so risk always
                    # has a live price. Non-numeric synthetic (crypto/forex)
                    # tokens raise ValueError → caught below → skipped (correct;
                    # they're not Kite WS tokens).
                    tok = int(getattr(getattr(pos, "instrument", None), "token", 0) or 0)
                    if tok > 0:
                        if tok not in must_keep:
                            must_keep_added_from_positions += 1
                        must_keep.add(tok)
                except Exception:
                    continue
        except Exception:
            logger.warning("zerodha_trim_position_lookup_failed", exc_info=True)

        # Sort subscriptions by LRU recency (most-recent first).  Tokens
        # never touched after subscribe are at the bottom.
        def _last_used(t: int) -> float:
            return self._token_last_used.get(t, 0.0)

        sorted_subs = sorted(all_subs, key=lambda i: _last_used(i.token), reverse=True)

        keep: list[SubscribedInstrument] = []
        evict_tokens: list[int] = []
        keep_set: set[int] = set()
        # Always preserve must-keep tokens first.
        for sub in sorted_subs:
            if sub.token in must_keep:
                keep.append(sub)
                keep_set.add(sub.token)
        # Fill remaining slots with most-recently-used.
        for sub in sorted_subs:
            if sub.token in keep_set:
                continue
            if len(keep) < keep_count:
                keep.append(sub)
                keep_set.add(sub.token)
            else:
                evict_tokens.append(sub.token)

        s.subscribedInstruments = keep
        await s.save()
        if evict_tokens:
            try:
                self._ws_unsubscribe(evict_tokens)
            except Exception:
                logger.warning("zerodha_trim_ws_unsubscribe_failed", exc_info=True)
            for tok in evict_tokens:
                self.ticks_by_token.pop(tok, None)
                self._token_last_used.pop(tok, None)
        # ALSO prune the tick_loop's iteration sets (_subscribed / _state) to
        # the SAME keep-set. Trimming only the WS above left those two growing
        # unbounded (7,171 tokens seen) while the WS stayed at keep_count — the
        # tick_loop then overlaid all 7,171 per pass on one core → ~10 s stale
        # prices. Bounding them here keeps every published price fresh no matter
        # how long the session runs. Best-effort: a failure just leaves the old
        # (harmless, if slower) behaviour.
        pruned_ticksets = 0
        try:
            from app.services import market_data_service as _mds

            pruned_ticksets = _mds.prune_subscribed_numeric(keep_set)
        except Exception:
            logger.debug("zerodha_trim_prune_ticksets_failed", exc_info=True)
        logger.info(
            "zerodha_subscriptions_trimmed",
            extra={
                "kept": len(keep),
                "removed": len(evict_tokens),
                "must_keep_added": must_keep_added_from_positions,
                "tick_sets_pruned": pruned_ticksets,
            },
        )
        return {
            "kept": len(keep),
            "removed": len(evict_tokens),
            "must_keep_added": must_keep_added_from_positions,
        }

    async def subscription_trim_loop(
        self, interval_sec: float = 1800.0, keep_count: int = 1500
    ) -> None:
        """Periodically LRU-trim subscribed instruments so the live WS token
        set stays bounded automatically — without an operator ever needing to
        manually "clear all subscriptions". Manual clear cold-starts the
        morning feed and triggers the on-demand re-subscribe + 2 s Kite REST
        `/quote` fallback storm (the 2026-06-24 slowness). Letting the list
        grow unbounded (it hit 3800 > the 3000 WS cap) overloads the leader's
        tick parsing AND leaves the overflow tokens with no live ticks.

        `trim_subscriptions_lru` always preserves open-position, watchlist and
        admin-pinned tokens, so trimming is safe. Runs on the FEED LEADER only
        (it drives `_ws_unsubscribe`, effective only on the worker holding the
        live WS). First sweep is delayed one interval so boot's own subscribe
        + open-position warm settle first.
        """
        import asyncio as _aio

        while True:
            try:
                await _aio.sleep(interval_sec)
                result = await self.trim_subscriptions_lru(keep_count)
                if result.get("removed"):
                    logger.info("zerodha_subscription_trim_loop", extra=result)
            except _aio.CancelledError:
                raise
            except Exception:
                logger.exception("zerodha_subscription_trim_loop_failed")

    async def find_instrument_by_symbol(self, symbol: str) -> dict[str, Any] | None:
        """Resolve a tradingsymbol via subscribed → cache → on-demand exchange fetch."""
        sym_u = (symbol or "").strip().upper()
        if not sym_u:
            return None

        aliases: dict[str, list[str]] = {
            "NIFTY50": ["NIFTY 50", "NIFTY"],
            "NIFTY": ["NIFTY 50"],
            "BANKNIFTY": ["NIFTY BANK", "BANKNIFTY"],
            "FINNIFTY": ["NIFTY FIN SERVICE", "FINNIFTY"],
            "MIDCPNIFTY": ["NIFTY MID SELECT", "MIDCPNIFTY"],
            "SENSEX": ["SENSEX", "BSE SENSEX"],
            "BANKEX": ["BANKEX", "BSE BANKEX"],
        }
        candidates = {sym_u, sym_u.replace(" ", ""), sym_u.replace("_", " ")}
        for alt in aliases.get(sym_u, []):
            candidates.add(alt.upper())

        def matches(inst: dict[str, Any]) -> bool:
            sym = (inst.get("symbol") or "").strip().upper()
            return sym in candidates or sym.replace(" ", "").replace("_", "") in {
                c.replace(" ", "").replace("_", "") for c in candidates
            }

        s = await self._get_settings()
        for i in s.subscribedInstruments:
            if matches(i.model_dump()):
                return i.model_dump()
        for cached in self._instruments_cache.values():
            for inst in cached:
                if matches(inst):
                    return inst

        # On-demand fetch across the major exchanges
        for ex in ("NSE", "BSE", "NFO", "MCX", "BFO"):
            try:
                lst = await self.fetch_instruments(ex)
                for inst in lst:
                    if matches(inst):
                        return inst
            except Exception as exc:  # noqa: BLE001
                logger.warning("zerodha_find_skip_exchange", extra={"exchange": ex, "error": str(exc)})
        return None

    async def debug_csv_sample(self, exchange: str = "NFO") -> dict[str, Any]:
        """Returns the first instrument from the requested exchange — handy for
        checking that Kite credentials work end-to-end without subscribing."""
        instruments = await self.fetch_instruments(exchange)
        return {
            "exchange": exchange,
            "count": len(instruments),
            "first": instruments[0] if instruments else None,
        }

    async def diagnose(self) -> dict[str, Any]:
        """End-to-end smoke test of the Zerodha pipeline. Each step is graded
        independently so the admin can pinpoint exactly where the data flow
        breaks down (auth · instruments fetch · REST quote · ticker)."""
        s = await self._get_settings()
        report: dict[str, Any] = {
            "credentials": {
                "ok": bool(s.apiKey and s.apiSecret),
                "apiKeySet": bool(s.apiKey),
                "apiSecretSet": bool(s.apiSecret),
            },
            "auth": {
                "isConnected": bool(s.accessToken and s.isConnected),
                "tokenExpiry": s.tokenExpiry.isoformat() if s.tokenExpiry else None,
                "isTokenExpired": (
                    (_aware_expiry := _ensure_aware_utc(s.tokenExpiry)) is not None
                    and now_utc() >= _aware_expiry
                ),
            },
            "subscriptions": {
                "count": len(s.subscribedInstruments),
                "sample": [i.symbol for i in s.subscribedInstruments[:5]],
            },
            "ticker": {
                "status": str(s.wsStatus),
                "lastError": s.wsLastError,
                "liveTicksHeld": len(self.ticks_by_token),
            },
            "restQuote": {"ok": False, "error": None, "sample": None},
            "instrumentsFetch": {"ok": False, "error": None, "sample": None},
        }

        # REST profile call — confirms the token actually works
        try:
            kc, _ = await self._kite_with_token()
            await asyncio.to_thread(kc.profile)
            report["auth"]["profileCall"] = "ok"
        except Exception as e:  # noqa: BLE001
            report["auth"]["profileCall"] = f"failed: {e}"

        # Instruments fetch (uses cache if fresh)
        try:
            inst = await self.fetch_instruments("NSE")
            report["instrumentsFetch"]["ok"] = bool(inst)
            report["instrumentsFetch"]["sample"] = inst[0] if inst else None
            report["instrumentsFetch"]["count"] = len(inst)
        except Exception as e:  # noqa: BLE001
            report["instrumentsFetch"]["error"] = str(e)

        # REST quote — pick a subscribed instrument first, else fall back to RELIANCE
        probe_key = None
        if s.subscribedInstruments:
            inst0 = s.subscribedInstruments[0]
            probe_key = f"{inst0.exchange}:{inst0.symbol}"
        else:
            probe_key = "NSE:RELIANCE"
        try:
            quotes = await self.get_quote([probe_key])
            report["restQuote"]["ok"] = bool(quotes)
            report["restQuote"]["key"] = probe_key
            report["restQuote"]["sample"] = quotes.get(probe_key) if isinstance(quotes, dict) else None
        except Exception as e:  # noqa: BLE001
            report["restQuote"]["error"] = str(e)
            report["restQuote"]["key"] = probe_key

        return report

    async def connect_with_token(self, request_token: str, account_index: int = 0) -> dict[str, Any]:
        """Manual fallback: paste request_token from Kite redirect when the
        OAuth callback can't reach the backend (e.g. mobile / mismatched
        redirect URL). Same as the OAuth path otherwise."""
        return await self.generate_session(request_token, account_index)

    # ── Quotes / history ────────────────────────────────────────────
    async def get_quote(self, instrument_keys: list[str]) -> dict[str, Any]:
        """instrument_keys are Kite-format strings like 'NSE:RELIANCE'."""
        kc, _ = await self._kite_with_token()
        try:
            data = await asyncio.to_thread(kc.quote, instrument_keys)
        except Exception as e:
            raise RuntimeError(f"Kite quote failed: {e}") from e
        return data or {}

    async def get_ltp(self, instrument_keys: list[str]) -> dict[str, Any]:
        kc, _ = await self._kite_with_token()
        try:
            return await asyncio.to_thread(kc.ltp, instrument_keys)
        except Exception as e:
            raise RuntimeError(f"Kite ltp failed: {e}") from e

    async def get_quotes_batch_snapshot(self, keys: list[str]) -> tuple[dict[str, dict[str, Any]], str | None]:
        """Single Kite REST `/quote` call for many instruments at once. Writes
        each result through the per-key 10s cache so individual ``get_quote_snapshot``
        callers see them too. Returns ``(snapshots, error)`` — error is a
        human-readable reason when the whole batch failed (token expired,
        network issue), otherwise None."""
        import time as _time

        if not keys:
            return {}, None
        # De-duplicate while preserving order
        unique_keys: list[str] = []
        seen: set[str] = set()
        for k in keys:
            if k and k not in seen:
                seen.add(k)
                unique_keys.append(k)

        out: dict[str, dict[str, Any]] = {}
        try:
            data = await self.get_quote(unique_keys)
        except RuntimeError as e:
            return {}, str(e)
        if not isinstance(data, dict):
            return {}, "Kite /quote returned an unexpected payload"

        now_t = _time.time()
        tokens_to_sub: list[int] = []
        sym_map: dict[int, dict[str, str]] = {}
        for key, snap in data.items():
            if not isinstance(snap, dict):
                continue
            try:
                exchange, symbol = key.split(":", 1)
            except ValueError:
                continue
            ohlc = snap.get("ohlc") or {}
            depth = snap.get("depth") or {}
            ltp = float(snap.get("last_price") or 0)
            normalised: dict[str, Any] = {
                "token": int(snap.get("instrument_token") or 0),
                "ltp": ltp,
                "open": float(ohlc.get("open") or 0),
                "high": float(ohlc.get("high") or 0),
                "low": float(ohlc.get("low") or 0),
                "close": float(ohlc.get("close") or 0),
                "volume": int(snap.get("volume") or 0),
                "change": float(snap.get("net_change") or 0),
                "depth": depth,
                "symbol": symbol,
                "exchange": exchange,
            }
            bids = depth.get("buy") or []
            asks = depth.get("sell") or []
            normalised["bid"] = float(bids[0].get("price") or ltp) if bids else ltp
            normalised["ask"] = float(asks[0].get("price") or ltp) if asks else ltp
            self._rest_quote_cache[key] = (normalised, now_t)
            out[key] = normalised

            # Track token for on-demand subscription
            token_int = normalised.get("token", 0)
            if token_int and token_int not in self._token_to_ws:
                tokens_to_sub.append(token_int)
                sym_map[token_int] = {"symbol": symbol, "exchange": exchange}

        # On-demand: auto-subscribe all tokens from this batch
        if tokens_to_sub:
            try:
                await self.subscribe_tokens_on_demand(tokens_to_sub, sym_map)
            except Exception:
                pass

        return out, None

    async def get_quote_snapshot(self, exchange: str, symbol: str) -> dict[str, Any] | None:
        """Last-trade snapshot from Kite REST `/quote`. Cached for 10s so a busy
        option chain (≈40 legs) doesn't hammer the API. Returns None when not
        connected or Kite rejects the call — overlay then falls back to mock."""
        import time as _time

        if not exchange or not symbol:
            return None
        key = f"{exchange.upper()}:{symbol}"
        cached = self._rest_quote_cache.get(key)
        now_t = _time.time()
        if cached and (now_t - cached[1]) < self._REST_QUOTE_TTL_SEC:
            return cached[0]
        try:
            data = await self.get_quote([key])
        except RuntimeError:
            return None
        snap = data.get(key) if isinstance(data, dict) else None
        if not isinstance(snap, dict):
            return None
        # Normalise to the same shape as `ticks_by_token` so the overlay can
        # treat a REST snapshot and a live tick interchangeably.
        ohlc = snap.get("ohlc") or {}
        depth = snap.get("depth") or {}
        ltp = float(snap.get("last_price") or 0)
        normalised: dict[str, Any] = {
            "token": int(snap.get("instrument_token") or 0),
            "ltp": ltp,
            "open": float(ohlc.get("open") or 0),
            "high": float(ohlc.get("high") or 0),
            "low": float(ohlc.get("low") or 0),
            "close": float(ohlc.get("close") or 0),
            "volume": int(snap.get("volume") or 0),
            "change": float(snap.get("net_change") or 0),
            "depth": depth,
            "symbol": symbol,
            "exchange": exchange,
        }
        # Best bid / ask from depth, fall back to LTP
        bids = depth.get("buy") or []
        asks = depth.get("sell") or []
        normalised["bid"] = float(bids[0].get("price") or ltp) if bids else ltp
        normalised["ask"] = float(asks[0].get("price") or ltp) if asks else ltp
        self._rest_quote_cache[key] = (normalised, now_t)

        # On-demand: auto-subscribe this token for live ticks
        token_int = normalised.get("token", 0)
        if token_int and token_int not in self._token_to_ws:
            try:
                await self.subscribe_tokens_on_demand(
                    [token_int],
                    {token_int: {"symbol": symbol, "exchange": exchange}},
                )
            except Exception:
                pass

        return normalised

    async def get_historical(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str = "5minute",
    ) -> list[dict[str, Any]]:
        kc, _ = await self._kite_with_token()
        # Kite's historical_data reads naive datetimes as IST (exchange-local).
        # Callers pass UTC, so a UTC `to_date` (e.g. 12:34 UTC) was being read
        # by Kite as 12:34 IST — it then returned nothing after ~12:30 IST and
        # the chart showed a ~5.5h GAP between the last historical candle and
        # the live bar (the "GOLD chart gap" bug). Convert to naive IST so Kite
        # fetches candles right up to the current IST minute.
        from app.utils.time_utils import to_ist

        from_ist = to_ist(from_date).replace(tzinfo=None)
        to_ist_dt = to_ist(to_date).replace(tzinfo=None)
        try:
            data = await asyncio.to_thread(
                kc.historical_data, instrument_token, from_ist, to_ist_dt, interval
            )
        except Exception as e:
            raise RuntimeError(f"Kite historical failed: {e}") from e
        # SDK returns list of dicts with date/open/high/low/close/volume
        return [
            {
                "time": int(c["date"].timestamp()),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": int(c.get("volume") or 0),
            }
            for c in data
        ]

    # ── Fast in-memory instrument search ─────────────────────────────

    async def search_instruments_fast(
        self, q: str, exchange: str | None = None, limit: int = 30,
    ) -> list[dict[str, Any]]:
        """Blazing-fast in-memory search across the Zerodha instrument cache.
        Falls back to MongoDB if the cache is empty. Searches symbol, name,
        and trading_symbol. Results sorted: exact prefix first, then contains."""
        q_upper = (q or "").strip().upper()
        if not q_upper:
            return []

        # Try in-memory cache first (NSE + NFO + MCX + BSE + BFO)
        results: list[tuple[int, dict[str, Any]]] = []

        # Ensure the cache is warm. `fetch_instruments` is now Redis-shared, so
        # on a worker that never fetched from Kite this pulls the catalog from
        # Redis (a sibling put it there) instead of coming up empty — the
        # multi-worker "search returns nothing on 3 of 4 workers" hole.
        if exchange:
            if exchange.upper() not in self._instruments_cache:
                try:
                    await self.fetch_instruments(exchange.upper())
                except Exception:
                    pass
        elif not self._instruments_cache:
            for ex in ("NSE", "NFO", "MCX"):
                try:
                    await self.fetch_instruments(ex)
                except Exception:
                    pass

        exchanges_to_search = [exchange.upper()] if exchange else list(self._instruments_cache.keys())
        caches = [self._instruments_cache.get(ex_key, []) for ex_key in exchanges_to_search]

        # Score the 80K-row catalogs in a THREAD — same reason as the option
        # chain scan: this CPU loop on the event loop stalled the worker (slow
        # search + starved risk shard). Reads only snapshot list refs (safe).
        def _score() -> list[tuple[int, dict[str, Any]]]:
            out: list[tuple[int, dict[str, Any]]] = []
            for cache in caches:
                for inst in cache:
                    sym = (inst.get("symbol") or "").upper()
                    name = (inst.get("name") or "").upper()
                    tsym = (inst.get("tradingSymbol") or inst.get("trading_symbol") or "").upper()
                    score = -1
                    if sym == q_upper or tsym == q_upper:
                        score = 0
                    elif sym.startswith(q_upper) or tsym.startswith(q_upper):
                        score = 1
                    elif q_upper in sym or q_upper in name or q_upper in tsym:
                        score = 2
                    if score >= 0:
                        out.append((score, inst))
                        if len(out) >= limit * 3:  # over-collect for sorting
                            break
            return out

        results = await asyncio.to_thread(_score)

        # Sort by score (exact > prefix > contains), then by symbol length (shorter first)
        results.sort(key=lambda x: (x[0], len(x[1].get("symbol") or "")))
        return [r[1] for r in results[:limit]]

    @staticmethod
    def _scan_option_catalog(
        catalog: list[dict[str, Any]], und_key: str, ex: str, today: date,
    ) -> tuple[list[dict[str, Any]], set, int]:
        """Pure, CPU-only scan of one exchange's catalog for `und_key`'s option
        legs. Extracted so it can run in a THREAD (`asyncio.to_thread`): iterating
        80K instruments on the event loop stalled it for up to a few seconds,
        which starved the co-located risk shard (`ltp_ms` spikes / tick overruns)
        and blocked other requests on that worker. Reads only the passed list
        (fetch_instruments REPLACES the cache list, never mutates in place, so a
        thread holding the old reference is safe)."""
        options: list[dict[str, Any]] = []
        expiry_set: set = set()
        ex_matches = 0
        # Prefix-collision guard: some index names are a strict prefix of a
        # DIFFERENT index. "SENSEX" is a prefix of "SENSEX50" (BSE Sensex 50,
        # a separate index that trades ~24–25k). The symbol match below keys
        # on "starts with und_key + a digit", and "SENSEX50…" satisfies that
        # (the digit is the '5'), so the SENSEX chain used to swallow SENSEX50
        # legs — dragging the ATM/spot proxy down to ~25k and hiding the real
        # ~77k strikes. Reject any leg that actually belongs to a longer sibling.
        _SIBLING_PREFIXES = {"SENSEX": ("SENSEX50",)}
        _siblings = _SIBLING_PREFIXES.get(und_key, ())
        for inst in catalog:
            it = (inst.get("instrumentType") or "").upper()
            if it not in ("CE", "PE"):
                continue
            name = (inst.get("name") or "").upper().replace(" ", "")
            sym = (inst.get("symbol") or "").upper().replace(" ", "")
            name_match = name == und_key
            sym_match = (
                sym.startswith(und_key)
                and len(sym) > len(und_key)
                and sym[len(und_key)].isdigit()
            )
            if not name_match and not sym_match:
                continue
            # Belongs to a longer sibling index (e.g. SENSEX50) → not this chain.
            if _siblings and any(
                sym.startswith(s) and len(sym) > len(s) and sym[len(s)].isdigit()
                for s in _siblings
            ):
                continue
            exp_str = inst.get("expiry")
            exp_d = None
            if exp_str:
                try:
                    exp_d = datetime.fromisoformat(exp_str.replace("Z", "+00:00")).date()
                except Exception:
                    try:
                        exp_d = datetime.strptime(str(exp_str)[:10], "%Y-%m-%d").date()
                    except Exception:
                        pass
            if exp_d is not None and exp_d < today:
                continue
            if exp_d is not None:
                expiry_set.add(exp_d)
            options.append({
                "token": str(inst.get("token") or 0),
                "symbol": inst.get("symbol"),
                "exchange": inst.get("exchange") or ex,
                "expiry": exp_d.isoformat() if exp_d else None,
                "strike": inst.get("strike"),
                "option_type": it,
                "lot_size": inst.get("lotSize"),
                "_expiry_date": exp_d,
            })
            ex_matches += 1
        return options, expiry_set, ex_matches

    async def get_option_chain_fast(
        self, underlying: str, expiry_date: date | None = None,
    ) -> tuple[list[dict[str, Any]], list[date]]:
        """Fast option chain from in-memory cache. Returns (options_list, expiries).
        No MongoDB, no Python loop over 80K instruments every call — we use
        a pre-filtered approach on the cached CSV."""
        und_key = (underlying or "").strip().upper()
        if not und_key:
            return [], []

        # Determine exchange
        sensex_like = {"SENSEX", "BANKEX"}
        mcx_like = {"CRUDEOIL", "GOLD", "GOLDM", "SILVER", "SILVERM", "NATURALGAS", "COPPER"}
        if und_key in sensex_like:
            exchanges = ["BFO"]
        elif und_key in mcx_like:
            exchanges = ["MCX"]
        else:
            exchanges = ["NFO", "BFO"]

        today = date.today()
        options: list[dict[str, Any]] = []
        expiry_set: set[date] = set()

        for ex in exchanges:
            try:
                catalog = await self.fetch_instruments(ex)
            except Exception as e:
                # Without surfacing this, callers see an empty option chain
                # with no clue why (Zerodha unauthenticated, expired token,
                # network blip). Log once per (underlying, exchange) miss.
                logger.warning(
                    "option_chain_fetch_instruments_failed",
                    extra={"underlying": und_key, "exchange": ex, "error": str(e)[:200]},
                )
                continue
            # Scan the 80K-row catalog in a THREAD so the per-request CPU work
            # never blocks this worker's event loop (and thus never stalls a
            # co-located risk shard's tick — the `ltp_ms` spike source).
            ex_options, ex_expiries, ex_matches = await asyncio.to_thread(
                self._scan_option_catalog, catalog, und_key, ex, today
            )
            options.extend(ex_options)
            expiry_set.update(ex_expiries)
            logger.info(
                "option_chain_catalog_scan",
                extra={
                    "underlying": und_key,
                    "exchange": ex,
                    "catalog_size": len(catalog),
                    "matches": ex_matches,
                },
            )

        # ── DB fallback ──────────────────────────────────────────────
        # If the live Kite catalog gave us nothing (its instruments endpoint
        # is rate-limited / token expired — the "No options available" storm),
        # build the chain from the `instruments` collection, which already
        # holds every seeded/synced option. This removes the hard dependency
        # on Kite's throttled instruments dump so the option chain keeps
        # working even while Kite is 429-ing.
        if not options:
            try:
                import re as _re

                from app.models.instrument import Instrument

                _pat = f"^{_re.escape(und_key)}[0-9]"
                docs = await Instrument.find(
                    {
                        "is_active": True,
                        "option_type": {"$ne": None},
                        "exchange": {"$in": exchanges},
                        "$or": [{"name": und_key}, {"symbol": {"$regex": _pat}}],
                    }
                ).to_list()
                # Same sibling-prefix guard as the catalog scan: "SENSEX" also
                # regex-matches "SENSEX50…" legs (BSE Sensex 50, a distinct
                # ~25k index) — drop them so they don't pollute the SENSEX chain.
                _siblings = {"SENSEX": ("SENSEX50",)}.get(und_key, ())
                for d in docs:
                    ot = str(getattr(d.option_type, "value", d.option_type) or "").upper()
                    if ot not in ("CE", "PE"):
                        continue
                    _sym = (d.symbol or "").upper().replace(" ", "")
                    if _siblings and any(
                        _sym.startswith(s) and len(_sym) > len(s) and _sym[len(s)].isdigit()
                        for s in _siblings
                    ):
                        continue
                    exp_d = d.expiry
                    if hasattr(exp_d, "date"):  # datetime → date
                        exp_d = exp_d.date()
                    if exp_d is not None and exp_d < today:
                        continue
                    try:
                        strike_val = float(str(d.strike)) if d.strike is not None else None
                    except Exception:
                        strike_val = None
                    options.append(
                        {
                            "token": str(d.token),
                            "symbol": d.symbol,
                            "exchange": str(getattr(d.exchange, "value", d.exchange)),
                            "expiry": exp_d.isoformat() if exp_d else None,
                            "strike": strike_val,
                            "option_type": ot,
                            "lot_size": d.lot_size,
                            "_expiry_date": exp_d,
                        }
                    )
                    if exp_d is not None:
                        expiry_set.add(exp_d)
                if options:
                    logger.info(
                        "option_chain_db_fallback",
                        extra={"underlying": und_key, "count": len(options)},
                    )
            except Exception:
                logger.exception(
                    "option_chain_db_fallback_failed", extra={"underlying": und_key}
                )

        sorted_expiries = sorted(expiry_set)
        return options, sorted_expiries

    # ── Multi-WebSocket Pool (on-demand subscription) ──────────────

    async def _start_ws_pool(self) -> None:
        """Start the first WebSocket connection (empty). Additional connections
        are spawned automatically when a single WS hits 3000 tokens."""
        s = await self._get_settings()
        if not s.apiKey or not s.accessToken:
            raise RuntimeError("Authenticate with Zerodha before connecting the ticker")

        with self._ticker_lock:
            # Already have live connections
            if any(e.get("connected") for e in self._tickers):
                return

        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._main_loop = None

        await self._spawn_ws_connection(s.apiKey, s.accessToken)
        logger.info("zerodha_ws_pool_started", extra={"connections": len(self._tickers)})

    async def _spawn_ws_connection(self, api_key: str, access_token: str) -> int:
        """Create a new RAW WebSocket connection and add it to the pool.

        Historical note — why this no longer uses ``kiteconnect.KiteTicker``
        --------------------------------------------------------------------
        KiteTicker runs on Twisted's reactor, which is a process-global
        singleton that CANNOT be restarted once it stops
        (``ReactorNotRestartable``). After the daily 08:00 IST token
        rotation (or any teardown) the reactor thread would die, and every
        subsequent ``KiteTicker.connect()`` silently spawned a thread that
        died instantly — the socket never opened, ``on_connect``/``on_close``
        never fired, and the admin saw "Ticker: ERROR / Last close reason:
        (none)" forever. The ONLY recovery was a full backend restart (fresh
        reactor). That was the operator's daily pain.

        This implementation mirrors the proven ``bharat_indian_funded`` MERN
        approach: a plain WebSocket straight to ``wss://ws.kite.trade``
        driven on the asyncio event loop. No Twisted, no reactor, so it can
        be opened and closed as many times as needed within one process —
        self-heal and daily re-login now recover WITHOUT a restart.

        Returns the index of the new connection.
        """
        idx = len(self._tickers)
        ws_label = f"WS-{idx + 1}"
        entry: dict[str, Any] = {
            # Raw `websockets` client connection handle (set once open).
            "ws": None,
            # asyncio task running the receive loop for this connection.
            "task": None,
            "tokens": set(),
            "connected": False,
            # `connecting` lets the capacity check count this slot while the
            # handshake is in flight, so a concurrent subscribe doesn't spawn
            # a redundant second connection.
            "connecting": True,
            "label": ws_label,
            "api_key": api_key,
            "access_token": access_token,
            "last_close_reason": "",
        }

        with self._ticker_lock:
            self._tickers.append(entry)

        await self._async_set_status(WsStatus.CONNECTING)
        # Kick off the receive loop. create_task returns immediately;
        # connect_ws() polls entry["connected"] for the outcome.
        entry["task"] = asyncio.create_task(self._ws_run_loop(entry))
        return idx

    # ── Raw WebSocket receive loop ──────────────────────────────────
    async def _ws_run_loop(self, entry: dict[str, Any]) -> None:
        """Open a raw WebSocket to Kite, subscribe queued tokens, and pump
        incoming binary tick frames into the cache + Redis fanout until the
        socket closes or the task is cancelled (by ``_stop_ticker``)."""
        import websockets

        api_key = entry["api_key"]
        access_token = entry["access_token"]
        label = entry["label"]
        url = (
            f"wss://ws.kite.trade?api_key={api_key}"
            f"&access_token={access_token}"
        )
        try:
            # ping_interval=None: Kite sends its own 1-byte heartbeats and
            # does not reliably answer client pings, so leave keepalive to
            # the server (matches the bharat `ws` defaults). max_size=None:
            # a full-mode burst across hundreds of instruments can exceed
            # the 1 MiB default frame cap.
            async with websockets.connect(
                url,
                ping_interval=None,
                ping_timeout=None,
                max_size=None,
                open_timeout=20,
            ) as ws:
                entry["ws"] = ws
                entry["connected"] = True
                entry["connecting"] = False
                entry["last_close_reason"] = ""
                logger.info(f"zerodha_{label}_connected")
                # Publish the warm-up moment: the stale-feed order block gives
                # a grace period after a (re)connect, and the validator runs on
                # workers that have no socket of their own.
                try:
                    from app.core.redis_client import cache_set as _cache_set

                    await _cache_set(FEED_WARM_KEY, int(time.time()), ttl_sec=3600)
                except Exception:
                    logger.debug("zerodha_feed_warm_publish_failed", exc_info=True)
                # Subscribe any tokens queued before the socket opened.
                queued = list(entry["tokens"])
                if queued:
                    await ws.send(json.dumps({"a": "subscribe", "v": queued}))
                    await ws.send(json.dumps({"a": "mode", "v": ["full", queued]}))
                    logger.info(
                        f"zerodha_{label}_subscribed_queued",
                        extra={"count": len(queued)},
                    )
                await self._async_set_status(WsStatus.CONNECTED)

                async for message in ws:
                    # Binary frames carry ticks; text frames are postbacks /
                    # error notices. Frames shorter than 2 bytes are Kite
                    # heartbeats.
                    if isinstance(message, str):
                        logger.info(
                            f"zerodha_{label}_text_frame",
                            extra={"kite_msg": message[:300]},
                        )
                        continue
                    if isinstance(message, (bytes, bytearray)):
                        if len(message) < 2:
                            continue
                        ticks = self._parse_binary_ticks(bytes(message))
                        if ticks:
                            # Per-account tick heartbeat: stamp WHICH account's
                            # socket delivered this frame so account_healthy()
                            # can treat a connected-but-silent socket (Kite
                            # half-open / token throttle) as UNHEALTHY and fail
                            # its exchanges over to the surviving account.
                            self._last_tick_at_by_api_key[api_key] = time.monotonic()
                            self._handle_parsed_ticks(ticks)
        except asyncio.CancelledError:
            # Intentional teardown via _stop_ticker — do not record an error.
            raise
        except Exception as e:  # noqa: BLE001
            entry["last_close_reason"] = str(e)[:180]
            logger.warning(
                f"zerodha_{label}_closed",
                extra={"reason": str(e)[:200]},
            )
        finally:
            entry["connected"] = False
            entry["connecting"] = False
            entry["ws"] = None
            # Only surface a DISCONNECTED/ERROR status if this entry is still
            # part of the live pool. When _stop_ticker cancelled us it has
            # already removed the entry and may have spun up a replacement —
            # writing status here would clobber the new connection's state.
            with self._ticker_lock:
                still_pooled = entry in self._tickers
                any_alive = any(e.get("connected") for e in self._tickers)
            if still_pooled and not any_alive:
                reason = entry.get("last_close_reason") or None
                try:
                    await self._async_set_status(
                        WsStatus.DISCONNECTED, error=reason
                    )
                except Exception:
                    pass

    def _handle_parsed_ticks(self, ticks: list[dict[str, Any]]) -> None:
        """Cache each tick and fan it out to Redis. Runs on the event loop
        (called from the receive loop), so publishes are fire-and-forget
        tasks rather than cross-thread ``run_coroutine_threadsafe`` calls."""
        # Capture once per batch — monotonic so it's immune to NTP
        # adjustments that could turn `now - received_at` negative and
        # silently bypass the tick-staleness order-validator guard.
        received_at_mono = time.monotonic()
        for tick in ticks or []:
            token = int(tick.get("instrument_token") or 0)
            if not token:
                continue
            ltp = float(tick.get("last_price") or 0)
            bid = ltp
            ask = ltp
            depth = tick.get("depth") or {}
            bids = depth.get("buy") or []
            asks = depth.get("sell") or []
            if bids and asks:
                bid = float(bids[0].get("price") or ltp)
                ask = float(asks[0].get("price") or ltp)
            ohlc = tick.get("ohlc") or {}
            payload: dict[str, Any] = {
                "token": token,
                "ltp": ltp,
                "bid": bid,
                "ask": ask,
                "open": float(ohlc.get("open") or 0),
                "high": float(ohlc.get("high") or 0),
                "low": float(ohlc.get("low") or 0),
                "close": float(ohlc.get("close") or 0),
                "volume": int(tick.get("volume_traded") or 0),
                "change": float(tick.get("change") or 0),
                # Exchange's OWN packet time (Unix seconds, FULL-mode byte 60)
                # and last-trade time (byte 44). Unlike `received_at` (when WE
                # got the frame), these come from the exchange — so a stale
                # snapshot Kite resends on (re)subscribe of a CLOSED session
                # carries the PREVIOUS session's value, letting the order
                # validator tell a genuinely-live session apart from a snapshot.
                "exchange_timestamp": tick.get("exchange_timestamp"),
                "last_trade_time": tick.get("last_trade_time"),
                # Monotonic clock — read by order_validator to reject orders
                # when this instrument's last tick is stale (late market
                # open, mid-session halt, exchange feed outage). See
                # `get_last_tick_age_sec` below.
                "received_at": received_at_mono,
            }
            # ── Bad-tick spike filter ────────────────────────────────────
            # A feed glitch occasionally pushes a GARBAGE price (UKOIL $120
            # when it's $71, NIFTY BANK ₹28 when it's ₹58,000). If the risk
            # enforcer acts on it, SL/TP/stop-out fires and the position
            # closes at that junk price → phantom loss/profit (CL21309488
            # ₹1.72L settlement, CL20412429 fake ₹53k). Reject any tick whose
            # LTP jumps >50% from the LAST-GOOD tick — no real instrument
            # moves that fast tick-to-tick. If the jump PERSISTS past
            # _SPIKE_STALE_SEC (a genuine fast move, or a fresh session after a
            # gap) we accept it, so the price can never get permanently stuck.
            _prev = self.ticks_by_token.get(token)
            if ltp > 0 and _prev:
                _prev_ltp = float(_prev.get("ltp") or 0)
                _prev_at = _prev.get("received_at")
                if (
                    _prev_ltp > 0
                    and abs(ltp - _prev_ltp) / _prev_ltp > self._MAX_TICK_SPIKE_PCT
                    and _prev_at is not None
                    and (received_at_mono - _prev_at) < self._SPIKE_STALE_SEC
                ):
                    logger.warning(
                        "zerodha_tick_spike_rejected",
                        extra={
                            "token": token,
                            "ltp": ltp,
                            "prev_ltp": _prev_ltp,
                            "dev_pct": round(abs(ltp - _prev_ltp) / _prev_ltp * 100, 1),
                        },
                    )
                    continue

            sym_info = self._symbol_by_token.get(token)
            if sym_info:
                payload["symbol"] = sym_info.get("symbol", "")
                payload["exchange"] = sym_info.get("exchange", "")
                self.ticks_by_symbol[payload["symbol"]] = payload
            self.ticks_by_token[token] = payload

            try:
                asyncio.create_task(publish(f"market:tick:{token}", payload))
            except Exception:
                pass

    # ── Binary tick parser (port of the Kite protocol) ──────────────
    # Faithful Python port of the KiteTicker / kiteconnectjs binary frame
    # parser. Frame: [num_packets:int16][ (len:int16)(packet) ... ].
    @staticmethod
    def _parse_binary_ticks(data: bytes) -> list[dict[str, Any]]:
        ticks: list[dict[str, Any]] = []
        if not data or len(data) < 2:
            return ticks
        num_packets = struct.unpack(">H", data[0:2])[0]
        idx = 2
        for _ in range(num_packets):
            if idx + 2 > len(data):
                break
            pkt_len = struct.unpack(">H", data[idx:idx + 2])[0]
            idx += 2
            packet = data[idx:idx + pkt_len]
            idx += pkt_len
            if len(packet) < 8:
                continue
            tick = ZerodhaService._parse_tick_packet(packet)
            if tick:
                ticks.append(tick)
        return ticks

    @staticmethod
    def _parse_tick_packet(packet: bytes) -> dict[str, Any] | None:
        length = len(packet)

        def u32(o: int) -> int:
            return struct.unpack(">I", packet[o:o + 4])[0]

        def u16(o: int) -> int:
            return struct.unpack(">H", packet[o:o + 2])[0]

        token = u32(0)
        # The last byte of the token encodes the exchange segment, which
        # determines the price divisor (currency segments use 10^7 / 10^4,
        # everything else paise → /100). Segment 9 = indices (no depth).
        segment = token & 0xFF
        if segment == 3:  # NSE CDS
            divisor = 10000000.0
        elif segment == 6:  # BSE CDS (BCD)
            divisor = 10000.0
        else:
            divisor = 100.0
        is_index = segment == 9

        tick: dict[str, Any] = {"instrument_token": token}

        # LTP mode (8 bytes)
        if length == 8:
            tick["last_price"] = u32(4) / divisor
            return tick

        # Indices: quote (28 bytes) / full (32 bytes) — no market depth.
        if is_index:
            ltp = u32(4) / divisor
            close = u32(20) / divisor
            tick["last_price"] = ltp
            tick["ohlc"] = {
                "high": u32(8) / divisor,
                "low": u32(12) / divisor,
                "open": u32(16) / divisor,
                "close": close,
            }
            tick["change"] = ((ltp - close) / close * 100) if close else 0.0
            if length >= 32:
                tick["exchange_timestamp"] = u32(28)
            return tick

        # Tradable instruments: quote (44 bytes) / full (184 bytes).
        if length >= 44:
            ltp = u32(4) / divisor
            close = u32(40) / divisor
            tick["last_price"] = ltp
            tick["last_traded_quantity"] = u32(8)
            tick["average_traded_price"] = u32(12) / divisor
            tick["volume_traded"] = u32(16)
            tick["total_buy_quantity"] = u32(20)
            tick["total_sell_quantity"] = u32(24)
            tick["ohlc"] = {
                "open": u32(28) / divisor,
                "high": u32(32) / divisor,
                "low": u32(36) / divisor,
                "close": close,
            }
            tick["change"] = ((ltp - close) / close * 100) if close else 0.0

        if length >= 184:
            tick["last_trade_time"] = u32(44)
            tick["oi"] = u32(48)
            tick["oi_day_high"] = u32(52)
            tick["oi_day_low"] = u32(56)
            tick["exchange_timestamp"] = u32(60)
            depth: dict[str, list[dict[str, Any]]] = {"buy": [], "sell": []}
            o = 64
            for _ in range(5):
                depth["buy"].append(
                    {"quantity": u32(o), "price": u32(o + 4) / divisor, "orders": u16(o + 8)}
                )
                o += 12
            for _ in range(5):
                depth["sell"].append(
                    {"quantity": u32(o), "price": u32(o + 4) / divisor, "orders": u16(o + 8)}
                )
                o += 12
            tick["depth"] = depth

        return tick

    def _schedule_ws_send(self, entry: dict[str, Any], payload: dict[str, Any]) -> None:
        """Fire a subscribe/unsubscribe/mode control frame on a live socket
        from synchronous code. The send is scheduled onto the captured event
        loop so it is safe to call from any context."""
        ws = entry.get("ws")
        loop = self._main_loop
        if ws is None or loop is None or not entry.get("connected"):
            return
        try:
            asyncio.run_coroutine_threadsafe(ws.send(json.dumps(payload)), loop)
        except Exception:
            pass

    def _ws_subscribe(self, tokens: list[int]) -> None:
        """Subscribe tokens on-demand — assign to least-loaded WS connection."""
        with self._ticker_lock:
            if not self._tickers:
                return
            for token in tokens:
                if token in self._token_to_ws:
                    continue  # already subscribed

                # ── Exchange-aware routing (dual-account HA) ──────────────
                # Prefer the token's EFFECTIVE account entry (post-failover).
                # If that account has no healthy entry with room, use the OTHER
                # account (that IS the failover). Fall back to pure least-loaded
                # when there's no exchange info or no targeted entry available.
                best_idx = -1
                ex = (self._symbol_by_token.get(token) or {}).get("exchange", "")
                if ex and self._account_api_key:
                    target_acct = self.resolve_target_account(ex)
                    best_idx = self._find_account_entry_idx_locked(target_acct)
                    if best_idx == -1:
                        best_idx = self._find_account_entry_idx_locked(1 - target_acct)

                if best_idx == -1:
                    # Least-loaded connected WS with capacity (original path).
                    best_count = self.MAX_TOKENS_PER_WS + 1
                    for i, entry in enumerate(self._tickers):
                        if entry.get("connected") and len(entry["tokens"]) < self.MAX_TOKENS_PER_WS:
                            if len(entry["tokens"]) < best_count:
                                best_count = len(entry["tokens"])
                                best_idx = i

                if best_idx == -1:
                    # No CONNECTED socket has room. If a socket is still
                    # mid-handshake (connecting), park the token on it — the
                    # run loop subscribes everything in entry["tokens"] the
                    # moment it opens. Otherwise mark it pending for a new
                    # connection (spawned async by the caller).
                    queued = False
                    for i, entry in enumerate(self._tickers):
                        if entry.get("connecting") and len(entry["tokens"]) < self.MAX_TOKENS_PER_WS:
                            entry["tokens"].add(token)
                            self._token_to_ws[token] = i
                            queued = True
                            break
                    if not queued:
                        self._pending_tokens = getattr(self, "_pending_tokens", set())
                        self._pending_tokens.add(token)
                    continue

                entry = self._tickers[best_idx]
                entry["tokens"].add(token)
                self._token_to_ws[token] = best_idx
                # Raw WebSocket control frames (Kite protocol): subscribe then
                # switch to full mode so depth/OHLC flow.
                self._schedule_ws_send(entry, {"a": "subscribe", "v": [token]})
                self._schedule_ws_send(entry, {"a": "mode", "v": ["full", [token]]})

    async def subscribe_tokens_on_demand(self, tokens: list[int], symbols: dict[int, dict[str, str]] | None = None) -> int:
        """Public async method: subscribe a list of tokens on-demand.
        Spawns new WS connections if existing ones are at capacity.
        `symbols` is an optional {token: {"symbol": ..., "exchange": ...}} map.

        Also persists the tokens to ``ZerodhaSettings.subscribedInstruments``
        so they show up in the admin's Zerodha Connect panel and survive a
        server restart (the WS pool re-resolves the list on every reconnect).
        """
        # ── Multi-worker guard: WS pool is LEADER-ONLY ───────────────────
        # The Kite WS pool must live on the single feed-leader worker (see
        # main.py + the "only ONE active WS per access_token" note above).
        # But the overlay/quote paths (get_quote / get_quote_snapshot) call
        # this on WHATEVER worker serves the request, so on a non-leader it
        # used to open a SECOND (Nth) Kite WS on the SAME access_token. Kite
        # allows only one — the extra sockets clash and drop with 1006,
        # silently killing live ticks for the tokens parked on them. That is
        # the intermittent stale-feed / "no live session" order-block (and the
        # risk_ltp_fetch_failed flood) that only a full restart cleared.
        # On a non-leader, delegate to the leader via the feed:subscribe
        # channel — its feed_subscribe_listener -> _forward_feed_subscription
        # re-invokes THIS method on the leader (where is_feed_leader() is True,
        # so it proceeds to the real subscribe below). The non-leader serves
        # quotes from the leader's mdlive snapshot. On a single worker
        # is_feed_leader() is always True → identical to the pre-change path.
        _is_leader = True
        try:
            from app.services import market_data_service as _mds

            _is_leader = _mds.is_feed_leader()
        except Exception:
            _is_leader = True  # detection failed → behave like before (local)
        if not _is_leader:
            toks = [str(t) for t in tokens if t]
            if toks:
                try:
                    from app.core.redis_client import publish as _publish

                    await _publish(_mds.FEED_SUBSCRIBE_CHANNEL, {"tokens": toks})
                except Exception:
                    logger.debug("zerodha_on_demand_delegate_failed", exc_info=True)
            return 0

        if symbols:
            self._symbol_by_token.update(symbols)

        # LRU-touch: refresh "last used" for every token in the request,
        # whether already subscribed or new. Keeps actively-viewed strikes
        # safe from eviction even when the same user reloads the option
        # chain repeatedly.
        now_ts = time.time()
        for t in tokens:
            self._token_last_used[t] = now_ts

        new_tokens = [t for t in tokens if t not in self._token_to_ws]

        # ── Persist to the admin's subscription list ─────────────────
        # This is what makes "click an option leg → see it in admin's
        # Subscribed list" work. We add even if the WS-pool already has
        # the token (idempotent set semantics).
        try:
            s = await self._get_settings()
            existing_tokens = {i.token for i in s.subscribedInstruments}
            added: list[SubscribedInstrument] = []
            for t in tokens:
                if t in existing_tokens:
                    continue
                meta = (
                    (symbols or {}).get(t)
                    or self._symbol_by_token.get(t)
                    or self._kite_meta_for(t)
                    or {}
                )
                # Never fall back to the token number as the symbol when the
                # catalog can name it. FULL mode is re-asserted BY SYMBOL, so a
                # symbol-less row silently receives no OHLC — day high/low stay
                # 0, the day-range order gate can't apply, and the admin panel
                # shows a wall of bare numbers. 435 of 576 stored rows were in
                # that state before the catalog lookup above was added.
                sub = SubscribedInstrument(
                    token=t,
                    symbol=str(meta.get("symbol") or "").strip() or str(t),
                    exchange=str(meta.get("exchange") or "").strip() or "NSE",
                )
                s.subscribedInstruments.append(sub)
                added.append(sub)
                existing_tokens.add(t)
            if added:
                await s.save()
                # Mirror into the local Instrument collection in the
                # background so user search/quote/history endpoints find
                # them without waiting for the next CSV refresh.
                for sub in added:
                    try:
                        asyncio.create_task(self._mirror_subscription_to_instrument(sub))
                    except Exception:
                        pass
        except Exception:
            # Persistence is best-effort — WS subscribe is still useful even
            # if the settings doc can't be written. Don't block live ticks.
            logger.exception("zerodha_on_demand_persist_failed")

        if not new_tokens:
            return 0

        # Capacity check. Count BOTH connected and still-connecting entries
        # toward capacity — without this, a subscribe firing during the WS
        # handshake spawns a second connection that Kite then rejects with
        # 403 (one-WS-per-token rule).
        with self._ticker_lock:
            usable_tickers = [
                e for e in self._tickers if e.get("connected") or e.get("connecting", False)
            ]
            total_capacity = sum(
                self.MAX_TOKENS_PER_WS - len(e["tokens"]) for e in usable_tickers
            )
            need_new_ws = len(new_tokens) > total_capacity
            pool_full = len(self._tickers) >= self.MAX_WS_CONNECTIONS

        if need_new_ws and not pool_full:
            s = await self._get_settings()
            if s.apiKey and s.accessToken:
                slots_available = self.MAX_WS_CONNECTIONS - len(self._tickers)
                connections_needed = min(
                    slots_available,
                    (len(new_tokens) - total_capacity + self.MAX_TOKENS_PER_WS - 1) // self.MAX_TOKENS_PER_WS,
                )
                for _ in range(max(0, connections_needed)):
                    try:
                        await self._spawn_ws_connection(s.apiKey, s.accessToken)
                        await asyncio.sleep(0.5)
                    except Exception:
                        logger.exception("zerodha_spawn_ws_failed")
                        break
        elif need_new_ws and pool_full:
            # Pool is at its hard cap (Kite gives us at most 1 WS per API
            # key; running more triggers the 1006 / RSV3 cascade documented
            # at the top of this class). Instead of silently dropping the
            # new subscriptions — which was the operator-flagged 22-May bug
            # where new option-chain strikes showed blank prices because
            # they'd been quietly dropped on the floor — evict the
            # least-recently-used tokens to make room.
            #
            # `_token_last_used` is refreshed every time the picker /
            # market-watch / position panel re-subscribes a token, so
            # eviction lands on legs nobody is actively viewing. Protected
            # tokens (admin-pinned, open positions) are never evicted.
            shortfall = len(new_tokens) - total_capacity
            if shortfall > 0:
                evict_candidates = sorted(
                    (
                        (ts, tok)
                        for tok, ts in self._token_last_used.items()
                        if tok in self._token_to_ws
                        and tok not in self._token_protected
                        and tok not in set(tokens)
                    ),
                    key=lambda x: x[0],
                )
                to_evict = [tok for _, tok in evict_candidates[:shortfall]]
                if to_evict:
                    try:
                        self._ws_unsubscribe(to_evict)
                        for tok in to_evict:
                            self._token_last_used.pop(tok, None)
                        logger.info(
                            "zerodha_ws_lru_evicted",
                            extra={
                                "evicted": len(to_evict),
                                "for_new": len(new_tokens),
                                "protected_skipped": sum(1 for t in self._token_to_ws if t in self._token_protected),
                            },
                        )
                    except Exception:
                        logger.exception("zerodha_ws_lru_eviction_failed")
                # Anything we couldn't evict (e.g. all tokens are
                # protected) still falls off — surface that loudly.
                still_short = shortfall - len(to_evict)
                if still_short > 0:
                    logger.warning(
                        "zerodha_ws_pool_capped",
                        extra={
                            "pool_size": len(self._tickers),
                            "max": self.MAX_WS_CONNECTIONS,
                            "tokens_dropped": still_short,
                        },
                    )

        self._ws_subscribe(new_tokens)

        total_subscribed = sum(len(e["tokens"]) for e in self._tickers)
        logger.info(
            "zerodha_on_demand_subscribed",
            extra={
                "requested": len(new_tokens),
                "total_active": total_subscribed,
                "connections": len(self._tickers),
            },
        )
        return len(new_tokens)

    def mark_tokens_protected(self, tokens: list[int]) -> None:
        """Mark tokens as exempt from LRU eviction. Use for instruments
        that MUST keep ticking regardless of UI activity — open positions
        (risk_enforcer reads LTPs), admin-pinned watchlists, etc."""
        for t in tokens:
            self._token_protected.add(int(t))

    def unmark_tokens_protected(self, tokens: list[int]) -> None:
        """Drop LRU-exemption for tokens — e.g. a position closes."""
        for t in tokens:
            self._token_protected.discard(int(t))

    async def unsubscribe_tokens_on_demand(self, tokens: list[int]) -> int:
        """Public async counterpart to subscribe_tokens_on_demand. Removes
        tokens from any active WS pool entry AND from the admin's persistent
        subscribedInstruments list so the panel reflects reality. Returns the
        count actually unsubscribed from the WS pool."""
        if not tokens:
            return 0
        before = sum(len(e["tokens"]) for e in self._tickers)
        self._ws_unsubscribe(tokens)
        after = sum(len(e["tokens"]) for e in self._tickers)
        removed = max(0, before - after)

        # Best-effort remove from the persistent list — keeps the admin
        # Zerodha Connect panel in sync with what's actually streaming.
        try:
            s = await self._get_settings()
            token_set = set(tokens)
            before_count = len(s.subscribedInstruments)
            s.subscribedInstruments = [
                i for i in s.subscribedInstruments if i.token not in token_set
            ]
            if len(s.subscribedInstruments) != before_count:
                await s.save()
        except Exception:
            logger.exception("zerodha_on_demand_unpersist_failed")

        if removed:
            logger.info(
                "zerodha_on_demand_unsubscribed",
                extra={"requested": len(tokens), "removed": removed, "total_active": after},
            )
        return removed

    def _ws_unsubscribe(self, tokens: list[int]) -> None:
        with self._ticker_lock:
            for token in tokens:
                ws_idx = self._token_to_ws.pop(token, None)
                # LRU bookkeeping — evicted token shouldn't keep its
                # timestamp slot, otherwise it accumulates over time and
                # the eviction candidate list grows unbounded.
                self._token_last_used.pop(token, None)
                if ws_idx is not None and ws_idx < len(self._tickers):
                    entry = self._tickers[ws_idx]
                    entry["tokens"].discard(token)
                    if entry.get("connected"):
                        self._schedule_ws_send(entry, {"a": "unsubscribe", "v": [token]})

    # ═══════════════════════════════════════════════════════════════════
    #  Dual-account HA failover  (routing + health + takeover)
    # ═══════════════════════════════════════════════════════════════════
    def account_healthy(self, account_index: int) -> bool:
        """True if the account's WS is connected AND (tick-based) live.

        A connected socket counts UNHEALTHY when the feed is demonstrably
        flowing elsewhere (some account produced a tick within
        `health_stale_sec`) but THIS account has produced none — i.e. a
        Kite half-open / token-throttled socket. When NO account is ticking
        (off-market / quiet), a connected socket counts healthy so we never
        falsely fail over every night.
        """
        api_key = self._account_api_key.get(account_index, "")
        if not api_key:
            return False
        with self._ticker_lock:
            entry = next(
                (e for e in self._tickers if e.get("api_key") == api_key), None
            )
            connected = bool(entry and entry.get("connected"))
        if not connected:
            return False
        # Off market hours a connected socket is SILENT BY DESIGN (no ticks) —
        # don't false-flag it UNHEALTHY. This is the pre-open / overnight /
        # single-market-closed case (e.g. Account B = MCX before 09:00, or any
        # account after 23:30) that was wrongly showing UNHEALTHY and triggering
        # a needless failover. During the trading window a connected-but-silent
        # socket IS suspect (half-open), so the tick check below still applies.
        if not self._feed_expected_now():
            return True
        now = time.monotonic()
        ages = [now - t for t in self._last_tick_at_by_api_key.values()]
        feed_live = any(a <= self._health_stale_sec for a in ages) if ages else False
        if not feed_live:
            return True  # quiet everywhere → connected == healthy
        last = self._last_tick_at_by_api_key.get(api_key)
        return last is not None and (now - last) <= self._health_stale_sec

    @staticmethod
    def _feed_expected_now() -> bool:
        """True during the broad Indian trading window (weekday ~09:00–23:30
        IST) when SOME exchange is live (NSE 09:15–15:30, MCX 09:00–23:30), so a
        connected-but-silent socket is genuinely suspect. Outside it, quiet is
        normal (pre-open / overnight / weekend) → a connected socket is healthy.
        """
        from datetime import time as _dtime

        from app.utils.time_utils import is_weekend, now_ist

        try:
            n = now_ist()
            if is_weekend(n.date()):
                return False
            return _dtime(9, 0) <= n.time() <= _dtime(23, 30)
        except Exception:
            return True  # fail-safe: keep the tick check active

    def _find_account_entry_idx_locked(self, account_index: int) -> int:
        """Index of the target account's CONNECTED pool entry with room.
        Caller MUST hold `self._ticker_lock`. -1 if none available."""
        key = self._account_api_key.get(account_index, "")
        if not key:
            return -1
        for i, e in enumerate(self._tickers):
            if (
                e.get("api_key") == key
                and e.get("connected")
                and len(e["tokens"]) < self.MAX_TOKENS_PER_WS
            ):
                return i
        return -1

    def resolve_target_account(self, exchange: str) -> int:
        """Effective account (post-failover, post-debounce) for an exchange.
        Read by the sync subscribe path. Falls back to the desired account
        from the routing map when the failover loop hasn't computed a route
        yet."""
        ex = (exchange or "").upper()
        if not self._failover_enabled_cache:
            return 0  # kill-switch: single account A carries everything
        if ex in self._effective_route:
            return self._effective_route[ex]
        return self._routing_map.get(ex, 0)

    def _reindex_token_map_locked(self) -> None:
        """Rebuild `_token_to_ws` from each entry's `tokens` set (source of
        truth). Call after ANY structural change to `_tickers` — removing a
        non-last entry shifts indices and would otherwise leave stale
        token→index pointers. Caller MUST hold `self._ticker_lock`."""
        self._token_to_ws = {}
        for i, e in enumerate(self._tickers):
            for tok in e.get("tokens", set()):
                self._token_to_ws[tok] = i

    async def disconnect_account_ws(self, account_index: int) -> None:
        """Tear down ONLY one account's WS entry (account-scoped teardown) and
        re-home its tokens onto the surviving account — WITHOUT touching the
        other socket. This is what lets a failed account be reconnected without
        killing the survivor (the fix for `_stop_ticker`'s all-or-nothing gap).
        """
        api_key = self._account_api_key.get(account_index) or ""
        if not api_key:
            try:
                s = await ZerodhaSettings.find_one(
                    ZerodhaSettings.account_index == account_index
                )
                api_key = (s.apiKey if s else "") or ""
            except Exception:
                api_key = ""
        if not api_key:
            return
        dropped: list[int] = []
        stale_tasks: list[asyncio.Task] = []
        with self._ticker_lock:
            keep, drop = [], []
            for e in self._tickers:
                (drop if e.get("api_key") == api_key else keep).append(e)
            for e in drop:
                dropped.extend(e.get("tokens", set()))
            self._tickers[:] = keep
            self._reindex_token_map_locked()
            stale_tasks = [
                e["task"] for e in drop if e.get("task") and not e["task"].done()
            ]
        for t in stale_tasks:
            try:
                t.cancel()
            except Exception:
                pass
        # Re-subscribe the orphaned tokens; _ws_subscribe re-routes them onto
        # the surviving account (target account has no entry → failover branch).
        if dropped:
            self._ws_subscribe(list(set(dropped)))
        logger.info(
            "zerodha_account_ws_disconnected",
            extra={"account_index": account_index, "reassigned": len(set(dropped))},
        )

    async def _load_routing_config(self):
        from app.models.zerodha_feed_routing import ZerodhaFeedRouting

        cfg = await ZerodhaFeedRouting.find_one()
        if cfg is None:
            cfg = ZerodhaFeedRouting()
            try:
                await cfg.insert()
            except Exception:
                logger.debug("zerodha_routing_config_seed_failed", exc_info=True)
        return cfg

    async def _refresh_account_keys(self) -> None:
        for idx in (0, 1):
            try:
                s = await ZerodhaSettings.find_one(
                    ZerodhaSettings.account_index == idx
                )
                self._account_api_key[idx] = (s.apiKey if s and s.apiKey else "")
            except Exception:
                pass

    def _apply_routing_moves(self) -> None:
        """Move already-subscribed tokens onto their EFFECTIVE account entry
        when routing/health changed. Idempotent — a steady state moves nothing.
        Capacity-guarded: if the target entry is full the token stays put
        (logged), never silently dropped."""
        moves: list[tuple[int, int, int]] = []
        skipped_cap = 0
        with self._ticker_lock:
            key_to_acct = {
                key: acct for acct, key in self._account_api_key.items() if key
            }
            for token, ws_idx in list(self._token_to_ws.items()):
                if ws_idx >= len(self._tickers):
                    continue
                cur_key = self._tickers[ws_idx].get("api_key")
                cur_acct = key_to_acct.get(cur_key)
                ex = (self._symbol_by_token.get(token) or {}).get("exchange", "")
                if not ex:
                    continue
                target_acct = self._effective_route.get(ex.upper())
                if target_acct is None or target_acct == cur_acct:
                    continue
                tgt_idx = self._find_account_entry_idx_locked(target_acct)
                if tgt_idx == -1:
                    skipped_cap += 1
                    continue
                moves.append((token, ws_idx, tgt_idx))
            for token, old_idx, tgt_idx in moves:
                old_e = self._tickers[old_idx]
                tgt_e = self._tickers[tgt_idx]
                old_e["tokens"].discard(token)
                if old_e.get("connected"):
                    self._schedule_ws_send(old_e, {"a": "unsubscribe", "v": [token]})
                tgt_e["tokens"].add(token)
                self._token_to_ws[token] = tgt_idx
                self._schedule_ws_send(tgt_e, {"a": "subscribe", "v": [token]})
                self._schedule_ws_send(tgt_e, {"a": "mode", "v": ["full", [token]]})
        if moves:
            logger.info("zerodha_feed_failover_moved", extra={"moved": len(moves)})
        if skipped_cap:
            logger.warning(
                "zerodha_feed_failover_capacity_skip",
                extra={"skipped": skipped_cap, "cap": self.MAX_TOKENS_PER_WS},
            )

    async def feed_failover_loop(self, interval_sec: float = 3.0) -> None:
        """Feed-leader-only controller: keep each exchange's tokens on the
        right account, failing over to the survivor when the desired account
        is unhealthy and failing back (debounced) when it recovers."""
        if self._failover_running:
            return
        self._failover_running = True
        logger.info(
            "zerodha_feed_failover_loop_started", extra={"interval_sec": interval_sec}
        )
        try:
            while self._failover_running:
                try:
                    cfg = await self._load_routing_config()
                    await self._refresh_account_keys()
                    self._routing_map = {
                        str(k).upper(): int(v)
                        for k, v in (cfg.exchange_account_map or {}).items()
                    }
                    self._failover_enabled_cache = bool(cfg.failover_enabled)
                    self._health_stale_sec = float(cfg.health_stale_sec or 15)
                    self._failover_confirm_down_sec = float(
                        cfg.failover_confirm_down_sec or 5
                    )
                    self._failback_confirm_up_sec = float(
                        cfg.failback_confirm_up_sec or 25
                    )
                    now = time.monotonic()
                    # Raw health + debounce bookkeeping.
                    for idx in (0, 1):
                        h = self.account_healthy(idx)
                        self._account_healthy_cache[idx] = h
                        if h:
                            self._account_up_since.setdefault(idx, now)
                            self._account_down_since.pop(idx, None)
                        else:
                            self._account_down_since.setdefault(idx, now)
                            self._account_up_since.pop(idx, None)
                    # Debounced effective route per exchange.
                    new_route: dict[str, int] = {}
                    for ex, desired in self._routing_map.items():
                        if not self._failover_enabled_cache:
                            new_route[ex] = 0
                            continue
                        other = 1 - desired
                        prev = self._effective_route.get(ex, desired)
                        other_healthy = self._account_healthy_cache.get(other, False)
                        desired_healthy = self._account_healthy_cache.get(desired, False)
                        down_for = (
                            now - self._account_down_since[desired]
                            if desired in self._account_down_since
                            else 0.0
                        )
                        up_for = (
                            now - self._account_up_since[desired]
                            if desired in self._account_up_since
                            else 0.0
                        )
                        if prev == desired:
                            # On desired: fail over only if confirmed down long
                            # enough AND the survivor is healthy.
                            if (
                                not desired_healthy
                                and down_for >= self._failover_confirm_down_sec
                                and other_healthy
                            ):
                                new_route[ex] = other
                            else:
                                new_route[ex] = desired
                        else:
                            # Failed over to `other`: fail back only after
                            # desired is confirmed up long enough. Exception:
                            # if the survivor also dies but desired is alive,
                            # snap back immediately (data > flap-avoidance).
                            if desired_healthy and up_for >= self._failback_confirm_up_sec:
                                new_route[ex] = desired
                            elif not other_healthy and desired_healthy:
                                new_route[ex] = desired
                            else:
                                new_route[ex] = other
                    self._effective_route = new_route
                    if self._failover_enabled_cache:
                        self._apply_routing_moves()
                    # Publish live status to Redis so the admin API (served by
                    # BACKEND workers, a separate process) can display it.
                    try:
                        from app.core.redis_client import cache_set

                        await cache_set(
                            FAILOVER_STATUS_KEY, self.get_failover_status(), ttl_sec=15
                        )
                        await cache_set(
                            WS_POOL_KEY, self.get_ws_pool_info(), ttl_sec=15
                        )
                    except Exception:
                        pass
                except Exception:
                    logger.exception("zerodha_feed_failover_tick_failed")
                await asyncio.sleep(interval_sec)
        finally:
            self._failover_running = False
            logger.info("zerodha_feed_failover_loop_stopped")

    async def feed_failover_cmd_listener(self) -> None:
        """LEADER-ONLY: consume admin failover commands (off-market test
        disconnect) from Redis and execute them on the FEED process, which
        owns the WS pool. The admin API (backend workers) can't touch the
        pool directly, so it publishes here."""
        from app.core.redis_client import pubsub

        backoff = 1.0
        ps: Any = None
        try:
            while True:
                try:
                    if ps is None:
                        ps = pubsub()
                        await ps.subscribe(FAILOVER_CMD_CHANNEL)
                        logger.info("zerodha_failover_cmd_listener_started")
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
                        if data.get("action") == "disconnect":
                            try:
                                acct = int(data.get("account"))
                                if acct in (0, 1):
                                    await self.disconnect_account_ws(acct)
                            except Exception:
                                logger.warning(
                                    "zerodha_failover_cmd_disconnect_failed",
                                    exc_info=True,
                                )
                        elif data.get("action") == "reconnect":
                            # A login finished on another worker (/callback):
                            # bring THAT account's socket up on the fresh token.
                            try:
                                acct = int(data.get("account"))
                                if acct in (0, 1):
                                    s = await self._get_settings(acct)
                                    if s.apiKey and s.accessToken:
                                        self._kick_ws_after_login(
                                            acct, s.apiKey, s.accessToken
                                        )
                            except Exception:
                                logger.warning(
                                    "zerodha_failover_cmd_reconnect_failed",
                                    exc_info=True,
                                )
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("zerodha_failover_cmd_listener_error", exc_info=True)
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
                    await ps.unsubscribe(FAILOVER_CMD_CHANNEL)
                except Exception:
                    pass

    def stop_feed_failover(self) -> None:
        self._failover_running = False

    def get_failover_status(self) -> dict[str, Any]:
        """Live snapshot for the admin Routing & Failover card."""
        now = time.monotonic()

        def _acct(idx: int) -> dict[str, Any]:
            key = self._account_api_key.get(idx, "")
            last = self._last_tick_at_by_api_key.get(key) if key else None
            with self._ticker_lock:
                entry = next(
                    (e for e in self._tickers if e.get("api_key") == key), None
                )
                connected = bool(entry and entry.get("connected"))
            return {
                "account_index": idx,
                "label": "A" if idx == 0 else "B",
                "configured": bool(key),
                "connected": connected,
                "healthy": self._account_healthy_cache.get(idx, False),
                "last_tick_age_sec": (round(now - last, 1) if last else None),
            }

        # Which exchanges are currently FAILED OVER (effective != desired).
        failovers = {
            ex: self._effective_route[ex]
            for ex, desired in self._routing_map.items()
            if ex in self._effective_route and self._effective_route[ex] != desired
        }
        return {
            "failover_enabled": self._failover_enabled_cache,
            "exchange_account_map": dict(self._routing_map),
            "effective_route": dict(self._effective_route),
            "active_failovers": failovers,
            "accounts": {"A": _acct(0), "B": _acct(1)},
        }

    def _stop_ticker(self) -> None:
        """Tear down every live raw WebSocket connection.

        Each connection is driven by an asyncio task running
        ``_ws_run_loop``. Cancelling that task raises ``CancelledError``
        inside the ``async with websockets.connect()`` block, which closes
        the socket cleanly on the next loop iteration. We snapshot and clear
        the pool FIRST (so any concurrent reader sees an empty, consistent
        pool), then cancel the detached tasks. Because the run loop guards
        its status write with ``entry in self._tickers``, a cancelled task
        can never clobber a freshly-spawned replacement's state.

        Unlike the old Twisted/KiteTicker path, there is no process-global
        reactor to keep alive or zombie thread to neuter — a fresh
        connection can be opened immediately after, any number of times.
        """
        with self._ticker_lock:
            entries = list(self._tickers)
            self._tickers.clear()
            self._token_to_ws.clear()
            self._token_last_used.clear()
            self._ticker = None
        for entry in entries:
            entry["connected"] = False
            entry["connecting"] = False
            task = entry.get("task")
            if task is not None and not task.done():
                try:
                    task.cancel()
                except Exception:
                    pass

    async def connect_ws(self, *, force: bool = True) -> None:
        """Start the WebSocket pool. If there are DB-persisted subscriptions
        (admin-pinned), subscribe those. Otherwise just start an empty pool
        for on-demand subscriptions.

        When ``force`` is True (the default), tear down any existing local
        socket AND wait a few seconds before reconnecting. This is the only
        reliable way to escape the 403 "WebSocket connection upgrade failed"
        loop: Zerodha allows exactly ONE active KiteTicker per access_token,
        and when a process crashes / a deploy swaps containers / a stale
        socket lingers, Kite's side keeps the old slot warm for a few
        seconds longer than ours does. Reconnecting too eagerly hits 403;
        a short sleep lets Kite release.

        Any explicit connect call un-pauses the self-heal loop so background
        re-arming resumes after a manual reconnect.
        """
        # Capture the asyncio event loop BEFORE spawning any Twisted
        # threads. The on_ticks / on_connect callbacks use
        # asyncio.run_coroutine_threadsafe() which needs a valid loop
        # reference. Setting it here (not just inside _start_ws_pool)
        # ensures it's always fresh — even after a daily token rotation
        # where _start_ws_pool may short-circuit on the "already have
        # live connections" guard.
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        # Lazily create the connect-lock on the current event loop. Cannot
        # do this in __init__ because there's no running loop there.
        if self._ws_connect_lock is None:
            self._ws_connect_lock = asyncio.Lock()

        # Serialise — if another caller is mid-connect (e.g. post-login
        # kickoff still running), wait for them instead of racing and
        # killing their in-flight ticker.
        async with self._ws_connect_lock:
            await self._connect_ws_locked(force=force)

    async def _connect_ws_locked(self, *, force: bool) -> None:
        # Re-arm self-heal — a fresh connect_ws call means the admin / a
        # fresh login wants the ticker back online, even if they had
        # previously hit Disconnect.
        self._self_heal_paused = False

        s = await self._get_settings()
        if not s.apiKey or not s.accessToken:
            raise RuntimeError("Authenticate with Zerodha before connecting the ticker")

        if not force:
            with self._ticker_lock:
                if any(e.get("connected") for e in self._tickers):
                    return

        # Hard-reset: kill every local socket, blank the WS status to
        # CONNECTING. Sleep ONLY if we actually had a live/connecting
        # socket on the SAME access_token to tear down — Kite's gateway
        # needs ~15 s to release that slot before it accepts a new one.
        #
        # Bharat-style direct connect: if the existing tickers were on
        # a DIFFERENT access_token (i.e. we just did a fresh OAuth /
        # manual-token login and rotated the credential), Kite has
        # already issued a brand-new slot for the new token — there is
        # no slot conflict and the 15 s wait is pure dead time. Same on
        # a cold boot with zero prior sockets.
        slot_held = False
        with self._ticker_lock:
            for e in self._tickers:
                if not (e.get("connected") or e.get("connecting")):
                    continue
                if e.get("access_token") == s.accessToken:
                    slot_held = True
                    break
        self._stop_ticker()
        await self._async_set_status(WsStatus.CONNECTING, error=None)
        if force and slot_held:
            await asyncio.sleep(15)

        # SINGLE-SHOT connect. The inner 8-attempt × 7-min backoff that
        # used to live here was the root cause of the daily 07:00 hang:
        # every self_heal cycle blocked for ~7 minutes, so we got 8
        # tries per hour instead of 180 (3 per minute).  Now the retry
        # cadence is driven entirely by `ws_self_heal_loop` (20 s) so
        # if Kite's slot releases at minute 4, we catch it within 20 s
        # instead of waiting for the 70 s-into-cycle attempt.
        last_error: Exception | None = None
        last_close_reason: str = ""
        try:
            await self._start_ws_pool()
            # The raw WebSocket usually opens within ~1-2 s, but during the
            # morning rush + post-token-rotation Kite can take 10-20 s to
            # release the prior slot. Poll up to 25 s for the handshake to
            # complete, breaking early on success (happy path ~1-2 s) OR as
            # soon as every connection attempt has FINISHED without
            # connecting (e.g. a 403) so a definitive failure doesn't wait
            # out the full window.
            for _ in range(100):  # 100 × 0.25 s = 25 s
                await asyncio.sleep(0.25)
                with self._ticker_lock:
                    if any(e.get("connected") for e in self._tickers):
                        break
                    # All run loops finished (not connected, not connecting)
                    # → attempt is over, surface the failure immediately.
                    if self._tickers and all(
                        not e.get("connected") and not e.get("connecting")
                        for e in self._tickers
                    ):
                        break
                    if not self._tickers:
                        break
            with self._ticker_lock:
                connected_now = any(e.get("connected") for e in self._tickers)
            if not connected_now:
                last_error = RuntimeError("WS upgrade did not complete")
                with self._ticker_lock:
                    for e in self._tickers:
                        reason = e.get("last_close_reason")
                        if reason:
                            last_close_reason = str(reason)
                            break
        except Exception as e:  # noqa: BLE001
            last_error = e

        if last_error is not None:
            self._stop_ticker()
            logger.warning(
                "zerodha_ws_attempt_failed",
                extra={
                    "error": str(last_error)[:200],
                    "close_reason": last_close_reason[:200],
                },
            )
            reason_l = last_close_reason.lower()
            looks_like_403 = "403" in reason_l or "forbidden" in reason_l or "1006" in reason_l
            if looks_like_403:
                msg = (
                    "Kite rejected the WebSocket (slot held). Self-heal "
                    "will retry every 20 s. If this persists past 2 min, "
                    "click Disconnect + reconnect Zerodha."
                )
            else:
                msg = (
                    "WebSocket connect failed. Last close reason: "
                    f"{last_close_reason[:180] or '(none)'}. Self-heal "
                    "will retry every 20 s."
                )
            await self._async_set_status(WsStatus.ERROR, error=msg)
            raise RuntimeError(str(last_error))

        # Subscribe any DB-persisted instruments (admin-pinned / watchlist)
        if s.subscribedInstruments:
            tokens = [i.token for i in s.subscribedInstruments]
            sym_map = {
                i.token: {"symbol": i.symbol, "exchange": i.exchange}
                for i in s.subscribedInstruments
            }
            await self.subscribe_tokens_on_demand(tokens, sym_map)

        # Warm live ticks for every OPEN-position token — boot AND every
        # reconnect. After the daily 08:00 IST token rotation (or an admin
        # "clear subscriptions") the persisted list above can be empty/stale,
        # so without this the morning's FIRST quote/fill for each held
        # instrument falls to the 2 s Kite REST `/quote` fallback in
        # `_zerodha_overlay` — the `zerodha_overlay_timeout` flood that made
        # MARKET fills take 2-4 s until the feed organically warmed (~3 hrs).
        # Force-subscribing held tokens here guarantees in-memory ticks exist
        # BEFORE users trade. Idempotent (subscribe_tokens_on_demand skips
        # already-subscribed) and additive (never unsubscribes). Numeric Kite
        # tokens only — Infoway/synthetic (crypto/forex) tokens carry their
        # own feed and must not be sent to Kite WS.
        try:
            from app.models.position import Position, PositionStatus

            open_positions = await Position.find(
                Position.status == PositionStatus.OPEN
            ).to_list()
            pos_tokens: list[int] = []
            pos_sym_map: dict[int, dict[str, str]] = {}
            for p in open_positions:
                try:
                    tok = int(p.instrument.token)
                except (TypeError, ValueError):
                    continue  # Infoway / synthetic token — not a Kite WS token
                ex = getattr(p.instrument, "exchange", None)
                ex_str = ex.value if hasattr(ex, "value") else str(ex or "NSE")
                pos_tokens.append(tok)
                pos_sym_map[tok] = {
                    "symbol": getattr(p.instrument, "symbol", "") or str(tok),
                    "exchange": ex_str,
                }
            if pos_tokens:
                await self.subscribe_tokens_on_demand(pos_tokens, pos_sym_map)
                # ALSO register in market_data_service._subscribed — the gate
                # tick_loop uses to mirror quotes into mdlive:{token}.
                # Subscribing on the Kite WS alone warms `_state` but NOT
                # `_subscribed`, and the sharded risk loop reads mdlive — so
                # without this the held token has no live price and risk skips
                # its SL/TP/stop-out every tick (risk_ltp_fetch_failed flood).
                try:
                    from app.services import market_data_service

                    market_data_service.subscribe([str(t) for t in pos_tokens])
                except Exception:
                    logger.exception("zerodha_open_position_mdlive_register_failed")
                logger.info(
                    "zerodha_open_position_tokens_warmed",
                    extra={"count": len(pos_tokens)},
                )
        except Exception:
            logger.exception("zerodha_open_position_warm_failed")

    def _update_ws_status(self, status: WsStatus, *, error: str | None = None) -> None:
        if self._main_loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._async_set_status(status, error=error), self._main_loop)
        except Exception:
            pass

    async def _async_set_status(self, status: WsStatus, *, error: str | None = None) -> None:
        s = await self._get_settings()
        s.wsStatus = status
        if error is not None:
            # Caller is recording a fresh failure — keep it.
            s.wsLastError = error
        elif status in (
            WsStatus.CONNECTED,
            WsStatus.CONNECTING,
            WsStatus.DISCONNECTED,
        ):
            # Transition to a clean state WITHOUT a new error — clear any
            # historical error message so the admin UI doesn't carry a
            # stale red banner forward. Hits these cases:
            #   • CONNECTING / CONNECTED — successful reconnect.
            #   • DISCONNECTED — admin clicked Disconnect (`disconnect_ws()`
            #     calls this with no error). Without the clear, the last
            #     403 from the prior session sticks to the panel forever.
            s.wsLastError = None
        await s.save()

    async def disconnect_ws(self) -> None:
        self._stop_ticker()
        await self._async_set_status(WsStatus.DISCONNECTED)
        # Pause the self-heal loop so it doesn't immediately reconnect
        # after an intentional admin disconnect. Resumes on next manual
        # connect_ws or fresh login.
        self._self_heal_paused = True

    async def force_reconnect_ws(self) -> None:
        """Operator-facing "do what backend restart would do" reconnect.

        Plain ``connect_ws()`` re-arms the self-heal pause flag and the
        main-loop reference, but it does NOT clear the heal-failure
        counter. Once a daily token rotation leaves that counter at 5+
        (next sleep ~16 min, capped at 5 min), the self-heal loop is
        effectively idle for minutes at a stretch — exactly the state
        every admin used to "fix" with a backend restart, because
        restart reinitialises the service object and the counter goes
        back to zero.

        This method does the same three things a restart does, in
        order, without dropping the process:
          1. Stop any half-alive ticker.
          2. Reset the self-heal counter + un-pause the loop so the
             very next try uses the base interval.
          3. Refresh the captured asyncio main loop (the prior one
             may have been starved by overlay timeouts during the
             stuck window).
          4. Drive a fresh ``connect_ws(force=True)`` synchronously
             and return its outcome so the admin button can show a
             success / failure toast in one round-trip.
        """
        logger.info(
            "zerodha_ws_force_reconnect_requested",
            extra={
                "prior_consecutive_failures": self._ws_consecutive_heal_failures,
                "prior_self_heal_paused": self._self_heal_paused,
            },
        )
        try:
            self._stop_ticker()
        except Exception:  # noqa: BLE001 — never let teardown block the reconnect
            logger.exception("zerodha_force_reconnect_stop_ticker_failed")
        self._ws_consecutive_heal_failures = 0
        self._self_heal_paused = False
        # Cut any in-flight self-heal sleep short — without this the
        # explicit force-reconnect would race the loop which might be
        # parked for up to 5 min already.
        self._wake_self_heal()
        try:
            self._main_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
        # connect_ws already serialises via the connect lock + carries
        # its own 25 s upgrade timeout, so propagate any error back to
        # the caller as-is for the admin UI to surface.
        await self.connect_ws(force=True)

    # ─────────────────────────── Self-heal loop ─────────────────────
    # Runs forever from FastAPI lifespan. Once a minute it checks WS
    # status; if it's in ERROR (5-retry exhaust) or DISCONNECTED while
    # auth is still valid, it tries `connect_ws(force=True)` again with
    # its own back-off ladder. This is the permanent fix for the daily
    # "WebSocket connect failed after 5 retries" admin had to click
    # through every morning after the 08:00 IST token rotation — the
    # token refresh is automatic (handled by `_kite_with_token`), so
    # all that was missing was a background re-arming of the WS pool.

    _self_heal_paused: bool = False
    _self_heal_running: bool = False
    # Wake-up signal so the self-heal loop can abort its current sleep
    # and try a fresh connect immediately. Set when:
    #   • manual login completes (operator clicked "Login to Zerodha")
    #   • the "Force reconnect ticker" button is clicked
    # Without this, the loop's exponential-backoff sleep (up to 5 min)
    # used to stay armed even after a fresh login landed, so the
    # operator saw "Ticker: ERROR" for minutes despite Authentication
    # being green. That's the daily "must restart backend" pain.
    # Lazily constructed on first use because there's no event loop at
    # __init__ time.
    _self_heal_wake: "asyncio.Event | None" = None

    def _wake_self_heal(self) -> None:
        """Cut the current self-heal sleep short. Safe from sync code —
        the event is only consulted inside the loop's awaiter."""
        try:
            if self._self_heal_wake is not None:
                self._self_heal_wake.set()
        except Exception:
            pass

    # Self-heal backoff caps. Base interval is the caller's value
    # (defaults to 30 s in main.py). Each consecutive failure DOUBLES
    # the next sleep up to `_SELF_HEAL_MAX_INTERVAL_SEC` (5 min) so
    # an unresponsive Kite endpoint isn't hammered every 20 s — that
    # was the "1500 attempts overnight, zero success" pattern. A single
    # success resets the counter so the next outage starts at the base
    # cadence again.
    _SELF_HEAL_MAX_INTERVAL_SEC = 300

    async def ws_self_heal_loop(self, interval_sec: float = 30.0) -> None:
        """Background task — periodically nudge a stuck WebSocket back
        online. Skipped when:
          - admin explicitly disconnected (`_self_heal_paused` flag)
          - access token missing / expired (waits for fresh login)
          - WS is already CONNECTED (nothing to do)

        Fires `connect_ws(force=True)` which carries its own 5-attempt
        back-off ladder (~3 min total) — that handles the slot-release
        window after every token rotation or process restart. If still
        failing after a heal attempt, the loop sleeps `interval_sec`
        and tries again.
        """
        if self._self_heal_running:
            return
        self._self_heal_running = True
        # Lazily build the wake-up event on the running loop. Doing this
        # in __init__ would attach to a loop that no longer exists by
        # the time the lifespan task starts.
        if self._self_heal_wake is None:
            self._self_heal_wake = asyncio.Event()
        logger.info("zerodha_ws_self_heal_loop_started", extra={"interval_sec": interval_sec})
        try:
            while self._self_heal_running:
                try:
                    # Exponential backoff: double the wait per consecutive
                    # failure, cap at _SELF_HEAL_MAX_INTERVAL_SEC. Resets
                    # to base on the first successful connect.
                    sleep_for = min(
                        interval_sec * (2 ** self._ws_consecutive_heal_failures),
                        float(self._SELF_HEAL_MAX_INTERVAL_SEC),
                    )
                    # Sleep with an early-wake escape: if a manual login
                    # or "Force reconnect" lands while we're parked,
                    # the event fires and we re-enter the body
                    # immediately instead of waiting out the (possibly
                    # 5-min capped) backoff. Without this, the daily
                    # 08:00 IST token rotation would clear the token,
                    # operator would login at 08:01, and self-heal
                    # wouldn't try again until ~08:06 — looked broken
                    # from the operator's seat.
                    try:
                        await asyncio.wait_for(self._self_heal_wake.wait(), timeout=sleep_for)
                        self._self_heal_wake.clear()
                    except asyncio.TimeoutError:
                        pass
                    if self._self_heal_paused:
                        continue
                    s = await self._get_settings()
                    if not s.apiKey or not s.accessToken:
                        # No auth yet — admin hasn't logged in. Self-heal
                        # has nothing to do. Try to trigger auto-login if
                        # creds + toggle are configured.
                        await self._maybe_trigger_auto_login_when_token_missing()
                        continue
                    # Auth token expired (nominal 08:00 IST clock)? Clear
                    # and try auto-login.
                    expiry = _ensure_aware_utc(s.tokenExpiry)
                    if expiry and now_utc() >= expiry:
                        logger.info("zerodha_ws_self_heal_token_expired_clearing")
                        try:
                            s.accessToken = None
                            s.refreshToken = None
                            s.tokenExpiry = None
                            s.isConnected = False
                            s.wsStatus = WsStatus.DISCONNECTED
                            await s.save()
                        except Exception:
                            logger.exception("zerodha_ws_self_heal_expired_clear_failed")
                        # CRITICAL: daily 08:00 IST rotation = clean
                        # slate. Without this reset the counter would
                        # carry the night's worth of failed retries into
                        # the new token's session, so the FIRST attempt
                        # after the fresh login would already be 5 min
                        # out. That's the daily "morning ko ticker
                        # nahi chala" operator pain.
                        self._ws_consecutive_heal_failures = 0
                        await self._maybe_trigger_auto_login_when_token_missing()
                        continue
                    # ── Account B independent self-heal (HA) ─────────────
                    # Heal B on EVERY tick, regardless of A's state. True HA
                    # needs each account to recover on its own; previously B
                    # was only healed while A was up (2966 gate), so an A
                    # outage left B un-monitored — the exact single-point the
                    # failover feature must remove.
                    try:
                        s_b = await self._get_settings(1)
                        if s_b.apiKey and s_b.accessToken and s_b.isConnected:
                            with self._ticker_lock:
                                b_ok = any(
                                    e.get("api_key") == s_b.apiKey
                                    and (e.get("connected") or e.get("connecting"))
                                    for e in self._tickers
                                )
                            if not b_ok:
                                logger.info("zerodha_account_b_self_heal_triggering")
                                asyncio.create_task(
                                    self._account_b_ws_connect(s_b.apiKey, s_b.accessToken)
                                )
                    except Exception:
                        logger.exception("zerodha_account_b_self_heal_failed")

                    # Account A already alive (A's OWN socket, not just "any" —
                    # with a healthy B, `any connected` would mask a dead A and
                    # A would never self-heal)? Then nothing more to do.
                    with self._ticker_lock:
                        a_connected = any(
                            e.get("api_key") == s.apiKey and e.get("connected")
                            for e in self._tickers
                        )
                    if a_connected:
                        continue
                    # Token nominally valid but WS dead. REST-probe before
                    # spawning yet another KiteTicker — if Kite has secretly
                    # invalidated the token (duplicate login from another
                    # device, IP block, server-side rotation), the probe
                    # clears the DB token and we skip until next login.
                    # This is the fix for "WS keeps failing for hours until
                    # backend restart" — restart was masking the dead token
                    # by forcing a fresh login path.
                    token_alive = await self.probe_and_clear_invalid_token()
                    if not token_alive:
                        logger.warning("zerodha_ws_self_heal_token_dead_skipping")
                        await self._maybe_trigger_auto_login_when_token_missing()
                        continue
                    # ERROR or DISCONNECTED with valid auth → try again.
                    # Refresh the event loop reference — it may have gone
                    # stale if the prior connect attempt captured a loop
                    # that was being starved by overlay timeouts.
                    try:
                        self._main_loop = asyncio.get_running_loop()
                    except RuntimeError:
                        pass
                    logger.info(
                        "zerodha_ws_self_heal_triggering",
                        extra={"current_status": s.wsStatus},
                    )
                    try:
                        await self.connect_ws(force=True)
                        self._ws_consecutive_heal_failures = 0
                        logger.info("zerodha_ws_self_heal_succeeded")
                    except Exception as e:
                        self._ws_consecutive_heal_failures = min(
                            self._ws_consecutive_heal_failures + 1, 6
                        )
                        logger.warning(
                            "zerodha_ws_self_heal_attempt_failed",
                            extra={
                                "error": str(e)[:200],
                                "consecutive_failures": self._ws_consecutive_heal_failures,
                                "next_sleep_sec": min(
                                    interval_sec
                                    * (2 ** self._ws_consecutive_heal_failures),
                                    float(self._SELF_HEAL_MAX_INTERVAL_SEC),
                                ),
                            },
                        )
                except Exception:
                    logger.exception("zerodha_ws_self_heal_tick_failed")
        finally:
            self._self_heal_running = False
            logger.info("zerodha_ws_self_heal_loop_stopped")

    def stop_ws_self_heal(self) -> None:
        self._self_heal_running = False

    # Rate-limit auto-login retries triggered from the self-heal loop so
    # we don't spawn 100 Playwright runs if Kite stays unresponsive.
    _last_auto_login_trigger_at: float = 0.0
    _AUTO_LOGIN_MIN_GAP_SEC = 300  # 5 minutes between auto-login attempts

    async def _maybe_trigger_auto_login_when_token_missing(self) -> None:
        """Self-heal helper: when the DB token is missing/dead AND the
        admin has configured auto-login + enabled it, kick off a fresh
        Playwright login. Rate-limited to one attempt every 5 minutes.

        This is what makes the system self-healing across restarts —
        previously the admin had to manually click "Login to Kite"
        whenever the token died mid-day.
        """
        try:
            import time as _time
            now = _time.monotonic()
            if now - self._last_auto_login_trigger_at < self._AUTO_LOGIN_MIN_GAP_SEC:
                return
            # Lazy import to avoid circular dependency at module load.
            from app.services.zerodha_auto_login import zerodha_auto_login
            if not await zerodha_auto_login.is_enabled():
                return
            self._last_auto_login_trigger_at = now
            logger.info("zerodha_ws_self_heal_triggering_auto_login")
            # Fire-and-forget — refresh_now is slow (~6 s Playwright run)
            # and we don't want to block the self-heal tick. The callback
            # will save the new token and kick off the WS via the existing
            # post-login flow.
            asyncio.create_task(
                zerodha_auto_login.refresh_now(triggered_by="self_heal_token_missing")
            )
        except Exception:
            logger.exception("zerodha_ws_self_heal_auto_login_trigger_failed")

    def get_last_tick_age_sec(self, token: int | str) -> float | None:
        """Seconds since the last tick payload landed for this token.

        Returns
        -------
        float
            Age of the most recent tick in seconds (monotonic delta).
        None
            We have never received a tick for this token in the current
            process lifetime, OR the token is unparseable.

        Used by the order validator to reject orders when an instrument's
        live feed is stale — catches late market opens, mid-session
        halts, and exchange feed outages that the static market-hours
        config doesn't know about (e.g. 28-May 2026 MCX opened at 17:00
        IST instead of 09:00; orders went through at zero/stale prices
        for 8 hours costing the admin real money).
        """
        try:
            tok = int(token)
        except (TypeError, ValueError):
            return None
        payload = self.ticks_by_token.get(tok)
        if not payload:
            return None
        received_at = payload.get("received_at")
        if received_at is None:
            return None
        import time as _t
        return _t.monotonic() - float(received_at)

    def get_exchange_ts_age_sec(self, token: int | str) -> float | None:
        """Seconds since the EXCHANGE generated this token's last packet.

        Unlike `get_last_tick_age_sec` (which measures when WE received the
        frame, and is therefore fooled by the stale snapshot Kite resends on
        every (re)subscribe of a CLOSED-session instrument), this reads the
        exchange's own `exchange_timestamp`. A snapshot of a closed market
        carries the PREVIOUS session's timestamp, so this reports a large age
        even when the frame just landed — the reliable "is this session
        actually live" signal the order validator uses to reject pre-open /
        holiday-evening-session / mid-halt orders.

        Returns None when we have no tick OR the tick carries no usable
        exchange_timestamp — the caller then falls back to the received_at
        gate, so behaviour is never harder than before.
        """
        try:
            tok = int(token)
        except (TypeError, ValueError):
            return None
        payload = self.ticks_by_token.get(tok)
        if not payload:
            return None
        try:
            ets_f = float(payload.get("exchange_timestamp") or 0)
        except (TypeError, ValueError):
            return None
        if ets_f <= 0:
            return None
        # `exchange_timestamp` is Unix epoch seconds; so is time.time().
        return time.time() - ets_f

    def get_ws_pool_info(self) -> dict[str, Any]:
        """Return current WebSocket pool status for admin diagnostics."""
        with self._ticker_lock:
            connections = []
            for entry in self._tickers:
                connections.append({
                    "label": entry.get("label", "?"),
                    "connected": entry.get("connected", False),
                    "connecting": entry.get("connecting", False),
                    "api_key": entry.get("api_key", ""),
                    "tokens_count": len(entry.get("tokens", set())),
                    "capacity": self.MAX_TOKENS_PER_WS,
                    "last_close_reason": entry.get("last_close_reason", ""),
                })
            return {
                "total_connections": len(self._tickers),
                "total_tokens_subscribed": sum(len(e.get("tokens", set())) for e in self._tickers),
                "connections": connections,
            }


# Singleton
zerodha = ZerodhaService()
