"""Tests for settle_period — wallet integration + idempotency.

Direction convention (locked):
  sharing_total_inr > 0 → broker pays admin (broker debit, admin credit)
  sharing_total_inr < 0 → admin pays broker (admin debit, broker credit)
  sharing_total_inr == 0 → no wallet calls
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest_asyncio
from bson import Decimal128

from app.models._base import Exchange, ProductType
from app.models.order import InstrumentRef
from app.models.pnl_sharing import (
    SettlementCadence,
    SharingSettlementStatus,
)
from app.models.position import Position, PositionStatus
from app.models.transaction import (
    TransactionStatus,
    TransactionType,
    WalletTransaction,
)
from app.models.wallet import Wallet
from app.services import pnl_sharing_service as svc


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def admin_wallet(db, admin_user) -> Wallet:
    w = Wallet(user_id=admin_user.id, available_balance=Decimal128("100000"))
    await w.insert()
    return w


@pytest_asyncio.fixture
async def broker_wallet(db, broker_user) -> Wallet:
    w = Wallet(user_id=broker_user.id, available_balance=Decimal128("100000"))
    await w.insert()
    return w


# ── Helpers (mirrors test_compute_snapshot.py) ───────────────────────


def _instrument() -> InstrumentRef:
    return InstrumentRef(
        token="3045",
        symbol="SBIN",
        trading_symbol="SBIN-EQ",
        exchange=Exchange.NSE,
        segment="NSE_EQUITY",
        lot_size=1,
        tick_size=Decimal128("0.05"),
    )


async def _make_position(
    *,
    user_id,
    realized_pnl: str,
    closed_at: datetime,
) -> Position:
    pos = Position(
        user_id=user_id,
        instrument=_instrument(),
        segment_type="NSE_EQUITY",
        product_type=ProductType.MIS,
        quantity=0.0,
        opening_quantity=1.0,
        realized_pnl=Decimal128(realized_pnl),
        status=PositionStatus.CLOSED,
        opened_at=closed_at,
        closed_at=closed_at,
    )
    await pos.insert()
    return pos


async def _make_brokerage_tx(
    *,
    user_id,
    amount: str,
    created_at: datetime,
) -> WalletTransaction:
    tx = WalletTransaction(
        user_id=user_id,
        transaction_type=TransactionType.BROKERAGE,
        amount=Decimal128(amount),
        narration="test brokerage",
        status=TransactionStatus.COMPLETED,
        created_at=created_at,
        updated_at=created_at,
    )
    await tx.insert()
    return tx


# ── Tests ────────────────────────────────────────────────────────────


async def test_settle_period_credits_admin_debits_broker(
    db,
    admin_user,
    broker_user,
    client_user,
    agreement,
    admin_wallet,
    broker_wallet,
):
    """Client lost 10000 + brokerage 500 → broker gain 10500 → admin 30% = 3150.
    Broker wallet: 100000 → 96850; admin: 100000 → 103150.
    """
    period_start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)

    await _make_position(
        user_id=client_user.id,
        realized_pnl="-10000",
        closed_at=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
    )
    await _make_brokerage_tx(
        user_id=client_user.id,
        amount="-500",
        created_at=datetime(2026, 5, 18, 12, 0, 1, tzinfo=UTC),
    )

    settlement = await svc.settle_period(
        agreement_id=agreement.id,
        period_start=period_start,
        period_end=period_end,
        cadence=SettlementCadence.MONTHLY,
        triggered_by="MANUAL",
        actor=admin_user,
    )

    assert settlement.status == SharingSettlementStatus.SETTLED
    assert Decimal(str(settlement.sharing_total_inr)) == Decimal("3150.00")
    assert settlement.transaction_ref_admin is not None
    assert settlement.transaction_ref_broker is not None
    assert settlement.settled_at is not None
    assert settlement.settled_by == admin_user.id

    aw = await Wallet.find_one(Wallet.user_id == admin_user.id)
    bw = await Wallet.find_one(Wallet.user_id == broker_user.id)
    assert Decimal(str(aw.available_balance)) == Decimal("103150.00")
    assert Decimal(str(bw.available_balance)) == Decimal("96850.00")


async def test_settle_period_idempotent(
    db,
    admin_user,
    broker_user,
    client_user,
    agreement,
    admin_wallet,
    broker_wallet,
):
    """Calling settle_period twice for the same period must NOT double-pay."""
    period_start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)

    await _make_position(
        user_id=client_user.id,
        realized_pnl="-10000",
        closed_at=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
    )
    await _make_brokerage_tx(
        user_id=client_user.id,
        amount="-500",
        created_at=datetime(2026, 5, 18, 12, 0, 1, tzinfo=UTC),
    )

    first = await svc.settle_period(
        agreement_id=agreement.id,
        period_start=period_start,
        period_end=period_end,
        cadence=SettlementCadence.MONTHLY,
        triggered_by="MANUAL",
        actor=admin_user,
    )
    second = await svc.settle_period(
        agreement_id=agreement.id,
        period_start=period_start,
        period_end=period_end,
        cadence=SettlementCadence.MONTHLY,
        triggered_by="MANUAL",
        actor=admin_user,
    )
    assert first.id == second.id
    assert second.status == SharingSettlementStatus.SETTLED

    aw = await Wallet.find_one(Wallet.user_id == admin_user.id)
    bw = await Wallet.find_one(Wallet.user_id == broker_user.id)
    # Moved only once
    assert Decimal(str(aw.available_balance)) == Decimal("103150.00")
    assert Decimal(str(bw.available_balance)) == Decimal("96850.00")


async def test_settle_period_fails_on_insufficient_balance(
    db,
    admin_user,
    broker_user,
    client_user,
    agreement,
):
    """No wallets pre-created → broker debit will fail (zero balance, no credit_limit).
    Settlement row should be FAILED with failure_reason set.
    """
    period_start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)

    # Client lost — broker owes admin; broker has no wallet (auto-created zero balance).
    await _make_position(
        user_id=client_user.id,
        realized_pnl="-10000",
        closed_at=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
    )
    await _make_brokerage_tx(
        user_id=client_user.id,
        amount="-500",
        created_at=datetime(2026, 5, 18, 12, 0, 1, tzinfo=UTC),
    )

    settlement = await svc.settle_period(
        agreement_id=agreement.id,
        period_start=period_start,
        period_end=period_end,
        cadence=SettlementCadence.MONTHLY,
        triggered_by="MANUAL",
        actor=admin_user,
    )

    assert settlement.status == SharingSettlementStatus.FAILED
    assert settlement.failure_reason is not None
    assert settlement.settled_at is None


async def test_settle_period_debits_admin_credits_broker_when_client_wins(
    db,
    admin_user,
    broker_user,
    client_user,
    agreement,
    admin_wallet,
    broker_wallet,
):
    """Client gained 5000, brokerage 200.
    Broker net = -5000 + 200 = -4800.
    Admin share 30% = -1500 (PNL) + 60 (BKG) = -1440.
    Admin pays broker 1440. Broker wallet 100000 -> 101440, admin 100000 -> 98560.
    """
    period_start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)

    # Client WON (positive realized_pnl) → broker loses → admin shares the loss
    await _make_position(
        user_id=client_user.id,
        realized_pnl="5000",
        closed_at=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
    )
    await _make_brokerage_tx(
        user_id=client_user.id,
        amount="-200",
        created_at=datetime(2026, 5, 18, 12, 0, 1, tzinfo=UTC),
    )

    settlement = await svc.settle_period(
        agreement_id=agreement.id,
        period_start=period_start,
        period_end=period_end,
        cadence=SettlementCadence.MONTHLY,
        triggered_by="MANUAL",
        actor=admin_user,
    )

    assert settlement.status == SharingSettlementStatus.SETTLED
    assert Decimal(str(settlement.sharing_total_inr)) == Decimal("-1440.00")
    assert settlement.transaction_ref_admin is not None
    assert settlement.transaction_ref_broker is not None
    assert settlement.settled_at is not None
    assert settlement.settled_by == admin_user.id

    aw = await Wallet.find_one(Wallet.user_id == admin_user.id)
    bw = await Wallet.find_one(Wallet.user_id == broker_user.id)
    assert Decimal(str(aw.available_balance)) == Decimal("98560.00")
    assert Decimal(str(bw.available_balance)) == Decimal("101440.00")


async def test_settle_period_zero_amount_no_wallet_calls(
    db,
    admin_user,
    broker_user,
    agreement,
    admin_wallet,
    broker_wallet,
):
    """No trades in window → sharing_total=0 → wallets unchanged, status=SETTLED."""
    period_start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)

    settlement = await svc.settle_period(
        agreement_id=agreement.id,
        period_start=period_start,
        period_end=period_end,
        cadence=SettlementCadence.MONTHLY,
        triggered_by="MANUAL",
        actor=admin_user,
    )

    assert settlement.status == SharingSettlementStatus.SETTLED
    assert Decimal(str(settlement.sharing_total_inr)) == Decimal("0.00")
    assert settlement.transaction_ref_admin is None
    assert settlement.transaction_ref_broker is None

    aw = await Wallet.find_one(Wallet.user_id == admin_user.id)
    bw = await Wallet.find_one(Wallet.user_id == broker_user.id)
    assert Decimal(str(aw.available_balance)) == Decimal("100000")
    assert Decimal(str(bw.available_balance)) == Decimal("100000")
