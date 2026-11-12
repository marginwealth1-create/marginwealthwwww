"""§2.2 Never quantize a cost basis to paise.

An average price is not a money amount. Rounded to 2 dp, a pyramid of
5000 @408.60, 5000 @408.60, 5000 @408.65 (true average 408.6166…) stored
408.62, and the close of all 15,000 units booked (410 − 408.62) × 15,000 =
20,700 against a flat-book oracle of 20,750. Every derived basis is kept at
6 dp instead: the pyramid average, the specific-lot re-derive, the merged
average of two carried lots, the CNC holding average, and the opening/close
price an admin edit pushes back onto the fills.

Every fill goes through the matching engine. The arbiter is the flat-book
oracle: Σ(sell value) − Σ(buy value) must equal Σ Position.realized_pnl (what
the wallet booked) and Σ blotter gross.
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
from app.models.holding import Holding
from app.models.position import Position, PositionStatus
from app.models.trade import Trade
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service
from app.utils.decimal_utils import quantize_basis
from tests._real_fill import INSTRUMENT, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_cost_basis_precision"
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
    await set_commission_per_lot(1)

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
        await Wallet(user_id=u.id, available_balance=Decimal128("100000000")).insert()
        return u

    try:
        c = await mk("CBP001", UserRole.CLIENT)
        yield SimpleNamespace(c=c, sa=await mk("CBPSA", UserRole.SUPER_ADMIN))
    finally:
        await client.drop_database(_DB)
        client.close()


async def _no_quote(_token: str) -> dict:
    return {}


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
        rows=events,
    )


async def _the_position(user: User) -> Position:
    [p] = await Position.find(Position.user_id == user.id).to_list()
    return p


def test_quantize_basis_keeps_six_places_and_rounds_like_money():
    assert quantize_basis(Decimal("6129250") / 15000) == Decimal("408.616667")
    assert quantize_basis(Decimal("100.0000005")) == Decimal("100.000000")  # half-even
    assert quantize_basis(Decimal("100.0000015")) == Decimal("100.000002")


async def test_a_pyramided_average_survives_and_prices_the_close(w):
    await market(w.c, "BUY", 5000, "408.60")
    await market(w.c, "BUY", 5000, "408.60")
    await market(w.c, "BUY", 5000, "408.65")
    assert Decimal(str((await _the_position(w.c)).avg_price)) == Decimal("408.616667")

    # A partial close books (close − 408.616667) × qty and leaves the average.
    t = await market(w.c, "SELL", 6000, "410")
    assert Decimal(str(t.pnl_inr)) + Decimal(str(t.brokerage)) == Decimal("8300.00")  # not 8,280
    assert Decimal(str((await _the_position(w.c)).avg_price)) == Decimal("408.616667")
    await market(w.c, "SELL", 9000, "410")

    b = await _books(w.c)
    assert b.flat and b.oracle == Decimal("20750")
    assert b.realized == b.oracle  # was 20,700: (410 − 408.62) × 15,000
    assert b.gross == b.oracle


async def test_a_specific_lot_exit_re_derives_the_surviving_basis_exactly(w):
    await market(w.c, "BUY", 10000, "100.00")
    await market(w.c, "BUY", 10000, "100.01")
    await market(w.c, "BUY", 10000, "100.02")
    await market(w.c, "SELL", 10000, "101", basis="100.00")  # Exit the 100.00 lot: +10,000
    # (100.01 × 30,000 − 100 × 10,000) / 20,000
    assert Decimal(str((await _the_position(w.c)).avg_price)) == Decimal("100.015")
    await market(w.c, "SELL", 20000, "101")

    b = await _books(w.c)
    assert b.flat and b.oracle == Decimal("29700")
    assert b.realized == b.oracle  # was 29,600: the survivors re-derived to 100.02
    assert b.gross == b.oracle


async def test_two_carried_lots_merge_at_the_exact_average(w):
    await market(w.c, "BUY", 10000, "408.60")
    with patch.object(market_data_service, "get_quote", _no_quote):
        await position_service.convert_intraday_to_carry({INSTRUMENT.segment})
        await market(w.c, "BUY", 5000, "408.65")  # a separate MIS row on day 2
        await position_service.convert_intraday_to_carry({INSTRUMENT.segment})
    [p] = await Position.find(Position.status == PositionStatus.OPEN).to_list()  # merged
    assert p.quantity == 15000
    assert Decimal(str(p.avg_price)) == Decimal("408.616667")

    await market(w.c, "SELL", 15000, "410", product=ProductType.NRML)
    b = await _books(w.c)
    assert b.flat and b.oracle == Decimal("20750")
    assert b.realized == b.oracle  # was 20,700


async def test_a_cnc_holding_averages_exactly(w):
    for price in ("408.60", "408.60", "408.65"):
        await market(w.c, "BUY", 5000, price, product=ProductType.CNC)
    [h] = await Holding.find(Holding.user_id == w.c.id).to_list()
    assert Decimal(str(h.avg_price)) == Decimal("408.616667")
    assert Decimal(str(h.invested_value)) == Decimal("6129250.00")  # was 6,129,300


async def test_an_admin_price_edit_reaches_the_fills_unrounded(w):
    await market(w.c, "BUY", 15000, "408")
    await market(w.c, "SELL", 15000, "410")
    p = await _the_position(w.c)

    await trading.admin_edit_position(
        str(p.id), {"avg_price": "408.6167", "close_price": "410.0049"}, w.sa, None
    )

    b = await _books(w.c)
    assert b.realized == Decimal("20823.00")  # (410.0049 − 408.6167) × 15,000
    assert b.oracle == b.realized
    # was 20,700: the fills were cut to 408.62 / 410.00
    assert b.gross == b.realized, [(e["entry_price"], e["close_price"], e["gross_pnl"]) for e in b.rows]
