"""Per-user support contact endpoint — resolves the caller's effective
WhatsApp + email via the admin hierarchy.

WhatsApp resolution walks UP from the calling user (CLIENT / DEALER /
MASTER / BROKER → parent BROKERs → ADMIN → SUPER_ADMIN), returning
the FIRST non-empty `User.support_whatsapp` it finds. This lets every
admin tier override their downstream pool's support contact without
admin needing to "broadcast" a new number — the user app's apk
transparently picks up whichever ancestor's value is set.

If nobody in the chain has a number set, falls back to the platform-
wide `platform.support_whatsapp` PlatformSetting row (managed by the
super-admin). This guarantees the apk always has SOMETHING to show when
the platform is correctly seeded — the original behaviour before the
per-admin override was added.

Email continues to read straight from PlatformSetting — only WhatsApp
goes through the cascade, since email branding is meant to stay
platform-wide.

Authenticated: the cascade needs to know whose parent chain to walk.
Anonymous reads used to be allowed (it was a single global row); that
shape no longer works once cascade is in play.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

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


async def _iter_ancestor_chain(user: User) -> AsyncIterator[User]:
    """Yields `user` followed by their admin-tier ancestors from most-specific
    to least-specific:
      1. `user` (self)
      2. Immediate `assigned_broker_id` and its `assigned_broker_id` chain
      3. Any remaining brokers in `reversed(user.broker_ancestry)` (closest first)
      4. `assigned_admin_id` (from `user` or any visited broker)
      5. `SUPER_ADMIN` user(s) as the top-of-tree fallback
    """
    seen: set[PydanticObjectId] = set()
    discovered_admin_ids: list[PydanticObjectId] = []
    if user.assigned_admin_id:
        discovered_admin_ids.append(user.assigned_admin_id)

    # 1. Self
    if user.id is not None:
        seen.add(user.id)
    yield user

    # 2. Walk immediate broker chain via assigned_broker_id (capped at 8 hops)
    nxt_broker_id = user.assigned_broker_id
    hops = 0
    while nxt_broker_id is not None and nxt_broker_id not in seen and hops < 8:
        hops += 1
        broker_doc = await User.get(nxt_broker_id)
        if broker_doc is None:
            break
        seen.add(nxt_broker_id)
        if broker_doc.assigned_admin_id and broker_doc.assigned_admin_id not in discovered_admin_ids:
            discovered_admin_ids.append(broker_doc.assigned_admin_id)
        yield broker_doc
        nxt_broker_id = broker_doc.assigned_broker_id

    # 3. Walk broker_ancestry in reverse (tip-to-root) for any parent brokers not yet visited
    for anc_id in reversed(list(user.broker_ancestry or [])):
        if anc_id is None or anc_id in seen:
            continue
        anc_doc = await User.get(anc_id)
        if anc_doc is None:
            continue
        seen.add(anc_id)
        if anc_doc.assigned_admin_id and anc_doc.assigned_admin_id not in discovered_admin_ids:
            discovered_admin_ids.append(anc_doc.assigned_admin_id)
        yield anc_doc

    # 4. Walk discovered assigned_admin_id(s)
    for admin_id in discovered_admin_ids:
        if admin_id in seen:
            continue
        admin_doc = await User.get(admin_id)
        if admin_doc is None:
            continue
        seen.add(admin_id)
        yield admin_doc

    # 5. Top-level SUPER_ADMIN fallback — users with no assigned_admin_id (or
    # whose sub-admin/broker chain left support blank) inherit the super-admin's
    # configured value from /admin/support.
    super_admins = await User.find(User.role == UserRole.SUPER_ADMIN).to_list()
    for sa in super_admins:
        if sa.id is not None and sa.id not in seen:
            seen.add(sa.id)
            yield sa


async def _resolve_whatsapp_for_user(user: User) -> str:
    """Walk UP the assignment chain from `user`, returning the first
    non-empty `support_whatsapp` found. Self is checked first so an
    admin/broker hitting the apk against their own login still sees
    their own number.
    """
    async for cur in _iter_ancestor_chain(user):
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
    return ""


async def _resolve_terms_for_user(user: User) -> tuple[str, bool]:
    """Walk the assignment chain to find the FIRST admin-tier ancestor
    whose `terms_enabled=True` and `terms_text` is non-empty. Returns
    `(text, enabled)`. If nothing in the chain has it enabled, returns
    ``("", False)`` and the user's app skips the T&C modal entirely.
    """
    async for cur in _iter_ancestor_chain(user):
        if bool(cur.terms_enabled) and (cur.terms_text or "").strip():
            return (cur.terms_text or "").strip(), True
    return "", False


async def _resolve_ticker_for_user(user: User) -> list[str]:
    """Walk the assignment chain and return the FIRST non-empty
    `ticker_messages` list — the closest admin-tier ancestor's home-page
    ticker."""
    async for cur in _iter_ancestor_chain(user):
        msgs = [m.strip() for m in (cur.ticker_messages or []) if m and m.strip()]
        if msgs:
            return msgs
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


@router.get("/market-hours", response_model=APIResponse[dict])
async def get_market_hours_for_user():
    """Return the active market-wise trading hours (IST) configured by admin."""
    from app.services.market_hours_service import get_market_hours

    return APIResponse(data={"hours": await get_market_hours()})


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
