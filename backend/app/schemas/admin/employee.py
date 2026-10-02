"""Request / response schemas for the Employee-management surface.

Employees are staff sub-users of an admin (super or sub). They reuse the
existing ``AdminPermissions`` boolean-per-section model; the creating admin can
only grant sections they themselves hold (the ceiling is enforced server-side).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.user import AdminPermissions


class CreateEmployeeRequest(BaseModel):
    full_name: str
    email: EmailStr
    mobile: str
    password: str = Field(min_length=6)
    permissions: AdminPermissions = Field(default_factory=AdminPermissions)


class UpdateEmployeePermissionsRequest(BaseModel):
    permissions: AdminPermissions


class ResetEmployeePasswordRequest(BaseModel):
    new_password: str = Field(min_length=6, max_length=128)


class EmployeeDTO(BaseModel):
    id: str
    user_code: str
    full_name: str
    email: str
    mobile: str
    status: str
    permissions: AdminPermissions | None = None
    employer_admin_id: str | None = None
    employer_name: str | None = None
    created_at: datetime | None = None
