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
from app.services import report_pdf_service
from app.utils.time_utils import now_utc


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
        agg["open_qty"] = int((agg.get("buy_qty") or 0) - (agg.get("sell_qty") or 0))

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


async def _tradebook_payload(
    user, from_date: datetime | None, to_date: datetime | None, limit: int,
) -> list[dict]:
    q: dict[str, Any] = {"user_id": user.id}
    if from_date or to_date:
        q["executed_at"] = {}
        if from_date:
            q["executed_at"]["$gte"] = from_date
        if to_date:
            q["executed_at"]["$lte"] = to_date
    rows = await Trade.find(q).sort("-executed_at").limit(limit).to_list()
    out = [
        {
            "id": str(t.id),
            "trade_number": t.trade_number,
            "order_id": str(t.order_id),
            "symbol": t.instrument.symbol,
            "exchange": str(t.instrument.exchange),
            "action": t.action.value,
            "quantity": t.quantity,
            "price": str(t.price),
            "value": str(t.value),
            "brokerage": str(t.brokerage),
            "total_charges": str(t.total_charges),
            "executed_at": t.executed_at,
        }
        for t in rows
    ]

    # Settlement-style closes (WEEKLY_SETTLEMENT / EXPIRY_SETTLED) write NO Trade
    # row — synthesize a closing-side entry so the tradebook is complete. These
    # showed on the admin's Position-based view but were missing from the user's
    # fill-based tradebook (operator: expiry-settled trades "tradebook me nahi").
    from app.models.position import Position, PositionStatus

    pq: dict[str, Any] = {
        "user_id": user.id,
        "status": PositionStatus.CLOSED.value,
        "close_reason": {"$in": ["WEEKLY_SETTLEMENT", "EXPIRY_SETTLED"]},
    }
    if from_date or to_date:
        pq["closed_at"] = {}
        if from_date:
            pq["closed_at"]["$gte"] = from_date
        if to_date:
            pq["closed_at"]["$lte"] = to_date
    async for p in Position.find(pq):
        if not p.closed_at:
            continue
        qty = abs(float(p.quantity or 0)) or abs(float(getattr(p, "opening_quantity", 0) or 0))
        if qty <= 0:
            continue
        opened_side = (
            p.opened_side.value
            if getattr(p, "opened_side", None) is not None
            else ("BUY" if float(p.quantity or 0) >= 0 else "SELL")
        )
        close_side = "SELL" if opened_side == "BUY" else "BUY"
        price = float(str(p.ltp))
        out.append({
            "id": f"settle_{p.id}",
            "trade_number": None,
            "order_id": str(p.id),
            "symbol": p.instrument.symbol,
            "exchange": str(p.instrument.exchange),
            "action": close_side,
            "quantity": qty,
            "price": str(p.ltp),
            "value": str(round(price * qty, 2)),
            "brokerage": "0",
            "total_charges": "0",
            "executed_at": p.closed_at,
        })

    out.sort(key=lambda r: r["executed_at"] or datetime.min, reverse=True)
    return out[:limit]


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
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = Query(default=500, le=2000),
):
    return APIResponse(data=await _tradebook_payload(user, from_date, to_date, limit))


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
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    limit: int = Query(default=500, le=2000),
):
    rows = await _tradebook_payload(user, from_date, to_date, limit)
    pdf = report_pdf_service.build_tradebook_pdf(user, rows)
    stamp = datetime.now().strftime("%Y%m%d")
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


# ── Full tradebook PDF (ARK Trader style, same as admin version) ───


def _d128(v: Any) -> float:
    if v is None:
        return 0.0
    return float(str(v))


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@router.get("/tradebook/full-pdf")
async def tradebook_full_pdf(
    user: CurrentUser,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
):
    from app.models.order import Order, OrderStatus
    from app.models.position import Position, PositionStatus
    from app.models.transaction import (
        TransactionStatus,
        TransactionType,
        WalletTransaction,
    )
    from app.models.wallet import Wallet

    now = now_utc()
    # Cap at a one-month span. Compare WHOLE days (`.days` floors the
    # timedelta) so the frontend's end-of-day padding on `to_date`
    # (…T23:59:59.999) doesn't tip a legitimate 31-calendar-day month
    # (e.g. 1 Jul → 1 Aug, or 1 Jan → 1 Feb) to 31d 23h59m and trip a raw
    # `> timedelta(days=31)` check — which rejected every 31-day-month
    # selection and blocked the download ("Maximum date range is 1 month").
    if from_date and to_date and (to_date - from_date).days > 31:
        raise HTTPException(status_code=400, detail="Maximum date range is 1 month")
    if not from_date and not to_date:
        from_date = now - timedelta(days=30)
        to_date = now

    q_time: dict[str, Any] = {}
    if from_date:
        q_time["$gte"] = from_date
    if to_date:
        q_time["$lte"] = to_date

    uid = user.id

    # 1. Trades
    trade_q: dict[str, Any] = {"user_id": uid}
    if q_time:
        trade_q["executed_at"] = q_time
    trades = await Trade.find(trade_q).sort("+executed_at").to_list()

    # 2. Money transactions
    tx_q: dict[str, Any] = {
        "user_id": uid,
        # Only SETTLED money moves — a PENDING / FAILED deposit or withdrawal
        # request must never show up as real cash-in/out on the statement
        # (that was the deposit/withdrawal mismatch clients reported).
        "status": TransactionStatus.COMPLETED.value,
        "transaction_type": {"$in": [
            TransactionType.DEPOSIT.value,
            TransactionType.WITHDRAWAL.value,
            TransactionType.ADJUSTMENT.value,
            TransactionType.BONUS.value,
        ]},
    }
    if q_time:
        tx_q["created_at"] = q_time
    money_txs = await WalletTransaction.find(tx_q).sort("+created_at").to_list()

    # Closed rows come from the SAME FIFO reconstruction the app's Closed tab
    # renders, so the PDF matches the Closed tab entry-by-entry (FIFO entry
    # price → close price → gross P&L) — not the netted-average pnl_inr, which
    # attributed per-row P&L differently and never lined up with the Closed tab.
    from app.services.tradebook_builder import fifo_closed_rows

    closed_rows, _fifo_net_pnl, sum_brokerage = await fifo_closed_rows(
        uid, from_date, to_date
    )

    for tx in money_txs:
        amt = _d128(tx.amount)
        closed_rows.append({
            "time": _fmt_dt(tx.created_at),
            "type": "Money",
            "ticket_id": str(tx.id)[-8:] if tx.id else "",
            "script": "",
            "amount": "",
            "type_detail": tx.transaction_type.value.title(),
            "open_price": "",
            "close_price": "",
            "dp_wd_aj": f"{amt:,.2f}",
            "brokerage": 0,
            "commission": 0,
            "total_pnl": 0,
            "comment": tx.narration[:20] if tx.narration else "",
        })
    closed_rows.sort(key=lambda r: r.get("time", ""))

    # 3. Money totals
    all_tx_q: dict[str, Any] = {
        "user_id": uid,
        # Same settled-only guard as above so the deposit / withdrawal /
        # adjustment / bonus totals reconcile with the actual wallet.
        "status": TransactionStatus.COMPLETED.value,
    }
    if q_time:
        all_tx_q["created_at"] = q_time
    all_txs = await WalletTransaction.find(all_tx_q).to_list()
    money_totals = {
        "credit_in": 0.0,
        "credit_out": 0.0,
        "deposit": sum(_d128(t.amount) for t in all_txs if t.transaction_type == TransactionType.DEPOSIT),
        "withdraw": sum(_d128(t.amount) for t in all_txs if t.transaction_type == TransactionType.WITHDRAWAL),
        "adjustment": sum(_d128(t.amount) for t in all_txs if t.transaction_type == TransactionType.ADJUSTMENT),
        "bonus": sum(_d128(t.amount) for t in all_txs if t.transaction_type == TransactionType.BONUS),
    }

    # 4. Open positions
    open_positions = await Position.find({"user_id": uid, "status": PositionStatus.OPEN.value}).to_list()
    opened_deals: list[dict[str, Any]] = []
    for p in open_positions:
        qty = abs(p.quantity)
        avg = _d128(p.avg_price)
        ltp = _d128(p.ltp)
        unrealized = _d128(p.unrealized_pnl)
        sl = _d128(p.stop_loss) if p.stop_loss else 0
        tp = _d128(p.target) if p.target else 0
        side = (p.opened_side.value if p.opened_side else ("Buy" if p.quantity > 0 else "Sell"))
        value = qty * ltp * (p.instrument.lot_size or 1)
        opened_deals.append({
            "ticket_id": str(p.id)[-8:] if p.id else "",
            "time": _fmt_dt(p.opened_at if hasattr(p, "opened_at") and p.opened_at else p.created_at),
            "type_detail": side,
            "amount": f"{qty:,.2f}",
            "script": p.instrument.symbol,
            "price": f"{avg:,.2f}",
            "sl": f"{sl:,.2f}" if sl else "",
            "tp": f"{tp:,.2f}" if tp else "",
            "current_price": f"{ltp:,.2f}",
            "commission": 0,
            "total_pnl": unrealized,
            "value": value,
        })

    # 5. Pending orders
    pending_db = await Order.find({
        "user_id": uid,
        "status": {"$in": [OrderStatus.PENDING.value, OrderStatus.OPEN.value]},
    }).to_list()
    pending_orders = [{
        "order_id": o.order_number,
        "type": "SLTP" if o.order_type.value in ("SL", "SL_M") else o.order_type.value,
        "type_detail": "SL/TP" if o.order_type.value in ("SL", "SL_M") else o.action.value,
        "amount": f"{o.quantity:,.2f}",
        "script": o.instrument.symbol,
        "price": f"{_d128(o.price):,.2f}" if o.price else "",
        "sl": f"{_d128(o.bracket_stop_loss):,.2f}" if o.bracket_stop_loss else "",
        "tp": f"{_d128(o.bracket_target):,.2f}" if o.bracket_target else "",
        "time": _fmt_dt(o.created_at),
    } for o in pending_db]

    # 6. Financial standings
    wallet = await Wallet.find_one({"user_id": uid})
    balance = _d128(wallet.available_balance) if wallet else 0
    used_margin = _d128(wallet.used_margin) if wallet else 0
    credit = _d128(wallet.credit_limit) if wallet else 0
    open_pnl = sum(d.get("total_pnl", 0) for d in opened_deals)
    equity = balance + used_margin + open_pnl
    free_margin = equity - used_margin
    margin_level = (equity / used_margin * 100) if used_margin > 0 else 0

    payload = {
        "from_label": from_date.strftime("%Y-%m-%d") if from_date else "Beginning",
        "to_label": to_date.strftime("%Y-%m-%d") if to_date else "Now",
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "closed_transactions": closed_rows,
        "money_totals": money_totals,
        "opened_deals": opened_deals,
        "pending_orders": pending_orders,
        "financial": {
            "balance": balance, "credit": credit,
            "equity": round(equity, 2), "total_pnl": round(open_pnl, 2),
            "used_margin": used_margin, "holding_margin": used_margin,
            "free_margin": round(free_margin, 2),
            "margin_level": f"{margin_level:.2f}%",
        },
        "total_brokerage": sum_brokerage,
        "admin_brand_name": "",
    }

    pdf_bytes = report_pdf_service.build_full_tradebook_pdf(user, payload)
    stamp = now.strftime("%Y%m%d")
    name = (getattr(user, "full_name", "") or "").strip().replace(" ", "_")
    code = getattr(user, "user_code", "") or "user"
    return _pdf_response(pdf_bytes, f"tradebook_{name}_{code}_{stamp}.pdf")
