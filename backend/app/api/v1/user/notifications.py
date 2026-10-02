"""User notifications."""

from __future__ import annotations

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException

from app.core.dependencies import CurrentUser
from app.models.notification import Notification
from app.schemas.common import APIResponse
from app.utils.time_utils import now_utc

router = APIRouter(prefix="/notifications", tags=["user-notifications"])


@router.get("", response_model=APIResponse[list])
async def list_notifications(user: CurrentUser, only_unread: bool = False, limit: int = 100):
    q = {"user_id": user.id}
    if only_unread:
        q["is_read"] = False
    rows = await Notification.find(q).sort("-created_at").limit(limit).to_list()
    return APIResponse(
        data=[
            {
                "id": str(n.id),
                "type": n.type.value,
                "level": n.level.value,
                "title": n.title,
                "message": n.message,
                "is_read": n.is_read,
                "data": n.data,
                "created_at": n.created_at,
            }
            for n in rows
        ]
    )


@router.post("/{notification_id}/read", response_model=APIResponse[dict])
async def mark_read(notification_id: str, user: CurrentUser):
    n = await Notification.get(PydanticObjectId(notification_id))
    if n is None or n.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    n.is_read = True
    n.read_at = now_utc()
    await n.save()
    return APIResponse(data={"ok": True})


@router.post("/mark-all-read", response_model=APIResponse[dict])
async def mark_all_read(user: CurrentUser):
    await Notification.find(
        Notification.user_id == user.id, Notification.is_read == False  # noqa: E712
    ).update_many({"$set": {"is_read": True, "read_at": now_utc()}})
    return APIResponse(data={"ok": True})


@router.get("/unread-count", response_model=APIResponse[dict])
async def unread_count(user: CurrentUser):
    c = await Notification.find(
        Notification.user_id == user.id, Notification.is_read == False  # noqa: E712
    ).count()
    return APIResponse(data={"count": c})
