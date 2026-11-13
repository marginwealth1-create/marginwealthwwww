"""Admin read surfaces cover every pool for the effective super-admin (D1)
while ADMIN / BROKER scope and every write stay as strict as before (D2).
Also: a sub-admin's EMPLOYEE logins never show up as client rows."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from beanie import PydanticObjectId, init_beanie
from bson import Decimal128
from fastapi import FastAPI, HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import notifications as notif_api
from app.api.v1.admin import reports, trading
from app.api.v1.admin import users as users_api
from app.core.config import settings
from app.core.dependencies import get_current_admin
from app.core.exceptions import InsufficientPermissionsError
from app.models.notification import Notification
from app.models.order import InstrumentRef, Order
from app.models.position import Position, PositionStatus
from app.models.trade import Trade
from app.models.transaction import DepositRequest, WalletTransaction, WithdrawalRequest
from app.models.user import AdminPermissions, User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.schemas.admin.common import BlockUserRequest
from app.utils.time_utils import now_utc

_DB = f"{settings.MONGODB_DB_NAME}_test_scope_admin_consumers"
_GOLD = InstrumentRef(token="G1", symbol="GOLD", exchange="MCX", segment="MCX")


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(
        database=client[_DB],
        document_models=[
            User,
            Trade,
            Order,
            Position,
            Wallet,
            WalletTransaction,
            DepositRequest,
            WithdrawalRequest,
            Notification,
        ],
    )

    def _no_redis(*a, **k):
        raise RuntimeError("redis disabled in tests")

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.get_redis", _no_redis)
    monkeypatch.setattr("app.core.redis_client.publish_batch", _no_publish)
    n = 0

    async def mk(role: UserRole, **kw) -> User:
        nonlocal n
        n += 1
        u = User(
            user_code=f"SAC{n:03d}",
            email=f"sac{n}@example.com",
            mobile=f"97000{n:05d}",
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
            demo_direct=await mk(UserRole.CLIENT, is_demo=True),
            closed_a2=await mk(
                UserRole.CLIENT, assigned_admin_id=a2.id, status=UserStatus.CLOSED
            ),
        )
        # Three clients from three pools BUY GOLD inside one 60 s bucket.
        t = now_utc().replace(second=1, microsecond=0) - timedelta(minutes=5)
        for i, c in enumerate((w.c_direct, w.c_a1, w.c_a2), start=1):
            await Trade(
                trade_number=f"T{i}",
                order_id=PydanticObjectId(),
                user_id=c.id,
                instrument=_GOLD,
                action="BUY",
                product_type="MIS",
                quantity=1,
                price=Decimal128("101"),
                value=Decimal128("101"),
                executed_at=t,
                pnl_inr=Decimal128(str(100 * i)),
                expected_price=Decimal128("100"),
                markup=Decimal128("1"),
                fill_latency_ms=5,
            ).insert()
            await Wallet(user_id=c.id, total_deposits=Decimal128("1000")).insert()
        for c, qty in ((w.c_direct, 10), (w.c_a1, 30)):
            await Position(
                user_id=c.id,
                instrument=_GOLD,
                segment_type="MCX",
                product_type="MIS",
                quantity=qty,
                status=PositionStatus.OPEN,
            ).insert()
        yield w
    finally:
        await client.drop_database(_DB)
        client.close()


def _ids(*users: User) -> set[str]:
    return {str(u.id) for u in users}


# ── §1 billing / tradebook PDF gate ──────────────────────────────────


async def test_tradebook_pdf_super_admin_reads_any_pool(w):
    for actor, target in ((w.sa, w.c_a1), (w.sa_emp, w.c_a2), (w.sa, w.c_direct)):
        resp = await reports.tradebook_pdf(str(target.id), actor, None)
        assert resp.media_type == "application/pdf"
    # Non-super-admins keep today's scoped_user_ids gate (incl. broker subtree).
    assert (await reports.tradebook_pdf(str(w.c_b1.id), w.a1, None)).media_type
    for actor, target in ((w.a2, w.c_a1), (w.a1_emp, w.c_a2), (w.b1, w.c_a1)):
        with pytest.raises(InsufficientPermissionsError):
            await reports.tradebook_pdf(str(target.id), actor, None)


# ── §1 execution reports ─────────────────────────────────────────────


async def test_execution_reports_cover_every_pool(w):
    for actor in (w.sa, w.sa_emp):
        sim = (await reports.similar_activity(actor, 1, 60, 3, 50, None)).data
        assert [c["user_count"] for c in sim["clusters"]] == [3]
        slip = (await reports.slippage_report(actor, 7, "user", 50, None)).data
        assert slip["totals"]["fills"] == 3
        lat = (await reports.latency_report(actor, 7, 1000, 50, None)).data
        assert {r["user_id"] for r in lat["rows"]} == _ids(w.c_direct, w.c_a1, w.c_a2)
        top = (await reports.top_movers(actor, 1, 10, False, None)).data
        assert {r["user_id"] for r in top["clients"]["gainers"]} == _ids(
            w.c_direct, w.c_a1, w.c_a2
        )
    # Sub-admin still sees only its own pool.
    assert (await reports.similar_activity(w.a2, 1, 60, 3, 50, None)).data["clusters"] == []
    slip = (await reports.slippage_report(w.a2, 7, "user", 50, None)).data
    assert slip["totals"]["fills"] == 1


# ── §1 summary reports ───────────────────────────────────────────────


async def test_summary_reports_cover_every_pool(w):
    assert (await reports.users_report(w.sa, None)).data["total"] == 4
    assert (await reports.users_report(w.a2, None)).data["total"] == 1
    assert (await reports.trades_report(w.sa, None)).data["week"]["count"] == 3
    assert (await reports.trades_report(w.a2, None)).data["week"]["count"] == 1
    fin = (await reports.financial_report(w.sa_emp, None)).data
    assert fin["total_deposits"] == 3000
    comp = (await reports.compliance_report(w.sa, None)).data
    assert comp["kyc_verified"] + comp["kyc_pending"] == 4


# ── §1 Users page ────────────────────────────────────────────────────


async def _list(actor, **kw):
    args = {"q": None, "role": None, "status": None, "parent_id": None, "mode": "live"}
    args.update(page=1, page_size=50, **kw)
    return (await users_api.list_users(actor, _=None, **args)).data


async def test_list_users_super_admin_sees_every_pool_writes_flagged(w):
    for actor in (w.sa, w.sa_emp):
        data = await _list(actor)
        rows = {r["id"]: r for r in data["items"]}
        assert set(rows) == _ids(w.c_direct, w.c_a1, w.c_b1, w.c_a2)
        assert data["meta"]["total"] == 4
        # Only the super-admin's own (unassigned) rows are writable.
        assert {i for i, r in rows.items() if not r["read_only"]} == _ids(w.c_direct)


async def test_list_users_excludes_employees_and_keeps_admin_scope(w):
    data = await _list(w.a1)
    assert {r["id"] for r in data["items"]} == _ids(w.c_a1, w.c_b1)
    assert not any(r.get("read_only") for r in data["items"])
    assert {r["id"] for r in (await _list(w.a1_emp))["items"]} == _ids(w.c_a1, w.c_b1)
    assert {r["id"] for r in (await _list(w.a2))["items"]} == _ids(w.c_a2)
    with pytest.raises(HTTPException):
        await _list(w.a1, role=UserRole.EMPLOYEE.value)


async def test_live_stats_matches_the_list(w):
    async def stats(actor, ids=None):
        resp = await users_api.users_live_stats(actor, ids, None)
        return {r["user_id"] for r in resp.data["items"]}

    every = _ids(w.c_direct, w.c_a1, w.c_b1, w.c_a2)
    assert await stats(w.sa, ",".join(every)) == every
    assert await stats(w.sa_emp) == every  # no demo, closed or staff rows
    assert await stats(w.a1) == _ids(w.c_a1, w.c_b1)
    # Sub-admin cannot read another pool by sending its ids.
    assert await stats(w.a2, f"{w.c_a1.id},{w.c_a2.id}") == _ids(w.c_a2)
    # Explicit ids never reach admin-tier / staff rows, in any pool.
    assert await stats(w.sa, f"{w.a2.id},{w.b1.id},{w.a1_emp.id}") == set()


async def test_drilldown_reads_widen_writes_stay_strict(w):
    assert (await users_api.get_user(str(w.c_a1.id), w.sa, None)).data["id"] == str(w.c_a1.id)
    assert (await users_api.live_trade_stats(str(w.c_a2.id), w.sa_emp)).data is not None
    with pytest.raises(InsufficientPermissionsError):
        await users_api.get_user(str(w.c_a1.id), w.a2, None)
    with pytest.raises(InsufficientPermissionsError):
        await users_api.block(str(w.c_a1.id), BlockUserRequest(), w.sa, None)


async def test_live_trade_stats_needs_users_grant(w):
    app = FastAPI()
    app.include_router(users_api.router)
    app.dependency_overrides[get_current_admin] = lambda: w.sa_emp
    url = f"/users/{w.c_a2.id}/live-trade-stats"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        with pytest.raises(InsufficientPermissionsError):
            await c.get(url)
        w.sa_emp.admin_permissions = AdminPermissions(users=True)
        assert (await c.get(url)).status_code == 200


# ── §1 sweep: house exposure, pnl cards, broadcast ─────────────────────────


async def test_house_exposure_folds_every_pool(w):
    rows = (await trading.house_exposure(w.sa, None, None)).data["rows"]
    assert [r["net_qty"] for r in rows] == [40]
    assert (await trading.house_exposure(w.a2, None, None)).data["rows"] == []


async def test_pnl_summary_cards_match_table_for_super_admins_staff(w):
    own = (await trading.positions_pnl_summary(w.sa, None, True, None)).data
    staff = (await trading.positions_pnl_summary(w.sa_emp, None, False, None)).data
    # Same direct book as the super-admin's own page (and the staff's table):
    # the direct client's booked close (100), not a1's 200 or a2's 300.
    assert own["week_net_client_pnl"] == staff["week_net_client_pnl"] == 100


async def test_broadcast_stays_in_direct_pool(w):
    # A broadcast WRITES into other users' inboxes, so it stays own-pool (D1).
    assert (await notif_api.broadcast_recipient_count(w.sa, None)).data["count"] == 1
    assert (await notif_api.broadcast_recipient_count(w.a1, None)).data["count"] == 2
    body = notif_api.BroadcastBody(title="t", message="m")
    assert (await notif_api.broadcast_notification(body, w.sa_emp, None)).data["count"] == 1
    assert {str(n.user_id) for n in await Notification.find_all().to_list()} == _ids(
        w.c_direct
    )
