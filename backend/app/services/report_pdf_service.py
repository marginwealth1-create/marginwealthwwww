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
from datetime import datetime
from typing import Any

from reportlab.lib import colors as rl_colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.fonts import addMapping
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

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


def _fmt_money(v: float | int | str | None) -> str:
    return f"{_rupee()}{_num(v):,.2f}"


def _fmt_qty(v: Any) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v or "—")


def _fmt_date(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, datetime):
        return v.strftime("%d %b %Y, %H:%M")
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
        Paragraph(title, styles["title"]),
        Paragraph(subtitle, styles["subtitle"]),
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
        return Paragraph(str(value), head_style if header else body_style)

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
            f"<font color='#64748B' size='8'>{label.upper()}</font><br/>"
            f"<font name='{bold}' color='#0F172A' size='13'>{value}</font>",
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
    code = getattr(user, "user_code", None) or ""
    name = getattr(user, "full_name", None) or ""
    return f"{name}<br/><font size='9'>{code}</font>" if code or name else "Trader"


def _footer_text() -> str:
    return f"Generated by MarginWealth on {datetime.now().strftime('%d %b %Y, %H:%M IST')}"


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
        # Trimmed column widths — 28+22+22+28+28+24+28 = 180mm fit the
        # 180mm content area exactly. With the wider 8pt + wrapping font
        # set in `_table()`, money columns now have room for ₹1,23,45,678.
        elems.append(
            _table(rows, [26 * mm, 18 * mm, 18 * mm, 28 * mm, 28 * mm, 22 * mm, 28 * mm]),
        )

    elems.append(Spacer(1, 14))
    elems.append(Paragraph(_footer_text(), styles["footer"]))
    doc.build(elems)
    return buf.getvalue()


def build_tradebook_pdf(user, rows: list[dict]) -> bytes:
    styles = _styles()
    doc, buf = _doc()
    elems = _header(
        "Tradebook",
        f"{len(rows)} trades",
        _user_label(user),
        styles,
    )
    body: list[list[Any]] = [
        ["Date", "Trade #", "Symbol", "Side", "Qty", "Price", "Value", "Charges"],
    ]
    for r in rows:
        side = (r.get("action") or "").upper()
        body.append(
            [
                _fmt_date(r.get("executed_at")),
                r.get("trade_number") or "—",
                r.get("symbol") or "—",
                side,
                _fmt_qty(r.get("quantity")),
                _fmt_money(r.get("price")),
                _fmt_money(r.get("value")),
                _fmt_money(r.get("total_charges")),
            ],
        )
    elems.append(
        _table(
            body,
            [28 * mm, 22 * mm, 24 * mm, 14 * mm, 16 * mm, 22 * mm, 26 * mm, 22 * mm],
        ),
    )
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
    """ARK Trader-style comprehensive tradebook PDF (landscape A4).

    Sections: Branded Header → Closed Transactions → Money Totals →
    Opened Deals → Pending Orders → Financial Standings.
    """
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
        title="Trade Book",
    )
    page_w = landscape(A4)[0] - 20 * mm

    code = getattr(user, "user_code", None) or ""
    uname = getattr(user, "full_name", None) or ""
    account_label = f"{code}: {uname}" if code and uname else code or uname or "Trader"

    admin_brand = payload.get("admin_brand_name", "") or ""
    rng_from = payload.get("from_label", "Beginning")
    rng_to = payload.get("to_label", "Now")
    generated_at = payload.get("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    total_brokerage = _num(payload.get("total_brokerage", 0))

    # ── Branded header ─────────────────────────────────────────────
    brand_name = admin_brand or "MarginWealth"
    header_data = [
        [
            Paragraph(
                f"<font name='{_FONT_BOLD}' size='12' color='#3B5BDB'>{brand_name}</font>",
                styles["title"],
            ),
            Paragraph(
                f"<font name='{_FONT_BOLD}' size='16' color='#263C9C'>Trade Book</font>",
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
        Paragraph(f"<b>{account_label}</b>", meta_style),
        Paragraph(f"<b>From:</b> {rng_from}  <b>To:</b> {rng_to}  |  <b>Time:</b> {generated_at}", meta_style),
        Spacer(1, 8),
    ]

    # ── Helper: green-header table (6pt font, no wrapping) ─────────
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

    def _ark_table(
        headers: list[str],
        rows: list[list[str]],
        col_widths: list[float],
        totals_row: list[str] | None = None,
    ) -> Table:
        th = _ark_cell_style(bold=True, size=HDR_SZ)
        td = _ark_cell_style(bold=False, size=FONT_SZ)

        data: list[list] = [[Paragraph(h, th) for h in headers]]
        for r in rows:
            data.append([Paragraph(str(c), td) for c in r])

        if totals_row:
            td_b = _ark_cell_style(bold=True, size=FONT_SZ, color=TEXT)
            data.append([Paragraph(str(c), td_b) for c in totals_row])

        t = Table(data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
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
        for i in range(1, len(data)):
            if i % 2 == 0:
                style_cmds.append(("BACKGROUND", (0, i), (-1, i), rl_colors.HexColor("#FAFAFA")))
        if totals_row:
            last = len(data) - 1
            style_cmds.append(("BACKGROUND", (0, last), (-1, last), ACCENT_SOFT))
            style_cmds.append(("LINEABOVE", (0, last), (-1, last), 0.75, ACCENT))
        t.setStyle(TableStyle(style_cmds))
        return t

    # ── Section 1: Closed Transactions ─────────────────────────────
    closed = payload.get("closed_transactions", [])
    elems.append(Paragraph("<b>Closed Transactions</b>", styles["h2"]))

    ct_headers = [
        "Time", "Type", "Ticket Id", "Script", "Amt",
        "Side", "Open Price", "Close Price",
        "DP/WD/AJ", "Brokerage", "Total PnL",
    ]
    # 11 cols, proportional to page_w — fills entire page
    _ct = [8, 3.5, 7, 9, 4, 3.5, 7, 7, 7, 6, 7]
    _ct_sum = sum(_ct)
    ct_widths = [page_w * w / _ct_sum for w in _ct]

    ct_rows: list[list[str]] = []
    total_brokerage_col = 0.0
    total_pnl = 0.0
    for tx in closed:
        brokerage = _num(tx.get("brokerage") or 0)
        pnl = _num(tx.get("total_pnl") or 0)
        total_brokerage_col += brokerage
        total_pnl += pnl

        pnl_str = f"{pnl:,.2f}" if pnl else ""
        dp_wd = tx.get("dp_wd_aj", "")
        if dp_wd and isinstance(dp_wd, (int, float)):
            dp_wd = f"{dp_wd:,.2f}"

        ct_rows.append([
            tx.get("time", ""),
            tx.get("type", ""),
            str(tx.get("ticket_id", "")),
            tx.get("script", ""),
            str(tx.get("amount", "")),
            tx.get("type_detail", ""),
            tx.get("open_price", ""),
            tx.get("close_price", ""),
            str(dp_wd),
            f"{brokerage:,.2f}" if brokerage else "0.00",
            pnl_str,
        ])

    totals_row = [
        "", "", "", "", "Totals", "", "", "",
        "", f"{total_brokerage_col:,.2f}",
        f"{total_pnl:,.2f}",
    ]

    if ct_rows:
        elems.append(_ark_table(ct_headers, ct_rows, ct_widths, totals_row))
    else:
        elems.append(Paragraph("No closed transactions in this period.", styles["subtitle"]))
    elems.append(Spacer(1, 8))

    # ── Section 2: Money Totals + Brokerage ────────────────────────
    money = payload.get("money_totals", {})
    elems.append(Paragraph("<b>Money Totals</b>", styles["h2"]))

    money_headers = ["CreditIn", "CreditOut", "Deposit", "Withdraw", "Adjustment", "Bonus", "Total Brokerage"]
    mw = page_w / 7
    money_widths = [mw] * 7
    money_row = [
        f"{_num(money.get('credit_in', 0)):,.2f}",
        f"{_num(money.get('credit_out', 0)):,.2f}",
        f"{_num(money.get('deposit', 0)):,.2f}",
        f"{_num(money.get('withdraw', 0)):,.2f}",
        f"{_num(money.get('adjustment', 0)):,.2f}",
        f"{_num(money.get('bonus', 0)):,.2f}",
        f"{total_brokerage:,.2f}",
    ]
    elems.append(_ark_table(money_headers, [money_row], money_widths))
    elems.append(Spacer(1, 8))

    # ── Section 3: Opened Deals ────────────────────────────────────
    opened = payload.get("opened_deals", [])
    elems.append(Paragraph("<b>Opened Deals</b>", styles["h2"]))

    od_headers = [
        "Ticket Id", "Time", "Side", "Amt", "Script",
        "Price", "SL", "TP", "Current Price",
        "Commission", "Total PnL", "Value",
    ]
    _od = [7, 9, 4, 5, 10, 8, 7, 7, 8, 7, 8, 10]
    _od_sum = sum(_od)
    od_widths = [page_w * w / _od_sum for w in _od]
    od_rows: list[list[str]] = []
    od_total_amt = 0.0
    od_total_com = 0.0
    od_total_pnl = 0.0
    od_total_val = 0.0
    for d in opened:
        # `amount` arrives pre-formatted ("9,000.00") from the payload builder,
        # so parse comma-tolerantly — a bare float() 500'd the whole PDF.
        amt = _num(d.get("amount"))
        com = _num(d.get("commission"))
        pnl = _num(d.get("total_pnl"))
        val = _num(d.get("value"))
        od_total_amt += amt
        od_total_com += com
        od_total_pnl += pnl
        od_total_val += val
        od_rows.append([
            str(d.get("ticket_id", "")),
            d.get("time", ""),
            d.get("type_detail", ""),
            f"{amt:,.2f}" if amt else str(d.get("amount", "")),
            d.get("script", ""),
            d.get("price", ""),
            d.get("sl", ""),
            d.get("tp", ""),
            d.get("current_price", ""),
            f"{com:,.2f}",
            f"{pnl:,.2f}",
            f"{val:,.2f}",
        ])

    od_totals = [
        "", "", "Totals", f"{od_total_amt:,.2f}", "", "", "", "", "",
        f"{od_total_com:,.2f}", f"{od_total_pnl:,.2f}", f"{od_total_val:,.2f}",
    ]
    if od_rows:
        elems.append(_ark_table(od_headers, od_rows, od_widths, od_totals))
    else:
        elems.append(Paragraph("No open deals.", styles["subtitle"]))
    elems.append(Spacer(1, 8))

    # ── Section 4: Pending Orders ──────────────────────────────────
    pending = payload.get("pending_orders", [])
    elems.append(Paragraph("<b>Pending Orders</b>", styles["h2"]))

    po_headers = ["Order Id", "Type", "Side", "Amt", "Script", "Price", "SL", "TP", "Time"]
    pw = page_w / 9
    po_widths = [pw] * 9
    po_rows: list[list[str]] = []
    for o in pending:
        po_rows.append([
            str(o.get("order_id", "")),
            o.get("type", ""),
            o.get("type_detail", ""),
            str(o.get("amount", "")),
            o.get("script", ""),
            str(o.get("price", "")),
            str(o.get("sl", "")),
            str(o.get("tp", "")),
            o.get("time", ""),
        ])
    if po_rows:
        elems.append(_ark_table(po_headers, po_rows, po_widths))
    else:
        elems.append(Paragraph("No pending orders.", styles["subtitle"]))
    elems.append(Spacer(1, 8))

    # ── Section 5: Financial Standings ─────────────────────────────
    fin = payload.get("financial", {})
    elems.append(Paragraph("<b>Financial Standings</b>", styles["h2"]))

    fin_items = [
        ("Balance", f"{_rupee()}{_num(fin.get('balance', 0)):,.2f}"),
        ("Credit", f"{_rupee()}{_num(fin.get('credit', 0)):,.2f}"),
        ("Equity", f"{_rupee()}{_num(fin.get('equity', 0)):,.2f}"),
        ("Total PnL", f"{_rupee()}{_num(fin.get('total_pnl', 0)):,.2f}"),
        ("Used Margin", f"{_rupee()}{_num(fin.get('used_margin', 0)):,.2f}"),
        ("Holding Margin", f"{_rupee()}{_num(fin.get('holding_margin', 0)):,.2f}"),
        ("Free Margin", f"{_rupee()}{_num(fin.get('free_margin', 0)):,.2f}"),
        ("Margin Level", fin.get("margin_level", "0.00%")),
        ("Brokerage Paid", f"{_rupee()}{total_brokerage:,.2f}"),
    ]

    td_fin = ParagraphStyle(
        "ark_fin", parent=base_ss["Normal"],
        fontName=_FONT_NAME, fontSize=8, leading=11, textColor=TEXT,
    )
    fin_data: list[list] = []
    fin_row: list = []
    for label, value in fin_items:
        cell = Paragraph(
            f"<font color='#64748B' size='7'><b>{label}</b></font><br/>"
            f"<font name='{_FONT_BOLD}' size='10'>{value}</font>",
            td_fin,
        )
        fin_row.append(cell)
        if len(fin_row) == 5:
            fin_data.append(fin_row)
            fin_row = []
    if fin_row:
        while len(fin_row) < 5:
            fin_row.append(Paragraph("", td_fin))
        fin_data.append(fin_row)

    cw = page_w / 5
    fin_tbl = Table(fin_data, colWidths=[cw] * 5)
    fin_tbl.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BOX", (0, 0), (-1, -1), 0.5, ACCENT),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, GRID),
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_SOFT),
    ]))
    elems.append(fin_tbl)

    elems.append(Spacer(1, 10))
    elems.append(Paragraph(
        f"{brand_name}  |  Generated on {datetime.now().strftime('%d %b %Y, %H:%M IST')}",
        styles["footer"],
    ))
    doc.build(elems)
    return buf.getvalue()


def build_margin_pdf(user, summary: dict) -> bytes:
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
                ("Total brokerage", _fmt_money(summary.get("total_brokerage"))),
            ],
            styles,
        ),
    )
    elems.append(Spacer(1, 14))
    elems.append(Paragraph(_footer_text(), styles["footer"]))
    doc.build(elems)
    return buf.getvalue()
