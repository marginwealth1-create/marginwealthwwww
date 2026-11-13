"""The Billing statement and the Trade Book as Excel workbooks.

Both read the PDFs' own tables (report_pdf_service.billing_sections /
trade_book_section), so a workbook can't print a number its PDF doesn't. One
sheet per document: each section is a bold title row, a bold header row, its
data rows, a bold totals row and a blank row. Amounts, rates and units are
numbers with a number format ('$' on a USD rate or notional); dates and
times are text in IST, as the PDF prints them.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.services.report_pdf_service import Num, Section, billing_sections, trade_book_section

_BOLD = Font(bold=True)


def _number_format(n: Num) -> str:
    if n.kind == "qty":
        return "#,##0" if n.value == n.value.to_integral_value() else "#,##0.########"
    fmt = "#,##0.00####" if n.kind == "fine" else "#,##0.00"
    if n.usd:
        return f'"$"{fmt}'
    return f'"₹"{fmt}' if n.kind == "inr" else fmt


def _render(title: str, meta: list[str], sections: list[Section], *, freeze: bool = False) -> bytes:
    wb = Workbook()
    ws: Worksheet = wb.active
    ws.title = title
    widths: dict[int, int] = {}

    def put(cells: list[Any], *, bold: bool = False, measure: bool = True) -> None:
        ws.append([c.value if isinstance(c, Num) else (c if c != "" else None) for c in cells])
        for i, c in enumerate(cells, 1):
            cell = ws.cell(ws.max_row, i)
            if bold:
                cell.font = _BOLD
            if isinstance(c, Num):
                cell.number_format = _number_format(c)
            elif isinstance(c, str) and c:
                # Text as the PDF prints it: openpyxl would make an admin's
                # '=SUM(…)' Funds comment a formula.
                cell.data_type = "s"
            if measure:
                widths[i] = max(widths.get(i, 0), len(str(c)))

    put([title], measure=False)
    ws["A1"].font = Font(bold=True, size=14)
    for line in meta:
        put([line], measure=False)
    ws.append([])
    for s in sections:
        put([s.title], bold=True, measure=False)
        if not s.rows:
            put([s.empty], measure=False)
        else:
            put(s.headers, bold=True)
            if freeze:
                ws.freeze_panes = f"A{ws.max_row + 1}"  # a string: ws.cell() would add a row
            for r in s.rows:
                put(r)
            if s.totals:
                put(s.totals, bold=True)
        ws.append([])
    for i, w in widths.items():
        ws.column_dimensions[get_column_letter(i)].width = min(max(w + 2, 10), 48)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _account(user: Any) -> str:
    code = getattr(user, "user_code", None) or ""
    name = getattr(user, "full_name", None) or ""
    return f"{code}: {name}" if code and name else code or name or "Trader"


def render_billing_xlsx(user: Any, payload: dict[str, Any]) -> bytes:
    """`payload`: tradebook_builder.statement_payload, as the PDF gets it."""
    return _render(
        "Billing Statement",
        [
            payload.get("admin_brand_name", "") or "MarginWealth",
            _account(user),
            f"From: {payload.get('from_label', 'Beginning')}  To: {payload.get('to_label', 'Now')}  |  "
            f"Time: {payload.get('generated_at', '')}",
        ],
        billing_sections(payload),
    )


def render_trade_book_xlsx(user: Any, rows: list[dict[str, Any]]) -> bytes:
    """`rows`: tradebook_builder.trade_rows, newest first. The header stays
    on screen as the fills scroll."""
    return _render(
        "Trade Book", [_account(user), f"{len(rows)} trades"], [trade_book_section(rows)], freeze=True
    )
