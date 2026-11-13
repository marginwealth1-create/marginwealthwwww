"""§6.4 A deleted or reopen-undone fill must stop being reported.

An admin delete flags the position's fills (superseded_by_reopen +
fifo_deleted) and hard-deletes their orders; a reopen flags the closes it
undid (superseded_by_reopen, + superseded_at). The screens that rebuild a
client's book from fills still read every fill: the Active tab FIFO'd an
undone close against the restored lots and fell back to a deleted position's
buy, a re-entry's Charges billed a deleted neighbour's (refunded) close, and
the Orders monitor, dashboard and broker report counted fills whose orders
were gone. One clause (models.trade.LIVE_FILL) is the "still stands" test.

The Orders-monitor header counts fills against a table of Orders: a reopen
keeps the undone close's order, so only a delete (fifo_deleted) leaves the
count there.

Every fill goes through the matching engine; delete, reopen and the screens
are the real handlers. Arbiters: Σ Active rows' M2M == the Position doc's
(ltp − avg) × qty, Charges == what the wallet still holds, and the header ==
the Orders table.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import brokers, dashboard, trading
from app.api.v1.user import positions as user_positions
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import Exchange
from app.models.order import InstrumentRef, Order
from app.models.position import Position
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, matching_engine
from tests._real_fill import INSTRUMENT, later, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_live_fill_queries"
OTHER = InstrumentRef(
    token="TBKG2",
    symbol="TBKO",
    trading_symbol="TBKO-EQ",
    exchange=Exchange.NSE,
    segment="NSE_EQUITY",
    lot_size=1,
)


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    async def _ltp(_token):
        return Decimal("120")

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(trading, "publish", _no_publish)
    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    monkeypatch.setattr(market_data_service, "get_display_ltp", _ltp)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs

    async def mk(code: str, role: UserRole, **kw) -> User:
        u = User(
            user_code=code,
            email=f"{code.lower()}@example.com",
            mobile=f"95{abs(hash(code)) % 10**8:08d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            **kw,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128("1000000")).insert()
        return u

    broker = await mk("LFQBRK", UserRole.BROKER)
    try:
        yield SimpleNamespace(
            db=client[_DB],
            c=await mk("LFQ001", UserRole.CLIENT, assigned_broker_id=broker.id),
            sa=await mk("LFQSA", UserRole.SUPER_ADMIN),
            broker=broker,
        )
    finally:
        await client.drop_database(_DB)
        client.close()


async def _positions(user: User) -> list[Position]:
    return sorted(await Position.find(Position.user_id == user.id).to_list(), key=lambda p: p.opened_at)


_LEDGER = (
    TransactionType.PNL,
    TransactionType.CHARGES,
    TransactionType.REVERSAL,
    TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY,
)


async def _ledger(user: User) -> Decimal:
    """What trading left in the wallet: P&L, charges and the undos' reversals."""
    rows = await WalletTransaction.find(
        WalletTransaction.user_id == user.id, {"transaction_type": {"$in": [t.value for t in _LEDGER]}}
    ).to_list()
    return sum((Decimal(str(r.amount)) for r in rows), Decimal(0))


async def test_a_reopened_position_shows_its_own_lots_on_the_active_tab(w):
    f1 = await market(w.c, "BUY", 10, "100")
    await later(w.db, minutes=1)
    await market(w.c, "SELL", 10, "90")  # P stopped out
    await later(w.db, minutes=5)
    await market(w.c, "BUY", 5, "130")  # Q
    await later(w.db, minutes=1)
    await market(w.c, "SELL", 5, "135")
    await later(w.db, minutes=5)
    p, q = await _positions(w.c)
    await trading.delete_position(str(q.id), w.sa, None)
    await later(w.db, minutes=1)
    await trading.admin_reopen_position(str(p.id), w.sa, None)
    p = await Position.get(p.id)

    # The undone close no longer eats the restored lots, and Q's deleted buy is
    # not the fallback's "newest fill": was Q-BUY 5 @130 + F1 5 @100 (M2M 50).
    rows = (await user_positions.list_active_trades(w.c)).data
    assert [(r["id"], r["quantity"]) for r in rows] == [(str(f1.id), 10.0)]
    m2m = sum((Decimal(r["pnl"]) + Decimal(r["brokerage"]) for r in rows), Decimal(0))
    assert m2m == (Decimal("120") - Decimal(str(p.avg_price))) * Decimal(str(p.quantity)) == Decimal("200")

    # Sizing a per-fill SL/TP and its fire-time guard: F1 is open again (was 0).
    assert await user_positions._fill_open_qty(w.c.id, f1) == 10.0
    exit_order = SimpleNamespace(sl_tp_source_trade_id=f1.id, user_id=w.c.id, instrument=INSTRUMENT)
    assert await matching_engine._exit_order_fill_open_qty(exit_order) == 10.0


async def test_a_reentry_is_not_charged_a_deleted_neighbours_close(w):
    await market(w.c, "BUY", 5, "100")  # Q
    await later(w.db, minutes=1)
    await market(w.c, "SELL", 5, "110")
    await later(w.db, seconds=3)
    await market(w.c, "BUY", 10, "120")  # P, re-entered 3 s after Q closed
    q, p = await _positions(w.c)
    await trading.delete_position(str(q.id), w.sa, None)

    # P's charge is its own open leg — all the wallet still holds. Was 15:
    # Q's refunded close sits inside P's 5 s slack.
    assert await _ledger(w.c) == Decimal("-10")
    [mine] = (await user_positions.open_positions(w.c)).data
    assert Decimal(mine["charges"]) == Decimal("10")
    [row] = (
        await trading.list_positions(
            w.sa,
            user_id=str(w.c.id),
            status="OPEN",
            q=None,
            product=None,
            from_date=None,
            to_date=None,
            max_hold_sec=None,
            own_scope=False,
            page=None,
            page_size=25,
            _=None,
        )
    ).data
    assert row["id"] == str(p.id) and Decimal(row["charges"]) == Decimal("10")


async def _deleted_and_reopened(w) -> None:
    """P on INSTRUMENT reopened (its close order stays), Q on OTHER deleted
    (its orders go). No clock shift: the Orders monitor counts today's fills."""
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 10, "110")
    await market(w.c, "BUY", 5, "100", instrument=OTHER)
    await market(w.c, "SELL", 5, "90", instrument=OTHER)
    p, q = sorted(await _positions(w.c), key=lambda x: x.instrument.token != INSTRUMENT.token)
    await trading.delete_position(str(q.id), w.sa, None)
    await trading.admin_reopen_position(str(p.id), w.sa, None)


async def test_the_orders_monitor_header_counts_the_orders_table(w):
    await _deleted_and_reopened(w)

    # Q's orders are gone, P's undone SELL still shows. Was 4 / 2 / 2.
    stats = (await trading.orders_stats(w.sa, None)).data
    assert await Order.find(Order.user_id == w.c.id).count() == 2
    assert (stats["total_trades"], stats["buy_trades"], stats["sell_trades"]) == (2, 1, 1)


async def test_counts_and_revenue_leave_deleted_and_undone_fills(w):
    await _deleted_and_reopened(w)

    # What stands: P's open leg (₹10), all the wallet still holds.
    assert await _ledger(w.c) == Decimal("-10")
    trading_card = (await dashboard.stats(w.sa)).data["trading"]
    assert (trading_card["trades_today"], trading_card["today_revenue"]) == (1, 10.0)  # was 4, 30.0
    fills = (
        await trading.list_trades(w.sa, None, user_id=str(w.c.id), limit=200, from_dt=None, to_dt=None)
    ).data
    assert len(fills) == 1  # was 4
    report = (await brokers.broker_report(str(w.broker.id), w.sa)).data
    assert report["trades"] == {"today": 1, "this_week": 1, "all_time": 1}  # was 4 each
    assert len(report["recent_trades"]) == 1
