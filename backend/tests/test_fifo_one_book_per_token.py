"""§2.1 One FIFO book per script, not per product type.

The EOD carry flips a surviving MIS position to NRML and leaves its opening
fills stamped MIS; every later order on that script adopts NRML. Keyed on the
FILL's product type, one position's lots sat in two FIFO queues, so a close
after the flip paired against half its own book (a sign flip on the row), the
units it could not pair were dropped, and the stranded lot later priced a
fresh round trip on the same script.

Every fill below goes through the matching engine and the carry is the real
EOD job. The arbiter is the flat-book oracle: Σ blotter gross must equal
Σ(sell value) − Σ(buy value) and Σ Position.realized_pnl (what the wallet
booked).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import trading
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import OrderAction, ProductType
from app.models.position import Position, PositionStatus
from app.models.trade import Trade
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service
from tests._real_fill import INSTRUMENT, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_fifo_one_book_per_token"
NRML = ProductType.NRML
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
    await set_commission_per_lot(20)  # ₹20 a unit, both legs

    async def mk(code: str, role: UserRole) -> User:
        u = User(
            user_code=code,
            email=f"{code.lower()}@example.com",
            mobile=f"95{abs(hash(code)) % 10**8:08d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128("1000000")).insert()
        return u

    try:
        c = await mk("OBK001", UserRole.CLIENT)
        yield SimpleNamespace(c=c, sa=await mk("OBKSA", UserRole.SUPER_ADMIN))
    finally:
        await client.drop_database(_DB)
        client.close()


async def _no_quote(_token: str) -> dict:
    return {}


async def _eod_carry() -> None:
    """The real EOD rollover: the open MIS position flips to NRML; its fills
    keep their MIS stamp."""
    with patch.object(market_data_service, "get_quote", _no_quote):
        out = await position_service.convert_intraday_to_carry({INSTRUMENT.segment})
    assert out["converted"] == 1, out


async def _books(user: User) -> SimpleNamespace:
    trades = await Trade.find(Trade.user_id == user.id).to_list()
    value = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    units = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    for t in trades:
        value[t.action] += Decimal(str(t.price)) * Decimal(str(t.quantity))
        units[t.action] += Decimal(str(t.quantity))
    positions = await Position.find(Position.user_id == user.id).to_list()
    events, _ = await position_service.list_closed_trade_events_fifo(user.id, limit=10_000)
    return SimpleNamespace(
        flat=units[OrderAction.BUY] == units[OrderAction.SELL],
        oracle=value[OrderAction.SELL] - value[OrderAction.BUY],
        realized=sum((Decimal(str(p.realized_pnl)) for p in positions), Decimal(0)),
        gross=sum((Decimal(str(e["gross_pnl"])) for e in events), Decimal(0)).quantize(CENT),
        billed_units=sum((Decimal(str(e["qty"])) for e in events), Decimal(0)),
        brokerage=sum((Decimal(str(e["brokerage"])) for e in events), Decimal(0)).quantize(CENT),
        charges=sum((Decimal(str(t.total_charges or t.brokerage or 0)) for t in trades), Decimal(0)),
        rows=events,
    )


async def test_a_close_after_the_carry_pairs_against_the_whole_position(w):
    await market(w.c, "BUY", 10, "100")  # day 1, MIS
    await _eod_carry()
    await market(w.c, "BUY", 10, "110", product=NRML)  # adopts the carry: avg 105
    await market(w.c, "SELL", 15, "108", product=NRML)  # engine books +45
    await market(w.c, "SELL", 5, "108", product=NRML)  # engine books +15

    b = await _books(w.c)
    assert b.flat and b.oracle == Decimal("60")
    assert b.realized == b.oracle
    assert b.gross == b.oracle, [(e["entry_price"], e["qty"], e["gross_pnl"]) for e in b.rows]
    assert b.billed_units == Decimal("20")
    assert b.brokerage == b.charges  # every leg's charge lands on a row


async def test_the_carried_lot_is_not_left_to_price_a_later_round_trip(w):
    await market(w.c, "BUY", 10, "100")
    await _eod_carry()
    await market(w.c, "BUY", 10, "110", product=NRML)
    await market(w.c, "SELL", 15, "108", product=NRML)
    await market(w.c, "SELL", 5, "108", product=NRML)
    # Flat: the next order has nothing to adopt and books a fresh MIS position.
    await market(w.c, "BUY", 10, "200")
    await market(w.c, "SELL", 10, "201")  # true +10

    b = await _books(w.c)
    assert b.flat and b.oracle == Decimal("70")
    assert b.realized == b.oracle
    assert b.gross == b.oracle, [(e["entry_price"], e["qty"], e["gross_pnl"]) for e in b.rows]


async def test_two_positions_held_at_once_under_two_products_stay_two_books(w):
    await market(w.c, "BUY", 10, "100")  # MIS long
    await market(w.c, "SELL", 5, "105", product=NRML)  # a separate NRML short
    assert await Position.find(Position.status == PositionStatus.OPEN).count() == 2
    assert (await _books(w.c)).rows == []  # nothing has closed

    await market(w.c, "SELL", 10, "103")  # closes the MIS long: +30
    await market(w.c, "BUY", 5, "104", product=NRML)  # closes the NRML short: +5

    b = await _books(w.c)
    assert b.flat and b.oracle == Decimal("35")
    assert b.realized == b.oracle
    assert b.gross == b.oracle, [(e["entry_price"], e["qty"], e["gross_pnl"]) for e in b.rows]


async def test_admin_open_price_edit_reaches_the_carried_positions_mis_fill(w):
    await market(w.c, "BUY", 10, "100")
    await _eod_carry()
    await market(w.c, "SELL", 10, "105", product=NRML)  # closes the carry: +50
    [p] = await Position.find(Position.user_id == w.c.id).to_list()
    assert p.status == PositionStatus.CLOSED

    await trading.admin_edit_position(str(p.id), {"avg_price": "99"}, w.sa, None)

    b = await _books(w.c)
    assert b.realized == Decimal("60")
    assert b.gross == b.realized, [(e["entry_price"], e["qty"], e["gross_pnl"]) for e in b.rows]


async def test_reopen_supersedes_the_pre_carry_partial_close(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 4, "102")  # pre-carry partial close, MIS-stamped: +8
    await _eod_carry()
    await market(w.c, "SELL", 6, "105", product=NRML)  # closes the carry: +30
    [p] = await Position.find(Position.user_id == w.c.id).to_list()

    await trading.admin_reopen_position(str(p.id), w.sa, None)  # undoes all 38
    await market(w.c, "SELL", 10, "103", product=NRML)  # the real close: +30

    b = await _books(w.c)
    assert b.realized == Decimal("30")
    assert b.gross == b.realized, [(e["entry_price"], e["qty"], e["gross_pnl"]) for e in b.rows]
