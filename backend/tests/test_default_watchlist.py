"""Default marketwatch instruments.

The trap here is the symbol match: MCX lists a mini and a micro beside
every big contract, and seeding CRUDEOILM instead of CRUDEOIL hands the
user a different lot size without saying so.

    pytest -q backend/tests/test_default_watchlist.py
"""

from __future__ import annotations

import re

import pytest

from app.services import default_watchlist as dw


def matches(base: str, symbol: str) -> bool:
    return re.match(dw.front_month_pattern(base), symbol) is not None


@pytest.mark.parametrize(
    ("base", "symbol"),
    [
        ("CRUDEOIL", "CRUDEOIL26OCTFUT"),
        ("NATURALGAS", "NATURALGAS26SEPFUT"),
        ("SILVER", "SILVER26DECFUT"),
        ("ZINC", "ZINC26SEPFUT"),
        ("NIFTY", "NIFTY26SEPFUT"),
        ("BANKNIFTY", "BANKNIFTY26SEPFUT"),
    ],
)
def test_the_big_contract_matches(base, symbol):
    assert matches(base, symbol)


@pytest.mark.parametrize(
    ("base", "symbol"),
    [
        # Mini / micro variants — same prefix, different contract.
        ("CRUDEOIL", "CRUDEOILM26OCTFUT"),
        ("SILVER", "SILVERM26NOVFUT"),
        ("SILVER", "SILVERMIC26NOVFUT"),
        ("SILVER", "SILVER10026SEPFUT"),
        ("ZINC", "ZINCMINI26SEPFUT"),
        # Other indices that merely END with the base name.
        ("NIFTY", "BANKNIFTY26SEPFUT"),
        ("NIFTY", "FINNIFTY26SEPFUT"),
        ("NIFTY", "MIDCPNIFTY26SEPFUT"),
        # Nearby instrument types.
        ("NIFTY", "NIFTY26SEP26000CE"),
        ("BANKNIFTY", "BANKNIFTY"),
    ],
)
def test_lookalikes_are_rejected(base, symbol):
    assert not matches(base, symbol)


class _FakeInst:
    def __init__(self, token, segment="CRYPTO_SPOT"):
        self.token = token
        self.symbol = token
        self.segment = segment
        self.exchange = "CRYPTO"


@pytest.mark.asyncio
async def test_a_user_who_has_seen_them_all_costs_nothing(monkeypatch):
    """The common path: no watchlist writes, no user save."""

    async def _resolved(force: bool = False):
        return [_FakeInst("A"), _FakeInst("B")]

    monkeypatch.setattr(dw, "resolve_defaults", _resolved)

    class _User:
        id = "u1"
        default_watchlist_seeded = ["A", "B"]

        async def save(self):  # pragma: no cover - must not be reached
            raise AssertionError("nothing to seed, so nothing to save")

    assert await dw.ensure_defaults(_User()) == 0


@pytest.mark.asyncio
async def test_a_removed_instrument_stays_removed(monkeypatch):
    """Only the token this user has never been offered gets added.

    GOLD_OCT was seeded and then deleted by the user; it must not come
    back. GOLD_NOV — next month's contract — must.
    """
    added: list[str] = []

    async def _resolved(force: bool = False):
        # Spot-shaped so the segment-chip branch is skipped and the
        # Zerodha subscribe no-ops on a non-numeric token.
        return [
            _FakeInst("GOLD_OCT", "CRYPTO_SPOT"),
            _FakeInst("GOLD_NOV", "CRYPTO_SPOT"),
        ]

    async def _fake_wl(_uid):
        return object()

    async def _fake_add(_wl, inst):
        added.append(inst.token)
        return True

    monkeypatch.setattr(dw, "resolve_defaults", _resolved)
    monkeypatch.setattr(dw, "_default_watchlist", _fake_wl)
    monkeypatch.setattr(dw, "_add", _fake_add)

    class _User:
        id = "u1"
        default_watchlist_seeded = ["GOLD_OCT"]
        saved = False

        async def save(self):
            self.saved = True

    u = _User()
    n = await dw.ensure_defaults(u)
    assert added == ["GOLD_NOV"]
    assert n == 1
    assert u.saved
    # And the new token is remembered, so a second load is a no-op.
    assert u.default_watchlist_seeded == ["GOLD_OCT", "GOLD_NOV"]
    added.clear()
    assert await dw.ensure_defaults(u) == 0
    assert added == []
