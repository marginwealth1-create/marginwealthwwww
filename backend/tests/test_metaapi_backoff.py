"""MetaAPI reconnect pacing.

The bug this pins down: the loop LOGGED a 900s backoff and then slept 60,
because the sleep re-capped the value. Retrying a rate-limited
subscribe/synchronize every minute is what kept the account wedged, so the
ladder is now a pure function with a test around it.

    pytest -q backend/tests/test_metaapi_backoff.py
"""

from __future__ import annotations

import inspect

import pytest

from app.services import metaapi_service as ms


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(1, 60), (2, 120), (3, 240), (4, 480), (5, 900), (9, 900)],
)
def test_ladder(failures, expected):
    assert ms.sync_backoff_sec(failures) == expected


def test_ladder_is_capped_at_a_quarter_hour():
    assert ms.sync_backoff_sec(50) == ms._SYNC_BACKOFF_MAX_SEC


def test_the_loop_sleeps_what_it_logged():
    """Guards the exact regression: a `min(..., 60)` on the sleep threw the
    sync ladder away, so the panel said 900 and it retried in 60."""
    src = inspect.getsource(ms.MetaApiFeed._run_loop)
    assert "await asyncio.sleep(delay)" in src
    assert "min(backoff, 60)" not in src


def test_sync_gets_more_time_than_a_connect_stage():
    """This account measured ~190s to synchronize; 60s could never pass."""
    assert ms._SYNC_TIMEOUT_SEC >= 300
    assert ms._SYNC_TIMEOUT_SEC > ms._CONNECT_TIMEOUT_SEC
