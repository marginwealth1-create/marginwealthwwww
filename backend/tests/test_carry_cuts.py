"""EOD carry-forward keeps what the funds cover instead of closing everything.

    pytest -q backend/tests/test_carry_cuts.py
"""

from decimal import Decimal as D

from app.services.position_service import plan_carry_cuts


def test_stock_keeps_the_quantity_the_fund_covers():
    # ₹1L fund, 500 shares @1000 bought at 5x (₹1L locked, nothing free).
    # Overnight needs 1x = ₹5L → extra ₹4L. Keep ₹1L worth = 100 shares.
    items = [("A", D(500), D(1), D(400000), D(100000), D(0))]
    assert plan_carry_cuts(items, D(400000), D(0)) == {"A": D(400)}


def test_losing_position_keeps_less():
    # Same, but price fell to 950: equity ₹75k → 78 shares at 1x.
    items = [("A", D(500), D(1), D(400000), D(100000), D(-25000))]
    cut = plan_carry_cuts(items, D(400000), D(0))["A"]
    assert cut == D(422)
    kept = D(500) - cut
    assert kept * 950 <= 75000


def test_fno_cuts_whole_lots():
    # 10 lots × 65, extra ₹10k/lot overnight, ₹2k/lot locked, ₹25k free.
    items = [("F", D(650), D(65), D(100000), D(20000), D(0))]
    # each lot cut frees 10k + 2k = 12k; short by 75k → 7 lots (84k)
    assert plan_carry_cuts(items, D(100000), D(25000)) == {"F": D(455)}


def test_worst_position_cut_first_and_the_rest_untouched():
    items = [
        ("LOSER", D(100), D(1), D(50000), D(10000), D(-5000)),
        ("WINNER", D(100), D(1), D(50000), D(10000), D(3000)),
    ]
    # short 20k; each LOSER share frees 500+100-50 = 550 → 37 shares
    assert plan_carry_cuts(items, D(100000), D(80000)) == {"LOSER": D(37)}


def test_fits_already_cuts_nothing():
    items = [("A", D(10), D(1), D(5000), D(1000), D(0))]
    assert plan_carry_cuts(items, D(5000), D(6000)) == {}


def test_position_that_frees_nothing_is_closed_in_full():
    # Loss bigger than margin + requirement: cutting it can't help → close.
    items = [("A", D(100), D(1), D(1000), D(1000), D(-5000))]
    assert plan_carry_cuts(items, D(1000), D(0)) == {"A": D(100)}
