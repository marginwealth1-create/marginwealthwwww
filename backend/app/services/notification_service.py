"""Admin notification fan-out — one source event → one row per admin
recipient up the user's tier chain.

The admin notification bell on the top-right of every admin /
super-admin / broker / sub-broker dashboard reads from
`admin_notifications` (model defined in `app/models/notification.py`).
Each row is per-(recipient_admin, event) so the bell query is a flat
`find({recipient_admin_id: me})` — no reverse-walking through the
user hierarchy at read time.

Public surface:

  create_for_admins(
    source_user_id,
    event_type, title, message,
    *, level=INFO, link=None,
    reference_type=None, reference_id=None, data=None,
  )
      Resolves the recipient set for a source user (every
      super-admin + the user's assigned admin + every broker in
      their ancestry) and inserts one AdminNotification row per
      recipient. Publishes a single `notification_created` admin
      event over Redis pub/sub so all attached admin browsers
      invalidate their bell query and refetch — the WS payload
      carries the recipient_admin_ids so each frontend can decide
      whether the refetch is theirs to act on.

      Best-effort: a Mongo / Redis hiccup on the notification
      side MUST NOT roll back the caller's primary write. Every
      branch in this module either swallows or logs+swallows
      exceptions.
"""

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId

from app.models.notification import (
    AdminNotification,
    AdminNotificationEventType,
    NotificationLevel,
)
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)


async def _resolve_recipients(
    source_user: User,
) -> list[PydanticObjectId]:
    """Walk the source user's tier chain and return the list of admin
    ids that should receive a notification when an event fires on this
    user.

    Recipients (deduplicated, order doesn't matter — each row is its
    own document):

      1. The user's OWNER admin ONLY — `source_user.assigned_admin_id`
         when set, otherwise every SUPER_ADMIN (the platform pool). So a
         super-admin sees ONLY their own pool's users, and one sub-admin
         never sees another sub-admin's users.
      2. Every broker id in `source_user.broker_ancestry` — root-first
         list up to the immediate parent (each broker / sub-broker sees
         its own subtree).
      3. `source_user.assigned_broker_id` if set and not already in
         the ancestry list — covers legacy rows whose ancestry hasn't
         been backfilled yet.

    Admin-role users (SUPER_ADMIN / ADMIN / BROKER) themselves never
    produce notifications about their own actions — the function
    accepts any User but the caller is expected to pass an end-user
    (CLIENT / DEALER / MASTER). Defensive: if the source is admin-
    tier we still resolve their chain anyway (e.g. an admin
    submitting a deposit on behalf of themselves — rare but possible).
    """
    recipients: set[PydanticObjectId] = set()

    # Tier ownership — each tier sees ONLY the users in its own pool:
    #   • user HAS an assigned sub-admin  → that admin owns it; super-admins
    #     do NOT get it (so one admin never sees another admin's users).
    #   • user has NO sub-admin (platform pool) → every super-admin owns it.
    # Operator-flagged: "ek admin ko 2 admin ka mat dikhao; super-admin ko
    # bhi sirf uske apne users ka dikhe."
    if source_user.assigned_admin_id is not None:
        recipients.add(source_user.assigned_admin_id)
    else:
        try:
            supers = await User.find(User.role == UserRole.SUPER_ADMIN).to_list()
            for s in supers:
                if s.id is not None:
                    recipients.add(s.id)
        except Exception:  # pragma: no cover
            logger.exception("notif_resolve_super_admins_failed")

    # Brokers / sub-brokers in the user's chain get their subtree's events.
    for broker_id in source_user.broker_ancestry or []:
        recipients.add(broker_id)

    if (
        source_user.assigned_broker_id is not None
        and source_user.assigned_broker_id not in recipients
    ):
        recipients.add(source_user.assigned_broker_id)

    # An end-user shouldn't be their own admin notification recipient —
    # they have the user-side `Notification` model for self-facing
    # alerts. Strip the source id if it accidentally landed in the set.
    if source_user.id is not None:
        recipients.discard(source_user.id)

    return list(recipients)


async def create_for_admins(
    source_user_id: str | PydanticObjectId,
    event_type: AdminNotificationEventType,
    title: str,
    message: str,
    *,
    level: NotificationLevel = NotificationLevel.INFO,
    link: str | None = None,
    reference_type: str | None = None,
    reference_id: str | None = None,
    data: dict[str, Any] | None = None,
) -> int:
    """Fan an event out into AdminNotification rows for every admin in
    the source user's tier chain. Returns the number of rows inserted.

    All branches swallow exceptions so a notification failure never
    rolls back the calling endpoint's primary write (deposit save,
    KYC submit, etc.).
    """
    try:
        uid = PydanticObjectId(str(source_user_id))
    except Exception:
        logger.warning("notif_invalid_source_user_id %s", source_user_id)
        return 0

    try:
        source = await User.get(uid)
    except Exception:  # pragma: no cover
        logger.exception("notif_source_user_lookup_failed user=%s", uid)
        return 0
    if source is None:
        logger.warning("notif_source_user_not_found user=%s", uid)
        return 0

    recipients = await _resolve_recipients(source)
    if not recipients:
        return 0

    # Enrich `data` with a few common fields the frontend rendering
    # leans on (user_name, user_code) so the bell row can show the
    # full identity without a separate lookup per render.
    enriched: dict[str, Any] = dict(data or {})
    enriched.setdefault("user_name", source.full_name)
    enriched.setdefault("user_code", source.user_code)

    rows: list[AdminNotification] = []
    for recipient_id in recipients:
        rows.append(
            AdminNotification(
                recipient_admin_id=recipient_id,
                source_user_id=uid,
                event_type=event_type,
                level=level,
                title=title,
                message=message,
                link=link,
                reference_type=reference_type,
                reference_id=reference_id,
                data=enriched,
            )
        )

    try:
        await AdminNotification.insert_many(rows)
    except Exception:  # pragma: no cover
        logger.exception(
            "notif_insert_many_failed user=%s event=%s count=%d",
            uid,
            event_type.value,
            len(rows),
        )
        return 0

    # Fan-out so every attached admin WS refreshes its bell. Payload
    # carries the recipient list so the frontend can decide quickly
    # whether it should refetch (the WS channel is shared across all
    # admins, but each browser only cares about events touching them).
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "notification_created",
            {
                "event_type": event_type.value,
                "source_user_id": str(uid),
                "recipient_admin_ids": [str(r) for r in recipients],
                "title": title,
                "level": level.value,
            },
        )
    except Exception:  # pragma: no cover
        logger.exception("notif_publish_event_failed")

    return len(rows)
