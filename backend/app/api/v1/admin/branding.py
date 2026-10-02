"""Admin-side branding management endpoints.

Lives under ``/api/v1/admin/branding/*`` and requires an ``ADMIN``
role token (super-admin can also call these — they just don't have
any users to brand to). All mutations are gated behind
``settings.BRANDING_ENABLED``; flipping the flag off effectively
freezes the subsystem without removing any data.

Endpoints:

| Method | Path                          | Purpose                              |
|--------|-------------------------------|--------------------------------------|
| GET    | /admin/branding/me            | Read current admin's branding row    |
| POST   | /admin/branding/logo          | Upload / replace logo (multipart)    |
| PUT    | /admin/branding               | Update brand_name + custom_domain    |
| POST   | /admin/branding/domain/verify | DNS check + enqueue cert provisioning|
| GET    | /admin/branding/domain/status | Poll lifecycle (PENDING → READY)     |
| POST   | /admin/branding/domain/disconnect | Clear the domain on this row     |
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.dependencies import CurrentAdmin
from app.core.exceptions import InsufficientPermissionsError
from app.models.user import UserRole
from app.schemas.common import APIResponse
from app.services import branding_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/branding", tags=["admin-branding"])


def _gate() -> None:
    if not settings.BRANDING_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Branding feature is not enabled on this server.",
        )


def _require_admin_role(admin) -> None:
    # SUPER_ADMIN can also configure their own row if they want to
    # platform-brand. BROKER cannot — branding is admin-tier only.
    if admin.role not in {UserRole.ADMIN, UserRole.SUPER_ADMIN}:
        raise InsufficientPermissionsError(
            "Branding is only available for admin-tier accounts."
        )


# ── Schemas ──────────────────────────────────────────────────────────
class BrandingUpdateRequest(BaseModel):
    brand_name: str | None = Field(default=None, max_length=64)
    # Per-admin Telegram link shown on that admin's login page. "" clears it.
    telegram_link: str | None = Field(default=None, max_length=300)
    # Pass empty-string or null to clear; pass a hostname to set/replace.
    custom_domain: str | None = Field(default=None, max_length=253)
    # Set true to explicitly disconnect — preferred over relying on
    # `custom_domain == ""` so the intent is unambiguous.
    clear_custom_domain: bool = False


class DomainStatusOut(BaseModel):
    custom_domain: str | None
    custom_domain_status: str | None
    custom_domain_last_error: str | None
    custom_domain_verified_at: str | None


# ── Endpoints ────────────────────────────────────────────────────────
@router.get("/me", response_model=APIResponse[dict])
async def get_my_branding(admin: CurrentAdmin):
    _gate()
    _require_admin_role(admin)
    return APIResponse(data=branding_service.to_branding_payload(admin))


@router.put("", response_model=APIResponse[dict])
async def update_branding(payload: BrandingUpdateRequest, admin: CurrentAdmin):
    _gate()
    _require_admin_role(admin)
    updated = await branding_service.update_branding(
        admin=admin,
        brand_name=payload.brand_name,
        telegram_link=payload.telegram_link,
        custom_domain=payload.custom_domain,
        clear_custom_domain=payload.clear_custom_domain,
    )
    return APIResponse(
        data=branding_service.to_branding_payload(updated),
        message="Branding updated.",
    )


@router.post("/logo", response_model=APIResponse[dict])
async def upload_logo(
    admin: CurrentAdmin,
    file: Annotated[UploadFile, File(description="PNG / JPEG / WEBP / SVG, ≤ 2 MB")],
):
    _gate()
    _require_admin_role(admin)
    content = await file.read()
    mime = file.content_type or "application/octet-stream"
    updated = await branding_service.save_logo(admin, content=content, mime=mime)
    return APIResponse(
        data=branding_service.to_branding_payload(updated),
        message="Logo uploaded.",
    )


@router.get("/domain/dns-preview", response_model=APIResponse[dict])
async def dns_preview(admin: CurrentAdmin):
    """Return a side-by-side preview of current vs expected A records.

    Lets the admin UI render `current` next to `update to` in the DNS
    instructions table — no need to context-switch to a `dig` tool.
    Returns the preview shape for whichever domain is currently saved
    on this admin's row; 400 if no domain set.
    """
    _gate()
    _require_admin_role(admin)
    if not admin.custom_domain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No custom_domain saved on this admin row.",
        )
    return APIResponse(
        data=await branding_service.resolve_dns_preview(admin.custom_domain),
    )


@router.post("/domain/verify", response_model=APIResponse[DomainStatusOut])
async def verify_domain(admin: CurrentAdmin):
    _gate()
    _require_admin_role(admin)
    updated = await branding_service.begin_domain_verification(admin)
    return APIResponse(
        data=DomainStatusOut(
            custom_domain=updated.custom_domain,
            custom_domain_status=updated.custom_domain_status,
            custom_domain_last_error=updated.custom_domain_last_error,
            custom_domain_verified_at=(
                updated.custom_domain_verified_at.isoformat()
                if updated.custom_domain_verified_at
                else None
            ),
        )
    )


@router.get("/domain/status", response_model=APIResponse[DomainStatusOut])
async def domain_status(admin: CurrentAdmin):
    _gate()
    _require_admin_role(admin)
    return APIResponse(
        data=DomainStatusOut(
            custom_domain=admin.custom_domain,
            custom_domain_status=admin.custom_domain_status,
            custom_domain_last_error=admin.custom_domain_last_error,
            custom_domain_verified_at=(
                admin.custom_domain_verified_at.isoformat()
                if admin.custom_domain_verified_at
                else None
            ),
        )
    )


@router.post("/domain/disconnect", response_model=APIResponse[dict])
async def disconnect_domain(admin: CurrentAdmin):
    _gate()
    _require_admin_role(admin)
    updated = await branding_service.disconnect_domain(admin)
    return APIResponse(
        data=branding_service.to_branding_payload(updated),
        message="Domain disconnected.",
    )
