"""§7.3 One vocabulary: Gross PNL, Brokerage, Net PNL = Gross − Brokerage.

The accounts dashboard called the pre-brokerage figure "Net PNL" and the
post-brokerage one "Total PNL", while the Billing statement calls the latter
Net PnL: the same words named two different numbers on two screens, a support
ticket on its own. The dashboard's Excel and PDF exports now say what the
statement says, and each label sits over the statement's figure.
"""

from __future__ import annotations

from io import BytesIO

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient
from openpyxl import load_workbook

from app.core.config import settings
from app.core.database import _document_models
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import accounts_dashboard_service as ads
from app.services import tradebook_builder
from app.utils.time_utils import now_utc, to_ist
from tests._real_fill import market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_accounts_dashboard_vocabulary"
OLD = {"Total PNL", "Net BKG", "PNL - Settlement", "PNL-Sett.", "NET CLIENT PNL", "NET CLIENT BKG"}


@pytest_asyncio.fixture
async def u(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs
    user = User(
        user_code="TAV001",
        email="tav001@example.com",
        mobile="9351600101",
        full_name="TAV001",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
    )
    await user.insert()
    await Wallet(user_id=user.id, available_balance=Decimal128("1000000")).insert()
    try:
        yield user
    finally:
        await client.drop_database(_DB)
        client.close()


def _num(v) -> float:
    return float(str(v).replace(",", ""))


async def test_each_export_label_sits_over_the_statement_figure(u, monkeypatch):
    await market(u, "BUY", 5, "200")
    await market(u, "SELL", 5, "210")  # gross +50, brokerage 5 + 5, net +40
    day = to_ist(now_utc()).date().isoformat()
    lo, hi = tradebook_builder.window(day, day)
    fin = (await tradebook_builder.statement_payload(u, lo, hi))["financial"]
    want = {"Gross PNL": fin["gross_pnl"], "Brokerage": fin["brokerage"], "Net PNL": fin["net_pnl"]}
    assert want == {"Gross PNL": 50.0, "Brokerage": 10.0, "Net PNL": 40.0}
    [row] = (await ads.get_entity_users(None, "SUPER_ADMIN", lo, hi, user_id=u.id))["items"]

    ws = load_workbook(BytesIO(ads.render_entity_users_excel("E", [row], "p"))).active
    header = [c.value for c in ws[4]]
    got = dict(zip(header, [c.value for c in ws[5]], strict=True))
    assert {k: got[k] for k in want} == want
    assert not OLD & set(header)

    ws = load_workbook(BytesIO(ads.render_single_user_excel(row, "p"))).active
    got = {r[0].value: r[1].value for r in ws.iter_rows(min_row=4) if r[0].value}
    assert {k: _num(got[k]) for k in want} == want
    assert not OLD & set(got)

    # The PDF: the tables ReportLab is handed, header to cell, widths included.
    tables: list = []
    real = ads.Table
    monkeypatch.setattr(ads, "Table", lambda data, *a, **k: tables.append((data, k)) or real(data, *a, **k))
    ads.render_entity_users_pdf("E", [row], "p")
    [(data, kw)] = tables
    assert len(data[0]) == len(data[1]) == len(kw["colWidths"])
    got = dict(zip(data[0], data[1], strict=True))
    assert {k: _num(got[k]) for k in want} == want
    assert not OLD & set(data[0])

    # The broker summary names the client's gross figure Gross.
    totals = dict.fromkeys(
        (
            "total_of_both",
            "settlement",
            "actual_pnl",
            "sharing_pnl",
            "sharing_bkg",
            "total_deposits",
            "total_withdrawals",
        ),
        "0",
    ) | {"net_client_pnl": "50", "net_client_bkg": "10"}
    ws = load_workbook(BytesIO(ads.render_broker_totals_excel(totals, "E", "p"))).active
    got = {r[0].value: r[1].value for r in ws.iter_rows(min_row=4) if r[0].value}
    assert (got["CLIENT GROSS PNL"], got["CLIENT BROKERAGE"]) == (50.0, 10.0)
    tables.clear()
    ads.render_broker_totals_pdf(totals, "E", "p")
    labels = {r[0]: _num(r[1]) for r in tables[0][0]}
    assert (labels["CLIENT GROSS PNL"], labels["CLIENT BROKERAGE"]) == (50.0, 10.0)
    assert not OLD & set(got) and not OLD & set(labels)
