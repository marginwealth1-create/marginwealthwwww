"""§5 The admin ledger strip buckets every row through one type set.

The LedgerSheet summed Total Deposits / Total Withdrawals / Net Cash Flow in
the browser over the newest 500 rows of ALL types, so an active trader's
older deposits fell off the end; and it bucketed every ADJUSTMENT by sign, so
a system-written one (the EOD carry-forward charge, a bogus-fill reversal)
read as an admin withdrawal / deposit. One set next to TransactionType decides
each row's bucket: REVERSAL and system adjustments are trading, never cash,
and the server sums the strip over every row.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import ledger as admin_ledger
from app.api.v1.admin import money_transactions, trading, users
from app.core.config import settings
from app.core.database import _document_models
from app.models.netting import NettingFieldsRequired, NettingSegment
from app.models.position import Position, PositionStatus
from app.models.transaction import TransactionType as T
from app.models.transaction import WalletTransaction, ledger_bucket
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.schemas.admin.common import WalletAdjustRequest
from app.services import market_data_service, position_service, wallet_service
from tests._real_fill import INSTRUMENT, market, set_commission_per_lot

ROUND = INSTRUMENT.model_copy(update={"token": "TLS1", "symbol": "TLS26ROUND"})
CARRY = INSTRUMENT.model_copy(
    update={"token": "TLS2", "symbol": "TLSCARRY", "exchange": "MCX", "segment": "MCX_FUTURE"}
)


@pytest_asyncio.fixture
async def w(monkeypatch, request):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    db = f"{settings.MONGODB_DB_NAME}_{request.module.__name__.rsplit('.', 1)[-1]}"  # one per importer
    await client.drop_database(db)
    await init_beanie(database=client[db], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    async def _quote(_token):
        return {"ltp": "100"}

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(market_data_service, "get_quote", _quote)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs
    await NettingSegment(
        name="MCX_FUT",
        displayName="MCX_FUT",
        **{**NettingFieldsRequired().model_dump(), "commission": 1.0, "carryForwardChargePercent": 0.5},
    ).insert()
    sa = User(
        user_code="TLS900",  # not an ADM/SUP/BRK code: no narration filter keys on it
        email="tls900@example.com",
        mobile="9351600900",
        full_name="Ops",
        password_hash="x",
        role=UserRole.SUPER_ADMIN,
        status=UserStatus.ACTIVE,
    )
    u = User(
        user_code="TLS001",
        email="tls001@example.com",
        mobile="9351600001",
        full_name="TLS001",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
    )
    await sa.insert()
    await u.insert()
    await Wallet(user_id=u.id, available_balance=Decimal128("1000000")).insert()
    try:
        yield SimpleNamespace(u=u, sa=sa)
    finally:
        await client.drop_database(db)
        client.close()


async def scenario(w) -> None:
    """A client's month: a deposit, an operator Add / Deduct Fund, a position
    carried overnight (the carry fee), a round trip an admin then deletes."""
    u, sa = w.u, w.sa
    await wallet_service.adjust(
        u.id, Decimal(10000), transaction_type=T.DEPOSIT, narration="Deposit approved"
    )
    await users.wallet_adjust(str(u.id), WalletAdjustRequest(amount=1000, narration="Add fund"), sa)
    await users.wallet_adjust(str(u.id), WalletAdjustRequest(amount=-300, narration="Deduct fund"), sa)
    await market(u, "BUY", 10, "100", instrument=CARRY)
    out = await position_service.convert_intraday_to_carry({CARRY.segment})
    assert out["charged"] == 1  # 0.5% of 10 × 100 = ₹5, as an ADJUSTMENT
    await market(u, "BUY", 5, "200", instrument=ROUND)
    await market(u, "SELL", 5, "210", instrument=ROUND)
    closed = await Position.find_one(
        {"user_id": u.id, "instrument.token": ROUND.token, "status": PositionStatus.CLOSED.value}
    )
    await trading.delete_position(str(closed.id), sa, None)


def test_reversal_and_system_adjustments_bucket_as_trading():
    assert ledger_bucket(T.REVERSAL, "-50", "Delete X — reverse close P&L") == "trading"
    assert ledger_bucket(T.REVERSAL, "40", "Reopen X — reverse close P&L") == "trading"
    assert ledger_bucket(T.PNL, "50", "") == ledger_bucket(T.CHARGES, "-5", "") == "trading"
    assert ledger_bucket(T.ADJUSTMENT, "-5", "Carry-forward charge 0.5% · GOLD") == "trading"
    assert ledger_bucket(T.ADJUSTMENT, "8", "Bogus 0-price fill reversal Reversal trade=1") == "trading"
    assert ledger_bucket(T.ADJUSTMENT, "1000", "Add fund") == "deposit"
    assert ledger_bucket(T.ADJUSTMENT, "-300", "Deduct fund") == "withdrawal"
    assert ledger_bucket(T.SETTLEMENT_OUTSTANDING_BOOKED, "-9", "") == "settlement"


async def test_the_strip_sums_every_cash_row_and_no_trading_row(w):
    await scenario(w)
    rows = await WalletTransaction.find({"user_id": w.u.id}).to_list()
    by_type = {r.transaction_type for r in rows}
    assert {T.ADJUSTMENT, T.REVERSAL, T.PNL, T.CHARGES} <= by_type
    carry = next(r for r in rows if (r.narration or "").startswith("Carry-forward charge"))
    assert Decimal(str(carry.amount)) == Decimal("-5")

    # One row per page: the strip is the whole ledger's, not the page's.
    page = (
        await admin_ledger.list_all(w.sa, user_id=str(w.u.id), transaction_type=None, page=1, page_size=1)
    ).data
    assert len(page["items"]) == 1
    s = page["summary"]
    assert Decimal(s["deposits"]) == Decimal("11000")  # 10,000 deposit + 1,000 Add Fund
    assert Decimal(s["withdrawals"]) == Decimal("300")  # the Deduct Fund, not the carry fee
    assert Decimal(s["net"]) == Decimal("10700")

    # Payments → Money reads the same patterns: the carry fee is no Deduct Fund.
    money = await money_transactions.money_by_user(w.sa, preset="all", from_date=None, to_date=None)
    [row] = money.data["users"]
    assert (row["deposit"], row["add_fund"], row["deduct"]) == (10000.0, 1000.0, 300.0)  # deduct was 305


async def test_the_sheet_asks_for_its_types_server_side(w):
    await scenario(w)
    got = (
        await admin_ledger.list_all(
            w.sa, user_id=str(w.u.id), transaction_type="DEPOSIT,WITHDRAWAL", page=1, page_size=500
        )
    ).data["items"]
    assert [i["transaction_type"] for i in got] == ["DEPOSIT"]
    adj = (
        await admin_ledger.list_all(
            w.sa, user_id=str(w.u.id), transaction_type="ADJUSTMENT", page=1, page_size=500
        )
    ).data["items"]
    buckets = {i["narration"].split(" ")[0]: i["bucket"] for i in adj}
    assert buckets == {"Add": "deposit", "Deduct": "withdrawal", "Carry-forward": "trading"}
