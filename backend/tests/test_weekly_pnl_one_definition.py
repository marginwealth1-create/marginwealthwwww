"""§5 / §8 One weekly P&L: every Today / This week / Last week figure is the
client's Billing statement for that window.

Before, five definitions sat side by side: the user's cards summed Trade.pnl_inr
and took the close's brokerage off a second time, kept fills an admin had
undone, missed every settlement and folded floating M2M in, over a week that
started on Sunday; the admin Users column summed the ledger (so a stop-out
showed only the cash part of its loss); the action menu and the sub-admin /
broker cards summed gross realized_pnl of positions CLOSED in the window (a
partial close of a position still open was nowhere); the Trades report used a
rolling 24 h / 7 days.

Now each reads tradebook_builder.booked over tradebook_builder.periods(): the
IST day, and the IST week Monday 00:00 -> Sunday 23:59:59.999. One client, one
realistic fortnight, every order through the matching engine.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import accounts as accounts_api
from app.api.v1.admin import brokers, management, reports, trading
from app.api.v1.admin import users as users_api
from app.api.v1.user import dashboard as dashboard_api
from app.api.v1.user import positions as positions_api
from app.core.config import settings
from app.core.database import _document_models
from app.core.dependencies import scoped_user_ids
from app.models._base import Exchange, ProductType
from app.models.order import InstrumentRef
from app.models.position import Position
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import accounts_dashboard_service as ads
from app.services import market_data_service, tradebook_builder
from app.utils.time_utils import IST, now_utc, to_ist
from tests._real_fill import INSTRUMENT, later, market, set_commission_per_lot, settle_weekly

_DB = f"{settings.MONGODB_DB_NAME}_test_weekly_pnl_one_definition"
FUT = INSTRUMENT.model_copy(update={"token": "TWP1", "symbol": "TWPFUT"})
ROUND = INSTRUMENT.model_copy(update={"token": "TWP2", "symbol": "TWPROUND"})
HELD = INSTRUMENT.model_copy(update={"token": "TWP3", "symbol": "TWPHELD"})
GONE = INSTRUMENT.model_copy(update={"token": "TWP4", "symbol": "TWPGONE"})
STOP = INSTRUMENT.model_copy(update={"token": "TWP5", "symbol": "TWPSTOP"})
BTC = InstrumentRef(
    token="TWP6",
    symbol="TWPBTC",
    trading_symbol="TWPBTC",
    exchange=Exchange.CRYPTO,
    segment="CRYPTO_SPOT",
    lot_size=1,
)


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(trading, "publish", _no_publish)
    monkeypatch.setitem(market_data_service._usd_inr_cache, "rate", 90.0)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs (NSE_EQ)
    n = 0

    async def mk(role: UserRole, **kw) -> User:
        nonlocal n
        n += 1
        u = User(
            user_code=f"TWP{n:03d}",
            email=f"twp{n}@example.com",
            mobile=f"93700{n:05d}",
            full_name=f"TWP{n}",
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            **kw,
        )
        await u.insert()
        return u

    try:
        sa = await mk(UserRole.SUPER_ADMIN)
        a1 = await mk(UserRole.ADMIN)
        b1 = await mk(UserRole.BROKER, assigned_admin_id=a1.id)
        # auto_settlement: a loss past the balance floors it at 0 and books the
        # rest as settlement outstanding (the stop-out shortfall path).
        c = await mk(
            UserRole.CLIENT,
            assigned_admin_id=a1.id,
            assigned_broker_id=b1.id,
            broker_ancestry=[b1.id],
            auto_settlement=True,
        )
        await Wallet(user_id=c.id, available_balance=Decimal128("1000")).insert()
        yield SimpleNamespace(db=client[_DB], sa=sa, a1=a1, b1=b1, c=c, monkeypatch=monkeypatch)
    finally:
        await client.drop_database(_DB)
        client.close()


def _d(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.01"))


async def _statement(u: User, lo, hi) -> tuple[Decimal, Decimal, Decimal]:
    fin = (await tradebook_builder.statement_payload(u, lo, hi))["financial"]
    return _d(fin["gross_pnl"]), _d(fin["brokerage"]), _d(fin["net_pnl"])


async def _fortnight(w) -> None:
    """Last week and this week for client `w.c`, every order a real fill."""
    c = w.c
    wk0 = tradebook_builder.periods()["week"][0]  # this Monday 00:00 IST
    # When each step happens: last Wednesday, the Saturday settlement, last
    # Sunday evening, this Monday just after midnight, now.
    steps = [wk0 - timedelta(days=5, hours=-4), wk0 - timedelta(days=2, hours=-5), wk0 - timedelta(hours=6)]
    steps += [min(wk0 + timedelta(minutes=5), now_utc() - timedelta(minutes=1)), now_utc()]
    gaps = [b - a for a, b in zip(steps, steps[1:], strict=False)]

    # Last Wednesday: an NRML future bought.
    await market(c, "BUY", 10, "100", instrument=FUT, product=ProductType.NRML)
    await later(w.db, seconds=gaps[0].total_seconds())
    # Saturday: the weekly settlement books +50 and re-opens the 10 at 105.
    await settle_weekly(c, w.monkeypatch, "105")
    await later(w.db, seconds=gaps[1].total_seconds())
    # Sunday evening: a crypto round trip — last week's, not this week's.
    await market(c, "BUY", 1, "100", instrument=BTC)
    await market(c, "SELL", 1, "102", instrument=BTC)
    await later(w.db, seconds=gaps[2].total_seconds())
    # This Monday 00:05 IST: an intraday round trip, -50.
    await market(c, "BUY", 5, "200", instrument=ROUND)
    await market(c, "SELL", 5, "190", instrument=ROUND)
    await later(w.db, seconds=gaps[3].total_seconds())
    # Today: the carried future closes (+70 off the settle price), a partial
    # close of a position still open (+40, 6 units left floating), a closed
    # round trip the admin deletes, and a stop-out whose -3,000 is more than
    # the wallet holds.
    await market(c, "SELL", 10, "112", instrument=FUT, product=ProductType.NRML)
    await market(c, "BUY", 10, "100", instrument=HELD)
    await market(c, "SELL", 4, "110", instrument=HELD)
    await market(c, "BUY", 5, "200", instrument=GONE)
    await market(c, "SELL", 5, "230", instrument=GONE)
    gone = await Position.find_one(Position.user_id == c.id, {"instrument.token": GONE.token})
    await trading.delete_position(str(gone.id), w.a1, None)
    await market(c, "BUY", 100, "100", instrument=STOP)
    await market(c, "SELL", 100, "70", instrument=STOP, squareoff=True)
    assert Decimal(str((await Wallet.find_one(Wallet.user_id == c.id)).settlement_outstanding)) > 0


async def test_every_card_reads_the_billing_statement(w):
    c = w.c
    await _fortnight(w)
    periods = tradebook_builder.periods()
    st = {k: await _statement(c, lo, hi) for k, (lo, hi) in periods.items()}
    assert st["last_week"][0] > Decimal("50")  # the settlement + the Sunday crypto close
    assert st["week"][0] < Decimal("-2800")  # the whole stop-out loss, not the cash part

    # ── the client's own cards ──
    positions_api._PNL_SUMMARY_CACHE.clear()
    mine = (await positions_api.positions_pnl_summary(c)).data
    assert _d(mine["today_pnl"]) == _d(mine["today_realised"]) == st["today"][2]
    assert _d(mine["week_pnl"]) == _d(mine["week_realised"]) == st["week"][2]
    assert _d(mine["last_week_pnl"]) == st["last_week"][2]
    assert mine["open_unrealised"] != 0  # floating is its own field, in no period figure
    assert _d((await dashboard_api.summary(c)).data["today_pnl"]) == st["today"][2]

    # ── admin Positions cards (and the summary's own realised fields) ──
    trading._ADMIN_PNL_CACHE.clear()
    adm = (await trading.positions_pnl_summary(w.a1, str(c.id), False, None)).data
    assert _d(adm["today_pnl"]) == _d(adm["today_realised"]) == st["today"][2]
    assert _d(adm["week_pnl"]) == _d(adm["week_realised"]) == st["week"][2]
    assert _d(adm["last_week_pnl"]) == st["last_week"][2]
    for k in ("week", "last_week"):
        g, b, net = st[k]
        assert (_d(adm[f"{k}_net_client_pnl"]), _d(adm[f"{k}_brokerage"])) == (g, b)
        assert _d(adm[f"{k}_total_of_both"]) == -net

    # ── admin Users list column and the row's action menu ──
    rows = (
        await users_api.list_users(
            w.a1, _=None, q=None, role=None, status=None, parent_id=None, mode="live", page=1, page_size=50
        )
    ).data["items"]
    [row] = [r for r in rows if r["id"] == str(c.id)]
    assert _d(row["wallet"]["weekly_closed_pnl"]) == st["week"][2]
    assert _d((await users_api.live_trade_stats(str(c.id), w.a1)).data["weekly_net_pnl"]) == st["week"][2]

    # ── sub-admin and broker detail pages: gross realised ──
    for rep in (
        (await management.sub_admin_report(str(w.a1.id), w.sa)).data,
        (await brokers.broker_report(str(w.b1.id), w.a1)).data,
    ):
        assert _d(rep["pnl"]["today_realised"]) == st["today"][0]
        assert _d(rep["pnl"]["week_realised"]) == st["week"][0]

    # ── Trades report: the IST day and week, brokerage as billed ──
    tr = (await reports.trades_report(w.a1, None)).data
    assert _d(tr["today"]["brokerage"]) == st["today"][1]
    assert _d(tr["week"]["brokerage"]) == st["week"][1]

    # ── accounts dashboard row ──
    for k, (lo, hi) in periods.items():
        [acc] = (await ads.get_entity_users(None, "SUPER_ADMIN", lo, hi, user_id=c.id))["items"]
        assert (_d(acc["net_pnl"]), _d(acc["net_bkg"]), _d(acc["total_pnl"])) == st[k], k

    # ── accounts dashboard CSV (/accounts/summary): Realized P&L and Brokerage
    # are the statement's Gross and Brokerage — the partial close of a position
    # still open, the USD crypto close in rupees, the settlement — lifetime too.
    st["lifetime"] = await _statement(c, None, None)
    dates = {k: (_ist(lo), _ist(hi)) for k, (lo, hi) in periods.items()} | {"lifetime": (None, None)}
    for k, (f, t) in dates.items():
        ents = (await accounts_api.accounts_summary(w.sa, "all_users", f, t, None, None)).data["entities"]
        [e] = [e for e in ents if e["id"] == str(c.id)]
        assert (_d(e["realized_pnl"]), _d(e["brokerage"])) == st[k][:2], k


def _ist(dt: datetime) -> str:
    return to_ist(dt).date().isoformat()


async def _mk(n: int, role: UserRole = UserRole.CLIENT, **kw) -> User:
    u = User(user_code=f"TWP{n:03d}", email=f"twp{n}@example.com", mobile=f"93700{n:05d}",
             full_name=f"TWP{n}", password_hash="x", role=role, status=UserStatus.ACTIVE, **kw)
    await u.insert()
    await Wallet(user_id=u.id, available_balance=Decimal128("10000")).insert()
    return u


async def _transferred_broker(w) -> SimpleNamespace:
    """b1 came over to a1 from a0: its client c3 and its sub-broker sb's client
    c5 still say a0. Under b1 also a demo client and a deleted (CLOSED) one.
    Each client books one closed round trip today: c +10, c3 +20, c5 +30,
    demo +50, gone +70."""
    a1, b1 = w.a1, w.b1
    a0 = await _mk(91, UserRole.ADMIN)
    c3 = await _mk(92, assigned_admin_id=a0.id, assigned_broker_id=b1.id, broker_ancestry=[b1.id])
    sb = await _mk(
        93, UserRole.BROKER, assigned_admin_id=a0.id, assigned_broker_id=b1.id, broker_ancestry=[b1.id]
    )
    c5 = await _mk(94, assigned_admin_id=a0.id, assigned_broker_id=sb.id, broker_ancestry=[b1.id, sb.id])
    under_b1 = {"assigned_admin_id": a1.id, "assigned_broker_id": b1.id, "broker_ancestry": [b1.id]}
    demo = await _mk(95, is_demo=True, **under_b1)
    gone = await _mk(96, **under_b1)
    for u, gain in ((w.c, 10), (c3, 20), (c5, 30), (demo, 50), (gone, 70)):
        inst = INSTRUMENT.model_copy(update={"token": f"TWPE{gain}", "symbol": f"TWPE{gain}"})
        await market(u, "BUY", 1, "100", instrument=inst)
        await market(u, "SELL", 1, str(100 + gain), instrument=inst)
    await User.get_motor_collection().update_one({"_id": gone.id}, {"$set": {"status": "CLOSED"}})
    return SimpleNamespace(a0=a0, c3=c3, sb=sb, c5=c5, demo=demo, gone=gone)


async def test_an_entity_reads_one_pool_on_every_screen(w):
    """An admin's or a broker's figure covers the same clients on every screen:
    demo and deleted (CLOSED) clients nowhere, and the clients of a broker
    transferred to this admin (their rows keep the old assigned_admin_id,
    sub-broker clients included) everywhere the admin's Positions table shows
    them — not on the table and the Accounts rows but missing from the KPI and
    the Close PNL card."""
    a1, b1, c = w.a1, w.b1, w.c
    x = await _transferred_broker(w)
    c3, c5 = x.c3, x.c5

    periods = tradebook_builder.periods()
    pool = await scoped_user_ids(a1)  # a1's Positions table and Users list
    assert set(pool) == {c.id, c3.id, c5.id}
    users = {u.id: u for u in (c, c3, c5)}

    async def total(ids, k: str) -> list[Decimal]:
        sts = [await _statement(users[i], *periods[k]) for i in ids]
        return [sum((s[j] for s in sts), Decimal(0)) for j in range(3)]

    for k in ("today", "week"):
        g, b, _net = await total(pool, k)
        assert g == Decimal("60")  # c +10, c3 +20, c5 +30
        f, t = _ist(periods[k][0]), _ist(periods[k][1])
        for actor in (w.sa, a1):  # the super-admin picking a1, and a1 on its own page
            kpi = (await accounts_api.broker_totals(str(a1.id), actor, f, t, None, None)).data
            assert (_d(kpi["net_client_pnl"]), _d(kpi["net_client_bkg"])) == (g, b), (k, actor.role)
            rows = (await accounts_api.entity_users(str(a1.id), actor, f, t, None, 1, 100, None, None)).data
            assert {r["user_id"] for r in rows["items"]} == {str(i) for i in pool}, (k, actor.role)
        assert _d((await management.sub_admin_report(str(a1.id), w.sa)).data["pnl"][f"{k}_realised"]) == g, k
        # b1's detail page counts its direct clients (c, c3); demo and deleted ones out.
        bg = (await total([c.id, c3.id], k))[0]
        assert _d((await brokers.broker_report(str(b1.id), a1)).data["pnl"][f"{k}_realised"]) == bg, k

    trading._ADMIN_PNL_CACHE.clear()
    adm = (await trading.positions_pnl_summary(a1, None, False, None)).data
    week = [_d(adm[f]) for f in ("week_net_client_pnl", "week_brokerage", "week_pnl")]
    assert week == await total(pool, "week")
    assert _d(adm["today_pnl"]) == (await total(pool, "today"))[2]


async def test_the_accounts_csv_reads_each_cards_pool(w):
    """/accounts/summary (the Accounts CSV): an admin's row (Admins scope) and a
    broker's row (Brokers / Sub-Brokers scopes) cover the clients that entity's
    on-screen card covers — a transferred broker's whole subtree, sub-broker
    clients included, demo and deleted clients out — and the grand total is the
    page's own KPI."""
    a1, b1 = w.a1, w.b1
    x = await _transferred_broker(w)
    lo, hi = tradebook_builder.periods()["week"]
    f, t = _ist(lo), _ist(hi)

    async def card(entity: User, actor: User) -> tuple:
        k = (await accounts_api.broker_totals(str(entity.id), actor, f, t, None, None)).data
        return _d(k["net_client_pnl"]), _d(k["net_client_bkg"]), k["client_count"]

    def row(e: dict) -> tuple:
        return _d(e["realized_pnl"]), _d(e["brokerage"]), e["user_count"]

    for actor, scopes in (
        (w.sa, {"admins": (a1, x.a0), "brokers": (b1,), "sub_brokers": (x.sb,)}),
        (a1, {"brokers": (b1,), "sub_brokers": (x.sb,)}),
    ):
        for scope, entities in scopes.items():
            data = (await accounts_api.accounts_summary(actor, scope, f, t, None, None)).data
            got = {e["id"]: e for e in data["entities"]}
            for ent in entities:
                assert row(got[str(ent.id)]) == await card(ent, actor), (scope, ent.user_code, actor.role)
            assert row(data["grand_total"]) == await card(actor, actor), (scope, actor.role)
    assert (await card(a1, w.sa))[0] == Decimal("60")  # c, c3, c5


async def test_all_time_realised_is_the_lifetime_statement(w):
    """The sub-admin and broker pages' All-time realised and the action menu's
    closed_pnl_all_time: the statement's lifetime Gross over the pool their
    Today / This week cards read — demo and deleted clients out, a partial
    close of a position still open in."""
    a1, b1, c = w.a1, w.b1, w.c
    x = await _transferred_broker(w)
    await market(c, "BUY", 10, "100", instrument=HELD)
    await market(c, "SELL", 4, "110", instrument=HELD)  # +40, 6 units still open

    async def life(*us: User) -> Decimal:
        return sum([(await _statement(u, None, None))[0] for u in us], Decimal(0))

    assert await life(c) == Decimal("50")
    sub = (await management.sub_admin_report(str(a1.id), w.sa)).data["pnl"]
    assert _d(sub["all_time_realised"]) == await life(c, x.c3, x.c5)
    brk = (await brokers.broker_report(str(b1.id), a1)).data["pnl"]
    assert _d(brk["all_time_realised"]) == await life(c, x.c3)  # its direct clients, as its other cards
    stats = (await users_api.live_trade_stats(str(c.id), a1)).data
    assert _d(stats["closed_pnl_all_time"]) == await life(c)


async def test_the_super_admins_whole_book_cards_read_one_pool(w):
    """pnl-summary for the super-admin without own_scope (the prefetch): the
    floating M2M covers the same clients as the week / today figures — the
    whole book, a sub-admin's clients and the super-admin's own direct ones."""
    c = w.c
    d0 = await _mk(97)  # the super-admin's own client
    await market(c, "BUY", 10, "100", instrument=HELD)
    await market(d0, "BUY", 5, "200", instrument=ROUND)
    await market(d0, "SELL", 2, "210", instrument=ROUND)  # +20, 3 units still open

    async def _ltp(_token):
        return Decimal("105")

    w.monkeypatch.setattr(market_data_service, "get_display_ltp", _ltp)
    trading._ADMIN_PNL_CACHE.clear()
    s = (await trading.positions_pnl_summary(w.sa, None, False, None)).data
    assert _d(s["open_unrealised"]) == Decimal("-235")  # c (105 − 100) × 10, d0 (105 − 200) × 3
    week = [await _statement(u, *tradebook_builder.periods()["week"]) for u in (c, d0)]
    assert _d(s["week_net_client_pnl"]) == sum((g for g, _, _ in week), Decimal(0))
    assert _d(s["week_pnl"]) == sum((n for _, _, n in week), Decimal(0))


def test_the_week_runs_monday_to_sunday_ist():
    sunday_night = datetime(2026, 11, 15, 23, 30, tzinfo=IST)  # Sunday
    p = tradebook_builder.periods(sunday_night)
    assert p["week"][0] == datetime(2026, 11, 9, tzinfo=IST)  # Monday
    sunday_end = datetime(2026, 11, 15, 23, 59, 59, tzinfo=IST)
    assert p["week"][1].astimezone(IST).replace(microsecond=0) == sunday_end
    assert p["last_week"][0] == datetime(2026, 11, 2, tzinfo=IST)
    assert p["last_week"][1] < p["week"][0]
    assert p["today"][0] == datetime(2026, 11, 15, tzinfo=IST)
