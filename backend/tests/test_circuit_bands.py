"""Upper / lower circuit (daily price band) enforcement.

Covers the full required matrix at the LOGIC level: the pure band evaluation
(`circuit_service.evaluate`), the 0-normalisation, the exchange allow-list /
fail-open path, and the exit exemption.

`_exempt` below mirrors the order validator's computation EXACTLY
(order_validator.py: signed_held / delta / projected_net / is_reducing, all in
LOTS) — if the validator's formula ever changes, these tests should be updated
in lockstep.
"""

from decimal import Decimal

import pytest

from app.services import circuit_service as cs

D = Decimal


# ── Mirrors of the production flow ───────────────────────────────────
def _exempt(signed_held: float, lots: float, action: str, is_squareoff: bool = False) -> bool:
    """The validator's exit exemption, verbatim (values in LOTS)."""
    delta = lots if action == "BUY" else -lots
    projected_net = signed_held + delta
    is_reducing = abs(projected_net) < abs(signed_held)
    return bool(is_squareoff or is_reducing)


def gate(
    *, signed_held: float, lots: float, action: str, lower, upper, cur, ref,
    order_type: str = "MARKET", is_squareoff: bool = False,
):
    """Full production decision: exemption first, then the band."""
    if _exempt(signed_held, lots, action, is_squareoff):
        return None  # exempt → always allowed
    return cs.evaluate(
        action=action, order_type=order_type,
        lower=lower, upper=upper, cur=cur, ref_price=ref,
    )


# Band used across the matrix: 90 … 110, price locked at an edge.
LO, UP = D("90"), D("110")


# ── 1-4: flat trader at each edge ────────────────────────────────────
def test_1_flat_upper_buy_rejected():
    hit = gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
               cur=D("110"), ref=D("110"))
    assert hit is not None and hit[0] == "UPPER_CIRCUIT_BUY"


def test_2_flat_upper_sell_accepted():
    assert gate(signed_held=0, lots=1, action="SELL", lower=LO, upper=UP,
                cur=D("110"), ref=D("110")) is None


def test_3_flat_lower_sell_rejected():
    hit = gate(signed_held=0, lots=1, action="SELL", lower=LO, upper=UP,
               cur=D("90"), ref=D("90"))
    assert hit is not None and hit[0] == "LOWER_CIRCUIT_SELL"


def test_4_flat_lower_buy_accepted():
    assert gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
                cur=D("90"), ref=D("90")) is None


# ── 5-7: the exit exemption (the two starred rows + partials) ────────
def test_5_short_at_upper_buy_to_cover_accepted():
    # SHORT 5 lots, upper circuit, BUY 5 to cover → locked side, allowed
    # ONLY because it reduces. This is one of the two silent-failure rows.
    assert gate(signed_held=-5, lots=5, action="BUY", lower=LO, upper=UP,
                cur=D("110"), ref=D("110")) is None


def test_6_long_at_lower_sell_to_exit_accepted():
    # LONG 5, lower circuit, SELL 5 to exit → the other starred row.
    assert gate(signed_held=5, lots=5, action="SELL", lower=LO, upper=UP,
                cur=D("90"), ref=D("90")) is None


def test_7_partial_close_accepted():
    # Partial exits must also be exempt (|projected| < |held|).
    assert gate(signed_held=-5, lots=2, action="BUY", lower=LO, upper=UP,
                cur=D("110"), ref=D("110")) is None
    assert gate(signed_held=5, lots=2, action="SELL", lower=LO, upper=UP,
                cur=D("90"), ref=D("90")) is None


def test_7b_exact_flatten_is_reducing():
    # Closing the WHOLE position: |0| < |5| → still reducing.
    assert _exempt(5, 5, "SELL") is True
    assert _exempt(-5, 5, "BUY") is True


def test_7c_flip_through_zero_is_reducing():
    # Over-sized close that flips the side still reduces (|−1| < |5|).
    assert _exempt(5, 6, "SELL") is True
    # …but a flip that ENDS bigger than it started does not.
    assert _exempt(5, 11, "SELL") is False


# ── 8: adding to a position on the locked side ───────────────────────
def test_8_long_at_upper_buy_more_rejected():
    hit = gate(signed_held=5, lots=1, action="BUY", lower=LO, upper=UP,
               cur=D("110"), ref=D("110"))
    assert hit is not None and hit[0] == "UPPER_CIRCUIT_BUY"


def test_8b_short_at_lower_sell_more_rejected():
    hit = gate(signed_held=-5, lots=1, action="SELL", lower=LO, upper=UP,
               cur=D("90"), ref=D("90"))
    assert hit is not None and hit[0] == "LOWER_CIRCUIT_SELL"


# ── 9: price outside the band (distinct code from the directional lock) ──
def test_9_limit_above_upper_rejected():
    # Mid-band live price, but the LIMIT is parked above the ceiling.
    hit = gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
               cur=D("100"), ref=D("115"), order_type="LIMIT")
    assert hit is not None and hit[0] == "UPPER_CIRCUIT"


def test_9b_limit_below_lower_rejected():
    hit = gate(signed_held=0, lots=1, action="SELL", lower=LO, upper=UP,
               cur=D("100"), ref=D("85"), order_type="LIMIT")
    assert hit is not None and hit[0] == "LOWER_CIRCUIT"


def test_9c_directional_and_price_codes_are_distinct():
    # The UI renders "only SELL allowed" differently from "your limit is
    # above the ceiling" — same band, different fix for the user.
    directional = gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
                       cur=D("110"), ref=D("110"))
    price = gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
                 cur=D("100"), ref=D("115"), order_type="LIMIT")
    assert directional[0] != price[0]


# ── 10: risk-engine square-off bypasses everything ───────────────────
def test_10_squareoff_at_either_circuit_executes():
    # Even ADDING-shaped (flat book) — squareoff must bypass every gate.
    assert gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
                cur=D("110"), ref=D("110"), is_squareoff=True) is None
    assert gate(signed_held=0, lots=1, action="SELL", lower=LO, upper=UP,
                cur=D("90"), ref=D("90"), is_squareoff=True) is None


# ── 11-12 + fail-open matrix ─────────────────────────────────────────
def test_11_no_band_allows_everything():
    for act, cur in (("BUY", D("110")), ("SELL", D("90"))):
        assert gate(signed_held=0, lots=1, action=act,
                    lower=None, upper=None, cur=cur, ref=cur) is None


def test_11b_one_sided_band_only_gates_that_side():
    # Upper known, lower unknown → a SELL at a low price must NOT be blocked.
    assert gate(signed_held=0, lots=1, action="SELL", lower=None, upper=UP,
                cur=D("50"), ref=D("50")) is None


def test_zero_limit_is_normalised_to_none():
    # THE #1 bug: feeds send 0 for "unknown". `price >= 0` is true for every
    # price, so a 0 ceiling would lock EVERY instrument at the upper circuit.
    assert cs._norm(0) is None
    assert cs._norm("0") is None
    assert cs._norm(0.0) is None
    assert cs._norm(None) is None
    assert cs._norm("garbage") is None
    assert cs._norm(-5) is None
    assert cs._norm("110.5") == D("110.5")


def test_zero_limit_end_to_end_does_not_lock():
    # A feed that reports upper=0 must leave BUY untouched.
    assert gate(signed_held=0, lots=1, action="BUY",
                lower=cs._norm(0), upper=cs._norm(0),
                cur=D("110"), ref=D("110")) is None


def test_ltp_zero_does_not_block():
    # Market closed / stale feed → LTP 0. `0 <= lower` is true, so an
    # unguarded lower check would block EVERY order the moment the feed
    # goes quiet.
    assert gate(signed_held=0, lots=1, action="SELL", lower=LO, upper=UP,
                cur=D("0"), ref=D("0")) is None
    assert gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
                cur=D("0"), ref=D("0")) is None


def test_ref_zero_skips_price_rule():
    # Live price mid-band, ref unknown (0) → rule 2 must not fire.
    assert gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
                cur=D("100"), ref=D("0")) is None


# ── Staleness guard: an expanded band must never false-lock ──────────
# The CRUDEOIL incident: exchange widened the band intraday (MCX 4%→6%→9%),
# our cached morning ceiling lagged, and `cur >= stale_upper` froze BUY on a
# scrip trading perfectly legally. A genuine lock pins price AT the edge, so a
# price materially PAST the edge proves the band is stale → ignore it.
def test_stale_upper_does_not_lock_buy():
    # Cached upper 110, live price 115 (well past it) → band stale → BUY allowed.
    assert gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
                cur=D("115"), ref=D("115")) is None


def test_stale_upper_crudeoil_repro():
    # The exact production numbers: cached upper ₹8266, live ₹8473 → no lock.
    assert gate(signed_held=0, lots=1, action="BUY",
                lower=D("7900"), upper=D("8266"),
                cur=D("8473"), ref=D("8473")) is None


def test_genuine_upper_still_locks_at_edge():
    # Price sitting exactly at the ceiling is a REAL lock — must still fire.
    hit = gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
               cur=D("110"), ref=D("110"))
    assert hit is not None and hit[0] == "UPPER_CIRCUIT_BUY"


def test_upper_within_tolerance_still_locks():
    # Last tick / rounding may nudge cur a hair past the edge (≤0.5%) — still a
    # live lock. 110 × 1.005 = 110.55.
    hit = gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
               cur=D("110.5"), ref=D("110.5"))
    assert hit is not None and hit[0] == "UPPER_CIRCUIT_BUY"


def test_upper_just_past_tolerance_is_stale():
    # 110.6 > 110.55 → judged stale → no lock.
    assert gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
                cur=D("110.6"), ref=D("110.6")) is None


def test_stale_lower_does_not_lock_sell():
    # Symmetric: cached lower 90, live 85 (well below) → band stale → SELL ok.
    assert gate(signed_held=0, lots=1, action="SELL", lower=LO, upper=UP,
                cur=D("85"), ref=D("85")) is None


def test_genuine_lower_still_locks_at_edge():
    hit = gate(signed_held=0, lots=1, action="SELL", lower=LO, upper=UP,
               cur=D("90"), ref=D("90"))
    assert hit is not None and hit[0] == "LOWER_CIRCUIT_SELL"


def test_stale_upper_also_frees_rule2_limit():
    # A stale ceiling must not flag a LIMIT via rule 2 either: cur 115 proves
    # upper 110 stale, so a limit at 113 (above the stale edge) is NOT blocked.
    assert gate(signed_held=0, lots=1, action="BUY", lower=LO, upper=UP,
                cur=D("115"), ref=D("113"), order_type="LIMIT") is None


# ── Exchange allow-list / fail-open on the lookup itself ─────────────
class _Instr:
    def __init__(self, exchange, token="1", symbol="X"):
        self.exchange = exchange
        self.token = token
        self.symbol = symbol


@pytest.mark.asyncio
async def test_12_non_band_exchanges_never_engage():
    # Crypto / forex / metals come from 24x7 international feeds — no band,
    # and no broker call is even attempted.
    for ex in ("CRYPTO", "FOREX", "COMMODITIES", "STOCKS", "INDICES", ""):
        assert await cs.get_circuit_band(_Instr(ex)) == (None, None)


@pytest.mark.asyncio
async def test_band_exchanges_are_allow_listed():
    assert cs.BAND_EXCHANGES == frozenset({"NSE", "BSE", "NFO", "BFO", "MCX", "CDS"})


@pytest.mark.asyncio
async def test_lookup_fails_open_on_bad_instrument():
    # Missing token / garbage object → (None, None), never an exception.
    assert await cs.get_circuit_band(_Instr("NSE", token="")) == (None, None)
    assert await cs.get_circuit_band(object()) == (None, None)
