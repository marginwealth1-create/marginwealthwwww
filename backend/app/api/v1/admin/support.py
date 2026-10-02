"""Per-admin support WhatsApp number — read + update for the calling admin.

Each admin-tier user (SUPER_ADMIN / ADMIN / BROKER, including nested
sub-brokers) can configure their OWN WhatsApp number that will be shown
to their downstream users on the apk's "Add funds → Support" button
(and any other Contact-support affordance). Resolution at the user end
walks up the parent_id chain, so a sub-broker who hasn't set their own
number inherits their parent broker's number; a client whose entire
broker chain is blank falls back to the platform-wide
`platform.support_whatsapp` PlatformSetting row.

Endpoints:
    GET  /admin/support — current admin's own number (empty string when unset)
    PUT  /admin/support — { "whatsapp": "<digits or +country digits>" }

Audit-logged as SETTING_CHANGE on the User entity so the existing audit
filter UI surfaces these rows alongside other per-user setting tweaks.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.dependencies import CurrentAdmin
from app.models._base import PermissionLevel
from app.models.audit_log import AuditAction
from app.models.user import User, UserRole
from app.schemas.common import APIResponse
from app.services.audit_service import log_event

router = APIRouter(prefix="/support", tags=["admin-support"])


class SupportPayload(BaseModel):
    whatsapp: str = Field(default="", max_length=32)


def _support_can_edit(admin: User) -> bool:
    """Whether this admin-tier actor may set their OWN support number.
    Admin / super-admin always can. A BROKER (incl. sub-broker) can only
    when granted the `support` permission at EDIT — this is the gate the
    operator wants: a broker without the grant can't override their
    clients' support number (they inherit the parent admin's instead)."""
    if admin.role != UserRole.BROKER:
        return True
    bp = admin.broker_permissions
    if bp is None:
        return False
    return getattr(bp, "support", PermissionLevel.OFF) == PermissionLevel.EDIT


@router.get("", response_model=APIResponse[dict])
async def get_my_support(admin: CurrentAdmin):
    """Returns the calling admin's stored WhatsApp number. Empty string
    when unset — the UI then renders the input as a placeholder and
    explains the inheritance fallback so the admin knows what the
    user actually sees today. `can_edit` tells the UI whether to enable
    the form (brokers need the `support` permission)."""
    return APIResponse(
        data={
            "whatsapp": (admin.support_whatsapp or "").strip(),
            "role": admin.role.value,
            "can_edit": _support_can_edit(admin),
        }
    )


@router.put("", response_model=APIResponse[dict])
async def set_my_support(
    payload: SupportPayload,
    admin: CurrentAdmin,
    request: Request,
):
    """Updates the calling admin's own support WhatsApp number. Each
    admin tier writes only its own row — there's no "set someone
    else's number" endpoint, by design: a super-admin manages the
    platform-wide fallback via PlatformSetting; brokers manage their
    own pool's number here.

    The value is stored verbatim (spacing, leading `+`, dashes all
    preserved) so the admin's chosen format round-trips intact when
    re-displayed in the form. Length cap of 32 chars accommodates the
    longest realistic shape `+CC XXX XXX-XXXX`.
    """
    # Broker gate: only a broker granted `support` at EDIT may set its own
    # number. Without the grant the write is rejected so the broker can't
    # override what its clients see — they inherit the parent admin's number.
    if not _support_can_edit(admin):
        raise HTTPException(
            status_code=403,
            detail="Your admin hasn't enabled the Support permission for you.",
        )

    new_value = (payload.whatsapp or "").strip()
    old_value = (admin.support_whatsapp or "").strip()
    if new_value == old_value:
        return APIResponse(
            data={
                "whatsapp": new_value,
                "role": admin.role.value,
                "can_edit": True,
            }
        )

    # Refuse obviously-broken numbers. The apk's `buildWhatsappUrl`
    # silently hides the button when digits.length < 8, so blocking
    # the same shape at write time avoids storing a value the user app
    # can never display. Empty string is fine — that's the "clear"
    # action which restores inheritance from the parent admin.
    if new_value:
        digits = "".join(ch for ch in new_value if ch.isdigit())
        if len(digits) < 8:
            raise HTTPException(
                status_code=400,
                detail="WhatsApp number is too short. Include the country code (e.g. +91…).",
            )

    user_doc = await User.get(admin.id)
    if user_doc is None:
        raise HTTPException(status_code=404, detail="Admin user not found")
    user_doc.support_whatsapp = new_value or None
    await user_doc.save()

    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="User",
        entity_id=admin.id,
        actor_id=admin.id,
        target_user_id=admin.id,
        old_values={"support_whatsapp": old_value},
        new_values={"support_whatsapp": new_value},
        metadata={"field": "support_whatsapp"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    return APIResponse(
        data={"whatsapp": new_value, "role": admin.role.value, "can_edit": True}
    )


# ── Home-page notification ticker (per-admin, cascades to clients) ──

class TickerPayload(BaseModel):
    messages: list[str] = Field(default_factory=list)


@router.get("/ticker", response_model=APIResponse[dict])
async def get_my_ticker(admin: CurrentAdmin):
    """This admin's home-page ticker lines. Empty ⇒ the ticker is hidden for
    their users (unless a parent admin has one — the app resolves that)."""
    return APIResponse(
        data={"messages": list(admin.ticker_messages or []), "role": admin.role.value}
    )


@router.put("/ticker", response_model=APIResponse[dict])
async def set_my_ticker(payload: TickerPayload, admin: CurrentAdmin):
    """Replace this admin's ticker lines. Frontend sends the full list (add /
    remove happen client-side). Blanks dropped; capped at 20 lines × 300 chars
    so a runaway paste can't bloat the marquee. Empty list clears the ticker."""
    cleaned = [m.strip()[:300] for m in (payload.messages or []) if m and m.strip()][:20]
    user_doc = await User.get(admin.id)
    if user_doc is None:
        raise HTTPException(status_code=404, detail="Admin user not found")
    user_doc.ticker_messages = cleaned
    await user_doc.save()
    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="User",
        entity_id=admin.id,
        actor_id=admin.id,
        target_user_id=admin.id,
        new_values={"ticker_count": len(cleaned)},
    )
    return APIResponse(
        data={"messages": cleaned},
        message=(f"{len(cleaned)} ticker line(s) live." if cleaned else "Ticker cleared."),
    )


# ── Terms & Conditions (per-admin, cascades to downstream clients) ──

class TermsPayload(BaseModel):
    text: str = Field(default="", max_length=20000)
    enabled: bool = False


@router.get("/terms", response_model=APIResponse[dict])
async def get_my_terms(admin: CurrentAdmin):
    return APIResponse(
        data={
            "text": admin.terms_text or "",
            "enabled": bool(admin.terms_enabled),
            "role": admin.role.value,
        }
    )


@router.put("/terms", response_model=APIResponse[dict])
async def set_my_terms(
    payload: TermsPayload,
    admin: CurrentAdmin,
    request: Request,
):
    """Update T&C text + enabled toggle. Enabling with empty text is
    rejected. Changing text resets downstream clients' accept marker
    so they re-accept on next visit (scoped by admin tier)."""
    new_text = (payload.text or "").strip()
    new_enabled = bool(payload.enabled)
    if new_enabled and not new_text:
        raise HTTPException(
            status_code=400,
            detail="Cannot enable T&C with empty text — add content first or toggle off.",
        )

    user_doc = await User.get(admin.id)
    if user_doc is None:
        raise HTTPException(status_code=404, detail="Admin user not found")

    old_text = user_doc.terms_text or ""
    old_enabled = bool(user_doc.terms_enabled)
    user_doc.terms_text = new_text or None
    user_doc.terms_enabled = new_enabled
    await user_doc.save()

    if new_text != old_text and new_text:
        # Reset every downstream CLIENT's terms_accepted_at so they
        # re-confirm the new version. Pools can be 1000s of rows, and
        # the update_many can take 5-30s on a cold MongoDB connection.
        # Fire-and-forget via asyncio.create_task so the API response
        # comes back immediately — without this the request hung long
        # enough for nginx to 504 the FIRST save, which surfaced in
        # the browser as a CORS error (504 has no Access-Control-
        # Allow-Origin header). Re-acceptance is eventually-consistent
        # — clients will see the new modal on their next page load
        # whenever the background reset finishes (typically a second
        # or two later).
        import asyncio

        from app.models._base import UserRole as _UR

        async def _cascade_reset() -> None:
            try:
                coll = User.get_motor_collection()
                if admin.role.value == _UR.BROKER.value:
                    await coll.update_many(
                        {"broker_ancestry": admin.id, "role": _UR.CLIENT.value},
                        {"$set": {"terms_accepted_at": None}},
                    )
                elif admin.role.value == _UR.ADMIN.value:
                    await coll.update_many(
                        {"assigned_admin_id": admin.id, "role": _UR.CLIENT.value},
                        {"$set": {"terms_accepted_at": None}},
                    )
                else:
                    await coll.update_many(
                        {"role": _UR.CLIENT.value},
                        {"$set": {"terms_accepted_at": None}},
                    )
            except Exception:  # noqa: BLE001
                import logging

                logging.getLogger(__name__).exception(
                    "terms_cascade_reset_failed admin_id=%s", admin.id
                )

        asyncio.create_task(_cascade_reset())

    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="User",
        entity_id=admin.id,
        actor_id=admin.id,
        target_user_id=admin.id,
        old_values={"terms_enabled": old_enabled, "terms_text_len": len(old_text)},
        new_values={"terms_enabled": new_enabled, "terms_text_len": len(new_text)},
        metadata={"field": "terms"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    return APIResponse(
        data={"text": new_text, "enabled": new_enabled, "role": admin.role.value}
    )
