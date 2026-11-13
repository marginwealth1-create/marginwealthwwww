"""§4.3 A settlement writes no fill, so the Trade Book synthesises its legs.

A weekly settlement closes the position and re-opens it at the same price and
instant; the Trade Book printed the closing leg and never the re-open, so it
sold units it never showed being bought back. The re-open comes from the
settlement record's new_position_id. An expiry genuinely ends, so it gets one
leg and no invented re-open, sized to what was held (not the peak). A
settlement whose re-open never landed keeps its one leg.

Every fill goes through the matching engine and the settlements are the real
jobs. The invariant: over a flat book, Σ bought units == Σ sold units.
"""

from __future__ import annotations

from decimal import Decimal

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.core.database import _document_models
from app.models.position import Position, PositionStatus
from app.models.position_settlement import PositionSettlement, SettlementBatch
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service, tradebook_builder, weekly_settlement_service
from app.utils.time_utils import now_utc
from tests._real_fill import market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_weekly_settlement_tradebook_legs"


@pytest_asyncio.fixture
async def c(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    async def _ltp(_token):
        return Decimal("1100")

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    await set_commission_per_lot(1)
    u = User(
        user_code="WSL001",
        email="wsl001@example.com",
        mobile="9367654321",
        full_name="WSL001",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
    )
    await u.insert()
    await Wallet(user_id=u.id, available_balance=Decimal128("100000000")).insert()
    try:
        yield u
    finally:
        await client.drop_database(_DB)
        client.close()


async def _open(user: User) -> Position:
    return await Position.find_one(Position.user_id == user.id, Position.status == PositionStatus.OPEN)


async def _settle(user: User) -> Position:
    batch = SettlementBatch(run_key="T", week_key="2026-W46", started_at=now_utc())
    await batch.insert()
    pos = await _open(user)
    settled = await weekly_settlement_service._settle_one_position(pos, batch=batch, fx_rate=Decimal(1))
    assert settled == "settled"
    return pos


def _units(rows, action: str) -> float:
    return sum(float(r["quantity"]) for r in rows if r["action"] == action)


async def test_a_weekly_settlement_prints_its_close_and_its_reopen(c):
    await market(c, "BUY", 100, "1000")
    old = await _settle(c)
    await market(c, "SELL", 100, "1150")

    rows = await tradebook_builder.trade_rows(c.id, None, None)
    assert [(r["action"], r["quantity"], Decimal(r["price"])) for r in rows] == [
        ("SELL", 100, Decimal("1150")),
        ("BUY", 100, Decimal("1100")),  # the re-open, listed above its close
        ("SELL", 100, Decimal("1100")),  # the settlement's close
        ("BUY", 100, Decimal("1000")),
    ]
    reopen, close = rows[1], rows[2]
    assert reopen["executed_at"] == close["executed_at"] == (await Position.get(old.id)).closed_at
    assert (reopen["id"], close["id"]) == (f"settle_open_{old.id}", f"settle_{old.id}")
    assert reopen["total_charges"] == close["total_charges"] == "0"
    assert _units(rows, "BUY") == _units(rows, "SELL")


async def test_a_settlement_whose_reopen_never_landed_keeps_one_leg(c):
    await market(c, "BUY", 100, "1000")
    old = await _settle(c)
    await PositionSettlement.find(PositionSettlement.old_position_id == old.id).update(
        {"$set": {"new_position_id": None, "status": "FAILED"}}
    )

    rows = await tradebook_builder.trade_rows(c.id, None, None)
    assert [r["action"] for r in rows] == ["SELL", "BUY"]


async def test_an_expiry_gets_one_leg_of_the_units_held(c):
    await market(c, "BUY", 150, "250")
    await market(c, "SELL", 75, "251")
    settled = await position_service.settle_expired_position(await _open(c), settlement_price=Decimal("252"))
    assert settled == "settled"

    rows = await tradebook_builder.trade_rows(c.id, None, None)
    assert [(r["action"], r["quantity"]) for r in rows] == [("SELL", 75), ("SELL", 75), ("BUY", 150)]
    assert not any(r["id"].startswith("settle_open_") for r in rows)  # no invented re-open
    assert _units(rows, "BUY") == _units(rows, "SELL")  # was 225 sold against 150 bought
