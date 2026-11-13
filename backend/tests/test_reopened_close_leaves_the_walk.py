"""§2.6 A reopened close must leave the walk — from here on.

An admin reopen flags the cycle's closing fills `superseded_by_reopen`, which
only hid their rows: the undone close still took its lots off the FIFO book
and the reopen put nothing back. The re-close then found an empty book (its
open leg's brokerage gone with the hidden row), or a short one (units it could
not pair dropped off the statement).

Forward-only: a reopen from now on stamps `superseded_at` and the walk leaves
those fills out, so their lots come back. A fill flagged before carries no
stamp and is walked exactly as on the statements already sent. The stamp is
held back where taking the closes out would re-pair what was already billed
(something traded on the script since) or leave lots the reopen did not
restore (the cycle re-added after a partial close).

Every fill goes through the matching engine; the reopen and the EOD carry are
the real code paths. The arbiter is the flat-book oracle over the fills that
still stand: Σ(sell value) − Σ(buy value) == Σ Position.realized_pnl == Σ
blotter gross.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import trading
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import OrderAction, ProductType
from app.models.position import Position
from app.models.trade import Trade
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, position_service, tradebook_builder
from app.utils.time_utils import now_utc, to_ist
from tests._real_fill import INSTRUMENT, later, market, set_commission_per_lot, settle_weekly

_DB = f"{settings.MONGODB_DB_NAME}_test_reopened_close_leaves_the_walk"
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
            mobile=f"92{abs(hash(code)) % 10**8:08d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128("1000000")).insert()
        return u

    try:
        db = client[_DB]
        yield SimpleNamespace(
            db=db,
            c=await mk("ROW001", UserRole.CLIENT),
            sa=await mk("ROWSA", UserRole.SUPER_ADMIN),
            monkeypatch=monkeypatch,
        )
    finally:
        await client.drop_database(_DB)
        client.close()


async def _later(w, *, minutes: float) -> None:
    await later(w.db, minutes=minutes)


async def _reopen(w) -> Position:
    [p] = await Position.find(Position.user_id == w.c.id).to_list()
    await trading.admin_reopen_position(str(p.id), w.sa, None)
    return p


async def _books(w) -> SimpleNamespace:
    trades = await Trade.find(Trade.user_id == w.c.id, Trade.superseded_by_reopen != True).to_list()  # noqa: E712
    value = {OrderAction.BUY: Decimal(0), OrderAction.SELL: Decimal(0)}
    for t in trades:
        value[t.action] += Decimal(str(t.price)) * Decimal(str(t.quantity))
    positions = await Position.find(Position.user_id == w.c.id).to_list()
    events, _ = await position_service.list_closed_trade_events_fifo(w.c.id, limit=10_000)
    events.reverse()  # oldest close first
    return SimpleNamespace(
        oracle=value[OrderAction.SELL] - value[OrderAction.BUY],
        realized=sum((Decimal(str(p.realized_pnl)) for p in positions), Decimal(0)),
        gross=sum((Decimal(str(e["gross_pnl"])) for e in events), Decimal(0)).quantize(CENT),
        units=sum((Decimal(str(e["qty"])) for e in events), Decimal(0)),
        brokerage=sum((Decimal(str(e["brokerage"])) for e in events), Decimal(0)).quantize(CENT),
        charges=sum((Decimal(str(t.total_charges)) for t in trades), Decimal(0)),
        rows=events,
    )


def _show(b: SimpleNamespace) -> list:
    return [(e["opened_side"], e["entry_price"], e["qty"], e["gross_pnl"], e["brokerage"]) for e in b.rows]


async def _paired_with(row: dict, t: Trade) -> bool:
    """Was this row's open leg fill `t` (its stored clock, after `_later`)?"""
    stored = (await Trade.get(t.id)).executed_at
    return row["opened_at"].replace(tzinfo=None) == stored.replace(tzinfo=None)


async def _stamped(t: Trade) -> bool:
    t = await Trade.get(t.id)
    assert t.superseded_by_reopen
    return getattr(t, "superseded_at", None) is not None


async def _reopen_then_reclose(w) -> SimpleNamespace:
    opened = await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    undone = await market(w.c, "SELL", 10, "110")  # +100, undone by the reopen
    await _later(w, minutes=10)
    await _reopen(w)
    await _later(w, minutes=10)
    await market(w.c, "SELL", 10, "130")  # the real close: +300
    return SimpleNamespace(opened=opened, undone=undone)


async def test_a_reopened_close_gives_its_lot_back(w):
    f = await _reopen_then_reclose(w)
    assert await _stamped(f.undone)

    b = await _books(w)
    assert b.oracle == b.realized == Decimal("300")
    assert b.gross == b.oracle, _show(b)
    [row] = b.rows
    assert await _paired_with(row, f.opened), _show(b)  # was the re-close's own clock
    assert b.brokerage == b.charges == Decimal("20"), _show(b)  # was 10: the open leg's charge


async def test_an_add_after_the_reopen_bills_every_unit(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "110")
    await _later(w, minutes=10)
    await _reopen(w)  # 10 @100 again
    await _later(w, minutes=10)
    await market(w.c, "BUY", 10, "120")  # avg 110
    await _later(w, minutes=1)
    await market(w.c, "SELL", 20, "130")  # +400

    b = await _books(w)
    assert b.oracle == b.realized == Decimal("400")
    assert b.gross == b.oracle, _show(b)
    assert b.units == 20, _show(b)  # was 10: the units with no lot left fell off
    assert b.brokerage == b.charges == Decimal("40"), _show(b)  # was 20


async def test_a_close_flagged_before_the_stamp_is_still_walked(w):
    f = await _reopen_then_reclose(w)
    # A reopen made before superseded_at existed set the flag alone.
    await Trade.get_motor_collection().update_many({}, {"$unset": {"superseded_at": ""}})

    b = await _books(w)
    # The pairing the statements already sent: the undone close took the lot
    # and the re-close is recovered from its own stamp. Its row now also
    # carries the open leg the undone close took (was 10.0: the close leg only,
    # so the column fell short of the ₹20 charged).
    [row] = b.rows
    assert (row["qty"], row["gross_pnl"], row["brokerage"]) == (10.0, 300.0, 20.0), _show(b)
    assert not await _paired_with(row, f.opened), _show(b)
    assert b.brokerage == b.charges == Decimal("20"), _show(b)


async def test_a_legacy_reopen_carries_forward_the_units_it_held(w):
    # The flag-only close took the lot, so the walk held nothing for the
    # restored units: Carry Forward fell back to today's quantity (0 once
    # the re-close came) while the next day's Brought Forward read the peak.
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "110")
    await _later(w, minutes=10)
    await _reopen(w)
    await Trade.get_motor_collection().update_many({}, {"$unset": {"superseded_at": ""}})
    await _later(w, minutes=2 * 24 * 60)  # held over two IST midnights
    await market(w.c, "SELL", 10, "130")

    async def on(days_ago: int) -> dict:
        day = to_ist(now_utc() - timedelta(days=days_ago)).date().isoformat()
        return await tradebook_builder.statement_payload(w.c, *tradebook_builder.window(day, day))

    def book(rows) -> list:
        return [(r["side"], r["qty"], r["price"]) for r in rows]

    d2, d1, d0 = await on(2), await on(1), await on(0)
    assert book(d2["carry_forward"]) == [("BUY", 10, 100)]  # was 0 units
    assert book(d1["brought_forward"]) == book(d1["carry_forward"]) == book(d2["carry_forward"])
    assert book(d0["brought_forward"]) == book(d1["carry_forward"])


async def test_a_legacy_reopen_bills_its_open_leg_once(w):
    # Opened and undone on one day, re-closed two days on: the open leg was
    # on no row and no Carry Forward (no lot), though the client paid it.
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "110")
    await _later(w, minutes=10)
    await _reopen(w)
    await Trade.get_motor_collection().update_many({}, {"$unset": {"superseded_at": ""}})
    await _later(w, minutes=2 * 24 * 60)
    await market(w.c, "SELL", 10, "130")

    async def on(days_ago: int) -> dict:
        day = to_ist(now_utc() - timedelta(days=days_ago)).date().isoformat()
        return await tradebook_builder.statement_payload(w.c, *tradebook_builder.window(day, day))

    def billed(s: dict) -> Decimal:
        return sum(
            (Decimal(str(r["brokerage"])) for r in s["closed_transactions"] if "brokerage" in r),
            Decimal(0),
        ) + sum((Decimal(str(d["commission"])) for d in s["carry_forward"]), Decimal(0))

    d2, d0 = await on(2), await on(0)
    assert [Decimal(str(d["commission"])) for d in d2["carry_forward"]] == [Decimal("10")]  # was 0
    assert billed(d2) == Decimal(str(d2["financial"]["brokerage"])) == Decimal("10")
    assert billed(d0) == Decimal(str(d0["financial"]["brokerage"])) == Decimal("10")  # the close leg


async def test_a_weekly_settlement_of_a_legacy_reopen_carries_its_open_leg(w, monkeypatch):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "110")
    await _later(w, minutes=10)
    await _reopen(w)
    await Trade.get_motor_collection().update_many({}, {"$unset": {"superseded_at": ""}})
    await _later(w, minutes=10)
    await settle_weekly(w.c, monkeypatch, "120")  # +200, re-opens 10 @ 120
    await _later(w, minutes=10)
    await market(w.c, "SELL", 10, "125")  # +50

    b = await _books(w)
    assert b.gross == b.realized == Decimal("250"), _show(b)
    assert b.brokerage == b.charges == Decimal("20"), _show(b)  # was 10: the open leg lost
    assert [r["brokerage"] for r in b.rows] == [10.0, 10.0], _show(b)


async def test_a_reopen_after_a_later_round_trip_keeps_the_legacy_flag(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    undone = await market(w.c, "SELL", 10, "110")  # P1 +100
    [p1] = await Position.find(Position.user_id == w.c.id).to_list()
    await _later(w, minutes=5)
    p2_open = await market(w.c, "BUY", 10, "200")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "210")  # P2 +100, already billed
    await _later(w, minutes=5)
    await trading.admin_reopen_position(str(p1.id), w.sa, None)
    await _later(w, minutes=5)
    await market(w.c, "SELL", 10, "130")  # P1 again: +300

    # Taking the undone close out would hand P1's lot to P2's billed close.
    assert not await _stamped(undone)
    b = await _books(w)
    assert b.oracle == b.realized == Decimal("400")
    assert b.gross == b.oracle, _show(b)
    p2_row = b.rows[0]
    assert await _paired_with(p2_row, p2_open), _show(b)


async def test_a_cycle_re_added_after_a_partial_close_undoes_only_the_last_close(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    first = await market(w.c, "SELL", 5, "110")  # +50, the client's own partial close
    await _later(w, minutes=1)
    await market(w.c, "BUY", 5, "120")  # back to 10 @ 110; 15 units bought
    await _later(w, minutes=1)
    last = await market(w.c, "SELL", 10, "130")
    await _later(w, minutes=10)
    await _reopen(w)  # the 10 coming back are what the last close took
    await _later(w, minutes=10)
    await market(w.c, "SELL", 10, "140")  # +300 against the restored avg 110
    await _later(w, minutes=5)
    rt_open = await market(w.c, "BUY", 5, "150")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 5, "160")  # +50

    # The partial close stands; the undone one leaves the walk. Was: both
    # undone (15 units for 10 restored), flag only, oracle -150 vs 350.
    assert not (await Trade.get(first.id)).superseded_by_reopen and await _stamped(last)
    b = await _books(w)
    assert b.oracle == b.realized == Decimal("400")
    assert b.gross == b.realized, _show(b)
    # No lot the reopen did not restore is left to pair the next round trip.
    assert await _paired_with(b.rows[-1], rt_open), _show(b)


async def test_the_netting_dialog_leaves_out_the_undone_close(w):
    await market(w.c, "BUY", 10, "100")
    await _later(w, minutes=1)
    await market(w.c, "SELL", 10, "110")
    await _later(w, minutes=10)
    p = await _reopen(w)

    async def _ltp(_token):
        return Decimal("105")

    w.monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    out = (await trading.position_netting_entries(str(p.id), w.sa, None)).data
    assert [(e["type"], e["volume"]) for e in out["entries"]] == [("Entry", 10.0)]  # was + Exit 10


async def _no_quote(_token: str) -> dict:
    return {}


async def test_a_carried_positions_reopen_stamps_its_pre_carry_close(w):
    opened = await market(w.c, "BUY", 10, "100")
    pre = await market(w.c, "SELL", 4, "102")  # MIS-stamped, before the carry: +8
    with patch.object(market_data_service, "get_quote", _no_quote):
        out = await position_service.convert_intraday_to_carry({INSTRUMENT.segment})
    assert out["converted"] == 1, out
    post = await market(w.c, "SELL", 6, "105", product=ProductType.NRML)  # +30
    await _reopen(w)  # undoes all 38
    await market(w.c, "SELL", 10, "103", product=ProductType.NRML)  # +30

    assert await _stamped(pre) and await _stamped(post)
    b = await _books(w)
    assert b.oracle == b.realized == Decimal("30")
    assert b.gross == b.oracle, _show(b)
    [row] = b.rows
    assert await _paired_with(row, opened), _show(b)
    assert b.brokerage == b.charges == Decimal("20"), _show(b)


@pytest.mark.parametrize(
    "book",
    [
        ("BUY", "SELL", "120", "115"),  # long: the lot tapped sits above the average
        ("SELL", "BUY", "100", "105"),  # short: below it
    ],
)
async def test_a_reopen_restores_what_the_units_cost_not_a_re_derived_average(w, book):
    # A specific-lot Exit re-derives the survivors' average (110 → 106.67 for
    # the long). The reopen put all 20 units back at that average, though they
    # cost 110: the re-close booked +166.67 on a flat book that made +100.
    side, back, tapped, close = book
    for px in ("100", "120"):
        await market(w.c, side, 10, px)
    await market(w.c, back, 5, close, basis=tapped)
    await market(w.c, back, 15, close)
    await _later(w, minutes=10)
    p = await _reopen(w)
    assert Decimal(str((await Position.get(p.id)).avg_price)) == Decimal("110")
    await _later(w, minutes=10)
    await market(w.c, back, 20, close)

    b = await _books(w)
    assert b.oracle == b.realized == Decimal("100"), _show(b)
    assert b.gross == b.oracle, _show(b)
