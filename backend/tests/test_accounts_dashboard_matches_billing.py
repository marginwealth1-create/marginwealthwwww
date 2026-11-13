"""§5 One weekly P&L: an accounts-dashboard row reads that user's Billing statement.

The row and the KPI cards summed realized_pnl of the positions CLOSED in the
window. A position still open at the window's end was left out with every
close it made inside it, and a position closed inside the window brought in
whatever its earlier closes booked. Seen on production (SY69XY, 9-15 Nov): a
NATURALGAS short still open had closed 3,750 units at +65,000, so the row read
-51,075 against the statement's +13,925.

Now both read the statement's figures: each close in the window at what the
wallet booked, weekly / expiry settlements included, undone fills out;
brokerage is every charge on a fill in the window.
"""

from __future__ import annotations

from decimal import Decimal

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.core.database import _document_models
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import accounts_dashboard_service as ads
from app.services import tradebook_builder
from app.utils.time_utils import now_utc, to_ist
from tests._real_fill import INSTRUMENT, market, set_commission_per_lot, settle_weekly

_DB = f"{settings.MONGODB_DB_NAME}_test_accounts_dashboard_matches_billing"
HELD = INSTRUMENT.model_copy(update={"token": "TAD1", "symbol": "TADHELD"})
ROUND = INSTRUMENT.model_copy(update={"token": "TAD2", "symbol": "TADROUND"})
SETTLED = INSTRUMENT.model_copy(update={"token": "TAD3", "symbol": "TADSETTLED"})


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs
    u = User(
        user_code="TAD001",
        email="tad001@example.com",
        mobile="9351654321",
        full_name="TAD001",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
    )
    await u.insert()
    await Wallet(user_id=u.id, available_balance=Decimal128("1000000")).insert()
    try:
        yield u
    finally:
        await client.drop_database(_DB)
        client.close()


async def test_the_row_and_the_cards_read_the_billing_statement(w, monkeypatch):
    u = w
    await market(u, "BUY", 3, "50", instrument=SETTLED)
    await settle_weekly(u, monkeypatch, "60")  # +30, the position re-opens
    await market(u, "BUY", 5, "200", instrument=ROUND)
    await market(u, "SELL", 5, "190", instrument=ROUND)  # -50
    await market(u, "BUY", 10, "100", instrument=HELD)
    await market(u, "SELL", 4, "110", instrument=HELD)  # +40, still open

    day = to_ist(now_utc()).date().isoformat()
    lo, hi = tradebook_builder.window(day, day)
    fin = (await tradebook_builder.statement_payload(u, lo, hi))["financial"]
    assert (fin["gross_pnl"], fin["brokerage"]) == (20.0, 27.0)  # 3 + 5 + 5 + 10 + 4

    [row] = (await ads.get_entity_users(None, "SUPER_ADMIN", lo, hi, user_id=u.id))["items"]
    assert Decimal(row["net_pnl"]) == Decimal("20.00")  # was -20.00: the open position's +40 missing
    assert Decimal(row["net_bkg"]) == Decimal("27.00")
    assert Decimal(row["total_pnl"]) == Decimal(str(fin["net_pnl"])) == Decimal("-7.00")

    cards = await ads.client_totals_for_users([u.id], lo, hi)
    assert cards["net_client_pnl"] == Decimal("20.00")
    assert cards["net_client_bkg"] == Decimal("27.00")
