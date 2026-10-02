"""Admin-side request/response schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateUserRequest(BaseModel):
    full_name: str
    # Optional for an admin-created account: those users log in with the
    # user code they're handed. A self-signup still demands both — see
    # `RegisterRequest`. Blank here means the backend stores a placeholder
    # (the email / mobile columns are uniquely indexed and non-null).
    email: str | None = None
    mobile: str | None = None
    password: str = Field(min_length=6)
    role: str = "CLIENT"
    parent_id: str | None = None
    # Broker / sub-broker to place this user under.  When the caller is
    # an ADMIN, this can be any broker/sub-broker in their pool.  When
    # the caller is a BROKER, this can be any sub-broker under them.
    # `null` = "Self" (keep the user directly under the caller).
    assign_to_broker_id: str | None = None
    is_demo: bool = False
    initial_balance: float = 0
    credit_limit: float = 0


class WalletAdjustRequest(BaseModel):
    amount: float
    narration: str
    transaction_type: str = "ADJUSTMENT"  # ADJUSTMENT / BONUS / PENALTY / PROMO


class BlockUserRequest(BaseModel):
    reason: str | None = None


class UpdateGlobalSettingRequest(BaseModel):
    patch: dict[str, Any]


class UpsertUserOverrideRequest(BaseModel):
    patch: dict[str, Any]


class ApproveDepositRequest(BaseModel):
    admin_remark: str | None = None


class RejectDepositRequest(BaseModel):
    admin_remark: str


class ApproveWithdrawalRequest(BaseModel):
    utr_number: str | None = None
    admin_remark: str | None = None


class RejectWithdrawalRequest(BaseModel):
    rejection_reason: str


class UpdatePlatformSettingRequest(BaseModel):
    setting_value: Any
