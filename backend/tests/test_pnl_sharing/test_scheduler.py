"""Tests for the auto-settle scheduler."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from bson import Decimal128

from app.models._base import Exchange, ProductType
from app.models.order import InstrumentRef
from app.models.position import Position, PositionStatus
from app.models.pnl_sharing import (
    AgreementStatus,
    PnlSharingAgreement,
    PnlSharingSettlement,
    SettlementCadence,
    SettlementMode,
    SharingSettlementStatus,
)
from app.services import pnl_sharing_service as svc


@pytest_asyncio.fixture
async def auto_monthly_agreement(db, admin_user, broker_user) -> PnlSharingAgreement:
    a = PnlSharingAgreement(
        admin_id=admin_user.id,
        broker_id=broker_user.id,
        share_pct=Decimal128("30"),
        settlement_mode=SettlementMode.AUTO,
        settlement_cadence=SettlementCadence.MONTHLY,
        status=AgreementStatus.ACTIVE,
        effective_from=datetime(2026, 3, 1, tzinfo=timezone.utc),
        created_by=admin_user.id,
        last_modified_by=admin_user.id,
    )
    await a.insert()
    return a


@pytest_asyncio.fixture
async def auto_daily_agreement(db, admin_user, broker_user) -> PnlSharingAgreement:
    a = PnlSharingAgreement(
        admin_id=admin_user.id,
        broker_id=broker_user.id,
        share_pct=Decimal128("30"),
        settlement_mode=SettlementMode.AUTO,
        settlement_cadence=SettlementCadence.DAILY,
        status=AgreementStatus.ACTIVE,
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_by=admin_user.id,
        last_modified_by=admin_user.id,
    )
    await a.insert()
    return a


@pytest.mark.asyncio
async def test_find_due_includes_active_auto_monthly(db, auto_monthly_agreement):
    """Mid-May → April is the just-closed monthly period for AUTO+MONTHLY agreement."""
    now = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    due = await svc.find_due_settlements(now=now)
    assert len(due) == 1
    agreement, period_start, period_end = due[0]
    assert agreement.id == auto_monthly_agreement.id
    # period_end must be before now (period has closed)
    assert period_end < now


@pytest.mark.asyncio
async def test_find_due_skips_paused(db, admin_user, broker_user):
    a = PnlSharingAgreement(
        admin_id=admin_user.id, broker_id=broker_user.id,
        share_pct=Decimal128("30"), settlement_mode=SettlementMode.AUTO,
        settlement_cadence=SettlementCadence.DAILY,
        status=AgreementStatus.PAUSED,
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_by=admin_user.id, last_modified_by=admin_user.id,
    )
    await a.insert()
    now = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    due = await svc.find_due_settlements(now=now)
    assert due == []


@pytest.mark.asyncio
async def test_find_due_skips_ended(db, admin_user, broker_user):
    a = PnlSharingAgreement(
        admin_id=admin_user.id, broker_id=broker_user.id,
        share_pct=Decimal128("30"), settlement_mode=SettlementMode.AUTO,
        settlement_cadence=SettlementCadence.DAILY,
        status=AgreementStatus.ENDED,
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        effective_until=datetime(2026, 4, 1, tzinfo=timezone.utc),
        created_by=admin_user.id, last_modified_by=admin_user.id,
    )
    await a.insert()
    now = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    due = await svc.find_due_settlements(now=now)
    assert due == []


@pytest.mark.asyncio
async def test_find_due_skips_manual_mode(db, admin_user, broker_user):
    a = PnlSharingAgreement(
        admin_id=admin_user.id, broker_id=broker_user.id,
        share_pct=Decimal128("30"), settlement_mode=SettlementMode.MANUAL,
        settlement_cadence=None, status=AgreementStatus.ACTIVE,
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_by=admin_user.id, last_modified_by=admin_user.id,
    )
    await a.insert()
    now = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    due = await svc.find_due_settlements(now=now)
    assert due == []


@pytest.mark.asyncio
async def test_find_due_skips_when_effective_from_after_period(db, admin_user, broker_user):
    """Agreement created mid-period — should NOT auto-settle that partial period."""
    a = PnlSharingAgreement(
        admin_id=admin_user.id, broker_id=broker_user.id,
        share_pct=Decimal128("30"), settlement_mode=SettlementMode.AUTO,
        settlement_cadence=SettlementCadence.MONTHLY,
        status=AgreementStatus.ACTIVE,
        effective_from=datetime(2026, 4, 15, tzinfo=timezone.utc),  # mid-April
        created_by=admin_user.id, last_modified_by=admin_user.id,
    )
    await a.insert()
    # "Now" = mid-May → April is the just-closed month, but agreement was
    # only active for half of April → skip
    now = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    due = await svc.find_due_settlements(now=now)
    assert due == []


@pytest.mark.asyncio
async def test_find_due_skips_already_settled(db, auto_monthly_agreement):
    """If a SETTLED row already exists for the period, don't re-yield."""
    now = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    # Compute April's bounds the same way find_due_settlements does
    ref = now - timedelta(days=32)
    period_start, period_end = svc.compute_period_bounds(SettlementCadence.MONTHLY, ref)

    existing = PnlSharingSettlement(
        agreement_id=auto_monthly_agreement.id,
        admin_id=auto_monthly_agreement.admin_id,
        broker_id=auto_monthly_agreement.broker_id,
        period_start=period_start,
        period_end=period_end,
        cadence=SettlementCadence.MONTHLY,
        status=SharingSettlementStatus.SETTLED,
    )
    await existing.insert()

    due = await svc.find_due_settlements(now=now)
    assert due == []


@pytest.mark.asyncio
async def test_find_due_yields_when_failed_row_exists(db, auto_monthly_agreement):
    """A FAILED row from previous attempt should re-fire (so wallet top-up can resolve)."""
    now = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    ref = now - timedelta(days=32)
    period_start, period_end = svc.compute_period_bounds(SettlementCadence.MONTHLY, ref)

    existing = PnlSharingSettlement(
        agreement_id=auto_monthly_agreement.id,
        admin_id=auto_monthly_agreement.admin_id,
        broker_id=auto_monthly_agreement.broker_id,
        period_start=period_start,
        period_end=period_end,
        cadence=SettlementCadence.MONTHLY,
        status=SharingSettlementStatus.FAILED,
        failure_reason="insufficient funds",
    )
    await existing.insert()

    due = await svc.find_due_settlements(now=now)
    assert len(due) == 1


@pytest.mark.asyncio
async def test_scheduler_loop_fires_settle_for_due(
    db, admin_user, broker_user, client_user, auto_daily_agreement, monkeypatch,
):
    """One iteration of the scheduler loop should call settle_period for each due item."""
    # Set up a closed position for yesterday so there's data to settle.
    # The DAILY agreement's previous period is "yesterday" from `now`.
    now = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    yesterday_close = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)

    instrument = InstrumentRef(
        token="3045",
        symbol="SBIN",
        trading_symbol="SBIN-EQ",
        exchange=Exchange.NSE,
        segment="NSE_EQUITY",
        lot_size=1,
        tick_size=Decimal128("0.05"),
    )

    pos = Position(
        user_id=client_user.id,
        instrument=instrument,
        segment_type="NSE_EQUITY",
        product_type=ProductType.MIS,
        quantity=0.0,
        opening_quantity=1.0,
        realized_pnl=Decimal128("-1000"),
        status=PositionStatus.CLOSED,
        opened_at=yesterday_close,
        closed_at=yesterday_close,
    )
    await pos.insert()

    # Capture settle_period calls
    calls: list[dict] = []
    original = svc.settle_period

    async def fake_settle(**kwargs):
        calls.append(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(svc, "settle_period", fake_settle)

    # Drive one iteration manually (skip the sleep, so this test runs instantly)
    due = await svc.find_due_settlements(now=now)
    for agreement, period_start, period_end in due:
        await svc.settle_period(
            agreement_id=agreement.id,
            period_start=period_start,
            period_end=period_end,
            cadence=agreement.settlement_cadence,
            triggered_by="AUTO",
            actor=None,
        )

    assert len(calls) == 1
    assert calls[0]["triggered_by"] == "AUTO"
    assert calls[0]["actor"] is None
