"""Audit logging — fire-and-forget writes to `audit_logs`.

The service avoids raising on failure (audit must not break business flow).
Production should attach this to a dedicated thread/queue if write throughput
becomes the bottleneck.
"""

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId

from app.models.audit_log import AuditAction, AuditLog

logger = logging.getLogger(__name__)


async def log_event(
    *,
    action: AuditAction,
    entity_type: str,
    entity_id: str | PydanticObjectId | None = None,
    actor_id: str | PydanticObjectId | None = None,
    target_user_id: str | PydanticObjectId | None = None,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    request_id: str | None = None,
) -> None:
    try:
        entry = AuditLog(
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            user_id=PydanticObjectId(actor_id) if actor_id else None,
            target_user_id=PydanticObjectId(target_user_id) if target_user_id else None,
            old_values=old_values,
            new_values=new_values,
            metadata=metadata or {},
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
        )
        await entry.insert()
    except Exception as e:  # pragma: no cover — never propagate audit errors
        logger.exception("audit_log_failed", extra={"error": str(e), "action": action})
