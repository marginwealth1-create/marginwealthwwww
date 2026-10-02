"""Admin notification bell — list / unread-count / mark-read.

Backed by the `admin_notifications` collection (model:
`app.models.notification.AdminNotification`). Every row is scoped to a
single recipient admin, so all endpoints here filter by
``recipient_admin_id = current_admin.id`` — no extra ACL plumbing
needed beyond the standard `CurrentAdmin` dependency.
"""

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.dependencies import CurrentAdmin, require_perm, scoped_user_ids
from app.models.audit_log import AuditAction
from app.models.notification import (
    AdminNotification,
    Notification,
    NotificationLevel,
    NotificationType,
)
from app.schemas.common import APIResponse
from app.services.audit_service import log_event
from app.utils.time_utils import now_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["admin-notifications"])


def _serialise(n: AdminNotification) -> dict[str, Any]:
    return {
        "id": str(n.id),
        "event_type": n.event_type.value,
        "level": n.level.value,
        "title": n.title,
        "message": n.message,
        "link": n.link,
        "reference_type": n.reference_type,
        "reference_id": n.reference_id,
        "data": n.data,
        "is_read": n.is_read,
        "read_at": n.read_at,
        "created_at": n.created_at,
        "source_user_id": str(n.source_user_id),
    }


@router.get("", response_model=APIResponse[list])
async def list_notifications(
    admin: CurrentAdmin,
    only_unread: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
):
    """Bell-panel feed. Defaults to "everything in the last 50 rows"
    sorted newest-first; `?only_unread=true` narrows to PENDING items.
    """
    q: dict[str, Any] = {"recipient_admin_id": admin.id}
    if only_unread:
        q["is_read"] = False
    rows = (
        await AdminNotification.find(q)
        .sort("-created_at")
        .limit(limit)
        .to_list()
    )
    return APIResponse(data=[_serialise(n) for n in rows])


@router.get("/unread-count", response_model=APIResponse[dict])
async def unread_count(admin: CurrentAdmin):
    """O(1) badge counter for the bell icon. Hit every few seconds /
    on every WS `notification_created` event."""
    count = await AdminNotification.find(
        AdminNotification.recipient_admin_id == admin.id,
        AdminNotification.is_read == False,  # noqa: E712 — beanie equality
    ).count()
    return APIResponse(data={"count": int(count)})


@router.post("/{notification_id}/read", response_model=APIResponse[dict])
async def mark_read(notification_id: str, admin: CurrentAdmin):
    """Mark a single notification read. Only the recipient admin
    themselves can flip their own copy — a broker can't clear an event
    from the super-admin's bell."""
    try:
        oid = PydanticObjectId(notification_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Notification not found")
    n = await AdminNotification.get(oid)
    if n is None or n.recipient_admin_id != admin.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    if not n.is_read:
        n.is_read = True
        n.read_at = now_utc()
        await n.save()
    return APIResponse(data=_serialise(n))


@router.post("/mark-all-read", response_model=APIResponse[dict])
async def mark_all_read(admin: CurrentAdmin):
    """Bulk-flip every unread row for this admin to read in one
    Mongo update. Used by the "Mark all read" link in the bell panel."""
    coll = AdminNotification.get_motor_collection()
    res = await coll.update_many(
        {"recipient_admin_id": admin.id, "is_read": False},
        {"$set": {"is_read": True, "read_at": now_utc()}},
    )
    return APIResponse(data={"marked": int(res.modified_count)})


# ── Broadcast: admin → all their own users ───────────────────────────
# A composer in the admin panel lets any admin/broker push a one-off
# notification (title + message + optional link) to EVERY user in their
# own pool at once. Scoped via `scoped_user_ids` so a broker only reaches
# their subtree and an admin only their clients; super-admin reaches all.
class BroadcastBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1000)
    link: str | None = Field(default=None, max_length=500)
    level: NotificationLevel = NotificationLevel.INFO


@router.get("/broadcast/recipients", response_model=APIResponse[dict])
async def broadcast_recipient_count(
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "read")),
):
    """How many users the current actor's broadcast would reach — shown in
    the composer so the admin knows the blast radius before sending."""
    ids = await scoped_user_ids(admin)
    return APIResponse(data={"count": len(ids)})


@router.post("/broadcast", response_model=APIResponse[dict])
async def broadcast_notification(
    body: BroadcastBody,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "read")),
):
    """Insert one SYSTEM notification per user in the actor's pool + push a
    live `notification` WS event to each so it lands in-app instantly (falls
    back to the user's next fetch if the socket is closed)."""
    title = body.title.strip()
    message = body.message.strip()
    if not title or not message:
        raise HTTPException(status_code=400, detail="Title and message are required")
    link = (body.link or "").strip() or None

    recipients = await scoped_user_ids(admin)
    if not recipients:
        return APIResponse(data={"count": 0})

    data: dict[str, Any] = {"source": "admin_broadcast", "sender_id": str(admin.id)}
    if link:
        data["link"] = link

    docs = [
        Notification(
            user_id=uid,
            type=NotificationType.SYSTEM,
            level=body.level,
            title=title,
            message=message,
            data=data,
        )
        for uid in recipients
    ]
    await Notification.insert_many(docs)

    # Live push — best-effort; the DB rows above are the source of truth.
    try:
        from app.core.redis_client import publish_batch

        payload = {
            "type": "notification",
            "payload": {
                "title": title,
                "message": message,
                "link": link,
                "level": body.level.value,
            },
        }
        await publish_batch(
            [(f"user:{uid}:notification", payload) for uid in recipients]
        )
    except Exception:
        logger.exception("broadcast_ws_push_failed admin=%s", admin.id)

    # Web Push — wakes the phone/tray even when the PWA is force-stopped or
    # the socket above is closed. Best-effort; no-ops when VAPID is
    # unconfigured. Single bulk subscription query for the whole pool.
    try:
        from app.services import push_service

        await push_service.send_to_users(
            recipients,
            title=title,
            body=message,
            url=link or "/notifications",
            tag=f"mp-broadcast-{admin.id}",
        )
    except Exception:
        logger.exception("broadcast_webpush_failed admin=%s", admin.id)

    try:
        await log_event(
            action=AuditAction.CREATE,
            entity_type="Notification",
            actor_id=admin.id,
            metadata={"action": "broadcast", "count": len(recipients), "title": title, "has_link": bool(link)},
        )
    except Exception:
        logger.exception("broadcast_audit_failed")

    return APIResponse(data={"count": len(recipients)})
