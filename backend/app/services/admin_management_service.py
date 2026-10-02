"""Super-admin operations on sub-admins and user assignment.

All mutations write an audit log entry. Pure data-layer; HTTP shaping lives
in [app.api.v1.admin.management](../api/v1/admin/management.py).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from beanie import PydanticObjectId

from app.core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from app.models.audit_log import AuditAction
from app.models.user import (
    AdminPermissions,
    User,
    UserRole,
    UserStatus,
)
from app.services import user_service
from app.services.audit_service import log_event
from app.utils.decimal_utils import to_decimal, to_decimal128


async def _get_sub_admin_or_404(sub_admin_id: str | PydanticObjectId) -> User:
    try:
        oid = PydanticObjectId(sub_admin_id)
    except Exception as e:
        raise ValidationFailedError("Invalid sub-admin id") from e
    sa = await User.get(oid)
    if sa is None or sa.role != UserRole.ADMIN:
        raise NotFoundError("Sub-admin not found")
    return sa


async def create_sub_admin(
    *,
    email: str,
    mobile: str,
    password: str,
    full_name: str,
    permissions: AdminPermissions,
    pnl_share_pct: Decimal,
    created_by: PydanticObjectId,
) -> User:
    if pnl_share_pct < 0 or pnl_share_pct > 100:
        raise ValidationFailedError("pnl_share_pct must be between 0 and 100")

    sa = await user_service.create_user(
        email=email,
        mobile=mobile,
        password=password,
        full_name=full_name,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
        created_by=created_by,
        # Sub-admin themselves are not assigned to anyone.
        assigned_admin_id=None,
    )
    sa.admin_permissions = permissions
    sa.pnl_share_pct = to_decimal128(pnl_share_pct)
    await sa.save()

    # Snapshot the super-admin's current effective settings (segments
    # + risk) into the new admin's tier-tables so their settings page
    # opens populated instead of blank. The admin can edit freely from
    # there — their edits never bubble back to super-admin, and any
    # future super-admin edits do NOT cascade down. See
    # `settings_snapshot` module docstring for the policy.
    try:
        from app.services.settings_snapshot import snapshot_for_new_admin

        await snapshot_for_new_admin(sa.id, source_super_admin_id=created_by)
    except Exception:
        # Snapshot is best-effort — a Mongo hiccup here must not roll
        # back the admin creation. The boot-time backfill will pick up
        # any miss on the next deploy.
        import logging as _lg

        _lg.getLogger(__name__).exception(
            "settings_snapshot_failed_on_admin_create admin=%s", sa.id
        )

    await log_event(
        action=AuditAction.SUB_ADMIN_CREATE,
        entity_type="User",
        entity_id=sa.id,
        actor_id=created_by,
        target_user_id=sa.id,
        new_values={
            "permissions": permissions.model_dump(),
            "pnl_share_pct": str(pnl_share_pct),
        },
    )
    return sa


async def update_sub_admin(
    sub_admin_id: str | PydanticObjectId,
    *,
    full_name: str | None,
    actor_id: PydanticObjectId,
) -> User:
    sa = await _get_sub_admin_or_404(sub_admin_id)
    changes: dict[str, Any] = {}
    if full_name is not None and full_name.strip() and full_name != sa.full_name:
        changes["full_name"] = full_name.strip()
        sa.full_name = full_name.strip()
    if changes:
        await sa.save()
        await log_event(
            action=AuditAction.SUB_ADMIN_UPDATE,
            entity_type="User",
            entity_id=sa.id,
            actor_id=actor_id,
            target_user_id=sa.id,
            new_values=changes,
        )
    return sa


async def update_permissions(
    sub_admin_id: str | PydanticObjectId,
    permissions: AdminPermissions,
    actor_id: PydanticObjectId,
) -> User:
    sa = await _get_sub_admin_or_404(sub_admin_id)
    old = sa.admin_permissions.model_dump() if sa.admin_permissions else None
    sa.admin_permissions = permissions
    await sa.save()
    await log_event(
        action=AuditAction.SUB_ADMIN_PERMS_UPDATE,
        entity_type="User",
        entity_id=sa.id,
        actor_id=actor_id,
        target_user_id=sa.id,
        old_values={"permissions": old},
        new_values={"permissions": permissions.model_dump()},
    )
    return sa


async def set_pnl_share(
    sub_admin_id: str | PydanticObjectId,
    pct: Decimal,
    actor_id: PydanticObjectId,
) -> User:
    pct_dec = to_decimal(pct)
    if pct_dec < 0 or pct_dec > 100:
        raise ValidationFailedError("pct must be between 0 and 100")
    sa = await _get_sub_admin_or_404(sub_admin_id)
    old = str(sa.pnl_share_pct) if sa.pnl_share_pct is not None else None
    sa.pnl_share_pct = to_decimal128(pct_dec)
    await sa.save()
    await log_event(
        action=AuditAction.SUB_ADMIN_PNL_SHARE_UPDATE,
        entity_type="User",
        entity_id=sa.id,
        actor_id=actor_id,
        target_user_id=sa.id,
        old_values={"pnl_share_pct": old},
        new_values={"pnl_share_pct": str(pct_dec)},
    )
    return sa


async def block_sub_admin(
    sub_admin_id: str | PydanticObjectId, actor_id: PydanticObjectId
) -> User:
    sa = await _get_sub_admin_or_404(sub_admin_id)
    sa.status = UserStatus.BLOCKED
    await sa.save()
    # Force-logout every active session of the blocked sub-admin (same
    # rationale as the client block path in admin/users.py).
    from app.services import auth_service as _auth

    await _auth.revoke_user_sessions(sa)
    await log_event(
        action=AuditAction.BLOCK,
        entity_type="User",
        entity_id=sa.id,
        actor_id=actor_id,
        target_user_id=sa.id,
        metadata={"kind": "SUB_ADMIN"},
    )
    return sa


async def unblock_sub_admin(
    sub_admin_id: str | PydanticObjectId, actor_id: PydanticObjectId
) -> User:
    sa = await _get_sub_admin_or_404(sub_admin_id)
    sa.status = UserStatus.ACTIVE
    sa.failed_login_count = 0
    sa.locked_until = None
    await sa.save()
    await log_event(
        action=AuditAction.UNBLOCK,
        entity_type="User",
        entity_id=sa.id,
        actor_id=actor_id,
        target_user_id=sa.id,
        metadata={"kind": "SUB_ADMIN"},
    )
    return sa


async def set_maintenance(
    sub_admin_id: str | PydanticObjectId,
    enabled: bool,
    actor_id: PydanticObjectId,
) -> User:
    """Flip this admin's whole pool into (or out of) maintenance. When ON,
    every non-admin user under this admin is blocked from logging in and any
    live session is kicked on its next request (enforced in the auth
    dependency + the frontend maintenance interceptor). No bulk session revoke
    is needed — the app polls every few seconds, so the kick is effectively
    immediate."""
    sa = await _get_sub_admin_or_404(sub_admin_id)
    sa.maintenance_mode = bool(enabled)
    await sa.save()
    await log_event(
        action=AuditAction.UPDATE,
        entity_type="User",
        entity_id=sa.id,
        actor_id=actor_id,
        target_user_id=sa.id,
        metadata={"kind": "SUB_ADMIN", "maintenance_mode": bool(enabled)},
    )
    return sa


async def set_payment_gateway(
    sub_admin_id: str | PydanticObjectId,
    enabled: bool,
    actor_id: PydanticObjectId,
) -> User:
    """Turn the Divinepay UPI gateway ON/OFF for this admin's pool. ON → their
    users get the auto-crediting online pay-in; OFF → the manual deposit flow."""
    sa = await _get_sub_admin_or_404(sub_admin_id)
    sa.payment_gateway_enabled = bool(enabled)
    await sa.save()
    await log_event(
        action=AuditAction.UPDATE,
        entity_type="User",
        entity_id=sa.id,
        actor_id=actor_id,
        target_user_id=sa.id,
        metadata={"kind": "SUB_ADMIN", "payment_gateway_enabled": bool(enabled)},
    )
    return sa


async def list_sub_admins(
    *, status: str | None = None, q: str | None = None, page: int = 1, page_size: int = 20
) -> tuple[list[User], int]:
    query: dict[str, Any] = {"role": UserRole.ADMIN.value}
    if status:
        query["status"] = status
    if q:
        regex = re.compile(re.escape(q.strip()), re.IGNORECASE)
        query["$or"] = [
            {"email": regex},
            {"mobile": regex},
            {"user_code": regex},
            {"full_name": regex},
        ]
    total = await User.find(query).count()
    rows = (
        await User.find(query)
        .sort("-created_at")
        .skip((page - 1) * page_size)
        .limit(page_size)
        .to_list()
    )
    return rows, total


async def count_assigned_users(sub_admin_id: PydanticObjectId) -> int:
    """Trading-client count for a sub-admin's pool — matches exactly what
    that admin sees on their own dashboard / accounts (the comprehensive
    scoped set: directly-assigned clients PLUS the whole broker subtree,
    with admin / broker / sub-broker LOGIN rows excluded).

    Was a flat ``{assigned_admin_id}`` count that also counted the admin's
    broker / sub-broker login accounts, so the super-admin's sub-admin
    list showed a larger number (e.g. 141) than the admin's own dashboard
    (122). Now delegates to the shared scope helper so all views agree.
    """
    from app.core.dependencies import count_admin_pool_clients

    return await count_admin_pool_clients(sub_admin_id)


async def count_assigned_brokers(sub_admin_id: PydanticObjectId) -> int:
    """Broker + sub-broker LOGIN accounts under a sub-admin — shown as a
    separate column from the trading-client count on the sub-admins list."""
    from app.core.dependencies import count_admin_pool_brokers

    return await count_admin_pool_brokers(sub_admin_id)


async def list_assigned_users(
    sub_admin_id: str | PydanticObjectId,
    *,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[User], int]:
    try:
        oid = PydanticObjectId(sub_admin_id)
    except Exception as e:
        raise ValidationFailedError("Invalid sub-admin id") from e
    # Comprehensive client scope (directly-assigned + whole broker
    # subtree, admin/broker login rows excluded, CLOSED soft-deleted rows
    # excluded) so this drill-in list and its row count match the sub-
    # admin's own dashboard / accounts and the "USERS" number on the sub-
    # admins list (see count_assigned_users).
    from app.core.dependencies import _NON_CLIENT_ROLES, _admin_pool_clause
    from app.models.user import UserStatus

    clause = await _admin_pool_clause(oid)
    query = {
        **clause,
        "role": {"$nin": _NON_CLIENT_ROLES},
        "status": {"$ne": UserStatus.CLOSED.value},
    }
    total = await User.find(query).count()
    rows = (
        await User.find(query)
        .sort("-created_at")
        .skip((page - 1) * page_size)
        .limit(page_size)
        .to_list()
    )
    return rows, total


async def reassign_user(
    user_id: str | PydanticObjectId,
    new_sub_admin_id: str | PydanticObjectId | None,
    actor_id: PydanticObjectId,
) -> User:
    """Move a user into a sub-admin's pool, or back to super-admin (None)."""
    target = await user_service.get_user_or_404(user_id)
    if target.role in {UserRole.SUPER_ADMIN, UserRole.ADMIN}:
        raise ConflictError("Cannot reassign an admin-role user")

    new_oid: PydanticObjectId | None = None
    if new_sub_admin_id is not None:
        sa = await _get_sub_admin_or_404(new_sub_admin_id)
        new_oid = sa.id

    old = str(target.assigned_admin_id) if target.assigned_admin_id else None
    target.assigned_admin_id = new_oid
    # Detach from the OLD owner's broker subtree. Without this the user keeps
    # its `assigned_broker_id` / `broker_ancestry` pointing at a broker that
    # belongs to the previous admin, so the OLD admin's dashboard (scoped by
    # broker_ancestry) still lists the transferred user. Clearing them makes the
    # user a DIRECT client of the destination admin — it disappears from the
    # source admin and shows cleanly under the new one. (A BROKER being moved
    # likewise becomes a top-level broker of the new admin; its own downline is
    # re-stamped below.)
    target.assigned_broker_id = None
    target.broker_ancestry = []
    # Stamp transfer telemetry so the destination dashboard can render
    # a "Transferred" badge and the audit trail of last-owner-change is
    # readable without joining audit_logs.
    from app.utils.time_utils import now_utc as _now_utc

    target.last_transferred_at = _now_utc()
    target.last_transferred_by = actor_id
    await target.save()

    # If the target is a BROKER, propagate the new assigned_admin_id
    # to its entire subtree (sub-brokers + client-tier users below).
    # Without this the destination admin's dashboard kept showing the
    # broker but none of its downline — operator-reported "super-admin
    # se transfer kiye gaye users admin ke cards me count nahi ho rahe".
    if target.role == UserRole.BROKER:
        try:
            coll = User.get_motor_collection()
            await coll.update_many(
                {"broker_ancestry": target.id},
                {"$set": {"assigned_admin_id": new_oid}},
            )
        except Exception:
            pass

    # Cache-bust the per-user netting + risk caches — the resolver reads
    # `assigned_admin_id` LIVE to pick the right sub-admin / super-admin
    # segment override, but the resolved settings are memoised in Redis
    # for 5 min. Without an explicit purge the user would keep trading
    # under the OLD owner's lot caps / margins / commissions for up to
    # CACHE_TTL after the transfer commits. User-flagged: "transfer
    # ke baad us user ki segment setting jis admin ne kiya hai uski
    # work karegi ki nahi?".
    try:
        from app.core.redis_client import cache_delete_pattern

        await cache_delete_pattern(f"netting_eff:{target.id}:*")
        await cache_delete_pattern(f"risk:{target.id}")
    except Exception:
        # Cache miss is harmless — settings will re-resolve on the
        # next order. Don't fail the transfer over Redis hiccups.
        pass
    await log_event(
        action=AuditAction.USER_REASSIGN,
        entity_type="User",
        entity_id=target.id,
        actor_id=actor_id,
        target_user_id=target.id,
        old_values={"assigned_admin_id": old},
        new_values={"assigned_admin_id": str(new_oid) if new_oid else None},
    )
    return target


async def bulk_reassign(
    user_ids: list[str],
    new_sub_admin_id: str | PydanticObjectId | None,
    actor_id: PydanticObjectId,
) -> dict[str, Any]:
    moved = 0
    failed: list[dict[str, str]] = []
    for uid in user_ids:
        try:
            await reassign_user(uid, new_sub_admin_id, actor_id)
            moved += 1
        except Exception as e:
            failed.append({"user_id": uid, "error": str(e)})
    return {"moved": moved, "failed": failed}


async def delete_sub_admin(
    sub_admin_id: PydanticObjectId,
    *,
    actor_id: PydanticObjectId,
) -> None:
    """Permanently delete a sub-admin. Reassigns their users back to the
    platform pool (assigned_admin_id = None) so they don't become orphans.
    Any ACTIVE / PAUSED P&L sharing agreements for this admin are ENDed to
    preserve history. Caller must be SUPER_ADMIN (gated at the router level).
    """
    sa = await _get_sub_admin_or_404(sub_admin_id)

    # Reassign assigned users back to platform pool
    coll = User.get_motor_collection()
    await coll.update_many(
        {"assigned_admin_id": sa.id},
        {"$set": {"assigned_admin_id": None}},
    )

    # End any active P&L sharing agreements for this admin (preserve history)
    from app.models.pnl_sharing import AgreementStatus, PnlSharingAgreement
    from app.utils.time_utils import now_utc

    await PnlSharingAgreement.find(
        PnlSharingAgreement.admin_id == sa.id,
        PnlSharingAgreement.status != AgreementStatus.ENDED,
    ).update_many({
        "$set": {
            "status": AgreementStatus.ENDED.value,
            "effective_until": now_utc(),
            "last_modified_by": actor_id,
        }
    })

    await sa.delete()

    await log_event(
        action=AuditAction.DELETE,
        entity_type="User",
        entity_id=sa.id,
        actor_id=actor_id,
        target_user_id=sa.id,
        new_values={
            "user_code": sa.user_code,
            "email": sa.email,
            "role": "ADMIN",
        },
    )


async def reset_password(
    sub_admin_id: PydanticObjectId,
    new_password: str,
    *,
    actor_id: PydanticObjectId,
) -> User:
    """Reset a sub-admin's password to a new value chosen by super-admin.
    Sub-admin should change it on next login (no flag enforced in Phase 1)."""
    from app.core.security import hash_password

    sa = await _get_sub_admin_or_404(sub_admin_id)
    sa.password_hash = hash_password(new_password)
    await sa.save()
    # Force-logout so the old password's sessions die immediately.
    from app.services import auth_service as _auth

    await _auth.revoke_user_sessions(sa)

    await log_event(
        action=AuditAction.PASSWORD_RESET,
        entity_type="User",
        entity_id=sa.id,
        actor_id=actor_id,
        target_user_id=sa.id,
    )
    return sa
