"""§4.1 The Billing PDF, inspected through the tables ReportLab is handed.

The operator's format, in order: Brought Forward → Funds Transactions (opening
on the ledger's balance_before) → one `<EXCHANGE> Trades List` per exchange
(NFO folds into NSE, BFO into BSE; one closed trade a line, Buy/Sell Rate and
Time, Gross PL and Net PL, a Totals line) → Script-wise P&L (turnover-weighted
averages) → Carry Forward → All Segments → Financial Standings (Balance, Gross,
Brokerage, Net).

Every row has as many cells as its header and the columns fill the frame — a
row with one cell more renders without complaint and shifts every number after
it one column left. The tables add up to Financial Standings and to the ledger.
Each heading claims room for its table (no heading stranded at a page foot);
cell text is escaped ("M&M" printed "M&M;"); rates print at the market's
precision and quantities keep their fractions, on the Trade Book and the P&L
PDF too.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient
from reportlab.platypus import CondPageBreak, Paragraph, SimpleDocTemplate, Table

from app.api.v1.user import reports as user_reports
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import Exchange
from app.models.order import InstrumentRef
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, report_pdf_service, tradebook_builder, wallet_service
from app.utils.time_utils import now_utc, to_ist
from tests._real_fill import INSTRUMENT, later, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_billing_pdf_net_pl"
MM = INSTRUMENT.model_copy(update={"token": "TMM1", "symbol": "M&M", "trading_symbol": "M&M-EQ"})
NIFTY = InstrumentRef(
    token="TNF1", symbol="NIFTY26NOVFUT", exchange=Exchange.NFO, segment="NSE_INDEX_FUTURE", lot_size=75
)
GOLD = InstrumentRef(token="TGLD1", symbol="GOLD26DECFUT", exchange=Exchange.MCX, segment="MCX_FUTURE")
EURUSD = InstrumentRef(token="TEUR1", symbol="EURUSD", exchange=Exchange.CRYPTO, segment="FOREX")
BTCUSD = InstrumentRef(token="TBTC1", symbol="BTCUSD", exchange=Exchange.CRYPTO, segment="CRYPTO_SPOT")
TRADES_HEAD = [
    "Ticket Id", "Script", "Qty", "Buy Rate", "Sell Rate", "Buy Time", "Sell Time",
    "Brokerage", "Gross PL", "Net PL", "Comment",
]  # fmt: skip


def _d(s: str) -> Decimal:
    return Decimal(s.replace(",", "").replace("₹", "").replace("Rs.", "").strip() or "0")


def _text(cell) -> str:
    """A cell as it prints (Table keeps a flowable cell as a 1-tuple)."""
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
    u = User(
        user_code="BPN001",
        email="bpn001@example.com",
        mobile="9347654321",
        full_name="A&B <Traders>",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
    )
    await u.insert()
    await Wallet(user_id=u.id, available_balance=Decimal128("100000000")).insert()

    docs: list[tuple[SimpleDocTemplate, list]] = []
    orig = SimpleDocTemplate.build

    def build(self, flowables, *a, **k):
        docs.append((self, list(flowables)))
        return orig(self, flowables, *a, **k)

    monkeypatch.setattr(SimpleDocTemplate, "build", build)
    got: dict[str, dict] = {}
    orig_full = report_pdf_service.build_full_tradebook_pdf

    def spy(user, data):
        got["payload"] = data
        return orig_full(user, data)

    monkeypatch.setattr(report_pdf_service, "build_full_tradebook_pdf", spy)
    try:
        yield SimpleNamespace(db=client[_DB], c=u, docs=docs, got=got)
    finally:
        await client.drop_database(_DB)
        client.close()


def _check_tables(doc: SimpleDocTemplate, flowables: list) -> None:
    tables = [f for f in flowables if isinstance(f, Table)]
    assert tables
    for t in tables:
        rows = t._cellvalues
        assert all(len(r) == len(rows[0]) for r in rows), [len(r) for r in rows]
        assert sum(t._argW) == pytest.approx(doc.width)


def _cells(t: Table) -> list[Paragraph]:
    return [p for r in t._cellvalues for c in r for p in (c if isinstance(c, (list, tuple)) else [c])
            if isinstance(p, Paragraph)]  # fmt: skip


def _sections(flowables: list) -> dict[str, list[list[str]]]:
    """Heading → the text of the table under it ([] when it has none)."""
    out: dict[str, list[list[str]]] = {}
    title = None
    for i, f in enumerate(flowables):
        if isinstance(f, Paragraph) and f.style.name == "h2":
            title = f.getPlainText()
            assert isinstance(flowables[i - 1], CondPageBreak), f"{title} can strand at a page foot"
            out[title] = []
        elif isinstance(f, Table) and title is not None and not out[title]:
            out[title] = [[_text(c) for c in r] for r in f._cellvalues]
    return out


async def _book(w) -> str:
    """Two days before the window: M&M bought (brought forward), a GOLD round
    trip (gone before the window). Inside it: a deposit, M&M closed and a
    second round trip at another size, a NIFTY short (NFO), a fractional
    EURUSD book closed at its 6-dp average, GOLD bought and held (carried
    forward)."""
    await market(w.c, "BUY", 10, "100", instrument=MM)
    await market(w.c, "BUY", 1, "70000", instrument=GOLD)
    await market(w.c, "SELL", 1, "70100", instrument=GOLD)
    await later(w.db, minutes=2 * 24 * 60)
    await wallet_service.adjust(
        w.c.id, Decimal("50000"), transaction_type=TransactionType.DEPOSIT, narration="UPI & NEFT <ref 7>"
    )
    await market(w.c, "SELL", 10, "110", instrument=MM)  # +100 off the brought-forward lot
    await market(w.c, "BUY", 30, "104", instrument=MM)
    await market(w.c, "SELL", 30, "112", instrument=MM)  # +240
    await market(w.c, "SELL", 75, "24500", instrument=NIFTY)
    await later(w.db, seconds=90)
    await market(w.c, "BUY", 75, "24400", instrument=NIFTY)  # short: +7,500
    await market(w.c, "BUY", 0.5, "1.08", instrument=EURUSD)
    await market(w.c, "BUY", 1, "1.09", instrument=EURUSD)
    await market(w.c, "SELL", 1.5, "1.10", instrument=EURUSD)  # booked off the 1.086667 average
    await market(w.c, "BUY", 1, "71000", instrument=GOLD)
    return to_ist(now_utc()).date().isoformat()


@pytest_asyncio.fixture
async def bill(w):
    """The client's own Billing PDF for today, as ReportLab was handed it."""
    day = await _book(w)
    resp = await user_reports.tradebook_full_pdf(user=w.c, from_date=day, to_date=day)
    [(doc, flowables)] = w.docs
    lo, hi = tradebook_builder.window(day, day)
    return SimpleNamespace(
        disposition=resp.headers["content-disposition"],
        doc=doc,
        flowables=flowables,
        s=_sections(flowables),
        fin={k: Decimal(str(v)) for k, v in w.got["payload"]["financial"].items()},
        lo=lo,
        hi=hi,
    )


def _tables(flowables: list, ncols: int) -> list[list[list[str]]]:
    return [
        [[_text(c) for c in r] for r in f._cellvalues]
        for f in flowables
        if isinstance(f, Table) and len(f._cellvalues[0]) == ncols
    ]


async def test_the_sections_come_in_the_operators_order_and_every_table_fits(bill):
    _check_tables(bill.doc, bill.flowables)
    assert list(bill.s) == [
        "Brought Forward",
        "Funds Transactions",
        "NSE Trades List",  # NIFTY (NFO) folds in: no NFO table
        "CRYPTO Trades List",
        "Script-wise P&L",
        "Carry Forward",
        "All Segments",
        "Financial Standings",
    ]
    # The Billing statement says so: it was headed "Trade Book", the per-fill
    # document's name, and saved as tradebook_*.pdf.
    assert bill.doc.title == "Billing Statement"
    heads = [f.getPlainText() for f in bill.flowables if isinstance(f, Table) for f in _cells(f)]
    assert "Billing Statement" in heads and "Trade Book" not in heads
    assert bill.disposition.startswith('attachment; filename="billing_')
    # The account line is escaped too (an unclosed "<Traders>" 500'd the PDF).
    assert any(isinstance(f, Paragraph) and "A&B <Traders>" in f.getPlainText() for f in bill.flowables)


async def test_bf_is_the_position_docs_and_funds_open_on_the_ledger(w, bill):
    bf_head, *bf = bill.s["Brought Forward"]
    assert not {"LTP", "Current Price", "Total PnL", "PnL"} & set(bf_head)  # no mark-to-market
    assert [(r[bf_head.index("Script")], r[bf_head.index("Qty")]) for r in bf] == [("M&M", "10")]

    first = (
        await WalletTransaction.find({"user_id": w.c.id, "created_at": {"$gte": bill.lo}})
        .sort("+created_at", "+_id")
        .first_or_none()
    )
    funds = bill.s["Funds Transactions"]
    assert funds[1][1] == "Opening Balance"
    assert _d(funds[1][2]) == first.balance_before.to_decimal()  # read off, not recomputed
    assert [(r[1], r[2], r[3]) for r in funds[2:-1]] == [("Deposit", "50,000.00", "UPI & NEFT <ref 7>")]


async def test_one_trade_a_line_with_gross_and_net_per_exchange(bill):
    col = {h: i for i, h in enumerate(TRADES_HEAD)}
    nse = bill.s["NSE Trades List"]
    assert nse[0] == TRADES_HEAD
    assert [r[col["Script"]] for r in nse[1:-1]] == ["M&M", "M&M", "NIFTY26NOVFUT"]
    [nifty] = [r for r in nse if r[col["Script"]] == "NIFTY26NOVFUT"]
    assert (nifty[col["Sell Rate"]], nifty[col["Buy Rate"]]) == ("24,500.00", "24,400.00")
    assert nifty[col["Sell Time"]] < nifty[col["Buy Time"]]  # a short sold first

    gross_all = Decimal(0)
    for name in ("NSE Trades List", "CRYPTO Trades List"):
        rows, totals = bill.s[name][1:-1], bill.s[name][-1]
        g = sum((_d(r[col["Gross PL"]]) for r in rows), Decimal(0))
        b = sum((_d(r[col["Brokerage"]]) for r in rows), Decimal(0))
        for r in rows:
            assert _d(r[col["Net PL"]]) == _d(r[col["Gross PL"]]) - _d(r[col["Brokerage"]])
        assert [_d(totals[col[h]]) for h in ("Brokerage", "Gross PL", "Net PL")] == [b, g, g - b]
        gross_all += g
    assert gross_all == bill.fin["gross_pnl"]

    # Rates at the market's precision (2 dp printed 1.09), fractional units
    # kept; a USD rate says so beside Gross / Net in rupees.
    crypto = bill.s["CRYPTO Trades List"][1:-1]
    eur = [(r[col["Qty"]], r[col["Buy Rate"]], r[col["Sell Rate"]]) for r in crypto]
    assert eur == [("0.5", "$1.086667", "$1.10"), ("1", "$1.086667", "$1.10")]
    sw_head, *sw = bill.s["Script-wise P&L"]
    [eur_sw] = [r for r in sw if r[sw_head.index("Script")] == "EURUSD"]
    assert [eur_sw[sw_head.index(h)] for h in ("Avg Buy", "Sell Turnover")] == ["$1.086667", "$1.65"]


async def test_script_wise_averages_are_turnover_weighted(bill):
    head, *rows = bill.s["Script-wise P&L"]
    [mm] = [r for r in rows if r[head.index("Script")] == "M&M"]
    got = {h: mm[head.index(h)] for h in ("Qty", "Avg Buy", "Avg Sell", "Buy Turnover", "Sell Turnover")}
    # A plain mean of the rates is 102 / 111.
    assert got == {
        "Qty": "40",
        "Avg Buy": "103.00",
        "Avg Sell": "111.50",
        "Buy Turnover": "4,120.00",
        "Sell Turnover": "4,460.00",
    }
    assert _d(mm[head.index("Gross PL")]) == Decimal("340")


async def test_all_segments_is_financial_standings_is_the_ledger(w, bill):
    cf_head, *cf = bill.s["Carry Forward"]
    assert not {"Current Price", "Total PnL", "SL", "TP"} & set(cf_head)
    assert [r[cf_head.index("Script")] for r in cf[:-1]] == ["GOLD26DECFUT"]

    # Trades lists plus the carried positions' opening charges: the one
    # overall figure, Financial Standings' — what the ledger booked.
    seg_head, *seg = bill.s["All Segments"]
    rows, total = seg[:-1], dict(zip(seg_head, seg[-1], strict=True))
    assert total["Segment"] == "All Segments"
    for h in ("Brokerage", "Gross PL", "Net PL"):
        assert _d(total[h]) == sum((_d(r[seg_head.index(h)]) for r in rows), Decimal(0))
    cf_com = _d(cf[-1][cf_head.index("Commission")])
    assert [r[0] for r in rows] == ["NSE", "CRYPTO", "Carry Forward"]
    assert _d(rows[-1][seg_head.index("Brokerage")]) == cf_com > 0
    fin = bill.fin
    assert [_d(total[h]) for h in ("Gross PL", "Brokerage", "Net PL")] == [
        fin["gross_pnl"], fin["brokerage"], fin["net_pnl"],
    ]  # fmt: skip
    assert fin["net_pnl"] == fin["gross_pnl"] - fin["brokerage"]
    window = {"user_id": w.c.id, "created_at": {"$gte": bill.lo, "$lte": bill.hi}}
    for kind, sign, figure in (
        (TransactionType.PNL, 1, "gross_pnl"),
        (TransactionType.CHARGES, -1, "brokerage"),
    ):
        booked = await WalletTransaction.find({**window, "transaction_type": kind.value}).to_list()
        assert sign * sum((r.amount.to_decimal() for r in booked), Decimal(0)) == fin[figure]

    fs = " ".join(_text(c) for r in bill.s["Financial Standings"] for c in r)
    assert all(label in fs for label in ("Balance", "Gross PnL", "Brokerage", "Net PnL"))
    assert not any(live in fs for live in ("Equity", "Margin", "Credit"))


async def test_the_trade_book_and_pnl_pdfs_keep_fractions_and_precision(w):
    await market(w.c, "SELL", 0.25, "61000", instrument=BTCUSD)
    await later(w.db, seconds=5)
    await market(w.c, "BUY", 0.05, "1.08345", instrument=EURUSD)
    await later(w.db, seconds=5)
    await market(w.c, "BUY", 1500, "200", instrument=MM)
    await user_reports.tradebook_pdf(user=w.c, from_date=None, to_date=None, limit=None)
    report_pdf_service.build_pnl_pdf(
        w.c, {"by_symbol": [{"symbol": "M&M", "buy_qty": 0.05, "sell_qty": 1500, "pnl": 0}]}
    )
    report_pdf_service.build_tax_pdf(w.c, {"buckets": {}})
    (tb_doc, tb), (pnl_doc, pnl), (_, tax) = w.docs
    _check_tables(tb_doc, tb)
    _check_tables(pnl_doc, pnl)

    [[head, *rows]] = _tables(tb, 8)
    got = [tuple(r[head.index(h)] for h in ("Symbol", "Qty", "Price", "Value")) for r in rows]
    assert got == [
        ("M&M", "1,500", "200.00", "₹300,000.00"),
        ("EURUSD", "0.05", "$1.08", "$0.05"),  # Qty printed 0
        ("BTCUSD", "0.25", "$61,000.00", "$15,250.00"),  # a dollar notional printed ₹15,250.00
    ]
    # The engine fills at 2 dp today; a finer price prints in full.
    assert report_pdf_service._fmt_rate("1.08345", "CRYPTO") == "1.08345"
    assert report_pdf_service._fmt_rate(1.0834512, "CDS") == "1.083451"
    assert report_pdf_service._fmt_rate(24500, "NFO") == "24,500.00"

    [by_symbol] = _tables(pnl, 7)
    assert by_symbol[0][-1] == "P&L"  # printed "P&L;"
    assert by_symbol[1][:3] == ["M&M", "0.05", "1,500"]
    assert any(isinstance(f, Paragraph) and f.getPlainText() == "Tax P&L (simplified)" for f in tax)


async def test_the_printed_rows_add_up_to_the_printed_totals(w):
    """A close FIFO-split over three lots printed Gross 40.38 three times
    (121.14) under a Totals line, All Segments and Financial Standings of
    121.15 — what the wallet booked. Each row is cut to the paisa with the
    rounding carried forward, and every total is the sum of the rows."""
    for px in ("1000", "1010", "1020.50"):
        await market(w.c, "BUY", 1, px, instrument=MM)
    await market(w.c, "SELL", 3, "1050.55", instrument=MM)  # books 121.15 off the 1010.166667 average
    day = to_ist(now_utc()).date().isoformat()
    await user_reports.tradebook_full_pdf(user=w.c, from_date=day, to_date=day)
    [(doc, flowables)] = w.docs
    _check_tables(doc, flowables)
    s = _sections(flowables)
    col = {h: i for i, h in enumerate(TRADES_HEAD)}
    rows, totals = s["NSE Trades List"][1:-1], s["NSE Trades List"][-1]
    assert len(rows) == 3
    for h in ("Brokerage", "Gross PL", "Net PL"):
        assert sum((_d(r[col[h]]) for r in rows), Decimal(0)) == _d(totals[col[h]]), h
    seg_head, *seg = s["All Segments"]
    fin = w.got["payload"]["financial"]
    for h, k in (("Gross PL", "gross_pnl"), ("Brokerage", "brokerage"), ("Net PL", "net_pnl")):
        assert _d(totals[col[h]]) == _d(seg[-1][seg_head.index(h)]) == Decimal(str(fin[k])), h
    pnl = {"user_id": w.c.id, "transaction_type": TransactionType.PNL.value}
    booked = sum((b.amount.to_decimal() for b in await WalletTransaction.find(pnl).to_list()), Decimal(0))
    assert _d(totals[col["Gross PL"]]) == booked == Decimal("121.15")


def test_script_wise_skips_blank_times():
    row = {"exchange": "NSE", "script": "X", "qty": 10, "buy_rate": 100, "sell_rate": 110,
           "brokerage": 0, "gross_pnl": 100, "net_pnl": 100}  # fmt: skip
    [s] = tradebook_builder.script_wise(
        [
            {**row, "buy_time": "", "sell_time": "2026-11-02 11:00:00"},
            {**row, "buy_time": "2026-11-02 10:00:00", "sell_time": "2026-11-02 12:00:00"},
        ]
    )
    assert (s["first_time"], s["last_time"]) == ("2026-11-02 10:00:00", "2026-11-02 12:00:00")
