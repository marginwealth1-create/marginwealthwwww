"""No new positions in a contract's final days — closing still allowed.

Segment setting `expiryNoNewTradeDays`. Calendar days, expiry day included:
3 on a Thursday expiry blocks Tue, Wed and Thu, so Monday is the last day a
position can be opened.

    pytest -q backend/tests/test_expiry_no_new_trade.py
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.order_validator import expiry_window_blocks_new_trade

THURSDAY_EXPIRY = date(2026, 9, 24)


@pytest.mark.parametrize(
    ("today", "blocked"),
    [
        (date(2026, 9, 18), False),  # a week out
        (date(2026, 9, 21), False),  # Monday — last day to open
        (date(2026, 9, 22), True),   # Tuesday — window opens
        (date(2026, 9, 23), True),   # Wednesday
        (date(2026, 9, 24), True),   # expiry day itself
    ],
)
def test_three_day_window_covers_expiry_day_and_the_two_before(today, blocked):
    assert (
        expiry_window_blocks_new_trade(expiry=THURSDAY_EXPIRY, today=today, days=3)
        is blocked
    )


def test_zero_days_turns_the_rule_off():
    assert (
        expiry_window_blocks_new_trade(
            expiry=THURSDAY_EXPIRY, today=date(2026, 9, 24), days=0
        )
        is False
    )


def test_one_day_means_expiry_day_only():
    assert (
        expiry_window_blocks_new_trade(
            expiry=THURSDAY_EXPIRY, today=date(2026, 9, 23), days=1
        )
        is False
    )
    assert (
        expiry_window_blocks_new_trade(
            expiry=THURSDAY_EXPIRY, today=date(2026, 9, 24), days=1
        )
        is True
    )


def test_instruments_without_an_expiry_are_untouched():
    # Cash equity, forex, crypto — nothing to count down to.
    assert (
        expiry_window_blocks_new_trade(expiry=None, today=date(2026, 9, 24), days=3)
        is False
    )


def test_an_already_expired_contract_is_not_this_rule_s_business():
    # The contract-live guard rejects those; this rule must not also claim
    # them, or the user gets the wrong reason.
    assert (
        expiry_window_blocks_new_trade(
            expiry=THURSDAY_EXPIRY, today=date(2026, 9, 25), days=3
        )
        is False
    )


def test_bad_types_never_raise_inside_validation():
    assert (
        expiry_window_blocks_new_trade(expiry="2026-09-24", today=date.today(), days=3)
        is False
    )
