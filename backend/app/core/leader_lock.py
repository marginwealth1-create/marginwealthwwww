"""Redis-backed leader election for background loops.

When the backend runs as multiple uvicorn workers (or replicated across
several instances), every worker currently spins up *its own* copy of
each background loop — pending-order poller, risk enforcer, expiry
cleanup, P&L sharing scheduler, etc. They all hammer the same MongoDB
collections every tick, and while the matching engine has its own
cross-worker dedup so duplicate fills don't land, the duplicated *scan*
is pure waste: at 4 workers you do 4× the read load for zero benefit.

This module gives each loop a Redis-based distributed leader lock
(``SET key value NX EX ttl``). Only the worker holding the lock runs
the loop body; the others poll for the lock and stand by. If the leader
dies (process crash, network partition, machine reboot), the TTL
expires and a standby worker picks up within ``poll_sec`` seconds.

Logic is deliberately scoped to scheduling — the wrapped loops, their
internals, intervals, side-effects and DB writes are unchanged. Whether
a loop runs serially on one worker or duplicated on N workers makes no
difference to its per-iteration semantics, only to who pays the cost.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Awaitable, Callable

from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)


# Renew the lock at 1/3 of its TTL — gives 2 missed renewals of headroom
# before the lock would actually expire under the leader's nose.
_RENEW_FRACTION = 3


class LeaderLock:
    """A single named lease against a Redis key.

    Each instance carries a unique random ``token`` so we can safely
    delete *only* the lease we actually own — never another worker's.
    """

    def __init__(self, key: str, *, ttl_sec: int = 30) -> None:
        self.key = key
        self.ttl_sec = max(5, int(ttl_sec))
        self.token = secrets.token_hex(16)
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    async def try_acquire(self) -> bool:
        """SET key token NX EX ttl. Returns True iff we just acquired."""
        try:
            ok = await get_redis().set(
                self.key, self.token, ex=self.ttl_sec, nx=True
            )
        except Exception as e:
            logger.warning(
                "leader_lock_acquire_error",
                extra={"key": self.key, "error": str(e)},
            )
            return False
        self._held = bool(ok)
        return self._held

    async def renew(self) -> bool:
        """Extend the TTL **only if we still own the lease**.

        Lua makes the GET+EXPIRE atomic so a worker that lost the lease
        between the two ops can't accidentally extend a successor's
        lease back to itself. Returns False on any failure — caller
        treats that as lost-leadership and stops the loop.
        """
        lua = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('EXPIRE', KEYS[1], ARGV[2])
        end
        return 0
        """
        try:
            res = await get_redis().eval(lua, 1, self.key, self.token, str(self.ttl_sec))
            return bool(int(res))
        except Exception as e:
            logger.warning(
                "leader_lock_renew_error",
                extra={"key": self.key, "error": str(e)},
            )
            return False

    async def release(self) -> None:
        """Atomic DEL — never blow away another worker's lease."""
        if not self._held:
            return
        lua = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        try:
            await get_redis().eval(lua, 1, self.key, self.token)
        except Exception as e:  # pragma: no cover
            logger.warning(
                "leader_lock_release_error",
                extra={"key": self.key, "error": str(e)},
            )
        self._held = False


LoopFactory = Callable[[], Awaitable[None]]


async def leader_elected(
    name: str,
    factory: LoopFactory,
    *,
    lock_key: str | None = None,
    ttl_sec: int = 30,
    poll_sec: float = 5.0,
    local_admit: Callable[[], bool] | None = None,
    local_release: Callable[[], None] | None = None,
) -> None:
    """Run ``factory()`` only on the worker holding the leader lock.

    Behaviour:
      * Polls for the lock every ``poll_sec`` while not leader.
      * Once leader, runs ``factory()`` concurrently with a renewer.
      * If the renewer ever sees a lost lease, cancels ``factory()`` so
        the loop's ``finally`` block runs (resetting its ``_running``
        flag) and we cleanly hand off to the next leader.
      * On a clean ``factory()`` exit (typically the lifespan shutdown
        flipping the loop's ``_running`` flag), we release the lease
        and return — the caller's supervise() sees a clean exit and
        won't restart us.
      * On ``CancelledError`` (lifespan ``task.cancel()``), we release
        and re-raise so the ASGI shutdown completes promptly.

    ``local_admit`` / ``local_release`` are an OPTIONAL per-process admission
    gate (used by risk sharding to spread shards across workers). ``local_admit``
    is called SYNCHRONOUSLY before attempting the Redis lock: returning False
    means "this worker may not run this loop right now" (e.g. it already runs
    another risk shard, or it is the feed leader) → we skip the acquire and
    poll again. When it returns True it has reserved a local slot, so we MUST
    ``local_release`` it on every exit path where we don't end up holding the
    lock (failed acquire, lost/clean leadership, cancellation). Because asyncio
    is single-threaded, the synchronous admit-reserve is race-free between the
    sibling shard loops of the same worker.
    """
    key = lock_key or f"leader:{name}"
    lock = LeaderLock(key, ttl_sec=ttl_sec)
    try:
        while True:
            # Per-process admission gate (e.g. one risk shard per worker).
            if local_admit is not None and not local_admit():
                await asyncio.sleep(poll_sec)
                continue

            acquired = await lock.try_acquire()
            if not acquired:
                # Didn't win the cluster lock → free the local slot we just
                # reserved so a sibling shard loop on this worker can use it.
                if local_release is not None:
                    local_release()
                # Quiet info — don't spam logs every poll cycle.
                await asyncio.sleep(poll_sec)
                continue

            logger.info("leader_acquired", extra={"loop": name, "key": key})
            try:
                clean_exit = await _run_as_leader(name, factory, lock)
            finally:
                try:
                    await lock.release()
                except Exception:  # pragma: no cover
                    pass
                if local_release is not None:
                    local_release()
                logger.info("leader_released", extra={"loop": name, "key": key})

            if clean_exit:
                # Loop exited via its own ``_running = False`` setter —
                # treat the same as a normal supervised exit and stop.
                return
            # Lost leadership mid-flight; sleep briefly and retry so we
            # don't immediately re-acquire on the same process.
            await asyncio.sleep(poll_sec)
    except asyncio.CancelledError:
        try:
            await lock.release()
        except Exception:  # pragma: no cover
            pass
        if local_release is not None:
            local_release()
        raise


async def _run_as_leader(
    name: str,
    factory: LoopFactory,
    lock: LeaderLock,
) -> bool:
    """Drive ``factory()`` while keeping the lease alive.

    Returns True if the factory exited cleanly (loop's ``_running``
    flag set to False), False if leadership was lost mid-flight.
    """
    lost_event = asyncio.Event()
    sleep_sec = max(1.0, lock.ttl_sec / _RENEW_FRACTION)

    async def renewer() -> None:
        try:
            while not lost_event.is_set():
                try:
                    await asyncio.sleep(sleep_sec)
                except asyncio.CancelledError:
                    return
                if not await lock.renew():
                    logger.warning(
                        "leader_lock_lost",
                        extra={"loop": name, "key": lock.key},
                    )
                    lost_event.set()
                    return
        except asyncio.CancelledError:
            return

    renewer_task = asyncio.create_task(renewer(), name=f"{name}_renewer")
    factory_task = asyncio.create_task(factory(), name=f"{name}_factory")
    waiter_task = asyncio.create_task(lost_event.wait(), name=f"{name}_lost_waiter")

    clean = True
    try:
        done, pending = await asyncio.wait(
            {factory_task, waiter_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if factory_task in done:
            # Loop returned (clean shutdown). Surface any unexpected error
            # to the supervise() wrapper so it can log + decide on restart.
            try:
                factory_task.result()
            except asyncio.CancelledError:  # pragma: no cover
                clean = False
            except Exception:
                clean = False
                raise
        else:
            # Lost leadership before the factory finished — cancel it so
            # the loop's ``finally`` runs and resets its ``_running`` flag.
            clean = False
            factory_task.cancel()
            try:
                await factory_task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
    finally:
        lost_event.set()
        for t in (renewer_task, waiter_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
    return clean
