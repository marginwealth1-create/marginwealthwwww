"""§4.1 One closed trade on one line, both legs side by side.

The statement prints Buy Rate / Sell Rate and Buy Time / Sell Time. A short
sells first and buys back later, so its Sell Time is the EARLIER of the two
timestamps — getting it backwards swaps every price on the statement and still
looks plausible. `tradebook_builder.pair_legs` is the one place a row is
oriented; the cases below pin it, and the real-fill cases pin the rows built
from it (a settlement row's buy leg is when the position opened, not the
settlement instant).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.core.database import _document_models
from app.models.position import Position, PositionStatus
from app.models.position_settlement import SettlementBatch
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, tradebook_builder, weekly_settlement_service
from app.services.tradebook_builder import pair_legs
from app.utils.time_utils import UTC, now_utc, to_ist
from tests._real_fill import later, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_tradebook_paired_rows"
T10 = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)  # 10:00 IST
T14 = T10 + timedelta(hours=4)  # 14:00 IST


def test_a_long_buys_at_the_entry_first():
    assert pair_legs("BUY", 100, 110, T10, T14) == (100, 110, T10, T14)


def test_a_short_sells_first_so_its_sell_time_is_the_earlier():
    buy_rate, sell_rate, buy_time, sell_time = pair_legs("SELL", 24500, 24400, T10, T14)
    assert (sell_rate, sell_time) == (24500, T10)  # the entry: sold at 10:00
    assert (buy_rate, buy_time) == (24400, T14)  # the cover: bought back at 14:00
    assert sell_time < buy_time


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    await set_commission_per_lot(1)
    u = User(
        user_code="TPR001",
        email="tpr001@example.com",
        mobile="9367654321",
        full_name="TPR001",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
    )
    await u.insert()
    await Wallet(user_id=u.id, available_balance=Decimal128("100000000")).insert()
    try:
        yield client[_DB], u
    finally:
        await client.drop_database(_DB)
        client.close()


async def test_a_real_short_prints_its_sell_leg_first(w):
    db, u = w
    await market(u, "SELL", 50, "24500")
    await later(db, minutes=240)
    await market(u, "BUY", 50, "24400")

    [row] = (await tradebook_builder.statement_payload(u, None, None))["closed_transactions"]
    assert (row["sell_rate"], row["buy_rate"]) == (24500, 24400)
    assert row["sell_time"] < row["buy_time"]
    assert row["gross_pnl"] == 5000
    assert row["net_pnl"] == 5000 - 100  # ₹50 a leg


async def test_a_settlement_row_buys_when_the_position_opened(w, monkeypatch):
    db, u = w
    await market(u, "BUY", 10, "100")
    await later(db, minutes=60)

    async def _ltp(_token):
        return Decimal("105")

    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    batch = SettlementBatch(run_key="TPR", week_key="2026-W46", started_at=now_utc())
    await batch.insert()
    pos = await Position.find_one(Position.user_id == u.id, Position.status == PositionStatus.OPEN)
    settled = await weekly_settlement_service._settle_one_position(pos, batch=batch, fx_rate=Decimal(1))
    assert settled == "settled"

    [row] = (await tradebook_builder.statement_payload(u, None, None))["closed_transactions"]
    assert row["comment"] == "Weekly settlement"
    assert (row["buy_rate"], row["sell_rate"]) == (100, 105)
    # Was the settlement instant on both legs.
    assert row["buy_time"] == to_ist(pos.opened_at).strftime("%Y-%m-%d %H:%M:%S") < row["sell_time"]
