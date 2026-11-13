"""User reports — P&L, tradebook, brokerage, tax, margin (JSON + PDF)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.dependencies import CurrentUser
from app.models.trade import Trade
from app.schemas.common import APIResponse
from app.services import report_pdf_service, tradebook_builder
from app.utils.time_utils import now_ist, now_utc


def _brand_slug() -> str:
    """Filename-safe brand prefix taken from APP_NAME.

    Downloaded report filenames were hard-coded to `marginwealth_…`, so every user
    on the current brand still received files stamped with the OLD one. Derive
    it from config instead: "MarginWealth" → "marginwealth".
    """
    raw = (settings.APP_NAME or "").strip().lower()
    slug = "".join(c if c.isalnum() else "_" for c in raw).strip("_")
    return slug.split("_")[0] or "report"

router = APIRouter(prefix="/reports", tags=["user-reports"])


# Shared payload builders so JSON + PDF endpoints stay byte-identical.
# (Avoids drift where the PDF says one number and the JSON another.)


async def _pnl_payload(user, from_date: datetime | None, to_date: datetime | None) -> dict:
    f = from_date or (now_utc() - timedelta(days=30))
    t = to_date or now_utc()
    trades = await Trade.find(
        Trade.user_id == user.id,
        Trade.executed_at >= f,
        Trade.executed_at <= t,
    ).sort("+executed_at").to_list()

    total_buy = 0.0
    total_sell = 0.0
    total_charges = 0.0
    # Canonical realised P&L = Σ Trade.pnl_inr — the matching engine sets it on
    # CLOSING fills only, already FIFO quantity-matched against the opening
    # fills and net of brokerage. The OLD formula (Σ sell_value − Σ buy_value)
    # was WRONG whenever buy_qty ≠ sell_qty (open / partially-open / reopened
    # positions): the unmatched opening leg's WHOLE notional surfaced as a
    # phantom loss. CL29519361 SENSEX (1240 bought / 1040 sold) read −₹1.55 Cr
    # instead of its real realised. Summing pnl_inr makes this report agree
    # with the admin Position-mgmt + Accounts cards (which use the same field),
    # so every page finally shows the same number.
    total_realized_net = 0.0
    # Count only the fills we actually aggregate (post the superseded skip),
    # so the "Trades" tile agrees with the money below instead of including
    # reopen-undone fills that contribute ₹0 to P&L / charges.
    matched_trades = 0
    by_symbol: dict[str, dict[str, Any]] = {}
    for tr in trades:
        # Skip closes an admin REOPEN/DELETE later undid — the admin cards net
        # these out via the reversal correction, so skipping them here keeps
        # the user report in lockstep with Position-mgmt / Accounts.
        if getattr(tr, "superseded_by_reopen", False):
            continue
        matched_trades += 1
        sym = tr.instrument.symbol
        v = float(str(tr.value))
        c = float(str(tr.total_charges))
        total_charges += c
        agg = by_symbol.setdefault(
            sym,
            {
                "symbol": sym,
                "buy_qty": 0,
                "sell_qty": 0,
                "buy_value": 0.0,
                "sell_value": 0.0,
                "charges": 0.0,
                "realized_net": 0.0,
                "trades": 0,
            },
        )
        agg["trades"] += 1
        if tr.action.value == "BUY":
            agg["buy_qty"] += tr.quantity
            agg["buy_value"] += v
            total_buy += v
        else:
            agg["sell_qty"] += tr.quantity
            agg["sell_value"] += v
            total_sell += v
        agg["charges"] += c
        if tr.pnl_inr is not None:
            p = float(str(tr.pnl_inr))
            agg["realized_net"] += p
            total_realized_net += p
        # Per-symbol NET P&L = canonical realised (net of brokerage).
        agg["pnl"] = round(agg["realized_net"], 2)

    # Net OPEN (unmatched) qty per symbol = buy_qty − sell_qty. A non-zero
    # value is exactly why a symbol's Buy/Sell VALUE columns don't tie out to
    # its Net P&L: realised P&L only counts matched (closed) quantity, while
    # the value columns are gross for the period. Positive = net long still
    # running; negative = net short still running (or its opening leg fell
    # before `from_date`). Surfacing it lets the UI explain the gap instead of
    # the client reading it as "missing money".
    for agg in by_symbol.values():
        # Units, fractions kept (int() read a 0.05 BTC position as 0).
        agg["open_qty"] = round((agg.get("buy_qty") or 0) - (agg.get("sell_qty") or 0), 8)

    net = round(total_realized_net, 2)                  # NET P&L (after brokerage)
    charges = round(total_charges, 2)
    realized = round(total_realized_net + charges, 2)   # GROSS P&L (before brokerage)
    by_symbol_rows = list(by_symbol.values())
    return {
        "from": f,
        "to": t,
        # Count of fills that actually contribute to this report (superseded
        # reopen-undone fills excluded) — keeps the tile consistent with money.
        "total_trades": matched_trades,
        # Legacy + PDF-builder field names (do NOT remove — report_pdf_service
        # reads these directly).
        "total_buy_value": round(total_buy, 2),
        "total_sell_value": round(total_sell, 2),
        "total_charges": charges,
        "net_pnl": net,
        "by_symbol": by_symbol_rows,
        # APK-facing field names — match the PnlReport TypeScript schema in
        # marginwealth-ind_apk/src/features/reports/api/reports.api.ts. Without
        # these the mobile P&L screen reads `undefined` for every total
        # and renders all ₹0.00 even when trades exist.
        "rows": [
            {
                "symbol": r["symbol"],
                "net_pnl": round(float(r.get("realized_net") or 0), 2),
                # Gross (before brokerage) = net realised + this symbol's charges.
                # NOT (sell_value − buy_value) — that was the phantom-notional bug
                # on unmatched buy/sell quantities.
                "realized_pnl": round(
                    float(r.get("realized_net") or 0) + float(r.get("charges") or 0),
                    2,
                ),
                "brokerage": round(float(r.get("charges") or 0), 2),
                # Real fill count for this symbol (was a bool→int that made
                # every symbol show "1 trade" regardless of activity).
                "trades": int(r.get("trades") or 0),
                # Net unmatched qty so the APK can flag a still-open position.
                "open_qty": int(r.get("open_qty") or 0),
            }
            for r in by_symbol_rows
        ],
        "total_realized": realized,
        "total_unrealized": 0.0,
        "total_brokerage": charges,
        "total_taxes": 0.0,
        "total_net": net,
    }


async def _brokerage_payload(
    user, from_date: datetime | None, to_date: datetime | None,
) -> dict:
    f = from_date or (now_utc() - timedelta(days=30))
    t = to_date or now_utc()
    trades = await Trade.find(
        Trade.user_id == user.id, Trade.executed_at >= f, Trade.executed_at <= t,
    ).to_list()
    totals = {"brokerage": 0.0, "total": 0.0}
    for tr in trades:
        # Skip reopen/delete-superseded fills — a deleted position's brokerage
        # must not linger in the Brokerage report either.
        if getattr(tr, "superseded_by_reopen", False):
            continue
        totals["brokerage"] += float(str(tr.brokerage))
        totals["total"] += float(str(tr.total_charges))
    return {
        "from": f,
        "to": t,
        "totals": {k: round(v, 2) for k, v in totals.items()},
        "trade_count": len(trades),
    }


async def _tax_payload(user) -> dict:
    """Simplified Indian tax-pnl bucketization. Real CG calc would consider
    FIFO holding period etc."""
    trades = await Trade.find(Trade.user_id == user.id).to_list()
    buckets = {"intraday_speculative": 0.0, "stcg": 0.0, "ltcg": 0.0, "fno": 0.0}
    for tr in trades:
        seg = (tr.instrument.segment or "").upper()
        v = float(str(tr.value))
        if "FUTURE" in seg or "OPTION" in seg:
            buckets["fno"] += v if tr.action.value == "SELL" else -v
        else:
            buckets["stcg"] += v if tr.action.value == "SELL" else -v
    return {"buckets": {k: round(v, 2) for k, v in buckets.items()}}


# ── JSON endpoints (existing contract) ───────────────────────────────


@router.get("/pnl", response_model=APIResponse[dict])
async def pnl_report(
    user: CurrentUser,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
):
    return APIResponse(data=await _pnl_payload(user, from_date, to_date))


@router.get("/tradebook", response_model=APIResponse[list])
async def tradebook(
    user: CurrentUser,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = Query(default=500, le=2000),
):
    """The Trade Book: one row per fill, newest first. Dates are whole IST
    days (YYYY-MM-DD)."""
    lo, hi = tradebook_builder.window(from_date, to_date)
    return APIResponse(data=await tradebook_builder.trade_rows(user.id, lo, hi, limit))


@router.get("/brokerage", response_model=APIResponse[dict])
async def brokerage_summary(
    user: CurrentUser,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
):
    return APIResponse(data=await _brokerage_payload(user, from_date, to_date))


@router.get("/tax", response_model=APIResponse[dict])
async def tax_pnl(user: CurrentUser):
    return APIResponse(data=await _tax_payload(user))


@router.get("/margin", response_model=APIResponse[dict])
async def margin_report(user: CurrentUser):
    from app.services import wallet_service
    s = await wallet_service.summary(user.id)
    return APIResponse(data=s)


# ── PDF endpoints ────────────────────────────────────────────────────
# Each PDF endpoint reuses the same payload-building helper as its JSON
# sibling, then hands the payload to report_pdf_service to render an
# in-memory PDF. Streamed back as application/pdf with a Content-Disposition
# that defaults the filename in both browsers and Expo's
# FileSystem.downloadAsync. No filesystem writes — payload lives in memory.


def _pdf_response(data: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([data]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(data)),
            # Expose Content-Disposition so the web JS download flow can
            # respect the server-suggested filename across CORS (browsers
            # hide non-simple headers from fetch by default).
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.get("/pnl/pdf")
async def pnl_report_pdf(
    user: CurrentUser,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
):
    payload = await _pnl_payload(user, from_date, to_date)
    pdf = report_pdf_service.build_pnl_pdf(user, payload)
    stamp = datetime.now().strftime("%Y%m%d")
    return _pdf_response(pdf, f"{_brand_slug()}_pnl_{stamp}.pdf")


@router.get("/tradebook/pdf")
async def tradebook_pdf(
    user: CurrentUser,
    from_date: str | None = None,
    to_date: str | None = None,
    # None → every fill in the window: a cut-off book printed no notice.
    limit: int | None = Query(default=None, le=2000),
):
    lo, hi = tradebook_builder.window(from_date, to_date)
    rows = await tradebook_builder.trade_rows(user.id, lo, hi, limit)
    pdf = report_pdf_service.build_tradebook_pdf(user, rows)
    stamp = now_ist().strftime("%Y%m%d")
    return _pdf_response(pdf, f"{_brand_slug()}_tradebook_{stamp}.pdf")


@router.get("/brokerage/pdf")
async def brokerage_pdf(
    user: CurrentUser,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
):
    payload = await _brokerage_payload(user, from_date, to_date)
    pdf = report_pdf_service.build_brokerage_pdf(user, payload)
    stamp = datetime.now().strftime("%Y%m%d")
    return _pdf_response(pdf, f"{_brand_slug()}_brokerage_{stamp}.pdf")


@router.get("/tax/pdf")
async def tax_pdf(user: CurrentUser):
    payload = await _tax_payload(user)
    pdf = report_pdf_service.build_tax_pdf(user, payload)
    stamp = datetime.now().strftime("%Y%m%d")
    return _pdf_response(pdf, f"{_brand_slug()}_tax_{stamp}.pdf")


@router.get("/margin/pdf")
async def margin_pdf(user: CurrentUser):
    from app.services import wallet_service
    s = await wallet_service.summary(user.id)
    if hasattr(s, "model_dump"):
        s = s.model_dump(mode="json")
    pdf = report_pdf_service.build_margin_pdf(user, s)
    stamp = datetime.now().strftime("%Y%m%d")
    return _pdf_response(pdf, f"{_brand_slug()}_margin_{stamp}.pdf")


# ── Billing statement PDF (the same statement the admin downloads) ───


@router.get("/tradebook/full-pdf")
async def tradebook_full_pdf(
    user: CurrentUser,
    from_date: str | None = None,
    to_date: str | None = None,
):
    """The client's own Billing statement. Dates are whole IST days
    (YYYY-MM-DD); none → the last 30 days; at most a month."""
    lo, hi = tradebook_builder.window(from_date, to_date)
    if lo is None and hi is None:
        today = now_ist().date()
        lo, hi = tradebook_builder.window(str(today - timedelta(days=30)), str(today))
    # Whole days: 1 Jul → 1 Aug is 31 days and 23:59:59.999, so `.days` is 31.
    if lo and hi and (hi - lo).days > 31:
        raise HTTPException(status_code=400, detail="Maximum date range is 1 month")

    payload = await tradebook_builder.statement_payload(user, lo, hi)
    pdf_bytes = report_pdf_service.build_full_tradebook_pdf(user, payload)
    stamp = now_ist().strftime("%Y%m%d")
    name = (getattr(user, "full_name", "") or "").strip().replace(" ", "_")
    code = getattr(user, "user_code", "") or "user"
    return _pdf_response(pdf_bytes, f"tradebook_{name}_{code}_{stamp}.pdf")
