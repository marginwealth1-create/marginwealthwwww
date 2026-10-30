"""Per-segment end-of-day cutoffs.

A forex session ends nowhere near an NSE one, so each settings row carries its
own cancel time. Blank means never — which is why the Indian rows are
backfilled with 00:00 rather than left empty.

    pytest -q backend/tests/test_eod_cutoff.py
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from app.services.eod_order_cleanup import (
    apply_cutoff_override,
    cutoff_reached,
    order_expired,
    parse_cutoff,
)

AFTERNOON = datetime(2026, 9, 18, 16, 0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("15:30", time(15, 30)),
        ("00:00", time(0, 0)),
        ("23:59", time(23, 59)),
        ("", None),
        (None, None),
        ("junk", None),
        ("25:00", None),
        ("15:60", None),
    ],
)
def test_cutoff_parsing(raw, expected):
    assert parse_cutoff(raw) == expected


def test_blank_never_clears_brackets():
    # cutoff_reached now only drives the opt-in SL/TP clearing; a blank row's
    # ORDERS still expire at the market close (see default_cutoff below).
    assert cutoff_reached(parse_cutoff(""), AFTERNOON, None) is False


def test_sweeps_once_the_time_has_passed():
    assert cutoff_reached(time(15, 30), AFTERNOON, None) is True


def test_does_not_sweep_before_the_time():
    assert cutoff_reached(time(23, 30), AFTERNOON, None) is False


def test_only_once_per_day():
    assert cutoff_reached(time(15, 30), AFTERNOON, "20260918") is False
    # …and again the next day.
    assert cutoff_reached(time(15, 30), datetime(2026, 9, 19, 16, 0), "20260918") is True


def test_midnight_reproduces_the_original_sweep():
    # 00:00 means the first tick of a new IST day is already past the cutoff.
    assert cutoff_reached(time(0, 0), datetime(2026, 9, 18, 0, 0, 30), None) is True


# ── Panel edits live in the super-admin override, not the seed ──────────


def test_panel_value_overrides_the_seed():
    out = {"FOREX": time(0, 0)}
    apply_cutoff_override(out, "forex", "23:30")
    assert out["FOREX"] == time(23, 30)


def test_null_override_inherits_the_seed():
    # Every other netting field treats null as "inherit"; so does this one.
    out = {"NSE_EQ": time(0, 0)}
    apply_cutoff_override(out, "NSE_EQ", None)
    assert out["NSE_EQ"] == time(0, 0)


def test_unparseable_override_turns_the_sweep_off():
    out = {"NSE_EQ": time(0, 0)}
    apply_cutoff_override(out, "NSE_EQ", "")
    assert "NSE_EQ" not in out


def test_override_on_a_segment_with_no_seed_time():
    out: dict[str, time] = {}
    apply_cutoff_override(out, "CRYPTO", "21:00")
    assert out == {"CRYPTO": time(21, 0)}


# ── Parked orders expire at their market's close ─────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))


def _ist(day, hh, mm):  # Oct 2026: 29 Thu, 30 Fri, 31 Sat, 2 Nov Mon
    return datetime(2026, 10 if day > 2 else 11, day, hh, mm, tzinfo=IST)


def test_nse_order_placed_today_expires_at_the_1541_close_same_day():
    # The old sweep only expired orders from BEFORE today → they lived 1-2 days.
    placed = _ist(29, 10, 0)
    assert not order_expired("NFO_OPTION", placed, _ist(29, 15, 40), None)
    assert order_expired("NFO_OPTION", placed, _ist(29, 15, 41), None)
    assert order_expired("NSE_EQUITY", placed, _ist(29, 15, 42), None)


def test_order_placed_after_close_gets_the_next_session():
    placed = _ist(29, 16, 0)
    assert not order_expired("NSE_INDEX_OPTION_SELL", placed, _ist(30, 15, 0), None)
    assert order_expired("NSE_INDEX_OPTION_SELL", placed, _ist(30, 15, 42), None)


def test_friday_evening_order_survives_the_weekend():
    placed = _ist(30, 18, 0)
    assert not order_expired("NFO_FUTURE", placed, _ist(31, 16, 0), None)
    assert not order_expired("NFO_FUTURE", placed, _ist(2, 10, 0), None)
    assert order_expired("NFO_FUTURE", placed, _ist(2, 15, 42), None)


def test_mcx_expires_after_the_mcx_close():
    placed = _ist(29, 20, 0)
    assert not order_expired("MCX_FUTURE", placed, _ist(29, 23, 54), None)
    assert order_expired("MCX_FUTURE", placed, _ist(29, 23, 56), None)


@pytest.mark.parametrize("seg", ["FOREX", "CRYPTO_SPOT", "COMMODITIES", "STOCKS", "INDICES"])
def test_round_the_clock_markets_expire_after_midnight(seg):
    placed = _ist(29, 23, 0)
    assert not order_expired(seg, placed, _ist(29, 23, 59), None)
    assert order_expired(seg, placed, _ist(30, 0, 1), None)


def test_configured_time_wins_and_naive_utc_is_handled():
    placed_naive_utc = datetime(2026, 10, 29, 4, 30)  # 10:00 IST
    assert order_expired("NSE_EQUITY", placed_naive_utc, _ist(29, 15, 5), time(15, 0))
    assert not order_expired("NSE_EQUITY", placed_naive_utc, _ist(29, 15, 5), None)


def test_held_token_survives_a_browser_unsubscribe():
    from app.services import market_data_service as mds

    mds.hold_tokens("risk", {"424242"})
    mds.subscribe(["515151"])
    mds.unsubscribe(["424242", "515151"])  # last browser left both charts
    assert "424242" in mds._subscribed
    assert "515151" not in mds._subscribed
    mds.hold_tokens("risk", set())
    mds.unsubscribe(["424242"])
    assert "424242" not in mds._subscribed
