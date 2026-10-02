"""Request/response DTOs for /api/v1/admin/pnl-sharing/* endpoints."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from beanie import PydanticObjectId
from pydantic import BaseModel, Field

from app.models.pnl_sharing import (
    AgreementStatus,
    AgreementType,
    SettlementCadence,
    SettlementMode,
    SharingSettlementStatus,
)


# ── Agreement endpoints ─────────────────────────────────────────────────


class CreateAgreementRequest(BaseModel):
    admin_id: PydanticObjectId
    broker_id: PydanticObjectId
    share_pct: Decimal = Field(ge=0, le=100)
    settlement_mode: SettlementMode
    settlement_cadence: SettlementCadence | None = None
    agreement_type: AgreementType = AgreementType.PNL_AND_BROKERAGE


class UpdateAgreementRequest(BaseModel):
    share_pct: Decimal | None = Field(default=None, ge=0, le=100)
    settlement_mode: SettlementMode | None = None
    settlement_cadence: SettlementCadence | None = None


class AgreementDTO(BaseModel):
    id: str
    admin_id: str
    admin_name: str | None = None
    admin_user_code: str | None = None
    broker_id: str
    broker_name: str | None = None
    broker_user_code: str | None = None
    share_pct: str
    settlement_mode: SettlementMode
    settlement_cadence: SettlementCadence | None
    agreement_type: AgreementType
    status: AgreementStatus
    effective_from: datetime
    effective_until: datetime | None
    created_at: datetime
    updated_at: datetime


# ── Settlement endpoints ─────────────────────────────────────────────────


class ManualSettleRequest(BaseModel):
    agreement_id: PydanticObjectId
    period_start: datetime | None = None  # default = current open period of `cadence`
    cadence: SettlementCadence


class SettlementDTO(BaseModel):
    id: str
    agreement_id: str
    admin_id: str
    broker_id: str
    period_start: datetime
    period_end: datetime
    cadence: SettlementCadence
    net_client_pnl_inr: str
    net_client_bkg_inr: str
    total_of_both_inr: str
    actual_pnl_inr: str
    sharing_pnl_inr: str
    sharing_bkg_inr: str
    sharing_total_inr: str
    share_pct_snapshot: str
    status: SharingSettlementStatus
    settled_at: datetime | None
    failure_reason: str | None
    retry_count: int


# ── Report endpoints ─────────────────────────────────────────────────


class ReportRow(BaseModel):
    period_start: datetime
    period_end: datetime
    net_client_pnl_inr: str
    net_client_bkg_inr: str
    total_of_both_inr: str
    actual_pnl_inr: str
    sharing_pnl_inr: str
    sharing_bkg_inr: str
    settlement_status: str  # SETTLED | PENDING | FAILED | UNSETTLED


class ReportSummary(BaseModel):
    total_sharing_pnl_inr: str
    total_sharing_bkg_inr: str
    periods_settled: int
    periods_pending: int
    periods_failed: int
    periods_unsettled: int


class ReportResponse(BaseModel):
    agreement: AgreementDTO
    rows: list[ReportRow]
    summary: ReportSummary
