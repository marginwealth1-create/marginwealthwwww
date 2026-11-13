"""The Billing statement and the Trade Book as Excel workbooks.

The Accounts dashboard's per-user Excel button offers the account summary,
the Billing statement and the Trade Book. A workbook must not print a number
its PDF doesn't: both renderers read one table builder per document, so every
numeric cell of the workbook is the PDF's cell for the same row and column,
held as a real number (not text) with its number format, USD rates marked '$'.

A realistic week goes through the matching engine: an NSE position brought
forward and weekly-settled, then partly closed (the rest carried), an MCX
round trip and an MCX position carried, a fractional EURUSD book and a BTCUSD
short (USD-quoted), a deposit.
"""

from __future__ import annotations

import io
import re
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient
from openpyxl import load_workbook
from reportlab.platypus import Paragraph, SimpleDocTemplate, Table

from app.api.v1.admin import accounts
from app.api.v1.admin import reports as admin_reports
from app.api.v1.admin import users as admin_users
from app.api.v1.user import reports as user_reports
from app.core.config import settings
from app.core.database import _document_models
from app.core.exceptions import InsufficientPermissionsError
from app.models._base import Exchange
from app.models.order import InstrumentRef
from app.models.trade import Trade
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.schemas.admin.common import WalletAdjustRequest
from app.services import (
    accounts_dashboard_service,
    brokerage_calculator,
    market_data_service,
    tradebook_builder,
    wallet_service,
)
from app.services.report_pdf_service import _amt, _qty, _rate, billing_sections
from app.utils.decimal_utils import to_decimal
from app.utils.time_utils import now_utc, to_ist
from tests._real_fill import later, market, set_commission_per_lot, settle_weekly

_DB = f"{settings.MONGODB_DB_NAME}_test_report_xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GOLD = InstrumentRef(token="TXG1", symbol="GOLD26DECFUT", exchange=Exchange.MCX, segment="MCX_FUTURE")
EURUSD = InstrumentRef(token="TXE1", symbol="EURUSD", exchange=Exchange.CRYPTO, segment="FOREX")
BTCUSD = InstrumentRef(token="TXB1", symbol="BTCUSD", exchange=Exchange.CRYPTO, segment="CRYPTO_SPOT")
# Columns that hold an amount, a rate or units: a number in the workbook.
NUMERIC = {
    "Qty", "Price", "Amount", "Buy Rate", "Sell Rate", "Brokerage", "Gross PL", "Net PL", "Avg Buy",
    "Avg Sell", "Buy Turnover", "Sell Turnover", "Commission", "Value", "Charges",
}  # fmt: skip


def _d(text: str) -> Decimal:
    return Decimal(re.sub(r"[,$₹]|Rs\.", "", text).strip())


def _text(cell) -> str:
    if isinstance(cell, (list, tuple)):
        return "".join(_text(c) for c in cell)
    return cell.getPlainText() if isinstance(cell, Paragraph) else str(cell)


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setitem(market_data_service._usd_inr_cache, "rate", 97.0)
    await set_commission_per_lot(1)

    async def mk(code: str, role: UserRole, **kw) -> User:
        u = User(
            user_code=code,
            email=f"{code.lower()}@example.com",
            mobile=f"95{abs(hash(code)) % 10**8:08d}",
            full_name=f"{code} Name",
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            **kw,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128("100000000")).insert()
        return u

    docs: list[tuple[SimpleDocTemplate, list]] = []
    orig = SimpleDocTemplate.build

    def build(self, flowables, *a, **k):
        docs.append((self, list(flowables)))
        return orig(self, flowables, *a, **k)

    monkeypatch.setattr(SimpleDocTemplate, "build", build)
    try:
        yield SimpleNamespace(
            db=client[_DB],
            mp=monkeypatch,
            docs=docs,
            c=await mk("XLS001", UserRole.CLIENT),
            sa=await mk("XLSSA", UserRole.SUPER_ADMIN),
            other_admin=await mk("XLSAD", UserRole.ADMIN, brand_name="Elsewhere"),
        )
    finally:
        await client.drop_database(_DB)
        client.close()


async def _week(w) -> dict[str, str]:
    """The week's trading; returns the dashboard's from/to dates (IST)."""
    await market(w.c, "BUY", 100, "1000")  # NSE, brought forward
    await later(w.db, minutes=8 * 24 * 60)
    await wallet_service.adjust(
        w.c.id, Decimal("50000"), transaction_type=TransactionType.DEPOSIT, narration="UPI 77"
    )
    await settle_weekly(w.c, w.mp, "1100")  # closed and re-opened at 1,100
    await later(w.db, minutes=2 * 24 * 60)
    await market(w.c, "SELL", 40, "1150")  # a partial close: 60 carried
    await market(w.c, "BUY", 1, "70000", instrument=GOLD)
    await market(w.c, "SELL", 1, "70100", instrument=GOLD)
    await market(w.c, "BUY", 2, "71000", instrument=GOLD)  # carried
    await market(w.c, "BUY", 0.5, "1.08", instrument=EURUSD)
    await market(w.c, "BUY", 1, "1.09", instrument=EURUSD)
    await market(w.c, "SELL", 1.5, "1.10", instrument=EURUSD)  # off the 1.086667 average
    await market(w.c, "SELL", 0.25, "61000", instrument=BTCUSD)
    await later(w.db, seconds=30)
    await market(w.c, "BUY", 0.25, "60000", instrument=BTCUSD)
    today = to_ist(now_utc()).date()
    return {"from_date": (today - timedelta(days=6)).isoformat(), "to_date": today.isoformat()}


async def _body(resp) -> bytes:
    return b"".join([chunk async for chunk in resp.body_iterator])


async def _sheet(resp, prefix: str):
    assert resp.media_type == XLSX
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="{prefix}_XLS001_Name_XLS001_')
    assert disposition.endswith('.xlsx"')
    wb = load_workbook(io.BytesIO(await _body(resp)))
    assert len(wb.worksheets) == 1
    return wb.active


def _groups(ws) -> list[list[tuple]]:
    """The sheet as its blank-row-separated blocks of cells."""
    out: list[list[tuple]] = [[]]
    for row in ws.iter_rows():
        if all(c.value in (None, "") for c in row):
            out.append([])
        else:
            out[-1].append(row)
    return [g for g in out if g]


def _xlsx_sections(ws) -> dict[str, list[tuple]]:
    """Title → the rows under it (header, data, totals)."""
    meta, *blocks = _groups(ws)
    out = {}
    for title, *rows in blocks:
        assert title[0].font.b, title[0].value
        out[title[0].value] = rows
    return out


def _pdf_sections(flowables: list) -> dict[str, list[list[str]]]:
    out: dict[str, list[list[str]]] = {}
    title = None
    for f in flowables:
        if isinstance(f, Paragraph) and f.style.name == "h2":
            title = f.getPlainText()
            out[title] = []
        elif isinstance(f, Table) and title is not None and not out[title]:
            out[title] = [[_text(c) for c in r] for r in f._cellvalues]
    return out


def _same_table(pdf: list[list[str]], xl: list[tuple]) -> int:
    """Row by row, cell by cell: the workbook prints the PDF's table. Returns
    how many numeric cells were compared."""
    assert len(xl) == len(pdf), (len(xl), len(pdf))
    head = pdf[0]
    assert [c.value for c in xl[0][: len(head)]] == head
    assert all(c.font.b for c in xl[0][: len(head)])
    numbers = 0
    for prow, xrow in zip(pdf[1:], xl[1:], strict=True):
        assert all(c.value in (None, "") for c in xrow[len(head) :])
        for h, ptext, cell in zip(head, prow, xrow, strict=False):
            if h in NUMERIC and ptext not in ("", "—"):
                assert isinstance(cell.value, (int, float)), (h, ptext, cell.value)
                assert Decimal(str(cell.value)) == _d(ptext), (h, ptext, cell.value)
                assert ptext.startswith("$") == ('"$"' in cell.number_format), (h, ptext, cell.number_format)
                assert cell.number_format != "General", h
                numbers += 1
            else:
                assert (cell.value or "") == ptext, (h, ptext, cell.value)
    return numbers


async def test_every_billing_cell_is_the_pdfs(w):
    dates = await _week(w)
    uid = str(w.c.id)
    await admin_reports.tradebook_pdf(user_id=uid, admin=w.sa, _=None, **dates)
    ws = await _sheet(await admin_reports.tradebook_xlsx(user_id=uid, admin=w.sa, _=None, **dates), "billing")
    [(_, flowables)] = w.docs
    pdf, xl = _pdf_sections(flowables), _xlsx_sections(ws)

    assert (
        list(xl)
        == list(pdf)
        == [
            "Brought Forward",
            "Funds Transactions",
            "NSE Trades List",
            "MCX Trades List",
            "CRYPTO Trades List",
            "Script-wise P&L",
            "Carry Forward",
            "All Segments",
            "Financial Standings",
        ]
    )
    numbers = sum(_same_table(pdf[t], xl[t]) for t in pdf if t != "Financial Standings")
    assert numbers > 60

    # The scenario is in it: the settlement's close, the partial close, the
    # carried NSE remainder and GOLD, USD rates marked.
    head, *nse = [[c.value for c in r] for r in xl["NSE Trades List"]]
    assert "Weekly settlement" in [r[head.index("Comment")] for r in nse]
    assert [r[head.index("Qty")] for r in nse[:-1]] == [100, 40]
    cf = {r[2].value: r[4].value for r in xl["Carry Forward"][1:-1]}
    assert cf == {"TBKG": 60, "GOLD26DECFUT": 2}
    crypto = xl["CRYPTO Trades List"]
    head = [c.value for c in crypto[0]]
    eur = [r for r in crypto[1:-1] if r[1].value == "EURUSD"]
    assert {r[head.index("Buy Rate")].value for r in eur} == {1.086667}
    assert all(r[head.index("Buy Rate")].number_format.startswith('"$"') for r in crypto[1:-1])

    # Each exchange's data rows add up to its Totals row.
    for title in ("NSE Trades List", "MCX Trades List", "CRYPTO Trades List"):
        head = [c.value for c in xl[title][0]]
        rows, totals = xl[title][1:-1], xl[title][-1]
        assert totals[1].value == "Totals" and all(c.font.b for c in totals if c.value is not None)
        for h in ("Brokerage", "Gross PL", "Net PL"):
            i = head.index(h)
            column = sum((Decimal(str(r[i].value)) for r in rows), Decimal(0))
            assert column == Decimal(str(totals[i].value)), h

    # Financial Standings is the statement's.
    lo, hi = tradebook_builder.window(dates["from_date"], dates["to_date"])
    fin = (await tradebook_builder.statement_payload(w.c, lo, hi))["financial"]
    fs_head, fs_row = xl["Financial Standings"]
    got = {h.value: Decimal(str(c.value)) for h, c in zip(fs_head, fs_row, strict=False) if h.value}
    assert got == {
        "Balance": Decimal(str(fin["balance"])),
        "Gross PnL": Decimal(str(fin["gross_pnl"])),
        "Brokerage": Decimal(str(fin["brokerage"])),
        "Net PnL": Decimal(str(fin["net_pnl"])),
    }
    assert ws.column_dimensions["B"].width >= 10


async def test_the_trade_book_workbook_is_one_row_per_fill(w):
    dates = await _week(w)
    uid = str(w.c.id)
    await admin_reports.trade_book_pdf(user_id=uid, admin=w.sa, _=None, **dates)
    resp = await admin_reports.trade_book_xlsx(user_id=uid, admin=w.sa, _=None, **dates)
    ws = await _sheet(resp, "tradebook")
    [(_, flowables)] = w.docs
    [pdf] = [[[_text(c) for c in r] for r in f._cellvalues] for f in flowables if isinstance(f, Table)][1:]
    [_meta, [title, *xl]] = _groups(ws)
    assert title[0].value == "Trade Book" and title[0].font.b
    assert _same_table(pdf, xl) > 40

    lo, hi = tradebook_builder.window(dates["from_date"], dates["to_date"])
    fills = await tradebook_builder.trade_rows(w.c.id, lo, hi)
    head = [c.value for c in xl[0]]
    col = {h: head.index(h) for h in ("Symbol", "Side", "Qty")}
    got = [(r[col["Symbol"]].value, r[col["Side"]].value, r[col["Qty"]].value) for r in xl[1:]]
    assert got == [(f["symbol"], f["action"], float(f["quantity"])) for f in fills]
    # The weekly settlement's two legs, which write no fill.
    assert sum(1 for f in fills if f["id"].startswith("settle_")) == 2
    # The header stays on screen.
    assert ws.freeze_panes == xl[1][0].coordinate


async def test_both_workbooks_are_gated_like_the_pdfs(w):
    uid = str(w.c.id)
    for endpoint in (admin_reports.tradebook_xlsx, admin_reports.trade_book_xlsx):
        with pytest.raises(InsufficientPermissionsError):
            await endpoint(user_id=uid, admin=w.other_admin, _=None)


# ── P7 fixup ──────────────────────────────────────────────────────────


def _adds_up(payload: dict) -> tuple[Decimal, Decimal]:
    """(Opening + Net Funds + Net PnL, Balance): one figure when the
    statement reconciles with itself."""
    funds, fin = payload["funds"], payload["financial"]
    net_funds = sum((Decimal(str(r["amount"])) for r in funds["rows"]), Decimal(0))
    return Decimal(str(funds["opening_balance"])) + net_funds + Decimal(str(fin["net_pnl"])), Decimal(
        str(fin["balance"])
    )


async def test_a_close_stamps_what_the_wallet_booked(w):
    """pnl_inr is the close's P&L net of its own charge: stamp + charge is the
    wallet's PNL row, on a half-paisa too. The BTCUSD close books (61,000.50 −
    60,000) × 0.25 × 97 = 24,262.125 → 24,262.12; the stamp rounded the net
    instead and read one paisa over, so Billing's Gross was 24,262.13 and the
    statement no longer added up to its Balance."""
    dates = await _week(w)

    async def _odd_paise(**_kw):  # a close charged ₹7.67
        return brokerage_calculator.ChargesBreakdown(Decimal("7.67"), Decimal("7.67"))

    await market(w.c, "SELL", 0.25, "61000.50", instrument=BTCUSD)
    await later(w.db, seconds=30)
    w.mp.setattr(brokerage_calculator, "calculate", _odd_paise)
    await market(w.c, "BUY", 0.25, "60000", instrument=BTCUSD)
    for t in await Trade.find({"user_id": w.c.id, "pnl_inr": {"$ne": None}}).to_list():
        [pnl_row] = await WalletTransaction.find(
            {"reference_id": str(t.order_id), "transaction_type": TransactionType.PNL.value}
        ).to_list()
        assert to_decimal(t.pnl_inr) + to_decimal(t.total_charges) == to_decimal(pnl_row.amount), t.instrument
    lo, hi = tradebook_builder.window(dates["from_date"], dates["to_date"])
    computed, balance = _adds_up(await tradebook_builder.statement_payload(w.c, lo, hi))
    assert computed == balance


async def test_promo_and_penalty_are_funds_on_the_statement(w):
    """An admin's PROMO / PENALTY is Add / Deduct Fund everywhere (P6), the
    Billing statement's Funds Transactions too, so its Net Funds is the
    Accounts KPI's cash and Opening + Net Funds + Net PnL is the Balance."""
    dates = await _week(w)
    for amount, tt in ((100, "PROMO"), (-200, "PENALTY")):
        req = WalletAdjustRequest(amount=amount, narration=f"{tt} entry", transaction_type=tt)
        await admin_users.wallet_adjust(str(w.c.id), req, w.sa)
    lo, hi = tradebook_builder.window(dates["from_date"], dates["to_date"])
    payload = await tradebook_builder.statement_payload(w.c, lo, hi)
    rows = {r["type"]: Decimal(str(r["amount"])) for r in payload["funds"]["rows"]}
    assert rows == {"Deposit": Decimal("50000"), "Promo": Decimal("100"), "Penalty": Decimal("-200")}
    cash_in, cash_out = await accounts_dashboard_service.cash_in_out([w.c.id], {"$gte": lo, "$lte": hi})
    assert sum(rows.values(), Decimal(0)) == cash_in - cash_out
    computed, balance = _adds_up(payload)
    assert computed == balance


async def test_a_name_outside_latin_1_still_downloads(w):
    """Headers go out as latin-1: a client named in Devanagari (or with a
    quote in the name) made every download 500. The header carries an ASCII
    name and the real one in filename* (RFC 5987)."""
    name = 'राम "Ravi" कुमार'
    w.c.full_name = w.sa.full_name = name
    await w.c.save()
    await w.sa.save()
    uid = str(w.c.id)
    responses = [
        await admin_reports.tradebook_pdf(user_id=uid, admin=w.sa, _=None),
        await admin_reports.tradebook_xlsx(user_id=uid, admin=w.sa, _=None),
        await admin_reports.trade_book_pdf(user_id=uid, admin=w.sa, _=None),
        await admin_reports.trade_book_xlsx(user_id=uid, admin=w.sa, _=None),
        await user_reports.tradebook_full_pdf(user=w.c),
        await accounts.export_entity_users_excel(
            str(w.sa.id), w.sa, None, None, None, user_id=w.c.id, _=None
        ),
        await accounts.export_entity_users_pdf(str(w.sa.id), w.sa, None, None, None, user_id=w.c.id, _=None),
    ]
    for resp in responses:
        disposition = resp.headers["content-disposition"]
        fallback = re.search(r'filename="([^"]*)"', disposition)[1]
        assert fallback.isascii() and '"' not in fallback, disposition
        [encoded] = re.findall(r"filename\*=utf-8''(\S+)", disposition, re.I)
        assert name in unquote(encoded).replace("_", " "), disposition


async def test_text_cells_stay_text(w):
    """A Funds comment is an admin's free text: '=SUM(1,2)' prints as written
    in the PDF, so the workbook holds it as text, never as a formula."""
    req = WalletAdjustRequest(amount=1, narration="=SUM(1,2)", transaction_type="ADJUSTMENT")
    await admin_users.wallet_adjust(str(w.c.id), req, w.sa)
    ws = await _sheet(await admin_reports.tradebook_xlsx(user_id=str(w.c.id), admin=w.sa, _=None), "billing")
    [cell] = [c for row in ws.iter_rows() for c in row if c.value == "=SUM(1,2)"]
    assert cell.data_type == "s"


def _main(v: float, kind: str) -> str:
    """How main's PDF printed a number (f-string formatting of the float)."""
    if kind == "qty":
        return f"{round(v, 8):,.8f}".rstrip("0").rstrip(".")
    whole, frac = f"{v:,.{6 if kind == 'fine' else 2}f}".split(".")
    return f"{whole}.{frac.rstrip('0').ljust(2, '0')}" if kind == "fine" else f"{whole}.{frac}"


def test_a_statement_prints_what_was_sent():
    """A re-downloaded statement prints every rate, average and value as the
    copy already sent: main formatted the float, so 2.675 printed 2.67 and a
    script-wise average of 17,298.75 / 150 printed 115.33. The workbook holds
    the same value."""
    for k in range(1, 100_000, 2):  # every half paisa 0.005 … 499.995
        v = k / 200
        assert str(_rate(v)) == str(_amt(v)) == _main(v, "rate"), v
        assert str(_rate(Decimal(str(v)))) == _main(v, "rate"), v
    for k in range(1, 20_000, 2):  # half a millionth: CDS / CRYPTO rates
        v = 1 + k / 2_000_000
        assert str(_rate(v, "CRYPTO")) == _main(v, "fine"), v
    for k in range(1, 20_000, 2):
        v = k / 200_000_000
        assert str(_qty(v)) == _main(v, "qty"), v

    sw = tradebook_builder.script_wise(
        [
            {
                "exchange": "NSE",
                "script": "NIFTY26NOV24000CE",
                "qty": q,
                "buy_rate": b,
                "sell_rate": 120.0,
                "brokerage": 1.0,
                "gross_pnl": 0.0,
                "buy_time": "",
                "sell_time": "",
            }
            for q, b in ((75, 115.3), (75, 115.35))
        ]
    )
    cf = [{"ticket_id": "T1", "time": "", "script": "X", "side": "BUY", "qty": 3.0, "price": 100.005,
           "exchange": "BSE", "commission": 0.0, "value": 300.015}]  # fmt: skip
    sections = {s.title: s for s in billing_sections({"script_wise": sw, "carry_forward": cf})}
    assert str(sections["Script-wise P&L"].rows[0][3]) == "115.33"
    [row] = sections["Carry Forward"].rows
    assert (str(row[5]), str(row[7])) == ("100.00", "300.01")
