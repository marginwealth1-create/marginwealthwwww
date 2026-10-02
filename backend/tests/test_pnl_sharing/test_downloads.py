"""Smoke tests for PDF + Excel report renderers.

We verify the renderers produce non-empty output with the right magic bytes,
not the actual layout. Layout regressions are caught manually.
"""

from datetime import datetime, timezone

from app.schemas.pnl_sharing import (
    AgreementDTO,
    ReportResponse,
    ReportRow,
    ReportSummary,
)
from app.services.pnl_sharing_excel_service import render_report_excel
from app.services.pnl_sharing_pdf_service import render_report_pdf


def _fake_report() -> ReportResponse:
    a = AgreementDTO(
        id="agid", admin_id="aid", broker_id="bid", share_pct="30",
        admin_user_code="MZADMIN", admin_name=None,
        broker_user_code="MZBROKER", broker_name=None,
        settlement_mode="MANUAL", settlement_cadence=None,
        agreement_type="PNL_AND_BROKERAGE",
        status="ACTIVE",
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        effective_until=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    rows = [
        ReportRow(
            period_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            period_end=datetime(2026, 5, 31, tzinfo=timezone.utc),
            net_client_pnl_inr="-1000.00",
            net_client_bkg_inr="500.00",
            total_of_both_inr="1500.00",
            actual_pnl_inr="1500.00",
            sharing_pnl_inr="300.00",
            sharing_bkg_inr="150.00",
            settlement_status="SETTLED",
        ),
    ]
    s = ReportSummary(
        total_sharing_pnl_inr="300.00",
        total_sharing_bkg_inr="150.00",
        periods_settled=1,
        periods_pending=0,
        periods_failed=0,
        periods_unsettled=0,
    )
    return ReportResponse(agreement=a, rows=rows, summary=s)


def test_render_pdf_returns_pdf_magic_bytes():
    b = render_report_pdf(_fake_report())
    assert b.startswith(b"%PDF"), "Output must be a PDF"
    assert len(b) > 500, "PDF should be non-trivial size"


def test_render_excel_returns_xlsx_zip_magic():
    b = render_report_excel(_fake_report())
    # xlsx is a zip archive — starts with PK\x03\x04
    assert b[:2] == b"PK"
    assert len(b) > 500


def test_render_pdf_empty_rows_still_renders():
    """No rows shouldn't crash the renderer."""
    rep = _fake_report()
    rep.rows.clear()
    b = render_report_pdf(rep)
    assert b.startswith(b"%PDF")


def test_render_excel_empty_rows_still_renders():
    rep = _fake_report()
    rep.rows.clear()
    b = render_report_excel(rep)
    assert b[:2] == b"PK"
