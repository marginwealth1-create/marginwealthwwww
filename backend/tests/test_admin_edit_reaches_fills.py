"""§6.1 / §6.2 / §6 extra: an admin position edit must reach the fills.

Everything that ACTS on a position reads the Position doc (the square-off, the
margin, the P&L the wallet books); everything that REPORTS it is rebuilt from
the fills (the FIFO blotter, the tradebook, the netting dialog). An edit that
moves one and not the other splits the book:

- an OPEN quantity edit left the opening fill at its old size (units with no
  lot fell off the statement, or orphan units priced a later round trip);
- an OPEN entry-price edit left the opening fill at its old price;
- a CLOSED price edit re-booked (final close − avg) × opening_quantity and
  stamped every close with the final close price.

Forward-only: these are edits made from now on. Every fill goes through the
matching engine; the edit, reopen and EOD carry are the real handlers. The
arbiters: Σ blotter gross == Σ Position.realized_pnl (== the flat-book oracle
when flat), and the wallet's P&L ledger == what the statement bills.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import trading
from app.core.config import settings
from app.core.database import _document_models
from app.core.exceptions import ConflictError
from app.models._base import Exchange, OrderAction, OrderType, ProductType
from app.models.audit_log import AuditLog
from app.models.order import InstrumentRef, Order, OrderStatus
from app.models.position import Position, PositionStatus
from app.models.position_settlement import SettlementBatch
from app.models.trade import Trade
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service, weekly_settlement_service
from app.utils.time_utils import now_utc
from tests._real_fill import INSTRUMENT, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_admin_edit_reaches_fills"
CENT = Decimal("0.01")
_LEDGER = (TransactionType.PNL, TransactionType.CHARGES, TransactionType.REVERSAL)
USD = InstrumentRef(
    token="TUSD1", symbol="TUSD", trading_symbol="TUSD", exchange=Exchange.CRYPTO, segment="FOREX", lot_size=1
)


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    async def _ltp(_token):
        return Decimal("100")

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(trading, "publish", _no_publish)
    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
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
        await Wallet(user_id=u.id, available_balance=Decimal128("1000000")).insert()
        return u

    try:
        c, sa = await mk("AEF001", UserRole.CLIENT), await mk("AEFSA", UserRole.SUPER_ADMIN)
        yield SimpleNamespace(c=c, sa=sa)
    finally:
        await client.drop_database(_DB)
        client.close()


async def _pos(w) -> Position:
    [p] = await Position.find(Position.user_id == w.c.id).sort("-opened_at").limit(1).to_list()
    return p


async def _edit(w, payload: dict) -> Position:
    p = await _pos(w)
    await trading.admin_edit_position(str(p.id), payload, w.sa, None)
    return await Position.get(p.id)


async def _ledger(w) -> Decimal:
    rows = await WalletTransaction.find(
        WalletTransaction.user_id == w.c.id, {"transaction_type": {"$in": [t.value for t in _LEDGER]}}
    ).to_list()
    return sum((Decimal(str(r.amount)) for r in rows), Decimal(0))


async def _books(w) -> SimpleNamespace:
    trades = await Trade.find(Trade.user_id == w.c.id, Trade.superseded_by_reopen != True).to_list()  # noqa: E712
    value = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    units = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    for t in trades:
        value[t.action] += Decimal(str(t.price)) * Decimal(str(t.quantity))
        units[t.action] += Decimal(str(t.quantity))
    positions = await Position.find(Position.user_id == w.c.id).to_list()
    events, _ = await position_service.list_closed_trade_events_fifo(w.c.id, limit=10_000)
    events.reverse()  # oldest close first
    net = sum(
        (
            Decimal(str(e["gross_pnl"])) * Decimal(str(e.get("fx_rate") or 1)) - Decimal(str(e["brokerage"]))
            for e in events
        ),
        Decimal(0),
    )
    return SimpleNamespace(
        flat=units[OrderAction.BUY] == units[OrderAction.SELL],
        oracle=value[OrderAction.SELL] - value[OrderAction.BUY],
        realized=sum((Decimal(str(p.realized_pnl)) for p in positions), Decimal(0)),
        gross=sum((Decimal(str(e["gross_pnl"])) for e in events), Decimal(0)).quantize(CENT),
        units=sum((Decimal(str(e["qty"])) for e in events), Decimal(0)),
        brokerage=sum((Decimal(str(e["brokerage"])) for e in events), Decimal(0)).quantize(CENT),
        charges=sum((Decimal(str(t.total_charges)) for t in trades), Decimal(0)),
        net=net.quantize(CENT),
        ledger=await _ledger(w),
        rows=events,
    )


def _show(b: SimpleNamespace) -> list:
    return [(e["opened_side"], e["entry_price"], e["qty"], e["gross_pnl"], e["brokerage"]) for e in b.rows]


def _one_book(b: SimpleNamespace, pnl: str) -> None:
    """Flat, and every screen says `pnl`: fills, positions, statement, wallet."""
    assert b.flat, _show(b)
    assert b.oracle == b.realized == Decimal(pnl), _show(b)
    assert b.gross == b.oracle, _show(b)
    assert b.brokerage == b.charges, _show(b)
    assert b.net == b.ledger, _show(b)


async def _fill(t: Trade) -> Trade:
    return await Trade.get(t.id)


async def _park_exit(w, t: Trade, qty: float) -> Order:
    """A per-fill SL parked on fill `t`, in the shape the Active tab parks it."""
    o = Order(
        order_number=f"SL{str(t.id)[-8:]}",
        user_id=w.c.id,
        instrument=INSTRUMENT,
        action=OrderAction.SELL,
        order_type=OrderType.SL_M,
        product_type=ProductType.MIS,
        lots=qty,
        quantity=qty,
        pending_quantity=qty,
        trigger_price=Decimal128("50"),
        status=OrderStatus.OPEN,
        is_squareoff=True,
        cost_basis_override=t.price,
        sl_tp_source_trade_id=t.id,
        oco_group_id=str(t.id),
        placed_by=w.c.id,
    )
    await o.insert()
    return o


# ── §6.1 quantity edits on an OPEN position ──────────────────────────────


async def test_a_quantity_raise_resizes_the_opening_fill(w):
    opened = await market(w.c, "BUY", 5, "100")
    p = await _edit(w, {"quantity": 10})
    assert p.opening_quantity == 10  # a reopen restores what the admin set
    assert (await _fill(opened)).quantity == 10
    await market(w.c, "SELL", 10, "110")  # the engine books +100 off the edited position

    b = await _books(w)
    _one_book(b, "100")
    assert b.units == 10, _show(b)  # was 5: the units with no lot fell off


async def test_a_quantity_cut_leaves_no_orphan_units(w):
    await market(w.c, "BUY", 10, "100")
    await _edit(w, {"quantity": 5})
    await market(w.c, "SELL", 5, "110")  # +50
    await market(w.c, "BUY", 5, "200")  # a fresh round trip on the script
    await market(w.c, "SELL", 5, "210")  # +50

    b = await _books(w)
    _one_book(b, "100")
    assert [r["entry_price"] for r in b.rows] == [100.0, 200.0], _show(b)


async def test_a_partly_closed_lot_keeps_its_billed_row(w):
    opened = await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 4, "110")  # +40, already on a statement
    [billed] = (await _books(w)).rows
    sl = await _park_exit(w, opened, 6)

    p = await _edit(w, {"quantity": 12})
    assert p.opening_quantity == 16
    survivor = await _fill(opened)
    assert survivor.quantity == 12  # the open lot keeps its id: the SL still points at it
    assert (await Order.get(sl.id)).quantity == 12
    order = await Order.get(opened.order_id)
    assert (order.quantity, order.filled_quantity) == (16, 16)
    await market(w.c, "SELL", 12, "105")  # +60

    b = await _books(w)
    _one_book(b, "100")
    assert b.units == 16, _show(b)
    first = b.rows[0]
    assert (first["qty"], first["gross_pnl"], first["brokerage"]) == (
        billed["qty"],
        billed["gross_pnl"],
        billed["brokerage"],
    ), _show(b)


async def test_a_quantity_edit_may_not_flip_or_flatten_the_position(w):
    await market(w.c, "BUY", 10, "100")
    for qty in (-5, 0):
        with pytest.raises(HTTPException) as e:
            await _edit(w, {"quantity": qty})
        assert e.value.status_code == 400
    assert (await _pos(w)).quantity == 10


async def test_a_closed_row_takes_no_open_only_fields(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 10, "110")
    with pytest.raises(HTTPException) as e:
        await _edit(w, {"quantity": 20})
    assert e.value.status_code == 400
    assert (await _pos(w)).quantity == 0


# ── §6.2 entry-price edits on an OPEN position ───────────────────────────


async def test_an_open_price_edit_reprices_the_lots_and_keeps_their_spacing(w):
    a = await market(w.c, "BUY", 5, "100")
    b_ = await market(w.c, "BUY", 5, "110")  # avg 105
    sl = await _park_exit(w, b_, 5)
    await _edit(w, {"avg_price": "100"})
    assert [Decimal(str((await _fill(t)).price)) for t in (a, b_)] == [Decimal("95"), Decimal("105")]
    assert Decimal(str((await Order.get(sl.id)).cost_basis_override)) == Decimal("105")
    await market(w.c, "SELL", 10, "120")  # +200 off avg 100

    _one_book(await _books(w), "200")


async def test_a_pyramid_keeps_its_ratio_and_spacing_through_one_edit(w):
    a = await market(w.c, "BUY", 5, "100")
    b_ = await market(w.c, "BUY", 15, "110")  # avg 107.5
    await _edit(w, {"quantity": 40, "avg_price": "105"})
    assert [((await _fill(t)).quantity, Decimal(str((await _fill(t)).price))) for t in (a, b_)] == [
        (10, Decimal("97.5")),
        (30, Decimal("107.5")),
    ]
    await market(w.c, "SELL", 40, "110")  # +200 off avg 105

    _one_book(await _books(w), "200")


async def test_an_open_price_edit_leaves_the_consumed_units_at_their_price(w):
    opened = await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 4, "110")  # +40, already billed against 100
    [billed] = (await _books(w)).rows
    await _edit(w, {"avg_price": "98"})
    assert Decimal(str((await _fill(opened)).price)) == Decimal("98")  # the 6 still open
    await market(w.c, "SELL", 6, "110")  # +72

    b = await _books(w)
    _one_book(b, "112")
    assert (b.rows[0]["qty"], b.rows[0]["gross_pnl"], b.rows[0]["brokerage"]) == (
        billed["qty"],
        billed["gross_pnl"],
        billed["brokerage"],
    ), _show(b)


async def test_an_open_price_edit_reaches_the_carried_mis_fill(w):
    await market(w.c, "BUY", 10, "100")  # MIS
    with patch.object(market_data_service, "get_quote", _no_quote):
        assert (await position_service.convert_intraday_to_carry({INSTRUMENT.segment}))["converted"] == 1
    await _edit(w, {"avg_price": "98"})  # the doc says NRML, its fill says MIS
    await market(w.c, "SELL", 10, "105", product=ProductType.NRML)  # +70

    _one_book(await _books(w), "70")


async def test_an_edit_after_a_reopen_carries_the_restored_lot(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 10, "110")
    p = await _pos(w)
    await trading.admin_reopen_position(str(p.id), w.sa, None)  # the lot is back in the walk
    await _edit(w, {"quantity": 20})
    await market(w.c, "SELL", 20, "120")  # +400

    b = await _books(w)
    _one_book(b, "400")
    assert b.units == 20, _show(b)


async def test_an_edit_after_a_legacy_reopen_leaves_the_fills_alone(w):
    opened = await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 10, "110")
    p = await _pos(w)
    await trading.admin_reopen_position(str(p.id), w.sa, None)
    # A reopen made before superseded_at: the undone close still drains the lot.
    await Trade.get_motor_collection().update_many({}, {"$unset": {"superseded_at": ""}})
    await _edit(w, {"quantity": 20})
    assert (await _fill(opened)).quantity == 10  # a consumed lot is not this position's to resize
    await market(w.c, "SELL", 20, "120")  # +400, recovered from its own stamp

    b = await _books(w)
    assert b.realized == b.gross == Decimal("400"), _show(b)
    assert b.units == 20, _show(b)


@pytest.mark.parametrize(
    ("edit", "close", "pnl"),
    [({"quantity": 10}, 10, "165"), ({"avg_price": "104"}, 5, "95")],
    ids=["quantity", "price"],
)
async def test_an_edit_after_an_average_priced_partial_close_keeps_the_book(w, edit, close, pnl):
    await market(w.c, "BUY", 3, "100")
    await market(w.c, "BUY", 3, "110")  # avg 105
    await market(w.c, "SELL", 1, "120")  # +15 off the average; FIFO took a 100 unit
    # The 5 still held cost 530 in lots, 525 on the doc: that 5 is what the
    # first close booked over its lot, and an edit must carry it, not scale it.
    await _edit(w, edit)
    await market(w.c, "SELL", close, "120")

    _one_book(await _books(w), pnl)  # was 160 / 100 in the fills


# ── §6 extra: price edits on a CLOSED position ────────────────────────────


async def test_a_closed_avg_edit_rebooks_each_close_at_its_own_price(w):
    opened = await market(w.c, "BUY", 10, "100")
    c1 = await market(w.c, "SELL", 5, "110")  # +50
    c2 = await market(w.c, "SELL", 5, "120")  # +100
    before = await _ledger(w)

    p = await _edit(w, {"avg_price": "101"})

    assert Decimal(str(p.realized_pnl)) == Decimal("140")
    assert await _ledger(w) - before == Decimal("-10")  # was +40: (120 − 101) × 10 − 150
    assert [Decimal(str((await _fill(t)).price)) for t in (opened, c1, c2)] == [
        Decimal("101"),
        Decimal("110"),  # was rewritten to the final close, 120
        Decimal("120"),
    ]
    _one_book(await _books(w), "140")


async def test_a_close_price_edit_needs_exactly_one_close(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 5, "110")
    await market(w.c, "SELL", 5, "120")
    before = await _books(w)

    with pytest.raises(ConflictError):
        await _edit(w, {"close_price": "125"})

    after = await _books(w)
    assert (after.realized, after.ledger, _show(after)) == (before.realized, before.ledger, _show(before))
    assert Decimal(str((await _pos(w)).ltp)) == Decimal("120")


async def test_a_close_price_edit_on_a_single_close(w):
    await market(w.c, "BUY", 10, "100")
    close = await market(w.c, "SELL", 10, "110")
    await _edit(w, {"close_price": "112"})
    assert Decimal(str((await _fill(close)).price)) == Decimal("112")
    _one_book(await _books(w), "120")


async def test_a_realized_override_is_wallet_only_and_says_so(w):
    await market(w.c, "BUY", 10, "100")
    c1 = await market(w.c, "SELL", 5, "110")
    c2 = await market(w.c, "SELL", 5, "120")
    fills = [(t.price, t.pnl_inr) for t in (await _fill(c1), await _fill(c2))]
    before = await _ledger(w)

    p = await _edit(w, {"realized_pnl": "100"})

    assert Decimal(str(p.realized_pnl)) == Decimal("100")
    assert await _ledger(w) - before == Decimal("-50")  # the capability stays
    assert [(t.price, t.pnl_inr) for t in (await _fill(c1), await _fill(c2))] == fills  # not rewritten
    [rev] = await WalletTransaction.find(
        WalletTransaction.user_id == w.c.id, WalletTransaction.transaction_type == TransactionType.REVERSAL
    ).to_list()
    assert "fills" in rev.narration.lower()
    [log] = await AuditLog.find(AuditLog.entity_id == str(p.id)).to_list()
    assert "wallet-only" in log.metadata["narration"].lower()


async def test_a_double_submitted_edit_moves_the_book_once(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 10, "110")  # +100
    p = await _pos(w)

    out = await asyncio.gather(
        *(trading.admin_edit_position(str(p.id), {"avg_price": "101"}, w.sa, None) for _ in range(2)),
        return_exceptions=True,
    )

    failed = [o for o in out if isinstance(o, Exception)]
    assert len(failed) == 1 and getattr(failed[0], "status_code", None) == 409, out
    _one_book(await _books(w), "90")  # the wallet moved -10 twice


async def test_a_closed_avg_edit_keeps_a_specific_lot_exit_on_its_lot(w):
    await market(w.c, "BUY", 10, "100")
    lot = await market(w.c, "BUY", 10, "200")
    await market(w.c, "SELL", 10, "250", basis="200")  # Active-tab Exit of the 200 lot: +500
    await market(w.c, "SELL", 10, "260")  # the rest off the re-derived avg 100: +1600
    await _edit(w, {"avg_price": "101"})  # every unit cost 1 more

    b = await _books(w)
    _one_book(b, "2080")
    assert [r["entry_price"] for r in b.rows] == [201.0, 101.0], _show(b)
    stored = (await _fill(lot)).executed_at
    assert b.rows[0]["opened_at"].replace(tzinfo=None) == stored.replace(tzinfo=None)  # still its lot


async def test_a_closed_avg_edit_books_each_close_at_its_own_fx(w, monkeypatch):
    monkeypatch.setitem(market_data_service._usd_inr_cache, "rate", 97.0)
    await market(w.c, "BUY", 10, "100", instrument=USD)
    await market(w.c, "SELL", 5, "110", instrument=USD)  # +50 USD at 97
    monkeypatch.setitem(market_data_service._usd_inr_cache, "rate", 90.0)
    await market(w.c, "SELL", 5, "120", instrument=USD)  # +100 USD at 90
    before = await _ledger(w)

    await _edit(w, {"avg_price": "101"})

    assert await _ledger(w) - before == Decimal("-935")  # −5 × 97 − 5 × 90
    _one_book(await _books(w), "140")


async def test_a_closed_avg_edit_on_a_weekly_settled_cycle(w, monkeypatch):
    await market(w.c, "BUY", 10, "100")
    db = Trade.get_motor_collection().database  # a week passes before the Saturday job
    for coll, fields in {"trades": ("executed_at",), "positions": ("opened_at",)}.items():
        await db[coll].update_many({}, [{"$set": {f: {"$subtract": [f"${f}", 86_400_000]} for f in fields}}])

    async def _settle_at(_token):
        return Decimal("105")

    monkeypatch.setattr(market_data_service, "get_ltp", _settle_at)
    batch = SettlementBatch(run_key="T", week_key="2026-W46", started_at=now_utc())
    await batch.insert()
    pos = await Position.find_one(Position.user_id == w.c.id, Position.status == PositionStatus.OPEN)
    out = await weekly_settlement_service._settle_one_position(pos, batch=batch, fx_rate=Decimal(1))
    assert out == "settled"
    before = await _ledger(w)

    await trading.admin_edit_position(str(pos.id), {"avg_price": "101"}, w.sa, None)  # the settled cycle

    assert await _ledger(w) - before == Decimal("-10")
    await market(w.c, "SELL", 10, "110")  # the re-opened lot: +50
    b = await _books(w)
    # Gross only: the settlement drain still drops the drained lot's open-leg
    # brokerage (§4.2.a, a later phase).
    assert b.flat and b.oracle == b.realized == b.gross == Decimal("90"), _show(b)


async def _no_quote(_token: str) -> dict:
    return {}
