"""MetaAPI (MetaTrader 4/5) feed settings — single-row collection.

Holds what used to live in `.env` (the METAAPI_* vars) so the admin panel can
connect an MT4/MT5 account the same way it connects Zerodha. The token is
encrypted at rest with AES-256-GCM (the same `ZERODHA_CREDS_KEY` the Kite
auto-login credentials use) and is never returned by the API.

Every empty field falls back to its `.env` value, so a deployment that hasn't
been switched over yet keeps running unchanged.
"""

from __future__ import annotations

from pydantic import Field

from app.models._base import TimestampMixin


class MetaApiSettings(TimestampMixin):
    enabled: bool = False

    # AES-256-GCM ciphertext + iv, both base64.
    encrypted_token: str = ""
    encrypted_token_iv: str = ""

    account_id: str = ""
    region: str = ""  # optional MetaAPI region, e.g. "new-york"

    # Platform symbols to stream. Empty → the .env / Infoway default lists.
    symbols: list[str] = Field(default_factory=list)
    # Platform symbol → broker symbol, for brokers that rename ("US30": "DJ30").
    symbol_map: dict[str, str] = Field(default_factory=dict)
    # MetaAPI plans cap concurrent subscriptions (current plan = 25); symbols
    # past the cap fall back to the Infoway feed instead of 429-flooding.
    max_symbols: int = 24

    class Settings:
        name = "metaapi_settings"
