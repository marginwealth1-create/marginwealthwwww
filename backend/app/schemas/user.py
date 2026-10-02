"""User profile schemas (returned by /user/me, /admin/users/...)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.user import (
    AccountType,
    CommunicationPrefs,
    KycInfo,
    RiskProfile,
    TradingHours,
    UserPermissions,
    UserRole,
    UserStatus,
)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    user_code: str
    email: str  # plain str — demo users use non-standard domains; soft-deleted users have mangled emails
    mobile: str
    full_name: str
    photo_url: str | None = None
    role: UserRole
    status: UserStatus
    account_type: AccountType
    is_demo: bool
    parent_id: str | None = None
    kyc: KycInfo
    permissions: UserPermissions
    trading_hours: TradingHours
    risk: RiskProfile
    communication: CommunicationPrefs
    two_fa_enabled: bool
    last_login_at: datetime | None = None
    created_at: datetime


class UserMeOut(UserOut):
    """Same as UserOut for now — separate type so we can extend safely."""


class UpdateProfileRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=128)
    photo_url: str | None = None
    communication: CommunicationPrefs | None = None
    kyc: KycInfo | None = None


class UserListFilters(BaseModel):
    q: str | None = None  # full-text on name/email/mobile/code
    role: UserRole | None = None
    status: UserStatus | None = None
    parent_id: str | None = None
    is_demo: bool | None = None
    page: int = 1
    page_size: int = 20
