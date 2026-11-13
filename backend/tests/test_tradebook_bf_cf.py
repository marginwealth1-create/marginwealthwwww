"""§4.1 Brought Forward and Carry Forward, with no mark-to-market.

Brought Forward is the positions the period inherited: a position that opened
before the window counts even if it closed inside it — it was still open on
day one. A position that closed before the window, or opened inside it, is not
brought forward. Carry Forward is what the period hands on. Which positions
comes from the Position docs; their units and price come from the book as it
stood at the boundary (the units held, the engine's average then) — not
opening_quantity, the lifecycle's peak, nor today's avg_price — so this week's
BF is last week's CF. Neither prints a live price, and a later trade moves
neither: a re-download weeks later must print the same rows.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.core.database import _document_models
from app.models.position import Position
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import tradebook_builder
from app.utils.time_utils import now_utc, to_ist
from tests._real_fill import INSTRUMENT, later, market, set_commission_per_lot, settle_weekly

_DB = f"{settings.MONGODB_DB_NAME}_test_tradebook_bf_cf"
CLOSED_BEFORE = INSTRUMENT.model_copy(update={"token": "TBF2", "symbol": "TBFB"})
OPENED_INSIDE = INSTRUMENT.model_copy(update={"token": "TBF3", "symbol": "TBFC"})
STILL_OPEN = INSTRUMENT.model_copy(update={"token": "TBF4", "symbol": "TBFD"})
MTM = {"ltp", "current_price", "total_pnl", "unrealized_pnl"}
FUT = INSTRUMENT.model_copy(update={"token": "TBF5", "symbol": "NIFTY26NOVFUT", "lot_size": 75})


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    await set_commission_per_lot(1)
    u = User(
        user_code="TBF001",
        email="tbf001@example.com",
        mobile="9357654321",
        full_name="TBF001",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
    )
    await u.insert()
    await Wallet(user_id=u.id, available_balance=Decimal128("100000000")).insert()
    try:
        yield client[_DB], u
    finally:
        await client.drop_database(_DB)
        client.close()


async def test_bf_is_what_was_open_on_day_one_and_cf_what_is_handed_on(w):
    db, u = w
    await market(u, "BUY", 10, "100")  # closed inside the window: still BF
    await market(u, "BUY", 5, "200", instrument=CLOSED_BEFORE)
    await market(u, "SELL", 5, "210", instrument=CLOSED_BEFORE)  # gone before the window
    await market(u, "BUY", 7, "300", instrument=STILL_OPEN)  # BF and CF
    await later(db, minutes=2 * 24 * 60)
    await market(u, "SELL", 10, "110")
    await market(u, "BUY", 3, "50", instrument=OPENED_INSIDE)  # CF only

    # Live prices move; neither table may follow them.
    await Position.get_motor_collection().update_many(
        {}, {"$set": {"ltp": Decimal128("999"), "unrealized_pnl": Decimal128("4893")}}
    )
    day = to_ist(now_utc()).date().isoformat()
    p = await tradebook_builder.statement_payload(u, *tradebook_builder.window(day, day))

    bf = [(r["script"], r["side"], r["qty"], r["price"]) for r in p["brought_forward"]]
    assert bf == [("TBKG", "BUY", 10, 100), ("TBFD", "BUY", 7, 300)]  # TBKG's quantity is 0 now
    cf = [(r["script"], r["qty"], r["price"], r["value"]) for r in p["carry_forward"]]
    assert cf == [("TBFD", 7, 300, 2100), ("TBFC", 3, 50, 150)]
    for r in p["brought_forward"] + p["carry_forward"]:
        assert not MTM & set(r), r
        assert 999 not in r.values()


async def test_no_window_start_brings_nothing_forward(w):
    db, u = w
    await market(u, "BUY", 10, "100")
    await later(db, minutes=60)
    p = await tradebook_builder.statement_payload(u, None, now_utc() + timedelta(hours=1))
    assert p["brought_forward"] == []
    assert [r["script"] for r in p["carry_forward"]] == ["TBKG"]
    assert Decimal(str(p["carry_forward"][0]["commission"])) == Decimal("10")


def _book(rows) -> list[tuple]:
    return [(r["script"], r["side"], r["qty"], r["price"]) for r in rows]


async def _on(u, day: str) -> dict:
    return await tradebook_builder.statement_payload(u, *tradebook_builder.window(day, day))


def _day(dt) -> str:
    return to_ist(dt).date().isoformat()


async def test_this_weeks_bf_is_last_weeks_cf_across_a_settlement_and_an_add(w, monkeypatch):
    """BF read opening_quantity (the position's peak) and both read today's
    avg_price, so an add after the settlement re-opened 75 @ 24,080 printed
    week B's BF as 150 @ 24,090 and rewrote week A's CF to 24,090."""
    db, u = w
    await market(u, "BUY", 150, "24000", instrument=FUT)
    await market(u, "SELL", 75, "24050", instrument=FUT)
    await settle_weekly(u, monkeypatch, "24080")  # re-opens 75 @ 24,080
    await later(db, minutes=7 * 24 * 60)
    week_a = _day(now_utc() - timedelta(days=7))
    sent = (await _on(u, week_a))["carry_forward"]
    assert _book(sent) == [("NIFTY26NOVFUT", "BUY", 75, 24080)]
    assert sent[0]["value"] == 75 * 24080

    await market(u, "BUY", 75, "24100", instrument=FUT)  # week B's add
    assert (await _on(u, week_a))["carry_forward"] == sent  # an issued statement stays put
    assert _book((await _on(u, _day(now_utc())))["brought_forward"]) == _book(sent)


async def test_bf_after_a_partial_close_of_a_pyramid_is_the_average_held(w):
    """A pyramid closed in part keeps the engine's average (105), not the
    FIFO lot left (110) and not the peak (20 units); a later add moves
    neither yesterday's CF nor today's BF."""
    db, u = w
    await market(u, "BUY", 10, "100")
    await market(u, "BUY", 10, "110")
    await market(u, "SELL", 10, "120")  # books (120 − 105) × 10
    await later(db, minutes=24 * 60)
    yesterday = _day(now_utc() - timedelta(days=1))
    sent = (await _on(u, yesterday))["carry_forward"]
    assert _book(sent) == [("TBKG", "BUY", 10, 105)]

    await market(u, "BUY", 10, "130")
    await market(u, "SELL", 5, "140")
    assert (await _on(u, yesterday))["carry_forward"] == sent
    today = await _on(u, _day(now_utc()))
    assert _book(today["brought_forward"]) == _book(sent)
    assert _book(today["carry_forward"]) == [("TBKG", "BUY", 15, 117.5)]


async def test_cf_after_a_specific_lot_exit_is_the_average_the_engine_re_derived(w):
    """Exiting the 110 lot of a 100 / 110 pyramid leaves the engine's average
    at (105 × 20 − 110 × 10) / 10 = 100, not 105."""
    _, u = w
    await market(u, "BUY", 10, "100")
    await market(u, "BUY", 10, "110")
    await market(u, "SELL", 10, "120", basis="110")
    [cf] = (await _on(u, _day(now_utc())))["carry_forward"]
    pos = await Position.find_one(Position.user_id == u.id)
    assert (cf["qty"], cf["price"]) == (10, 100) == (10, float(str(pos.avg_price)))
