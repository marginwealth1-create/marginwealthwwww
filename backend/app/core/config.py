"""Application configuration loaded from environment variables.

All settings are validated by Pydantic at startup; invalid config fails fast
rather than crashing later in a request path.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────
    APP_NAME: str = "MarginWealth"
    # Public support address surfaced by /user/support and the apk/web
    # "Contact support" affordance. Seeded into the `platform.support_email`
    # PlatformSetting on first boot (admins can then edit it in Platform
    # Settings). Empty = not configured, and the UI simply hides the email
    # option rather than showing a dead address — which is why this has no
    # invented default. Previously the seed hard-coded a stale
    # `support@marginwealth.com` straight into the DB.
    SUPPORT_EMAIL: str = ""
    APP_ENV: Literal["development", "staging", "production"] = "development"
    APP_DEBUG: bool = False
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_BASE_URL: str = "http://localhost:8000"

    # ── MongoDB ──────────────────────────────────────────────────────
    MONGODB_URL: str = "mongodb://localhost:27017/marginwealth"
    MONGODB_DB_NAME: str = "marginwealth"
    MONGODB_REPLICA_SET: str = ""
    MONGODB_MAX_POOL_SIZE: int = 100
    MONGODB_MIN_POOL_SIZE: int = 10

    # ── Feed/loop process isolation ─────────────────────────────────────
    # When True (DEFAULT) this process starts the `leader:feed` bundle —
    # upstream feeds (MetaAPI/Zerodha/Infoway/Binance) + the 0.1 s tick_loop
    # + feed_subscribe_listener + pending_order_poller + Zerodha WS self-heal
    # + auto-login + subscription-trim. On a single process this is today's
    # behaviour. To ISOLATE the feed from HTTP (so a CPU-pegged HTTP worker
    # can never starve the tick_loop → frozen prices), set this to False on
    # the gunicorn HTTP service and run ONE dedicated process (uvicorn,
    # separate port, no external traffic) with RUN_FEED_LOOP=true — that
    # process wins `leader:feed` and drives the feed on its own event loop.
    RUN_FEED_LOOP: bool = True

    # ── Risk-loop sharding (horizontal scale of SL/TP/stop-out) ──────────
    # Number of shards the risk_enforcer is split across. DEFAULT 1 =
    # single-worker, co-located on `leader:feed`, prices read in-process —
    # BYTE-FOR-BYTE today's behaviour. Set > 1 (and <= worker count /
    # WEB_CONCURRENCY) ONLY when `risk_enforcer_tick_overrun` is sustained
    # > 1-2 s: users are then partitioned by user_id hash across N shard
    # workers (`leader:risk:shard:0..N-1`), each reading prices from the
    # leader's Redis `mdlive` snapshot. Flip back to 1 to instantly revert.
    # Change ONLY when the market is closed (a shard-count change re-hashes
    # users for one tick; the atomic-claim dedup covers that transition).
    RISK_SHARDS: int = 1

    # Risk-loop tick interval (seconds). The risk_enforcer sweep (bracket
    # SL/TP + margin stop-out) runs every RISK_TICK_SEC on each shard.
    # 0.5 s proved too aggressive for the current book + single-box infra:
    # the busiest shard (a whale user holding ~70 of ~120 open positions —
    # sharding can't split ONE user) couldn't finish its sweep inside 0.5 s,
    # so risk_enforcer_tick_overrun fired continuously. 1.0 s gives every
    # shard 2× the budget AND halves the per-second Mongo/Redis/CPU load
    # (fewer contention spikes), while still reacting to a stop-out / SL / TP
    # within ~1 s — 5× faster than the legacy 5 s loop and imperceptible to
    # users. Lower back toward 0.5 only once the busiest shard's total_ms
    # sits comfortably under the chosen interval.
    RISK_TICK_SEC: float = 1.0

    # Market tick-fanout interval (seconds) — how often the feed leader's
    # `market_tick_loop` overlays subscribed tokens, refreshes `mdlive`, and
    # publishes CHANGED prices to the `/ws/marketdata` fanout. Lower = snappier
    # tick-by-tick price movement on the web/APK AND fresher `mdlive` for the
    # risk enforcer (better stop-out/SL timing). Default 0.1 s (100ms ≈ 10 Hz)
    # — the SAFE value: at ~1100+ subscribed Zerodha tokens the overlay churn
    # at 0.07 pushed the single-core feed to ~75-85% even off-market, leaving
    # too little weekday headroom. 100ms still looks tick-by-tick to the eye,
    # and the loop only PUBLISHES on a price change so a flat book costs
    # nothing. Tunable via env if a box has spare CPU to burn.
    MARKET_TICK_SEC: float = 0.1

    # ── Feed process split (multi-core) ──────────────────────────────
    # The feed leader is ONE asyncio event loop = ONE CPU core (Python GIL).
    # To use a second core, run TWO feed processes split by upstream:
    #   FEED_GROUP=main   → Zerodha WS pool (NSE/BSE/NFO/MCX) + poller + failover
    #                       + all Zerodha helpers. Holds `leader:feed`.
    #   FEED_GROUP=global → Binance + Infoway + MetaAPI (crypto/forex/metals)
    #                       only. Holds a SEPARATE `leader:feed:global` lock.
    #   FEED_GROUP=all    → single process runs everything (DEFAULT — today's
    #                       behaviour, byte-identical; keeps single-box/dev
    #                       deploys unchanged).
    # The split is self-scoping: each process's in-process `_state` only holds
    # its own upstream's ticks, so tick_loop / poller naturally act on just
    # their group; `mdlive` + `market:tick` writes never overlap (numeric vs
    # symbol tokens); the pending-order dedup lock prevents any double-fire.
    # Risk enforcement is unaffected (RISK_SHARDS>1 runs on the HTTP backend
    # workers reading `mdlive`, which carries BOTH groups). Revert to a single
    # feed by setting FEED_GROUP=all and stopping the global service.
    FEED_GROUP: str = "all"

    # ── Upper / lower circuit (daily price band) enforcement ─────────
    # Master switch for the circuit gate in the order validator. OFF by
    # default so this ships dark and prod behaviour is byte-identical until
    # deliberately enabled.
    CIRCUIT_BANDS_ENABLED: bool = False
    # Rollout safety valve. With bands ENABLED but ENFORCE off, a would-be
    # circuit rejection is ALLOWED through and logged at WARNING instead —
    # so an operator can watch `circuit_block` lines for a session and
    # confirm the band data is sane before real orders start bouncing.
    # Flip True to actually reject.
    CIRCUIT_ENFORCE: bool = False

    # ── Redis ────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    # Bumped from 50 → 300 after the market_tick_loop started raising
    # `ConnectionError: Too many connections` once the Zerodha WS pool
    # crossed ~1500 subscribed tokens. Every 250 ms tick publishes one
    # message per token over pub/sub, plus the order validator + cache
    # helpers all pull from the same pool. Empirical headroom needed
    # ≈ token_count / 20 + steady ~50 for HTTP path; 300 leaves slack
    # for spikes during option-chain expansion.
    REDIS_MAX_CONNECTIONS: int = 300

    # ── WebSocket limits ─────────────────────────────────────────────
    # Hard cap on simultaneous WebSocket connections per client IP,
    # enforced via Redis (see app/core/ws_limiter.py). Generous default
    # so users on shared NAT exits / corporate proxies aren't penalised;
    # set to 0 to disable the limiter entirely.
    WS_MAX_CONNECTIONS_PER_IP: int = 100
    # Per-connection cap on instrument-token subscriptions on the
    # `/ws/marketdata` socket. Each subscribed token costs one slot in
    # the in-process ``MarketTickHub`` fanout map and one entry in the
    # upstream Zerodha / Infoway ticker — a power-user holding 200+
    # symbols in one watchlist would otherwise multiply tick-publish
    # work across the whole worker pool. 70 fits a typical user's full
    # watchlist + the option-chain expansion they have open at once,
    # with headroom; bigger requests get rejected with an explicit
    # `subscription_limit` error frame so the frontend can prompt the
    # user to unsubscribe something first.
    WS_MAX_SUBSCRIPTIONS_PER_CONN: int = 70

    # ── JWT ──────────────────────────────────────────────────────────
    # Refresh-token TTL widened from 7 → 30 days so the mobile app keeps
    # users logged in for a month (matches Zerodha / Groww / Upstox UX).
    # The token rotates on every refresh so a fresh login resets the
    # 30-day window — a daily-active user effectively never sees a login
    # screen unless they sign out explicitly or revoke from another device.
    JWT_SECRET: SecretStr = Field(default=SecretStr("change-me"))
    JWT_ALGORITHM: str = "HS256"
    # Access token bumped from 15 → 1440 min (24 h) so the silent-refresh
    # cycle fires at most once a day instead of every 15 min. User-flagged
    # symptom on the installed PWA: "30 din set hai fir bhi logout ho
    # raha hai". Cause was a transient refresh failure (PWA resume before
    # network reattaches, backend deploy mid-suspend, etc.) which the
    # frontend interceptor used to convert into a hard /login redirect.
    # Longer access lifetime + revised frontend retry semantics together
    # close that hole. Backend revocation is still instantaneous because
    # /auth/refresh + the JTI allow-list rotate on every refresh, so a
    # logout from another device kills the next refresh attempt — the
    # access token only lives until its own TTL after that, which 24 h
    # is still well within the security budget for a personal trading
    # app.
    JWT_ACCESS_TTL_MIN: int = 1440
    JWT_REFRESH_TTL_DAYS: int = 30

    # ── Admin extra security ─────────────────────────────────────────
    ADMIN_API_KEY: SecretStr = Field(default=SecretStr("change-me-admin"))
    ADMIN_IP_WHITELIST: str = ""

    # ── CORS ─────────────────────────────────────────────────────────
    CORS_USER_ORIGIN: str = "http://localhost:3000"
    CORS_ADMIN_ORIGIN: str = "http://localhost:3001"

    # ── Public backend URL (used by OAuth callback URLs etc.) ────────
    # Override in production to your actual API hostname, e.g.
    # https://api.marginwealth.com — Kite redirects the user's browser here.
    BACKEND_PUBLIC_URL: str = "http://localhost:8000"

    # ── Rate limit ───────────────────────────────────────────────────
    RATE_LIMIT_AUTH_PER_MIN: int = 5
    RATE_LIMIT_DEFAULT_PER_MIN: int = 100
    RATE_LIMIT_TRADING_PER_MIN: int = 300

    # ── External APIs ────────────────────────────────────────────────
    ANGEL_ONE_API_KEY: str = ""
    ANGEL_ONE_CLIENT_CODE: str = ""
    ANGEL_ONE_CLIENT_PIN: str = ""
    ANGEL_ONE_TOTP_SECRET: str = ""
    ZERODHA_API_KEY: str = ""
    ZERODHA_API_SECRET: str = ""
    # AES-256-GCM key for encrypting the Zerodha auto-login credentials at rest.
    # 32 raw bytes, base64-encoded. Generate with:
    #   python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
    # If unset, the auto-login service refuses to save credentials so a
    # misconfigured deploy can't accidentally store plaintext.
    ZERODHA_CREDS_KEY: SecretStr = Field(default=SecretStr(""))
    PRICE_FEED_PROVIDER: Literal["mock", "angel_one", "zerodha"] = "mock"

    # Infoway — global forex / crypto / metals / energy / stocks / indices feed.
    INFOWAY_API_KEY: SecretStr = Field(default=SecretStr(""))
    # Optional dedicated key for the `common` (forex / metals / energy) channel.
    # Each Infoway key allows only ~1-2 concurrent WS connections, so crypto and
    # gold/forex can't always share one key. When set, the common channel uses
    # THIS key while crypto/stock stay on INFOWAY_API_KEY — giving gold its own
    # connection slot. Empty → common falls back to INFOWAY_API_KEY (old behaviour).
    INFOWAY_API_KEY_COMMON: SecretStr = Field(default=SecretStr(""))
    INFOWAY_AUTO_CONNECT: bool = True

    # Divinepay UPI pay-in gateway (single platform merchant account, shared
    # with our other project — reuse the same live sk_ key). Server-side only.
    DIVINEPAY_API_KEY: SecretStr = Field(default=SecretStr(""))
    DIVINEPAY_BASE_URL: str = "https://divinepay.us.cc"
    INFOWAY_DEFAULT_CRYPTO: str = "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT"
    # NOTE: keep this list pure forex pairs (6-char major/minor crosses). Don't
    # add USDINR here — Indian-rupee derivatives belong on the NSE/BSE CDS
    # segment, not the international Infoway forex bucket the user-side
    # "Forex" chip surfaces.
    INFOWAY_DEFAULT_FOREX: str = "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF,NZDUSD"
    # Spot precious metals + common energy contracts (Infoway uses the same
    # ticker style — XAUUSD = gold/USD, XAGUSD = silver/USD, USOIL = WTI).
    INFOWAY_DEFAULT_METALS: str = "XAUUSD,XAGUSD,XPTUSD,XPDUSD"
    INFOWAY_DEFAULT_ENERGY: str = "USOIL,UKOIL,NATGAS"
    # ── Binance crypto feed (free public WS, no API key) ─────────────────
    # When True, CRYPTO symbols get their live price from Binance
    # (wss://stream.binance.com) instead of Infoway — matching the Binance
    # chart users see and giving a smoother/faster tick. Forex / metals /
    # energy stay on Infoway regardless. If empty, BINANCE_CRYPTO_SYMBOLS
    # falls back to the INFOWAY_DEFAULT_CRYPTO list (same USDT-pair names).
    BINANCE_CRYPTO_FEED: bool = False
    BINANCE_CRYPTO_SYMBOLS: str = ""
    # ── MetaAPI (MetaTrader) feed for forex / metals / indices / commodities ──
    # When True, those segments get their live price from a connected MT4/MT5
    # account via metaapi.cloud instead of Infoway (crypto stays on Binance).
    # Infoway remains the automatic FALLBACK when MetaAPI has no tick for a
    # symbol. METAAPI_SYMBOLS defaults to the INFOWAY_DEFAULT_FOREX/METALS/
    # ENERGY/INDICES lists; METAAPI_SYMBOL_MAP handles per-broker name
    # differences ("US30:DJ30,USOIL:WTI,EURUSD:EURUSD.raw").
    METAAPI_FEED: bool = False
    METAAPI_TOKEN: SecretStr = Field(default=SecretStr(""))
    METAAPI_ACCOUNT_ID: str = ""
    METAAPI_REGION: str = ""            # optional MetaAPI region (e.g. "new-york")
    METAAPI_SYMBOLS: str = ""           # blank → forex+metals+energy+indices defaults
    METAAPI_SYMBOL_MAP: str = ""        # "PLATFORM:MT,..." per-broker symbol aliases
    # International equities subscribe through Infoway's dedicated `stock`
    # WebSocket business channel (US / HK / A-share coverage). Indices
    # share the `common` channel with forex/metals/energy. Both are
    # treated as explicit allowlists by `_classify_infoway_code` so an
    # AAPL-shaped string can't be mis-routed as a forex pair.
    # Defaults cover the most-traded US tickers + global indices; admin
    # can override via env without code changes.
    INFOWAY_DEFAULT_STOCKS: str = "AAPL,MSFT,GOOGL,AMZN,TSLA,NVDA,META,NFLX"
    INFOWAY_DEFAULT_INDICES: str = "SPX500,NAS100,US30,UK100,DE40,JPN225,HK50"

    # ── Email / SMS ──────────────────────────────────────────────────
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: SecretStr = Field(default=SecretStr(""))
    SMTP_FROM: str = "no-reply@marginwealth.com"
    SMTP_TLS: bool = True
    SMS_PROVIDER: Literal["mock", "twilio", "msg91"] = "mock"
    SMS_API_KEY: SecretStr = Field(default=SecretStr(""))
    SMS_SENDER_ID: str = "STPFX"

    # ── S3 ───────────────────────────────────────────────────────────
    S3_BUCKET: str = ""
    S3_REGION: str = "ap-south-1"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: SecretStr = Field(default=SecretStr(""))
    S3_ENDPOINT_URL: str = ""

    # ── Celery ───────────────────────────────────────────────────────
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # ── Observability ────────────────────────────────────────────────
    SENTRY_DSN: str = ""
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # ── Web Push (VAPID) ─────────────────────────────────────────────
    # RFC 8292 application-server identity. The browser hands these
    # back at subscribe time and the push service uses them to verify
    # that the message originated from us. Generate ONCE per
    # deployment and keep the private key secret; rotating it
    # invalidates every existing subscription so users have to
    # re-subscribe (i.e. re-grant notification permission).
    # Quick-gen:
    #     python -m scripts.generate_vapid_keys
    # leave blank in dev — push send is a no-op until both are set.
    VAPID_PUBLIC_KEY: str = ""
    VAPID_PRIVATE_KEY: SecretStr = Field(default=SecretStr(""))
    VAPID_SUBJECT: str = "mailto:admin@marginwealth.com"

    # ── Seed ─────────────────────────────────────────────────────────
    SEED_SUPER_ADMIN_EMAIL: str = "admin@marginwealth.com"
    SEED_SUPER_ADMIN_PASSWORD: SecretStr = Field(default=SecretStr("Admin@123"))
    SEED_SUPER_ADMIN_MOBILE: str = "9999999999"
    RUN_SEED_ON_STARTUP: bool = True

    # ── Trading ──────────────────────────────────────────────────────
    DEFAULT_TIMEZONE: str = "Asia/Kolkata"
    MARKET_OPEN_TIME: str = "09:15"
    MARKET_CLOSE_TIME: str = "15:30"
    MUHURAT_OPEN_TIME: str = "18:15"
    MUHURAT_CLOSE_TIME: str = "19:15"

    # ── White-label branding ─────────────────────────────────────────
    # Master kill-switch for the white-label branding subsystem. When
    # False (default), the new schema fields on User exist but no code
    # path reads/writes them, the `/api/v1/branding/*` endpoints (added
    # in Phase 2) return 503, and the frontend BrandingProvider falls
    # back to default platform branding. Flip to True only after Phase
    # 1 is observed clean for ≥ 24h. Keeps prod 0-second reversible.
    BRANDING_ENABLED: bool = False
    # Public IPv4 the platform answers on — admins point their custom
    # domain's A records here for DNS verification (Phase 4). Empty
    # default keeps the verify endpoint a no-op when unset.
    PLATFORM_PUBLIC_IP: str = ""

    # ── Multi-tenant login isolation ─────────────────────────────────
    # When a login lands on a branded admin's custom domain, gate it so
    # ONLY accounts that belong to that admin can authenticate there:
    #   * the admin who owns the domain (logging into their own site),
    #   * any user whose `assigned_admin_id` is that admin (the admin's
    #     whole downstream pool — clients/dealers/masters/brokers),
    #   * any SUPER_ADMIN (platform owner, allowed on every domain).
    # A user of admin X can no longer log in via admin Y's domain.
    #
    # The platform's OWN main domain (anything that does NOT resolve to
    # an admin's custom_domain) stays unrestricted — the super-admin
    # main login accepts every account, exactly as today. Transferring
    # a user to another admin (which rewrites `assigned_admin_id`) moves
    # their login scope automatically.
    #
    # Requires BRANDING_ENABLED (needs domain→admin resolution). Master
    # OFF by default so this ships dark; prod behaviour is byte-identical
    # until deliberately enabled.
    LOGIN_TENANT_ISOLATION: bool = False
    # Rollout safety valve. With isolation ON but ENFORCE OFF, a
    # cross-tenant login is ALLOWED but logged at WARNING so operators
    # can see who WOULD be blocked (and catch any mis-assigned user)
    # before turning on hard rejection. Flip True to actually reject.
    LOGIN_TENANT_ISOLATION_ENFORCE: bool = False

    # ── Bonus Management ─────────────────────────────────────────────
    # Master flag for the whole Bonus feature (templates, grants, credit
    # pool that absorbs losses, wager/expiry). OFF by default so the
    # feature ships fully inert: all /bonuses routes 503, no engine /
    # wallet / risk behaviour changes, no background loop. Flip True to
    # activate. 0-second reversible.
    BONUSES_ENABLED: bool = False

    # ── Outage-proof boolean parsing ─────────────────────────────────
    # A mistyped boolean env var (the real incident: `METAAPI_FEED=fasle`)
    # made pydantic's strict bool_parsing raise at Settings() construction,
    # which crashed EVERY worker on boot → full platform outage from a single
    # typo in an OPTIONAL feature flag. This validator recognises the normal
    # truthy/falsy spellings and, for anything it can't parse, falls back to
    # the field's OWN default (so the feature just stays at its safe default)
    # with a loud ERROR log telling the operator to fix the .env — the
    # platform stays UP. Applied to the feature/ops toggles, NOT to
    # security-critical guards (APP_DEBUG intentionally included: a typo there
    # → default False, which is the production-safe value).
    @field_validator(
        "APP_DEBUG",
        "RUN_FEED_LOOP",
        "INFOWAY_AUTO_CONNECT",
        "BINANCE_CRYPTO_FEED",
        "METAAPI_FEED",
        "SMTP_TLS",
        "LOG_JSON",
        "RUN_SEED_ON_STARTUP",
        "BRANDING_ENABLED",
        "LOGIN_TENANT_ISOLATION",
        "LOGIN_TENANT_ISOLATION_ENFORCE",
        "CIRCUIT_BANDS_ENABLED",
        "CIRCUIT_ENFORCE",
        "BONUSES_ENABLED",
        mode="before",
    )
    @classmethod
    def _lenient_bool(cls, v, info):
        if isinstance(v, bool) or v is None:
            return v
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "on", "t", "y"}:
            return True
        if s in {"0", "false", "no", "off", "f", "n", ""}:
            return False
        # Unrecognised (typo) → this field's declared default, never a crash.
        import logging

        default = cls.model_fields[info.field_name].default
        logging.getLogger(__name__).error(
            "config_invalid_bool field=%s value=%r -> using default %r "
            "(feature left at safe default; FIX THE .env line)",
            info.field_name,
            v,
            default,
        )
        return default

    # ─────────────────────────────────────────────────────────────────
    @field_validator("MONGODB_URL")
    @classmethod
    def _validate_mongo_url(cls, v: str) -> str:
        if not v.startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGODB_URL must start with mongodb:// or mongodb+srv://")
        return v

    @field_validator("REDIS_URL")
    @classmethod
    def _validate_redis_url(cls, v: str) -> str:
        if not v.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("REDIS_URL must start with redis://, rediss://, or unix://")
        return v

    # ── Production fail-closed secrets guard ─────────────────────────
    @model_validator(mode="after")
    def _enforce_production_secrets(self) -> "Settings":
        """Refuse to BOOT in production if any security-critical secret is
        still a placeholder/dev value, the seed super-admin password is the
        well-known default, debug is on, or MongoDB has no credentials.

        Development / staging are intentionally exempt so local work keeps
        using the convenient defaults. This is the safety net that stops a
        misconfigured deploy from shipping with:
          • a forgeable JWT_SECRET (anyone could mint a valid admin token),
          • a publicly-known ADMIN_API_KEY,
          • the Admin@123 seed password,
          • an unauthenticated MongoDB (anyone on the network could read
            wallets / overwrite the super-admin password hash),
          • debug tracebacks leaking internals to clients.
        """
        if self.APP_ENV != "production":
            return self

        problems: list[str] = []

        jwt_secret = self.JWT_SECRET.get_secret_value()
        if len(jwt_secret) < 32 or "change" in jwt_secret.lower():
            problems.append(
                "JWT_SECRET is weak/placeholder — use a random secret of ≥32 chars"
            )

        api_key = self.ADMIN_API_KEY.get_secret_value()
        if len(api_key) < 24 or "change" in api_key.lower():
            problems.append(
                "ADMIN_API_KEY is weak/placeholder — use a random key of ≥24 chars"
            )

        seed_pw = self.SEED_SUPER_ADMIN_PASSWORD.get_secret_value()
        if seed_pw in {"Admin@123", "", "change-me"}:
            problems.append(
                "SEED_SUPER_ADMIN_PASSWORD is the default — set a strong unique password"
            )

        if self.APP_DEBUG:
            problems.append("APP_DEBUG must be false in production")

        # An authenticated Mongo URI always carries credentials as
        # `user:pass@host`. No '@' ⇒ no auth ⇒ the DB is open to anyone who
        # can reach the port. (Atlas / any authed deploy passes this.)
        if "@" not in self.MONGODB_URL:
            problems.append(
                "MONGODB_URL has no credentials — enable MongoDB auth (SCRAM) in production"
            )

        if problems:
            raise ValueError(
                "Refusing to start: insecure production config detected:\n  - "
                + "\n  - ".join(problems)
                + "\nFix these in the production .env (see backend/SECURITY.md)."
            )
        return self

    @property
    def admin_ip_whitelist_set(self) -> set[str]:
        return {ip.strip() for ip in self.ADMIN_IP_WHITELIST.split(",") if ip.strip()}

    @property
    def cors_allowed_origins(self) -> list[str]:
        """Flatten both CORS_USER_ORIGIN and CORS_ADMIN_ORIGIN, splitting
        comma-separated values so each origin lands as its own list entry
        (Starlette's CORSMiddleware compares origins as exact strings — a
        single list entry like `"https://a,https://b"` matches nothing)."""
        raw = f"{self.CORS_USER_ORIGIN},{self.CORS_ADMIN_ORIGIN}"
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def zerodha_redirect_url(self) -> str:
        """Canonical Kite-Connect callback URL. Always lives on the backend
        because the request_token exchange happens server-side."""
        base = (self.BACKEND_PUBLIC_URL or "http://localhost:8000").rstrip("/")
        return f"{base}/api/v1/admin/zerodha/callback"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
