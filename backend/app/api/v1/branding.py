"""Public branding lookup endpoints.

These are unauthenticated by design — the login/signup/landing pages
need to render an admin's logo + brand name *before* the user has
any token. Returns 404 cleanly when:

* admin not found, or
* admin role != ADMIN, or
* admin status != ACTIVE, or
* feature flag is off.

Mounted at ``/api/v1/branding`` from ``app/main.py`` (NOT under
``/api/v1/user`` — these are platform-level lookups, not user-scoped).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.core.config import settings
from app.schemas.common import APIResponse
from app.services import branding_service

router = APIRouter(prefix="/branding", tags=["branding"])


def _gate() -> None:
    if not settings.BRANDING_ENABLED:
        # 503 (not 404) so monitoring tools can distinguish "feature
        # off globally" from "this admin doesn't exist".
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Branding feature is not enabled on this server.",
        )


@router.get("/by-code/{user_code}", response_model=APIResponse[dict])
async def by_code(user_code: str):
    """Resolve an admin by their `user_code` (e.g. ``ADM12345678``).

    The ``user_code`` doubles as the public referral key — a single
    routing identity per admin, no extra slug field needed.
    """
    _gate()
    admin = await branding_service.find_admin_by_user_code(user_code)
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No branding for this code."
        )
    return APIResponse(data=branding_service.to_branding_payload(admin))


@router.get("/platform", response_model=APIResponse[dict])
async def platform():
    """Resolve the PLATFORM-DEFAULT branding — i.e. the super admin's
    logo + brand_name + favicon.  Used by the user app when the
    visitor is on the platform host (marginwealth.live, no tenant
    referral) so the auth screens still render the super admin's
    branding instead of falling back to the generic glyph.
    """
    _gate()
    super_admin = await branding_service.find_platform_super_admin()
    if super_admin is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No platform branding configured.",
        )
    return APIResponse(data=branding_service.to_branding_payload(super_admin))


@router.get("/by-domain", response_model=APIResponse[dict])
async def by_domain(
    domain: str = Query(..., min_length=3, max_length=253),
):
    """Resolve an admin by their connected ``custom_domain``.

    Used by ``frontend-user`` on every page load to detect that it's
    running on a tenant's host (vs the platform host) and apply that
    tenant's branding.
    """
    _gate()
    admin = await branding_service.find_admin_by_domain(domain)
    if admin is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No branding for this domain."
        )
    return APIResponse(data=branding_service.to_branding_payload(admin))
