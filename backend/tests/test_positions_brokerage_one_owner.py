"""§7.1 A fill belongs to exactly one position.

The admin Positions page gave each row the charges of every fill inside its
time window. Windows overlap: a scalper's re-entry two seconds after a close
falls in both rows' 5 s slack, and a carried NRML row (opened as MIS, flipped
by the EOD rollover) took every MIS round trip on its token since it opened.
So one fill was billed on several rows and the column added up to far more
than the client was charged.

Now every live fill of the book goes to ONE position, closed neighbours off
the page included: Σ per-row Brokerage over the Open and Closed tabs is Σ the
charges on the client's live fills, which is the Billing statement's and the
Accounts dashboard's Brokerage. One index scalper, every order a real fill.
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
from app.api.v1.user import positions as positions_api
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import ProductType
from app.models.position import Position, PositionStatus
from app.models.trade import LIVE_FILL, Trade
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import accounts_dashboard_service as ads
from app.services import position_service, tradebook_builder
from app.utils.decimal_utils import to_decimal
from app.utils.time_utils import now_utc
from tests._real_fill import INSTRUMENT, later, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_positions_brokerage_one_owner"
IDX = INSTRUMENT.model_copy(update={"token": "TPB1", "symbol": "TPBNIFTY"})


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(trading, "publish", _no_publish)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs: a fill's charge is its size
    try:
        a1 = User(
            user_code="TPB001",
            email="tpb1@example.com",
            mobile="9380000001",
            full_name="TPB admin",
            password_hash="x",
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        await a1.insert()
        c = User(
            user_code="TPB002",
            email="tpb2@example.com",
            mobile="9380000002",
            full_name="TPB scalper",
            password_hash="x",
            role=UserRole.CLIENT,
            status=UserStatus.ACTIVE,
            assigned_admin_id=a1.id,
        )
        await c.insert()
        await Wallet(user_id=c.id, available_balance=Decimal128("100000")).insert()
        yield SimpleNamespace(db=client[_DB], a1=a1, c=c)
    finally:
        await client.drop_database(_DB)
        client.close()


async def _newest(c: User) -> Position:
    return await Position.find(Position.user_id == c.id).sort("-opened_at").first_or_none()


async def _round_trip(w, qty: int, exit_px: str, *, hold: float = 30) -> Position:
    await market(w.c, "BUY", qty, "100", instrument=IDX)
    await later(w.db, seconds=hold)
    await market(w.c, "SELL", qty, exit_px, instrument=IDX)
    return await _newest(w.c)


async def _scalper_day(w) -> dict[str, Position]:
    """One token, one client. The charge on every fill is its size."""
    # Yesterday's carry: 10 bought MIS, flipped to NRML by the EOD rollover —
    # which rewrites only the Position doc; the fill stays MIS.
    await market(w.c, "BUY", 10, "100", instrument=IDX)
    carried = await _newest(w.c)
    carried.product_type = ProductType.NRML  # what convert_intraday_to_carry._carry writes
    await carried.save()
    await later(w.db, minutes=1)
    # MIS round trips on the same token while the NRML row stays open; the
    # second re-enters two seconds after the first closed.
    m1 = await _round_trip(w, 2, "101")
    await later(w.db, seconds=2)
    m2 = await _round_trip(w, 3, "99")
    await later(w.db, minutes=1)
    m3 = await _round_trip(w, 4, "102", hold=60)
    await later(w.db, minutes=1)
    # A round trip the admin deletes: its fills are refunded, on no row.
    gone = await _round_trip(w, 6, "97")
    await trading.delete_position(str(gone.id), w.a1, None)
    await later(w.db, minutes=1)
    m4 = await _round_trip(w, 5, "101")
    await later(w.db, minutes=1)
    # The admin reopens m3: its close is undone, m4 traded while it sat closed.
    await trading.admin_reopen_position(str(m3.id), w.a1, None)
    return {"carried": carried, "m1": m1, "m2": m2, "m3": m3, "m4": m4}


async def _page(w, **kw) -> list[dict]:
    args = {"user_id": str(w.c.id), "status": None, "q": None, "product": None, "from_date": None}
    args |= {"to_date": None, "max_hold_sec": None, "own_scope": False, "page": None, "page_size": 25}
    return (await trading.list_positions(w.a1, **(args | kw), _=None)).data


async def test_every_fill_is_billed_on_one_row(w):
    pos = await _scalper_day(w)
    ids = {str(p.id): k for k, p in pos.items()}

    # Open tab (one shot), then the Closed tab one row a page — a row's
    # neighbours are off its page and must still claim their own fills.
    rows = await _page(w)
    for n in (1, 2, 3):
        rows += (await _page(w, status="CLOSED", page=n, page_size=1))["rows"]
    charges = {ids[r["id"]]: Decimal(r["charges"]) for r in rows}
    assert charges == {"carried": 10, "m3": 4, "m1": 4, "m2": 6, "m4": 10}
    assert {r["order_type"] for r in rows} == {"MARKET"}

    # Σ the rows == Σ the live fills' charges == Billing == Accounts dashboard.
    live = await Trade.find({"user_id": w.c.id, **LIVE_FILL}).to_list()
    fills = sum(to_decimal(t.total_charges or t.brokerage or 0) for t in live)
    assert sum(charges.values()) == fills == Decimal(34)
    assert (await tradebook_builder.booked([w.c.id], None, None))[w.c.id][1] == fills
    lo, hi = now_utc() - timedelta(hours=1), now_utc() + timedelta(minutes=1)
    [acc] = (await ads.get_entity_users(None, "SUPER_ADMIN", lo, hi, user_id=w.c.id))["items"]
    assert Decimal(str(acc["net_bkg"])) == fills

    # The client's own Open tab: the carried row keeps its MIS-stamped
    # opening charge, the reopened row only its own.
    mine = {ids[d["id"]]: Decimal(d["charges"]) for d in (await positions_api.open_positions(w.c)).data}
    assert mine == {"carried": 10, "m3": 4}
    assert (await Position.get(pos["m3"].id)).status == PositionStatus.OPEN


async def test_the_legs_dialog_and_the_fill_window_read_the_one_owner(w):
    """The row's netting dialog and position_fills_query (the fills a delete,
    reopen or edit of the row reaches) list exactly the fills the row bills:
    the carried NRML row its own MIS-stamped opening fill and none of the MIS
    round trips, the reopened row none of its neighbour's legs."""
    pos = await _scalper_day(w)
    rows = {k: await Position.get(p.id) for k, p in pos.items()}  # m3 was reopened since
    owned = await position_service.own_fills(list(rows.values()))
    assert len(owned[rows["carried"].id]) == 1
    for k, p in rows.items():
        mine = owned[p.id]
        legs = (await trading.position_netting_entries(str(p.id), w.a1, None)).data["entries"]
        got = [(e["executed_at"], e["side"], e["volume"]) for e in legs]
        want = [(t.executed_at.isoformat(), t.action.value, float(t.quantity)) for t in mine]
        assert got == want, k
        q = await position_service.position_fills_query(p)
        assert {t.id for t in await Trade.find({**q, **LIVE_FILL}).to_list()} == {t.id for t in mine}, k
