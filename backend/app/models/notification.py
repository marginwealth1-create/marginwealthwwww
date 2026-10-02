"""In-app notifications — TTL 90 days after creation."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum

from beanie import PydanticObjectId
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin
from app.utils.time_utils import now_utc


class NotificationType(StrEnum):
    ORDER = "ORDER"
    TRADE = "TRADE"
    POSITION = "POSITION"
    WALLET = "WALLET"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    PRICE_ALERT = "PRICE_ALERT"
    SYSTEM = "SYSTEM"
    MARGIN = "MARGIN"
    SQUAREOFF = "SQUAREOFF"
    SECURITY = "SECURITY"


class NotificationLevel(StrEnum):
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    DANGER = "DANGER"


class Notification(TimestampMixin):
    user_id: PydanticObjectId
    type: NotificationType
    level: NotificationLevel = NotificationLevel.INFO
    title: str
    message: str
    is_read: bool = False
    read_at: datetime | None = None
    data: dict = Field(default_factory=dict)

    expires_at: datetime = Field(default_factory=lambda: now_utc() + timedelta(days=90))

    class Settings:
        name = "notifications"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("is_read", ASCENDING),
                    ("created_at", DESCENDING),
                ]
            ),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("type", ASCENDING)]),
            IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0),
        ]


# ── Admin notifications ──────────────────────────────────────────────
class AdminNotificationEventType(StrEnum):
    """Inbox event types surfaced in the admin notification bell.

    Distinct from the user-facing `NotificationType` enum so the two
    inboxes can evolve independently. The values map 1-to-1 to what
    triggers them on the user side (deposit submission, withdrawal
    submission, KYC submission, settlement request creation, etc.).
    """

    DEPOSIT_SUBMITTED = "DEPOSIT_SUBMITTED"
    WITHDRAWAL_SUBMITTED = "WITHDRAWAL_SUBMITTED"
    KYC_SUBMITTED = "KYC_SUBMITTED"
    SETTLEMENT_REQUESTED = "SETTLEMENT_REQUESTED"
    USER_REGISTERED = "USER_REGISTERED"


class AdminNotification(TimestampMixin):
    """One row per (admin recipient × event). A single user-side
    deposit fan-outs into N AdminNotification rows — one for the
    super-admin, one for the user's assigned admin (if any), and one
    for every broker in the user's ancestry. That way the admin's bell
    panel is a simple `find({recipient_admin_id: me})` query without
    any reverse-walking through the user hierarchy at read time.

    Read-state and TTL (60 d auto-expire) are per-row, so an admin
    marking their own copy read does NOT clear the same event for
    the super-admin or another broker in the chain.
    """

    # Which admin user sees this row in their bell. NOT the source user.
    recipient_admin_id: PydanticObjectId

    # Which end-user triggered the event (the deposit submitter, the
    # KYC submitter, the user whose wallet went negative, etc.).
    source_user_id: PydanticObjectId

    event_type: AdminNotificationEventType
    level: NotificationLevel = NotificationLevel.INFO
    title: str
    message: str

    # Deep-link the bell row should open when clicked. Examples:
    #   "/payments?tab=deposits"
    #   "/payments?tab=settlements"
    #   "/kyc"
    #   "/users/{user_id}"
    link: str | None = None

    # Foreign-key to the originating record (DepositRequest /
    # WithdrawalRequest / KycSubmission / SettlementRequest id) so the
    # row can be cross-referenced from the audit log.
    reference_type: str | None = None
    reference_id: str | None = None

    # Snapshot of useful context the admin sees at-a-glance — user
    # display name, amount, etc. Avoids an N+1 lookup on every bell
    # refresh.
    data: dict = Field(default_factory=dict)

    is_read: bool = False
    read_at: datetime | None = None

    # 60 d TTL on the read state — admin notifications are
    # operational, not archival. The dedicated section pages
    # (/payments, /kyc) remain the system of record for everything
    # older than that.
    expires_at: datetime = Field(default_factory=lambda: now_utc() + timedelta(days=60))

    class Settings:
        name = "admin_notifications"
        indexes = [
            IndexModel(
                [
                    ("recipient_admin_id", ASCENDING),
                    ("is_read", ASCENDING),
                    ("created_at", DESCENDING),
                ]
            ),
            IndexModel(
                [("recipient_admin_id", ASCENDING), ("created_at", DESCENDING)]
            ),
            IndexModel([("event_type", ASCENDING)]),
            IndexModel(
                [("reference_type", ASCENDING), ("reference_id", ASCENDING)]
            ),
            IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0),
        ]
