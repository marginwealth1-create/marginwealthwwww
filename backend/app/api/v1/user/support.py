"""Per-user support contact endpoint — resolves the caller's effective
WhatsApp + email via the admin hierarchy.

WhatsApp resolution walks UP from the calling user (CLIENT / DEALER /
MASTER / BROKER → ADMIN → SUPER_ADMIN) following `parent_id`, returning
the FIRST non-empty `User.support_whatsapp` it finds. This lets every
admin tier override their downstream pool's support contact without
admin needing to "broadcast" a new number — the user app's apk
transparently picks up whichever ancestor's value is set.

If nobody in the chain has a number set, falls back to the platform-
wide `platform.support_whatsapp` PlatformSetting row (managed by the
super-admin via the Platform Settings page). This guarantees the apk
always has SOMETHING to show when the platform is correctly seeded —
the original behaviour before the per-admin override was added.

Email continues to read straight from PlatformSetting — only WhatsApp
goes through the cascade, since email branding is meant to stay
platform-wide.

Authenticated: the cascade needs to know whose parent chain to walk.
Anonymous reads used to be allowed (it was a single global row); that
shape no longer works once cascade is in play.
"""

from __future__ import annotations

from beanie import PydanticObjectId
from fastapi import APIRouter

from app.core.dependencies import CurrentUser
from app.models._base import PermissionLevel
from app.models.platform_setting import PlatformSetting
from app.models.user import User, UserRole
from app.schemas.common import APIResponse
from app.utils.time_utils import now_utc

router = APIRouter(prefix="/support", tags=["user-support"])


async def _read_setting(key: str) -> str:
    row = await PlatformSetting.find_one(PlatformSetting.setting_key == key)
    if row is None or row.setting_value is None:
        return ""
    val = row.setting_value
    return str(val).strip()


def _broker_support_allowed(broker: User) -> bool:
    """True when a BROKER node is allowed to have its own support number
    honoured — i.e. the admin granted it the `support` permission (VIEW or
    EDIT). Fail-closed: a broker whose `broker_permissions` is missing/None
    is treated as NOT allowed, so its number is skipped and the cascade
    falls back to the parent admin."""
    bp = broker.broker_permissions
    if bp is None:
        return False
    level = getattr(bp, "support", PermissionLevel.OFF)
    return level != PermissionLevel.OFF


async def _resolve_whatsapp_for_user(user: User) -> str:
    """Walk UP the assignment chain from `user`, returning the first
    non-empty `support_whatsapp` found. Self is checked first so an
    admin/broker hitting the apk against their own login still sees
    their own number.

    Walk order at each hop (most specific first):
      1. `assigned_broker_id` — direct broker / sub-broker
      2. `assigned_admin_id`  — admin pool owner

    `parent_id` is NOT used: CLIENT rows are created without it most of
    the time, which left the cascade stuck at the first node and the
    number invisible to users.  The assignment fields are what every
    user actually carries.

    Capped at 8 hops as a defensive guard against a corrupted chain.
    """
    cur: User | None = user
    seen: set[PydanticObjectId] = set()
    hops = 0
    while cur is not None and hops < 8:
        if cur.id in seen:
            break
        seen.add(cur.id)
        val = (cur.support_whatsapp or "").strip()
        # Permission gate: a BROKER (incl. sub-broker) node's number is only
        # honoured when the admin has granted it the `support` permission.
        # When that permission is OFF the broker's number is skipped, so the
        # cascade falls through to the parent admin's number automatically —
        # exactly the "admin turns support off → clients see admin's number"
        # flow. Admin / super-admin nodes are never gated (their number is the
        # intended fallback). `_broker_support_allowed` treats a broker with no
        # permissions object as NOT allowed (fail-closed).
        if val and (cur.role != UserRole.BROKER or _broker_support_allowed(cur)):
            return val
        next_id = cur.assigned_broker_id or cur.assigned_admin_id
        if next_id is None or next_id in seen:
            break
        cur = await User.get(next_id)
        hops += 1
    return ""


async def _resolve_terms_for_user(user: User) -> tuple[str, bool]:
    """Walk the assignment chain to find the FIRST admin-tier ancestor
    whose `terms_enabled=True` and `terms_text` is non-empty. Returns
    `(text, enabled)`. If nothing in the chain has it enabled, returns
    ``("", False)`` and the user's app skips the T&C modal entirely.
    """
    cur: User | None = user
    seen: set[PydanticObjectId] = set()
    hops = 0
    while cur is not None and hops < 8:
        if cur.id in seen:
            break
        seen.add(cur.id)
        if bool(cur.terms_enabled) and (cur.terms_text or "").strip():
            return (cur.terms_text or "").strip(), True
        next_id = cur.assigned_broker_id or cur.assigned_admin_id
        if next_id is None or next_id in seen:
            break
        cur = await User.get(next_id)
        hops += 1
    return "", False


async def _resolve_ticker_for_user(user: User) -> list[str]:
    """Walk the assignment chain and return the FIRST non-empty
    `ticker_messages` list — the closest admin-tier ancestor's home-page
    ticker. Same cascade + 8-hop cap as the WhatsApp resolver."""
    cur: User | None = user
    seen: set[PydanticObjectId] = set()
    hops = 0
    while cur is not None and hops < 8:
        if cur.id in seen:
            break
        seen.add(cur.id)
        msgs = [m.strip() for m in (cur.ticker_messages or []) if m and m.strip()]
        if msgs:
            return msgs
        next_id = cur.assigned_broker_id or cur.assigned_admin_id
        if next_id is None or next_id in seen:
            break
        cur = await User.get(next_id)
        hops += 1
    return []


@router.get("/ticker", response_model=APIResponse[dict])
async def get_ticker_for_user(user: CurrentUser):
    """Home-page ticker lines for the calling user (their broker/admin's).
    Empty list ⇒ the app renders no ticker."""
    return APIResponse(data={"messages": await _resolve_ticker_for_user(user)})


@router.get("/terms", response_model=APIResponse[dict])
async def get_terms_for_user(user: CurrentUser):
    """Effective T&C for the calling user — text + enabled flag from
    the closest admin-tier ancestor that has set them. ``needs_accept``
    tells the app whether to show the modal (enabled AND user hasn't
    accepted yet OR admin reset acceptance after a text update)."""
    text, enabled = await _resolve_terms_for_user(user)
    needs_accept = bool(enabled and not user.terms_accepted_at)
    return APIResponse(
        data={
            "text": text,
            "enabled": enabled,
            "needs_accept": needs_accept,
            "accepted_at": user.terms_accepted_at,
        }
    )


@router.post("/terms/accept", response_model=APIResponse[dict])
async def accept_terms(user: CurrentUser):
    """Stamp `terms_accepted_at = now()` on the calling user so the
    modal stops appearing until admin updates the text."""
    user.terms_accepted_at = now_utc()
    await user.save()
    return APIResponse(data={"accepted_at": user.terms_accepted_at})


@router.get("/promo", response_model=APIResponse[dict])
async def get_promo_button(user: CurrentUser):
    """Platform-wide promo button config for the dashboard header. Super-admin
    sets these in Platform Settings. Returns ``enabled`` only when the switch is
    ON *and* a URL is present, so the client can render unconditionally."""
    row = await PlatformSetting.find_one(
        PlatformSetting.setting_key == "promo.button_enabled"
    )
    raw = row.setting_value if row is not None else False
    on = raw is True or str(raw).strip().lower() in {"true", "1", "yes", "on"}
    url = await _read_setting("promo.button_url")
    label = (await _read_setting("promo.button_label")) or "Offer"
    return APIResponse(
        data={"enabled": bool(on and url), "url": url, "label": label}
    )


@router.get("", response_model=APIResponse[dict])
async def get_support_contacts(user: CurrentUser):
    """Returns the effective WhatsApp + email for THIS user. WhatsApp
    walks the admin hierarchy; email is the global PlatformSetting row.
    Both default to empty strings when unset — the UI hides the
    corresponding action button in that case so the user never sees a
    half-broken "Contact support" affordance."""
    whatsapp = await _resolve_whatsapp_for_user(user)
    if not whatsapp:
        # Last-resort fallback: super-admin set a global WhatsApp via
        # the Platform Settings page (this was the only mechanism
        # before per-admin overrides existed). Keeps existing
        # deployments working even when no User row has been updated
        # yet through the new admin Support page.
        whatsapp = await _read_setting("platform.support_whatsapp")
    email = await _read_setting("platform.support_email")
    return APIResponse(
        data={
            "whatsapp": whatsapp,
            "email": email,
        }
    )
