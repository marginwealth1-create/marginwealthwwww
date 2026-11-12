"""§2.4 An intraday reversal must not be billed twice.

The FIFO blotter read every unstamped fill facing opposite-side lots as a close
the engine forgot to stamp. A fresh open after an admin deleted an OPEN
position (its opening fills stay in the walk) drained the stale lots, emitted
their P&L, and the real close that followed was rebuilt again by the
algebraic branch: one round trip, billed twice. A close took the FIFO front
whatever side it held, and a reversal's excess never opened a lot, so the new
leg's units and brokerage fell off the statement.

Every fill goes through the matching engine; the delete is the real admin
endpoint. The arbiter is the flat-book oracle over the fills that still stand:
Σ(sell value) − Σ(buy value) must equal Σ Position.realized_pnl (what the
wallet booked) and Σ blotter gross, and every live leg's charge lands on a row.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import trading
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import OrderAction
from app.models.position import Position, PositionStatus
from app.models.position_settlement import SettlementBatch
from app.models.trade import Trade
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service, weekly_settlement_service
from app.utils.time_utils import now_utc
from tests._real_fill import market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_fifo_reversal_double_count"
CENT = Decimal("0.01")


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
            mobile=f"94{abs(hash(code)) % 10**8:08d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128("1000000")).insert()
        return u

    try:
        c = await mk("REV001", UserRole.CLIENT)
        yield SimpleNamespace(c=c, sa=await mk("REVSA", UserRole.SUPER_ADMIN))
    finally:
        await client.drop_database(_DB)
        client.close()


async def _books(user: User) -> SimpleNamespace:
    # A deleted position's fills are flagged superseded; they no longer stand.
    trades = await Trade.find(Trade.user_id == user.id, Trade.superseded_by_reopen != True).to_list()  # noqa: E712
    value = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    units = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    for t in trades:
        value[t.action] += Decimal(str(t.price)) * Decimal(str(t.quantity))
        units[t.action] += Decimal(str(t.quantity))
    positions = await Position.find(Position.user_id == user.id).to_list()
    events, _ = await position_service.list_closed_trade_events_fifo(user.id, limit=10_000)
    events.reverse()  # oldest close first
    return SimpleNamespace(
        flat=units[OrderAction.BUY] == units[OrderAction.SELL],
        oracle=value[OrderAction.SELL] - value[OrderAction.BUY],
        realized=sum((Decimal(str(p.realized_pnl)) for p in positions), Decimal(0)),
        gross=sum((Decimal(str(e["gross_pnl"])) for e in events), Decimal(0)).quantize(CENT),
        billed_units=sum((Decimal(str(e["qty"])) for e in events), Decimal(0)),
        brokerage=sum((Decimal(str(e["brokerage"])) for e in events), Decimal(0)).quantize(CENT),
        charges=sum((Decimal(str(t.total_charges)) for t in trades), Decimal(0)),
        rows=events,
    )


def _show(b: SimpleNamespace) -> list:
    return [(e["opened_side"], e["entry_price"], e["qty"], e["gross_pnl"], e["brokerage"]) for e in b.rows]


async def test_a_reversal_is_billed_once(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 15, "110")  # closes 10 (+100), opens a 5 short @110
    await market(w.c, "BUY", 5, "100")  # closes the short: +50

    b = await _books(w.c)
    assert b.flat and b.oracle == Decimal("150")
    assert b.realized == b.oracle
    assert b.gross == b.oracle, _show(b)
    assert b.billed_units == Decimal("15")
    assert b.brokerage == b.charges, _show(b)  # the short's open leg is a third of the SELL 15
    short = b.rows[-1]
    assert (short["opened_side"], short["entry_price"], short["qty"]) == ("SELL", 110.0, 5.0)


async def test_a_reversal_then_an_add_bills_every_unit(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 15, "110")  # +100, short 5 @110
    await market(w.c, "SELL", 5, "120")  # short 10 @115
    await market(w.c, "BUY", 10, "100")  # +150

    b = await _books(w.c)
    assert b.flat and b.oracle == Decimal("250")
    assert b.realized == b.oracle
    assert b.gross == b.oracle, _show(b)
    assert b.billed_units == Decimal("20"), _show(b)
    assert b.brokerage == b.charges, _show(b)


async def _delete_open_long(w) -> None:
    """BUY 10 @100, then the admin deletes the OPEN position: its fill stays in
    the trades collection, flagged superseded."""
    await market(w.c, "BUY", 10, "100")
    [p] = await Position.find(Position.user_id == w.c.id).to_list()
    await trading.delete_position(str(p.id), w.sa, None)


async def test_a_fresh_open_after_a_deleted_position_is_not_a_close(w):
    await _delete_open_long(w)
    await market(w.c, "SELL", 10, "150")  # a fresh short: nothing to close
    # An add a minute later opens no position, but the book holds its own side.
    await market(w.c, "SELL", 5, "160", at=now_utc() + timedelta(minutes=1))  # short 15 @153.333333
    await market(w.c, "BUY", 15, "140", at=now_utc() + timedelta(minutes=2))  # +200

    b = await _books(w.c)
    assert b.flat and b.oracle == Decimal("200")
    assert b.realized == b.oracle
    assert b.gross == b.oracle, _show(b)  # was 500 + 300 phantom + 200
    assert [e["opened_side"] for e in b.rows] == ["SELL", "SELL"], _show(b)
    assert b.brokerage == b.charges, _show(b)  # not the deleted BUY's charge


async def test_a_close_pairs_only_with_the_opposite_side(w):
    # The deleted long's BUY lot sits at the front; the short's close must
    # not take it.
    await _delete_open_long(w)
    await market(w.c, "SELL", 10, "150")
    await market(w.c, "BUY", 10, "140")  # +100

    b = await _books(w.c)
    assert b.realized == b.oracle == Decimal("100")
    assert b.gross == b.oracle, _show(b)
    [row] = b.rows
    assert (row["opened_side"], row["entry_price"]) == ("SELL", 150.0), _show(b)
    assert b.brokerage == b.charges, _show(b)


async def test_a_settlement_drains_only_its_own_side(w, monkeypatch):
    await _delete_open_long(w)
    await market(w.c, "SELL", 10, "150")

    async def _ltp(_token):
        return Decimal("145")

    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    batch = SettlementBatch(run_key="T", week_key="2026-W46", started_at=now_utc())
    await batch.insert()
    pos = await Position.find_one(Position.user_id == w.c.id, Position.status == PositionStatus.OPEN)
    out = await weekly_settlement_service._settle_one_position(pos, batch=batch, fx_rate=Decimal(1))
    assert out == "settled"
    reopened = await Position.find_one(Position.user_id == w.c.id, Position.status == PositionStatus.OPEN)
    await market(w.c, "BUY", 10, "140")  # +50 against the settlement price

    b = await _books(w.c)
    assert b.realized == b.oracle == Decimal("100")
    assert b.gross == b.oracle, _show(b)
    close = b.rows[-1]
    # The settlement took the short's own lot; this close pairs the re-seed.
    assert close["opened_at"].replace(tzinfo=None) == reopened.opened_at.replace(tzinfo=None), _show(b)
