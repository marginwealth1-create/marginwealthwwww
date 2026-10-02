from decimal import Decimal

import pytest
from beanie import PydanticObjectId
from bson import Decimal128

from app.models.pnl_sharing import (
    AgreementStatus, PnlSharingAgreement, SettlementCadence, SettlementMode,
)
from app.services import pnl_sharing_service as svc


@pytest.mark.asyncio
async def test_create_agreement_happy(db, admin_user, broker_user):
    a = await svc.create_agreement(
        actor=admin_user, admin_id=admin_user.id, broker_id=broker_user.id,
        share_pct=Decimal("30"), settlement_mode=SettlementMode.MANUAL,
        settlement_cadence=None,
    )
    assert a.status == AgreementStatus.ACTIVE
    assert str(a.share_pct) == "30"


@pytest.mark.asyncio
async def test_create_rejects_duplicate_active(db, admin_user, broker_user, agreement):
    with pytest.raises(svc.AgreementConflict):
        await svc.create_agreement(
            actor=admin_user, admin_id=admin_user.id, broker_id=broker_user.id,
            share_pct=Decimal("25"), settlement_mode=SettlementMode.MANUAL,
            settlement_cadence=None,
        )


@pytest.mark.asyncio
async def test_create_rejects_broker_not_under_admin(db, admin_user, broker_user):
    with pytest.raises(svc.AgreementValidationError):
        await svc.create_agreement(
            actor=admin_user, admin_id=PydanticObjectId(), broker_id=broker_user.id,
            share_pct=Decimal("25"), settlement_mode=SettlementMode.MANUAL,
            settlement_cadence=None,
        )


@pytest.mark.asyncio
async def test_update_share_pct(db, admin_user, agreement):
    updated = await svc.update_agreement(
        actor=admin_user, agreement_id=agreement.id, share_pct=Decimal("40"),
    )
    assert str(updated.share_pct) == "40"


@pytest.mark.asyncio
async def test_pause_resume_end(db, admin_user, agreement):
    paused = await svc.pause_agreement(actor=admin_user, agreement_id=agreement.id)
    assert paused.status == AgreementStatus.PAUSED
    resumed = await svc.resume_agreement(actor=admin_user, agreement_id=agreement.id)
    assert resumed.status == AgreementStatus.ACTIVE
    ended = await svc.end_agreement(actor=admin_user, agreement_id=agreement.id)
    assert ended.status == AgreementStatus.ENDED
    assert ended.effective_until is not None


@pytest.mark.asyncio
async def test_auto_mode_requires_cadence(db, admin_user, broker_user):
    with pytest.raises(svc.AgreementValidationError):
        await svc.create_agreement(
            actor=admin_user, admin_id=admin_user.id, broker_id=broker_user.id,
            share_pct=Decimal("30"), settlement_mode=SettlementMode.AUTO,
            settlement_cadence=None,
        )


@pytest.mark.asyncio
async def test_share_pct_out_of_range(db, admin_user, broker_user):
    with pytest.raises(svc.AgreementValidationError):
        await svc.create_agreement(
            actor=admin_user, admin_id=admin_user.id, broker_id=broker_user.id,
            share_pct=Decimal("150"), settlement_mode=SettlementMode.MANUAL,
            settlement_cadence=None,
        )


@pytest.mark.asyncio
async def test_create_brokerage_only_agreement(db, admin_user, broker_user):
    from app.models.pnl_sharing import AgreementType, SettlementMode
    a = await svc.create_agreement(
        actor=admin_user, admin_id=admin_user.id, broker_id=broker_user.id,
        share_pct=Decimal("25"), settlement_mode=SettlementMode.MANUAL,
        settlement_cadence=None, agreement_type=AgreementType.BROKERAGE_ONLY,
    )
    assert a.agreement_type == AgreementType.BROKERAGE_ONLY


@pytest.mark.asyncio
async def test_same_pair_can_have_both_agreement_types(db, admin_user, broker_user):
    """The same admin↔broker pair should be able to hold both a
    PNL_AND_BROKERAGE and a BROKERAGE_ONLY agreement simultaneously."""
    from app.models.pnl_sharing import AgreementType, SettlementMode
    a1 = await svc.create_agreement(
        actor=admin_user, admin_id=admin_user.id, broker_id=broker_user.id,
        share_pct=Decimal("30"), settlement_mode=SettlementMode.MANUAL,
        settlement_cadence=None,
        agreement_type=AgreementType.PNL_AND_BROKERAGE,
    )
    a2 = await svc.create_agreement(
        actor=admin_user, admin_id=admin_user.id, broker_id=broker_user.id,
        share_pct=Decimal("10"), settlement_mode=SettlementMode.MANUAL,
        settlement_cadence=None,
        agreement_type=AgreementType.BROKERAGE_ONLY,
    )
    assert a1.id != a2.id
    assert a1.agreement_type != a2.agreement_type
