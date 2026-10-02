"""Symbol-style instruments must reach `mdlive` without a client asking.

The live failure: after every backend restart the whole FOREX / INDICES /
STOCKS list showed a frozen price, because `_subscribed` is in-memory and
those tokens only entered it when some client happened to subscribe.

    pytest -q backend/tests/test_international_subscriptions.py
"""

from __future__ import annotations

import pytest

from app.services import market_data_service as mds


class _Inst:
    def __init__(self, token, segment="FOREX"):
        self.token = token
        self.segment = segment


@pytest.fixture
def catalogue(monkeypatch):
    """Stub the Instrument query and capture what gets forwarded."""
    forwarded: list[list[str]] = []

    def install(rows):
        class _Q:
            def __init__(self, rows):
                self._rows = rows

            async def to_list(self):
                return self._rows

        from app.models.instrument import Instrument

        monkeypatch.setattr(Instrument, "find", classmethod(lambda cls, *a, **k: _Q(rows)))

        async def _fwd(tokens):
            forwarded.append(list(tokens))

        monkeypatch.setattr(mds, "_forward_feed_subscription", _fwd)
        return forwarded

    return install


@pytest.mark.asyncio
async def test_symbol_tokens_are_subscribed(catalogue, monkeypatch):
    monkeypatch.setattr(mds, "_subscribed", set())
    forwarded = catalogue([_Inst("EURUSD"), _Inst("SPX500", "INDICES"), _Inst("AAPL", "STOCKS")])
    assert await mds.ensure_international_subscriptions() == 3
    assert forwarded == [["EURUSD", "SPX500", "AAPL"]]


@pytest.mark.asyncio
async def test_already_subscribed_sends_nothing(catalogue, monkeypatch):
    """The 2-minute pass must not re-subscribe 40 symbols at MetaAPI —
    that traffic is what their per-account rate limit punishes."""
    monkeypatch.setattr(mds, "_subscribed", {"EURUSD", "SPX500"})
    forwarded = catalogue([_Inst("EURUSD"), _Inst("SPX500", "INDICES")])
    assert await mds.ensure_international_subscriptions() == 0
    assert forwarded == []


@pytest.mark.asyncio
async def test_numeric_zerodha_tokens_are_not_touched(catalogue, monkeypatch):
    """Numeric tokens belong to the Kite WS and its LRU trim."""
    monkeypatch.setattr(mds, "_subscribed", set())
    forwarded = catalogue([_Inst("256265", "INDICES"), _Inst("EURUSD")])
    assert await mds.ensure_international_subscriptions() == 1
    assert forwarded == [["EURUSD"]]
