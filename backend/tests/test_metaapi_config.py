"""MetaAPI feed config comes from the admin panel, with .env as the fallback.

The feed used to read METAAPI_* straight off `settings`. Now the admin row
wins field by field, so an operator can connect MetaAPI without SSH — but a
server that still only has .env must keep working.

    pytest -q backend/tests/test_metaapi_config.py
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from pydantic import SecretStr

from app.core.config import settings as app_settings
from app.services import metaapi_service as ms
from app.utils import crypto


@pytest.fixture(autouse=True)
def _fresh_key(monkeypatch):
    # crypto reads the key off the settings OBJECT, which is built when
    # app.core.config is first imported — so setting os.environ here only
    # works when this module happens to import first. Patch the object.
    monkeypatch.setattr(
        app_settings,
        "ZERODHA_CREDS_KEY",
        SecretStr(base64.b64encode(b"k" * 32).decode()),
        raising=False,
    )
    crypto._key.cache_clear()
    yield
    crypto._key.cache_clear()


class _Row:
    """Stand-in for the MetaApiSettings document."""

    def __init__(self, **kw):
        self.enabled = kw.get("enabled", True)
        self.encrypted_token = kw.get("encrypted_token", "")
        self.encrypted_token_iv = kw.get("encrypted_token_iv", "")
        self.account_id = kw.get("account_id", "")
        self.region = kw.get("region", "")
        self.symbols = kw.get("symbols", [])
        self.symbol_map = kw.get("symbol_map", {})
        self.max_symbols = kw.get("max_symbols", 24)


def _feed(row):
    feed = ms.MetaApiFeed()

    async def fetch():
        return row

    feed._fetch_doc = fetch
    return feed


def _env(monkeypatch, **kw):
    monkeypatch.setattr(ms.settings, "METAAPI_FEED", kw.get("feed", True), raising=False)
    monkeypatch.setattr(ms.settings, "METAAPI_TOKEN", SecretStr(kw.get("token", "env-token")), raising=False)
    monkeypatch.setattr(ms.settings, "METAAPI_ACCOUNT_ID", kw.get("account", "env-account"), raising=False)
    monkeypatch.setattr(ms.settings, "METAAPI_SYMBOLS", kw.get("symbols", "EURUSD,XAUUSD"), raising=False)
    monkeypatch.setattr(ms.settings, "METAAPI_SYMBOL_MAP", kw.get("symbol_map", "US30:DJ30"), raising=False)


def test_env_is_used_when_the_admin_never_saved_settings(monkeypatch):
    _env(monkeypatch)
    cfg = asyncio.run(_feed(None).load_config())
    assert cfg["enabled"] is True
    assert cfg["token"] == "env-token"
    assert cfg["account_id"] == "env-account"
    assert cfg["symbols"] == ["EURUSD", "XAUUSD"]
    assert cfg["alias"] == {"US30": "DJ30"}
    assert cfg["source"] == "env"


def test_admin_settings_win_over_env(monkeypatch):
    _env(monkeypatch)
    ct, iv = crypto.encrypt("admin-token")
    row = _Row(
        encrypted_token=ct,
        encrypted_token_iv=iv,
        account_id="admin-account",
        region="new-york",
        symbols=["gbpusd", "USOIL"],
        symbol_map={"USOIL": "WTI"},
    )
    cfg = asyncio.run(_feed(row).load_config())
    assert cfg["token"] == "admin-token"
    assert cfg["account_id"] == "admin-account"
    assert cfg["region"] == "new-york"
    assert cfg["symbols"] == ["GBPUSD", "USOIL"]
    assert cfg["alias"] == {"USOIL": "WTI"}
    assert cfg["source"] == "admin"


def test_disabled_in_admin_stops_the_feed_even_with_env_on(monkeypatch):
    _env(monkeypatch, feed=True)
    ct, iv = crypto.encrypt("admin-token")
    row = _Row(enabled=False, encrypted_token=ct, encrypted_token_iv=iv, account_id="a")
    cfg = asyncio.run(_feed(row).load_config())
    assert cfg["enabled"] is False
    assert cfg["configured"] is True  # creds are there, the admin just turned it off


def test_symbols_are_capped_at_the_account_subscription_limit(monkeypatch):
    _env(monkeypatch)
    row = _Row(account_id="a", symbols=[f"SYM{i}" for i in range(30)], max_symbols=5)
    row.encrypted_token, row.encrypted_token_iv = crypto.encrypt("t")
    cfg = asyncio.run(_feed(row).load_config())
    assert len(cfg["symbols"]) == 5


def test_missing_credentials_never_report_enabled(monkeypatch):
    _env(monkeypatch, token="", account="")
    cfg = asyncio.run(_feed(None).load_config())
    assert cfg["enabled"] is False
    assert cfg["configured"] is False
