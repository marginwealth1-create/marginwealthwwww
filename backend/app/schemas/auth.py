"""Auth request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.user import AdminPermissions, BrokerPermissions
from app.utils.validators import (
    is_valid_mobile_in,
    is_valid_pan,
    normalize_mobile_in,
)


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=128, description="email or 10-digit mobile")
    password: str = Field(min_length=6, max_length=128)
    two_fa_code: str | None = Field(default=None, min_length=6, max_length=8)

    @field_validator("identifier")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip().lower()


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access_token expires
    user: "AuthUserOut"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str | None = None  # if provided, revoke this refresh token


class AuthUserOut(BaseModel):
    id: str
    user_code: str
    email: str
    mobile: str
    full_name: str
    role: str
    status: str
    is_demo: bool
    two_fa_enabled: bool
    must_change_password: bool
    # Sub-admin context — only populated when role == "ADMIN", else None.
    admin_permissions: AdminPermissions | None = None
    pnl_share_pct: str | None = None
    # Broker context — only populated when role == "BROKER".
    broker_permissions: BrokerPermissions | None = None
    # Parent broker id — set when this BROKER was created under another
    # broker (i.e., they're a sub-broker). The UI flips the role chip to
    # "Sub-broker" when this is present.
    assigned_broker_id: str | None = None


class RegisterRequest(BaseModel):
    email: EmailStr
    mobile: str
    password: str = Field(min_length=6, max_length=128)
    full_name: str = Field(min_length=2, max_length=128)
    pan: str | None = None
    referral_code: str | None = None

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, v: str) -> str:
        v = normalize_mobile_in(v)
        if not is_valid_mobile_in(v):
            raise ValueError("Invalid Indian mobile number")
        return v

    @field_validator("pan")
    @classmethod
    def _pan(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.upper().strip()
        if not is_valid_pan(v):
            raise ValueError("Invalid PAN format")
        return v

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not any(c.isupper() for c in v):
            raise ValueError("Password must contain an uppercase letter")
        if not any(c.islower() for c in v):
            raise ValueError("Password must contain a lowercase letter")
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain a digit")
        return v


class OtpRequest(BaseModel):
    identifier: str = Field(description="email or mobile")
    purpose: str = Field(description="register / login / reset_password / withdrawal")


class OtpVerifyRequest(BaseModel):
    identifier: str
    purpose: str
    code: str = Field(min_length=4, max_length=8)


class ForgotPasswordRequest(BaseModel):
    identifier: str  # email or mobile


class ResetPasswordRequest(BaseModel):
    identifier: str
    otp: str = Field(min_length=4, max_length=8)
    new_password: str = Field(min_length=6, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6, max_length=128)


class TwoFASetupResponse(BaseModel):
    secret: str
    provisioning_uri: str  # otpauth:// URI for QR codes


class TwoFAEnableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class TwoFADisableRequest(BaseModel):
    password: str
    code: str = Field(min_length=6, max_length=6)


# Forward refs
TokenPair.model_rebuild()
