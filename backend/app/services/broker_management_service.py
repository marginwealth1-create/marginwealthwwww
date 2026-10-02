"""Broker tier — create/manage brokers (admin → broker, broker → sub-broker).

Mirrors `admin_management_service.py` for the broker layer. Two key
differences vs. sub-admin:

  1. Permissions are TRI-STATE (`PermissionLevel.OFF | VIEW | EDIT`)
     rather than boolean. Every grant is validated against the actor's
     own cap via `max_grantable_perms(actor)`.
  2. Brokers can nest. A broker creating a sub-broker propagates its
     ancestry (`new.broker_ancestry = creator.broker_ancestry + [creator.id]`)
     so a single multikey query on `broker_ancestry` scopes the whole
     subtree.

All mutations write an audit-log entry. Pure data layer — HTTP shaping
lives in `app/api/v1/admin/brokers.py`.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from beanie import PydanticObjectId

from app.core.dependencies import (
    assert_broker_in_scope,
    max_grantable_perms,
)
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.models._base import PermissionLevel
from app.models.audit_log import AuditAction
from app.models.user import (
    BrokerPermissions,
    User,
    UserRole,
    UserStatus,
)
from app.services import user_service
from app.services.audit_service import log_event
from app.utils.decimal_utils import to_decimal, to_decimal128


# ── Validation helpers ───────────────────────────────────────────────
def _validate_permissions_against_cap(
    requested: BrokerPermissions, cap: dict[str, PermissionLevel]
) -> None:
    """Raises if any requested level exceeds the actor's cap.

    Cap comparison uses ordering OFF < VIEW < EDIT defined on
    PermissionLevel. Used both at create and at update time.
    """
    for key in BrokerPermissions.model_fields:
        want_raw = getattr(requested, key, PermissionLevel.OFF)
        want = (
            want_raw if isinstance(want_raw, PermissionLevel)
            else PermissionLevel(want_raw)
        )
        max_level = cap.get(key, PermissionLevel.OFF)
        if not PermissionLevel.at_least(max_level, want):
            raise ValidationFailedError(
                f"Permission '{key}' = {want.value} exceeds your cap of {max_level.value}"
            )


def _clip_to_cap(
    current: BrokerPermissions, cap: dict[str, PermissionLevel]
) -> tuple[BrokerPermissions, list[str]]:
    """Returns a copy of `current` with every key clipped to the cap, plus
    the list of keys actually clipped. Used by the cascade after a parent
    downgrade so descendant permissions stay <= their new parent's cap."""
    changes: list[str] = []
    data = current.model_dump()
    for key in BrokerPermissions.model_fields:
        actual_raw = data.get(key, PermissionLevel.OFF.value)
        actual = (
            actual_raw if isinstance(actual_raw, PermissionLevel)
            else PermissionLevel(actual_raw)
        )
        ceiling = cap.get(key, PermissionLevel.OFF)
        if not PermissionLevel.at_least(ceiling, actual):
            data[key] = ceiling.value
            changes.append(key)
    return BrokerPermissions(**data), changes


def _resolve_creator_chain(creator: User) -> tuple[PydanticObjectId | None, list[PydanticObjectId]]:
    """Returns ``(assigned_admin_id, broker_ancestry)`` to stamp on a newly
    created broker, given the creator's role.

      - SUPER_ADMIN → ``(None, [])`` — top broker in platform pool.
      - ADMIN       → ``(admin.id, [])`` — top broker under that admin.
      - BROKER      → ``(creator.assigned_admin_id,
                          creator.broker_ancestry + [creator.id])`` — sub-broker.
    """
    if creator.role == UserRole.SUPER_ADMIN:
        return None, []
    if creator.role == UserRole.ADMIN:
        return creator.id, []
    if creator.role == UserRole.BROKER:
        return (
            creator.assigned_admin_id,
            list(creator.broker_ancestry or []) + [creator.id],
        )
    raise ValidationFailedError("Cannot create brokers from this role")


# ── CRUD ─────────────────────────────────────────────────────────────
async def create_broker(
    *,
    creator: User,
    email: str,
    mobile: str,
    password: str,
    full_name: str,
    permissions: BrokerPermissions,
    pnl_share_pct: Decimal,
    brokerage_share_pct: Decimal = Decimal("0"),
    assigned_admin_id: PydanticObjectId | None = None,
) -> User:
    """Mints a new BROKER row. Validates permission cap, sets the ownership
    chain, and writes an audit log.

    Allowed creators: SUPER_ADMIN (top-level broker in platform pool, or
    pinned to a specific admin via `assigned_admin_id`), ADMIN (top-level
    broker in their pool — `assigned_admin_id` arg ignored / must match
    self), or BROKER (sub-broker in their subtree — `assigned_admin_id`
    arg ignored / must match the inherited admin).
    """
    if pnl_share_pct < 0 or pnl_share_pct > 100:
        raise ValidationFailedError("pnl_share_pct must be between 0 and 100")
    if brokerage_share_pct < 0 or brokerage_share_pct > 100:
        raise ValidationFailedError("brokerage_share_pct must be between 0 and 100")

    cap = max_grantable_perms(creator)
    _validate_permissions_against_cap(permissions, cap)

    resolved_admin_id, ancestry = _resolve_creator_chain(creator)

    # Super-admin may pin the broker under any existing ADMIN. For non-super
    # callers, reject any payload assigned_admin_id that differs from the
    # natural chain (defense in depth — frontend shouldn't send it).
    if assigned_admin_id is not None:
        if creator.role == UserRole.SUPER_ADMIN:
            target_admin = await User.get(assigned_admin_id)
            if target_admin is None or target_admin.role != UserRole.ADMIN:
                raise ValidationFailedError(
                    "assigned_admin_id must reference an existing ADMIN user"
                )
            resolved_admin_id = target_admin.id
        elif assigned_admin_id != resolved_admin_id:
            raise ValidationFailedError(
                "Only super-admin may set assigned_admin_id on broker create"
            )

    assigned_admin_id = resolved_admin_id

    new = await user_service.create_user(
        email=email,
        mobile=mobile,
        password=password,
        full_name=full_name,
        role=UserRole.BROKER,
        status=UserStatus.ACTIVE,
        created_by=creator.id,
        assigned_admin_id=assigned_admin_id,
        assigned_broker_id=creator.id if creator.role == UserRole.BROKER else None,
        broker_ancestry=ancestry,
    )
    new.broker_permissions = permissions
    new.broker_pnl_share_pct = to_decimal128(pnl_share_pct)
    new.broker_brokerage_share_pct = to_decimal128(brokerage_share_pct)
    await new.save()

    # Snapshot the creator's current effective settings (segments + risk)
    # into the new broker's tier-tables. Creator may be super-admin
    # (top-level broker in platform pool), admin (top-level broker in
    # admin's pool), or another broker (sub-broker chain). See
    # `settings_snapshot` module docstring for the inheritance policy.
    try:
        from app.services.settings_snapshot import snapshot_for_new_broker

        await snapshot_for_new_broker(new.id, creator=creator)
    except Exception:
        # Snapshot is best-effort. Boot-time backfill catches misses.
        import logging as _lg

        _lg.getLogger(__name__).exception(
            "settings_snapshot_failed_on_broker_create broker=%s creator=%s",
            new.id,
            creator.id,
        )

    await log_event(
        action=AuditAction.BROKER_CREATE,
        entity_type="User",
        entity_id=new.id,
        actor_id=creator.id,
        target_user_id=new.id,
        new_values={
            "permissions": permissions.model_dump(),
            "pnl_share_pct": str(pnl_share_pct),
            "brokerage_share_pct": str(brokerage_share_pct),
            "broker_ancestry": [str(x) for x in ancestry],
        },
    )
    return new


async def get_broker_or_404(broker_id: str | PydanticObjectId) -> User:
    try:
        oid = PydanticObjectId(broker_id)
    except Exception as e:
        raise ValidationFailedError("Invalid broker id") from e
    b = await User.get(oid)
    if b is None or b.role != UserRole.BROKER:
        raise NotFoundError("Broker not found")
    return b


async def update_broker(
    actor: User,
    broker_id: str | PydanticObjectId,
    *,
    full_name: str | None,
) -> User:
    b = await assert_broker_in_scope(actor, broker_id)
    changes: dict[str, Any] = {}
    if full_name is not None and full_name.strip() and full_name != b.full_name:
        changes["full_name"] = full_name.strip()
        b.full_name = full_name.strip()
    if changes:
        await b.save()
        await log_event(
            action=AuditAction.BROKER_UPDATE,
            entity_type="User",
            entity_id=b.id,
            actor_id=actor.id,
            target_user_id=b.id,
            new_values=changes,
        )
    return b


async def update_broker_permissions(
    actor: User,
    broker_id: str | PydanticObjectId,
    new_perms: BrokerPermissions,
) -> tuple[User, list[dict]]:
    """Replaces the broker's permissions. After saving, cascade-clips any
    descendant sub-broker whose grant now exceeds this broker's cap so
    privilege never escalates beyond what the parent currently grants.

    Returns ``(broker, cascaded_changes)`` — `cascaded_changes` is a list
    of {"id", "user_code", "changes": [...keys clipped]} for the audit
    surface so the admin UI can show what got auto-downgraded.
    """
    b = await assert_broker_in_scope(actor, broker_id)
    cap = max_grantable_perms(actor)
    _validate_permissions_against_cap(new_perms, cap)

    old = b.broker_permissions.model_dump() if b.broker_permissions else None
    b.broker_permissions = new_perms
    await b.save()
    await log_event(
        action=AuditAction.BROKER_PERMS_UPDATE,
        entity_type="User",
        entity_id=b.id,
        actor_id=actor.id,
        target_user_id=b.id,
        old_values={"permissions": old},
        new_values={"permissions": new_perms.model_dump()},
    )

    # Cascade-clip descendants — anyone with broker_ancestry containing b.id
    # AND role == BROKER. Use the broker's new perms as the new descendant cap.
    descendant_cap: dict[str, PermissionLevel] = {
        k: (
            getattr(new_perms, k)
            if isinstance(getattr(new_perms, k), PermissionLevel)
            else PermissionLevel(getattr(new_perms, k))
        )
        for k in BrokerPermissions.model_fields
    }
    cascaded: list[dict] = []
    descendants = await User.find(
        {"role": UserRole.BROKER.value, "broker_ancestry": b.id}
    ).to_list()
    for sub in descendants:
        if sub.broker_permissions is None:
            continue
        clipped, keys_changed = _clip_to_cap(sub.broker_permissions, descendant_cap)
        if keys_changed:
            sub.broker_permissions = clipped
            await sub.save()
            await log_event(
                action=AuditAction.BROKER_PERMS_UPDATE,
                entity_type="User",
                entity_id=sub.id,
                actor_id=actor.id,
                target_user_id=sub.id,
                metadata={
                    "kind": "CASCADE_CLIP",
                    "clipped_keys": keys_changed,
                    "from_broker_id": str(b.id),
                },
            )
            cascaded.append(
                {
                    "id": str(sub.id),
                    "user_code": sub.user_code,
                    "changes": keys_changed,
                }
            )
    return b, cascaded


async def set_broker_pnl_share(
    actor: User,
    broker_id: str | PydanticObjectId,
    pct: Decimal,
    brokerage_pct: Decimal | None = None,
) -> User:
    pct_dec = to_decimal(pct)
    if pct_dec < 0 or pct_dec > 100:
        raise ValidationFailedError("pct must be between 0 and 100")
    b = await assert_broker_in_scope(actor, broker_id)
    old = str(b.broker_pnl_share_pct) if b.broker_pnl_share_pct is not None else None
    old_bkg = (
        str(b.broker_brokerage_share_pct)
        if b.broker_brokerage_share_pct is not None
        else None
    )
    b.broker_pnl_share_pct = to_decimal128(pct_dec)
    new_values: dict[str, str | None] = {"pnl_share_pct": str(pct_dec)}
    if brokerage_pct is not None:
        bkg_dec = to_decimal(brokerage_pct)
        if bkg_dec < 0 or bkg_dec > 100:
            raise ValidationFailedError("brokerage_pct must be between 0 and 100")
        b.broker_brokerage_share_pct = to_decimal128(bkg_dec)
        new_values["brokerage_share_pct"] = str(bkg_dec)
    await b.save()
    await log_event(
        action=AuditAction.BROKER_PNL_SHARE_UPDATE,
        entity_type="User",
        entity_id=b.id,
        actor_id=actor.id,
        target_user_id=b.id,
        old_values={"pnl_share_pct": old, "brokerage_share_pct": old_bkg},
        new_values=new_values,
    )
    return b


async def block_broker(actor: User, broker_id: str | PydanticObjectId) -> User:
    b = await assert_broker_in_scope(actor, broker_id)
    b.status = UserStatus.BLOCKED
    await b.save()
    await log_event(
        action=AuditAction.BLOCK,
        entity_type="User",
        entity_id=b.id,
        actor_id=actor.id,
        target_user_id=b.id,
        metadata={"kind": "BROKER"},
    )
    return b


async def delete_broker(actor: User, broker_id: str | PydanticObjectId) -> User:
    """Permanently delete a broker / sub-broker. Their direct children (users +
    sub-brokers) are moved UP to this broker's parent so nobody is orphaned, and
    the broker is dropped from every descendant's ancestry chain. Scope-checked:
    an admin can delete their brokers; a broker can delete their sub-brokers."""
    b = await assert_broker_in_scope(actor, broker_id)
    coll = User.get_motor_collection()
    parent_broker = b.assigned_broker_id  # None → children become direct clients of the admin
    # 1) Move direct children up one level (assigned_broker_id B → B's parent).
    await coll.update_many(
        {"assigned_broker_id": b.id},
        {"$set": {"assigned_broker_id": parent_broker}},
    )
    # 2) Drop B from every descendant's broker_ancestry chain (keeps the rest of
    #    the hierarchy intact; assigned_admin_id is unchanged so they stay in the
    #    same admin's pool).
    await coll.update_many(
        {"broker_ancestry": b.id},
        {"$pull": {"broker_ancestry": b.id}},
    )
    await b.delete()
    await log_event(
        action=AuditAction.DELETE,
        entity_type="User",
        entity_id=b.id,
        actor_id=actor.id,
        target_user_id=b.id,
        metadata={"kind": "BROKER", "user_code": b.user_code},
    )
    return b


async def set_broker_payment_gateway(
    actor: User, broker_id: str | PydanticObjectId, enabled: bool
) -> User:
    """Turn the Divinepay UPI gateway ON/OFF for this broker's users. The admin
    (whose own gateway the super-admin enabled) decides per broker. Enforced in
    divinepay_service.gateway_on_for — the owning admin must ALSO be enabled, so
    a broker toggle can't grant the gateway when the admin isn't allowed."""
    b = await assert_broker_in_scope(actor, broker_id)
    b.payment_gateway_enabled = bool(enabled)
    await b.save()
    await log_event(
        action=AuditAction.UPDATE,
        entity_type="User",
        entity_id=b.id,
        actor_id=actor.id,
        target_user_id=b.id,
        metadata={"kind": "BROKER", "payment_gateway_enabled": bool(enabled)},
    )
    return b


async def unblock_broker(actor: User, broker_id: str | PydanticObjectId) -> User:
    b = await assert_broker_in_scope(actor, broker_id)
    b.status = UserStatus.ACTIVE
    b.failed_login_count = 0
    b.locked_until = None
    await b.save()
    await log_event(
        action=AuditAction.UNBLOCK,
        entity_type="User",
        entity_id=b.id,
        actor_id=actor.id,
        target_user_id=b.id,
        metadata={"kind": "BROKER"},
    )
    return b


async def reset_broker_password(
    actor: User,
    broker_id: str | PydanticObjectId,
    new_password: str,
) -> User:
    """Reset a broker's (or sub-broker's) password to a value chosen by
    the actor. Mirrors `admin_management_service.reset_password` for the
    sub-admin tier so the admin-side three-dot menu can expose the same
    flow for every tier the actor owns.

    Scope is enforced by `assert_broker_in_scope` — super-admins can
    reset any broker, admins their own brokers, brokers their own sub-
    brokers. The target itself is unblocked of any failed-login lockout
    so the broker can sign in with the new password immediately.
    """
    from app.core.security import hash_password

    b = await assert_broker_in_scope(actor, broker_id)
    b.password_hash = hash_password(new_password)
    # Mirror the unblock side-effects so a brand-new password isn't
    # stalled behind a stale lockout. Doesn't change `status`.
    b.failed_login_count = 0
    b.locked_until = None
    await b.save()
    await log_event(
        action=AuditAction.PASSWORD_RESET,
        entity_type="User",
        entity_id=b.id,
        actor_id=actor.id,
        target_user_id=b.id,
        metadata={"kind": "BROKER"},
    )
    return b


# ── Listing ──────────────────────────────────────────────────────────
async def list_brokers_for(
    actor: User,
    *,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    admin_id: PydanticObjectId | None = None,
    include_sub: bool = False,
) -> tuple[list[User], int]:
    """Returns brokers visible to the actor.

      - SUPER_ADMIN + admin_id=X → brokers under that admin (assigned_admin_id == X)
      - SUPER_ADMIN + admin_id=None → top brokers in platform pool (no assigned_admin_id)
      - ADMIN       → brokers in their pool (assigned_admin_id == admin.id)
        and NO parent broker (top brokers under the admin). `admin_id` arg ignored.
      - BROKER      → their direct sub-brokers (assigned_broker_id == self.id)

    When ``include_sub=True`` the assigned_broker_id filter is dropped
    so the result includes the full subtree (top brokers + every
    sub-broker under them). Used by the create-user dropdown so admins
    can place a new client directly under any broker / sub-broker in
    their pool, and brokers can pick any descendant.
    """
    query: dict[str, Any] = {"role": UserRole.BROKER.value}

    if actor.role == UserRole.SUPER_ADMIN:
        if admin_id is not None:
            query["assigned_admin_id"] = admin_id
        else:
            query["assigned_admin_id"] = None
        if not include_sub:
            query["assigned_broker_id"] = None
    elif actor.role == UserRole.ADMIN:
        query["assigned_admin_id"] = actor.id
        if not include_sub:
            query["assigned_broker_id"] = None
    elif actor.role == UserRole.BROKER:
        if include_sub:
            # Full subtree under this broker — match anyone whose
            # broker_ancestry chain includes us.
            query["broker_ancestry"] = actor.id
        else:
            query["assigned_broker_id"] = actor.id
    else:
        return [], 0

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


async def count_assigned_users(broker_id: PydanticObjectId) -> int:
    """Direct clients of this broker — broker_ancestry's last element is
    the immediate broker. We use a positional match for that."""
    coll = User.get_motor_collection()
    return await coll.count_documents(
        {
            "role": {"$nin": [UserRole.SUPER_ADMIN.value, UserRole.ADMIN.value, UserRole.BROKER.value]},
            "assigned_broker_id": broker_id,
        }
    )


async def count_subtree_users(broker_id: PydanticObjectId) -> int:
    """Whole subtree client count (descendants of any depth)."""
    coll = User.get_motor_collection()
    return await coll.count_documents(
        {
            "role": {"$nin": [UserRole.SUPER_ADMIN.value, UserRole.ADMIN.value, UserRole.BROKER.value]},
            "broker_ancestry": broker_id,
        }
    )


async def list_subtree_clients(
    broker_id: str | PydanticObjectId,
    *,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[User], int]:
    """Whole subtree clients (every CLIENT/MASTER/DEALER with broker_id in
    their broker_ancestry)."""
    try:
        oid = PydanticObjectId(broker_id)
    except Exception as e:
        raise ValidationFailedError("Invalid broker id") from e
    query = {
        "role": {"$nin": [UserRole.SUPER_ADMIN.value, UserRole.ADMIN.value, UserRole.BROKER.value]},
        "broker_ancestry": oid,
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


# ── Reassignment ─────────────────────────────────────────────────────
async def reassign_user_to_broker(
    actor: User,
    user_id: str | PydanticObjectId,
    new_broker_id: str | PydanticObjectId | None,
) -> User:
    """Move a client into a broker's pool, or back out (None ⇒ admin pool).

    Re-stamps `assigned_admin_id`, `assigned_broker_id`, and
    `broker_ancestry` so existing scope queries continue to find them.
    Actor must own both source (existing pool) and destination (new
    broker) — enforced via the existing `assert_*_in_scope` helpers.
    """
    from app.core.dependencies import assert_user_in_scope as _assert_user

    target = await _assert_user(actor, user_id)
    if target.role in {UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.BROKER}:
        raise ConflictError("Cannot reassign an admin/broker-tier user")

    new_broker: User | None = None
    if new_broker_id is not None:
        new_broker = await assert_broker_in_scope(actor, new_broker_id)

    old = {
        "assigned_admin_id": str(target.assigned_admin_id) if target.assigned_admin_id else None,
        "assigned_broker_id": str(target.assigned_broker_id) if target.assigned_broker_id else None,
        "broker_ancestry": [str(x) for x in (target.broker_ancestry or [])],
    }

    if new_broker is None:
        # Return to admin/platform pool — clear broker linkage but keep
        # assigned_admin_id (drops back to actor's pool).
        if actor.role == UserRole.ADMIN:
            target.assigned_admin_id = actor.id
        elif actor.role == UserRole.SUPER_ADMIN:
            target.assigned_admin_id = None
        target.assigned_broker_id = None
        target.broker_ancestry = []
    else:
        target.assigned_admin_id = new_broker.assigned_admin_id
        target.assigned_broker_id = new_broker.id
        target.broker_ancestry = list(new_broker.broker_ancestry or []) + [new_broker.id]

    # Stamp transfer telemetry — mirrors admin_management_service.reassign_user
    # so the "Transferred" badge fires for broker hops too.
    from app.utils.time_utils import now_utc as _now_utc

    target.last_transferred_at = _now_utc()
    target.last_transferred_by = actor.id
    await target.save()

    # Cache-bust the per-user netting + risk caches — same reasoning as
    # admin_management_service.reassign_user. Broker hops change
    # broker_ancestry (and therefore which BrokerSegmentOverride row
    # applies), so the resolver's 5-min memoisation would keep serving
    # the old broker's lot caps / margins until the cache expired.
    try:
        from app.core.redis_client import cache_delete_pattern

        await cache_delete_pattern(f"netting_eff:{target.id}:*")
        await cache_delete_pattern(f"risk:{target.id}")
    except Exception:
        pass
    await log_event(
        action=AuditAction.USER_REASSIGN_TO_BROKER,
        entity_type="User",
        entity_id=target.id,
        actor_id=actor.id,
        target_user_id=target.id,
        old_values=old,
        new_values={
            "assigned_admin_id": str(target.assigned_admin_id) if target.assigned_admin_id else None,
            "assigned_broker_id": str(target.assigned_broker_id) if target.assigned_broker_id else None,
            "broker_ancestry": [str(x) for x in (target.broker_ancestry or [])],
        },
    )
    return target


async def bulk_reassign_to_broker(
    actor: User,
    user_ids: list[str],
    new_broker_id: str | PydanticObjectId | None,
) -> dict[str, Any]:
    moved = 0
    failed: list[dict[str, str]] = []
    for uid in user_ids:
        try:
            await reassign_user_to_broker(actor, uid, new_broker_id)
            moved += 1
        except Exception as e:
            failed.append({"user_id": uid, "error": str(e)})
    return {"moved": moved, "failed": failed}
