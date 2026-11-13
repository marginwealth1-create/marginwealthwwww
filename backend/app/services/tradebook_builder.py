"""The Trade Book and the Billing statement — one builder each, both sides.

Two documents (handoff §4):

- **Trade Book** (`trade_rows`): one row per fill, newest first — what was
  executed — plus the legs a settlement books without writing a fill.
- **Billing statement** (`statement_payload`): the same trades put through the
  FIFO walk the app's Closed tab uses
  (``position_service.list_closed_trade_events_fifo``), each close priced at
  what the wallet booked, dressed as an account statement.

The client's and the admin's endpoints call these same functions, so both
sides render the same document; both read their dates through `window`
(whole IST days) and every time printed is IST. Read-only: nothing here
writes to the database.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.core.exceptions import ValidationFailedError
from app.models.order import Order, OrderStatus
from app.models.position import Position, PositionStatus
from app.models.position_settlement import PositionSettlement, PositionSettlementStatus
from app.models.trade import LIVE_FILL, Trade
from app.models.transaction import TransactionStatus, TransactionType, WalletTransaction
from app.services import position_service
from app.utils.decimal_utils import quantize_money, to_decimal
from app.utils.time_utils import UTC, end_of_day_ist, now_utc, start_of_day_ist, to_ist

_SETTLED_COMMENT = {"WEEKLY_SETTLEMENT": "Weekly settlement", "EXPIRY_SETTLED": "Expiry settlement"}


def _ist_day(v: str) -> date:
    v = v.strip()
    try:
        if len(v) == 10:
            return date.fromisoformat(v)
        # An instant from an older app build (browser-local midnight as UTC):
        # the IST day it falls on.
        return to_ist(datetime.fromisoformat(v.replace("Z", "+00:00"))).date()
    except ValueError as e:
        raise ValidationFailedError(f"Not a date: {v!r} (expected YYYY-MM-DD)") from e


def window(from_date: str | None, to_date: str | None) -> tuple[datetime | None, datetime | None]:
    """A statement's or trade book's window as UTC instants: whole IST days,
    from 00:00:00 IST of the first day to 23:59:59.999 IST of the last. The
    one parser both sides' reports go through."""
    lo = start_of_day_ist(_ist_day(from_date)).astimezone(UTC) if from_date else None
    hi = end_of_day_ist(_ist_day(to_date)).replace(microsecond=999000).astimezone(UTC) if to_date else None
    return lo, hi


def _fmt_dt(dt: datetime | None) -> str:
    return to_ist(dt).strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _naive(dt: datetime | None) -> datetime | None:
    """UTC wall clock without tzinfo, so stored and query times compare."""
    return dt.astimezone(UTC).replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def _between(lo: datetime | None, hi: datetime | None) -> Callable[[datetime | None], bool]:
    a, b = _naive(lo), _naive(hi)

    def inside(ts: datetime | None) -> bool:
        c = _naive(ts)
        return c is None or ((a is None or c >= a) and (b is None or c <= b))

    return inside


def _span(lo: datetime | None, hi: datetime | None) -> dict[str, Any] | None:
    q = {k: v for k, v in (("$gte", lo), ("$lte", hi)) if v is not None}
    return q or None


def _money(v: Decimal) -> float:
    return float(quantize_money(v))


def _side(p: Position) -> str:
    if p.opened_side is not None:
        return p.opened_side.value
    return "BUY" if float(p.quantity or 0) >= 0 else "SELL"


async def fifo_closed_rows(
    user_id: Any,
    from_date: datetime | None,
    to_date: datetime | None,
    *,
    lots: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], float, float]:
    """Return ``(closed_rows, gross_pnl, sum_brokerage)`` for the statement.

    Rows are the FIFO closed-trade events whose CLOSE falls inside the window
    (the basis is walked over the whole history, so a sell inside the window
    pairs against its real earlier buy). Each row bills only the brokerage
    legs charged inside the window: a close's opening leg charged before the
    window was billed by that earlier statement (on its Carry Forward). Gross
    is in the wallet's units — a USD row converts at the rate the wallet
    booked it. `lots` receives the book as it stood at `to_date`.
    """
    events, _total = await position_service.list_closed_trade_events_fifo(
        user_id, skip=0, limit=1_000_000, lots=lots, until=to_date
    )
    inside = _between(from_date, to_date)
    rows: list[dict[str, Any]] = []
    gross_sum = Decimal(0)
    brk_sum = Decimal(0)

    for ev in events:
        if not inside(ev.get("closed_at")):
            continue
        inst = ev["instrument"]
        qty = float(ev["qty"])
        # The walk's gross is native; brokerage is already in wallet units.
        gross_d = to_decimal(float(ev["gross_pnl"])) * to_decimal(float(ev.get("fx_rate") or 1))
        brk = sum(float(a) for ts, a in ev.get("legs", ()) if inside(ts))
        gross_sum += gross_d
        brk_sum += to_decimal(brk)
        rows.append(
            {
                "time": _fmt_dt(ev.get("closed_at")),
                "type": "Close",
                "ticket_id": str(ev.get("_row_id") or "")[-8:],
                "script": inst.symbol,
                "amount": f"{qty:,.2f}",
                "type_detail": ev["opened_side"],
                "open_time": _fmt_dt(ev.get("opened_at")),
                "open_price": f"{float(ev['entry_price']):,.2f}",
                "close_price": f"{float(ev['close_price']):,.2f}",
                "dp_wd_aj": "",
                "brokerage": brk,
                "commission": brk,
                "open_com": brk,
                "total_pnl": float(gross_d),
                "comment": _SETTLED_COMMENT.get(ev.get("close_reason") or "", ""),
            }
        )

    return rows, _money(gross_sum), _money(brk_sum)


async def trade_rows(
    user_id: Any,
    from_date: datetime | None,
    to_date: datetime | None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """The Trade Book: one row per fill still standing, newest first.

    A settlement closes a position without writing a fill, so its legs are
    synthesised from the position: the closing leg of the units it held, and
    for a weekly settlement that re-opened them (the settlement record's
    new_position_id) the re-open at the same price and instant. An expiry
    ends the contract, so it gets no re-open."""
    span = _span(from_date, to_date)
    q: dict[str, Any] = {"user_id": user_id, **LIVE_FILL}
    if span:
        q["executed_at"] = span
    found = Trade.find(q).sort("-executed_at")
    if limit:
        found = found.limit(limit)
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
        for t in await found.to_list()
    ]

    pq: dict[str, Any] = {
        "user_id": user_id,
        "status": PositionStatus.CLOSED.value,
        "close_reason": {"$in": list(position_service.SETTLE_REASONS)},
    }
    if span:
        pq["closed_at"] = span
    settled = [p for p in await Position.find(pq).to_list() if p.closed_at]
    weekly = [p.id for p in settled if p.close_reason == "WEEKLY_SETTLEMENT"]
    # Only a re-open that landed and still stands: a failed settlement left
    # the close alone, and a deleted successor took its units back.
    reopened = (
        {
            s.old_position_id: s.new_position_id
            for s in await PositionSettlement.find(
                {
                    "old_position_id": {"$in": weekly},
                    "status": PositionSettlementStatus.DONE.value,
                    "new_position_id": {"$ne": None},
                    "successor_deleted_at": None,
                }
            ).to_list()
        }
        if weekly
        else {}
    )
    for p in settled:
        # The units the settlement closed; a legacy expiry row zeroed them.
        qty = abs(float(p.quantity or 0)) or abs(float(p.opening_quantity or 0))
        if qty <= 0:
            continue
        side = _side(p)
        legs = [(f"settle_{p.id}", "SELL" if side == "BUY" else "BUY", p.id)]
        if p.id in reopened:
            legs.append((f"settle_open_{p.id}", side, reopened[p.id]))
        for row_id, action, order_id in legs:
            out.append(
                {
                    "id": row_id,
                    "trade_number": None,
                    "order_id": str(order_id),
                    "symbol": p.instrument.symbol,
                    "exchange": str(p.instrument.exchange),
                    "action": action,
                    "quantity": qty,
                    "price": str(p.ltp),
                    "value": str(quantize_money(to_decimal(p.ltp) * to_decimal(qty))),
                    "brokerage": "0",
                    "total_charges": "0",
                    "executed_at": p.closed_at,
                }
            )

    # Newest first; a re-open lists above the close it follows.
    out.sort(
        key=lambda r: (_naive(r["executed_at"]) or datetime.min, r["id"].startswith("settle_open_")),
        reverse=True,
    )
    return out[:limit] if limit else out


async def statement_payload(
    user: Any, from_date: datetime | None, to_date: datetime | None, *, brand: str = ""
) -> dict[str, Any]:
    """The Billing statement's payload for `user` over [from_date, to_date].

    Its tables add up: Σ closed-row brokerage + Σ Carry Forward commission is
    Financial Standings' Brokerage — every charge on a fill executed in the
    window — and Σ closed-row Gross is its Gross PnL (what the wallet booked
    as P&L in the window); Net = Gross − Brokerage. Balance is the ledger's
    balance at the end of the window."""
    uid = user.id
    span = _span(from_date, to_date)
    inside = _between(from_date, to_date)
    hi = _naive(to_date)

    lots: list[dict[str, Any]] = []
    closed_rows, gross, _ = await fifo_closed_rows(uid, from_date, to_date, lots=lots)

    # ── Funds: settled money moves only ────────────────────────────────
    money_types = [
        TransactionType.DEPOSIT,
        TransactionType.WITHDRAWAL,
        TransactionType.ADJUSTMENT,
        TransactionType.BONUS,
    ]
    tx_q: dict[str, Any] = {
        "user_id": uid,
        # A PENDING / FAILED request is not cash in or out.
        "status": TransactionStatus.COMPLETED.value,
        "transaction_type": {"$in": [t.value for t in money_types]},
    }
    if span:
        tx_q["created_at"] = span
    money_txs = await WalletTransaction.find(tx_q).sort("+created_at").to_list()
    totals = {t: Decimal(0) for t in money_types}
    for tx in money_txs:
        totals[tx.transaction_type] += to_decimal(tx.amount)
        closed_rows.append(
            {
                "time": _fmt_dt(tx.created_at),
                "type": "Money",
                "ticket_id": str(tx.id)[-8:] if tx.id else "",
                "script": "",
                "amount": "",
                "type_detail": tx.transaction_type.value.title(),
                "open_time": "",
                "open_price": "",
                "close_price": "",
                "dp_wd_aj": f"{to_decimal(tx.amount):,.2f}",
                "brokerage": 0,
                "commission": 0,
                "open_com": 0,
                "total_pnl": 0,
                "comment": tx.narration[:20] if tx.narration else "",
            }
        )
    closed_rows.sort(key=lambda r: r.get("time", ""))
    money_totals = {
        "credit_in": 0.0,
        "credit_out": 0.0,
        "deposit": _money(totals[TransactionType.DEPOSIT]),
        "withdraw": _money(totals[TransactionType.WITHDRAWAL]),
        "adjustment": _money(totals[TransactionType.ADJUSTMENT]),
        "bonus": _money(totals[TransactionType.BONUS]),
    }

    # ── Everything charged in the window ───────────────────────────────
    fill_q: dict[str, Any] = {"user_id": uid, **LIVE_FILL}
    if span:
        fill_q["executed_at"] = span
    brokerage = sum(
        (to_decimal(t.total_charges or t.brokerage or 0) for t in await Trade.find(fill_q).to_list()),
        Decimal(0),
    )

    # ── Carry Forward: what was held at the end of the window ──────────
    def held(p: Position) -> bool:
        if hi is None:
            return p.status == PositionStatus.OPEN
        opened, closed = _naive(p.opened_at), _naive(p.closed_at)
        if opened is not None and opened > hi:
            return False
        if position_service.in_reopen_gap(p, hi.replace(tzinfo=UTC)):
            return False
        return p.status == PositionStatus.OPEN if closed is None else closed > hi

    opened_deals: list[dict[str, Any]] = []
    for p in await Position.find(Position.user_id == uid).sort("+opened_at").to_list():
        if not held(p):
            continue
        side = _side(p)
        token, product = p.instrument.token, p.product_type.value
        mine = [f for f in lots if f["key"] in ((token, ""), (token, product)) and f["side"] == side]
        # Units held then (the book at to_date); the doc's own qty is today's.
        units = round(sum(float(f["qty"]) for f in mine), 8) if mine else abs(p.quantity)
        qty = to_decimal(units)
        # The opening legs of those units charged inside the window.
        commission = sum(
            (
                to_decimal(f.get("open_brk", 0.0) * float(f["qty"]) / f["original_qty"])
                for f in mine
                if f.get("original_qty") and inside(f["opened_at"])
            ),
            Decimal(0),
        )
        avg, ltp = to_decimal(p.avg_price), to_decimal(p.ltp)
        sl = to_decimal(p.stop_loss) if p.stop_loss else Decimal(0)
        tp = to_decimal(p.target) if p.target else Decimal(0)
        opened_deals.append(
            {
                "ticket_id": str(p.id)[-8:] if p.id else "",
                "time": _fmt_dt(p.opened_at or p.created_at),
                "type_detail": side,
                "amount": f"{qty:,.2f}",
                "script": p.instrument.symbol,
                "price": f"{avg:,.2f}",
                "sl": f"{sl:,.2f}" if sl else "",
                "tp": f"{tp:,.2f}" if tp else "",
                "current_price": f"{ltp:,.2f}",
                "commission": float(commission),
                "total_pnl": float(to_decimal(p.unrealized_pnl)),
                # Units × price: quantity is already in units, not lots.
                "value": float(qty * ltp),
                "comment": "",
            }
        )

    # ── Pending orders (as they stand now) ─────────────────────────────
    pending_orders = [
        {
            "order_id": o.order_number,
            "type": "SLTP" if o.order_type.value in ("SL", "SL_M") else o.order_type.value,
            "type_detail": "SL/TP" if o.order_type.value in ("SL", "SL_M") else o.action.value,
            "amount": f"{o.quantity:,.2f}",
            "script": o.instrument.symbol,
            "price": f"{to_decimal(o.price):,.2f}" if o.price else "",
            "sl": f"{to_decimal(o.bracket_stop_loss):,.2f}" if o.bracket_stop_loss else "",
            "tp": f"{to_decimal(o.bracket_target):,.2f}" if o.bracket_target else "",
            "time": _fmt_dt(o.created_at),
            "comment": "",
        }
        for o in await Order.find(
            {"user_id": uid, "status": {"$in": [OrderStatus.PENDING.value, OrderStatus.OPEN.value]}}
        ).to_list()
    ]

    # ── Financial Standings: four figures that reconcile with the above ─
    bal_q: dict[str, Any] = {"user_id": uid}
    if to_date is not None:
        bal_q["created_at"] = {"$lte": to_date}
    last = await WalletTransaction.find(bal_q).sort("-created_at", "-_id").first_or_none()
    financial = {
        "balance": _money(to_decimal(last.balance_after) if last else Decimal(0)),
        "gross_pnl": gross,
        "brokerage": _money(brokerage),
        "net_pnl": _money(to_decimal(gross) - brokerage),
    }

    return {
        "from_label": to_ist(from_date).date().isoformat() if from_date else "Beginning",
        "to_label": to_ist(to_date).date().isoformat() if to_date else "Now",
        "generated_at": _fmt_dt(now_utc()),
        "closed_transactions": closed_rows,
        "money_totals": money_totals,
        "opened_deals": opened_deals,
        "pending_orders": pending_orders,
        "financial": financial,
        "total_brokerage": financial["brokerage"],
        "admin_brand_name": brand,
    }
