"""Shared FIFO tradebook rows.

The SINGLE source both the user and the admin "Full Tradebook" PDFs render for
their closed-trade section. It is the EXACT same FIFO reconstruction the app's
Closed tab uses (``position_service.list_closed_trade_events_fifo``), so the
PDF matches the Closed tab **entry-by-entry** with the **same net P&L** — no
average-cost drift, no "tradebook aur closed-trade alag" mismatch.

Why not the per-``Trade.pnl_inr`` list the reports used before: ``pnl_inr`` is
booked off the NETTED position's weighted-average price, while the Closed tab
walks a FIFO queue (sell matched to the oldest buy lot). Totals agree only for
fully-flat positions; per-row entry price + P&L attribution diverged. Rendering
the FIFO events makes every row identical to what the user sees in the app.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _fmt_dt(dt: datetime | None) -> str:
    # Identical to the format both reports.py files already use, so nothing
    # else on the PDF shifts.
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _naive(dt: datetime | None) -> datetime | None:
    """Strip tz so a tz-aware query bound and a tz-naive stored close time
    compare cleanly (everything here is UTC)."""
    return dt.replace(tzinfo=None) if (dt is not None and dt.tzinfo is not None) else dt


async def fifo_closed_rows(
    user_id: Any, from_date: datetime | None, to_date: datetime | None
) -> tuple[list[dict[str, Any]], float, float]:
    """Return ``(closed_rows, net_pnl, sum_brokerage)`` for the tradebook PDF.

    Rows are the FIFO closed-trade events whose CLOSE falls inside
    ``[from_date, to_date]`` (the FIFO basis is still computed over the user's
    full trade history, so a sell inside the window pairs against its real
    earlier buy). Each row carries every key both PDF builders read (superset;
    the user builder ignores the extra ``open_time`` / ``open_com``).

    ``net_pnl`` is Σ gross P&L — the SAME per-row figure the Closed tab shows
    (``realized_pnl = gross``, brokerage listed separately).
    """
    from app.services import position_service

    # limit huge → every event (the function itself caps input trades at 3000).
    events, _total = await position_service.list_closed_trade_events_fifo(
        user_id, skip=0, limit=1_000_000
    )

    lo = _naive(from_date)
    hi = _naive(to_date)
    rows: list[dict[str, Any]] = []
    net_pnl = 0.0
    sum_brokerage = 0.0

    for ev in events:
        closed_at = ev.get("closed_at")
        c = _naive(closed_at)
        if lo is not None and c is not None and c < lo:
            continue
        if hi is not None and c is not None and c > hi:
            continue

        inst = ev["instrument"]
        entry = float(ev["entry_price"])
        close = float(ev["close_price"])
        qty = float(ev["qty"])
        gross = float(ev["gross_pnl"])
        brk = float(ev["brokerage"])
        net_pnl += gross
        sum_brokerage += brk

        rows.append(
            {
                "time": _fmt_dt(closed_at),
                "type": "Close",
                "ticket_id": str(ev.get("_row_id") or "")[-8:],
                "script": inst.symbol,
                "amount": f"{qty:,.2f}",
                "type_detail": ev["opened_side"],
                "open_time": _fmt_dt(ev.get("opened_at")),
                "open_price": f"{entry:,.2f}",
                "close_price": f"{close:,.2f}",
                "dp_wd_aj": "",
                "brokerage": brk,
                "commission": brk,
                "open_com": brk,
                "total_pnl": gross,
                "comment": (
                    "Weekly settlement"
                    if ev.get("close_reason") == "WEEKLY_SETTLEMENT"
                    else ""
                ),
            }
        )

    return rows, round(net_pnl, 2), round(sum_brokerage, 2)
