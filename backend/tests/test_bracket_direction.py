"""Guard against wrong-side SL/TP legs (the SILVERM fake-fill incident).

An SL/TP placed on the profitable side of the market is already "hit" the
instant it is stored — the risk enforcer fires it and it fills at the exact
(untraded) leg price, booking fake P&L. `bracket_direction_error` must reject
such a leg; every real order path routes through it.
"""

from app.services.order_validator import bracket_direction_error as err


def test_the_real_incident_short_sl_below():
    # SELL SILVERM26AUGFUT @ ~2,32,400 with SL at 1,32,680 (a lakh below).
    # A short's SL must be ABOVE the price → this must be rejected.
    assert err("SELL", 232400, sl=132680) is not None


def test_valid_short_passes():
    assert err("SELL", 232400, sl=232500, tp=232000) is None


def test_valid_long_passes():
    assert err("BUY", 232400, sl=232000, tp=232900) is None


def test_every_wrong_side_rejected():
    assert err("BUY", 232400, sl=232500) is not None   # long SL not below
    assert err("BUY", 232400, tp=232000) is not None   # long TP not above
    assert err("SELL", 232400, sl=232000) is not None  # short SL not above
    assert err("SELL", 232400, tp=232500) is not None  # short TP not below


def test_fail_open_when_no_mark():
    assert err("BUY", 0, sl=120) is None
    assert err("SELL", 0, tp=999) is None


def test_none_and_clear_skipped():
    assert err("BUY", 232400) is None                  # nothing set
    assert err("SELL", 232400, sl=0, tp=0) is None     # 0 = clear → skipped


def test_accepts_decimal128_and_str():
    from bson import Decimal128

    assert err("SELL", Decimal128("232400"), sl=Decimal128("132680")) is not None
    assert err("BUY", "232400", sl="232000", tp="232900") is None
