"""§4.3 Reports → P&L counts every rupee the wallet booked as P&L.

A weekly or expiry settlement, and the netting of opposite carried lots
(POSITION_DEFRAG), book P&L straight to the wallet without writing a fill, so
a report summed off fills alone showed a settled week as a flat zero. And a
close's pnl_inr is already net of ITS OWN leg's brokerage: the report read
Σ pnl_inr as Net (the opening leg never came off) and Σ pnl_inr + Σ charges
as Gross (the opening leg added in).

Arbiter, per script and in total: Gross == Σ wallet PNL (+ the overflow a loss
booked to settlement outstanding) in the window, Brokerage == −Σ CHARGES in
the window, Net == Gross − Brokerage. Every fill goes through the matching
engine and every settlement is the real job; nothing is seeded on the ledger.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import trading
from app.api.v1.user.reports import _pnl_payload
from app.core.config import settings
from app.core.database import _document_models
from app.models.position import Position, PositionStatus
from app.models.position_settlement import SettlementBatch
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service, report_pdf_service, weekly_settlement_service
from app.utils.time_utils import now_utc
from tests._real_fill import INSTRUMENT, later, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_reports_pnl_settlement"
SYM = INSTRUMENT.symbol
DAY = 24 * 60


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(trading, "publish", _no_publish)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs

    async def mk(code: str, role: UserRole, balance: str, auto: bool = False) -> User:
        u = User(
            user_code=code,
            email=f"{code.lower()}@example.com",
            mobile=f"93{abs(hash(code)) % 10**8:08d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            auto_settlement=auto,  # a loss past cash books to settlement outstanding
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128(balance)).insert()
        return u

    try:
        yield SimpleNamespace(
            db=client[_DB],
            c=await mk("RPL001", UserRole.CLIENT, "100000000"),
            poor=await mk("RPL002", UserRole.CLIENT, "1000", auto=True),
            sa=await mk("RPLSA", UserRole.SUPER_ADMIN, "0"),
        )
    finally:
        await client.drop_database(_DB)
        client.close()


def _d(v) -> Decimal:
    return Decimal(str(v))


async def _ledger(user: User, lo, hi, *kinds: TransactionType) -> Decimal:
    rows = await WalletTransaction.find(
        {
            "user_id": user.id,
            "transaction_type": {"$in": [k.value for k in kinds]},
            "created_at": {"$gte": lo, "$lte": hi},
        }
    ).to_list()
    return sum((r.amount.to_decimal() for r in rows), Decimal(0))


async def _report(user: User, *, ledger: bool = True) -> SimpleNamespace:
    """The report over the last 30 days, checked against itself and, unless
    an admin action reversed something, against the ledger."""
    lo, hi = now_utc() - timedelta(days=30), now_utc()
    r = await _pnl_payload(user, lo, hi)
    gross = sum((_d(x["realized_pnl"]) for x in r["rows"]), Decimal(0))
    brk = sum((_d(x["brokerage"]) for x in r["rows"]), Decimal(0))
    net = sum((_d(x["net_pnl"]) for x in r["rows"]), Decimal(0))

    # The script rows add up to the totals, and Net is Gross − Brokerage.
    assert gross == _d(r["total_realized"])
    assert brk == _d(r["total_brokerage"]) == _d(r["total_charges"])
    assert net == _d(r["total_net"]) == _d(r["net_pnl"]) == gross - brk
    for x in r["rows"]:
        assert _d(x["net_pnl"]) == _d(x["realized_pnl"]) - _d(x["brokerage"])
    # The PDF's rows say the same, in plain numbers.
    assert sum((_d(s["pnl"]) for s in r["by_symbol"]), Decimal(0)) == net
    assert not any(isinstance(v, Decimal) for s in r["by_symbol"] for v in s.values())

    if ledger:
        assert gross == await _ledger(
            user, lo, hi, TransactionType.PNL, TransactionType.SETTLEMENT_OUTSTANDING_BOOKED
        )
        assert brk == -(await _ledger(user, lo, hi, TransactionType.CHARGES))
    rows = {x["symbol"]: x for x in r["rows"]}
    return SimpleNamespace(gross=gross, brk=brk, net=net, rows=rows, raw=r)


async def _settle_weekly(user: User, monkeypatch, price: str) -> None:
    async def _ltp(_token):
        return Decimal(price)

    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    batch = SettlementBatch(run_key=f"T{price}", week_key=f"2026-W{price}", started_at=now_utc())
    await batch.insert()
    pos = await Position.find_one(Position.user_id == user.id, Position.status == PositionStatus.OPEN)
    settled = await weekly_settlement_service._settle_one_position(pos, batch=batch, fx_rate=Decimal(1))
    assert settled == "settled"


async def _expire(user: User, price: str) -> Position:
    pos = await Position.find_one(Position.user_id == user.id, Position.status == PositionStatus.OPEN)
    assert await position_service.settle_expired_position(pos, settlement_price=Decimal(price)) == "settled"
    return pos


async def _no_quote(_token: str) -> dict:
    return {}


async def test_a_close_bills_each_legs_brokerage_once(w):
    await market(w.c, "BUY", 10, "100")  # ₹10
    await market(w.c, "SELL", 10, "110")  # +100, ₹10
    r = await _report(w.c)
    # Was Net 90 (the open leg never came off) and Gross 110 (it was added in).
    assert (r.gross, r.brk, r.net) == (Decimal("100"), Decimal("20"), Decimal("80"))


async def test_a_weekly_settled_loss_is_in_the_report(w, monkeypatch):
    await market(w.c, "BUY", 100, "1400")  # ₹100
    await _settle_weekly(w.c, monkeypatch, "1250")  # −15,000 to the wallet, no fill
    r = await _report(w.c)
    assert r.gross == Decimal("-15000")  # was a flat 0
    assert _d(r.rows[SYM]["net_pnl"]) == Decimal("-15100")

    await market(w.c, "SELL", 100, "1260")  # +1,000 off the 1250 re-open, ₹100
    r = await _report(w.c)
    assert (r.gross, r.brk, r.net) == (Decimal("-14000"), Decimal("200"), Decimal("-14200"))  # read +900
    assert list(r.rows) == [SYM]
    assert _d(r.rows[SYM]["settlement_pnl"]) == Decimal("-15000")
    assert report_pdf_service.build_pnl_pdf(w.c, r.raw)[:4] == b"%PDF"


async def test_a_loss_past_the_balance_counts_what_went_to_outstanding(w, monkeypatch):
    await market(w.poor, "BUY", 100, "1400")  # ₹100 of the ₹1,000
    await _settle_weekly(w.poor, monkeypatch, "1250")  # ₹900 from cash, ₹14,100 to outstanding
    outstanding = await WalletTransaction.find_one(
        {"user_id": w.poor.id, "transaction_type": TransactionType.SETTLEMENT_OUTSTANDING_BOOKED.value}
    )
    assert outstanding is not None
    r = await _report(w.poor)
    assert r.gross == Decimal("-15000")  # not the −900 the PNL row alone carries


async def test_an_expiry_settlement_is_in_the_report(w):
    await market(w.c, "BUY", 150, "250")  # ₹150
    await market(w.c, "SELL", 75, "251")  # +75, ₹75
    await _expire(w.c, "254")  # +300 on the 75 held, no fill
    r = await _report(w.c)
    # Was Gross 225, Net 0: the expiry missing, the buy's leg in and out again.
    assert (r.gross, r.brk, r.net) == (Decimal("375"), Decimal("225"), Decimal("150"))


async def test_a_reopened_expiry_leaves_the_report_and_a_re_expiry_counts_once(w):
    await market(w.c, "BUY", 150, "250")
    await market(w.c, "SELL", 75, "251")
    pos = await _expire(w.c, "252")
    await later(w.db, minutes=5)
    await trading.admin_reopen_position(str(pos.id), w.sa, None)  # undoes the expiry and the sell

    r = await _report(w.c, ledger=False)  # the reopen's REVERSAL moved the ledger
    assert r.gross == Decimal("0")  # the reversed expiry is out
    assert r.brk == Decimal("150")  # the buy still stands

    await later(w.db, minutes=5)
    await _expire(w.c, "253")  # the contract is dead: it settles again, 150 × 3
    r = await _report(w.c, ledger=False)
    assert r.gross == Decimal("450")  # not 600: the reversed expiry stays out


async def test_a_netting_of_carried_lots_is_in_the_report(w):
    await market(w.c, "BUY", 10, "100")  # ₹10, carried overnight
    with patch.object(market_data_service, "get_quote", _no_quote):
        await position_service.convert_intraday_to_carry({INSTRUMENT.segment})
    await later(w.db, minutes=DAY)
    await market(w.c, "SELL", 10, "110")  # ₹10, a fresh intraday short
    with patch.object(market_data_service, "get_quote", _no_quote):
        await position_service.convert_intraday_to_carry({INSTRUMENT.segment})
    netted = await WalletTransaction.find_one({"user_id": w.c.id, "reference_type": "POSITION_DEFRAG"})
    assert netted is not None and netted.amount.to_decimal() == Decimal("100")
    assert await Position.find(Position.user_id == w.c.id).count() == 0  # the lots were deleted

    r = await _report(w.c)
    assert (r.gross, r.brk, r.net) == (Decimal("100"), Decimal("20"), Decimal("80"))  # Gross read 20
    assert list(r.rows) == [SYM]


async def test_the_report_writes_nothing(w, monkeypatch):
    await market(w.c, "BUY", 10, "100")
    await _settle_weekly(w.c, monkeypatch, "105")
    counts = {n: await w.db[n].count_documents({}) for n in await w.db.list_collection_names()}
    await _report(w.c)
    assert counts == {n: await w.db[n].count_documents({}) for n in await w.db.list_collection_names()}
