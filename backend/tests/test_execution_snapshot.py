"""What the market looked like at a fill — the input to slippage and latency.

This runs inside the fill path, so the rule is: never raise, never block a
trade. A junk quote yields None, not an exception.

    pytest -q backend/tests/test_execution_snapshot.py
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.services.matching_engine import execution_snapshot


def _now_ms() -> float:
    return time.time() * 1000


def test_markup_is_the_difference_between_fill_and_reference():
    snap = execution_snapshot(
        quote={"ts": _now_ms()},
        order_created_at=None,
        reference_ltp=Decimal("100.00"),
        fill_price=Decimal("100.25"),
    )
    assert snap["markup"] == Decimal("0.25")


def test_markup_is_negative_when_the_fill_is_below_reference():
    snap = execution_snapshot(
        quote={},
        order_created_at=None,
        reference_ltp=Decimal("100.00"),
        fill_price=Decimal("99.80"),
    )
    assert snap["markup"] == Decimal("-0.20")


def test_our_own_write_stamp_is_not_treated_as_tick_age():
    # `ts` is set when the snapshot is written, so it is always ~0 ms old.
    # Trusting it would report every fill as landing on a fresh price.
    snap = execution_snapshot(
        quote={"ts": _now_ms() - 2500},
        order_created_at=None,
        reference_ltp=None,
        fill_price=None,
    )
    assert snap["tick_age_ms"] is None


def test_fill_latency_counts_from_when_the_order_was_accepted():
    created = datetime.now(timezone.utc) - timedelta(seconds=3)
    snap = execution_snapshot(
        quote={}, order_created_at=created, reference_ltp=None, fill_price=None
    )
    assert 2500 <= snap["fill_latency_ms"] <= 5000


def test_naive_timestamps_are_treated_as_utc():
    # Mongo hands datetimes back without tzinfo; subtracting one from an aware
    # now() raises, and this must not take a fill down with it.
    created = datetime.utcnow() - timedelta(seconds=2)
    snap = execution_snapshot(
        quote={}, order_created_at=created, reference_ltp=None, fill_price=None
    )
    assert snap["fill_latency_ms"] is not None
    assert snap["fill_latency_ms"] >= 1000


def test_junk_never_raises_inside_the_fill_path():
    snap = execution_snapshot(
        quote={"ts": "not-a-number"},
        order_created_at="not-a-date",
        reference_ltp=Decimal("0"),
        fill_price=Decimal("5"),
    )
    assert snap == {"tick_age_ms": None, "fill_latency_ms": None, "markup": None}


def test_missing_quote_is_tolerated():
    snap = execution_snapshot(
        quote={}, order_created_at=None, reference_ltp=None, fill_price=None
    )
    assert snap["tick_age_ms"] is None
    assert snap["markup"] is None


def test_tick_age_uses_the_exchange_packet_time_not_our_write_time():
    # `ts` is stamped when we write the snapshot, so it always looks fresh;
    # only the exchange's own timestamp says how old the price really was.
    snap = execution_snapshot(
        quote={"ts": _now_ms(), "exchange_timestamp": (time.time() - 4)},
        order_created_at=None,
        reference_ltp=None,
        fill_price=None,
    )
    assert 3500 <= snap["tick_age_ms"] <= 5000


def test_exchange_timestamp_in_milliseconds_is_understood_too():
    snap = execution_snapshot(
        quote={"exchange_timestamp": _now_ms() - 2000},
        order_created_at=None,
        reference_ltp=None,
        fill_price=None,
    )
    assert 1500 <= snap["tick_age_ms"] <= 3000


def test_parked_orders_report_no_fill_latency():
    # A LIMIT order placed ten minutes ago and triggered now waited; it was
    # not slow to execute.
    created = datetime.now(timezone.utc) - timedelta(minutes=10)
    snap = execution_snapshot(
        quote={},
        order_created_at=created,
        reference_ltp=None,
        fill_price=None,
        immediate=False,
    )
    assert snap["fill_latency_ms"] is None
