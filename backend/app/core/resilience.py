"""Transient-error retry helpers for Redis and MongoDB.

These decorators wrap idempotent I/O calls so a single transient failure
(network blip, connection-pool exhaustion, replica-set election in flight)
does not bubble up as a user-visible 500. They are deliberately narrow:

  * They retry ONLY on a curated list of transient exception classes.
  * They do NOT retry on application-level errors (validation, business
    rules, ``InsufficientFundsError``, etc.). Those propagate immediately.
  * They never change a function's return value or signature — a wrapped
    function behaves identically to the original on the success path.

The ``redis-py`` and ``motor`` drivers already perform low-level connection
retries. These decorators are the *application-level* safety net for the
cases where the driver gives up after exhausting its own retry budget.

Usage:

    from app.core.resilience import redis_retry, mongo_retry

    @redis_retry()
    async def cache_get(key: str) -> Any | None:
        ...

    @mongo_retry()
    async def get_open_positions(user_id: str) -> list[Position]:
        ...

DO NOT apply these decorators to non-idempotent operations (e.g. wallet
debits, order placement) without separate idempotency keys — a retry after
a partial server-side success would double-execute. The matching engine,
wallet service, and risk enforcer all have their own dedup paths
(Redis SETNX claims, audit-log idempotency) and should stay outside this
helper.
"""

from __future__ import annotations

import asyncio
import logging
import random
from functools import wraps
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ── Transient exception buckets ─────────────────────────────────────────
def _redis_transient_excs() -> tuple[type[BaseException], ...]:
    """Resolve Redis transient exception classes lazily so this module
    imports cleanly even if redis-py is missing in a slim test env."""
    classes: list[type[BaseException]] = []
    try:
        from redis.exceptions import (  # type: ignore
            BusyLoadingError,
            ConnectionError as RedisConnectionError,
            TimeoutError as RedisTimeoutError,
        )

        classes.extend([RedisConnectionError, RedisTimeoutError, BusyLoadingError])
    except Exception:  # pragma: no cover
        pass
    # Always include the generic asyncio TimeoutError — it surfaces from
    # `asyncio.wait_for(...)` wrappers around Redis ops.
    classes.append(asyncio.TimeoutError)
    return tuple(classes)


def _mongo_transient_excs() -> tuple[type[BaseException], ...]:
    classes: list[type[BaseException]] = []
    try:
        from pymongo.errors import (  # type: ignore
            AutoReconnect,
            ConnectionFailure,
            NetworkTimeout,
            ServerSelectionTimeoutError,
            WriteConcernError,
        )

        classes.extend(
            [
                AutoReconnect,
                ConnectionFailure,
                NetworkTimeout,
                ServerSelectionTimeoutError,
                WriteConcernError,
            ]
        )
    except Exception:  # pragma: no cover
        pass
    classes.append(asyncio.TimeoutError)
    return tuple(classes)


# Cache the resolved tuples so we don't re-import on every call.
_REDIS_TRANSIENT = _redis_transient_excs()
_MONGO_TRANSIENT = _mongo_transient_excs()


# ── Generic retry-async decorator ───────────────────────────────────────
def retry_async(
    *,
    exceptions: tuple[type[BaseException], ...],
    attempts: int = 3,
    base_delay_sec: float = 0.05,
    max_delay_sec: float = 1.0,
    jitter: bool = True,
    name: str | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Return a decorator that retries an async function on listed errors.

    Parameters
    ----------
    exceptions:
        Tuple of exception classes that should trigger a retry. Anything
        not in this tuple propagates on the first occurrence.
    attempts:
        Total number of attempts (including the first). ``attempts=3``
        means up to two retries after the initial failure.
    base_delay_sec / max_delay_sec:
        Exponential backoff window. Each retry waits
        ``min(base * 2**i, max)`` seconds, optionally jittered.
    jitter:
        Add ±25% random jitter to the delay so concurrent callers don't
        retry in lockstep and re-overload the just-recovered service.
    name:
        Optional label used in logs. Defaults to the wrapped function's
        ``__qualname__``.
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        label = name or getattr(fn, "__qualname__", repr(fn))

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except exceptions as e:  # noqa: PERF203
                    last_exc = e
                    if attempt >= attempts:
                        logger.warning(
                            "retry_async_giving_up",
                            extra={
                                "fn": label,
                                "attempt": attempt,
                                "error": str(e),
                                "error_type": type(e).__name__,
                            },
                        )
                        raise
                    delay = min(base_delay_sec * (2 ** (attempt - 1)), max_delay_sec)
                    if jitter:
                        delay *= 0.75 + random.random() * 0.5
                    logger.info(
                        "retry_async_transient_failure",
                        extra={
                            "fn": label,
                            "attempt": attempt,
                            "next_delay_sec": round(delay, 3),
                            "error": str(e),
                            "error_type": type(e).__name__,
                        },
                    )
                    await asyncio.sleep(delay)
            # Unreachable — loop either returns or raises. Defensive only.
            assert last_exc is not None
            raise last_exc  # pragma: no cover

        return wrapper

    return decorator


# ── Pre-configured decorators ───────────────────────────────────────────
def redis_retry(
    *,
    attempts: int = 3,
    base_delay_sec: float = 0.05,
    max_delay_sec: float = 0.5,
    name: str | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Retry transient Redis errors (connection / timeout / busy-loading).

    Tight delays — Redis is in-memory and a real failure surfaces quickly.
    The cache hot-path can't tolerate seconds of latency, so cap at 500 ms
    total worst-case (50 + 100 + 200 ≈ 350 ms over 3 attempts).
    """
    return retry_async(
        exceptions=_REDIS_TRANSIENT,
        attempts=attempts,
        base_delay_sec=base_delay_sec,
        max_delay_sec=max_delay_sec,
        jitter=True,
        name=name,
    )


def mongo_retry(
    *,
    attempts: int = 3,
    base_delay_sec: float = 0.1,
    max_delay_sec: float = 2.0,
    name: str | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Retry transient MongoDB errors (auto-reconnect, replica-set election).

    Slightly more generous than ``redis_retry`` because a Mongo replica-
    set election can take 1-3 s to settle — we want callers to ride
    through it instead of erroring the user-facing request.
    """
    return retry_async(
        exceptions=_MONGO_TRANSIENT,
        attempts=attempts,
        base_delay_sec=base_delay_sec,
        max_delay_sec=max_delay_sec,
        jitter=True,
        name=name,
    )
