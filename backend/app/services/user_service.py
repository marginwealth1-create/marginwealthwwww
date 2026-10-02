"""User-domain operations — lookups, code generation, hierarchy walks."""

from __future__ import annotations

import secrets
from typing import Any, Iterable

from beanie import PydanticObjectId
from beanie.operators import Or

from app.core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from app.core.security import hash_password
from app.models._base import ALL_SEGMENTS
from app.models.user import (
    KycInfo,
    User,
    UserPermissions,
    UserRole,
    UserSegment,
    UserStatus,
)
from app.models.wallet import Wallet
from app.utils.validators import is_valid_mobile_in, normalize_mobile_in


# Stand-ins for a missing email / phone on an admin-created account.
NO_EMAIL_DOMAIN = "noemail.marginwealth.live"
NO_MOBILE_PREFIX = "NOMOB"


def placeholder_email(user_code: str) -> str:
    return f"{user_code.lower()}@{NO_EMAIL_DOMAIN}"


def placeholder_mobile(user_code: str) -> str:
    return f"{NO_MOBILE_PREFIX}{user_code}"


def is_placeholder_contact(value: str | None) -> bool:
    """True for a generated stand-in — the UI shows a dash instead."""
    v = (value or "").strip()
    return v.endswith("@" + NO_EMAIL_DOMAIN) or v.startswith(NO_MOBILE_PREFIX)


# Six characters, letters and digits mixed. O/0 and I/1/L are left out —
# the code IS a login id now (operator: "user user id se login toh ho jata
# hai"), so it gets read off a screen and typed on a phone.
_CODE_LETTERS = "ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_DIGITS = "23456789"
_CODE_LEN = 6


def make_user_code() -> str:
    """One candidate code, e.g. 'K7M2QX'. Always has a letter AND a digit.

    The letter guarantee also keeps a code from ever looking like a phone
    number, which matters because login resolves a bare identifier by
    trying mobile before user_code.
    """
    pool = _CODE_LETTERS + _CODE_DIGITS
    chars = [
        secrets.choice(_CODE_LETTERS),
        secrets.choice(_CODE_DIGITS),
        *(secrets.choice(pool) for _ in range(_CODE_LEN - 2)),
    ]
    # Shuffle so the letter and digit aren't always in the first two slots.
    for i in range(len(chars) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        chars[i], chars[j] = chars[j], chars[i]
    return "".join(chars)


async def generate_user_code(role: UserRole) -> str:
    """Returns a unique 6-character user_code. Retries on conflict.

    `role` is no longer part of the code — the old 'CL12345678' shape was
    ten characters of which eight were noise. Existing codes keep working
    untouched; nothing in the codebase parses the prefix.
    """
    for _ in range(10):
        code = make_user_code()
        existing = await User.find_one(User.user_code == code)
        if existing is None:
            return code
    raise ConflictError("Could not generate a unique user code; please retry")


async def find_by_identifier(identifier: str) -> User | None:
    """Lookup by email OR mobile (10-digit Indian)."""
    ident = identifier.strip().lower()
    if "@" in ident:
        return await User.find_one(User.email == ident)
    mobile = normalize_mobile_in(ident)
    if is_valid_mobile_in(mobile):
        return await User.find_one(User.mobile == mobile)
    # last resort: user_code
    return await User.find_one(User.user_code == ident.upper())


async def email_or_mobile_taken(
    email: str | None,
    mobile: str | None,
    exclude_user_id: PydanticObjectId | None = None,
) -> str | None:
    """Returns the field name that conflicts, or None.

    CLOSED rows (soft-deleted by admin → /admin/users/{id} DELETE) are
    NOT counted as conflicts: re-registering with a previously deleted
    user's email should succeed.  The delete path renames their
    email/mobile to a sentinel so the unique index doesn't fight a new
    insert either — this is defence-in-depth on the API side.
    """
    # An admin-created user may have neither — only check what was given.
    wanted: list[dict] = []
    if email:
        wanted.append({"email": email.lower()})
    if mobile:
        wanted.append({"mobile": mobile})
    if not wanted:
        return None
    query: list[dict[str, Any]] = [
        {"$or": wanted},
        {"status": {"$ne": UserStatus.CLOSED.value}},
    ]
    # Editing an existing user: their OWN row is not a conflict.
    if exclude_user_id is not None:
        query.append({"_id": {"$ne": exclude_user_id}})
    existing = await User.find_one({"$and": query})
    if existing is None:
        return None
    if email and existing.email == email.lower():
        return "email"
    return "mobile"


async def create_user(
    *,
    email: str | None,
    mobile: str | None,
    password: str,
    full_name: str,
    role: UserRole = UserRole.CLIENT,
    status: UserStatus = UserStatus.ACTIVE,
    parent_id: PydanticObjectId | None = None,
    kyc: KycInfo | None = None,
    permissions: UserPermissions | None = None,
    is_demo: bool = False,
    created_by: PydanticObjectId | None = None,
    assigned_admin_id: PydanticObjectId | None = None,
    assigned_broker_id: PydanticObjectId | None = None,
    broker_ancestry: list[PydanticObjectId] | None = None,
    signup_origin: str | None = None,
) -> User:
    email_l = (email or "").lower().strip()
    mobile_raw = (mobile or "").strip()
    mobile_n = normalize_mobile_in(mobile_raw) if mobile_raw else ""
    conflict = await email_or_mobile_taken(email_l, mobile_n)
    if conflict:
        raise ConflictError(
            f"A user with this {conflict} already exists",
            details={"field": conflict},
        )

    code = await generate_user_code(role)
    # Both columns are uniquely indexed and non-null, so a user created
    # without an email / phone (admin-created accounts log in with their
    # user code) gets a placeholder derived from that code — unique by
    # construction, and never a valid address or number, so it can't
    # collide with a real one or be mistaken for it.
    if not email_l:
        email_l = placeholder_email(code)
    if not mobile_n:
        mobile_n = placeholder_mobile(code)

    user = User(
        user_code=code,
        email=email_l,
        mobile=mobile_n,
        password_hash=hash_password(password),
        full_name=full_name.strip(),
        role=role,
        status=status,
        parent_id=parent_id,
        kyc=kyc or KycInfo(),
        permissions=permissions or UserPermissions(),
        is_demo=is_demo,
        created_by=created_by,
        assigned_admin_id=assigned_admin_id,
        assigned_broker_id=assigned_broker_id,
        broker_ancestry=broker_ancestry or [],
        signup_origin=signup_origin,
    )
    await user.insert()

    # Create wallet (one per user) — sequential. An earlier asyncio.gather
    # version raced on Beanie's shared session on cold Mongo connections
    # and surfaced as a 500 from /auth/register, so the micro-optimisation
    # was reverted in favour of reliability.
    wallet = Wallet(user_id=user.id)  # type: ignore[arg-type]
    await wallet.insert()

    # Default segment access — all enabled (admin can prune later).
    await UserSegment.insert_many(
        [
            UserSegment(user_id=user.id, segment=s.value, enabled=True)  # type: ignore[arg-type]
            for s in ALL_SEGMENTS
        ]
    )

    return user


async def get_user_or_404(user_id: str | PydanticObjectId) -> User:
    try:
        oid = PydanticObjectId(user_id)
    except Exception as e:
        raise ValidationFailedError("Invalid user id") from e
    user = await User.get(oid)
    if user is None:
        raise NotFoundError("User not found")
    return user


async def is_under_admin_maintenance(user: User) -> bool:
    """True when the user's OWNING admin has maintenance mode ON.

    Used to block login and kick live sessions for a whole pool when the
    super-admin flips an admin into maintenance. Admins / super-admins are
    never blocked by it, and a user with no owning admin (the super-admin's
    own pool) isn't either — the switch is strictly per-admin.

    ponytail: one extra _id lookup per authenticated request; add a short
    Redis cache keyed by admin_id only if the auth hot-path shows up in
    profiling.
    """
    if user.role in (UserRole.SUPER_ADMIN, UserRole.ADMIN):
        return False
    admin_id = user.assigned_admin_id
    if not admin_id:
        return False
    admin = await User.get(admin_id)
    return bool(admin and getattr(admin, "maintenance_mode", False))


async def carry_toggle_enabled_for(user: User) -> bool:
    """True when the per-position Carry-Forward toggle feature is enabled for
    this user — i.e. any admin-tier ancestor (broker → admin → super-admin) has
    ``carry_forward_toggle_enabled=True``. Walks the assignment chain (broker
    first, then admin) exactly like the support-WhatsApp resolver, capped at 8
    hops. When False the platform's normal auto-carry runs and the app hides
    the per-position toggle."""
    cur: User | None = user
    seen: set[PydanticObjectId] = set()
    hops = 0
    while cur is not None and hops < 8:
        if cur.id in seen:
            break
        seen.add(cur.id)
        if bool(getattr(cur, "carry_forward_toggle_enabled", False)):
            return True
        nxt = getattr(cur, "assigned_broker_id", None) or getattr(cur, "assigned_admin_id", None)
        if nxt is None or nxt in seen:
            break
        cur = await User.get(nxt)
        hops += 1
    return False


async def descendants_of(user_id: PydanticObjectId, *, max_depth: int = 6) -> list[User]:
    """BFS through hierarchy. max_depth caps cost; trees deeper than 6 are
    almost certainly a misconfiguration."""
    out: list[User] = []
    frontier: Iterable[PydanticObjectId] = [user_id]
    for _ in range(max_depth):
        next_frontier: list[PydanticObjectId] = []
        if not frontier:
            break
        children = await User.find(User.parent_id.in_(list(frontier))).to_list()  # type: ignore[attr-defined]
        if not children:
            break
        out.extend(children)
        next_frontier = [c.id for c in children]  # type: ignore[misc]
        frontier = next_frontier
    return out
