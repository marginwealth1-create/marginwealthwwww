"""The blotter must not print 0 for an instrument that simply hasn't ticked.

`get_ltp` stays strict — 0 means "no live price", and every execution path
depends on that. `get_display_ltp` is the blotter's version: it falls back to
the last price we saw, which is what kept the admin Positions page swinging
between a real M2M and zero on illiquid MCX contracts.

    pytest -q backend/tests/test_display_ltp.py
"""

from __future__ import annotations

from decimal import Decimal

from app.services import market_data_service as m


async def _with_quote(monkeypatch, quote: dict):
    async def fake_get_quote(token: str) -> dict:
        return quote

    monkeypatch.setattr(m, "get_quote", fake_get_quote)


async def test_live_price_wins(monkeypatch):
    await _with_quote(monkeypatch, {"ltp": 1418.6, "last_ltp": 1400.0})
    assert await m.get_display_ltp("147156231") == Decimal("1418.60")


async def test_falls_back_to_the_last_known_price(monkeypatch):
    # SILVER26DECFUT between two trades: the 30 s live snapshot has expired,
    # `mdlast` still holds the real price.
    await _with_quote(monkeypatch, {"ltp": 0, "last_ltp": 233621.0})
    assert await m.get_display_ltp("126774791") == Decimal("233621.00")


async def test_zero_when_nothing_is_known(monkeypatch):
    await _with_quote(monkeypatch, {"ltp": 0})
    assert await m.get_display_ltp("999") == Decimal("0.00")


async def test_get_ltp_stays_strict(monkeypatch):
    """Execution reads this one — a stale price must never reach a fill."""
    await _with_quote(monkeypatch, {"ltp": 0, "last_ltp": 233621.0})
    assert await m.get_ltp("126774791") == Decimal("0.00")
