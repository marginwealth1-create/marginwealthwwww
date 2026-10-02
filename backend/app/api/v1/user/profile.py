"""User profile endpoint — /api/v1/user/users/me."""

from __future__ import annotations

from fastapi import APIRouter

from app.core.config import settings
from app.core.dependencies import CurrentUser
from app.models.user import User, UserRole, UserStatus
from app.schemas.common import APIResponse
from app.schemas.user import UpdateProfileRequest, UserMeOut
from app.services import branding_service

router = APIRouter(prefix="/users", tags=["user-profile"])


@router.get("/me", response_model=APIResponse[UserMeOut])
async def get_me(user: CurrentUser):
    return APIResponse(data=_user_to_me(user))


@router.put("/me", response_model=APIResponse[UserMeOut])
async def update_me(payload: UpdateProfileRequest, user: CurrentUser):
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.photo_url is not None:
        user.photo_url = payload.photo_url
    if payload.communication is not None:
        user.communication = payload.communication
    if payload.kyc is not None:
        # Only allow updating non-verified KYC fields
        if not user.kyc.is_verified:
            user.kyc = payload.kyc
    await user.save()
    return APIResponse(data=_user_to_me(user))


@router.get("/me/branding", response_model=APIResponse[dict])
async def get_my_branding(user: CurrentUser):
    """Return the branding payload for the logged-in user's
    ``assigned_admin_id`` (or ``None`` if the user is in the
    super-admin/platform pool).

    The frontend's ``BrandingProvider`` calls this once after login
    to apply the right logo / brand-name / favicon on the dashboard,
    and to decide whether to redirect to the admin's custom domain
    (gated on ``user.signup_origin``).
    """
    if not settings.BRANDING_ENABLED:
        return APIResponse(
            data={"branding": None, "signup_origin": user.signup_origin}
        )
    if user.assigned_admin_id is None:
        return APIResponse(
            data={"branding": None, "signup_origin": user.signup_origin}
        )
    admin = await User.get(user.assigned_admin_id)
    if (
        admin is None
        or admin.role != UserRole.ADMIN
        or admin.status != UserStatus.ACTIVE
    ):
        return APIResponse(
            data={"branding": None, "signup_origin": user.signup_origin}
        )
    return APIResponse(
        data={
            "branding": branding_service.to_branding_payload(admin),
            "signup_origin": user.signup_origin,
        }
    )


def _user_to_me(user) -> UserMeOut:
    return UserMeOut(
        id=str(user.id),
        user_code=user.user_code,
        email=user.email,
        mobile=user.mobile,
        full_name=user.full_name,
        photo_url=user.photo_url,
        role=user.role,
        status=user.status,
        account_type=user.account_type,
        is_demo=user.is_demo,
        parent_id=str(user.parent_id) if user.parent_id else None,
        kyc=user.kyc,
        permissions=user.permissions,
        trading_hours=user.trading_hours,
        risk=user.risk,
        communication=user.communication,
        two_fa_enabled=user.two_fa_enabled,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@router.get("/terms", response_model=APIResponse[dict])
async def get_terms(user: CurrentUser):
    """The Terms & Conditions this user should see — their owning admin's text
    (admins set it in Platform Settings), falling back to the super-admin's
    platform default. Empty string when nobody has set one yet."""
    admin = None
    if getattr(user, "assigned_admin_id", None):
        admin = await User.get(user.assigned_admin_id)
    terms = (getattr(admin, "terms_and_conditions", None) or "").strip() if admin else ""
    if not terms:
        sa = await User.find_one(User.role == UserRole.SUPER_ADMIN)
        terms = (getattr(sa, "terms_and_conditions", None) or "").strip() if sa else ""
    return APIResponse(data={"terms": terms})
