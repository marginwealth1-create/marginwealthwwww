"""Web Push send service — fires browser-vendor push messages so
operators get the deposit/withdrawal toast in the system tray even
when the PWA is force-stopped or the phone is locked.

Configuration:
    settings.VAPID_PUBLIC_KEY  — base64-encoded application-server key
    settings.VAPID_PRIVATE_KEY — matching private key (kept in env)
    settings.VAPID_SUBJECT     — mailto: contact (RFC 8292)

Generate the pair ONCE per deployment via:
    python -m scripts.generate_vapid_keys
The public key is also handed to the frontend (see /push/vapid-key
endpoint below) so the SW can subscribe to the same key pair.

Failure policy: every send is wrapped in try/except. A push hiccup
must NEVER block the calling request (deposit submit, wallet adjust,
…) or roll back a Mongo write. Failures log + swallow. A 404/410
response from the push service means the subscription is dead — we
delete the row so it stops cluttering the fan-out loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from beanie import PydanticObjectId

from app.core.config import settings
from app.models.push_subscription import PushSubjectType, PushSubscription

logger = logging.getLogger(__name__)


def _push_enabled() -> bool:
    """Return True only when both halves of the VAPID pair are present.
    In dev / on a fresh server the keys are blank — push calls become
    silent no-ops so missing configuration doesn't blow up requests."""
    return bool(settings.VAPID_PUBLIC_KEY) and bool(
        settings.VAPID_PRIVATE_KEY.get_secret_value()
    )


def _vapid_claims() -> dict[str, str]:
    return {"sub": settings.VAPID_SUBJECT}


def _send_one_sync(sub: PushSubscription, payload: dict[str, Any]) -> tuple[bool, int | None]:
    """Synchronous push call (pywebpush is sync only). Returns
    (ok, status_code_if_known). On 404/410 the subscription is dead
    and the caller deletes it. On other errors we just log."""
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("pywebpush_not_installed — skipping push send")
        return (False, None)
    try:
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.keys.p256dh, "auth": sub.keys.auth},
            },
            data=json.dumps(payload),
            vapid_private_key=settings.VAPID_PRIVATE_KEY.get_secret_value(),
            vapid_claims=_vapid_claims(),
            ttl=60 * 60 * 24,  # 24h — operator news is stale after that
        )
        return (True, 200)
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        # 404 Not Found / 410 Gone = browser pushed away the subscription.
        # Anything else is transient (rate limit, DNS hiccup, push
        # service down) and gets retried on the next event.
        if status in (404, 410):
            return (False, status)
        logger.warning("webpush_failed status=%s endpoint=%s", status, sub.endpoint[:80])
        return (False, status)
    except Exception:
        logger.exception("webpush_unexpected_error")
        return (False, None)


async def _send_one(sub: PushSubscription, payload: dict[str, Any]) -> None:
    """Run the sync webpush call in a thread so we don't block the
    asyncio loop. Delete the row if the browser says the endpoint is
    gone (404/410)."""
    ok, status = await asyncio.to_thread(_send_one_sync, sub, payload)
    if not ok and status in (404, 410):
        try:
            await sub.delete()
            logger.info("push_subscription_pruned endpoint=%s status=%s", sub.endpoint[:80], status)
        except Exception:
            pass


async def _fan_out(subs: list[PushSubscription], payload: dict[str, Any]) -> None:
    """Send to every subscription concurrently. Each task is independent;
    one bad subscription never starves the others."""
    if not subs:
        return
    if not _push_enabled():
        logger.debug("push_send_skipped (no VAPID configured) count=%d", len(subs))
        return
    await asyncio.gather(*[_send_one(s, payload) for s in subs], return_exceptions=True)


# ── Public send helpers ──────────────────────────────────────────────


async def _compute_recipient_admin_ids(
    source_user_id: PydanticObjectId | str,
) -> list[PydanticObjectId]:
    """Return the chain of admin/broker IDs that OWN this user — every
    operator who should hear about the user's deposit / withdrawal /
    wallet move. NO platform-wide broadcast.

    Rules (operator request: "ek admin ka dusre admin ko nahi jaye"):
      - The user's `assigned_admin_id`, if set.
      - Every broker in `broker_ancestry` (covers the broker who owns
        the user + every sub-broker / parent broker up the chain).
      - SUPER_ADMIN ids ONLY when the user is a platform-direct user
        (no assigned_admin_id, no broker_ancestry). That covers super-
        admin's own pool while keeping super-admin OUT of the loop for
        users that already belong to an admin or broker tree.

    Failures bubble up as an empty list — the push then no-ops, same
    as having no subscribers. We never want a bad lookup here to
    accidentally broadcast platform-wide.
    """
    from app.models.user import User, UserRole

    try:
        uid = (
            PydanticObjectId(source_user_id)
            if isinstance(source_user_id, str)
            else source_user_id
        )
    except Exception:
        return []
    user = await User.get(uid)
    if user is None:
        return []

    recipients: set[PydanticObjectId] = set()
    if user.assigned_admin_id is not None:
        recipients.add(user.assigned_admin_id)
    for bid in user.broker_ancestry or []:
        recipients.add(bid)
    if not recipients:
        # Platform-direct user — bring super-admins in.
        coll = User.get_motor_collection()
        async for doc in coll.find(
            {"role": UserRole.SUPER_ADMIN.value}, {"_id": 1}
        ):
            recipients.add(doc["_id"])
    return list(recipients)


async def send_to_user_owners(
    source_user_id: PydanticObjectId | str,
    *,
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
) -> list[PydanticObjectId]:
    """Scope-aware fan-out: only push to the admins / brokers who
    actually own the source user. Returns the resolved recipient ID
    list so the caller can include it in the parallel admin:events
    publish (so the in-page WS toast can filter the same way).
    """
    recipients = await _compute_recipient_admin_ids(source_user_id)
    if not recipients:
        return []
    subs = await PushSubscription.find(
        PushSubscription.subject_type == PushSubjectType.ADMIN,
        {"subject_id": {"$in": recipients}},
    ).to_list()
    await _fan_out(subs, {"title": title, "body": body, "url": url, "tag": tag})
    return recipients


async def send_to_admins(
    *,
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
) -> None:
    """Platform-wide fan-out. Use ONLY for events that legitimately
    concern every admin (system-wide warnings). For user-triggered
    activity prefer `send_to_user_owners` so admins aren't pinged for
    other pools' traffic.
    """
    subs = await PushSubscription.find(
        PushSubscription.subject_type == PushSubjectType.ADMIN
    ).to_list()
    await _fan_out(subs, {"title": title, "body": body, "url": url, "tag": tag})


async def send_to_user(
    user_id: PydanticObjectId | str,
    *,
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
) -> None:
    """Push to one specific trader — used for the
    'your deposit was approved / admin added funds' surface."""
    uid = PydanticObjectId(user_id) if isinstance(user_id, str) else user_id
    subs = await PushSubscription.find(
        PushSubscription.subject_type == PushSubjectType.USER,
        PushSubscription.subject_id == uid,
    ).to_list()
    await _fan_out(subs, {"title": title, "body": body, "url": url, "tag": tag})


async def send_to_users(
    user_ids: list[PydanticObjectId | str],
    *,
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
) -> None:
    """Bulk fan-out to a set of traders in ONE subscription query — used
    by the admin broadcast so a "message to all users" wakes every
    force-stopped PWA in the pool with a single Mongo read + a concurrent
    push burst. No-ops when VAPID is unconfigured (dev)."""
    uids: list[PydanticObjectId] = []
    for u in user_ids:
        try:
            uids.append(PydanticObjectId(u) if isinstance(u, str) else u)
        except Exception:
            continue
    if not uids:
        return
    subs = await PushSubscription.find(
        PushSubscription.subject_type == PushSubjectType.USER,
        {"subject_id": {"$in": uids}},
    ).to_list()
    await _fan_out(subs, {"title": title, "body": body, "url": url, "tag": tag})
