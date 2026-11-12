"""§2.5 A deleted position's opening lots must leave the cost book.

An admin delete flagged every fill in the position's window
`superseded_by_reopen`, which only hides the rows the walk emits. The bare
opening legs stayed queued in the FIFO book, and the next close on the script
paired them: split rows, the wrong opened_at, units stranded for good. The
same delete refunded one closing fill's brokerage (partial closes stayed
charged while every statement hid them), refunded a quick re-entry's opening
charge (±15s), and its window — starting at the FIRST opened_at, which a
reopen keeps — swallowed a whole position traded between the close and the
reopen. A weekly settlement's re-seed of a deleted successor stayed queued.

Every fill goes through the matching engine; the delete, the reopen and the
settlement are the real code paths. Time passes by moving every stored clock
back (`_later`). The arbiter is the flat-book oracle over the fills that still
stand: Σ(sell value) − Σ(buy value) == Σ Position.realized_pnl (what the
wallet booked) == Σ blotter gross.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import trading
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import OrderAction
from app.models.order import Order
from app.models.position import Position, PositionStatus
from app.models.position_settlement import SettlementBatch
from app.models.trade import Trade
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service, weekly_settlement_service
from app.utils.time_utils import now_utc
from tests._real_fill import market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_deleted_fill_leaves_the_book"
CENT = Decimal("0.01")
START = Decimal("1000000")

_CLOCKS = {
    "trades": ("executed_at", "created_at"),
    "positions": ("opened_at", "closed_at", "reopened_at", "created_at"),
    "orders": ("created_at", "executed_at"),
    "wallet_transactions": ("created_at",),
    "position_settlements": ("settled_at", "created_at"),
}


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(trading, "publish", _no_publish)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs

    async def mk(code: str, role: UserRole) -> User:
        u = User(
            user_code=code,
            email=f"{code.lower()}@example.com",
            mobile=f"93{abs(hash(code)) % 10**8:08d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128(str(START))).insert()
        return u

    try:
        db = client[_DB]
        yield SimpleNamespace(
            db=db,
            c=await mk("DEL001", UserRole.CLIENT),
            sa=await mk("DELSA", UserRole.SUPER_ADMIN),
            monkeypatch=monkeypatch,
        )
    finally:
        await client.drop_database(_DB)
        client.close()


async def _later(w, *, minutes: float = 0, seconds: float = 0) -> None:
    """Time passes: every stored clock moves back, so what comes next happens
    that much after everything already written."""
    ms = int((minutes * 60 + seconds) * 1000)
    for coll, fields in _CLOCKS.items():
        shift = {
            f: {"$cond": [{"$eq": [{"$type": f"${f}"}, "date"]}, {"$subtract": [f"${f}", ms]}, f"${f}"]}
            for f in fields
        }
        await w.db[coll].update_many({}, [{"$set": shift}])


async def _only_position(w, status: PositionStatus | None = None) -> Position:
    q = [Position.user_id == w.c.id] + ([Position.status == status] if status else [])
    [p] = await Position.find(*q).to_list()
    return p


async def _delete(w, p: Position) -> None:
    await trading.delete_position(str(p.id), w.sa, None)


async def _weekly_settle(w, ltp: str) -> None:
    async def _ltp(_token):
        return Decimal(ltp)

    w.monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    batch = SettlementBatch(run_key="T", week_key="2026-W46", started_at=now_utc())
    await batch.insert()
    pos = await _only_position(w, PositionStatus.OPEN)
    out = await weekly_settlement_service._settle_one_position(pos, batch=batch, fx_rate=Decimal(1))
    assert out == "settled"


async def _books(w) -> SimpleNamespace:
    trades = await Trade.find(Trade.user_id == w.c.id, Trade.superseded_by_reopen != True).to_list()  # noqa: E712
    value = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    for t in trades:
        value[t.action] += Decimal(str(t.price)) * Decimal(str(t.quantity))
    positions = await Position.find(Position.user_id == w.c.id).to_list()
    events, _ = await position_service.list_closed_trade_events_fifo(w.c.id, limit=10_000)
    events.reverse()  # oldest close first
    wallet = await Wallet.find_one(Wallet.user_id == w.c.id)
    return SimpleNamespace(
        oracle=value[OrderAction.SELL] - value[OrderAction.BUY],
        realized=sum((Decimal(str(p.realized_pnl)) for p in positions), Decimal(0)),
        gross=sum((Decimal(str(e["gross_pnl"])) for e in events), Decimal(0)).quantize(CENT),
        brokerage=sum((Decimal(str(e["brokerage"])) for e in events), Decimal(0)).quantize(CENT),
        charges=sum((Decimal(str(t.total_charges)) for t in trades), Decimal(0)),
        equity=Decimal(str(wallet.available_balance)) + Decimal(str(wallet.used_margin)),
        rows=events,
    )


def _show(b: SimpleNamespace) -> list:
    return [(e["opened_side"], e["entry_price"], e["qty"], e["gross_pnl"], e["brokerage"]) for e in b.rows]


async def _paired_with(row: dict, t: Trade) -> bool:
    """Was this row's open leg fill `t` (its stored clock, after `_later`)?"""
    stored = (await Trade.get(t.id)).executed_at
    return row["opened_at"].replace(tzinfo=None) == stored.replace(tzinfo=None)


@pytest.mark.parametrize("side,close", [("BUY", "SELL"), ("SELL", "BUY")])
async def test_a_deleted_open_position_leaves_the_book(w, side, close):
    # An OPEN position priced far off the market, deleted by the admin.
    await market(w.c, side, 5, "100")
    await _later(w, minutes=10)
    await _delete(w, await _only_position(w))
    await _later(w, minutes=10)

    opened = await market(w.c, side, 10, "500")
    await _later(w, minutes=1)
    await market(w.c, close, 10, "510" if side == "BUY" else "490")  # +100

    b = await _books(w)
    assert b.oracle == b.realized == Decimal("100")
    assert b.gross == b.oracle, _show(b)
    [row] = b.rows  # was two: 5 against the deleted lot, 5 against the real one
    assert (row["opened_side"], row["entry_price"], row["qty"]) == (side, 500.0, 10.0), _show(b)
    assert await _paired_with(row, opened), _show(b)
    assert b.brokerage == b.charges, _show(b)


async def test_a_partly_closed_open_delete_takes_its_close_out_too(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 4, "110")  # +40, position still open
    await _later(w, minutes=10)
    await _delete(w, await _only_position(w))
    await _later(w, minutes=10)

    opened = await market(w.c, "BUY", 10, "500")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "510")  # +100

    b = await _books(w)
    assert b.oracle == b.realized == Decimal("100")
    assert b.gross == b.oracle, _show(b)
    [row] = b.rows  # was 6 against the deleted lot's remainder + 4 real
    assert await _paired_with(row, opened), _show(b)
    assert b.brokerage == b.charges, _show(b)
    assert b.equity == START + Decimal("100") - b.charges  # the delete left no trace


async def test_a_closed_delete_refunds_every_partial_close(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 5, "110")  # +50, ₹5
    await _later(w, minutes=1)
    await market(w.c, "SELL", 5, "120")  # +100, ₹5 — closes it
    await _later(w, minutes=10)
    await _delete(w, await _only_position(w))

    b = await _books(w)
    assert b.rows == [] and b.realized == b.oracle == 0
    assert b.equity == START  # was START − 5: the first partial's charge stayed


async def test_a_quick_re_entry_keeps_its_own_charges(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "110")  # +100, closes P
    first = await Position.find_one(Position.user_id == w.c.id)
    await _later(w, seconds=8)
    again = await market(w.c, "BUY", 10, "120")  # a new position, 8s later
    await _later(w, minutes=10)
    await _delete(w, first)

    b = await _books(w)
    assert b.realized == 0 and b.rows == []
    assert b.charges == Decimal("10")  # the re-entry's open leg still stands
    assert b.equity == START - b.charges  # was START: its ₹10 refunded too
    t = await Trade.get(again.id)
    assert not t.superseded_by_reopen and not t.fifo_deleted
    assert await Order.get(again.order_id) is not None


async def test_deleting_a_reopened_position_leaves_the_one_in_between(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "110")  # P1 +100
    p1 = await _only_position(w)
    await _later(w, minutes=5)
    p2_open = await market(w.c, "BUY", 10, "200")
    await _later(w, minutes=1)
    p2_close = await market(w.c, "SELL", 10, "210")  # P2 +100
    await _later(w, minutes=5)
    await trading.admin_reopen_position(str(p1.id), w.sa, None)
    await _later(w, minutes=5)
    await market(w.c, "SELL", 10, "130")  # P1 again: +300
    await _later(w, minutes=5)
    await _delete(w, await Position.get(p1.id))

    b = await _books(w)
    assert b.oracle == b.realized == Decimal("100")  # P2 is all that stands
    assert b.gross == b.oracle, _show(b)  # was 0: P2's fills went with P1
    [row] = b.rows
    assert row["entry_price"] == 200.0, _show(b)
    assert b.brokerage == b.charges == Decimal("20"), _show(b)
    assert b.equity == START + Decimal("100") - b.charges  # P2's charges stay paid
    for t in (p2_open, p2_close):
        assert await Order.get(t.order_id) is not None


@pytest.mark.parametrize("successor", ["open", "closed"])
async def test_a_deleted_weekly_successor_leaves_no_re_seeded_lot(w, successor):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=60)
    await _weekly_settle(w, "105")  # +50, re-opened 10 @105
    await _later(w, minutes=10)
    if successor == "closed":
        await market(w.c, "SELL", 10, "108")  # +30, reversed by the delete
        await _later(w, minutes=10)
    p1 = await Position.find_one(Position.user_id == w.c.id, Position.close_reason != "WEEKLY_SETTLEMENT")
    await _delete(w, p1)
    await _later(w, minutes=10)

    opened = await market(w.c, "BUY", 10, "500")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "510")  # +100

    b = await _books(w)
    assert b.realized == Decimal("150")  # the settlement's 50 stands
    assert b.gross == b.realized, _show(b)
    settle, close = b.rows
    assert settle["close_reason"] == "WEEKLY_SETTLEMENT"
    # The round trip pairs its own open, not the deleted successor's re-seed.
    assert await _paired_with(close, opened), _show(b)
    assert close["brokerage"] == 20.0, _show(b)
