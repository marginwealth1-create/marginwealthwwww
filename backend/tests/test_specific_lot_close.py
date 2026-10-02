"""Specific-lot close (Active-tab per-fill Exit) — money math.

The Active-tab Exit realizes P&L against the TAPPED fill's own entry price
instead of the position average, and recomputes the surviving lots' average so
the total realized over the full close stays identical to avg-price accounting
(money is conserved — only the interim attribution changes).

These lock the two formulas used by `position_service.apply_fill` /
`matching_engine.execute_market_order` in the `cost_basis_override` path:

    realized     = (exit_price - basis) * closed_qty * sign
    remaining_avg = (cur_avg * Q - basis * closed_qty) / (Q - closed_qty)

`basis` = the tapped fill's entry price (override) instead of `cur_avg`.
"""

from decimal import Decimal

D = Decimal


def realized(exit_price, basis, closed_qty, sign):
    return (D(str(exit_price)) - D(str(basis))) * D(str(closed_qty)) * D(str(sign))


def remaining_avg(cur_avg, cur_qty_abs, basis, closed_qty):
    rem = D(str(cur_qty_abs)) - D(str(closed_qty))
    if rem <= 0:
        return D(str(cur_avg))  # fully closed → avg irrelevant
    return (D(str(cur_avg)) * D(str(cur_qty_abs)) - D(str(basis)) * D(str(closed_qty))) / rem


# ── The operator's exact scenario: BUY 103, 105, 110 (1 lot each) ────────
# avg = (103+105+110)/3 = 106.  Long → sign +1.
AVG = D("106")


def test_close_the_105_lot_books_against_105_not_average():
    # Tap the 105 fill, exit at 120. Must realize (120-105)=15, NOT (120-106)=14.
    assert realized(120, 105, 1, 1) == D("15")


def test_remaining_average_recomputed_from_surviving_lots():
    # After closing the 105 lot, survivors are {103,110} → avg 106.5 (not 106).
    assert remaining_avg(AVG, 3, 105, 1) == D("106.5")


def test_total_pnl_conserved_vs_avg_price():
    # Close the 105 lot at 120, then the surviving 2 lots at 130.
    specific = realized(120, 105, 1, 1) + realized(130, remaining_avg(AVG, 3, 105, 1), 2, 1)
    # Avg-price method: close 1 @120 against 106, avg of survivors stays 106,
    # then 2 @130 against 106.
    avg_price = realized(120, AVG, 1, 1) + realized(130, AVG, 2, 1)
    assert specific == avg_price == D("62")


def test_short_side_sign():
    # SELL 3 @ [103,105,110], avg 106, sign -1. Cover the 105 lot at 100 →
    # a short covered cheaper is a PROFIT: (100-105)*1*-1 = +5.
    assert realized(100, 105, 1, -1) == D("5")
    assert remaining_avg(AVG, 3, 105, 1) == D("106.5")


def test_close_the_expensive_lot():
    # Tap the 110 lot, exit 120 → (120-110)=10. Survivors {103,105} → avg 104.
    assert realized(120, 110, 1, 1) == D("10")
    assert remaining_avg(AVG, 3, 110, 1) == D("104")


def test_full_close_last_lot_basis_only():
    # Closing the final lot (Q==closed) realizes against basis; no remaining avg.
    assert realized(120, 103, 1, 1) == D("17")
    assert remaining_avg(103, 1, 103, 1) == D("103")  # rem<=0 → unchanged


def test_partial_qty_within_a_lot():
    # A fill of 10 lots, close 4 of them at 120 vs a basis of 105.
    assert realized(120, 105, 4, 1) == D("60")
    # Position 10 @ avg 106, remove 4 @105 → (106*10 - 105*4)/6 = 106.666...
    ra = remaining_avg(106, 10, 105, 4)
    assert ra == (D("106") * 10 - D("105") * 4) / 6


def test_order_and_trade_have_override_field():
    # Guard the wiring: the opt-in field must exist on BOTH models (Order sets
    # it, Trade carries it for the closed-blotter FIFO pairing).
    from app.models.order import Order
    from app.models.trade import Trade

    assert "cost_basis_override" in Order.model_fields
    assert "cost_basis_override" in Trade.model_fields


# ── Closed-blotter pairing: pick the matching lot, not the FIFO front ────
# Replicates list_closed_trade_events_fifo's opening-fill selection so the
# Closed row shows the lot the user actually exited (operator: exited 4093.36
# but Closed showed 4093.10 — the FIFO front).
def _pick_index(queue_prices, basis):
    if basis is None:
        return 0
    for j, p in enumerate(queue_prices):
        if abs(p - basis) < 1e-6:
            return j
    return 0  # no match → FIFO front


def test_closed_blotter_pairs_the_tapped_lot():
    q = [103.0, 105.0, 110.0]  # FIFO queue, oldest first
    # Specific-lot close of the 105 lot → pairs index 1 (entry 105), NOT front.
    assert _pick_index(q, 105.0) == 1
    # The Closed row's gross then uses 105, matching the wallet booking.
    entry = q[_pick_index(q, 105.0)]
    assert realized(120, entry, 1, 1) == D("15")


def test_closed_blotter_the_operator_xauusd_case():
    # Exited the 4093.36 fill from {4093.10, 4093.36, 4093.93}; Closed must
    # pair 4093.36 (idx 1), not the FIFO front 4093.10 (idx 0).
    q = [4093.10, 4093.36, 4093.93]
    assert _pick_index(q, 4093.36) == 1
    assert q[_pick_index(q, 4093.36)] == 4093.36


def test_closed_blotter_falls_back_to_fifo_without_basis():
    # No override → pure FIFO front (unchanged for every other close).
    q = [103.0, 105.0, 110.0]
    assert _pick_index(q, None) == 0


# ── Active-tab reconstruction: drop the tapped fill, not the FIFO oldest ──
# Replicates list_active_trades' same-side consumption: with a specific-lot
# basis, consume the matching-price fill first, then FIFO for any remainder.
def _consume(fills, close_qty, basis):
    """fills: list of [price, remaining_qty] (oldest-first). Returns the list
    after consuming `close_qty`, matching-price-first when basis is set."""
    fills = [list(f) for f in fills]
    remain = close_qty
    if basis is not None:
        for f in fills:
            if remain <= 0:
                break
            if f[1] > 0 and abs(f[0] - basis) < 1e-6:
                c = min(f[1], remain)
                f[1] -= c
                remain -= c
    for f in fills:
        if remain <= 0:
            break
        c = min(f[1], remain)
        f[1] -= c
        remain -= c
    return fills


def test_active_tab_drops_the_tapped_fill():
    # Operator's XAUUSD case: fills 4100.35, 4100.49, 4100.56 (oldest-first),
    # exit the 4100.49 lot (100). It must be the one consumed; the other two
    # survive — NOT the oldest 4100.35 (the pre-fix FIFO bug).
    fills = [[4100.35, 100], [4100.49, 100], [4100.56, 100]]
    out = _consume(fills, 100, basis=4100.49)
    survivors = [f[0] for f in out if f[1] > 1e-9]
    assert survivors == [4100.35, 4100.56]


def test_active_tab_fifo_when_no_basis():
    # No override → oldest consumed (unchanged avg-price behaviour).
    fills = [[4100.35, 100], [4100.49, 100], [4100.56, 100]]
    out = _consume(fills, 100, basis=None)
    survivors = [f[0] for f in out if f[1] > 1e-9]
    assert survivors == [4100.49, 4100.56]
