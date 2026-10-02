"""Renders a P&L Sharing report as a PDF byte stream.

Uses ReportLab Platypus to build a clean A4 layout:
- Header: agreement metadata (admin ↔ broker, %, mode, cadence, status)
- Period summary card (Sharing PNL, Sharing BKG totals over the date range)
- Per-period table (rows from build_report)

Returns bytes so the API layer can stream them via StreamingResponse.
"""

from __future__ import annotations

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.schemas.pnl_sharing import ReportResponse


def render_report_pdf(report: ReportResponse) -> bytes:
    """Build a PDF for the report. Returns the raw bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="P&L Sharing Report",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=16, spaceAfter=8)
    meta = ParagraphStyle(
        "meta", parent=styles["Normal"], fontSize=9, textColor=colors.grey
    )

    a = report.agreement
    elements: list = []

    # Title
    elements.append(
        Paragraph(
            f"P&amp;L Sharing — {a.admin_user_code or a.admin_id} ⇄ "
            f"{a.broker_user_code or a.broker_id}",
            h1,
        )
    )
    elements.append(
        Paragraph(
            f"{a.share_pct}% · {a.settlement_mode}"
            f"{' · ' + a.settlement_cadence if a.settlement_cadence else ''}"
            f" · {a.status}",
            meta,
        )
    )
    elements.append(Spacer(1, 8))

    # Summary card
    s = report.summary
    summary_data = [
        ["Total Sharing PNL", s.total_sharing_pnl_inr],
        ["Total Sharing BKG", s.total_sharing_bkg_inr],
        ["Periods Settled", str(s.periods_settled)],
        ["Periods Pending", str(s.periods_pending)],
        ["Periods Failed", str(s.periods_failed)],
        ["Periods Unsettled", str(s.periods_unsettled)],
    ]
    summary_tbl = Table(summary_data, colWidths=[60 * mm, 60 * mm])
    summary_tbl.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.grey),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    elements.append(summary_tbl)
    elements.append(Spacer(1, 14))

    # Detail rows
    elements.append(Paragraph("Period detail", styles["Heading3"]))
    table_data = [
        [
            "Period start",
            "Period end",
            "Net Client PNL",
            "Net Client BKG",
            "Total",
            "Sharing PNL",
            "Sharing BKG",
            "Status",
        ]
    ]
    for r in report.rows:
        table_data.append(
            [
                r.period_start.strftime("%Y-%m-%d"),
                r.period_end.strftime("%Y-%m-%d"),
                r.net_client_pnl_inr,
                r.net_client_bkg_inr,
                r.total_of_both_inr,
                r.sharing_pnl_inr,
                r.sharing_bkg_inr,
                r.settlement_status,
            ]
        )
    detail = Table(table_data, repeatRows=1)
    detail.setStyle(
        TableStyle(
            [
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#27272a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (2, 0), (-2, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d4d4d8")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    elements.append(detail)

    doc.build(elements)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes
