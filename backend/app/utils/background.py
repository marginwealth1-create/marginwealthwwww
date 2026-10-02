"""Fire-and-forget background task helper.

A bare ``asyncio.create_task()`` has two footguns we keep tripping over:

  1. The event loop holds only a WEAK reference to the task, so one with no
     other reference can be garbage-collected mid-flight ("Task was destroyed
     but it is pending!"). We pin a strong ref in a module-level set until it
     finishes.
  2. An exception inside the coroutine surfaces as an "exception was never
     retrieved" warning at GC time. We attach a done-callback that logs it.

Use this for pure SIDE EFFECTS that must not add latency to the request that
triggered them — WebSocket / admin-event fan-out, audit-log writes, P&L
sharing pings, push notifications. NEVER use it for anything the HTTP response
depends on (wallet writes, position updates, fill persistence): those must
stay awaited inline so a failure is surfaced to the caller.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger(__name__)

# Strong references to in-flight tasks so the loop's weak-ref doesn't let the
# GC reap them before they complete. Cleared by the done-callback.
_tasks: set[asyncio.Task[Any]] = set()


def fire_and_forget(coro: Coroutine[Any, Any, Any], *, label: str = "") -> None:
    """Schedule ``coro`` to run AFTER the current request returns, swallowing
    (but logging) any error. No-ops gracefully when there's no running loop."""
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        # No running event loop (sync context / certain test setups). Close
        # the coroutine so Python doesn't warn about it never being awaited.
        coro.close()
        return

    _tasks.add(task)

    def _done(t: asyncio.Task[Any]) -> None:
        _tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.warning(
                    "background task failed%s: %r",
                    f" [{label}]" if label else "",
                    exc,
                )

    task.add_done_callback(_done)
