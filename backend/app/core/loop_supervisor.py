"""Background-loop crash supervisor.

Wraps long-running asyncio loops (`risk_enforcer_loop`, `pending_order_poller`,
`market_tick_loop`, etc.) so that an unexpected exception escaping the loop's
own ``try/except/finally`` block does NOT silently kill the loop for the rest
of the process lifetime.

Behaviour:
  * If the wrapped loop returns normally (e.g. because a `stop_*()` setter
    flipped the loop's `_running` flag to False during shutdown), the
    supervisor exits cleanly. No restart.
  * If the wrapped loop raises any exception other than ``CancelledError``,
    the supervisor logs it (with full traceback), sleeps for an
    exponential-backoff window, then re-invokes the factory to start a
    fresh coroutine instance.
  * ``CancelledError`` is propagated unchanged so FastAPI's lifespan
    shutdown logic (`task.cancel()` + `await task`) keeps working
    exactly as before.

This file deliberately adds NO business logic — it is pure plumbing. Each
wrapped loop's internal behaviour, intervals, side-effects and DB writes
are unchanged. Only the outermost crash boundary is hardened.
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


# Optional Prometheus counter — exposed on /metrics via the existing
# `prometheus_fastapi_instrumentator` integration. Counter is created
# lazily and tolerates the prometheus_client package being absent so
# this module is safe to import in test environments.
_restart_counter = None
try:  # pragma: no cover - optional dep
    from prometheus_client import Counter

    _restart_counter = Counter(
        "background_loop_restarts_total",
        "Number of times a supervised background loop crashed and was restarted",
        labelnames=("loop",),
    )
except Exception:  # pragma: no cover
    _restart_counter = None


LoopFactory = Callable[[], Awaitable[None]]


async def supervise(
    loop_name: str,
    factory: LoopFactory,
    *,
    initial_backoff_sec: float = 5.0,
    max_backoff_sec: float = 60.0,
    healthy_after_sec: float = 60.0,
) -> None:
    """Run ``factory()`` forever, restarting it on any non-Cancelled crash.

    Parameters
    ----------
    loop_name:
        Stable identifier used in logs, Sentry breadcrumbs and Prometheus
        labels. Use the same name as the underlying loop (e.g. ``"risk_enforcer"``).
    factory:
        Zero-arg callable returning a fresh coroutine each time it's
        invoked. Use ``functools.partial`` to bind interval / config args.
    initial_backoff_sec:
        Sleep before the first restart after a crash. Defaults to 5 s so
        we don't tight-loop on a hard failure (e.g. Mongo down).
    max_backoff_sec:
        Upper bound for the exponential backoff. Once a loop has been
        crashing repeatedly we cap retries at this interval and keep
        trying — the loop will eventually recover when the underlying
        dependency comes back.
    healthy_after_sec:
        If a crashed loop ran successfully for at least this long before
        crashing, reset the backoff to ``initial_backoff_sec``. Avoids
        slowly drifting toward ``max_backoff_sec`` from sporadic single
        failures separated by hours.
    """
    backoff = initial_backoff_sec
    while True:
        started_at = monotonic()
        try:
            await factory()
        except asyncio.CancelledError:
            # Lifespan shutdown -> propagate so the surrounding task
            # finishes cleanly. The wrapped loop's `finally` block has
            # already reset its `_running` flag.
            logger.info("supervised_loop_cancelled", extra={"loop": loop_name})
            raise
        except Exception as e:  # noqa: BLE001 — supervisor must catch ALL
            ran_for = monotonic() - started_at
            # If the loop was healthy for a while before crashing, treat
            # this as a fresh failure and reset the backoff window.
            if ran_for >= healthy_after_sec:
                backoff = initial_backoff_sec

            logger.exception(
                "supervised_loop_crashed",
                extra={
                    "loop": loop_name,
                    "ran_for_sec": round(ran_for, 2),
                    "next_retry_sec": backoff,
                    "error": str(e),
                },
            )
            if _restart_counter is not None:
                try:
                    _restart_counter.labels(loop=loop_name).inc()
                except Exception:  # pragma: no cover
                    pass

            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                # Shutdown signalled while we were sleeping between
                # restarts — exit cleanly.
                logger.info(
                    "supervised_loop_cancelled_during_backoff",
                    extra={"loop": loop_name},
                )
                raise

            backoff = min(backoff * 2.0, max_backoff_sec)
            continue
        else:
            # Clean return — usually shutdown via the loop's own
            # `_running = False` setter. Don't restart.
            logger.info(
                "supervised_loop_exited_cleanly",
                extra={"loop": loop_name, "ran_for_sec": round(monotonic() - started_at, 2)},
            )
            return
