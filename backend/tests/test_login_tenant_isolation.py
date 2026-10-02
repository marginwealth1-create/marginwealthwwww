"""Unit tests for multi-tenant login isolation — the `user_in_tenant`
predicate that gates which accounts may authenticate on a given branded
admin domain.

Pure-function tests — no DB / Redis / event loop. They lock the exact
allow/deny matrix the feature promises:

    * a user of admin X can log in on X's domain,
    * a user of admin X is REJECTED on admin Y's domain,
    * the admin owning the domain can log in on their own site,
    * a SUPER_ADMIN can log in on ANY domain (platform owner),
    * a super-admin-pool user (assigned_admin_id=None) is rejected on any
      branded tenant domain (they must use the main platform domain).

ObjectId / str equivalence is covered because real callers pass Beanie
PydanticObjectId while tests pass plain strings.
"""

from bson import ObjectId

from app.models.user import UserRole
from app.services.auth_service import user_in_tenant

# Two distinct admins ("X" owns the domain under test; "Y" is a stranger).
ADMIN_X = ObjectId()
ADMIN_Y = ObjectId()


def _check(*, role, uid, assigned, tenant):
    return user_in_tenant(
        user_id=uid,
        user_role=role,
        user_assigned_admin_id=assigned,
        tenant_admin_id=tenant,
    )


# ── Downstream users ─────────────────────────────────────────────────
def test_client_of_admin_allowed_on_own_domain():
    # Client assigned to X logging into X's domain → allowed.
    assert _check(role=UserRole.CLIENT, uid=ObjectId(), assigned=ADMIN_X, tenant=ADMIN_X)


def test_client_of_admin_rejected_on_other_domain():
    # The core isolation guarantee: X's client CANNOT log in via Y's domain.
    assert not _check(
        role=UserRole.CLIENT, uid=ObjectId(), assigned=ADMIN_X, tenant=ADMIN_Y
    )


def test_dealer_and_master_follow_assigned_admin():
    for role in (UserRole.DEALER, UserRole.MASTER):
        assert _check(role=role, uid=ObjectId(), assigned=ADMIN_X, tenant=ADMIN_X)
        assert not _check(role=role, uid=ObjectId(), assigned=ADMIN_X, tenant=ADMIN_Y)


def test_broker_of_admin_allowed_on_own_domain():
    # A broker carries assigned_admin_id = their parent admin.
    assert _check(role=UserRole.BROKER, uid=ObjectId(), assigned=ADMIN_X, tenant=ADMIN_X)
    assert not _check(
        role=UserRole.BROKER, uid=ObjectId(), assigned=ADMIN_X, tenant=ADMIN_Y
    )


# ── The admin themselves ─────────────────────────────────────────────
def test_admin_allowed_on_their_own_domain():
    # Admin X (assigned_admin_id is None — owned by super-admin) logging
    # into their OWN branded domain is allowed via the id == tenant match.
    assert _check(role=UserRole.ADMIN, uid=ADMIN_X, assigned=None, tenant=ADMIN_X)


def test_admin_rejected_on_another_admins_domain():
    # Admin Y must not log into admin X's site.
    assert not _check(role=UserRole.ADMIN, uid=ADMIN_Y, assigned=None, tenant=ADMIN_X)


# ── Super-admin carve-out ────────────────────────────────────────────
def test_super_admin_allowed_everywhere():
    # Platform owner can authenticate on any branded domain.
    assert _check(role=UserRole.SUPER_ADMIN, uid=ObjectId(), assigned=None, tenant=ADMIN_X)
    assert _check(role=UserRole.SUPER_ADMIN, uid=ObjectId(), assigned=ADMIN_Y, tenant=ADMIN_X)


# ── Platform-pool users (no admin) ───────────────────────────────────
def test_super_pool_user_rejected_on_branded_domain():
    # A user with no assigned admin belongs to the platform pool — they
    # log in via the MAIN domain (which never gates), NOT a tenant domain.
    assert not _check(role=UserRole.CLIENT, uid=ObjectId(), assigned=None, tenant=ADMIN_X)


# ── Id-type equivalence (ObjectId vs str) ────────────────────────────
def test_str_and_objectid_ids_compare_equal():
    uid = ObjectId()
    # assigned as str, tenant as ObjectId — must still match.
    assert _check(role=UserRole.CLIENT, uid=uid, assigned=str(ADMIN_X), tenant=ADMIN_X)
    # user IS the admin, ids given as str on both sides.
    assert _check(role=UserRole.ADMIN, uid=str(ADMIN_X), assigned=None, tenant=str(ADMIN_X))
    # mismatch still rejects across types.
    assert not _check(
        role=UserRole.CLIENT, uid=uid, assigned=str(ADMIN_X), tenant=str(ADMIN_Y)
    )
