"""Admin Web Push endpoints — store + remove browser push subscriptions.

The frontend (PwaRegister + AdminWsBridge) calls these once after the
operator grants notification permission. The subscription contains the
browser-vendor push endpoint and the cryptographic material needed to
encrypt the payload; we keep one row per (admin, device) so a single
admin can stay covered on phone + laptop + desktop.
"""

from __future__ import annotations

import logging

from beanie import PydanticObjectId
from fastapi import APIRouter, Header
from pydantic import BaseModel

from app.core.config import settings
from app.core.dependencies import CurrentAdmin
from app.models.push_subscription import PushKeys, PushSubjectType, PushSubscription
from app.schemas.common import APIResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/push", tags=["admin-push"])


class _SubscribeBody(BaseModel):
    endpoint: str
    keys: PushKeys
    label: str | None = None


@router.get("/vapid-key", response_model=APIResponse[dict])
async def vapid_key(admin: CurrentAdmin):
    """Return the application-server public key the SW needs to
    subscribe. Public — no secret material here."""
    return APIResponse(data={"public_key": settings.VAPID_PUBLIC_KEY})


@router.post("/subscribe", response_model=APIResponse[dict])
async def subscribe(
    body: _SubscribeBody,
    admin: CurrentAdmin,
    user_agent: str | None = Header(default=None, alias="User-Agent"),
):
    """Upsert a subscription. Browsers re-emit the same endpoint for
    the same install, so we key on endpoint to keep the table
    deduped — re-subscribing simply refreshes the row in place."""
    existing = await PushSubscription.find_one(PushSubscription.endpoint == body.endpoint)
    if existing is not None:
        existing.subject_type = PushSubjectType.ADMIN
        existing.subject_id = admin.id
        existing.keys = body.keys
        existing.label = body.label or existing.label
        existing.user_agent = user_agent or existing.user_agent
        await existing.save()
        return APIResponse(data={"id": str(existing.id), "created": False})
    sub = PushSubscription(
        subject_type=PushSubjectType.ADMIN,
        subject_id=admin.id,
        endpoint=body.endpoint,
        keys=body.keys,
        label=body.label,
        user_agent=user_agent,
    )
    await sub.insert()
    return APIResponse(data={"id": str(sub.id), "created": True})


class _UnsubBody(BaseModel):
    endpoint: str


@router.post("/unsubscribe", response_model=APIResponse[dict])
async def unsubscribe(body: _UnsubBody, admin: CurrentAdmin):
    sub = await PushSubscription.find_one(
        PushSubscription.endpoint == body.endpoint,
        PushSubscription.subject_id == admin.id,
    )
    if sub is None:
        return APIResponse(data={"ok": True, "found": False})
    await sub.delete()
    return APIResponse(data={"ok": True, "found": True})
