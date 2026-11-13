"""§6.6 A reopen must take back exactly what it takes off the statement.

The reopen held three notions of "the cycle": the cash refund reversed the
whole Position.realized_pnl at one fx rate, the brokerage refund came from ONE
fill found within 15 s of closed_at, and the supersede hid a window of closes.
Now one set — the cycle's live closes, opposite to the side the position
holds (`position_service.cycle_closes`) — gives the refund (Σ pnl_inr +
charges, each close at its own rate), the brokerage, the supersede and the
P&L the position keeps (a close the reopen does not undo stands).

Also here: a double-submitted reopen or delete refunds once (an atomic claim
before any wallet write); a reopen records the span the position sat closed,
so another position traded in it is not swept into this one's charges,
netting dialog or delete; and an undo whose cash sits in margin draws it from
there (draw_margin) instead of parking a due the week's P&L never sees.

Forward-only: reopens and deletes made from now on. Every fill goes through
the matching engine; the reopen, delete, positions list and netting dialog are
the real admin handlers. Arbiters: Σ blotter gross == Σ Position.realized_pnl
(== the flat-book oracle when flat) and the wallet ledger == the statement.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from fastapi import HTTPException
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import marketwatch, trading
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import Exchange, OrderAction
from app.models.order import InstrumentRef, Order
from app.models.position import Position, PositionStatus
from app.models.trade import Trade
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service, wallet_service
from tests._real_fill import later, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_reopen_delete_one_window"
CENT = Decimal("0.01")
# What moves the wallet's cash: the week's P&L sums exactly these.
_LEDGER = (
    TransactionType.PNL,
    TransactionType.CHARGES,
    TransactionType.REVERSAL,
    TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY,
)
USD = InstrumentRef(
    token="TUSD1", symbol="TUSD", trading_symbol="TUSD", exchange=Exchange.CRYPTO, segment="FOREX", lot_size=1
)
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

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(trading, "publish", _no_publish)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs

    async def mk(code: str, role: UserRole, cash: str = "1000000", **kw) -> User:
        u = User(
            user_code=code,
            email=f"{code.lower()}@example.com",
            mobile=f"94{abs(hash(code)) % 10**8:08d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            **kw,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128(cash)).insert()
        return u

    try:
        yield SimpleNamespace(
            db=client[_DB],
            c=await mk("ROD001", UserRole.CLIENT),
            sa=await mk("RODSA", UserRole.SUPER_ADMIN),
            mk=mk,
            monkeypatch=monkeypatch,
        )
    finally:
        await client.drop_database(_DB)
        client.close()


async def _later(w, *, minutes: float) -> None:
    await later(w.db, minutes=minutes)


async def _only(user: User) -> Position:
    [p] = await Position.find(Position.user_id == user.id).to_list()
    return p


async def _reopen(w, user: User | None = None) -> Position:
    p = await _only(user or w.c)
    await trading.admin_reopen_position(str(p.id), w.sa, None)
    return await Position.get(p.id)


async def _ledger(user: User) -> Decimal:
    rows = await WalletTransaction.find(
        WalletTransaction.user_id == user.id, {"transaction_type": {"$in": [t.value for t in _LEDGER]}}
    ).to_list()
    return sum((Decimal(str(r.amount)) for r in rows), Decimal(0))


async def _rows(user: User, kind: TransactionType) -> list[WalletTransaction]:
    return await WalletTransaction.find(
        WalletTransaction.user_id == user.id, WalletTransaction.transaction_type == kind
    ).to_list()


async def _books(w) -> SimpleNamespace:
    trades = await Trade.find(Trade.user_id == w.c.id, Trade.superseded_by_reopen != True).to_list()  # noqa: E712
    value = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    for t in trades:
        value[t.action] += Decimal(str(t.price)) * Decimal(str(t.quantity))
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
        oracle=value[OrderAction.SELL] - value[OrderAction.BUY],
        realized=sum((Decimal(str(p.realized_pnl)) for p in positions), Decimal(0)),
        gross=sum((Decimal(str(e["gross_pnl"])) for e in events), Decimal(0)).quantize(CENT),
        net=net.quantize(CENT),
        rows=events,
    )


def _show(b: SimpleNamespace) -> list:
    return [(e["opened_side"], e["entry_price"], e["qty"], e["gross_pnl"], e["brokerage"]) for e in b.rows]


async def test_a_partial_close_cycle_refunds_both_closes_brokerage(w):
    await market(w.c, "BUY", 10, "100")  # brokerage 10
    await market(w.c, "SELL", 4, "110")  # +40, brokerage 4
    await market(w.c, "SELL", 6, "90")  # -60, brokerage 6
    await _later(w, minutes=10)
    await _reopen(w)

    # Both closes left the statement, so both closes' charges come back; the
    # open leg's stays (the lot is open again). Was -16: one close refunded.
    assert await _ledger(w.c) == Decimal("-10")
    b = await _books(w)
    assert b.rows == [] and b.realized == 0, _show(b)

    await _later(w, minutes=10)
    await market(w.c, "SELL", 10, "105")  # +50
    b = await _books(w)
    assert b.oracle == b.realized == b.gross == Decimal("50"), _show(b)
    assert await _ledger(w.c) == b.net == Decimal("30"), _show(b)


async def test_a_reopen_keeps_the_pnl_of_the_close_it_does_not_undo(w):
    await market(w.c, "BUY", 10, "100")  # brokerage 10
    await market(w.c, "SELL", 15, "110")  # closes the long 10 (+100), opens short 5; brokerage 15
    await market(w.c, "BUY", 5, "105")  # +25 on the short; brokerage 5
    await _later(w, minutes=10)
    p = await _reopen(w)

    # Only the short's close is undone: the long's +100 stands (was zeroed,
    # and taken out of the wallet with the rest).
    assert (p.status, p.quantity, Decimal(str(p.avg_price))) == (PositionStatus.OPEN, -5, Decimal("110"))
    b = await _books(w)
    assert b.realized == b.gross == Decimal("100"), _show(b)
    assert await _ledger(w.c) == Decimal("75")  # -10 - 15 + 100

    await _later(w, minutes=10)
    await market(w.c, "BUY", 5, "100")  # +50 on the restored short
    b = await _books(w)
    assert b.oracle == b.realized == b.gross == Decimal("150"), _show(b)


async def test_a_usd_cycle_refunds_each_close_at_its_own_rate(w):
    w.monkeypatch.setitem(market_data_service._usd_inr_cache, "rate", 97.0)
    await market(w.c, "BUY", 10, "100", instrument=USD)
    await market(w.c, "SELL", 5, "110", instrument=USD)  # +50 USD at 97 = 4,850
    w.monkeypatch.setitem(market_data_service._usd_inr_cache, "rate", 90.0)
    await market(w.c, "SELL", 5, "120", instrument=USD)  # +100 USD at 90 = 9,000
    assert await _ledger(w.c) == Decimal("13450")  # 4,850 + 9,000 − 400 charges
    await _later(w, minutes=10)
    p = await _reopen(w)

    # Only the open leg's charge is left. Was +50: both closes were taken back
    # at the close rate 90 (13,500, not 13,850) with one close's charge.
    assert await _ledger(w.c) == Decimal("-200")
    assert Decimal(str(p.realized_pnl)) == 0


async def _both(call) -> list:
    out = await asyncio.gather(call(), call(), return_exceptions=True)
    failed = [o for o in out if isinstance(o, Exception)]
    assert len(failed) == 1 and getattr(failed[0], "status_code", None) == 409, out
    return out


async def test_a_double_reopen_refunds_once(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 10, "110")  # +100
    await _later(w, minutes=10)
    p = await _only(w.c)

    await _both(lambda: trading.admin_reopen_position(str(p.id), w.sa, None))
    assert len(await _rows(w.c, TransactionType.REVERSAL)) == 1
    assert await _ledger(w.c) == Decimal("-10")  # the open leg's charge
    assert (await Position.get(p.id)).status == PositionStatus.OPEN


async def test_a_double_delete_refunds_once(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 10, "110")  # +100
    await _later(w, minutes=10)
    p = await _only(w.c)

    await _both(lambda: trading.delete_position(str(p.id), w.sa, None))
    assert len(await _rows(w.c, TransactionType.REVERSAL)) == 1
    assert await _ledger(w.c) == 0
    assert await Position.get(p.id) is None


async def _closed_row(w, p: Position) -> dict:
    rows = (
        await trading.list_positions(
            w.sa,
            user_id=str(w.c.id),
            status="CLOSED",
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
    [row] = [r for r in rows if r["id"] == str(p.id)]
    return row


async def test_a_position_traded_while_another_sat_closed_stays_its_own(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "110")  # P closes: +100
    p = await _only(w.c)
    await _later(w, minutes=5)
    q_open = await market(w.c, "BUY", 5, "120")  # Q, while P sits closed
    await _later(w, minutes=1)
    q_close = await market(w.c, "SELL", 5, "130")  # +50
    await _later(w, minutes=5)
    await trading.admin_reopen_position(str(p.id), w.sa, None)
    await _later(w, minutes=5)
    await market(w.c, "SELL", 10, "105")  # P again: +50
    p = await Position.get(p.id)

    # P's charges: its open leg and its standing close — not Q's open leg.
    assert Decimal((await _closed_row(w, p))["charges"]) == Decimal("20")  # was 25
    entries = (await trading.position_netting_entries(str(p.id), w.sa, None)).data["entries"]
    assert [(e["type"], e["volume"]) for e in entries] == [("Entry", 10.0), ("Exit", 10.0)]  # was + Q's legs
    assert len(p.reopen_gaps) == 1

    await trading.delete_position(str(p.id), w.sa, None)
    for q in (q_open, q_close):
        t = await Trade.get(q.id)
        assert not t.superseded_by_reopen and not t.fifo_deleted
        assert await Order.get(t.order_id) is not None
    assert await _ledger(w.c) == Decimal("40")  # Q's footprint alone: -5 + 50 - 5
    b = await _books(w)
    assert b.realized == b.gross == Decimal("50"), _show(b)


@pytest.mark.parametrize("undo", ["delete", "reopen"])
async def test_an_undo_whose_cash_sits_in_margin_leaves_the_week(w, undo):
    # Auto settlement: a debit past free cash floors at 0 and books the rest.
    c = await w.mk("ROD002", UserRole.CLIENT, cash="1000", auto_settlement=True)
    await market(c, "BUY", 10, "100")  # brokerage 10
    await market(c, "SELL", 10, "150")  # +500, brokerage 10: 1,480 free
    a = await _only(c)
    await market(c, "BUY", 10, "100", instrument=OTHER, margin="1400")  # the profit now backs B: 70 free
    await _later(w, minutes=10)
    if undo == "delete":
        await trading.delete_position(str(a.id), w.sa, None)
    else:
        await trading.admin_reopen_position(str(a.id), w.sa, None)

    # The 480 the undo takes back comes out of B's margin, as a close loss
    # would; was 70 from cash and a 410 due the week never counted.
    assert await _rows(c, TransactionType.SETTLEMENT_OUTSTANDING_BOOKED) == []
    left = Decimal("-10") if undo == "delete" else Decimal("-20")  # B's open leg (+ A's, open again)
    assert await _ledger(c) == left
    wallet = await Wallet.find_one(Wallet.user_id == c.id)
    assert Decimal(str(wallet.settlement_outstanding)) == 0


async def test_a_position_traded_inside_anothers_closed_span_keeps_its_fills(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "110")  # P closes: +100
    p = await _only(w.c)
    await _later(w, minutes=5)
    q_open = await market(w.c, "BUY", 5, "120")  # Q, while P sits closed
    await _later(w, minutes=1)
    await market(w.c, "SELL", 5, "130")  # +50
    await _later(w, minutes=5)
    await trading.admin_reopen_position(str(p.id), w.sa, None)
    await _later(w, minutes=5)
    await market(w.c, "SELL", 10, "105")  # P again: +50 — P's life now spans Q's
    [q] = [x for x in await Position.find(Position.user_id == w.c.id).to_list() if x.id != p.id]
    was, ledger = await _books(w), await _ledger(w.c)

    await trading.delete_position(str(q.id), w.sa, None)

    assert (await Trade.get(q_open.id)).fifo_deleted  # was left live: Q's fills fell inside P's span
    b = await _books(w)
    assert b.oracle == b.realized == b.gross == Decimal("50"), _show(b)  # was 50 against 100
    # Q's footprint leaves the wallet and the statement alike (was -50 and 0).
    # (P's own re-close row lacks its open leg's charge: Q traded after P's
    # close, so the reopen kept the legacy flag — the phase-3 guard.)
    assert await _ledger(w.c) - ledger == b.net - was.net == Decimal("-40"), _show(b)


async def test_a_reopen_undoes_only_the_closes_that_took_the_units_back(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 4, "110")  # +40: the client's own partial close
    await market(w.c, "BUY", 4, "120")  # back to 10 @ 108
    await market(w.c, "SELL", 10, "115")  # +70: the close the admin undoes
    await _later(w, minutes=10)
    p = await _reopen(w)

    assert (p.quantity, Decimal(str(p.avg_price))) == (10, Decimal("108"))
    b = await _books(w)
    assert b.realized == b.gross == Decimal("40"), _show(b)  # the partial close stands
    assert await _ledger(w.c) == Decimal("22")  # -10 + 40 - 4 - 4: was -14, both closes undone

    await _later(w, minutes=10)
    await market(w.c, "SELL", 10, "115")  # +70
    b = await _books(w)
    assert b.oracle == b.realized == b.gross == Decimal("110"), _show(b)
    assert await _ledger(w.c) == b.net, _show(b)  # was 46 against 60


async def test_a_reopen_brings_back_what_the_undone_close_took(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 6, "110")  # +60, stands
    await market(w.c, "BUY", 2, "100")  # 6 @ 100; the peak stays 10
    await market(w.c, "SELL", 6, "115")  # +90, undone
    await _later(w, minutes=10)
    p = await _reopen(w)

    assert (p.quantity, p.opening_quantity, Decimal(str(p.avg_price))) == (6, 6, Decimal("100"))  # was 10
    await _later(w, minutes=10)
    await market(w.c, "SELL", 6, "120")  # +120
    b = await _books(w)
    assert b.oracle == b.realized == b.gross == Decimal("180"), _show(b)
    assert await _ledger(w.c) == b.net, _show(b)


async def test_a_delete_while_a_reopen_holds_the_position_waits_its_turn(w):
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "SELL", 10, "110")  # +100
    await _later(w, minutes=10)
    p = await _only(w.c)
    real, seen = wallet_service.adjust, []

    async def adjust(*a, **k):  # the reopen's first wallet write: its claim is held
        if not seen:
            seen.append("reopen")
            try:
                await trading.delete_position(str(p.id), w.sa, None)
            except HTTPException as e:
                seen.append(e.status_code)
        return await real(*a, **k)

    w.monkeypatch.setattr(wallet_service, "adjust", adjust)
    await trading.admin_reopen_position(str(p.id), w.sa, None)

    assert seen == ["reopen", 409]  # was: both ran, two REVERSALs, an OPEN doc with no fills
    assert await _ledger(w.c) == Decimal("-10")  # the reopen won: its open leg's charge
    p = await Position.get(p.id)
    assert (p.status, p.quantity) == (PositionStatus.OPEN, 10)


async def test_a_usd_delete_reverses_each_close_at_its_own_rate(w):
    w.monkeypatch.setitem(market_data_service._usd_inr_cache, "rate", 97.0)
    await market(w.c, "BUY", 10, "100", instrument=USD)
    await market(w.c, "SELL", 5, "110", instrument=USD)  # +50 USD at 97 = 4,850
    w.monkeypatch.setitem(market_data_service._usd_inr_cache, "rate", 90.0)
    await market(w.c, "SELL", 5, "120", instrument=USD)  # +100 USD at 90 = 9,000
    await _later(w, minutes=10)

    await trading.delete_position(str((await _only(w.c)).id), w.sa, None)

    assert await _ledger(w.c) == 0  # was +350: 150 USD taken back at 90


async def test_a_reopen_it_cannot_margin_still_locks_it(w):
    c = await w.mk("ROD003", UserRole.CLIENT, cash="1000", auto_settlement=True)
    await market(c, "BUY", 10, "100")
    await market(c, "SELL", 10, "150")  # +500
    a = await _only(c)
    await market(c, "BUY", 10, "100", instrument=OTHER, margin="1400")  # the profit backs B
    await _later(w, minutes=10)
    await trading.admin_reopen_position(str(a.id), w.sa, None)  # no free cash left to lock A's margin

    wallet = await Wallet.find_one(Wallet.user_id == c.id)
    opened = await Position.find(Position.user_id == c.id, Position.status == PositionStatus.OPEN).to_list()
    locked = sum((Decimal(str(x.margin_used)) for x in opened), Decimal(0))
    assert len(opened) == 2 and locked > 0
    assert Decimal(str(wallet.used_margin)) == locked  # was A's margin short: freed when A closes


async def test_a_backdated_add_in_a_reopen_gap_is_the_positions_own(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "110")  # +100
    p = await _only(w.c)
    await _later(w, minutes=10)
    await trading.admin_reopen_position(str(p.id), w.sa, None)
    await _later(w, minutes=10)
    p = await Position.get(p.id)
    add = await market(w.c, "BUY", 5, "100")  # adds to P
    # Super-admin "Book as a past trade": the client says they added while P
    # was wrongly closed.
    await marketwatch._stamp_history(await Order.get(add.order_id), p.reopened_at - timedelta(minutes=5))

    await trading.delete_position(str(p.id), w.sa, None)

    assert (await Trade.get(add.id)).fifo_deleted
    assert await _ledger(w.c) == 0  # was -5: the add's charge never came back
    b = await _books(w)
    assert b.rows == [] and b.oracle == 0, _show(b)
