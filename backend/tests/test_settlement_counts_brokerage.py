"""§3: house P&L in the sub-admin and broker weekly settlements includes the
brokerage the clients were actually charged.

Every fill below goes through the matching engine with a real netting
commission. No ledger row is seeded, so the settlement sees what the fill path
wrote (a CHARGES row and Trade.brokerage), not a BROKERAGE row shaped for it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.core.database import _document_models
from app.models.admin_settlement import AdminSettlement, AdminSettlementStatus
from app.models.broker_settlement import BrokerSettlement, BrokerSettlementStatus
from app.models.transaction import TransactionType
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import admin_settlement_service as admin_svc
from app.services import broker_settlement_service as broker_svc
from app.services import wallet_service
from app.utils.time_utils import now_utc
from tests._real_fill import market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_settlement_counts_brokerage"


def _d(v) -> Decimal:
    return Decimal(str(v))


@pytest_asyncio.fixture
async def pool():
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)
    await set_commission_per_lot(20)  # 25 qty → ₹500 a fill

    n = 0

    async def mk(role: UserRole, **kw) -> User:
        nonlocal n
        n += 1
        u = User(
            user_code=f"SCB{n:03d}",
            email=f"scb{n}@example.com",
            mobile=f"96000{n:05d}",
            full_name=f"U{n}",
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            **kw,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128("1000000")).insert()
        return u

    sa = await mk(UserRole.ADMIN, pnl_share_pct=Decimal128("40"))
    br = await mk(
        UserRole.BROKER,
        assigned_admin_id=sa.id,
        broker_pnl_share_pct=Decimal128("20"),
        broker_brokerage_share_pct=Decimal128("50"),
    )
    under = {"assigned_admin_id": sa.id, "assigned_broker_id": br.id, "broker_ancestry": [br.id]}
    client_ = await mk(UserRole.CLIENT, **under)
    demo = await mk(UserRole.CLIENT, is_demo=True, **under)
    try:
        yield sa, br, client_, demo
    finally:
        await client.drop_database(_DB)
        client.close()


async def _round_trip_loss(user: User) -> None:
    """BUY 25 @100, SELL 25 @90: a ₹250 loss plus ₹500 brokerage per leg."""
    await market(user, "BUY", 25, "100")
    await market(user, "SELL", 25, "90")


async def test_admin_settlement_counts_fill_brokerage_and_manual_brokerage(pool):
    sa, _br, client_, demo = pool
    await _round_trip_loss(client_)
    await _round_trip_loss(demo)  # demo trades never reach the house book
    # Admin manual BROKERAGE entry (ledger manual-entry path): a ₹100 debit.
    await wallet_service.adjust(
        client_.id, Decimal("-100"),
        transaction_type=TransactionType.BROKERAGE, narration="manual brokerage",
    )

    row, frozen = await admin_svc.compute_settlement(sa.id, now_utc())

    assert frozen is False
    assert row.user_count == 1  # client only: no demo, no broker
    assert _d(row.gross_user_loss_inr) == Decimal("250")
    assert _d(row.gross_user_profit_inr) == Decimal("0")
    assert _d(row.total_brokerage_inr) == Decimal("1100")  # 2 fills × 500 + 100
    assert _d(row.net_house_pnl_inr) == Decimal("1350")
    assert _d(row.sub_admin_share_inr) == Decimal("540")  # 40 %


async def test_broker_settlement_counts_fill_brokerage(pool):
    _sa, br, client_, demo = pool
    await _round_trip_loss(client_)
    await _round_trip_loss(demo)

    row, frozen = await broker_svc.compute_settlement(br.id, now_utc())

    assert frozen is False
    assert row.user_count == 1
    assert _d(row.total_brokerage_inr) == Decimal("1000")
    assert _d(row.net_house_pnl_inr) == Decimal("1250")
    # 20 % of the ₹250 P&L leg + 50 % of the ₹1,000 brokerage leg.
    assert _d(row.broker_share_inr) == Decimal("550")


async def test_closed_account_activity_still_counts(pool):
    sa, _br, client_, _demo = pool
    await _round_trip_loss(client_)
    client_.status = UserStatus.CLOSED
    await client_.save()

    row, _ = await admin_svc.compute_settlement(sa.id, now_utc())

    assert row.user_count == 1
    assert _d(row.total_brokerage_inr) == Decimal("1000")


async def test_frozen_admin_and_broker_rows_are_never_recomputed(pool):
    sa, br, client_, _demo = pool
    await _round_trip_loss(client_)
    a_row, _ = await admin_svc.compute_settlement(sa.id, now_utc())
    b_row, _ = await broker_svc.compute_settlement(br.id, now_utc())
    await admin_svc.finalize(a_row.id, sa.id)
    await broker_svc.finalize(b_row.id, sa.id)
    await broker_svc.mark_paid(b_row.id, sa.id)

    await _round_trip_loss(client_)  # more trading after the lock
    a_again, a_frozen = await admin_svc.compute_settlement(sa.id, now_utc())
    b_again, b_frozen = await broker_svc.compute_settlement(br.id, now_utc())

    assert (a_frozen, b_frozen) == (True, True)
    a_db = await AdminSettlement.get(a_row.id)
    b_db = await BrokerSettlement.get(b_row.id)
    assert a_db.status == AdminSettlementStatus.FINALIZED
    assert b_db.status == BrokerSettlementStatus.PAID
    assert _d(a_db.total_brokerage_inr) == _d(a_row.total_brokerage_inr) == Decimal("1000")
    assert _d(b_db.broker_share_inr) == _d(b_row.broker_share_inr)


async def test_compute_never_overwrites_a_finalize_that_lands_mid_compute(pool, monkeypatch):
    """A finalize between compute's read and its write must survive."""
    sa, br, client_, _demo = pool
    await _round_trip_loss(client_)
    a_row, _ = await admin_svc.compute_settlement(sa.id, now_utc())
    b_row, _ = await broker_svc.compute_settlement(br.id, now_utc())

    real_admin_pool = admin_svc._sub_admin_user_ids
    real_broker_pool = broker_svc._direct_client_ids

    async def admin_pool_then_finalize(sa_id):
        await admin_svc.finalize(a_row.id, sa.id)
        return await real_admin_pool(sa_id)

    async def broker_pool_then_finalize(br_id):
        await broker_svc.finalize(b_row.id, sa.id)
        return await real_broker_pool(br_id)

    monkeypatch.setattr(admin_svc, "_sub_admin_user_ids", admin_pool_then_finalize)
    monkeypatch.setattr(broker_svc, "_direct_client_ids", broker_pool_then_finalize)

    _, a_frozen = await admin_svc.compute_settlement(sa.id, now_utc())
    _, b_frozen = await broker_svc.compute_settlement(br.id, now_utc())

    assert (a_frozen, b_frozen) == (True, True)
    assert (await AdminSettlement.get(a_row.id)).status == AdminSettlementStatus.FINALIZED
    assert (await BrokerSettlement.get(b_row.id)).status == BrokerSettlementStatus.FINALIZED
