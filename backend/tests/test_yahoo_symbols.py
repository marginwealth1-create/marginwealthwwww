"""Every international instrument must map to a Yahoo chart symbol.

The licensed chart now serves ALL of them — the free TradingView embed that
used to cover forex, metals, energy and US stocks is gone — so a token
missing from the map is no longer a cosmetic gap: that instrument renders an
empty chart pane.

    pytest -q backend/tests/test_yahoo_symbols.py
"""

from __future__ import annotations

import pytest

from app.api.v1.user.instruments import _yahoo_symbol_for

# The live catalog, by segment (tokens as the Instrument collection holds
# them). Crypto is served by Binance klines, not Yahoo, so it is not here.
CATALOG = [
    ("XAUUSD", "GC=F"), ("XAGUSD", "SI=F"), ("XPTUSD", "PL=F"), ("XPDUSD", "PA=F"),
    ("USOIL", "CL=F"), ("UKOIL", "BZ=F"), ("NATGAS", "NG=F"),
    ("US30", "^DJI"), ("NAS100", "^NDX"), ("SPX500", "^GSPC"),
    ("UK100", "^FTSE"), ("DE40", "^GDAXI"), ("JPN225", "^N225"), ("HK50", "^HSI"),
    ("AAPL", "AAPL"), ("AMZN", "AMZN"), ("GOOGL", "GOOGL"), ("META", "META"),
    ("MSFT", "MSFT"), ("NFLX", "NFLX"), ("NVDA", "NVDA"), ("TSLA", "TSLA"),
    ("EURUSD", "EURUSD=X"), ("GBPUSD", "GBPUSD=X"), ("USDJPY", "USDJPY=X"),
    ("AUDUSD", "AUDUSD=X"), ("NZDUSD", "NZDUSD=X"), ("USDCAD", "USDCAD=X"),
    ("USDCHF", "USDCHF=X"), ("FX_USDINR", "USDINR=X"),
]


@pytest.mark.parametrize(("token", "expected"), CATALOG)
def test_every_international_token_maps(token: str, expected: str):
    assert _yahoo_symbol_for(token) == expected


def test_metals_do_not_use_the_fx_form():
    """Yahoo serves XAUUSD=X with ZERO intraday candles (measured against
    EURUSD=X's 1295). Metals have to go to the COMEX future."""
    assert _yahoo_symbol_for("XAUUSD") != "XAUUSD=X"


def test_indian_and_crypto_tokens_are_left_alone():
    # Zerodha's numeric tokens and Binance-fed crypto must not hit Yahoo.
    assert _yahoo_symbol_for("738561") is None
    assert _yahoo_symbol_for("CRYPTO_BTCUSD") is None
    assert _yahoo_symbol_for("") is None
