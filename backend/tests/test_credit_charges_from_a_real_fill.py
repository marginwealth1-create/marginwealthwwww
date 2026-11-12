"""§3: a MARKET order through the engine, a real netting commission, and NO
seeded ledger row. Whatever the fill path wrote, the P&L-sharing snapshot, the
sharing payout and the sub-admin settlement must all see the charge.
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
from app.models.pnl_sharing import (
    AgreementType,
    PnlSharingAgreement,
    SettlementCadence,
    SettlementMode,
    SharingSettlementStatus,
)
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import admin_settlement_service, pnl_sharing_service
from app.utils.time_utils import now_utc
from tests._real_fill import market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_credit_charges_real_fill"


@pytest_asyncio.fixture
async def world():
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)
    await set_commission_per_lot(20)  # 25 qty → ₹500

    n = 0

    async def mk(code: str, role: UserRole, balance: str, **kw) -> User:
        nonlocal n
        n += 1
        u = User(
            user_code=code,
            email=f"{code.lower()}@example.com",
            mobile=f"95000{n:05d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            **kw,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128(balance)).insert()
        return u

    sa = await mk("CRFADM", UserRole.ADMIN, "100000")
    br = await mk("CRFBRK", UserRole.BROKER, "100000", assigned_admin_id=sa.id)
    agreement = PnlSharingAgreement(
        admin_id=sa.id,
        broker_id=br.id,
        share_pct=Decimal128("30"),
        settlement_mode=SettlementMode.MANUAL,
        agreement_type=AgreementType.BROKERAGE_ONLY,
        effective_from=now_utc() - timedelta(days=30),
        created_by=sa.id,
        last_modified_by=sa.id,
    )
    await agreement.insert()

    async def client_with(balance: str) -> User:
        return await mk(
            "CRFCLI", UserRole.CLIENT, balance,
            assigned_admin_id=sa.id, assigned_broker_id=br.id,
            broker_ancestry=[br.id], auto_settlement=True,
        )

    try:
        yield sa, br, agreement, client_with
    finally:
        await client.drop_database(_DB)
        client.close()


def _window():
    return pnl_sharing_service.compute_period_bounds(SettlementCadence.DAILY, now_utc())


async def test_open_fill_charge_is_shared_and_settled(world):
    sa, br, agreement, client_with = world
    client_ = await client_with("100000")

    trade = await market(client_, "BUY", 25, "100")

    # What the fill path really writes: Trade.brokerage and a CHARGES debit.
    assert Decimal(str(trade.brokerage)) == Decimal("500.00")
    types = {t.transaction_type for t in await WalletTransaction.find(
        {"user_id": client_.id}).to_list()}
    assert TransactionType.CHARGES in types and TransactionType.BROKERAGE not in types

    start, end = _window()
    snap = await pnl_sharing_service.compute_sharing_snapshot(agreement, start, end)
    assert snap.net_client_bkg_inr == Decimal("500.00")
    assert snap.sharing_total_inr == Decimal("150.00")  # BROKERAGE_ONLY, 30 %

    row = await pnl_sharing_service.settle_period(
        agreement_id=agreement.id, period_start=start, period_end=end,
        cadence=SettlementCadence.DAILY, triggered_by="MANUAL", actor=sa,
    )
    assert row.status == SharingSettlementStatus.SETTLED
    assert Decimal(str(row.sharing_total_inr)) == Decimal("150.00")
    bw = await Wallet.find_one(Wallet.user_id == br.id)
    aw = await Wallet.find_one(Wallet.user_id == sa.id)
    assert Decimal(str(bw.available_balance)) == Decimal("99850.00")
    assert Decimal(str(aw.available_balance)) == Decimal("100150.00")

    s_row, _ = await admin_settlement_service.compute_settlement(sa.id, now_utc())
    assert Decimal(str(s_row.total_brokerage_inr)) == Decimal("500.00")


async def test_charge_absorbed_into_dues_still_counts(world):
    """An empty wallet books the charge to dues: no CHARGES row is written at
    all, yet the client was still charged ₹500."""
    sa, _br, agreement, client_with = world
    client_ = await client_with("0")

    await market(client_, "BUY", 25, "100")

    assert await WalletTransaction.find_one(
        {"user_id": client_.id, "transaction_type": TransactionType.CHARGES.value}
    ) is None
    start, end = _window()
    snap = await pnl_sharing_service.compute_sharing_snapshot(agreement, start, end)
    assert snap.net_client_bkg_inr == Decimal("500.00")
    s_row, _ = await admin_settlement_service.compute_settlement(sa.id, now_utc())
    assert Decimal(str(s_row.total_brokerage_inr)) == Decimal("500.00")
