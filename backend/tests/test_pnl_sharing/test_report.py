"""Tests for build_report — multi-period aggregation of sharing snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from bson import Decimal128

from app.models._base import Exchange, ProductType
from app.models.order import InstrumentRef
from app.models.pnl_sharing import SettlementCadence
from app.models.position import Position, PositionStatus
from app.models.transaction import (
    TransactionStatus,
    TransactionType,
    WalletTransaction,
)
from app.services import pnl_sharing_service as svc


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


async def test_report_daily_aggregates_per_day(db, agreement, broker_user, client_user):
    """Two closed positions on different days → two rows in DAILY report.

    Day 1 (2026-05-18 IST): clients lost 1000 + brokerage 50 → broker gains 1050
      → admin 30%: 300 PNL + 15 BKG = 315.
    Day 2 (2026-05-19 IST): clients lost 2000 + brokerage 100 → broker gains 2100
      → admin 30%: 600 PNL + 30 BKG = 630.
    """
    # Use mid-day IST times so they fall cleanly within their UTC day too.
    # 2026-05-18 06:30 UTC = 2026-05-18 12:00 IST → Day 1
    await _make_position(
        user_id=client_user.id,
        realized_pnl="-1000",
        closed_at=datetime(2026, 5, 18, 6, 30, tzinfo=UTC),
    )
    await _make_brokerage_tx(
        user_id=client_user.id,
        amount="-50",
        created_at=datetime(2026, 5, 18, 6, 30, 1, tzinfo=UTC),
    )
    # 2026-05-19 06:30 UTC = 2026-05-19 12:00 IST → Day 2
    await _make_position(
        user_id=client_user.id,
        realized_pnl="-2000",
        closed_at=datetime(2026, 5, 19, 6, 30, tzinfo=UTC),
    )
    await _make_brokerage_tx(
        user_id=client_user.id,
        amount="-100",
        created_at=datetime(2026, 5, 19, 6, 30, 1, tzinfo=UTC),
    )

    # IST-aligned bounds: from = 2026-05-18 00:00 IST (2026-05-17 18:30 UTC)
    # to = within 2026-05-19 IST day (2026-05-19 12:00 UTC = 2026-05-19 17:30 IST)
    report = await svc.build_report(
        agreement=agreement,
        cadence=SettlementCadence.DAILY,
        from_dt=datetime(2026, 5, 17, 18, 30, tzinfo=UTC),
        to_dt=datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
    )

    assert len(report.rows) == 2

    # Day 1: 30% share
    assert report.rows[0].sharing_pnl_inr == "300.00"
    assert report.rows[0].sharing_bkg_inr == "15.00"
    assert report.rows[0].settlement_status == "UNSETTLED"

    # Day 2: 30% share
    assert report.rows[1].sharing_pnl_inr == "600.00"
    assert report.rows[1].sharing_bkg_inr == "30.00"
    assert report.rows[1].settlement_status == "UNSETTLED"

    # Summary aggregates
    assert report.summary.total_sharing_pnl_inr == "900.00"
    assert report.summary.total_sharing_bkg_inr == "45.00"
    assert report.summary.periods_settled == 0
    assert report.summary.periods_pending == 0
    assert report.summary.periods_failed == 0
    assert report.summary.periods_unsettled == 2


async def test_report_summary_aggregates(db, agreement, broker_user, client_user):
    """Summary totals match sum of row values for a one-month MONTHLY report."""
    await _make_position(
        user_id=client_user.id,
        realized_pnl="-1000",
        closed_at=datetime(2026, 5, 18, 6, 30, tzinfo=UTC),
    )
    await _make_brokerage_tx(
        user_id=client_user.id,
        amount="-50",
        created_at=datetime(2026, 5, 18, 6, 30, 1, tzinfo=UTC),
    )

    # IST-aligned bounds: from = 2026-05-01 00:00 IST (2026-04-30 18:30 UTC)
    # to = within May 2026 IST month (2026-05-15 12:00 UTC = 2026-05-15 17:30 IST)
    report = await svc.build_report(
        agreement=agreement,
        cadence=SettlementCadence.MONTHLY,
        from_dt=datetime(2026, 4, 30, 18, 30, tzinfo=UTC),
        to_dt=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
    )

    # 1 month, 1 position: 30% of 1000 PNL + 30% of 50 BKG
    assert len(report.rows) == 1
    assert report.summary.total_sharing_pnl_inr == "300.00"
    assert report.summary.total_sharing_bkg_inr == "15.00"
    assert report.summary.periods_unsettled == 1
    assert report.summary.periods_settled == 0
    # Row totals also match
    assert report.rows[0].sharing_pnl_inr == "300.00"
    assert report.rows[0].sharing_bkg_inr == "15.00"
    # Sanity check on values used to derive sharing
    assert Decimal(report.rows[0].net_client_pnl_inr) == Decimal("-1000.00")
    assert Decimal(report.rows[0].net_client_bkg_inr) == Decimal("50.00")
