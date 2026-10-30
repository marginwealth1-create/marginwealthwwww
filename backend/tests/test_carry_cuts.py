"""EOD carry-forward: overnight margin, exit-side mark, FIFO carry plan,
sweep time maths.

    pytest -q backend/tests/test_carry_cuts.py
"""

from datetime import time
from decimal import Decimal as D

from app.services import market_data_service as mds
from app.services.position_service import (
    affordable_carry,
    carry_step,
    exit_mark,
    overnight_margin,
    plan_carry,
    rollover_fire_time,
)

TIMES = {"margin_calc_mode": "times", "overnight_leverage": 10, "overnight_margin_percentage": 100}
FIXED = {"margin_calc_mode": "fixed", "overnight_fixed_margin_per_lot": 15000, "overnight_margin_percentage": 0}


# ── overnight_margin ───────────────────────────────────────────────────────

def test_times_margin_is_notional_over_overnight_leverage():
    assert overnight_margin(TIMES, 100, D("1000"), 1) == D("10000.00")


def test_fixed_margin_is_per_lot_and_ignores_price():
    assert overnight_margin(FIXED, 130, D("99999"), 65) == D("30000.00")  # 2 lots


def test_usd_converted_only_in_times(monkeypatch):
    monkeypatch.setitem(mds._usd_inr_cache, "rate", 90.0)
    assert overnight_margin(TIMES, 1, D("100"), 1, is_usd=True) == D("900.00")
    assert overnight_margin(FIXED, 65, D("100"), 65, is_usd=True) == D("15000.00")


def test_short_position_qty_sign_does_not_matter():
    assert overnight_margin(TIMES, -100, D("1000"), 1) == D("10000.00")


# ── exit_mark ──────────────────────────────────────────────────────────────

def test_long_exits_at_bid_short_at_ask():
    q = {"bid": 99, "ask": 101}
    assert exit_mark(10, q, 100) == D(99)
    assert exit_mark(-10, q, 100) == D(101)


def test_crossed_or_missing_book_falls_back_ltp_then_mark_then_avg():
    assert exit_mark(10, {"bid": 105, "ask": 101}, 100) == D(100)
    assert exit_mark(10, {}, None, D("98"), D("90")) == D(98)
    assert exit_mark(10, {}, 0, None, D("90")) == D(90)


# ── plan_carry (items oldest first: key, qty, margin, square brokerage, step) ─

BOOK = [
    ("A", D(100), D(60000), D(1000), D(1)),  # oldest
    ("B", D(50), D(50000), D(500), D(1)),
    ("C", D(10), D(20000), D(0), D(1)),      # newest
]


def test_everything_fits_everything_carries():
    assert plan_carry(BOOK, D(130000)) == {"A": D(100), "B": D(50), "C": D(10)}


def test_shortfall_trims_the_oldest_and_carries_the_rest_in_full():
    # short 30k: A frees (60k − 1k brokerage) per 100 → keep 49, B / C full.
    assert plan_carry(BOOK, D(100000)) == {"A": D(49), "B": D(50), "C": D(10)}


def test_big_shortfall_squares_oldest_whole_and_boundary_partial():
    assert plan_carry(BOOK, D(20000)) == {"A": D(0), "B": D(0), "C": D(9)}


def test_square_brokerage_is_counted():
    no_brk = [("A", D(100), D(60000), D(0), D(1))] + BOOK[1:]
    assert plan_carry(no_brk, D(100000))["A"] == D(50)  # vs 49 with brokerage


def test_more_funds_from_floating_profit_keeps_more():
    assert plan_carry(BOOK, D(110000))["A"] > plan_carry(BOOK, D(100000))["A"]


def test_crypto_trims_in_fractional_steps():
    items = [("X", D("0.5"), D(10000), D(0), D("0.01"))]
    assert plan_carry(items, D(2345)) == {"X": D("0.11")}


def test_carry_step():
    assert carry_step(0.01, 1) == D("0.01")   # crypto / forex
    assert carry_step(1, 65) == D(1)          # F&O: whole units
    assert carry_step(0, 1) == D(1)


# ── executor fallback: plan was off, carry what the cash covers ─────────────

def test_unfunded_flip_keeps_what_cash_covers_not_zero():
    # 30-Oct J4MP6E BANKNIFTY 54600CE: 30 qty, ₹274.38 intraday lock, ₹2,730.30
    # overnight, ₹2,401.93 free after the other squares → 28 carry (was: all squared).
    assert affordable_carry(D(30), D("2401.93"), D("274.38"), D("2730.30"), D(1)) == D(28)


def test_fallback_squares_when_nothing_fits_and_never_keeps_all():
    assert affordable_carry(D(30), D(0), D(10), D(3000), D(1)) == D(0)
    assert affordable_carry(D(30), D(999999), D(0), D(3000), D(1)) == D(29)  # still unfunded → at least one step goes


# ── sweep time ─────────────────────────────────────────────────────────────

def test_fire_time_divmod_and_clamp():
    assert rollover_fire_time(time(15, 30), 12) == time(15, 42)
    assert rollover_fire_time(time(15, 55), 10) == time(16, 5)
    assert rollover_fire_time(time(23, 55), 10) == time(23, 59)
