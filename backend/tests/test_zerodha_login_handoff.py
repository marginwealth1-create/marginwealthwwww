"""Zerodha login hand-off across workers and accounts.

Auto-login lets Kite redirect to /callback, which does the only request_token
exchange. That request can land on any HTTP worker, but the Kite WS pool lives
only on the feed leader — and with two accounts on one redirect URL, the
callback must be told which account's secret to use.

    pytest -q backend/tests/test_zerodha_login_handoff.py
"""

from __future__ import annotations

import asyncio

import pytest

from app.services import market_data_service as mds
from app.services import zerodha_service as zs


class _AccountB:
    apiKey = "key_b"
    apiSecret = "secret_b"
    accessToken = None
    refreshToken = None
    tokenExpiry = None
    isConnected = False
    lastConnected = None

    async def save(self):
        pass


class _Kite:
    def generate_session(self, request_token, api_secret):
        assert api_secret == "secret_b"
        return {"access_token": "fresh", "refresh_token": None}


@pytest.fixture
def worker(monkeypatch):
    """A fresh service instance = one gunicorn worker's in-process state."""
    svc = type(zs.zerodha)()
    published: list = []
    kicked: list = []

    async def get_settings(account_index=0):
        assert account_index == 1
        return _AccountB()

    async def no_instruments(exchange):
        return []

    async def publish(channel, payload):
        published.append((channel, payload))

    svc._get_settings = get_settings
    svc._kite = lambda api_key, access_token=None: _Kite()
    svc.fetch_instruments = no_instruments
    svc._kick_ws_after_login = lambda *args: kicked.append(args)
    monkeypatch.setattr(zs, "publish", publish)
    return svc, published, kicked


def test_login_url_tells_callback_which_account(worker):
    svc, _, _ = worker
    assert "redirect_params=account%3D1" in asyncio.run(svc.get_login_url(1))


def test_callback_on_http_worker_hands_reconnect_to_leader(worker, monkeypatch):
    svc, published, kicked = worker
    monkeypatch.setattr(mds, "is_feed_leader", lambda: False)
    asyncio.run(svc.generate_session("rt", 1))
    assert published == [(zs.FAILOVER_CMD_CHANNEL, {"action": "reconnect", "account": 1})]
    assert kicked == []


def test_callback_on_leader_reconnects_only_that_account(worker, monkeypatch):
    svc, published, kicked = worker
    monkeypatch.setattr(mds, "is_feed_leader", lambda: True)
    asyncio.run(svc.generate_session("rt", 1))
    assert kicked == [(1, "key_b", "fresh")]
    assert published == []
