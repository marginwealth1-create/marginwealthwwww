"""Per-IP WebSocket connection limiter.

Tracks the number of concurrent WebSocket connections per client IP
across the whole cluster using a Redis counter. Each WS handler calls
``acquire(ip)`` before ``ws.accept()`` and ``release(ip)`` in its
``finally`` block.

The cap is intentionally generous (default 100 per IP) so legitimate
users behind shared NAT exits aren't penalised, while still blocking a
single rogue client from opening thousands of sockets and exhausting
the worker's pool. Tune via the ``WS_MAX_CONNECTIONS_PER_IP`` setting
without touching this file.

Fail-open semantics: if Redis is briefly unavailable the limiter
allows the connection through. Auth and protocol-level checks still
happen downstream so this is no security regression — we'd rather not
block a real user during a Redis blip.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)


# 1 hour TTL — defensive: if a process crashes mid-decrement, the
# counter would otherwise leak forever and slowly lock that IP out.
_KEY_TTL_SEC = 60 * 60


def _key(ip: str) -> str:
    return f"ws:conn_count:{ip}"


def client_ip(ws: Any) -> str:
    """Best-effort client IP extraction from a Starlette/FastAPI WebSocket.

    Honors ``X-Forwarded-For`` (first hop) when present so we count by
    the real client behind a reverse proxy. Falls back to the direct
    peer address. Returns ``"unknown"`` when neither is available so
    the limiter can decide to fail open.
    """
    try:
        xff = ws.headers.get("x-forwarded-for") if hasattr(ws, "headers") else None
    except Exception:
        xff = None
    if xff:
        # First entry is the originating client per RFC 7239 / common practice.
        return xff.split(",", 1)[0].strip()
    try:
        if ws.client and ws.client.host:
            return str(ws.client.host)
    except Exception:  # pragma: no cover
        pass
    return "unknown"


async def acquire(ip: str, *, max_per_ip: int) -> bool:
    """Atomically increment per-IP counter, returning False if over cap.

    Lua keeps INCR + cap-check + auto-decrement-on-overflow + EXPIRE all
    in one round-trip so two concurrent connections from the same IP
    can't both squeeze past the cap on a race.
    """
    if not ip or ip == "unknown":
        return True  # fail open — better than blocking a real user
    if max_per_ip <= 0:
        return True  # disabled
    lua = """
    local v = redis.call('INCR', KEYS[1])
    if tonumber(v) == 1 then
        redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
    end
    if tonumber(v) > tonumber(ARGV[1]) then
        redis.call('DECR', KEYS[1])
        return 0
    end
    return v
    """
    try:
        res = await get_redis().eval(
            lua, 1, _key(ip), str(max_per_ip), str(_KEY_TTL_SEC)
        )
        allowed = int(res) > 0
        if not allowed:
            logger.info(
                "ws_limiter_rejected",
                extra={"ip": ip, "max_per_ip": max_per_ip},
            )
        return allowed
    except Exception as e:
        logger.warning(
            "ws_limiter_acquire_failed_fail_open",
            extra={"ip": ip, "error": str(e)},
        )
        return True


async def release(ip: str) -> None:
    """Decrement the counter, never going below zero.

    Safe to call even if ``acquire`` was never called (or returned
    False) — the floor-at-zero guard makes it a no-op in those cases.
    """
    if not ip or ip == "unknown":
        return
    lua = """
    local v = redis.call('GET', KEYS[1])
    if v then
        local n = tonumber(v)
        if n and n > 0 then
            redis.call('DECR', KEYS[1])
        end
    end
    return 1
    """
    try:
        await get_redis().eval(lua, 1, _key(ip))
    except Exception:  # pragma: no cover
        pass
