"""No new price for N seconds → no new trades on that instrument.

A 60-second version of this was removed in July 2026 for false-blocking during
feed hiccups, so the rules that keep it from doing that again are the point of
these tests: it is off by default at 0, silent when there is nothing to judge,
and inactive while the feed is still warming up after a reconnect.

    pytest -q backend/tests/test_stale_feed_block.py
"""

from __future__ import annotations

from app.services.order_validator import stale_feed_block_reason


def test_blocks_when_the_price_has_not_moved_past_the_limit():
    msg = stale_feed_block_reason(
        symbol="CRUDEOIL", price_age_sec=45, threshold_sec=30, feed_warm_age_sec=600
    )
    assert msg is not None
    assert "CRUDEOIL" in msg and "45s" in msg


def test_allows_a_price_inside_the_limit():
    assert (
        stale_feed_block_reason(
            symbol="X", price_age_sec=29.9, threshold_sec=30, feed_warm_age_sec=600
        )
        is None
    )


def test_zero_turns_the_feature_off():
    assert (
        stale_feed_block_reason(
            symbol="X", price_age_sec=9999, threshold_sec=0, feed_warm_age_sec=600
        )
        is None
    )


def test_no_price_age_never_blocks():
    # Nothing to judge by — an instrument with no exchange timestamp at all
    # must not be blocked by this rule.
    assert (
        stale_feed_block_reason(
            symbol="X", price_age_sec=None, threshold_sec=30, feed_warm_age_sec=600
        )
        is None
    )


def test_warm_up_after_a_reconnect_suspends_the_block():
    # Right after a deploy every instrument looks quiet; freezing the whole
    # platform for a minute is the failure this grace period exists to avoid.
    assert (
        stale_feed_block_reason(
            symbol="X", price_age_sec=300, threshold_sec=30, feed_warm_age_sec=5
        )
        is None
    )


def test_block_resumes_once_the_grace_period_has_passed():
    msg = stale_feed_block_reason(
        symbol="X", price_age_sec=300, threshold_sec=30, feed_warm_age_sec=61
    )
    assert msg is not None


def test_unknown_feed_warm_time_still_blocks():
    # No warm-up marker (Redis cold) must not become a way to bypass the rule.
    msg = stale_feed_block_reason(
        symbol="X", price_age_sec=120, threshold_sec=30, feed_warm_age_sec=None
    )
    assert msg is not None


# ── MetaAPI symbols (forex / metals / indices / US stocks) ───────────
#
# These carry no exchange packet time, so until now the guard above could
# never fire for them: our own writer re-stamps `ts` every tick whether or
# not the price moved. The provider's own tick time rides as `feed_ts`.

import datetime as _dt

from app.services.metaapi_service import MetaApiFeed


class _FakeTerminalState:
    def __init__(self, price):
        self._price = price

    def price(self, symbol=None):  # noqa: ARG002 - mirrors the SDK signature
        return self._price


class _FakeConn:
    def __init__(self, price):
        self.terminal_state = _FakeTerminalState(price)


def _feed_with(price):
    feed = MetaApiFeed()
    feed._conn = _FakeConn(price)
    return feed


def test_tick_carries_the_brokers_own_time():
    when = _dt.datetime(2026, 9, 23, 6, 0, tzinfo=_dt.timezone.utc)
    tick = _feed_with({"bid": 1.1, "ask": 1.2, "time": when}).get_tick("EURUSD")
    assert tick is not None
    assert tick["feed_ts"] == when.timestamp()


def test_tick_without_a_time_is_unknown_not_fresh():
    """No provider timestamp must read as "can't tell" (guard skipped), never
    as "just now" — which is what our own write time would have claimed."""
    tick = _feed_with({"bid": 1.1, "ask": 1.2}).get_tick("EURUSD")
    assert tick is not None
    assert tick["feed_ts"] is None
    assert stale_feed_block_reason(
        symbol="EURUSD", price_age_sec=None, threshold_sec=30, feed_warm_age_sec=None
    ) is None


def test_a_frozen_metaapi_stream_blocks_the_trade():
    """The failure this closes: the stream stops, our loop keeps republishing
    the same price with a fresh `ts`, and the trade goes through at a price
    that is minutes old."""
    when = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=90)
    tick = _feed_with({"bid": 1.1, "ask": 1.2, "time": when}).get_tick("EURUSD")
    age = _dt.datetime.now(_dt.timezone.utc).timestamp() - tick["feed_ts"]
    assert age > 30
    msg = stale_feed_block_reason(
        symbol="EURUSD", price_age_sec=age, threshold_sec=30, feed_warm_age_sec=None
    )
    assert msg is not None and "EURUSD" in msg
