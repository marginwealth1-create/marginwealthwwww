"""Admin auth endpoint — login + refresh + logout (with mandatory 2FA, API-key + IP guard).

Note: the API-key + IP guard is enforced by `get_current_admin` for protected
routes. The login endpoint itself is intentionally accessible without a key —
otherwise no one could log in. We rely on rate-limiting + correct credentials
+ mandatory 2FA + audit logging to harden it.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from app.core.dependencies import CurrentAdmin
from app.core.exceptions import InvalidCredentialsError
from app.core.rate_limit import rate_limit
from app.models.audit_log import AuditAction
from app.models.user import User, UserRole
from app.schemas.admin.auth import AdminLoginRequest, AdminTokenPair, AdminUserOut
from app.schemas.auth import LogoutRequest, RefreshRequest, TokenPair
from app.schemas.common import APIResponse, OkResponse
from app.core.config import settings
from app.services import auth_service


async def _branding_fields_for(admin_user: User) -> dict:
    """Return the branding kwargs to pass into AdminUserOut for this row.

    Cascade rules (confirmed with operator):
      - SUPER_ADMIN → no branding ever. Sidebar shows platform default.
                      Super-admin runs the whole system and is not part
                      of any tenant.
      - ADMIN       → their OWN brand_name / logo_url.
      - BROKER      → branding INHERITED from their parent ADMIN
                      (resolved via `assigned_admin_id`). Sub-brokers
                      have the same `assigned_admin_id` populated by
                      the broker-management service when they're
                      minted, so this single hop covers any depth of
                      sub-broker nesting without walking parent_id.
                      A broker created directly under super-admin (no
                      assigned_admin_id) gets platform default.

    The helper short-circuits when `BRANDING_ENABLED=false` so admins
    on a fresh deploy see the unchanged platform sidebar until the
    operator flips the flag.
    """
    # custom_domain is always returned — needed for referral link generation
    # on the dashboard regardless of BRANDING_ENABLED. brand_name/logo_url
    # are still gated behind the flag (white-label sidebar feature).
    if admin_user.role == UserRole.ADMIN:
        return {
            "brand_name": admin_user.brand_name if settings.BRANDING_ENABLED else None,
            "logo_url": admin_user.logo_url if settings.BRANDING_ENABLED else None,
            "custom_domain": admin_user.custom_domain,
            "custom_domain_status": admin_user.custom_domain_status,
        }

    if admin_user.role == UserRole.BROKER and admin_user.assigned_admin_id is not None:
        parent_admin = await User.get(admin_user.assigned_admin_id)
        if (
            parent_admin is not None
            and parent_admin.role == UserRole.ADMIN
        ):
            return {
                "brand_name": parent_admin.brand_name if settings.BRANDING_ENABLED else None,
                "logo_url": parent_admin.logo_url if settings.BRANDING_ENABLED else None,
                "custom_domain": parent_admin.custom_domain,
                "custom_domain_status": parent_admin.custom_domain_status,
            }

    # SUPER_ADMIN, top-level brokers under super-admin pool, anything else.
    return {"brand_name": None, "logo_url": None, "custom_domain": None, "custom_domain_status": None}

router = APIRouter(prefix="/auth", tags=["admin-auth"])


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


@router.post(
    "/login",
    response_model=APIResponse[AdminTokenPair],
    status_code=status.HTTP_200_OK,
    dependencies=[rate_limit("auth")],
)
async def admin_login(payload: AdminLoginRequest, request: Request):
    pair: TokenPair = await auth_service.authenticate(
        identifier=payload.identifier,
        password=payload.password,
        two_fa_code=payload.two_fa_code,
        audience="admin",
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if pair.user.role not in {
        UserRole.SUPER_ADMIN.value,
        UserRole.ADMIN.value,
        UserRole.BROKER.value,
    }:
        raise InvalidCredentialsError()

    admin_user = await User.get(pair.user.id)
    if admin_user is None:
        raise InvalidCredentialsError()

    return APIResponse(
        data=AdminTokenPair(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_in=pair.expires_in,
            admin=AdminUserOut(
                id=pair.user.id,
                user_code=pair.user.user_code,
                email=pair.user.email,
                full_name=pair.user.full_name,
                role=pair.user.role,
                last_login_at=None,
                admin_permissions=admin_user.admin_permissions,
                pnl_share_pct=(
                    str(admin_user.pnl_share_pct)
                    if admin_user.pnl_share_pct is not None
                    else None
                ),
                broker_permissions=admin_user.broker_permissions,
                assigned_broker_id=(
                    str(admin_user.assigned_broker_id)
                    if admin_user.assigned_broker_id
                    else None
                ),
                **(await _branding_fields_for(admin_user)),
            ),
        )
    )


@router.post(
    "/employee-login",
    response_model=APIResponse[AdminTokenPair],
    status_code=status.HTTP_200_OK,
    dependencies=[rate_limit("auth")],
)
async def employee_login(payload: AdminLoginRequest, request: Request):
    """Separate login portal for EMPLOYEE (staff) accounts. Same admin token
    machinery, but accepts ONLY role == EMPLOYEE — so admins can't use the
    employee portal and employees can't use the admin /login. After login the
    employee lands in the admin app showing only their granted sections."""
    pair: TokenPair = await auth_service.authenticate(
        identifier=payload.identifier,
        password=payload.password,
        two_fa_code=payload.two_fa_code,
        audience="admin",
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    if pair.user.role != UserRole.EMPLOYEE.value:
        raise InvalidCredentialsError()

    emp = await User.get(pair.user.id)
    if emp is None:
        raise InvalidCredentialsError()

    return APIResponse(
        data=AdminTokenPair(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_in=pair.expires_in,
            admin=AdminUserOut(
                id=pair.user.id,
                user_code=pair.user.user_code,
                email=pair.user.email,
                full_name=pair.user.full_name,
                role=pair.user.role,
                last_login_at=None,
                admin_permissions=emp.admin_permissions,
                pnl_share_pct=None,
                broker_permissions=None,
                assigned_broker_id=None,
                **(await _branding_fields_for(emp)),
            ),
        )
    )


@router.post("/refresh", response_model=APIResponse[AdminTokenPair])
async def admin_refresh(payload: RefreshRequest):
    pair = await auth_service.refresh_tokens(payload.refresh_token)
    if pair.user.role not in {
        UserRole.SUPER_ADMIN.value,
        UserRole.ADMIN.value,
        UserRole.BROKER.value,
        UserRole.EMPLOYEE.value,
    }:
        raise InvalidCredentialsError()
    admin_user = await User.get(pair.user.id)
    if admin_user is None:
        raise InvalidCredentialsError()
    return APIResponse(
        data=AdminTokenPair(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_in=pair.expires_in,
            admin=AdminUserOut(
                id=pair.user.id,
                user_code=pair.user.user_code,
                email=pair.user.email,
                full_name=pair.user.full_name,
                role=pair.user.role,
                admin_permissions=admin_user.admin_permissions,
                pnl_share_pct=(
                    str(admin_user.pnl_share_pct)
                    if admin_user.pnl_share_pct is not None
                    else None
                ),
                broker_permissions=admin_user.broker_permissions,
                assigned_broker_id=(
                    str(admin_user.assigned_broker_id)
                    if admin_user.assigned_broker_id
                    else None
                ),
                **(await _branding_fields_for(admin_user)),
            ),
        )
    )


@router.post("/logout", response_model=APIResponse[OkResponse])
async def admin_logout(payload: LogoutRequest, admin: CurrentAdmin):
    from app.services.audit_service import log_event

    await auth_service.logout(refresh_token=payload.refresh_token, user_id=str(admin.id))
    await log_event(action=AuditAction.LOGOUT, entity_type="User", entity_id=admin.id, actor_id=admin.id)
    return APIResponse(data=OkResponse(message="Admin logged out"))


@router.get("/me", response_model=APIResponse[AdminUserOut])
async def admin_me(admin: CurrentAdmin):
    return APIResponse(
        data=AdminUserOut(
            id=str(admin.id),
            user_code=admin.user_code,
            email=admin.email,
            full_name=admin.full_name,
            role=admin.role.value,
            last_login_at=admin.last_login_at.isoformat() if admin.last_login_at else None,
            admin_permissions=admin.admin_permissions,
            broker_permissions=admin.broker_permissions,
            pnl_share_pct=(
                str(admin.pnl_share_pct) if admin.pnl_share_pct is not None else None
            ),
            assigned_broker_id=(
                str(admin.assigned_broker_id) if admin.assigned_broker_id else None
            ),
            **(await _branding_fields_for(admin)),
        )
    )


class AdminChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6, max_length=128)


@router.post(
    "/change-password",
    response_model=APIResponse[OkResponse],
    dependencies=[rate_limit("auth")],
)
async def admin_change_password(payload: AdminChangePasswordRequest, admin: CurrentAdmin):
    """Any admin-tier user (SUPER_ADMIN / ADMIN / BROKER / sub-broker / EMPLOYEE)
    changes their OWN password: verify the current one, then set the new hash.
    The current session stays valid — no forced re-login."""
    from app.core.security import hash_password, verify_password
    from app.services.audit_service import log_event

    if not verify_password(payload.current_password, admin.password_hash):
        raise InvalidCredentialsError("Current password is incorrect")
    if verify_password(payload.new_password, admin.password_hash):
        raise InvalidCredentialsError("New password must be different from the current one")
    admin.password_hash = hash_password(payload.new_password)
    admin.must_change_password = False
    await admin.save()
    await log_event(
        action=AuditAction.PASSWORD_CHANGE,
        entity_type="User",
        entity_id=admin.id,
        actor_id=admin.id,
        target_user_id=admin.id,
        metadata={"self": True},
    )
    return APIResponse(data=OkResponse(message="Password changed successfully"))
