"""Scope helpers: `all_pools` widens only the effective super-admin's POOL
clause (D1); ADMIN / BROKER scope is untouched (D2); an EMPLOYEE resolves to
its parent (D3). Also: EMPLOYEE targets are rejected by assert_user_in_scope,
and the audit scope resolves an `audit`-granted EMPLOYEE to its parent's pool."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin.settings import _audit_scope_user_ids
from app.core import dependencies as deps
from app.core.config import settings
from app.core.exceptions import ConflictError, InsufficientPermissionsError
from app.models.user import AdminPermissions, User, UserRole, UserStatus
from app.services import admin_management_service as mgmt

_DB = f"{settings.MONGODB_DB_NAME}_test_scope_all_pools"


@pytest_asyncio.fixture
async def w():
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=[User])
    n = 0

    async def mk(role: UserRole, **kw) -> User:
        nonlocal n
        n += 1
        u = User(
            user_code=f"SCP{n:03d}",
            email=f"scope{n}@example.com",
            mobile=f"98000{n:05d}",
            full_name=f"U{n}",
            password_hash="x",
            role=role,
            status=kw.pop("status", UserStatus.ACTIVE),
            **kw,
        )
        await u.insert()
        return u

    try:
        sa = await mk(UserRole.SUPER_ADMIN)
        a1 = await mk(UserRole.ADMIN)
        a2 = await mk(UserRole.ADMIN)
        b1 = await mk(UserRole.BROKER, assigned_admin_id=a1.id)
        w = SimpleNamespace(
            sa=sa,
            a1=a1,
            a2=a2,
            b1=b1,
            sa_emp=await mk(UserRole.EMPLOYEE, assigned_admin_id=sa.id, created_by=sa.id),
            a1_emp=await mk(UserRole.EMPLOYEE, assigned_admin_id=a1.id, created_by=a1.id),
            c_direct=await mk(UserRole.CLIENT),
            c_a1=await mk(UserRole.CLIENT, assigned_admin_id=a1.id),
            c_b1=await mk(
                UserRole.CLIENT,
                assigned_admin_id=a1.id,
                assigned_broker_id=b1.id,
                broker_ancestry=[b1.id],
            ),
            c_a2=await mk(UserRole.CLIENT, assigned_admin_id=a2.id),
            # Must stay out even with all_pools: the non-pool filters hold.
            demo_a2=await mk(UserRole.CLIENT, assigned_admin_id=a2.id, is_demo=True),
            closed_a2=await mk(
                UserRole.CLIENT, assigned_admin_id=a2.id, status=UserStatus.CLOSED
            ),
        )
        yield w
    finally:
        await client.drop_database(_DB)
        client.close()


def _ids(*users: User) -> set:
    return {u.id for u in users}


async def test_default_scope_unchanged(w):
    assert set(await deps.scoped_user_ids(w.sa)) == _ids(w.c_direct)
    assert set(await deps.scoped_user_ids(w.sa_emp)) == _ids(w.c_direct)
    assert set(await deps.scoped_user_ids(w.a1)) == _ids(w.c_a1, w.c_b1)
    assert set(await deps.scoped_user_ids(w.a1_emp)) == _ids(w.c_a1, w.c_b1)
    assert set(await deps.scoped_user_ids(w.a2)) == _ids(w.c_a2)
    assert set(await deps.scoped_user_ids(w.b1)) == _ids(w.c_b1)
    assert await deps.scoped_user_filter(w.sa) == {"assigned_admin_id": None}


async def test_all_pools_widens_effective_super_admin_only(w):
    every = _ids(w.c_direct, w.c_a1, w.c_b1, w.c_a2)
    assert set(await deps.scoped_user_ids(w.sa, all_pools=True)) == every
    assert set(await deps.scoped_user_ids(w.sa_emp, all_pools=True)) == every
    # include_closed still applies on top of the widened pool.
    assert set(
        await deps.scoped_user_ids(w.sa, all_pools=True, include_closed=True)
    ) == every | _ids(w.closed_a2)
    assert await deps.scoped_user_filter(w.sa, all_pools=True) == {}
    assert await deps.scoped_user_filter(w.sa_emp, all_pools=True) == {}


async def test_all_pools_ignored_for_admin_broker_and_their_employees(w):
    for actor in (w.a1, w.a1_emp, w.a2, w.b1):
        assert set(await deps.scoped_user_ids(actor, all_pools=True)) == set(
            await deps.scoped_user_ids(actor)
        )
        assert await deps.scoped_user_filter(
            actor, all_pools=True
        ) == await deps.scoped_user_filter(actor)


async def test_is_effective_super_admin(w):
    assert await deps.is_effective_super_admin(w.sa) is True
    assert await deps.is_effective_super_admin(w.sa_emp) is True
    for actor in (w.a1, w.a1_emp, w.a2, w.b1):
        assert await deps.is_effective_super_admin(actor) is False


async def test_assert_user_in_scope_rejects_employee_targets(w):
    with pytest.raises(InsufficientPermissionsError):
        await deps.assert_user_in_scope(w.a1, w.a1_emp.id)
    with pytest.raises(InsufficientPermissionsError):
        await deps.assert_user_in_scope(w.sa, w.sa_emp.id, for_read=True)
    # Clients still pass.
    assert (await deps.assert_user_in_scope(w.a1, w.c_a1.id)).id == w.c_a1.id


async def test_reassign_rejects_employee_targets(w):
    # A1 must not hand its own staff login to A2 (it would then act on A2's pool).
    with pytest.raises(ConflictError):
        await mgmt.reassign_user(w.a1_emp.id, w.a2.id, w.a1.id)
    assert (await User.get(w.a1_emp.id)).assigned_admin_id == w.a1.id


async def test_audit_scope_resolves_employee_to_parent(w):
    # Without the `audit` grant an employee keeps today's own-events view.
    for emp in (w.sa_emp, w.a1_emp):
        assert await _audit_scope_user_ids(emp) == [emp.id]
        emp.admin_permissions = AdminPermissions(audit=True)
    assert await _audit_scope_user_ids(w.sa_emp) is None
    scope = set(await _audit_scope_user_ids(w.a1_emp))
    assert _ids(w.a1, w.a1_emp, w.c_a1, w.c_b1) <= scope
    assert not scope & _ids(w.c_a2, w.c_direct)
