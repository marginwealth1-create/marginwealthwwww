"""Statement extras: the walk reads the whole book, and USD rows bill rupees.

1. The FIFO walk read only the newest 3000 fills. For an active client a
   statement window older than that printed no closed rows at all, and a close
   whose open had fallen off the cap lost its open leg's brokerage.
2. The wallet books a USD close at gross × fx_rate (the engine and the weekly
   settlement both do), but the statement summed the walk's NATIVE gross, and
   the walk's settlement rows carried no rate at all.

Every fill goes through the matching engine; the settlement is the real weekly
job. The 3000 padding fills are copies of one engine-written round trip. The
arbiter is the flat-book oracle: Σ(sell value) − Σ(buy value), times the rate
the wallet moved at, equals Σ realized_pnl_inr (what the wallet booked) and
Σ statement P&L.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.core.database import _document_models
from app.models._base import Exchange, OrderAction
from app.models.order import InstrumentRef
from app.models.position import Position, PositionStatus
from app.models.position_settlement import SettlementBatch
from app.models.trade import Trade
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service, weekly_settlement_service
from app.services.tradebook_builder import fifo_closed_rows
from app.utils.time_utils import now_utc
from tests._real_fill import INSTRUMENT, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_fifo_full_history_and_fx"
CENT = Decimal("0.01")
FX = Decimal("97")

PAD = INSTRUMENT.model_copy(update={"token": "TPAD1", "symbol": "TPAD", "trading_symbol": "TPAD-EQ"})
USD = InstrumentRef(
    token="TUSD1",
    symbol="TUSD",
    trading_symbol="TUSD",
    exchange=Exchange.CRYPTO,
    segment="FOREX",
    lot_size=1,
)


@pytest_asyncio.fixture
async def c(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setitem(market_data_service._usd_inr_cache, "rate", float(FX))
    await set_commission_per_lot(1)  # ₹1 a unit, both legs
    u = User(
        user_code="FHF001",
        email="fhf001@example.com",
        mobile="9387654321",
        full_name="FHF001",
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


async def _oracle(user: User) -> Decimal:
    value = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    for t in await Trade.find(Trade.user_id == user.id).to_list():
        value[t.action] += Decimal(str(t.price)) * Decimal(str(t.quantity))
    return value[OrderAction.SELL] - value[OrderAction.BUY]


async def _wallet_booked(user: User) -> Decimal:
    positions = await Position.find(Position.user_id == user.id).to_list()
    return sum((position_service.realized_pnl_inr(p) for p in positions), Decimal(0))


async def _statement(user: User, lo=None, hi=None):
    rows, net, brk = await fifo_closed_rows(user.id, lo, hi)
    total = sum((Decimal(str(r["gross_pnl"])) for r in rows), Decimal(0)).quantize(CENT)
    assert Decimal(str(net)) == total
    return rows, total, Decimal(str(brk))


async def test_an_old_statement_window_still_has_its_rows(c):
    t0 = now_utc() - timedelta(days=30)
    await market(c, "BUY", 10, "100", at=t0)
    await market(c, "SELL", 10, "130", at=t0 + timedelta(minutes=1))  # +300

    # 3000 newer fills: a flat round trip on another script, copied 1500 times.
    buy = await market(c, "BUY", 1, "50", instrument=PAD)
    sell = await market(c, "SELL", 1, "50", instrument=PAD)
    coll = Trade.get_motor_collection()
    pair = [await coll.find_one({"_id": t.id}, {"_id": 0}) for t in (buy, sell)]
    day1 = t0 + timedelta(days=1)
    await coll.insert_many(
        [
            {**d, "trade_number": f"PAD{i}_{j}", "executed_at": day1 + timedelta(seconds=2 * i + j)}
            for i in range(1500)
            for j, d in enumerate(pair)
        ]
    )

    rows, total, brk = await _statement(c, t0 - timedelta(hours=1), t0 + timedelta(hours=1))
    assert [r["script"] for r in rows] == ["TBKG"]
    assert total == Decimal("300")
    assert brk == Decimal("20")  # the open leg's ₹10 too, not just the close's

    # Whole book: flat, the padding nets 0.
    _, total, _ = await _statement(c)
    assert total == await _oracle(c) == await _wallet_booked(c) == Decimal("300")


async def test_a_usd_close_bills_the_rupees_the_wallet_booked(c):
    await market(c, "BUY", 10, "100", instrument=USD)
    await market(c, "BUY", 10, "103", instrument=USD)
    close = await market(c, "SELL", 20, "110", instrument=USD)
    assert Decimal(str(close.fx_rate)) == FX

    _, total, _ = await _statement(c)
    booked = await _wallet_booked(c)
    assert booked == (await _oracle(c) * FX).quantize(CENT) == Decimal("16490")
    assert total == booked


async def test_a_usd_weekly_settlement_row_bills_rupees(c, monkeypatch):
    await market(c, "BUY", 10, "100", instrument=USD)

    async def _ltp(_token):
        return Decimal("105")

    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    batch = SettlementBatch(run_key="T", week_key="2026-W46", started_at=now_utc())
    await batch.insert()
    pos = await Position.find_one(Position.user_id == c.id, Position.status == PositionStatus.OPEN)
    assert await weekly_settlement_service._settle_one_position(pos, batch=batch, fx_rate=FX) == "settled"
    await market(c, "SELL", 10, "110", instrument=USD)  # +50 a unit off the 105 re-open

    rows, total, _ = await _statement(c)
    assert sorted(r["comment"] for r in rows) == ["", "Weekly settlement"]
    booked = await _wallet_booked(c)
    assert booked == (await _oracle(c) * FX).quantize(CENT) == Decimal("9700")
    assert total == booked
