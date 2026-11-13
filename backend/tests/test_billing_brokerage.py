"""§4.2 The billing statement's own tables add up to what the ledger booked.

Each row bills only the brokerage legs charged inside the window:
- a weekly or expiry settlement charges nothing itself, so its row carries the
  opening legs of the lots it drained (it printed ₹0 and those legs were lost);
- a lot still open at period end carries its in-window opening leg on its
  Carry Forward row (CF printed a hard-coded 0);
- a close bills its opening leg only when the open was inside the window.
Financial Standings' Brokerage is everything charged in the window, not just
the closed rows. CF Value is units × price (not × lot_size again), and an
expiry settles the units actually held, not the position's peak.

Every fill goes through the matching engine and the settlements are the real
jobs; nothing is seeded on the ledger. The arbiter is the ledger itself:
Σ row brokerage == Financial Standings Brokerage == −Σ CHARGES in the window,
Σ row Gross == Financial Standings Gross == Σ PNL in the window, Net = Gross −
Brokerage.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.core.database import _document_models
from app.models.position import Position, PositionStatus
from app.models.position_settlement import SettlementBatch
from app.models.trade import Trade
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service, tradebook_builder, weekly_settlement_service
from app.utils.time_utils import now_utc, to_ist
from tests._real_fill import INSTRUMENT, later, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_billing_brokerage"
CENT = Decimal("0.01")
WEEK = 7 * 24 * 60
FUT = INSTRUMENT.model_copy(update={"token": "TFUT1", "symbol": "TFUT26NOVFUT", "lot_size": 75})


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs
    u = User(
        user_code="BBK001",
        email="bbk001@example.com",
        mobile="9377654321",
        full_name="BBK001",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
    )
    await u.insert()
    await Wallet(user_id=u.id, available_balance=Decimal128("100000000")).insert()
    try:
        yield SimpleNamespace(db=client[_DB], c=u)
    finally:
        await client.drop_database(_DB)
        client.close()


def _d(v) -> Decimal:
    return Decimal(str(v).replace(",", ""))


def _day(dt) -> str:
    return to_ist(dt).date().isoformat()


async def _filled_on(trade: Trade) -> str:
    """The IST day a fill sits on now (after `later` moved it)."""
    return _day((await Trade.get(trade.id)).executed_at)


async def _ledger(user: User, lo, hi, kind: TransactionType) -> Decimal:
    rows = await WalletTransaction.find(
        {"user_id": user.id, "transaction_type": kind.value, "created_at": {"$gte": lo, "$lte": hi}}
    ).to_list()
    return sum((_d(r.amount) for r in rows), Decimal(0))


async def _statement(user: User, day: str):
    """The statement for one IST day, checked against itself and the ledger."""
    lo, hi = tradebook_builder.window(day, day)
    p = await tradebook_builder.statement_payload(user, lo, hi)
    trades = [r for r in p["closed_transactions"] if r["type"] == "Close"]
    row_brk = sum((_d(r["brokerage"]) for r in trades), Decimal(0))
    cf_brk = sum((_d(d["commission"]) for d in p["opened_deals"]), Decimal(0))
    gross = sum((_d(r["total_pnl"]) for r in trades), Decimal(0)).quantize(CENT)
    fin = {k: _d(v) for k, v in p["financial"].items()}

    assert (row_brk + cf_brk).quantize(CENT) == fin["brokerage"] == _d(p["total_brokerage"])
    assert fin["brokerage"] == -await _ledger(user, lo, hi, TransactionType.CHARGES)
    assert gross == fin["gross_pnl"] == await _ledger(user, lo, hi, TransactionType.PNL)
    assert fin["net_pnl"] == fin["gross_pnl"] - fin["brokerage"]
    return SimpleNamespace(trades=trades, cf=p["opened_deals"], fin=fin, payload=p)


async def _settle_weekly(user: User, monkeypatch, price: str) -> None:
    async def _ltp(_token):
        return Decimal(price)

    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    batch = SettlementBatch(run_key=f"T{price}", week_key=f"2026-W{price}", started_at=now_utc())
    await batch.insert()
    pos = await Position.find_one(Position.user_id == user.id, Position.status == PositionStatus.OPEN)
    settled = await weekly_settlement_service._settle_one_position(pos, batch=batch, fx_rate=Decimal(1))
    assert settled == "settled"


async def test_a_weekly_settlement_row_carries_the_opening_leg_it_drained(w, monkeypatch):
    await market(w.c, "BUY", 100, "1000")  # ₹100 charged
    await _settle_weekly(w.c, monkeypatch, "1100")  # +10,000, charges nothing
    await market(w.c, "SELL", 100, "1150")  # +5,000 off the 1100 re-open, ₹100 charged

    s = await _statement(w.c, _day(now_utc()))
    by_comment = {r["comment"]: r for r in s.trades}
    assert _d(by_comment["Weekly settlement"]["brokerage"]) == Decimal("100")  # was 0: the buy's ₹100 lost
    assert _d(by_comment[""]["brokerage"]) == Decimal("100")  # the close leg only
    assert s.fin["brokerage"] == Decimal("200")
    assert s.fin["gross_pnl"] == Decimal("15000")


async def test_a_settlement_week_bills_its_own_open_leg_and_the_next_week_its_close(w, monkeypatch):
    buy = await market(w.c, "BUY", 100, "1000")
    await _settle_weekly(w.c, monkeypatch, "1100")
    await later(w.db, minutes=WEEK)
    settled_on = await _filled_on(buy)
    await market(w.c, "SELL", 100, "1150")

    week1 = await _statement(w.c, settled_on)
    assert [_d(r["brokerage"]) for r in week1.trades] == [Decimal("100")]
    assert [_d(d["commission"]) for d in week1.cf] == [Decimal("0")]  # the re-open charged nothing
    week2 = await _statement(w.c, _day(now_utc()))
    assert [_d(r["brokerage"]) for r in week2.trades] == [Decimal("100")]
    assert (week1.fin["gross_pnl"], week2.fin["gross_pnl"]) == (Decimal("10000"), Decimal("5000"))


async def test_a_close_bills_only_the_legs_charged_inside_the_window(w):
    buy = await market(w.c, "BUY", 10, "100")  # ₹10
    await later(w.db, minutes=WEEK)
    opened_on = await _filled_on(buy)
    await market(w.c, "SELL", 10, "110")  # ₹10

    # The week the position opened: nothing closed, its ₹10 sits on the CF row.
    opened = await _statement(w.c, opened_on)
    assert opened.trades == []
    assert [(_d(d["amount"]), _d(d["commission"])) for d in opened.cf] == [(Decimal("10"), Decimal("10"))]
    assert opened.fin["brokerage"] == Decimal("10")  # was 0: closed rows only

    # The week it closed: the close leg alone, not October's open leg again.
    closed = await _statement(w.c, _day(now_utc()))
    assert [_d(r["brokerage"]) for r in closed.trades] == [Decimal("10")]  # was 20
    assert closed.cf == []
    assert closed.fin["gross_pnl"] == Decimal("100")


async def test_a_partly_closed_lot_splits_its_open_leg_between_the_close_and_cf(w):
    await market(w.c, "BUY", 10, "100")  # opened today, still open
    await market(w.c, "BUY", 5, "102")
    await market(w.c, "SELL", 6, "105")  # takes 6 off the first lot
    s = await _statement(w.c, _day(now_utc()))
    # 10 + 5 open legs + 6 close = 21; the close row bills 6 of the first lot.
    assert [_d(r["brokerage"]) for r in s.trades] == [Decimal("12")]
    assert [_d(d["commission"]) for d in s.cf] == [Decimal("9")]
    assert _d(s.cf[0]["amount"]) == Decimal("9")


async def test_cf_value_is_units_times_price_not_times_lot_size_again(w):
    await market(w.c, "BUY", 1500, "200", instrument=FUT)  # 20 lots of 75: 1,500 units
    pos = await Position.find_one(Position.user_id == w.c.id)
    s = await _statement(w.c, _day(now_utc()))
    [cf] = s.cf
    assert _d(cf["amount"]) == Decimal("1500")
    assert _d(cf["value"]) == Decimal("1500") * _d(pos.ltp)  # ×75 printed 22,50,00,000
    fill = await Trade.find_one(Trade.user_id == w.c.id)
    assert _d(cf["commission"]) == _d(fill.total_charges) > 0


async def test_an_expiry_settles_the_units_held_not_the_peak(w):
    await market(w.c, "BUY", 150, "250")  # ₹150
    await market(w.c, "SELL", 75, "251")  # +75, ₹75
    await later(w.db, minutes=5)
    pos = await Position.find_one(Position.user_id == w.c.id, Position.status == PositionStatus.OPEN)
    assert await position_service.settle_expired_position(pos, settlement_price=Decimal("252")) == "settled"

    s = await _statement(w.c, _day(now_utc()))
    expiry = [r for r in s.trades if r["comment"] == "Expiry settlement"]
    assert len(expiry) == 1
    assert _d(expiry[0]["amount"]) == Decimal("75")  # was 150 (opening_quantity, the peak)
    assert _d(expiry[0]["brokerage"]) == Decimal("75")  # the drained half of the buy's ₹150
    assert s.fin["gross_pnl"] == Decimal("225")  # 75 + 75 × 2; was 375
    assert s.fin["brokerage"] == Decimal("225")


async def test_the_statement_writes_nothing(w, monkeypatch):
    await market(w.c, "BUY", 10, "100")
    await _settle_weekly(w.c, monkeypatch, "105")
    counts = {n: await w.db[n].count_documents({}) for n in await w.db.list_collection_names()}
    lo, hi = tradebook_builder.window(_day(now_utc() - timedelta(days=1)), _day(now_utc()))
    await tradebook_builder.statement_payload(w.c, lo, hi)
    await tradebook_builder.trade_rows(w.c.id, lo, hi)
    assert counts == {n: await w.db[n].count_documents({}) for n in await w.db.list_collection_names()}
