"""Diwali Muhurat 2026-11-08 (Sunday): Indian markets open only 14:20–15:40 IST."""
from datetime import datetime, timezone

from app.services.eod_order_cleanup import order_expired
from app.services.market_hours_service import DEFAULT_MARKET_HOURS, is_market_open_by_config
from app.utils.time_utils import IST, muhurat_open


def _at(h, m, day=8):
    return datetime(2026, 11, day, h, m, tzinfo=IST)


def test_window_edges_and_scope():
    assert muhurat_open("NSE_EQ", _at(14, 19)) is False
    assert muhurat_open("NFO_OPT", _at(14, 20)) is True
    assert muhurat_open("MCX_FUT", _at(15, 40)) is True
    assert muhurat_open("BSE_EQ", _at(15, 41)) is False
    assert muhurat_open("CRYPTO", _at(14, 30)) is None      # 24x7 untouched
    assert muhurat_open("FOREX", _at(14, 30)) is None
    assert muhurat_open("NSE_EQ", _at(14, 30, day=15)) is None  # normal Sunday
    assert muhurat_open("NSE_EQ", _at(14, 20), grace_sec=60) is False  # stop-out grace
    assert muhurat_open("NSE_EQ", _at(14, 21), grace_sec=60) is True


def test_order_and_exit_gate_open_on_sunday():
    hours = dict(DEFAULT_MARKET_HOURS)
    assert is_market_open_by_config(hours, "NFO_OPT", "NFO", _at(14, 30))[0] is True
    assert is_market_open_by_config(hours, "MCX_FUT", "MCX", _at(14, 30))[0] is True
    assert is_market_open_by_config(hours, "NSE_EQ", "NSE", _at(16, 0))[0] is False
    assert is_market_open_by_config(hours, "NSE_EQ", "NSE", _at(10, 0))[0] is False
    assert is_market_open_by_config(hours, "NSE_EQ", "NSE", _at(14, 30, day=15))[0] is False


def test_muhurat_pending_order_dies_at_session_close():
    placed = _at(14, 30).astimezone(timezone.utc)
    assert order_expired("NFO_OPT", placed, _at(15, 39), None) is False
    assert order_expired("NFO_OPT", placed, _at(15, 40), None) is True
    assert order_expired("CRYPTO", placed, _at(15, 45), None) is False
