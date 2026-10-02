"""Wipe today's stored Zerodha access token + feed cache so you can log in fresh.

Use this when you've turned OFF auto-login and want a clean slate to do a
MANUAL Kite login (new account / new token). It does NOT touch your saved
`apiKey` / `apiSecret` — those are needed to generate the next login URL —
and it does NOT re-enable auto-login.

What it clears:
  1. `zerodha_settings` (every account row): accessToken, refreshToken,
     tokenExpiry, isConnected, wsStatus, wsLastError, lastConnected.
  2. Redis feed cache: `mdlive:*`, `mdlast:*`, and the `zerodha:*` failover /
     ws-pool / feed-warm keys the ticker publishes.

It also reports whether auto-login is still enabled, so a scheduler won't
silently re-fill the token behind your manual login.

Run from the backend folder:

    cd /opt/marginwealth/backend
    source .venv/bin/activate
    python -m scripts.reset_zerodha_token

Idempotent — safe to re-run. Restart the backend afterwards so the in-memory
ticker drops its live socket:

    sudo systemctl restart marginwealth-backend
"""

from __future__ import annotations

import asyncio
import logging

from app.core.database import close_database, init_database
from app.core.redis_client import cache_delete_pattern, close_redis, init_redis
from app.models.zerodha_auto_login import ZerodhaAutoLogin
from app.models.zerodha_settings import WsStatus, ZerodhaSettings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reset_zerodha_token")

# Redis key patterns the ticker / feed leader publishes. mdlive/mdlast carry
# per-token prices; the zerodha:* keys hold failover status, feed-warm ts and
# the ws-pool snapshot. All are safe to drop — they self-repopulate on the
# next live tick after a fresh login.
_REDIS_PATTERNS = ("mdlive:*", "mdlast:*", "zerodha:*")


async def main() -> None:
    await init_database()
    print("MongoDB connected")

    # ── 1. Null the stored token on every account row ────────────────
    rows = await ZerodhaSettings.find_all().to_list()
    if not rows:
        print("No zerodha_settings rows found — nothing to clear in DB.")
    for s in rows:
        had_token = bool(s.accessToken)
        s.accessToken = None
        s.refreshToken = None
        s.tokenExpiry = None
        s.isConnected = False
        s.lastConnected = None
        s.wsStatus = WsStatus.DISCONNECTED
        s.wsLastError = None
        await s.save()
        api_key_note = "apiKey kept" if s.apiKey else "no apiKey saved"
        print(
            f"  account {s.account_index}: token cleared "
            f"({'had a token' if had_token else 'already empty'}, {api_key_note})"
        )

    # ── 2. Drop the Redis feed cache ─────────────────────────────────
    try:
        await init_redis()
    except Exception as e:  # noqa: BLE001
        print(f"Redis init failed — skipped cache flush: {str(e)[:120]}")
    else:
        total = 0
        for pat in _REDIS_PATTERNS:
            n = await cache_delete_pattern(pat)
            total += n
            print(f"  redis {pat:12} → {n} keys deleted")
        print(f"  redis total: {total} keys deleted")
        await close_redis()

    # ── 3. Warn if auto-login could re-fill the token ────────────────
    auto_rows = await ZerodhaAutoLogin.find_all().to_list()
    enabled = [a for a in auto_rows if a.is_enabled]
    if enabled:
        accounts = ", ".join(str(a.account_index) for a in enabled)
        print(
            f"\n  WARNING: auto-login is STILL ENABLED for account(s) {accounts} "
            f"— the scheduler may re-login and overwrite your manual token. "
            f"Disable it from the admin Zerodha page before logging in manually."
        )
    else:
        print("\n  auto-login is disabled on all accounts — good, manual token will stick.")

    print(
        "\nDone. Now restart the backend so the ticker drops its live socket, "
        "then do a fresh manual login from the admin Zerodha page:"
        "\n  sudo systemctl restart marginwealth-backend"
    )
    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
