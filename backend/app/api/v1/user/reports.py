"""User reports — P&L, tradebook, brokerage, tax, margin (JSON + PDF)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.dependencies import CurrentUser
from app.models.position import Position, PositionStatus
from app.models.position_settlement import PositionSettlement
from app.models.trade import LIVE_FILL, Trade
from app.models.transaction import TransactionType, WalletTransaction
from app.schemas.common import APIResponse
from app.services import report_pdf_service, tradebook_builder
from app.utils.decimal_utils import quantize_money, to_decimal
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


# P&L the wallet books without writing a fill: a weekly or expiry settlement,
# and the netting of opposite carried lots.
_NO_FILL_PNL = ("POSITION_SETTLEMENT", "EXPIRY_SETTLEMENT", "POSITION_DEFRAG")


async def _unfilled_pnl(uid: Any, f: datetime, t: datetime) -> list[tuple[str, Decimal]]:
    """(script, rupees) for every wallet row in [f, t] that booked P&L with no
    fill behind it — the PNL rows, plus the part of a loss past the cash that
    went to settlement outstanding — each attributed through the record it
    points at. A row an admin reopen / delete reversed is out, as their fills
    are: a weekly row is flagged superseded_by_reopen; an expiry row stands
    only while its position is still the one that expiry closed."""
    rows = await WalletTransaction.find(
        {
            "user_id": uid,
            "transaction_type": {
                "$in": [TransactionType.PNL.value, TransactionType.SETTLEMENT_OUTSTANDING_BOOKED.value]
            },
            "reference_type": {"$in": list(_NO_FILL_PNL)},
            "created_at": {"$gte": f, "$lte": t},
            "superseded_by_reopen": {"$ne": True},
        }
    ).to_list()

    def ids(*kinds: str) -> list[PydanticObjectId]:
        return list(
            {PydanticObjectId(r.reference_id) for r in rows if r.reference_type in kinds and r.reference_id}
        )

    weekly = {
        str(s.id): s.symbol
        for s in await PositionSettlement.find({"_id": {"$in": ids("POSITION_SETTLEMENT")}}).to_list()
    }
    pos = {
        str(p.id): p
        for p in await Position.find({"_id": {"$in": ids("EXPIRY_SETTLEMENT", "POSITION_DEFRAG")}}).to_list()
    }
    out: list[tuple[str, Decimal]] = []
    for r in rows:
        p = pos.get(r.reference_id or "")
        if r.reference_type == "POSITION_SETTLEMENT":
            sym = weekly.get(r.reference_id or "", "—")
        elif r.reference_type == "EXPIRY_SETTLEMENT":
            # Reopened (open, or closed again later) or deleted: reversed.
            if (
                p is None
                or p.status != PositionStatus.CLOSED
                or p.closed_at is None
                or abs((p.closed_at - r.created_at).total_seconds()) > 60
            ):
                continue
            sym = p.instrument.symbol
        else:
            # A netting that left the book flat deleted every lot; its
            # narration names the script.
            narrated = (r.narration or "").removeprefix("Auto-consolidate ").partition(": netted offset")[0]
            sym = p.instrument.symbol if p else narrated
        out.append((sym, to_decimal(r.amount)))
    return out


async def _pnl_payload(user, from_date: datetime | None, to_date: datetime | None) -> dict:
    """Realised P&L per script over the window.

    Gross is what the wallet booked as P&L: each close's raw P&L (pnl_inr is
    net of that close's own charge, so the charge goes back on) plus the P&L
    booked with no fill (`_unfilled_pnl`). Brokerage is every charge on a fill
    in the window, opening legs included. Net = Gross − Brokerage, per script
    and in total, so the script rows add up to the totals. Fills an admin
    reopen / delete took back are out (LIVE_FILL), as is their P&L."""
    f = from_date or (now_utc() - timedelta(days=30))
    t = to_date or now_utc()
    trades = (
        await Trade.find({"user_id": user.id, **LIVE_FILL, "executed_at": {"$gte": f, "$lte": t}})
        .sort("+executed_at")
        .to_list()
    )

    zero = Decimal(0)
    by_symbol: dict[str, dict[str, Any]] = {}

    def agg(sym: str) -> dict[str, Any]:
        return by_symbol.setdefault(
            sym,
            {
                "symbol": sym,
                "buy_qty": 0,
                "sell_qty": 0,
                **dict.fromkeys(("buy_value", "sell_value", "charges", "gross", "settlement"), zero),
                "trades": 0,
            },
        )

    for tr in trades:
        a = agg(tr.instrument.symbol)
        v = to_decimal(tr.value)
        c = to_decimal(tr.total_charges or tr.brokerage or 0)
        a["trades"] += 1
        if tr.action.value == "BUY":
            a["buy_qty"] += tr.quantity
            a["buy_value"] += v
        else:
            a["sell_qty"] += tr.quantity
            a["sell_value"] += v
        a["charges"] += c
        if tr.pnl_inr is not None:
            a["gross"] += to_decimal(tr.pnl_inr) + c
    for sym, amount in await _unfilled_pnl(user.id, f, t):
        a = agg(sym)
        a["gross"] += amount
        a["settlement"] += amount

    def money(d: Decimal) -> float:
        return float(quantize_money(d))

    by_symbol_rows = []
    for a in by_symbol.values():
        gross, settled = quantize_money(a.pop("gross")), a.pop("settlement")
        charges = quantize_money(a["charges"])
        net = gross - charges
        by_symbol_rows.append(
            {
                **a,
                "buy_value": money(a["buy_value"]),
                "sell_value": money(a["sell_value"]),
                "charges": money(charges),
                "settlement_pnl": money(settled),
                "gross_pnl": money(gross),
                "realized_net": money(net),
                "pnl": money(net),
                # Net OPEN (unmatched) units = buy − sell: why a script's
                # Buy/Sell VALUE columns don't tie out to its P&L (realised
                # P&L counts closed units only). Fractions kept.
                "open_qty": round(a["buy_qty"] - a["sell_qty"], 8),
            }
        )

    gross_total = sum((to_decimal(r["gross_pnl"]) for r in by_symbol_rows), zero)
    charges_total = sum((to_decimal(r["charges"]) for r in by_symbol_rows), zero)
    net_total = money(gross_total - charges_total)
    return {
        "from": f,
        "to": t,
        "total_trades": len(trades),
        # Legacy + PDF-builder field names (do NOT remove — report_pdf_service
        # reads these directly).
        "total_buy_value": money(sum((to_decimal(r["buy_value"]) for r in by_symbol_rows), zero)),
        "total_sell_value": money(sum((to_decimal(r["sell_value"]) for r in by_symbol_rows), zero)),
        "total_charges": money(charges_total),
        "net_pnl": net_total,
        "by_symbol": by_symbol_rows,
        # APK-facing field names — match the PnlReport TypeScript schema in
        # marginwealth-ind_apk/src/features/reports/api/reports.api.ts.
        "rows": [
            {
                "symbol": r["symbol"],
                "net_pnl": r["realized_net"],
                "realized_pnl": r["gross_pnl"],  # gross, before brokerage
                "brokerage": r["charges"],
                "settlement_pnl": r["settlement_pnl"],
                "trades": r["trades"],
                # Net unmatched qty so the APK can flag a still-open position.
                "open_qty": int(r["open_qty"]),
            }
            for r in by_symbol_rows
        ],
        "total_realized": money(gross_total),
        "total_unrealized": 0.0,
        "total_brokerage": money(charges_total),
        "total_taxes": 0.0,
        "total_net": net_total,
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
