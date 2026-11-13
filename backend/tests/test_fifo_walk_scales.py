"""The FIFO walk reads a client's whole fill history (no 3000-trade cap since
the statement fix), so every per-event step must stay cheap in the number of
the client's positions: matching each close to its Position's close_reason
scanned every closed position on the script, once per row — a day-trader with
years on one script froze the Closed tab, the statement PDF and the event loop
(pending orders, stop-outs) it shares.

Seeded in bulk on purpose: this checks cost, not the fill path (the fill path's
arithmetic is covered by the real-order tests beside it).
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest_asyncio
from beanie import PydanticObjectId, init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.core.database import _document_models
from app.models._base import OrderAction, ProductType
from app.models.position import Position, PositionStatus
from app.models.trade import Trade
from app.services import position_service
from app.utils.time_utils import now_utc
from tests._real_fill import INSTRUMENT

_DB = f"{settings.MONGODB_DB_NAME}_test_fifo_walk_scales"
ROUND_TRIPS = 4000


@pytest_asyncio.fixture
async def db():
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)
    try:
        yield
    finally:
        await client.drop_database(_DB)
        client.close()


async def test_a_long_history_on_one_script_walks_in_linear_time(db):
    uid = PydanticObjectId()
    start = now_utc() - timedelta(days=400)
    trades, positions = [], []
    for i in range(ROUND_TRIPS):
        opened = start + timedelta(minutes=i)
        closed = opened + timedelta(seconds=30)
        for action, at, pnl in ((OrderAction.BUY, opened, None), (OrderAction.SELL, closed, "0.98")):
            trades.append(
                Trade(
                    trade_number=f"T{i}{action.value}",
                    order_id=PydanticObjectId(),
                    user_id=uid,
                    instrument=INSTRUMENT,
                    action=action,
                    product_type=ProductType.MIS,
                    quantity=1,
                    price=Decimal128("100" if pnl is None else "101"),
                    value=Decimal128("100"),
                    brokerage=Decimal128("0.01"),
                    pnl_inr=Decimal128(pnl) if pnl else None,
                    closed_quantity=1 if pnl else None,
                    executed_at=at,
                )
            )
        positions.append(
            Position(
                user_id=uid,
                instrument=INSTRUMENT,
                segment_type=INSTRUMENT.segment,
                product_type=ProductType.MIS,
                quantity=0,
                opened_side=OrderAction.BUY,
                opening_quantity=1,
                avg_price=Decimal128("100"),
                realized_pnl=Decimal128("1"),
                status=PositionStatus.CLOSED,
                opened_at=opened,
                closed_at=closed,
                close_reason="SL_HIT",
            )
        )
    await Trade.insert_many(trades)
    await Position.insert_many(positions)

    t0 = time.perf_counter()
    rows, total = await position_service.list_closed_trade_events_fifo(uid, limit=25)
    took = time.perf_counter() - t0

    assert total == ROUND_TRIPS
    assert {r["close_reason"] for r in rows} == {"SL_HIT"}
    assert took < 5, f"{took:.1f}s for {ROUND_TRIPS} round trips"
