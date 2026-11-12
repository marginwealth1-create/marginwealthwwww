"""Accounts dashboard entity scope (§1 sweep / §4.4).

Every /admin/accounts entity endpoint goes through one guard: an EMPLOYEE
entity (incl. the caller's own id) is its parent's pool, an unknown entity
role means NO users, non-super-admins may name only themselves or a BROKER in
their pool, and the effective super-admin may name any entity (D1). Also:
the per-user table lists BLOCKED clients (D4), the virtual 'direct' row lists
the no-broker users, and the row exports take a user_id.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import accounts
from app.core.config import settings
from app.core.exceptions import InsufficientPermissionsError
from app.models.pnl_sharing import PnlSharingAgreement
from app.models.position import Position
from app.models.trade import Trade
from app.models.transaction import WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import accounts_dashboard_service as ads

_DB = f"{settings.MONGODB_DB_NAME}_test_accounts_entity_scope"
_NO_DATES = {"from_date": None, "to_date": None, "preset": None, "_": None}


@pytest_asyncio.fixture
async def w():
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(
        database=client[_DB],
        document_models=[User, Position, Trade, Wallet, WalletTransaction, PnlSharingAgreement],
    )
    n = 0

    async def mk(role: UserRole, **kw) -> User:
        nonlocal n
        n += 1
        u = User(
            user_code=f"AES{n:03d}",
            email=f"aes{n}@example.com",
            mobile=f"97000{n:05d}",
            full_name=f"U{n}",
            password_hash="x",
            role=role,
            status=kw.pop("status", UserStatus.ACTIVE),
            **kw,
        )
        await u.insert()
        if role == UserRole.CLIENT:
            # Non-zero activity so the zero-activity row guard keeps it.
            await Wallet(user_id=u.id, settlement_outstanding=Decimal128("100")).insert()
        return u

    try:
        sa = await mk(UserRole.SUPER_ADMIN)
        a1 = await mk(UserRole.ADMIN)
        a2 = await mk(UserRole.ADMIN)
        b1 = await mk(UserRole.BROKER, assigned_admin_id=a1.id)
        sb1 = await mk(
            UserRole.BROKER, assigned_admin_id=a1.id, assigned_broker_id=b1.id,
            broker_ancestry=[b1.id],
        )
        b2 = await mk(UserRole.BROKER, assigned_admin_id=a2.id)
        yield SimpleNamespace(
            sa=sa, a1=a1, a2=a2, b1=b1, sb1=sb1, b2=b2,
            sa_emp=await mk(UserRole.EMPLOYEE, assigned_admin_id=sa.id, created_by=sa.id),
            a1_emp=await mk(UserRole.EMPLOYEE, assigned_admin_id=a1.id, created_by=a1.id),
            c_direct=await mk(UserRole.CLIENT),
            c_a1=await mk(UserRole.CLIENT, assigned_admin_id=a1.id),
            c_a1_blocked=await mk(
                UserRole.CLIENT, assigned_admin_id=a1.id, status=UserStatus.BLOCKED
            ),
            c_b1=await mk(
                UserRole.CLIENT, assigned_admin_id=a1.id, assigned_broker_id=b1.id,
                broker_ancestry=[b1.id],
            ),
            c_sb1=await mk(
                UserRole.CLIENT, assigned_admin_id=a1.id, assigned_broker_id=sb1.id,
                broker_ancestry=[b1.id, sb1.id],
            ),
            c_a2=await mk(UserRole.CLIENT, assigned_admin_id=a2.id),
            c_b2=await mk(
                UserRole.CLIENT, assigned_admin_id=a2.id, assigned_broker_id=b2.id,
                broker_ancestry=[b2.id],
            ),
            closed_a1=await mk(
                UserRole.CLIENT, assigned_admin_id=a1.id, status=UserStatus.CLOSED
            ),
        )
    finally:
        await client.drop_database(_DB)
        client.close()


def _codes(*users: User) -> set[str]:
    return {u.user_code for u in users}


async def _table(caller: User, entity_id) -> set[str]:
    r = await accounts.entity_users(
        entity_id=str(entity_id), admin=caller, page=1, page_size=100, search=None,
        **_NO_DATES,
    )
    return {row["user_code"] for row in r.data["items"]}


def _a1_pool(w) -> set[str]:
    return _codes(w.c_a1, w.c_a1_blocked, w.c_b1, w.c_sb1)


def _everyone(w) -> set[str]:
    return _a1_pool(w) | _codes(w.c_direct, w.c_a2, w.c_b2)


async def test_admin_and_broker_see_own_pool_and_brokers_in_it(w):
    # D4: the BLOCKED client is a row (status != CLOSED, not == ACTIVE).
    assert await _table(w.a1, w.a1.id) == _a1_pool(w)
    assert await _table(w.a1, w.b1.id) == _codes(w.c_b1, w.c_sb1)
    assert await _table(w.a1, w.sb1.id) == _codes(w.c_sb1)
    assert await _table(w.b1, w.b1.id) == _codes(w.c_b1, w.c_sb1)
    assert await _table(w.b1, w.sb1.id) == _codes(w.c_sb1)


@pytest.mark.parametrize(
    ("caller", "entity"),
    [
        ("b1", "sa"),  # super-admin entity used to mean "whole platform"
        ("b1", "a1"),
        ("b1", "c_a2"),  # any CLIENT id used to mean "whole platform" too
        ("a1", "sa"),
        ("a1", "a2"),
        ("a1", "b2"),
        ("a1", "c_a2"),
        ("a1_emp", "a2"),
    ],
)
async def test_out_of_scope_entity_is_rejected_on_every_endpoint(w, caller, entity):
    caller, eid = getattr(w, caller), str(getattr(w, entity).id)
    calls = [
        accounts.entity_users(eid, caller, page=1, page_size=15, search=None, **_NO_DATES),
        accounts.broker_totals(eid, caller, **_NO_DATES),
        accounts.export_entity_users_excel(eid, caller, user_id=None, **_NO_DATES),
        accounts.export_entity_users_pdf(eid, caller, user_id=None, **_NO_DATES),
        accounts.export_broker_totals_excel(eid, caller, **_NO_DATES),
        accounts.export_broker_totals_pdf(eid, caller, **_NO_DATES),
    ]
    for call in calls:
        with pytest.raises(InsufficientPermissionsError):
            await call


async def test_employee_entity_is_its_parents_pool(w):
    # The All Users tab sends the logged-in employee's own id.
    assert await _table(w.a1_emp, w.a1_emp.id) == _a1_pool(w)
    assert await _table(w.a1_emp, w.a1.id) == _a1_pool(w)
    r = await accounts.broker_totals(str(w.a1_emp.id), w.a1_emp, **_NO_DATES)
    assert r.data["client_count"] == 4
    # trading.py's Total-of-Both cards call _entity_pool_ids(admin.id, role).
    ids = await ads._entity_pool_ids(w.a1_emp.id, UserRole.EMPLOYEE.value)
    assert set(ids) == {u.id for u in (w.c_a1, w.c_a1_blocked, w.c_b1, w.c_sb1)}


async def test_effective_super_admin_names_any_entity(w):
    assert await _table(w.sa, w.sa.id) == _everyone(w)
    assert await _table(w.sa_emp, w.sa_emp.id) == _everyone(w)
    assert await _table(w.sa, w.a2.id) == _codes(w.c_a2, w.c_b2)
    assert await _table(w.sa_emp, w.b2.id) == _codes(w.c_b2)
    # A CLIENT is not an entity: no users, never "all".
    assert await _table(w.sa, w.c_a2.id) == set()
    assert await ads._entity_pool_ids(w.c_a2.id, UserRole.CLIENT.value) == []


async def test_direct_row_lists_no_broker_users_in_callers_pool(w):
    assert await _table(w.sa, "direct") == _codes(w.c_direct, w.c_a1, w.c_a1_blocked, w.c_a2)
    assert await _table(w.a1, "direct") == _codes(w.c_a1, w.c_a1_blocked)
    assert await _table(w.b1, "direct") == set()


async def test_entity_exports_take_direct_and_a_scoped_user_id(w, monkeypatch):
    rows: list[list[dict]] = []
    monkeypatch.setattr(ads, "render_entity_users_excel", lambda _n, d, _l: rows.append(d) or b"")
    monkeypatch.setattr(ads, "render_entity_users_pdf", lambda _n, d, _l: rows.append(d) or b"")

    async def export(fn, caller, entity, user_id=None) -> set[str]:
        await fn(str(entity), caller, user_id=user_id, **_NO_DATES)
        return {r["user_code"] for r in rows.pop()}

    for fn in (accounts.export_entity_users_excel, accounts.export_entity_users_pdf):
        assert await export(fn, w.a1, "direct") == _codes(w.c_a1, w.c_a1_blocked)
        assert await export(fn, w.a1, w.a1.id) == _a1_pool(w)
        assert await export(fn, w.a1, w.a1.id, w.c_b1.id) == _codes(w.c_b1)
        # An id outside the entity's scope returns nothing.
        assert await export(fn, w.a1, w.a1.id, w.c_a2.id) == set()
        assert await export(fn, w.a1, w.b1.id, w.c_a1.id) == set()
        assert await export(fn, w.sa, w.sa.id, w.c_b2.id) == _codes(w.c_b2)


async def test_summary_reads_every_pool_for_super_admins_employee(w):
    r = await accounts.accounts_summary(admin=w.sa_emp, scope="brokers", **_NO_DATES)
    assert r.data["grand_total"]["user_count"] == 7
    assert {e["id"] for e in r.data["entities"]} >= {str(w.b1.id), str(w.sb1.id), str(w.b2.id)}
    # D2: a sub-admin's employee is unchanged.
    r = await accounts.accounts_summary(admin=w.a1_emp, scope="brokers", **_NO_DATES)
    assert r.data["grand_total"]["user_count"] == 4
    assert {e["id"] for e in r.data["entities"]} == {"direct", str(w.b1.id), str(w.sb1.id)}
