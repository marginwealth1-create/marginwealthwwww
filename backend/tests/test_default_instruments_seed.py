"""Default global instruments are catalogued on every boot.

The terminal's Stocks / Indices tabs read the Instrument catalogue. On a
MetaAPI-fed deployment Infoway never subscribes those symbols, so nothing ever
mirrored them and both tabs were empty.

    pytest -q backend/tests/test_default_instruments_seed.py
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import infoway_service as ifs


class _FakeInstrument:
    """Stand-in for the Beanie document: records what the seed creates."""

    created: dict = {}
    token = None  # makes `Instrument.token == code` a harmless bool

    def __init__(self, **kw):
        self.kw = kw

    async def insert(self):
        _FakeInstrument.created[self.kw["token"]] = self.kw

    @classmethod
    async def find_one(cls, *_args, **_kw):
        return None


@pytest.fixture
def created(monkeypatch):
    _FakeInstrument.created = {}
    monkeypatch.setattr("app.models.instrument.Instrument", _FakeInstrument)
    return _FakeInstrument.created


def test_seed_catalogues_every_default_stock_and_index(created):
    n = asyncio.run(ifs.seed_default_instruments())
    segments = {tok: row["segment"] for tok, row in created.items()}

    for stock in ("AAPL", "MSFT", "TSLA"):
        assert segments.get(stock) == "STOCKS", f"{stock} missing from the catalogue"
    for index in ("SPX500", "NAS100", "US30"):
        assert segments.get(index) == "INDICES", f"{index} missing from the catalogue"
    assert segments.get("EURUSD") == "FOREX"
    assert segments.get("XAUUSD") == "COMMODITIES"
    assert n == len(created)


def test_seed_skips_crypto_so_coins_are_not_listed_twice(created):
    # The Indian seed already ships CRYPTO_SPOT rows (BTCUSD); adding Infoway's
    # BTCUSDT on top would show Bitcoin twice in the terminal.
    asyncio.run(ifs.seed_default_instruments())
    assert not [t for t in created if t.endswith("USDT")]


def test_seed_does_not_duplicate_rows_that_already_exist(monkeypatch):
    """Runs on every boot, so a second pass must create nothing."""

    class _ExistingRow:
        lot_size = 1
        exchange = segment = instrument_type = name = None
        is_active = is_tradable = False

        async def save(self):
            pass

    class _Present(_FakeInstrument):
        @classmethod
        async def find_one(cls, *_args, **_kw):
            return _ExistingRow()

    monkeypatch.setattr("app.models.instrument.Instrument", _Present)
    assert asyncio.run(ifs.seed_default_instruments()) == 0
