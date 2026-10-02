"""FastAPI dependency factory for sliding-window rate limiting.

Use as a dependency on routes that need stricter limits than the default:

    @router.post("/login", dependencies=[Depends(rate_limit("auth"))])
"""

from __future__ import annotations

from typing import Literal

from fastapi import Depends, Request

from app.core.config import settings
from app.core.exceptions import RateLimitExceededError
from app.core.redis_client import sliding_window_check

LimitBucket = Literal["auth", "default", "trading"]


def _bucket_limit(bucket: LimitBucket) -> int:
    if bucket == "auth":
        return settings.RATE_LIMIT_AUTH_PER_MIN
    if bucket == "trading":
        return settings.RATE_LIMIT_TRADING_PER_MIN
    return settings.RATE_LIMIT_DEFAULT_PER_MIN


def _client_ip(request: Request) -> str:
    # Honour X-Forwarded-For (left-most) when behind LB; fallback to socket IP.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def rate_limit(bucket: LimitBucket = "default", *, by: Literal["ip", "user"] = "ip"):
    """Returns a FastAPI dependency that enforces a 60-second sliding window."""

    async def _dep(request: Request) -> None:
        if by == "user":
            user = getattr(request.state, "user", None)
            ident = str(user.id) if user else _client_ip(request)
        else:
            ident = _client_ip(request)
        key = f"rl:{bucket}:{ident}:{request.url.path}"
        allowed, count = await sliding_window_check(
            key, max_requests=_bucket_limit(bucket), window_sec=60
        )
        if not allowed:
            raise RateLimitExceededError(
                details={"bucket": bucket, "current": count, "limit": _bucket_limit(bucket)}
            )

    return Depends(_dep)
