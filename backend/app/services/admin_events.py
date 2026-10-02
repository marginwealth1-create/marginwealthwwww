"""Single publish helper for the `admin:events` pub/sub channel.

Why a dedicated module: the admin WebSocket subscribes to ONE channel
(`admin:events`) rather than per-admin queues, so every emitter funnels
through a single function. That keeps the publish path one line at every
call site:

    await publish_admin_event("position_closed", {"user_id": ..., "id": ...})

The frontend's `AdminWsBridge` switches on the `type` field and
invalidates the matching React Query keys (positions / orders / wallet /
deposits / withdrawals / kyc). Adding a new event type just means
publishing it here and adding a case to the bridge.

Failures are logged + swallowed — a missing Redis must never block the
HTTP / WS request that triggered the event, exactly like the user-side
publishers in `wallet_service` and `admin/trading.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.redis_client import publish

logger = logging.getLogger(__name__)

ADMIN_CHANNEL = "admin:events"


async def publish_admin_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """Publish a JSON event to the global `admin:events` channel.

    Args:
        event_type: One of the strings the AdminWsBridge knows about
                    (`position_update`, `order_update`, `deposit_update`,
                    `withdrawal_update`, `kyc_update`, `wallet_update`).
        payload: Anything JSON-serialisable. The frontend doesn't rely on
                 the body for invalidation (the `type` alone tells it
                 which query keys to refresh) — payload is just for
                 future fine-grained handling.
    """
    body = {"type": event_type, **(payload or {})}
    try:
        await publish(ADMIN_CHANNEL, body)
    except Exception:  # pragma: no cover
        # Swallow — never let a pub/sub hiccup take down the calling
        # request. The admin dashboard's polling still keeps numbers
        # eventually-consistent if a single publish is lost.
        logger.exception("admin_event_publish_failed", extra={"type": event_type})


async def trade_alert_fields(
    user_id: Any,
    *,
    action: str,
    quantity: float,
    price: Any,
    symbol: str,
    event: str,
) -> dict[str, Any]:
    """Extra publish fields for a fill on a user the admin is watching.

    Returns ``{}`` — the normal case — when the user's `trade_alert` flag
    is off, so the fill path costs exactly one projected read. When it IS
    on, the payload carries everything the toast needs plus the
    `recipient_admin_ids` scope filter the deposit / withdrawal toasts
    already use, so one admin's watch never rings in another's panel.
    """
    from beanie import PydanticObjectId

    from app.models.user import User

    try:
        doc = await User.get_motor_collection().find_one(
            {"_id": PydanticObjectId(str(user_id))},
            {
                "trade_alert": 1,
                "trade_alert_sound": 1,
                "full_name": 1,
                "user_code": 1,
            },
        )
        if not doc or not doc.get("trade_alert"):
            return {}
        from app.services.push_service import _compute_recipient_admin_ids

        recipients = await _compute_recipient_admin_ids(str(user_id))
        return {
            "recipient_admin_ids": [str(r) for r in recipients],
            "alert": {
                "sound": doc.get("trade_alert_sound") or "chime",
                "user_name": doc.get("full_name") or "",
                "user_code": doc.get("user_code") or "",
                "action": action,
                "qty": quantity,
                "price": float(price),
                "symbol": symbol,
                "event": event,
            },
        }
    except Exception:
        # Never let the alert lookup break a fill — the publish still goes
        # out without it and the admin's tables refresh as before.
        logger.exception("trade_alert_fields_failed")
        return {}
