"""P&L Sharing API — agreements (Phase A).

Settlements + reports endpoints will be added in subsequent tasks.

All endpoints under /api/v1/admin/pnl-sharing/*. Role scoping enforced inside
each handler:
  - SUPER_ADMIN: god mode (sees and edits all)
  - ADMIN: own agreements only
  - BROKER: own agreement only, read-only
"""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.core.dependencies import CurrentAdmin
from app.models.pnl_sharing import (
    AgreementStatus,
    AgreementType,
    PnlSharingAgreement,
    PnlSharingSettlement,
    SettlementCadence,
    SharingSettlementStatus,
)
from app.models.user import User, UserRole
from app.schemas.common import APIResponse
from app.schemas.pnl_sharing import (
    AgreementDTO,
    CreateAgreementRequest,
    ManualSettleRequest,
    ReportResponse,
    SettlementDTO,
    UpdateAgreementRequest,
)
from app.services import pnl_sharing_service as svc
from app.utils.time_utils import now_utc

router = APIRouter(prefix="/pnl-sharing", tags=["pnl-sharing"])


def _can_edit(actor: User, agreement: PnlSharingAgreement) -> bool:
    if actor.role == UserRole.SUPER_ADMIN:
        return True
    if actor.role == UserRole.ADMIN and agreement.admin_id == actor.id:
        return True
    return False


def _can_view(actor: User, agreement: PnlSharingAgreement) -> bool:
    if actor.role == UserRole.SUPER_ADMIN:
        return True
    if actor.role == UserRole.ADMIN and agreement.admin_id == actor.id:
        return True
    if actor.role == UserRole.BROKER and agreement.broker_id == actor.id:
        return True
    return False


async def _serialize_agreement(a: PnlSharingAgreement) -> AgreementDTO:
    admin = await User.get(a.admin_id)
    broker = await User.get(a.broker_id)
    return AgreementDTO(
        id=str(a.id),
        admin_id=str(a.admin_id),
        admin_name=admin.full_name if admin else None,
        admin_user_code=admin.user_code if admin else None,
        broker_id=str(a.broker_id),
        broker_name=broker.full_name if broker else None,
        broker_user_code=broker.user_code if broker else None,
        share_pct=str(a.share_pct),
        settlement_mode=a.settlement_mode,
        settlement_cadence=a.settlement_cadence,
        agreement_type=a.agreement_type,
        status=a.status,
        effective_from=a.effective_from,
        effective_until=a.effective_until,
        created_at=a.created_at,
        updated_at=a.updated_at,
    )


async def _serialize_settlement(s: PnlSharingSettlement) -> SettlementDTO:
    return SettlementDTO(
        id=str(s.id),
        agreement_id=str(s.agreement_id),
        admin_id=str(s.admin_id),
        broker_id=str(s.broker_id),
        period_start=s.period_start,
        period_end=s.period_end,
        cadence=s.cadence,
        net_client_pnl_inr=str(s.net_client_pnl_inr),
        net_client_bkg_inr=str(s.net_client_bkg_inr),
        total_of_both_inr=str(s.total_of_both_inr),
        actual_pnl_inr=str(s.actual_pnl_inr),
        sharing_pnl_inr=str(s.sharing_pnl_inr),
        sharing_bkg_inr=str(s.sharing_bkg_inr),
        sharing_total_inr=str(s.sharing_total_inr),
        share_pct_snapshot=str(s.share_pct_snapshot),
        status=s.status,
        settled_at=s.settled_at,
        failure_reason=s.failure_reason,
        retry_count=s.retry_count,
    )


@router.get("/agreements", response_model=APIResponse[list[AgreementDTO]])
async def list_agreements(
    actor: CurrentAdmin,
    status_filter: AgreementStatus | None = Query(default=None, alias="status"),
    admin_id: PydanticObjectId | None = Query(default=None),
    broker_id: PydanticObjectId | None = Query(default=None),
    agreement_type: AgreementType | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    rows = await svc.list_agreements_for_actor(
        actor=actor, status=status_filter,
        admin_id=admin_id, broker_id=broker_id,
        agreement_type=agreement_type,
        skip=skip, limit=limit,
    )
    dtos = [await _serialize_agreement(a) for a in rows]
    return APIResponse(data=dtos)


@router.post("/agreements", response_model=APIResponse[AgreementDTO])
async def create_agreement(body: CreateAgreementRequest, actor: CurrentAdmin):
    if actor.role == UserRole.ADMIN and body.admin_id != actor.id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "cannot create for another admin"
        )
    if actor.role == UserRole.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "broker cannot create agreement")
    try:
        a = await svc.create_agreement(
            actor=actor,
            admin_id=body.admin_id,
            broker_id=body.broker_id,
            share_pct=body.share_pct,
            settlement_mode=body.settlement_mode,
            settlement_cadence=body.settlement_cadence,
            agreement_type=body.agreement_type,
        )
    except svc.AgreementConflict as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    except svc.AgreementValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return APIResponse(data=await _serialize_agreement(a))


@router.get("/agreements/{agreement_id}", response_model=APIResponse[AgreementDTO])
async def get_agreement(agreement_id: PydanticObjectId, actor: CurrentAdmin):
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agreement not found")
    if not _can_view(actor, a):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    return APIResponse(data=await _serialize_agreement(a))


@router.patch("/agreements/{agreement_id}", response_model=APIResponse[AgreementDTO])
async def update_agreement_endpoint(
    agreement_id: PydanticObjectId,
    body: UpdateAgreementRequest,
    actor: CurrentAdmin,
):
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agreement not found")
    if not _can_edit(actor, a):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no edit access")
    try:
        updated = await svc.update_agreement(
            actor=actor,
            agreement_id=agreement_id,
            share_pct=body.share_pct,
            settlement_mode=body.settlement_mode,
            settlement_cadence=body.settlement_cadence,
        )
    except svc.AgreementValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return APIResponse(data=await _serialize_agreement(updated))


@router.post("/agreements/{agreement_id}/pause", response_model=APIResponse[AgreementDTO])
async def pause_endpoint(agreement_id: PydanticObjectId, actor: CurrentAdmin):
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agreement not found")
    if not _can_edit(actor, a):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no edit access")
    try:
        updated = await svc.pause_agreement(actor=actor, agreement_id=agreement_id)
    except svc.AgreementValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return APIResponse(data=await _serialize_agreement(updated))


@router.post("/agreements/{agreement_id}/resume", response_model=APIResponse[AgreementDTO])
async def resume_endpoint(agreement_id: PydanticObjectId, actor: CurrentAdmin):
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agreement not found")
    if not _can_edit(actor, a):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no edit access")
    try:
        updated = await svc.resume_agreement(actor=actor, agreement_id=agreement_id)
    except svc.AgreementValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return APIResponse(data=await _serialize_agreement(updated))


@router.post("/agreements/{agreement_id}/end", response_model=APIResponse[AgreementDTO])
async def end_endpoint(agreement_id: PydanticObjectId, actor: CurrentAdmin):
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agreement not found")
    if not _can_edit(actor, a):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no edit access")
    try:
        updated = await svc.end_agreement(actor=actor, agreement_id=agreement_id)
    except svc.AgreementValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return APIResponse(data=await _serialize_agreement(updated))


@router.get("/settlements", response_model=APIResponse[list[SettlementDTO]])
async def list_settlements(
    actor: CurrentAdmin,
    agreement_id: PydanticObjectId | None = Query(default=None),
    status_filter: SharingSettlementStatus | None = Query(default=None, alias="status"),
    from_date: datetime | None = Query(default=None),
    to_date: datetime | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    q = PnlSharingSettlement.find()
    if actor.role == UserRole.ADMIN:
        q = q.find(PnlSharingSettlement.admin_id == actor.id)
    elif actor.role == UserRole.BROKER:
        q = q.find(PnlSharingSettlement.broker_id == actor.id)
    if agreement_id is not None:
        q = q.find(PnlSharingSettlement.agreement_id == agreement_id)
    if status_filter is not None:
        q = q.find(PnlSharingSettlement.status == status_filter)
    if from_date is not None:
        q = q.find(PnlSharingSettlement.period_start >= from_date)
    if to_date is not None:
        q = q.find(PnlSharingSettlement.period_end <= to_date)
    rows = await (
        q.sort(-PnlSharingSettlement.period_start)
         .skip(skip)
         .limit(limit)
         .to_list()
    )
    return APIResponse(data=[await _serialize_settlement(r) for r in rows])


@router.post("/settlements/manual", response_model=APIResponse[SettlementDTO])
async def manual_settle(body: ManualSettleRequest, actor: CurrentAdmin):
    agreement = await PnlSharingAgreement.get(body.agreement_id)
    if agreement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agreement not found")
    if not _can_edit(actor, agreement):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no settle access")
    ref = body.period_start or now_utc()
    period_start, period_end = svc.compute_period_bounds(body.cadence, ref)
    try:
        settlement = await svc.settle_period(
            agreement_id=agreement.id,
            period_start=period_start,
            period_end=period_end,
            cadence=body.cadence,
            triggered_by="MANUAL",
            actor=actor,
        )
    except svc.AgreementValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return APIResponse(data=await _serialize_settlement(settlement))


@router.post(
    "/settlements/{settlement_id}/retry",
    response_model=APIResponse[SettlementDTO],
)
async def retry_settlement(settlement_id: PydanticObjectId, actor: CurrentAdmin):
    s = await PnlSharingSettlement.get(settlement_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "settlement not found")
    if s.status != SharingSettlementStatus.FAILED:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "only FAILED settlements can be retried"
        )
    agreement = await PnlSharingAgreement.get(s.agreement_id)
    if agreement is None or not _can_edit(actor, agreement):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no retry access")
    try:
        settlement = await svc.settle_period(
            agreement_id=s.agreement_id,
            period_start=s.period_start,
            period_end=s.period_end,
            cadence=s.cadence,
            triggered_by="MANUAL",
            actor=actor,
        )
    except svc.AgreementValidationError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return APIResponse(data=await _serialize_settlement(settlement))


@router.get("/reports/{agreement_id}", response_model=APIResponse[ReportResponse])
async def get_report(
    agreement_id: PydanticObjectId,
    actor: CurrentAdmin,
    period: SettlementCadence = Query(...),
    from_date: datetime = Query(..., alias="from"),
    to_date: datetime = Query(..., alias="to"),
):
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agreement not found")
    if not _can_view(actor, a):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    if from_date > to_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "from > to")
    report = await svc.build_report(
        agreement=a, cadence=period, from_dt=from_date, to_dt=to_date,
    )
    return APIResponse(
        data=ReportResponse(
            agreement=await _serialize_agreement(a),
            rows=report.rows,
            summary=report.summary,
        )
    )


@router.get("/me/agreement", response_model=APIResponse[AgreementDTO])
async def my_agreement(actor: CurrentAdmin):
    """Broker-self: fetch the broker's own active agreement (404 if none)."""
    if actor.role != UserRole.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "brokers only")
    a = await PnlSharingAgreement.find_one(
        PnlSharingAgreement.broker_id == actor.id,
        PnlSharingAgreement.status != AgreementStatus.ENDED,
    )
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active agreement")
    return APIResponse(data=await _serialize_agreement(a))


@router.get("/reports/{agreement_id}/download")
async def download_report(
    agreement_id: PydanticObjectId,
    actor: CurrentAdmin,
    period: SettlementCadence = Query(...),
    from_date: datetime = Query(..., alias="from"),
    to_date: datetime = Query(..., alias="to"),
    format: str = Query(..., regex="^(pdf|excel)$"),
):
    """Stream a PDF or Excel report. Same auth scope as GET /reports/{id}."""
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agreement not found")
    if not _can_view(actor, a):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no access")
    if from_date > to_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "from > to")
    report = await svc.build_report(
        agreement=a, cadence=period, from_dt=from_date, to_dt=to_date,
    )
    response = ReportResponse(
        agreement=await _serialize_agreement(a),
        rows=report.rows,
        summary=report.summary,
    )

    if format == "pdf":
        from app.services.pnl_sharing_pdf_service import render_report_pdf
        data = render_report_pdf(response)
        media_type = "application/pdf"
        ext = "pdf"
    else:  # excel
        from app.services.pnl_sharing_excel_service import render_report_excel
        data = render_report_excel(response)
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        ext = "xlsx"

    filename = (
        f"pnl_sharing_{a.admin_user_code or 'admin'}_"
        f"{a.broker_user_code or 'broker'}_{period}.{ext}"
    )
    return StreamingResponse(
        BytesIO(data),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
