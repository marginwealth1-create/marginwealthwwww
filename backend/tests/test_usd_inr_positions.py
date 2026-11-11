"""Position-level USD→INR rules: only positions opened under the conversion
(open stamp present) convert; a reversal uses the booked close rate, never
today's rate."""

from types import SimpleNamespace

from bson import Decimal128

from app.services import market_data_service as mds
from app.services.position_service import position_fx_rate, realized_pnl_inr


def _pos(seg="FOREX", realized="10", open_rate=None, close_rate=None):
    d = lambda v: Decimal128(str(v)) if v is not None else None  # noqa: E731
    return SimpleNamespace(
        segment_type=seg,
        instrument=SimpleNamespace(segment=seg),
        realized_pnl=d(realized),
        open_usd_inr_rate=d(open_rate),
        close_usd_inr_rate=d(close_rate),
    )


def test_legacy_unstamped_usd_position_stays_1_to_1():
    p = _pos(open_rate=None)
    assert not mds.position_is_usd(p)
    assert position_fx_rate(p) == 1
    assert str(realized_pnl_inr(p)) == "10.00"


def test_inr_position_never_scaled():
    p = _pos(seg="NSE_EQ", open_rate=97)
    assert position_fx_rate(p) == 1


def test_close_rate_wins_over_open_and_live(monkeypatch):
    monkeypatch.setitem(mds._usd_inr_cache, "rate", 99.0)
    p = _pos(open_rate=95, close_rate=96)
    assert str(realized_pnl_inr(p)) == "960.00"


def test_open_rate_when_not_closed(monkeypatch):
    monkeypatch.setitem(mds._usd_inr_cache, "rate", 99.0)
    assert position_fx_rate(_pos(open_rate=95)) == 95


def test_zero_stamp_falls_through_to_live(monkeypatch):
    monkeypatch.setitem(mds._usd_inr_cache, "rate", 99.0)
    assert position_fx_rate(_pos(open_rate=95, close_rate=0)) == 95


def test_rate_1_books_usd_pnl_1_to_1(monkeypatch):
    # Wallet has no currency: a $156 profit adds 156, not 156 × 97.
    monkeypatch.setitem(mds._usd_inr_cache, "rate", 1.0)
    assert str(realized_pnl_inr(_pos(realized="156", open_rate=1))) == "156.00"
    assert str(realized_pnl_inr(_pos(realized="156", open_rate=1, close_rate=1))) == "156.00"
