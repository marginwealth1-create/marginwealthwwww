"""Web Push subscriptions — one row per browser/PWA install per user.

When a user / admin grants notification permission and the SW subscribes
to the browser's push service, we store the resulting endpoint here so
the backend can fire a server-sent push later (deposit submitted /
approved, withdrawal processed, admin Add/Deduct Fund, etc.) — even when
the PWA process has been killed or the phone is locked.

One subject can hold MULTIPLE subscriptions (phone + laptop + desktop),
hence no unique on subject_id. Endpoints ARE unique platform-wide
(browsers use the same push URL for the same install), so re-subscribing
on the same device updates the row in place via `endpoint`'s unique
index instead of stacking duplicates.
"""

from __future__ import annotations

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin


class PushSubjectType(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"


class PushKeys(BaseModel):
    """The cryptographic material the browser hands back at subscribe
    time. Required by RFC 8291; pywebpush passes them straight through
    to the push service so the message is encrypted end-to-end."""
    p256dh: str
    auth: str


class PushSubscription(TimestampMixin):
    # Whose subscription is this — a trader or an admin? Kept on every
    # row so the send service can pick the right query without a join.
    subject_type: PushSubjectType
    subject_id: PydanticObjectId
    # User-supplied label so the operator can identify which device the
    # subscription belongs to in a future "manage devices" UI ("iPhone
    # 14", "Office Chrome", …). Optional — most subscriptions arrive
    # unnamed and that's fine.
    label: str | None = None
    endpoint: str
    keys: PushKeys
    user_agent: str | None = None

    class Settings:
        name = "push_subscriptions"
        indexes = [
            IndexModel([("endpoint", ASCENDING)], unique=True),
            IndexModel([("subject_type", ASCENDING), ("subject_id", ASCENDING)]),
        ]
