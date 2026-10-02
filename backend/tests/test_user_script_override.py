"""Derivative shorthand in script overrides, per-user and global.

Admins type `CRUDEOILFUT` on both Script tabs, because the real contract
name (`CRUDEOIL26OCTFUT`) changes every month. The global layer resolved
that; the per-user layer matched exact symbols only, so every per-user
script override silently did nothing — 19 dead rows in production, all
of them margin overrides somebody believed were live.

`_best_pattern_row` is the shared picker both layers now use.

    pytest -q backend/tests/test_user_script_override.py
"""

from __future__ import annotations

import pytest

from app.services.netting_service import _best_pattern_row, _split_pattern


class _Row:
    """Stands in for a NettingScriptOverride / UserSegmentOverride doc."""

    def __init__(self, symbol):
        self.symbol = symbol

    def __repr__(self):  # nicer assertion output
        return f"<{self.symbol}>"


@pytest.mark.parametrize(
    ("contract", "expected"),
    [
        ("CRUDEOIL26OCTFUT", "CRUDEOILFUT"),
        ("CRUDEOIL26NOVFUT", "CRUDEOILFUT"),
        ("GOLD26DECFUT", "GOLDFUT"),
        ("ZINC26SEPFUT", None),
    ],
)
def test_shorthand_covers_every_expiry(contract, expected):
    rows = [_Row("CRUDEOILFUT"), _Row("GOLDFUT")]
    hit = _best_pattern_row(rows, contract)
    assert (hit.symbol if hit else None) == expected


@pytest.mark.parametrize("order", [("GOLDFUT", "GOLDMFUT"), ("GOLDMFUT", "GOLDFUT")])
def test_the_mini_contract_keeps_its_own_margin(order):
    """Both rows match GOLDM26DECFUT under the start/end rule, and in
    production they carry Rs 1L vs Rs 3k per lot — so a first-wins scan
    priced the mini contract by whichever row Mongo returned first."""
    hit = _best_pattern_row([_Row(order[0]), _Row(order[1])], "GOLDM26DECFUT")
    assert hit is not None and hit.symbol == "GOLDMFUT"


@pytest.mark.parametrize("order", [("GOLDFUT", "GOLDMFUT"), ("GOLDMFUT", "GOLDFUT")])
def test_the_big_contract_is_unaffected(order):
    hit = _best_pattern_row([_Row(order[0]), _Row(order[1])], "GOLD26DECFUT")
    assert hit is not None and hit.symbol == "GOLDFUT"


def test_a_row_naming_one_contract_is_not_a_pattern():
    """It must not spill onto the other months — the caller has already
    tried it as an exact match."""
    assert _best_pattern_row([_Row("CRUDEOIL26OCTFUT")], "CRUDEOIL26NOVFUT") is None


def test_rows_without_a_symbol_are_skipped():
    """A segment-wide user override carries symbol=None."""
    assert _best_pattern_row([_Row(None), _Row("")], "CRUDEOIL26OCTFUT") is None


def test_option_shorthand_splits_too():
    assert _split_pattern("BANKNIFTYCE") == ("BANKNIFTY", "CE")
    assert _split_pattern("CRUDEOILFUT") == ("CRUDEOIL", "FUT")
    # Real contracts carry digits and are never patterns.
    assert _split_pattern("CRUDEOIL26OCTFUT") is None
