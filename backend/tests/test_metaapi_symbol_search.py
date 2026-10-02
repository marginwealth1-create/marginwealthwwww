"""Broker symbols are searchable and resolve back to the broker's spelling.

Brokers decorate instruments (AAPL.US, US500-F, EURUSD.r) and rename a few
outright (SPX500 = US500). Search shows our clean name; subscribing must use
the broker's exact string, or the symbol is silently dropped as "not offered".

    pytest -q backend/tests/test_metaapi_symbol_search.py
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.metaapi_service import MetaApiFeed, clean_broker_symbol

BROKER = [
    "EURUSD.r",
    "AAPL.US",
    "AAPL.US-24",
    "TSLA.US",
    "US500",
    "US500-F",
    "GER40",
    "SPOTCRUDE",
    "XAUUSD",
    "NATGAS",
]


@pytest.fixture
def feed():
    f = MetaApiFeed()
    f._index_broker_symbols(BROKER)
    return f


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("AAPL.US", "AAPL"),
        ("AAPL.US-24", "AAPL"),
        ("US500-F", "US500"),
        ("EURUSD.r", "EURUSD"),
        ("XAUUSD", "XAUUSD"),
    ],
)
def test_broker_decorations_are_stripped(raw, clean):
    assert clean_broker_symbol(raw) == clean


def test_our_name_resolves_to_the_brokers_spelling(feed):
    assert feed._mt_symbol("AAPL") == "AAPL.US"
    assert feed._mt_symbol("EURUSD") == "EURUSD.r"
    assert feed._mt_symbol("XAUUSD") == "XAUUSD"


def test_renamed_symbols_resolve_through_the_alias_table(feed):
    assert feed._mt_symbol("SPX500") == "US500"   # alias → exact
    assert feed._mt_symbol("DE40") == "GER40"
    assert feed._mt_symbol("USOIL") == "SPOTCRUDE"


def test_admin_alias_beats_everything(feed):
    feed._alias = {"AAPL": "AAPL.US-24"}
    assert feed._mt_symbol("AAPL") == "AAPL.US-24"


def test_plain_contract_wins_over_variants(feed):
    # AAPL.US and AAPL.US-24 both clean to AAPL; the shorter (spot) one wins.
    assert feed._broker_by_clean["AAPL"] == "AAPL.US"
    assert feed._broker_by_clean["US500"] == "US500"


def test_search_lists_each_symbol_once_prefix_first(feed):
    hits = asyncio.run(feed.search_symbols("US"))
    assert hits.count("US500") == 1
    assert hits.index("US500") < hits.index("EURUSD")  # prefix ranks first
    assert "AAPL" not in hits


def test_offers_covers_variants_and_renames(feed):
    assert asyncio.run(feed.offers("AAPL")) is True
    assert asyncio.run(feed.offers("SPX500")) is True
    assert asyncio.run(feed.offers("NOSUCHTHING")) is False
