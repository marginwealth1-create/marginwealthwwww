"""Per-user admin trade alerts.

The publish path runs on EVERY fill, so the gate matters more than the
payload: a user without the flag must cost one projected read and
nothing else.

    pytest -q backend/tests/test_trade_alert.py
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from beanie import PydanticObjectId

from app.services import admin_events

USER_ID = PydanticObjectId()
ADMIN_ID = PydanticObjectId()


class _FakeCollection:
    def __init__(self, doc):
        self.doc = doc
        self.calls = 0

    async def find_one(self, *_a, **_kw):
        self.calls += 1
        return self.doc


@pytest.fixture
def patched(monkeypatch):
    """Point the helper at a fake user doc + a known recipient list."""
    from app.models.user import User
    from app.services import push_service

    state: dict = {}

    def install(doc):
        coll = _FakeCollection(doc)
        state["coll"] = coll
        monkeypatch.setattr(User, "get_motor_collection", classmethod(lambda cls: coll))

        async def _recipients(_uid):
            state["recipients_called"] = True
            return [ADMIN_ID]

        monkeypatch.setattr(push_service, "_compute_recipient_admin_ids", _recipients)
        return state

    return install


async def _call(event="opened"):
    return await admin_events.trade_alert_fields(
        USER_ID,
        action="BUY",
        quantity=50,
        price=Decimal("23450.5"),
        symbol="NIFTY24SEPFUT",
        event=event,
    )


@pytest.mark.asyncio
async def test_user_without_the_flag_gets_nothing(patched):
    state = patched({"trade_alert": False, "full_name": "A", "user_code": "U1"})
    assert await _call() == {}
    # The expensive half — resolving the owning admins — never ran.
    assert "recipients_called" not in state


@pytest.mark.asyncio
async def test_missing_user_is_not_an_error(patched):
    patched(None)
    assert await _call() == {}


@pytest.mark.asyncio
async def test_watched_user_carries_the_toast_payload(patched):
    patched(
        {
            "trade_alert": True,
            "trade_alert_sound": "bell",
            "full_name": "Ravi",
            "user_code": "SS1042",
        }
    )
    out = await _call(event="closed")
    assert out["recipient_admin_ids"] == [str(ADMIN_ID)]
    assert out["alert"] == {
        "sound": "bell",
        "user_name": "Ravi",
        "user_code": "SS1042",
        "action": "BUY",
        "qty": 50,
        "price": 23450.5,
        "symbol": "NIFTY24SEPFUT",
        "event": "closed",
    }


@pytest.mark.asyncio
async def test_sound_falls_back_when_never_set(patched):
    patched({"trade_alert": True, "full_name": "", "user_code": ""})
    out = await _call()
    assert out["alert"]["sound"] == "chime"


@pytest.mark.asyncio
async def test_a_broken_lookup_never_breaks_the_fill(monkeypatch):
    from app.models.user import User

    class _Boom:
        async def find_one(self, *_a, **_kw):
            raise RuntimeError("mongo down")

    monkeypatch.setattr(User, "get_motor_collection", classmethod(lambda cls: _Boom()))
    assert await _call() == {}
