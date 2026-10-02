"""Request / response schemas for the super-admin management surface."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field

from app.models.user import AdminPermissions


class CreateSubAdminRequest(BaseModel):
    full_name: str
    email: EmailStr
    mobile: str
    password: str = Field(min_length=6)
    permissions: AdminPermissions = Field(default_factory=AdminPermissions)
    pnl_share_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)


class UpdateSubAdminRequest(BaseModel):
    full_name: str | None = None


class UpdatePermissionsRequest(BaseModel):
    permissions: AdminPermissions


class UpdatePnlShareRequest(BaseModel):
    pct: Decimal = Field(ge=0, le=100)


class SetMaintenanceRequest(BaseModel):
    enabled: bool


class AssignUserRequest(BaseModel):
    sub_admin_id: str | None = None  # None → return user to super-admin pool


class BulkAssignRequest(BaseModel):
    user_ids: list[str] = Field(min_length=1)
    sub_admin_id: str | None = None


class RecomputeSettlementRequest(BaseModel):
    week_start: date  # IST Monday (any date inside the week also accepted by service)
    sub_admin_id: str | None = None  # None → recompute for every sub-admin


class MarkPaidRequest(BaseModel):
    notes: str | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=6, max_length=128)


class SubAdminDTO(BaseModel):
    id: str
    user_code: str
    full_name: str
    email: str
    mobile: str
    status: str
    permissions: AdminPermissions | None = None
    pnl_share_pct: str
    user_count: int = 0  # active trading clients (CLOSED + broker rows excluded)
    broker_count: int = 0  # broker + sub-broker login accounts under this admin
    maintenance_mode: bool = False  # ON → this admin's whole pool is login-locked
    payment_gateway_enabled: bool = False  # ON → this pool gets the Divinepay UPI flow
    created_at: datetime | None = None


class SettlementDTO(BaseModel):
    id: str
    sub_admin_id: str
    sub_admin_name: str | None = None
    sub_admin_code: str | None = None
    period_start: datetime
    period_end: datetime
    user_count: int
    gross_user_loss_inr: str
    gross_user_profit_inr: str
    total_brokerage_inr: str
    net_house_pnl_inr: str
    pnl_share_pct_snapshot: str
    sub_admin_share_inr: str
    status: str
    finalized_at: datetime | None = None
    paid_at: datetime | None = None
    notes: str | None = None
    frozen: bool = False
