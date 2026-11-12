"""§2.3 Bill a close at the price the wallet was charged.

The matching engine keeps Position.avg_price and books a close as
(close − avg) × qty — or against the tapped lot on an Active-tab specific-lot
Exit. The blotter paired every close against its FIFO lot's own price, so the
statement and the wallet priced the same trade two ways: a partly closed
pyramid printed a different week, and across a weekly settlement (which books
off the engine average) the gap never washed out.

Engine side (new fills only): a specific-lot basis is honoured only on a
PARTIAL close of the position it was tapped on. Exiting the last lot after an
average-price close books against the average, a basis that would drive the
survivors' average below zero is refused, and a lot from before a weekly
settlement re-opened the position is not a lot of it any more.

Every fill goes through the matching engine; the settlement is the real
weekly job. The arbiter is the flat-book oracle: Σ(sell value) − Σ(buy value)
must equal Σ Position.realized_pnl (what the wallet booked) and Σ blotter gross.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.user import positions as user_positions
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import OrderAction
from app.models.order import Order
from app.models.position import Position, PositionStatus
from app.models.position_settlement import SettlementBatch
from app.models.trade import Trade
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, order_service, position_service, weekly_settlement_service
from app.utils.time_utils import now_utc
from tests._real_fill import market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_fifo_basis_matches_wallet"
CENT = Decimal("0.01")


@pytest_asyncio.fixture
async def c(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs
    u = User(
        user_code="FBW001",
        email="fbw001@example.com",
        mobile="9312345678",
        full_name="FBW001",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
    )
    await u.insert()
    await Wallet(user_id=u.id, available_balance=Decimal128("10000000")).insert()
    try:
        yield u
    finally:
        await client.drop_database(_DB)
        client.close()


async def _books(user: User) -> SimpleNamespace:
    trades = await Trade.find(Trade.user_id == user.id).to_list()
    value = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    for t in trades:
        value[t.action] += Decimal(str(t.price)) * Decimal(str(t.quantity))
    positions = await Position.find(Position.user_id == user.id).to_list()
    events, _ = await position_service.list_closed_trade_events_fifo(user.id, limit=10_000)
    return SimpleNamespace(
        oracle=value[OrderAction.SELL] - value[OrderAction.BUY],
        realized=sum((Decimal(str(p.realized_pnl)) for p in positions), Decimal(0)),
        gross=sum((Decimal(str(e["gross_pnl"])) for e in events), Decimal(0)).quantize(CENT),
        brokerage=sum((Decimal(str(e["brokerage"])) for e in events), Decimal(0)).quantize(CENT),
        charges=sum((Decimal(str(t.total_charges)) for t in trades), Decimal(0)),
        rows=[(e["entry_price"], e["qty"], e["gross_pnl"]) for e in events],
    )


async def _billed_as_booked(user: User) -> SimpleNamespace:
    b = await _books(user)
    assert b.gross == b.realized, b.rows
    return b


async def _weekly_settle(user: User, ltp: str, monkeypatch) -> None:
    """The real Saturday job on the open position: book (ltp − avg) × qty to
    the wallet, close it, re-open an identical one at `ltp`."""

    async def _ltp(_token):
        return Decimal(ltp)

    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    batch = SettlementBatch(run_key="T", week_key="2026-W46", started_at=now_utc())
    await batch.insert()
    pos = await Position.find_one(Position.user_id == user.id, Position.status == PositionStatus.OPEN)
    out = await weekly_settlement_service._settle_one_position(pos, batch=batch, fx_rate=Decimal(1))
    assert out == "settled"


async def test_every_close_is_billed_at_what_the_wallet_booked(c):
    await market(c, "BUY", 10, "100")
    await market(c, "BUY", 10, "200.5")
    await market(c, "BUY", 10, "300")  # engine avg 200.166667
    await market(c, "SELL", 10, "250")  # average-price close: +498.33, not lot 100's +1500
    await _billed_as_booked(c)
    await market(c, "SELL", 10, "250", basis="300")  # Active-tab Exit of the 300 lot: −500
    await _billed_as_booked(c)
    await market(c, "SELL", 10, "260")  # the last 10 against the re-derived average

    b = await _billed_as_booked(c)
    assert b.oracle == Decimal("1595")
    assert b.realized == b.oracle
    assert b.brokerage == b.charges  # every leg's charge still lands on a row


async def test_a_specific_lot_exit_releases_its_own_basis_not_the_lot_it_pairs(c):
    # No queued lot sits at the basis, so the walk pairs the FIFO front (100):
    # billed at 200 it must also take 200 — not 100 — off what the book cost.
    await market(c, "BUY", 10, "100")
    await market(c, "BUY", 10, "200.5")
    await market(c, "BUY", 10, "300")
    await market(c, "SELL", 10, "250", basis="200")
    await _billed_as_booked(c)
    await market(c, "SELL", 20, "250")

    b = await _billed_as_booked(c)
    assert b.realized == b.oracle == Decimal("1495")


async def test_a_pyramid_settled_over_the_weekend_bills_one_basis(c, monkeypatch):
    # The settlement books (190 − engine avg 150) × 10; a week-1 close priced
    # off lot 100 instead of the average left +500 on the statement for good.
    await market(c, "BUY", 10, "100")
    await market(c, "BUY", 10, "200")
    await market(c, "SELL", 10, "180")  # +300
    await _weekly_settle(c, "190", monkeypatch)  # +400
    await market(c, "SELL", 10, "195")  # +50

    b = await _billed_as_booked(c)
    assert b.realized == b.oracle == Decimal("750")


@pytest.mark.parametrize(
    "fills",
    [
        # Average-price close, then the Active tab's survivor (200) exited: the
        # last lot has no survivors to carry (avg − lot), so it books at avg.
        [
            ("BUY", 10, "100", None),
            ("BUY", 10, "200", None),
            ("SELL", 10, "180", None),
            ("SELL", 10, "180", "200"),
        ],
        # A basis worth more than the whole book: survivors' average would go
        # negative (it was floored at 0 and the difference lost).
        [
            ("BUY", 10, "100", None),
            ("BUY", 10, "110", None),
            ("SELL", 5, "120", "1000"),
            ("SELL", 15, "120", None),
        ],
    ],
)
async def test_a_basis_the_book_cannot_carry_books_at_the_average(c, fills):
    for action, qty, price, basis in fills:
        await market(c, action, qty, price, basis=basis)

    b = await _billed_as_booked(c)
    assert b.realized == b.oracle


async def test_an_active_tab_exit_of_a_settled_lot_books_at_the_average(c, monkeypatch):
    hour_ago = now_utc() - timedelta(hours=1)
    a = await market(c, "BUY", 10, "100", at=hour_ago)
    await market(c, "BUY", 10, "200", at=hour_ago)
    await _weekly_settle(c, "190", monkeypatch)  # +800, re-opened 20 @ 190

    # The Active tab's Exit on fill A, routed to the real engine at 195.
    async def _route(*, user, payload):
        t = await market(
            user,
            payload["action"],
            payload["force_quantity"],
            "195",
            basis=payload.get("cost_basis_override"),
        )
        return await Order.get(t.order_id)

    async def _free(*a, **k):
        return True

    monkeypatch.setattr(order_service, "place_order", _route)
    monkeypatch.setattr("app.core.redis_client.idempotency_check_and_set", _free)
    monkeypatch.setattr("app.utils.background.fire_and_forget", lambda coro, **k: coro.close())
    await user_positions.close_active_trade(str(a.id), c)

    # Lot A's move to 190 was settled already; this exit earns 195 − 190.
    b = await _billed_as_booked(c)
    assert b.realized == Decimal("850")


async def test_a_parked_exit_from_before_the_settlement_books_at_the_average(c, monkeypatch):
    hour_ago = now_utc() - timedelta(hours=1)
    await market(c, "BUY", 10, "100", at=hour_ago)
    await market(c, "BUY", 10, "200", at=hour_ago)
    await _weekly_settle(c, "190", monkeypatch)
    # A per-fill stop on lot 100, parked last week, fires after the settlement.
    await market(c, "SELL", 10, "195", basis="100", placed_at=hour_ago)

    b = await _billed_as_booked(c)
    assert b.realized == Decimal("850")
