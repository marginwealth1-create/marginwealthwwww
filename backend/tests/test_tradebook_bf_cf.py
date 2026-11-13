"""§4.1 Brought Forward and Carry Forward, with no mark-to-market.

Brought Forward is the positions the period inherited, read off the Position
docs rather than replayed from fills: a position that opened before the window
counts even if it closed inside it — it was still open on day one — and its
quantity is `opening_quantity`, since `quantity` is 0 once it closes. A
position that closed before the window, or opened inside it, is not brought
forward. Carry Forward is what the period hands on. Neither prints a live
price: a re-download weeks later must print the same rows.
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
from tests._real_fill import INSTRUMENT, later, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_tradebook_bf_cf"
CLOSED_BEFORE = INSTRUMENT.model_copy(update={"token": "TBF2", "symbol": "TBFB"})
OPENED_INSIDE = INSTRUMENT.model_copy(update={"token": "TBF3", "symbol": "TBFC"})
STILL_OPEN = INSTRUMENT.model_copy(update={"token": "TBF4", "symbol": "TBFD"})
MTM = {"ltp", "current_price", "total_pnl", "unrealized_pnl"}


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
