"""FastAPI dependencies for auth, role guards, and admin extras.

Two distinct token audiences:
    • USER tokens  → require role in {CLIENT, DEALER, MASTER, ADMIN, SUPER_ADMIN}
    • ADMIN tokens → require role in {ADMIN, SUPER_ADMIN} + API key + IP allow-list

Tokens carry the role inside the JWT, but we *always* re-fetch the user from
DB on every request — a token is meaningless if the account has been blocked.
"""

from __future__ import annotations

from typing import Annotated

from beanie import PydanticObjectId
from fastapi import Depends, Header, Request
from fastapi.security import OAuth2PasswordBearer

from app.core.config import settings
from app.core.exceptions import (
    AccountBlockedError,
    AccountInactiveError,
    InsufficientPermissionsError,
    NotFoundError,
    TokenInvalidError,
)
from app.core.security import decode_token
from app.models._base import PermissionLevel
from app.models.user import User, UserRole, UserStatus

# BROKER is admin-tier (admin login endpoint accepts them, JWT audience
# stays "admin") but visibility + write capability is narrowed below via
# require_broker_permission(perm, min_level).
# EMPLOYEE is admin-tier for login/JWT purposes too: employees log in through
# the (separate) admin auth machinery, pass get_current_admin, and are then
# narrowed to their granted sections via require_admin_permission and scoped to
# their PARENT admin's pool via effective_scope_actor. They are NEVER super-admin
# and can never manage other admins/employees (see require_admin_tier).
ADMIN_ROLES: set[UserRole] = {
    UserRole.SUPER_ADMIN,
    UserRole.ADMIN,
    UserRole.BROKER,
    UserRole.EMPLOYEE,
}

# Grouped (umbrella) permissions that are ALSO satisfied by any of their
# granular children — so a super-admin/admin can grant an employee just one nav
# page (e.g. "positions") and the employee still passes the umbrella gate the
# shared backend endpoints use (e.g. require_perm("trading_view")). Nav
# visibility stays granular (per-item empPerm on the frontend); the backend is
# intentionally permissive at the umbrella level (any trading child → the shared
# /trading endpoints), which is fine — the employee only SEES what they were
# granted, and all data stays scoped to their parent admin.
_UMBRELLA_CHILDREN: dict[str, tuple[str, ...]] = {
    "trading_view": ("orders", "positions", "marketwatch"),
    "ledger": ("money_transactions", "broker_deposits"),
}


def _admin_bool_perm(perms, perm: str) -> bool:
    """True if the admin-tier `admin_permissions` object grants `perm` directly
    OR grants any of its granular children (see _UMBRELLA_CHILDREN)."""
    if perms is None:
        return False
    if bool(getattr(perms, perm, False)):
        return True
    return any(bool(getattr(perms, c, False)) for c in _UMBRELLA_CHILDREN.get(perm, ()))


_user_oauth = OAuth2PasswordBearer(tokenUrl="/api/v1/user/auth/login", auto_error=True)
_admin_oauth = OAuth2PasswordBearer(tokenUrl="/api/v1/admin/auth/login", auto_error=True)


# ── Helpers ───────────────────────────────────────────────────────────
async def _resolve_user(token: str) -> User:
    payload = decode_token(token, expected_type="access")
    sub = payload.get("sub")
    if not sub:
        raise TokenInvalidError()
    try:
        oid = PydanticObjectId(sub)
    except Exception as e:  # pragma: no cover
        raise TokenInvalidError() from e
    user = await User.get(oid)
    if user is None:
        raise TokenInvalidError("User not found")
    if user.status == UserStatus.BLOCKED:
        raise AccountBlockedError()
    if user.status != UserStatus.ACTIVE:
        raise AccountInactiveError()
    # Session-epoch gate. A token minted before the user's `token_version`
    # was last bumped (admin block / password reset) is dead on arrival,
    # even though it's still cryptographically valid and unexpired. Old
    # tokens carry no `ver` claim → defaults to 0, matching a fresh user's
    # token_version=0, so this stays backward-compatible until the first bump.
    if int(payload.get("ver", 0) or 0) != int(getattr(user, "token_version", 0) or 0):
        raise TokenInvalidError("Session expired — please log in again")
    # Per-admin maintenance gate. If this user's owning admin flipped the pool
    # into maintenance, every live session is kicked here on its next request
    # (and the frontend shows the maintenance popup + logs out).
    from app.core.exceptions import MaintenanceModeError
    from app.services import user_service

    if await user_service.is_under_admin_maintenance(user):
        raise MaintenanceModeError()
    return user


# ── User-side dependencies ────────────────────────────────────────────
async def get_current_user(
    request: Request,
    token: Annotated[str, Depends(_user_oauth)],
) -> User:
    user = await _resolve_user(token)
    request.state.user = user
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


# ── Admin-side dependencies ───────────────────────────────────────────
def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


async def get_current_admin(
    request: Request,
    token: Annotated[str, Depends(_admin_oauth)],
    x_admin_api_key: Annotated[str | None, Header()] = None,
) -> User:
    # 1) API-key gate
    expected = settings.ADMIN_API_KEY.get_secret_value()
    if not expected or x_admin_api_key != expected:
        raise InsufficientPermissionsError("Admin API key required")

    # 2) IP allow-list (if configured)
    allow = settings.admin_ip_whitelist_set
    if allow and _client_ip(request) not in allow:
        raise InsufficientPermissionsError("Admin IP not allowed")

    # 3) Token
    user = await _resolve_user(token)
    if user.role not in ADMIN_ROLES:
        raise InsufficientPermissionsError("Admin role required")

    request.state.user = user
    return user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]


def require_super_admin(user: CurrentAdmin) -> User:
    if user.role != UserRole.SUPER_ADMIN:
        raise InsufficientPermissionsError("Super admin role required")
    return user


SuperAdmin = Annotated[User, Depends(require_super_admin)]


def require_admin_tier(user: CurrentAdmin) -> User:
    """SUPER_ADMIN or ADMIN only. Used by the Employee-management surface:
    every admin (super or sub) manages their OWN employees, but an EMPLOYEE
    can never create/manage employees, and a BROKER is out of scope here."""
    if user.role not in {UserRole.SUPER_ADMIN, UserRole.ADMIN}:
        raise InsufficientPermissionsError("Admin role required")
    return user


AdminTier = Annotated[User, Depends(require_admin_tier)]


# ── Optional auth (for endpoints that work with or without a token) ───
async def get_optional_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> User | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    try:
        return await _resolve_user(authorization[7:])
    except Exception:
        return None


# ── Sub-admin scoping helpers ─────────────────────────────────────────
# Strict ownership model: each admin only sees / acts on users in their own
# pool. Super-admin's pool = users with no assigned sub-admin (the platform
# pool). Sub-admin's pool = users whose `assigned_admin_id` matches their id.
# To act on a user that belongs to a sub-admin, super-admin must first
# reassign the user back to themselves via
# POST /api/v1/admin/management/users/{user_id}/assign (body: sub_admin_id=null).
_NON_CLIENT_ROLES = [
    UserRole.SUPER_ADMIN.value,
    UserRole.ADMIN.value,
    UserRole.BROKER.value,
    UserRole.EMPLOYEE.value,
]


async def effective_scope_actor(admin: User) -> User:
    """Resolve the actor whose POOL an admin-tier request operates on.

    An EMPLOYEE is staff of its creating admin and must see/act on THAT
    admin's pool — never its own (an employee owns no clients). So for an
    employee we return the parent admin (``assigned_admin_id``); for everyone
    else the actor is itself. All async scope helpers route through this, so a
    sub-admin's employee gets the sub-admin's pool and a super-admin's employee
    gets the platform pool — reusing the existing per-role logic unchanged.
    Falls back to the employee itself if the parent can't be loaded (its own
    empty pool → sees nothing, fail-safe).
    """
    if admin.role != UserRole.EMPLOYEE:
        return admin
    parent_id = getattr(admin, "assigned_admin_id", None)
    if parent_id is None:
        return admin
    parent = await User.get(parent_id)
    return parent if parent is not None else admin


def scoped_admin_filter(admin: User) -> dict:
    """Mongo filter to scope a User-collection query to the actor's own pool.

    Behavior per role:
      - SUPER_ADMIN: users in platform-default pool (no assigned admin)
      - ADMIN: users they own directly (admin's pool — automatically
        includes broker descendants since broker creation propagates
        ``assigned_admin_id = creating_admin.id`` down the chain)
      - BROKER: every user with this broker's id anywhere in
        ``broker_ancestry`` — covers the whole subtree (sub-brokers and
        their clients) via a single multikey-index lookup.
    """
    if admin.role == UserRole.SUPER_ADMIN:
        return {"assigned_admin_id": None}
    if admin.role == UserRole.BROKER:
        return {"broker_ancestry": admin.id}
    # ADMIN (and any other admin-tier role added later) defaults here.
    return {"assigned_admin_id": admin.id}


async def _admin_pool_clause(admin_id: PydanticObjectId) -> dict:
    """The ASSIGNMENT half of an ADMIN-role actor's owned-user filter
    (no role / status conditions — callers add their own).

    For ADMIN we union two buckets:
      1. Users with ``assigned_admin_id == admin.id`` (direct transfers
         and self-created clients).
      2. Users anywhere under a BROKER that itself lives in this admin's
         pool — via ``assigned_broker_id`` (direct broker clients) or
         ``broker_ancestry`` (whole subtree). Catches the case where a
         super-admin transferred a BROKER to this admin: the reassign
         endpoint only mutates the broker row, not its descendants, so
         the subtree clients keep their old ``assigned_admin_id`` but
         logically belong to the new admin.

    Returns a single-key ``{"assigned_admin_id": id}`` when the admin has
    no brokers (keeps the index lookup trivial), otherwise an ``$or``.
    """
    coll = User.get_motor_collection()
    broker_ids = [
        doc["_id"]
        async for doc in coll.find(
            {"role": UserRole.BROKER.value, "assigned_admin_id": admin_id},
            {"_id": 1},
        )
    ]
    if not broker_ids:
        return {"assigned_admin_id": admin_id}
    return {
        "$or": [
            {"assigned_admin_id": admin_id},
            {"assigned_broker_id": {"$in": broker_ids}},
            {"broker_ancestry": {"$in": broker_ids}},
        ]
    }


async def _pool_clause(admin: User) -> dict:
    """Assignment clause for any admin-tier actor (no role/status filter).

    EMPLOYEE actors resolve to their PARENT admin first (effective_scope_actor)
    so they operate on the parent's pool.
    """
    admin = await effective_scope_actor(admin)
    if admin.role == UserRole.SUPER_ADMIN:
        return {"assigned_admin_id": None}
    if admin.role == UserRole.BROKER:
        return {"broker_ancestry": admin.id}
    return await _admin_pool_clause(admin.id)


async def scoped_user_filter(admin: User) -> dict:
    """Async Mongo filter scoping a User query to the actor's owned pool,
    INCLUDING the broker subtree for ADMIN.

    Differs from the synchronous :func:`scoped_admin_filter` (which, for
    ADMIN, returns only the flat ``{assigned_admin_id: admin.id}`` and so
    MISSES transferred-broker subtree clients). Use this in list / count
    endpoints so the rows shown match the dashboard count produced by
    :func:`scoped_user_ids`.

    Does NOT add a role filter — callers that want client-tier-only rows
    keep their own ``role`` condition (every current caller already does).
    Combine with other ``$or`` conditions (e.g. a search box) via
    ``$and`` so the scope ``$or`` isn't clobbered.
    """
    return await _pool_clause(admin)


async def scoped_user_ids(
    admin: User, *, include_closed: bool = False
) -> list[PydanticObjectId]:
    """Returns the explicit list of user_ids the actor may touch.

    Role admin-tier rows (SUPER_ADMIN, ADMIN, BROKER) are always excluded
    so they don't leak into ledger / trading / wallet queries — those
    are aggregations meant for client-tier rows only. The assignment half
    (incl. the ADMIN broker-subtree union) is shared with
    :func:`scoped_user_filter` / :func:`count_admin_pool_clients` so the
    three can never drift.

    ``include_closed`` (default False): CLOSED (soft-deleted) users are
    excluded so a deleted user vanishes from trading / positions / P&L /
    accounts aggregations. Pass ``True`` for FINANCIAL-record views (Money
    Transactions) where a deleted user's cash in/out must still be visible
    for audit — the money moved is real and doesn't disappear with the user.
    """
    coll = User.get_motor_collection()
    base = await _pool_clause(admin)
    q: dict = {
        **base,
        "role": {"$nin": _NON_CLIENT_ROLES},
        "is_demo": {"$ne": True},
    }
    if not include_closed:
        q["status"] = {"$ne": UserStatus.CLOSED.value}
    cursor = coll.find(q, {"_id": 1})
    return [doc["_id"] async for doc in cursor]


async def super_admin_direct_user_ids(
    *, include_closed: bool = False
) -> list[PydanticObjectId]:
    """Client-tier users directly under the SUPER_ADMIN — NOT delegated to any
    admin (``assigned_admin_id is None``). This is the super-admin's OWN book,
    excluding every sub-admin's pool. Used by the super-admin Positions page so
    its table + Net-P&L cards show only the super-admin's own users, never any
    admin's clients (operator: "sirf super admin ke user ka data dikha")."""
    coll = User.get_motor_collection()
    q: dict = {
        "role": {"$nin": _NON_CLIENT_ROLES},
        "is_demo": {"$ne": True},
        "assigned_admin_id": None,
    }
    if not include_closed:
        q["status"] = {"$ne": UserStatus.CLOSED.value}
    cursor = coll.find(q, {"_id": 1})
    return [doc["_id"] async for doc in cursor]


async def count_admin_pool_clients(admin_id: PydanticObjectId) -> int:
    """Trading-client count for an ADMIN-role sub-admin's pool — identical
    to ``len(await scoped_user_ids(that_admin))``.

    Use for the super-admin's sub-admin list so its "USERS" column matches
    what the admin sees on their own dashboard / accounts. The previous
    flat ``{assigned_admin_id}`` count also counted the admin's BROKER /
    sub-broker LOGIN accounts (non-trading rows), so the list showed a
    larger number (e.g. 141) than the admin's dashboard (122).

    Excludes CLOSED (soft-deleted) rows so the number matches the admin
    Dashboard "Total users" tile, which filters ``status != CLOSED`` (see
    api/v1/admin/dashboard.py). The flat count also counted deleted
    clients, inflating the figure further.
    """
    coll = User.get_motor_collection()
    clause = await _admin_pool_clause(admin_id)
    return await coll.count_documents(
        {
            **clause,
            "role": {"$nin": _NON_CLIENT_ROLES},
            "status": {"$ne": UserStatus.CLOSED.value},
        }
    )


async def count_admin_pool_brokers(admin_id: PydanticObjectId) -> int:
    """Broker + sub-broker LOGIN accounts under an ADMIN-role sub-admin
    (every BROKER-role row in their pool, any depth), excluding CLOSED.

    Counted separately from :func:`count_admin_pool_clients` so the super-
    admin's sub-admins list can show "users" (trading clients) and
    "brokers" (login accounts) as two distinct columns instead of lumping
    them into one inflated number.
    """
    coll = User.get_motor_collection()
    clause = await _admin_pool_clause(admin_id)
    return await coll.count_documents(
        {
            **clause,
            "role": UserRole.BROKER.value,
            "status": {"$ne": UserStatus.CLOSED.value},
        }
    )


async def assert_user_in_scope(
    admin: User, target_user_id: str | PydanticObjectId, *, for_read: bool = False
) -> User:
    """Loads the target user and 403s if the actor doesn't own them.

    Admin-tier targets (SUPER_ADMIN / ADMIN / BROKER) are rejected here
    — those are managed via the dedicated management endpoints. Pool
    membership semantics per role:
      - SUPER_ADMIN: target.assigned_admin_id IS NULL
      - ADMIN: target.assigned_admin_id == admin.id
      - BROKER: admin.id in target.broker_ancestry

    ``for_read=True`` relaxes ONLY the super-admin path: the platform owner
    oversees every pool, so a read-only view (e.g. a user's ledger / txn
    history) may target any client — even one assigned to a sub-admin. WRITES
    keep the default strict check (super-admin must reassign the user into
    their own pool before adjusting its wallet). Sub-admin / broker scope is
    unchanged either way — cross-pool isolation between admins still holds.
    """
    # EMPLOYEE actors operate on their parent admin's pool.
    admin = await effective_scope_actor(admin)
    try:
        oid = PydanticObjectId(target_user_id)
    except Exception as e:  # pragma: no cover
        raise NotFoundError("User not found") from e
    target = await User.get(oid)
    if target is None:
        raise NotFoundError("User not found")
    if target.role in {UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.BROKER}:
        raise InsufficientPermissionsError(
            "Cannot operate on an admin/broker user via this endpoint"
        )
    if admin.role == UserRole.SUPER_ADMIN:
        if target.assigned_admin_id is not None and not for_read:
            raise InsufficientPermissionsError(
                "User is assigned to a sub-admin. Reassign to your pool first."
            )
        return target
    if admin.role == UserRole.BROKER:
        if admin.id not in (target.broker_ancestry or []):
            raise InsufficientPermissionsError("User not in your scope")
        return target
    # ADMIN
    if target.assigned_admin_id != admin.id:
        raise InsufficientPermissionsError("User not in your scope")
    return target


def require_admin_permission(perm: str):
    """Factory: FastAPI dep that allows SUPER_ADMIN through and checks
    ``admin_permissions.<perm>`` for ADMIN. BROKER is rejected — broker
    surfaces use ``require_broker_permission`` or ``require_perm`` instead."""

    async def _dep(admin: CurrentAdmin) -> User:
        if admin.role == UserRole.SUPER_ADMIN:
            return admin
        if admin.role == UserRole.BROKER:
            raise InsufficientPermissionsError(
                f"Permission '{perm}' not granted (broker)"
            )
        # ADMIN / EMPLOYEE — grant if the perm OR any granular child is held.
        if not _admin_bool_perm(admin.admin_permissions, perm):
            raise InsufficientPermissionsError(
                f"Permission '{perm}' not granted"
            )
        return admin

    return _dep


# ── Broker-tier helpers ───────────────────────────────────────────────
def require_broker_permission(perm: str, min_level: str = "VIEW"):
    """Factory: FastAPI dep for BROKER role. SUPER_ADMIN and ADMIN pass
    through unchanged (their permissions are gated separately). BROKER
    needs ``broker_permissions[perm]`` at >= ``min_level``."""

    required = PermissionLevel(min_level)

    async def _dep(admin: CurrentAdmin) -> User:
        if admin.role in {UserRole.SUPER_ADMIN, UserRole.ADMIN}:
            return admin
        # BROKER
        perms = admin.broker_permissions
        if perms is None:
            raise InsufficientPermissionsError(
                f"Permission '{perm}' not granted"
            )
        actual_raw = getattr(perms, perm, PermissionLevel.OFF)
        actual = (
            actual_raw if isinstance(actual_raw, PermissionLevel)
            else PermissionLevel(actual_raw)
        )
        if not PermissionLevel.at_least(actual, required):
            raise InsufficientPermissionsError(
                f"Permission '{perm}' requires {required.value} (have {actual.value})"
            )
        return admin

    return _dep


def assert_role_aware_perm(
    admin: "User", *, admin_perm: str, broker_perm: str, mode: str = "write"
) -> None:
    """Permission check that uses DIFFERENT keys for admin-tier vs broker so a
    broker action can be gated separately from the admin one on the SAME
    endpoint. Used inside endpoint bodies (e.g. wallet-adjust, reset-password)
    where a broker must NOT inherit the action from a broad `users` grant.

    super-admin → allowed; ADMIN / EMPLOYEE → ``admin_permissions[admin_perm]``
    (unchanged from `require_perm`); BROKER → ``broker_permissions[broker_perm]``
    at the required level (VIEW for read, EDIT for write). Raises otherwise."""
    if admin.role == UserRole.SUPER_ADMIN:
        return
    if admin.role in {UserRole.ADMIN, UserRole.EMPLOYEE}:
        if not _admin_bool_perm(admin.admin_permissions, admin_perm):
            raise InsufficientPermissionsError(f"Permission '{admin_perm}' not granted")
        return
    # BROKER
    required = PermissionLevel("EDIT" if mode == "write" else "VIEW")
    perms = admin.broker_permissions
    actual_raw = getattr(perms, broker_perm, PermissionLevel.OFF) if perms else PermissionLevel.OFF
    actual = actual_raw if isinstance(actual_raw, PermissionLevel) else PermissionLevel(actual_raw)
    if not PermissionLevel.at_least(actual, required):
        raise InsufficientPermissionsError(
            f"Permission '{broker_perm}' requires {required.value} (have {actual.value})"
        )


def require_perm(perm: str, mode: str = "read"):
    """Combined dep used by existing routers that serve both admin- and
    broker-tier callers. ``mode="read"`` requires VIEW or EDIT; ``"write"``
    requires EDIT. For SUPER_ADMIN / ADMIN it falls through to the boolean
    admin permission (true ⇒ allowed, regardless of mode); for BROKER it
    enforces the tri-state level.
    """
    min_level = "EDIT" if mode == "write" else "VIEW"

    async def _dep(admin: CurrentAdmin) -> User:
        if admin.role == UserRole.SUPER_ADMIN:
            return admin
        # EMPLOYEE reuses the ADMIN boolean check against its own
        # admin_permissions (granted sections, capped at its parent's perms).
        # A granular child (e.g. "positions") also satisfies its umbrella
        # (e.g. "trading_view") so one-page grants reach the shared endpoints.
        if admin.role in {UserRole.ADMIN, UserRole.EMPLOYEE}:
            if not _admin_bool_perm(admin.admin_permissions, perm):
                raise InsufficientPermissionsError(
                    f"Permission '{perm}' not granted"
                )
            return admin
        # BROKER — reuse the tri-state checker
        perms = admin.broker_permissions
        if perms is None:
            raise InsufficientPermissionsError(
                f"Permission '{perm}' not granted"
            )
        required = PermissionLevel(min_level)
        actual_raw = getattr(perms, perm, PermissionLevel.OFF)
        actual = (
            actual_raw if isinstance(actual_raw, PermissionLevel)
            else PermissionLevel(actual_raw)
        )
        if not PermissionLevel.at_least(actual, required):
            raise InsufficientPermissionsError(
                f"Permission '{perm}' requires {required.value} (have {actual.value})"
            )
        return admin

    return _dep


async def assert_broker_in_scope(
    actor: User, target_broker_id: str | PydanticObjectId
) -> User:
    """Ownership check when an actor mutates a broker record.

    Rules:
      - SUPER_ADMIN: target broker must be in super-admin's pool
        (assigned_admin_id IS NULL)
      - ADMIN: target broker's assigned_admin_id == admin.id
      - BROKER: target broker is in actor's subtree (actor.id in
        target.broker_ancestry)
    """
    try:
        oid = PydanticObjectId(target_broker_id)
    except Exception as e:  # pragma: no cover
        raise NotFoundError("Broker not found") from e
    target = await User.get(oid)
    if target is None or target.role != UserRole.BROKER:
        raise NotFoundError("Broker not found")
    if actor.role == UserRole.SUPER_ADMIN:
        if target.assigned_admin_id is not None:
            raise InsufficientPermissionsError(
                "Broker is under a sub-admin's pool"
            )
        return target
    if actor.role == UserRole.ADMIN:
        if target.assigned_admin_id != actor.id:
            raise InsufficientPermissionsError("Broker not in your scope")
        return target
    if actor.role == UserRole.BROKER:
        if actor.id not in (target.broker_ancestry or []):
            raise InsufficientPermissionsError("Broker not in your subtree")
        return target
    raise InsufficientPermissionsError("Cannot manage brokers from this role")


def max_grantable_perms(actor: User) -> dict[str, PermissionLevel]:
    """Returns the cap level for each broker-permission key. Used by the
    broker create/update endpoints to validate that a requested grant
    doesn't exceed what the actor themselves can grant.

    - SUPER_ADMIN: EDIT for everything.
    - ADMIN: per-key, admin_permissions[k] true → EDIT, false → OFF.
      The new `sub_brokers` key on BrokerPermissions has no admin
      counterpart and is always cap=EDIT for admins.
    - BROKER: per-key, broker_permissions[k] level directly.
    """
    # Keys live on BrokerPermissions — import inside to avoid cycles.
    from app.models.user import BrokerPermissions

    keys = list(BrokerPermissions.model_fields.keys())
    out: dict[str, PermissionLevel] = {}

    if actor.role == UserRole.SUPER_ADMIN:
        for k in keys:
            out[k] = PermissionLevel.EDIT
        return out

    if actor.role == UserRole.ADMIN:
        ap = actor.admin_permissions
        # `trading_view` / `ledger` are UMBRELLA perms on AdminPermissions —
        # an admin who holds ANY child (positions/orders/marketwatch under
        # trading_view; money_transactions/broker_deposits under ledger)
        # effectively has the umbrella and must be able to grant it to a
        # broker. Mirrors the "accept any child too" rule the AdminPermissions
        # model documents. Without this, an admin provisioned with only the
        # child flags (umbrella left False) saw Trading view / Ledger greyed
        # out in the broker-permission modal and couldn't grant them.
        _umbrella_children = {
            "trading_view": ("positions", "orders", "marketwatch"),
            "ledger": ("money_transactions", "broker_deposits"),
        }
        for k in keys:
            if k in ("sub_brokers", "support"):
                # Admin can always grant these broker-tier capabilities (the
                # admin itself doesn't consume them): `sub_brokers` gates
                # minting sub-brokers, `support` gates the broker setting its
                # own support number for its clients. No AdminPermissions flag
                # backs them, so hard-cap at EDIT or they'd be greyed out.
                out[k] = PermissionLevel.EDIT
                continue
            if k == "user_password":
                # No dedicated AdminPermissions flag — resetting a user's
                # password is part of USER management, so an admin who can
                # manage users can delegate password resets to a broker. Maps
                # to the admin's `users` flag (else the row is always greyed).
                allowed = bool(ap and getattr(ap, "users", False))
                out[k] = PermissionLevel.EDIT if allowed else PermissionLevel.OFF
                continue
            allowed = bool(ap and getattr(ap, k, False))
            if not allowed and k in _umbrella_children:
                allowed = bool(
                    ap and any(getattr(ap, c, False) for c in _umbrella_children[k])
                )
            out[k] = PermissionLevel.EDIT if allowed else PermissionLevel.OFF
        return out

    # BROKER
    bp = actor.broker_permissions
    for k in keys:
        v = getattr(bp, k, PermissionLevel.OFF) if bp is not None else PermissionLevel.OFF
        out[k] = v if isinstance(v, PermissionLevel) else PermissionLevel(v)
    return out
