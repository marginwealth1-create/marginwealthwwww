from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.models.pnl_sharing import SettlementCadence
from app.services.pnl_sharing_service import compute_period_bounds

IST = ZoneInfo("Asia/Kolkata")


def _ist(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST)


def test_daily_bounds_midweek():
    ref = _ist(2026, 5, 18, 14, 30)  # Mon 14:30 IST
    start, end = compute_period_bounds(SettlementCadence.DAILY, ref)
    assert start == _ist(2026, 5, 18, 0, 0, 0).astimezone(ZoneInfo("UTC"))
    assert end == _ist(2026, 5, 18, 23, 59, 59).replace(microsecond=999999).astimezone(ZoneInfo("UTC"))


def test_weekly_bounds_midweek():
    # Wed 2026-05-20 → week is Mon 2026-05-18 to Sun 2026-05-24
    ref = _ist(2026, 5, 20, 10, 0)
    start, end = compute_period_bounds(SettlementCadence.WEEKLY, ref)
    assert start == _ist(2026, 5, 18, 0, 0, 0).astimezone(ZoneInfo("UTC"))
    assert end == _ist(2026, 5, 24, 23, 59, 59).replace(microsecond=999999).astimezone(ZoneInfo("UTC"))


def test_weekly_bounds_on_sunday():
    # Sun 2026-05-24 → same week as test above
    ref = _ist(2026, 5, 24, 23, 0)
    start, _end = compute_period_bounds(SettlementCadence.WEEKLY, ref)
    assert start == _ist(2026, 5, 18, 0, 0, 0).astimezone(ZoneInfo("UTC"))


def test_monthly_bounds():
    ref = _ist(2026, 5, 15, 12, 0)
    start, end = compute_period_bounds(SettlementCadence.MONTHLY, ref)
    assert start == _ist(2026, 5, 1, 0, 0, 0).astimezone(ZoneInfo("UTC"))
    assert end == _ist(2026, 5, 31, 23, 59, 59).replace(microsecond=999999).astimezone(ZoneInfo("UTC"))


def test_monthly_bounds_february_leap():
    # 2024 was a leap year
    ref = _ist(2024, 2, 10)
    start, end = compute_period_bounds(SettlementCadence.MONTHLY, ref)
    assert end == _ist(2024, 2, 29, 23, 59, 59).replace(microsecond=999999).astimezone(ZoneInfo("UTC"))


def test_naive_datetime_treated_as_ist():
    ref = datetime(2026, 5, 18, 14, 30)  # naive — assumed IST
    start, _end = compute_period_bounds(SettlementCadence.DAILY, ref)
    assert start == _ist(2026, 5, 18, 0, 0, 0).astimezone(ZoneInfo("UTC"))
