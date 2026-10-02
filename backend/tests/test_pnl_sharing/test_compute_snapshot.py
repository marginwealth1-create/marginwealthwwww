"""Tests for compute_sharing_snapshot — broker-level P&L share aggregation.

Sign convention (locked):
  net_client_pnl_inr is CLIENT view (positive = clients profited).
  total_of_both_inr / sharing_* are BROKER view (positive = broker won).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from bson import Decimal128

from app.models._base import Exchange, ProductType
from app.models.order import InstrumentRef
from app.models.position import Position, PositionStatus
from app.models.transaction import (
    TransactionStatus,
    TransactionType,
    WalletTransaction,
)
from app.services.pnl_sharing_service import compute_sharing_snapshot


def _instrument() -> InstrumentRef:
    """Minimal INR-native InstrumentRef so no USD→INR conversion runs."""
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


async def test_snapshot_clients_lost_broker_wins(db, agreement, client_user):
    """Clients lost ₹10,000 (realized_pnl=-10000), brokerage ₹500.

    Broker view: gains ₹10,000 + ₹500 = ₹10,500.
    Admin (30%): ₹3,000 PNL + ₹150 BKG = ₹3,150 total.
    """
    period_start = datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 5, 18, 23, 59, 59, tzinfo=UTC)

    await _make_position(
        user_id=client_user.id,
        realized_pnl="-10000",
        closed_at=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
    )
    await _make_brokerage_tx(
        user_id=client_user.id,
        amount="-500",  # debit (charged); abs() in service handles sign
        created_at=datetime(2026, 5, 18, 12, 0, 1, tzinfo=UTC),
    )

    snap = await compute_sharing_snapshot(agreement, period_start, period_end)

    assert snap.net_client_pnl_inr == Decimal("-10000.00")
    assert snap.net_client_bkg_inr == Decimal("500.00")
    assert snap.total_of_both_inr == Decimal("10500.00")  # -(-10000) + 500
    assert snap.actual_pnl_inr == Decimal("10500.00")
    assert snap.sharing_pnl_inr == Decimal("3000.00")
    assert snap.sharing_bkg_inr == Decimal("150.00")
    assert snap.sharing_total_inr == Decimal("3150.00")


async def test_snapshot_clients_won_broker_loses(db, agreement, client_user):
    """Clients gained ₹5,000, brokerage ₹200.

    Broker view: -₹5,000 PNL + ₹200 BKG = -₹4,800.
    Admin (30%): -₹1,500 PNL + ₹60 BKG = -₹1,440.
    """
    period_start = datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 5, 18, 23, 59, 59, tzinfo=UTC)

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

    snap = await compute_sharing_snapshot(agreement, period_start, period_end)

    assert snap.net_client_pnl_inr == Decimal("5000.00")
    assert snap.net_client_bkg_inr == Decimal("200.00")
    assert snap.total_of_both_inr == Decimal("-4800.00")
    assert snap.actual_pnl_inr == Decimal("-4800.00")
    assert snap.sharing_pnl_inr == Decimal("-1500.00")
    assert snap.sharing_bkg_inr == Decimal("60.00")
    assert snap.sharing_total_inr == Decimal("-1440.00")


async def test_snapshot_outside_window_excluded(db, agreement, client_user):
    """A position closed BEFORE the window must not count."""
    period_start = datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 5, 18, 23, 59, 59, tzinfo=UTC)

    # Day before the window — should be filtered out.
    await _make_position(
        user_id=client_user.id,
        realized_pnl="-10000",
        closed_at=datetime(2026, 5, 17, 23, 0, tzinfo=UTC),
    )
    # Also a brokerage row from outside the window — also filtered out.
    await _make_brokerage_tx(
        user_id=client_user.id,
        amount="-500",
        created_at=datetime(2026, 5, 17, 23, 0, 1, tzinfo=UTC),
    )

    snap = await compute_sharing_snapshot(agreement, period_start, period_end)

    assert snap.net_client_pnl_inr == Decimal("0.00")
    assert snap.net_client_bkg_inr == Decimal("0.00")
    assert snap.total_of_both_inr == Decimal("0.00")
    assert snap.actual_pnl_inr == Decimal("0.00")
    assert snap.sharing_pnl_inr == Decimal("0.00")
    assert snap.sharing_bkg_inr == Decimal("0.00")
    assert snap.sharing_total_inr == Decimal("0.00")


# ── Subtree test: sub-broker descendants should be INCLUDED in agreement ─────


async def test_brokerage_only_agreement_zeros_sharing_pnl(
    db, admin_user, broker_user, client_user
):
    """A BROKERAGE_ONLY agreement: clients lost ₹10000 + brokerage ₹500.
    Expected: sharing_pnl=0 (regardless of client loss), sharing_bkg=150 (30% of 500)."""
    from app.models.pnl_sharing import (
        AgreementStatus, AgreementType, PnlSharingAgreement,
        SettlementMode,
    )

    a = PnlSharingAgreement(
        admin_id=admin_user.id,
        broker_id=broker_user.id,
        share_pct=Decimal128("30"),
        settlement_mode=SettlementMode.MANUAL,
        settlement_cadence=None,
        status=AgreementStatus.ACTIVE,
        agreement_type=AgreementType.BROKERAGE_ONLY,
        effective_from=datetime(2026, 5, 1, tzinfo=UTC),
        created_by=admin_user.id,
        last_modified_by=admin_user.id,
    )
    await a.insert()

    period_start = datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 5, 18, 23, 59, 59, tzinfo=UTC)

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

    snap = await compute_sharing_snapshot(a, period_start, period_end)
    assert snap.net_client_pnl_inr == Decimal("-10000.00")  # display value still set
    assert snap.net_client_bkg_inr == Decimal("500.00")
    assert snap.sharing_pnl_inr == Decimal("0")  # KEY: zero, not 3000
    assert snap.sharing_bkg_inr == Decimal("150.00")
    assert snap.sharing_total_inr == Decimal("150.00")


async def test_snapshot_includes_subbroker_clients_in_subtree(
    db, admin_user, broker_user, agreement
):
    """Agreement is admin↔parent_broker. If broker has a sub-broker, and
    that sub-broker has clients, those clients trades should ALSO count
    toward this agreement (subtree-inclusive sharing).

    Setup:
      parent_broker  ← agreement here
      └─ sub_broker
          └─ sub_client

    sub_client trades and loses 10000. Brokerage 500. 30% share.
    Expected: admin gets 3000 PNL + 150 BKG = 3150 total.
    """
    from beanie import PydanticObjectId
    from app.models.user import User, UserRole, UserStatus

    sub_broker = User(
        user_code="TBRK_SUB",
        email="subbroker@example.com",
        mobile="9999900099",
        full_name="Sub Broker",
        password_hash="x",
        role=UserRole.BROKER,
        status=UserStatus.ACTIVE,
        assigned_admin_id=admin_user.id,
        assigned_broker_id=broker_user.id,
        broker_ancestry=[broker_user.id],
    )
    await sub_broker.insert()

    sub_client = User(
        user_code="TCLI_SUB",
        email="subclient@example.com",
        mobile="9999900100",
        full_name="Sub Client",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
        assigned_admin_id=admin_user.id,
        assigned_broker_id=sub_broker.id,
        broker_ancestry=[broker_user.id, sub_broker.id],
    )
    await sub_client.insert()

    period_start = datetime(2026, 5, 18, 0, 0, tzinfo=UTC)
    period_end = datetime(2026, 5, 18, 23, 59, 59, tzinfo=UTC)

    await _make_position(
        user_id=sub_client.id,
        realized_pnl="-10000",
        closed_at=datetime(2026, 5, 18, 12, 0, tzinfo=UTC),
    )
    await _make_brokerage_tx(
        user_id=sub_client.id,
        amount="-500",
        created_at=datetime(2026, 5, 18, 12, 0, 1, tzinfo=UTC),
    )

    snap = await compute_sharing_snapshot(agreement, period_start, period_end)

    assert snap.net_client_pnl_inr == Decimal("-10000.00")
    assert snap.net_client_bkg_inr == Decimal("500.00")
    assert snap.total_of_both_inr == Decimal("10500.00")
    assert snap.sharing_pnl_inr == Decimal("3000.00")
    assert snap.sharing_bkg_inr == Decimal("150.00")
    assert snap.sharing_total_inr == Decimal("3150.00")
