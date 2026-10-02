"""Per-segment end-of-day cutoffs.

A forex session ends nowhere near an NSE one, so each settings row carries its
own cancel time. Blank means never — which is why the Indian rows are
backfilled with 00:00 rather than left empty.

    pytest -q backend/tests/test_eod_cutoff.py
"""

from __future__ import annotations

from datetime import datetime, time

import pytest

from app.services.eod_order_cleanup import (
    apply_cutoff_override,
    cutoff_reached,
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


def test_blank_means_the_segment_is_never_swept():
    # Forex and crypto relied on this before the setting existed; a blank must
    # not start cancelling their overnight orders.
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
