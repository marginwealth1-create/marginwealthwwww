"""fetch_instruments must not stall callers when Kite can't be reached.

With no Zerodha token, the worker that won the Redis lock raised BEFORE the
cooldown was recorded, and every other worker polled Redis for 5 s per
exchange. User search walks three exchanges, so it took 15 s on every call —
long enough that the terminal's auto-select was cancelled by the watchlist's
2 s refetch before it could land, leaving the chart and order panel empty.

    pytest -q backend/tests/test_zerodha_instruments_unavailable.py
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.services import zerodha_service as zs


@pytest.fixture
def fake_redis(monkeypatch):
    """In-memory stand-in for the three Redis helpers fetch_instruments uses,
    shared between service instances the way real workers share Redis."""
    store: dict = {}

    async def cache_get(key):
        return store.get(key)

    async def cache_set(key, value, ttl_sec=None):
        store[key] = value

    async def idempotency_check_and_set(key, ttl_sec=3600):
        if key in store:
            return False
        store[key] = "1"
        return True

    monkeypatch.setattr(zs, "cache_get", cache_get)
    monkeypatch.setattr(zs, "cache_set", cache_set)
    monkeypatch.setattr(zs, "idempotency_check_and_set", idempotency_check_and_set)
    return store


def _worker_without_token():
    """A fresh service instance = a separate worker's in-process state."""
    svc = type(zs.zerodha)()

    async def no_token():
        raise RuntimeError("Zerodha is not authenticated. Connect from admin panel.")

    svc._kite_with_token = no_token
    return svc


def test_sibling_worker_does_not_wait_for_a_fetch_that_is_never_coming(fake_redis):
    async def run() -> float:
        holder, sibling = _worker_without_token(), _worker_without_token()
        with pytest.raises(RuntimeError):
            await holder.fetch_instruments("NSE")  # wins the lock, finds no token
        t0 = time.monotonic()
        assert await sibling.fetch_instruments("NSE") == []
        return time.monotonic() - t0

    elapsed = asyncio.run(run())
    # The regression was a 5 s poll per exchange here.
    assert elapsed < 0.4, f"sibling worker waited {elapsed:.2f}s"


def test_lock_holder_records_its_own_cooldown(fake_redis):
    async def run() -> float:
        worker = _worker_without_token()
        with pytest.raises(RuntimeError):
            await worker.fetch_instruments("MCX")
        t0 = time.monotonic()
        assert await worker.fetch_instruments("MCX") == []
        return time.monotonic() - t0

    assert asyncio.run(run()) < 0.1
