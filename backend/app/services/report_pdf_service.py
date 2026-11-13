"""Lightweight PDF builders for the user-facing reports.

Uses ReportLab (already in requirements.txt) to render a one-pager PDF per
report kind. All builders return a `bytes` payload that the FastAPI route
streams back via `StreamingResponse`. No filesystem writes — the PDF lives in
memory only.

The same blueprint is consumed by both the Next.js web frontend and the Expo
APK; the APK saves the bytes via `expo-file-system` + `expo-sharing`, the web
triggers a normal browser download via `application/pdf` content-disposition.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors as rl_colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.fonts import addMapping
from reportlab.platypus import (
    CondPageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.utils.decimal_utils import quantize_basis, quantize_money
from app.utils.time_utils import now_ist, to_ist

BRAND = rl_colors.HexColor("#3B5BDB")  # MarginWealth brand blue
BRAND_SOFT = rl_colors.HexColor("#EEF2FF")
GRID = rl_colors.HexColor("#E5E7EB")
TEXT = rl_colors.HexColor("#0F172A")
MUTED = rl_colors.HexColor("#64748B")
BUY = rl_colors.HexColor("#0F766E")
SELL = rl_colors.HexColor("#DC2626")


# ── Font with Indian Rupee (₹, U+20B9) glyph support ─────────────────
# ReportLab's built-in PDF fonts (Helvetica / Times / Courier) are Type 1
# fonts with the WinAnsi encoding — they predate Unicode and have NO
# glyph for ₹. Renders show up as a black square (■), which is exactly
# what the user reported in the P&L PDF.
#
# Fix: register a TrueType font that DOES carry ₹, and route every
# Paragraph / Table cell through it. We try a list of candidate paths
# at import time, pick whichever one resolves first, and fall back to
# the legacy "Rs." prefix in `_fmt_money` if absolutely nothing works
# (so the PDF still renders cleanly on a stripped-down host).
_FONT_NAME = "Helvetica"
_FONT_BOLD = "Helvetica-Bold"
_HAS_RUPEE = False


def _register_unicode_font() -> None:
    """Locate a system TrueType font that includes ₹ (U+20B9) and
    register it under the stable aliases used elsewhere in this module.
    Search order: bundled app font → Linux server paths → Windows
    system paths → macOS system paths. First hit wins."""
    global _FONT_NAME, _FONT_BOLD, _HAS_RUPEE

    here = os.path.dirname(__file__)
    bundled = os.path.normpath(os.path.join(here, "..", "..", "assets", "fonts"))

    # Each entry: (alias, regular path, bold path-or-None). The bold
    # path is optional — if it's missing we fall back to the regular
    # face for bold spans (better than reverting to Helvetica which
    # would re-introduce the ■ glyph).
    candidates: list[tuple[str, str, str | None]] = [
        # Bundled with the app (preferred — no system dependency).
        ("AppSans", os.path.join(bundled, "NotoSans-Regular.ttf"),
         os.path.join(bundled, "NotoSans-Bold.ttf")),
        ("AppSans", os.path.join(bundled, "DejaVuSans.ttf"),
         os.path.join(bundled, "DejaVuSans-Bold.ttf")),

        # Linux (Ubuntu / Debian / Amazon Linux) — DejaVu is in
        # fonts-dejavu-core which is installed by default on most
        # server images and ships with ₹.
        ("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("DejaVu", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
        # Noto Sans — often present alongside DejaVu.
        ("NotoSans", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
         "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),

        # Windows — Arial and Segoe UI both carry ₹ (Win 8+).
        ("Arial", "C:\\Windows\\Fonts\\arial.ttf",
         "C:\\Windows\\Fonts\\arialbd.ttf"),
        ("Segoe", "C:\\Windows\\Fonts\\segoeui.ttf",
         "C:\\Windows\\Fonts\\segoeuib.ttf"),

        # macOS.
        ("HelveticaNeue", "/System/Library/Fonts/HelveticaNeue.ttc", None),
        ("Arial", "/Library/Fonts/Arial.ttf",
         "/Library/Fonts/Arial Bold.ttf"),
    ]

    for alias, reg, bold in candidates:
        if not os.path.exists(reg):
            continue
        try:
            pdfmetrics.registerFont(TTFont(alias, reg))
            bold_alias = f"{alias}-Bold"
            if bold and os.path.exists(bold):
                pdfmetrics.registerFont(TTFont(bold_alias, bold))
            else:
                pdfmetrics.registerFont(TTFont(bold_alias, reg))
            # Map `<b>` inside Paragraph HTML to the bold face. Without
            # this, `<b>` falls back to the synthetic-bold renderer which
            # ignores our TTF entirely and would re-introduce ■ for ₹.
            addMapping(alias, 0, 0, alias)         # normal
            addMapping(alias, 1, 0, bold_alias)    # bold
            addMapping(alias, 0, 1, alias)         # italic — synth
            addMapping(alias, 1, 1, bold_alias)    # bold italic — synth
            _FONT_NAME = alias
            _FONT_BOLD = bold_alias
            _HAS_RUPEE = True
            return
        except Exception:
            # Bad / unreadable font file — keep trying the next one.
            continue


_register_unicode_font()


def _rupee() -> str:
    """₹ when the host font carries U+20B9, otherwise the plain-ASCII
    fallback. Keeps PDFs readable on a stripped-down server image."""
    return "₹" if _HAS_RUPEE else "Rs. "


def _text(v: Any) -> str:
    """Data as Paragraph text. ReportLab parses it as mini-XML: an "&" in a
    scrip ("M&M") printed "M&M;", a "<" in a client's name 500'd the PDF (or
    made it fetch an <img> URL). A ₹ in it goes through _rupee() too."""
    return escape(str(v)).replace("₹", _rupee())


def _num(v: Any) -> float:
    """Parse a payload number that may ALREADY be a display-formatted string.

    The tradebook payload builders emit some fields pre-formatted
    (``f"{qty:,.2f}"`` → ``"9,000.00"``), so a bare ``float()`` blew up with
    ``ValueError: could not convert string to float: '9,000.00'`` the moment a
    value crossed 1,000 and picked up a thousands separator — which 500'd the
    whole tradebook PDF (browser showed a bare "Network Error"). Values under
    1,000 parsed fine, which is why it looked intermittent. Strips commas /
    currency symbols / whitespace and never raises — unparseable → 0.0.
    """
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(
            str(v).replace(",", "").replace("₹", "").replace("Rs.", "").strip() or 0
        )
    except (TypeError, ValueError):
        return 0.0


# Forex and crypto quote to 5–6 places; Indian markets to the paisa.
_FINE_RATE = frozenset({"CDS", "CRYPTO"})
_UNIT_STEP = Decimal("0.00000001")


@dataclass(frozen=True)
class Num:
    """A number in a printed table, held at exactly the precision the PDF
    prints it, so the PDF's text and the workbook's cell are one value.
    kind: "amt" 2 dp | "inr" 2 dp with ₹ | "rate" 2 dp | "fine" 2–6 dp
    (CDS / CRYPTO) | "qty" units, fractions kept. `usd`: a USD-quoted rate
    or notional says so — the P&L and charges beside it are rupees, so
    61,000 → 60,000 on 0.25 BTC beside a Gross of 24,250 read as broken."""

    value: Decimal
    kind: str = "amt"
    usd: bool = False

    def __str__(self) -> str:
        if self.kind == "qty":
            return f"{self.value:,.8f}".rstrip("0").rstrip(".")
        text = f"{self.value:,.{6 if self.kind == 'fine' else 2}f}"
        if self.kind == "fine":  # trailing zeros trimmed back to 2 dp
            whole, frac = text.split(".")
            text = f"{whole}.{frac.rstrip('0').ljust(2, '0')}"
        if self.usd:
            return f"${text}"
        return f"{_rupee()}{text}" if self.kind == "inr" else text


def _dec(v: Any) -> Decimal:
    """The float the PDF always formatted, exactly (Decimal(float), not its
    repr): rounded at the printed precision it reads as the f-string read it,
    so a re-downloaded statement prints what was sent. A half-paisa average
    rounds as its float lies: 115.325 printed 115.33, 2.675 printed 2.67."""
    return Decimal(_num(v))


def _amt(v: Any, kind: str = "amt", usd: Any = False) -> Num:
    return Num(quantize_money(_dec(v)), kind, bool(usd))


def _rate(v: Any, exchange: Any = "", usd: Any = False) -> Num | str:
    """A price at its market's precision: 2 dp, or up to 6 for CDS / CRYPTO
    (EURUSD 1.08345 printed 1.08)."""
    if v is None or v == "":
        return ""
    fine = str(exchange) in _FINE_RATE
    d = _dec(v)
    return Num(quantize_basis(d) if fine else quantize_money(d), "fine" if fine else "rate", bool(usd))


def _qty(v: Any) -> Num | str:
    """Units, fractions kept: a 0.05 BTC fill printed 0."""
    if v is None or v == "":
        return "—"
    return Num(_dec(v).quantize(_UNIT_STEP), "qty")


def _total(cells: Any) -> Num:
    """Printed amounts summed exactly, so a Totals line is its rows' sum."""
    return Num(sum((c.value for c in cells), Decimal(0)))


def _fmt_money(v: float | int | str | None) -> str:
    return str(_amt(v, "inr"))


def _fmt_qty(v: Any) -> str:
    return str(_qty(v))


def _fmt_rate(v: Any, exchange: Any = "") -> str:
    return str(_rate(v, exchange))


def _fmt_date(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, datetime):
        return to_ist(v).strftime("%d %b %Y, %H:%M")
    return str(v)


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title",
            parent=base["Title"],
            fontName=_FONT_BOLD,
            fontSize=20,
            textColor=TEXT,
            spaceAfter=2,
            leading=24,
        ),
        "subtitle": ParagraphStyle(
            "subtitle",
            parent=base["Normal"],
            fontName=_FONT_NAME,
            fontSize=10,
            textColor=MUTED,
            spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontName=_FONT_BOLD,
            fontSize=12,
            textColor=TEXT,
            spaceBefore=6,
            spaceAfter=6,
        ),
        "label": ParagraphStyle(
            "label",
            parent=base["Normal"],
            fontName=_FONT_NAME,
            fontSize=9,
            textColor=MUTED,
            spaceAfter=2,
        ),
        "value": ParagraphStyle(
            "value",
            parent=base["Normal"],
            fontName=_FONT_NAME,
            fontSize=12,
            textColor=TEXT,
            spaceAfter=6,
        ),
        "footer": ParagraphStyle(
            "footer",
            parent=base["Normal"],
            fontName=_FONT_NAME,
            fontSize=8,
            textColor=MUTED,
            alignment=1,  # center
        ),
    }


def _header(title: str, subtitle: str, user_label: str, styles: dict) -> list:
    band = Table(
        [[Paragraph(f"<b>MarginWealth</b>", styles["title"]), Paragraph(user_label, styles["subtitle"])]],
        colWidths=[110 * mm, 70 * mm],
    )
    band.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LINEBELOW", (0, 0), (-1, -1), 1.5, BRAND),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ],
        ),
    )
    return [
        band,
        Spacer(1, 8),
        Paragraph(_text(title), styles["title"]),
        Paragraph(_text(subtitle), styles["subtitle"]),
        Spacer(1, 6),
    ]


def _table(rows: list[list[Any]], col_widths: list[float]) -> Table:
    """Build a tight-padded table that wraps long cell content instead of
    overflowing the column. Each cell is wrapped in a Paragraph so the
    layout engine breaks long strings (₹1,23,456.78 / symbol names) into
    multiple lines rather than running into the next column — the
    overflow the user reported in the P&L PDF.
    """
    base = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "td",
        parent=base["Normal"],
        fontName=_FONT_NAME,
        fontSize=8,
        leading=10,
        textColor=TEXT,
        wordWrap="CJK",  # break inside long money strings if needed
    )
    head_style = ParagraphStyle(
        "th",
        parent=base["Normal"],
        fontName=_FONT_BOLD,
        fontSize=8,
        leading=10,
        textColor=TEXT,
        wordWrap="CJK",
    )

    def cell(value: Any, header: bool) -> Any:
        if isinstance(value, Paragraph):
            return value
        return Paragraph(_text(value), head_style if header else body_style)

    wrapped: list[list[Any]] = []
    for i, r in enumerate(rows):
        wrapped.append([cell(c, header=(i == 0)) for c in r])

    t = Table(wrapped, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    t.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), BRAND_SOFT),
                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, BRAND),
                ("GRID", (0, 1), (-1, -1), 0.3, GRID),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ],
        ),
    )
    return t


def _summary_grid(items: list[tuple[str, str]], styles: dict) -> Table:
    """Borderless KPI strip — LABEL on top, VALUE below, four to a row.

    Previously this was a 4-up grid wrapped in `BOX` + `INNERGRID` lines
    which the user flagged as ugly cell borders in the PDF. The fix is
    to drop both lines and let the typography (small muted label / bold
    dark value) carry the structure on its own, which is what the
    in-app dashboard already does.
    """
    cells: list[list[Paragraph]] = []
    row: list[Paragraph] = []
    bold = _FONT_BOLD
    for label, value in items:
        cell = Paragraph(
            f"<font color='#64748B' size='8'>{_text(label.upper())}</font><br/>"
            f"<font name='{bold}' color='#0F172A' size='13'>{_text(value)}</font>",
            styles["value"],
        )
        row.append(cell)
        if len(row) == 4:
            cells.append(row)
            row = []
    if row:
        while len(row) < 4:
            row.append(Paragraph("", styles["value"]))
        cells.append(row)

    grid = Table(cells, colWidths=[45 * mm] * 4)
    grid.setStyle(
        TableStyle(
            [
                # No BOX / INNERGRID — clean borderless layout.
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ],
        ),
    )
    return grid


def _doc() -> tuple[SimpleDocTemplate, io.BytesIO]:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="MarginWealth Report",
    )
    return doc, buf


def _user_label(user) -> str:
    code = _text(getattr(user, "user_code", None) or "")
    name = _text(getattr(user, "full_name", None) or "")
    return f"{name}<br/><font size='9'>{code}</font>" if code or name else "Trader"


def _footer_text() -> str:
    return f"Generated by MarginWealth on {now_ist().strftime('%d %b %Y, %H:%M IST')}"


# ── The two documents' printed tables (PDF and workbook read these) ──


@dataclass
class Section:
    """One printed table: the PDF draws it, the workbook writes it."""

    title: str
    headers: list[str]
    weights: list[float]  # the PDF's column widths, relative (mm for the Trade Book)
    rows: list[list[Any]]
    totals: list[Any] | None = None
    empty: str = "None."
    card: bool = False  # Financial Standings: label / value cards in the PDF


_EXCHANGE_ORDER = ("NSE", "BSE", "MCX", "CDS", "CRYPTO")
_TRADES_HEAD = [
    "Ticket Id", "Script", "Qty", "Buy Rate", "Sell Rate", "Buy Time", "Sell Time",
    "Brokerage", "Gross PL", "Net PL", "Comment",
]  # fmt: skip
_FINANCIAL = (
    ("Balance", "balance"), ("Gross PnL", "gross_pnl"), ("Brokerage", "brokerage"), ("Net PnL", "net_pnl"),
)  # fmt: skip


def trade_book_section(rows: list[dict[str, Any]]) -> Section:
    """The Trade Book's table: one row per fill, newest first, as
    tradebook_builder.trade_rows lists them (settlement legs included)."""
    return Section(
        "Trade Book",
        ["Date", "Trade #", "Symbol", "Side", "Qty", "Price", "Value", "Charges"],
        # mm, 180 in all: the frame's width (an IST date and time needs the room).
        [32, 22, 26, 14, 16, 22, 26, 22],
        [
            [
                _fmt_date(r.get("executed_at")),
                r.get("trade_number") or "—",
                r.get("symbol") or "—",
                (r.get("action") or "").upper(),
                _qty(r.get("quantity")),
                _rate(r.get("price"), r.get("exchange"), r.get("usd")),
                _amt(r.get("value"), "inr", r.get("usd")),  # a notional in its own currency
                _amt(r.get("total_charges"), "inr"),
            ]
            for r in rows
        ],
    )


def billing_sections(payload: dict[str, Any]) -> list[Section]:
    """The Billing statement's tables (tradebook_builder.statement_payload),
    in the operator's order (handoff §4.1): Brought Forward → Funds
    Transactions → one Trades List per exchange → Script-wise P&L → Carry
    Forward → All Segments → Financial Standings. Nothing here is a live
    price; every Totals line is the sum of the rows printed above it."""
    out = [
        Section(
            "Brought Forward",
            ["Ticket Id", "Open Time", "Script", "Side", "Qty", "Price"],
            [7, 10, 12, 5, 6, 8],
            [
                [d["ticket_id"], d["time"], d["script"], d["side"], _qty(d["qty"]),
                 _rate(d["price"], d["exchange"], d.get("usd"))]
                for d in payload.get("brought_forward", [])
            ],  # fmt: skip
            empty="Nothing brought forward.",
        )
    ]

    # Funds, opening on the ledger's balance.
    funds = payload.get("funds", {})
    money = [(m, _amt(m["amount"])) for m in funds.get("rows", [])]
    out.append(
        Section(
            "Funds Transactions",
            ["Time", "Type", "Amount", "Comment"],
            [10, 8, 8, 24],
            [["", "Opening Balance", _amt(funds.get("opening_balance")), ""]]
            + [[m["time"], m["type"], a, m["comment"]] for m, a in money],
            ["", "Net Funds", _total(a for _, a in money), ""],
        )
    )

    # One Trades List per exchange; both legs on one line.
    closed = payload.get("closed_transactions", [])
    exchanges = sorted(
        {r["exchange"] for r in closed},
        key=lambda e: (_EXCHANGE_ORDER.index(e) if e in _EXCHANGE_ORDER else len(_EXCHANGE_ORDER), e),
    )
    segments: list[list[Any]] = []
    for ex in exchanges:
        rows = [
            [r["ticket_id"], r["script"], _qty(r["qty"]),
             _rate(r["buy_rate"], ex, r.get("usd")), _rate(r["sell_rate"], ex, r.get("usd")),
             r["buy_time"], r["sell_time"],
             _amt(r["brokerage"]), _amt(r["gross_pnl"]), _amt(r["net_pnl"]), r["comment"]]
            for r in closed
            if r["exchange"] == ex
        ]  # fmt: skip
        brk, gross = _total(r[7] for r in rows), _total(r[8] for r in rows)
        net = Num(gross.value - brk.value)
        segments.append([ex, brk, gross, net])
        out.append(
            Section(
                f"{ex} Trades List",
                _TRADES_HEAD,
                [6, 11, 5, 7, 7, 10, 10, 6, 7, 7, 8],
                rows,
                ["", "Totals", "", "", "", "", "", brk, gross, net, ""],
            )
        )
    if not exchanges:
        out.append(Section("Trades List", [], [], [], empty="No closed trades in this period."))

    sw = [
        [s["exchange"], s["script"], _qty(s["qty"]),
         _rate(s["avg_buy"], s["exchange"], s.get("usd")), _rate(s["avg_sell"], s["exchange"], s.get("usd")),
         _amt(s["buy_turnover"], usd=s.get("usd")), _amt(s["sell_turnover"], usd=s.get("usd")),
         s["first_time"], s["last_time"],
         _amt(s["brokerage"]), _amt(s["gross_pnl"]), _amt(s["net_pnl"])]
        for s in payload.get("script_wise", [])
    ]  # fmt: skip
    out.append(
        Section(
            "Script-wise P&L",
            ["Exchange", "Script", "Qty", "Avg Buy", "Avg Sell", "Buy Turnover", "Sell Turnover",
             "First Trade", "Last Trade", "Brokerage", "Gross PL", "Net PL"],  # fmt: skip
            [5, 10, 5, 7, 7, 8, 8, 10, 10, 6, 7, 7],
            sw,
            ["", "Totals", "", "", "", "", "", "", "", *(_total(r[i] for r in sw) for i in (9, 10, 11))],
            empty="No closed trades in this period.",
        )
    )

    # Carry Forward: what the period hands on.
    cf = [
        [d["ticket_id"], d["time"], d["script"], d["side"], _qty(d["qty"]),
         _rate(d["price"], d["exchange"], d.get("usd")), _amt(d["commission"]),
         _amt(d["value"], usd=d.get("usd"))]
        for d in payload.get("carry_forward", [])
    ]  # fmt: skip
    cf_com = _total(r[6] for r in cf)
    out.append(
        Section(
            "Carry Forward",
            ["Ticket Id", "Open Time", "Script", "Side", "Qty", "Price", "Commission", "Value"],
            [7, 10, 12, 5, 6, 8, 7, 9],
            cf,
            ["", "", "Totals", "", "", "", cf_com, ""],
            empty="Nothing carried forward.",
        )
    )

    # All Segments: the one overall figure, back together. The carried
    # positions' opening charges are the period's too.
    if cf_com.value:
        segments.append(["Carry Forward", cf_com, Num(Decimal(0)), Num(-cf_com.value)])
    out.append(
        Section(
            "All Segments",
            ["Segment", "Brokerage", "Gross PL", "Net PL"],
            [10, 8, 8, 8],
            segments,
            ["All Segments", *(_total(r[i] for r in segments) for i in (1, 2, 3))],
        )
    )

    fin = payload.get("financial", {})
    out.append(
        Section(
            "Financial Standings",
            [label for label, _ in _FINANCIAL],
            [1, 1, 1, 1],
            [[_amt(fin.get(key, 0), "inr") for _, key in _FINANCIAL]],
            card=True,
        )
    )
    return out


# ── Builders ──────────────────────────────────────────────────────────


def build_pnl_pdf(user, payload: dict) -> bytes:
    styles = _styles()
    doc, buf = _doc()
    rng_from = _fmt_date(payload.get("from"))
    rng_to = _fmt_date(payload.get("to"))
    net_pnl = _num(payload.get("net_pnl") or 0)
    elems = _header(
        "Profit & Loss Statement",
        f"Period: {rng_from} → {rng_to}",
        _user_label(user),
        styles,
    )
    elems.append(
        _summary_grid(
            [
                ("Total trades", str(payload.get("total_trades", 0))),
                ("Buy value", _fmt_money(payload.get("total_buy_value"))),
                ("Sell value", _fmt_money(payload.get("total_sell_value"))),
                ("Charges", _fmt_money(payload.get("total_charges"))),
            ],
            styles,
        ),
    )
    elems.append(Spacer(1, 14))
    pnl_color = "#0F766E" if net_pnl >= 0 else "#DC2626"
    elems.append(
        Paragraph(
            f"<font color='#64748B' size='10'>NET P&amp;L</font><br/>"
            f"<font color='{pnl_color}' size='22'><b>{('+' if net_pnl >= 0 else '')}{_fmt_money(net_pnl)}</b></font>",
            styles["value"],
        ),
    )
    elems.append(Spacer(1, 12))

    by_symbol = payload.get("by_symbol") or []
    if by_symbol:
        elems.append(Paragraph("Symbol-wise breakdown", styles["h2"]))
        rows: list[list[Any]] = [["Symbol", "Buy Qty", "Sell Qty", "Buy Value", "Sell Value", "Charges", "P&L"]]
        for s in by_symbol:
            pnl = _num(s.get("pnl") or 0)
            rows.append(
                [
                    s.get("symbol", "—"),
                    _fmt_qty(s.get("buy_qty")),
                    _fmt_qty(s.get("sell_qty")),
                    _fmt_money(s.get("buy_value")),
                    _fmt_money(s.get("sell_value")),
                    _fmt_money(s.get("charges")),
                    f"{('+' if pnl >= 0 else '')}{_fmt_money(pnl)}",
                ],
            )
        # 28+20+20+30+30+24+28 = 180mm, the content area exactly (it summed
        # to 168). With the 8pt wrapping font set in `_table()`, money
        # columns have room for ₹1,23,45,678.
        elems.append(
            _table(rows, [28 * mm, 20 * mm, 20 * mm, 30 * mm, 30 * mm, 24 * mm, 28 * mm]),
        )

    elems.append(Spacer(1, 14))
    elems.append(Paragraph(_footer_text(), styles["footer"]))
    doc.build(elems)
    return buf.getvalue()


def build_tradebook_pdf(user, rows: list[dict]) -> bytes:
    styles = _styles()
    doc, buf = _doc()
    elems = _header(
        "Trade Book",
        f"{len(rows)} trades",
        _user_label(user),
        styles,
    )
    t = trade_book_section(rows)
    elems.append(_table([t.headers, *t.rows], [w * mm for w in t.weights]))
    elems.append(Spacer(1, 14))
    elems.append(Paragraph(_footer_text(), styles["footer"]))
    doc.build(elems)
    return buf.getvalue()


def build_brokerage_pdf(user, payload: dict) -> bytes:
    styles = _styles()
    doc, buf = _doc()
    totals = payload.get("totals") or {}
    elems = _header(
        "Brokerage Summary",
        f"Period: {_fmt_date(payload.get('from'))} → {_fmt_date(payload.get('to'))}",
        _user_label(user),
        styles,
    )
    elems.append(
        _summary_grid(
            [
                ("Total trades", str(payload.get("trade_count", 0))),
                ("Brokerage", _fmt_money(totals.get("brokerage"))),
                ("Total charges", _fmt_money(totals.get("total"))),
                ("Net (charges)", _fmt_money(totals.get("total"))),
            ],
            styles,
        ),
    )
    elems.append(Spacer(1, 14))
    elems.append(Paragraph(_footer_text(), styles["footer"]))
    doc.build(elems)
    return buf.getvalue()


def build_tax_pdf(user, payload: dict) -> bytes:
    styles = _styles()
    doc, buf = _doc()
    elems = _header(
        "Tax P&L (simplified)",
        "Indian capital-gains bucketization (simplified; consult a tax advisor)",
        _user_label(user),
        styles,
    )
    buckets = payload.get("buckets") or {}
    body: list[list[Any]] = [["Bucket", "Net realized"]]
    label_map = {
        "intraday_speculative": "Intraday (speculative)",
        "stcg": "Equity STCG",
        "ltcg": "Equity LTCG",
        "fno": "Futures & Options",
    }
    for k, v in buckets.items():
        body.append([label_map.get(k, k), _fmt_money(v)])
    elems.append(_table(body, [60 * mm, 40 * mm]))
    elems.append(Spacer(1, 14))
    elems.append(Paragraph(_footer_text(), styles["footer"]))
    doc.build(elems)
    return buf.getvalue()


def build_full_tradebook_pdf(user, payload: dict) -> bytes:
    """The Billing statement (landscape A4), in the operator's order (handoff
    §4.1): Brought Forward → Funds Transactions → one Trades List per exchange
    → Script-wise P&L → Carry Forward → All Segments → Financial Standings.
    Rates print at their market's precision, units with their fractions."""
    from reportlab.lib.pagesizes import landscape

    ACCENT = BRAND
    ACCENT_SOFT = BRAND_SOFT
    WHITE = rl_colors.white
    FONT_SZ = 7
    HDR_SZ = 7

    styles = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=10 * mm, rightMargin=10 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title="Billing Statement",
    )
    page_w = landscape(A4)[0] - 20 * mm

    code = getattr(user, "user_code", None) or ""
    uname = getattr(user, "full_name", None) or ""
    account_label = f"{code}: {uname}" if code and uname else code or uname or "Trader"

    rng_from = payload.get("from_label", "Beginning")
    rng_to = payload.get("to_label", "Now")
    generated_at = payload.get("generated_at", now_ist().strftime("%Y-%m-%d %H:%M:%S"))

    # ── Branded header ─────────────────────────────────────────────
    brand_name = payload.get("admin_brand_name", "") or "MarginWealth"
    header_data = [
        [
            Paragraph(
                f"<font name='{_FONT_BOLD}' size='12' color='#3B5BDB'>{_text(brand_name)}</font>",
                styles["title"],
            ),
            Paragraph(
                f"<font name='{_FONT_BOLD}' size='16' color='#263C9C'>Billing Statement</font>",
                styles["title"],
            ),
            Paragraph(
                f"<font size='8' color='#64748B'>Account Statement</font>",
                styles["subtitle"],
            ),
        ],
    ]
    header_tbl = Table(header_data, colWidths=[page_w * 0.35, page_w * 0.35, page_w * 0.30])
    header_tbl.setStyle(TableStyle([
        ("ALIGN", (0, 0), (0, 0), "LEFT"),
        ("ALIGN", (1, 0), (1, 0), "CENTER"),
        ("ALIGN", (2, 0), (2, 0), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 2, ACCENT),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    meta_style = ParagraphStyle(
        "ark_meta", parent=styles["subtitle"],
        fontName=_FONT_NAME, fontSize=8, textColor=TEXT, alignment=1,
    )
    elems: list = [
        header_tbl,
        Spacer(1, 4),
        Paragraph(f"<b>{_text(account_label)}</b>", meta_style),
        Paragraph(
            f"<b>From:</b> {_text(rng_from)}  <b>To:</b> {_text(rng_to)}  |  "
            f"<b>Time:</b> {_text(generated_at)}",
            meta_style,
        ),
        Spacer(1, 8),
    ]

    base_ss = getSampleStyleSheet()

    def _ark_cell_style(bold: bool = False, size: int = FONT_SZ, color=None) -> ParagraphStyle:
        return ParagraphStyle(
            f"ark_{'th' if bold else 'td'}_{size}_{id(color)}",
            parent=base_ss["Normal"],
            fontName=_FONT_BOLD if bold else _FONT_NAME,
            fontSize=size, leading=size + 2,
            textColor=color or (WHITE if bold else TEXT),
            wordWrap="CJK",
        )

    th = _ark_cell_style(bold=True, size=HDR_SZ)
    td = _ark_cell_style(bold=False, size=FONT_SZ)
    td_b = _ark_cell_style(bold=True, size=FONT_SZ, color=TEXT)

    def section(title: str) -> None:
        # Claim room for the heading, the header row and a line of data, or
        # the heading strands at a page foot with its table overleaf.
        elems.extend([CondPageBreak(25 * mm), Paragraph(f"<b>{_text(title)}</b>", styles["h2"])])

    def table(
        headers: list[str],
        weights: list[float],
        rows: list[list[Any]],
        totals: list[Any] | None = None,
        empty: str = "None.",
    ) -> None:
        if not rows:
            elems.extend([Paragraph(_text(empty), styles["subtitle"]), Spacer(1, 8)])
            return
        data = [[Paragraph(_text(h), th) for h in headers]]
        data += [[Paragraph(_text(c), td) for c in r] for r in rows]
        if totals:
            data.append([Paragraph(_text(c), td_b) for c in totals])
        t = Table(data, colWidths=[page_w * w / sum(weights) for w in weights], hAlign="LEFT", repeatRows=1)
        style_cmds: list = [
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("ALIGN", (0, 0), (-1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.25, GRID),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]
        style_cmds += [
            ("BACKGROUND", (0, i), (-1, i), rl_colors.HexColor("#FAFAFA")) for i in range(2, len(data), 2)
        ]
        if totals:
            style_cmds += [
                ("BACKGROUND", (0, -1), (-1, -1), ACCENT_SOFT),
                ("LINEABOVE", (0, -1), (-1, -1), 0.75, ACCENT),
            ]
        t.setStyle(TableStyle(style_cmds))
        elems.extend([t, Spacer(1, 8)])

    td_fin = ParagraphStyle(
        "ark_fin", parent=base_ss["Normal"],
        fontName=_FONT_NAME, fontSize=8, leading=11, textColor=TEXT,
    )
    for sec in billing_sections(payload):
        section(sec.title)
        if not sec.card:
            table(sec.headers, sec.weights, sec.rows, sec.totals, sec.empty)
            continue
        # Financial Standings: the period's own four figures.
        cards = Table(
            [[
                Paragraph(
                    f"<font color='#64748B' size='7'><b>{_text(label)}</b></font><br/>"
                    f"<font name='{_FONT_BOLD}' size='10'>{_text(value)}</font>",
                    td_fin,
                )
                for label, value in zip(sec.headers, sec.rows[0], strict=True)
            ]],
            colWidths=[page_w / len(sec.headers)] * len(sec.headers),
        )
        cards.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("BOX", (0, 0), (-1, -1), 0.5, ACCENT),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, GRID),
            ("BACKGROUND", (0, 0), (-1, -1), ACCENT_SOFT),
        ]))
        elems.append(cards)

    elems.append(Spacer(1, 10))
    elems.append(Paragraph(
        f"{_text(brand_name)}  |  Generated on {now_ist().strftime('%d %b %Y, %H:%M IST')}",
        styles["footer"],
    ))
    doc.build(elems)
    return buf.getvalue()


def build_margin_pdf(user, summary: dict) -> bytes:
    """`summary`: user reports' _margin_payload (the wallet summary plus the
    statement's lifetime `brokerage`)."""
    styles = _styles()
    doc, buf = _doc()
    elems = _header(
        "Margin Report",
        "Live wallet snapshot",
        _user_label(user),
        styles,
    )
    elems.append(
        _summary_grid(
            [
                ("Available balance", _fmt_money(summary.get("available_balance"))),
                ("Used margin", _fmt_money(summary.get("used_margin"))),
                ("Credit limit", _fmt_money(summary.get("credit_limit"))),
                ("Realized P&L", _fmt_money(summary.get("realized_pnl"))),
                ("Unrealized P&L", _fmt_money(summary.get("unrealized_pnl"))),
                ("Total deposits", _fmt_money(summary.get("total_deposits"))),
                ("Total withdrawals", _fmt_money(summary.get("total_withdrawals"))),
                ("Total brokerage", _fmt_money(summary.get("brokerage"))),
            ],
            styles,
        ),
    )
    elems.append(Spacer(1, 14))
    elems.append(Paragraph(_footer_text(), styles["footer"]))
    doc.build(elems)
    return buf.getvalue()
