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
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from bson import ObjectId

from app.api.v1.user.ledger import _LABELS, _sanitize_narration
from app.core.exceptions import ValidationFailedError
from app.models.position import Position, PositionStatus
from app.models.position_settlement import PositionSettlement, PositionSettlementStatus
from app.models.trade import LIVE_FILL, Trade
from app.models.transaction import CASH_TYPES, TransactionStatus, TransactionType, WalletTransaction
from app.models.wallet import Wallet
from app.services import position_service
from app.services.admin_settlement_service import ist_week_bounds
from app.services.market_data_service import is_usd_quoted_segment
from app.utils.decimal_utils import quantize_money, to_decimal
from app.utils.time_utils import UTC, end_of_day_ist, now_utc, start_of_day_ist, to_ist

_SETTLED_COMMENT = {"WEEKLY_SETTLEMENT": "Weekly settlement", "EXPIRY_SETTLED": "Expiry settlement"}
# The statement lists derivatives with their cash market: one NSE table, not
# an NSE and an NFO one.
_FOLD = {"NFO": "NSE", "BFO": "BSE"}


def exchange_label(exchange: Any) -> str:
    e = str(exchange or "")
    return _FOLD.get(e, e)


def pair_legs(side: str, entry: Any, close: Any, opened_at: Any, closed_at: Any) -> tuple[Any, Any, Any, Any]:
    """One closed trade's legs as (buy_rate, sell_rate, buy_time, sell_time).

    A long bought at the entry and sold at the close. A short SOLD at the
    entry — first — and bought back at the close, so its sell time is the
    EARLIER of the two."""
    if side == "BUY":
        return entry, close, opened_at, closed_at
    return close, entry, closed_at, opened_at


def script_wise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (exchange, script): units closed, average buy/sell rate as turnover
    over quantity (weighted, not a mean of rates), the turnovers, first to
    last trade time and the P&L. Blank times are skipped: "" sorts before
    every real time."""
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        a = acc.setdefault(
            (r["exchange"], r["script"]),
            {
                "exchange": r["exchange"],
                "script": r["script"],
                "usd": r.get("usd", False),
                **dict.fromkeys(
                    ("qty", "buy_turnover", "sell_turnover", "brokerage", "gross_pnl"), Decimal(0)
                ),
                "times": [],
            },
        )
        q = to_decimal(r["qty"])
        a["qty"] += q
        a["buy_turnover"] += to_decimal(r["buy_rate"]) * q
        a["sell_turnover"] += to_decimal(r["sell_rate"]) * q
        a["brokerage"] += to_decimal(r["brokerage"])
        a["gross_pnl"] += to_decimal(r["gross_pnl"])
        a["times"] += [t for t in (r["buy_time"], r["sell_time"]) if t]
    out = []
    for a in acc.values():
        times, q = a.pop("times"), a["qty"]
        out.append(
            {
                **a,
                "avg_buy": a["buy_turnover"] / q if q else Decimal(0),
                "avg_sell": a["sell_turnover"] / q if q else Decimal(0),
                "net_pnl": a["gross_pnl"] - a["brokerage"],
                "first_time": min(times, default=""),
                "last_time": max(times, default=""),
            }
        )
    return out


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


def periods(now: datetime | None = None) -> dict[str, tuple[datetime, datetime]]:
    """The windows every Today / This week / Last week P&L reads `booked`
    over: the IST day, and the IST week Monday 00:00 -> Sunday 23:59:59.999
    (the settlements' own ist_week_bounds)."""
    now = now or now_utc()
    day = to_ist(now).date().isoformat()
    lo, hi = window(day, day)
    return {
        "today": (lo, hi),  # type: ignore[dict-item]  # both dates given
        "week": ist_week_bounds(now),
        "last_week": ist_week_bounds(now - timedelta(days=7)),
    }


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


def _paise(exact: list[Decimal]) -> list[Decimal]:
    """Each amount to the paisa with the rounding carried forward, so the
    printed amounts add up to their rounded total (a close split over three
    lots printed 40.38 thrice under a booked 121.15)."""
    out, cum, done = [], Decimal(0), Decimal(0)
    for v in exact:
        cum += v
        out.append(quantize_money(cum) - done)
        done += out[-1]
    return out


def _side(p: Position) -> str:
    if p.opened_side is not None:
        return p.opened_side.value
    return "BUY" if float(p.quantity or 0) >= 0 else "SELL"


def _held(p: Position, at: datetime) -> bool:
    """Was `p` open at instant `at` (naive UTC)?"""
    opened, closed = _naive(p.opened_at), _naive(p.closed_at)
    if opened is not None and opened > at:
        return False
    if position_service.in_reopen_gap(p, at.replace(tzinfo=UTC)):
        return False
    return p.status == PositionStatus.OPEN if closed is None else closed > at


def _units(p: Position) -> Decimal:
    """BF/CF's units when the walk holds no lot for `p` (one fallback for
    both, so this week's BF is last week's CF): the quantity, kept on a
    settlement close and on an open position, else the peak a close zeroed."""
    return abs(to_decimal(p.quantity or 0)) or abs(to_decimal(p.opening_quantity or 0))


def _deal(
    p: Position, book: list[dict[str, Any]], fallback: Decimal
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A Brought / Carry Forward row for `p` off `book`, the walk's lots as
    they stood at the boundary: the units held and the engine's average
    then, so this week's BF is last week's CF and a later add rewrites
    neither (opening_quantity is the lifecycle's peak, avg_price today's).
    Also returns the lots it read."""
    side = _side(p)
    token, product = p.instrument.token, p.product_type.value
    mine = [f for f in book if f["key"] in ((token, ""), (token, product)) and f["side"] == side]
    avg = mine[0].get("avg") if mine else None
    row = {
        "ticket_id": str(p.id)[-8:] if p.id else "",
        "time": _fmt_dt(p.opened_at or p.created_at),
        "exchange": exchange_label(p.instrument.exchange),
        "script": p.instrument.symbol,
        "usd": is_usd_quoted_segment(p.instrument.segment),
        "side": side,
        # ponytail: a position with no lots in the walk (legacy, or admin-made
        # with no fill) falls back to its doc as it is today.
        "qty": round(sum(float(f["qty"]) for f in mine), 8) if mine else float(fallback),
        "price": float(to_decimal(avg if avg is not None else p.avg_price)),
    }
    return row, mine


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

    # Oldest close first (stable: a close's sub-rows keep their order).
    for ev in sorted(
        (e for e in events if inside(e.get("closed_at"))),
        key=lambda e: _naive(e["closed_at"]) or datetime.min,
    ):
        inst = ev["instrument"]
        # The walk's gross is native; brokerage is already in wallet units.
        gross_d = to_decimal(float(ev["gross_pnl"])) * to_decimal(float(ev.get("fx_rate") or 1))
        brk = to_decimal(sum(float(a) for ts, a in ev.get("legs", ()) if inside(ts)))
        gross_sum += gross_d
        brk_sum += brk
        buy, sell, buy_at, sell_at = pair_legs(
            ev["opened_side"], ev["entry_price"], ev["close_price"], ev.get("opened_at"), ev.get("closed_at")
        )
        rows.append(
            {
                "ticket_id": str(ev.get("_row_id") or "")[-8:],
                "exchange": exchange_label(inst.exchange),
                "script": inst.symbol,
                "usd": is_usd_quoted_segment(inst.segment),
                "side": ev["opened_side"],
                "qty": float(ev["qty"]),
                "buy_rate": float(buy),
                "sell_rate": float(sell),
                "buy_time": _fmt_dt(buy_at),
                "sell_time": _fmt_dt(sell_at),
                "brokerage": float(brk),
                "gross_pnl": float(gross_d),
                "net_pnl": float(gross_d - brk),
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
            "usd": is_usd_quoted_segment(t.instrument.segment),
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
                    "usd": is_usd_quoted_segment(p.instrument.segment),
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


async def booked(
    user_ids: list[Any], from_date: datetime | None, to_date: datetime | None
) -> dict[Any, tuple[Decimal, Decimal]]:
    """Each user's Billing statement Gross PnL and Brokerage over the window,
    for many users in two queries (the accounts dashboard and every Today /
    This week / Last week P&L card, over `periods`):
    every close in the window at what the wallet booked on it, weekly and
    expiry settlements at their settle price, undone fills out; brokerage is
    every charge on a fill executed in the window. The walk prices a close
    by the same stamp (_booked_gross), so this is the statement's Gross
    without walking each history."""
    span = _span(from_date, to_date)
    out: dict[Any, list[Decimal]] = {}
    fill_q: dict[str, Any] = {"user_id": {"$in": user_ids}, **LIVE_FILL}
    if span:
        fill_q["executed_at"] = span
    for t in await Trade.find(fill_q).to_list():
        charge = to_decimal(t.total_charges or t.brokerage or 0)
        acc = out.setdefault(t.user_id, [Decimal(0), Decimal(0)])
        acc[1] += charge
        if t.pnl_inr is not None:  # a close: pnl_inr is net of its own charge
            acc[0] += to_decimal(t.pnl_inr) + charge
    pos_q: dict[str, Any] = {
        "user_id": {"$in": user_ids},
        "status": PositionStatus.CLOSED.value,
        "close_reason": {"$in": ["WEEKLY_SETTLEMENT", "EXPIRY_SETTLED"]},
    }
    if span:
        pos_q["closed_at"] = span
    for p in await Position.find(pos_q).to_list():
        units = abs(to_decimal(p.quantity or 0)) or abs(to_decimal(p.opening_quantity or 0))
        sign = 1 if _side(p) == "BUY" else -1
        move = (to_decimal(p.ltp) - to_decimal(p.avg_price)) * units * sign
        out.setdefault(p.user_id, [Decimal(0), Decimal(0)])[0] += move * position_service.position_fx_rate(p)
    return {uid: (quantize_money(g), quantize_money(b)) for uid, (g, b) in out.items()}


async def net(user_ids: list[Any], from_date: datetime | None, to_date: datetime | None) -> Decimal:
    """Σ the users' Billing statement Net P&L (Gross − Brokerage) over the window."""
    return sum((g - b for g, b in (await booked(user_ids, from_date, to_date)).values()), Decimal(0))


# What a live fill's or a weekly settlement's own ledger rows are: Net PnL
# holds them (a stop-out's due is the rest of its loss).
_BILLED = frozenset(
    {
        TransactionType.PNL,
        TransactionType.CHARGES,
        TransactionType.BROKERAGE,
        TransactionType.TRADE,
        TransactionType.SETTLEMENT_OUTSTANDING_BOOKED,
    }
)


def _worth_change(t: WalletTransaction) -> Decimal:
    """What a COMPLETED ledger row did to cash less settlement outstanding.
    Its amount, except: a due booked off a negative wallet (an admin
    approving a SettlementRequest, the boot clamp) only renames that
    negative cash as owed; a recovery out of cash pays the due with it; a
    recovery with a positive amount (an admin delete / reopen unbooking a
    stop-out's due) writes off what was owed, with no cash."""
    amount = to_decimal(t.amount)
    if t.transaction_type == TransactionType.SETTLEMENT_OUTSTANDING_BOOKED:
        clamp = t.reference_type == "SettlementRequest" or (t.narration or "").startswith("Migration:")
        return Decimal(0) if clamp else amount
    if t.transaction_type == TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY:
        return max(amount, Decimal(0))
    # ponytail: a loss a bonus credit absorbs (BONUSES_ENABLED, off on prod)
    # writes no row for that part; give the statement a bonus line if it goes live.
    return amount


async def statement_payload(
    user: Any, from_date: datetime | None, to_date: datetime | None, *, brand: str = ""
) -> dict[str, Any]:
    """The Billing statement's payload for `user` over [from_date, to_date],
    in the operator's order (handoff §4.1): Brought Forward, Funds, the closed
    trades (the PDF lists them per exchange), Script-wise P&L, Carry Forward
    and Financial Standings.

    Its tables add up: Σ closed-row brokerage + Σ Carry Forward commission is
    Financial Standings' Brokerage — every charge on a fill executed in the
    window — and Σ closed-row Gross is its Gross PnL (what the wallet booked
    as P&L in the window); Net = Gross − Brokerage. Opening and Balance are
    what the account is worth at either edge (`_worth_change`), so Opening +
    Funds + Net PnL + Reversals & Adjustments is the Balance. Nothing here is
    a live price."""
    uid = user.id
    span = _span(from_date, to_date)
    inside = _between(from_date, to_date)

    lots: list[dict[str, Any]] = []
    closed_rows, _, _ = await fifo_closed_rows(uid, from_date, to_date, lots=lots)

    # ── Funds: settled money moves.
    # CASH_TYPES: an admin's PROMO / PENALTY is Add / Deduct Fund here too.
    tx_q: dict[str, Any] = {
        "user_id": uid,
        # A PENDING / FAILED request is not cash in or out.
        "status": TransactionStatus.COMPLETED.value,
        "transaction_type": {"$in": [t.value for t in CASH_TYPES]},
    }
    if span:
        tx_q["created_at"] = span
    funds_rows = [
        {
            "time": _fmt_dt(tx.created_at),
            "type": tx.transaction_type.value.title(),
            "amount": _money(to_decimal(tx.amount)),
            "comment": tx.narration or "",
        }
        for tx in await WalletTransaction.find(tx_q).sort("+created_at", "+_id").to_list()
    ]
    # Opening and Balance: what the account is worth at each edge — cash,
    # margin in open positions included, less what the client owes — the
    # wallet's worth now less every row after the edge. Not a row's
    # balance_before / balance_after: that is the free cash (blocking margin
    # writes no row), so a window that opened or closed with a position open
    # read the margin as missing.
    wallet = await Wallet.find_one(Wallet.user_id == uid)
    worth = (
        to_decimal(wallet.available_balance)
        + to_decimal(wallet.used_margin)
        - to_decimal(wallet.settlement_outstanding)
        if wallet
        else Decimal(0)
    )
    # One read of the rows for both edges, so a write landing mid-statement
    # moves Opening and Balance alike.
    lower = min(from_date, to_date) if from_date and to_date else from_date
    ledger = await WalletTransaction.find(
        {
            "user_id": uid,
            "status": TransactionStatus.COMPLETED.value,
            **({"created_at": {"$gte": lower}} if lower else {}),
        }
    ).to_list()
    since, until = _between(from_date, None), _between(None, to_date)
    opening = worth - sum((_worth_change(t) for t in ledger if since(t.created_at)), Decimal(0))
    balance = worth - sum((_worth_change(t) for t in ledger if not until(t.created_at)), Decimal(0))

    # ── Reversals & Adjustments: what moved the account in the window that is
    # neither Funds nor the trades' Net PnL — an admin undo's reversal
    # (whichever week the trade was), the P&L and charges of a fill since
    # undone, a sharing payout … — so Opening + Funds + Net PnL + these is
    # the Balance. Net PnL already holds a live fill's P&L and charges and a
    # weekly settlement's P&L. Typed and worded as the client's own ledger.
    moved = [
        t
        for t in ledger
        if inside(t.created_at) and t.transaction_type not in CASH_TYPES and _worth_change(t)
    ]
    orders = {
        t.reference_id
        for t in moved
        if t.reference_type == "ORDER" and ObjectId.is_valid(t.reference_id or "")
    }
    live = (
        {
            str(f.order_id)
            for f in await Trade.find(
                {"user_id": uid, "order_id": {"$in": [ObjectId(o) for o in orders]}, **LIVE_FILL}
            ).to_list()
        }
        if orders
        else set()
    )

    def priced(t: WalletTransaction) -> bool:
        if t.transaction_type not in _BILLED:
            return False
        return t.reference_type == "POSITION_SETTLEMENT" or (
            t.reference_type == "ORDER" and t.reference_id in live
        )

    adjustments = [
        {
            "time": _fmt_dt(t.created_at),
            "type": _LABELS.get(t.transaction_type, t.transaction_type.value.title()),
            "amount": _money(_worth_change(t)),
            "comment": _sanitize_narration(t)
            + (" (trade since undone)" if t.reference_type == "ORDER" else ""),
        }
        for t in sorted(moved, key=lambda t: (_naive(t.created_at), t.id))
        if not priced(t)
    ]

    # ── Everything charged in the window ───────────────────────────────
    fill_q: dict[str, Any] = {"user_id": uid, **LIVE_FILL}
    if span:
        fill_q["executed_at"] = span
    brokerage = sum(
        (to_decimal(t.total_charges or t.brokerage or 0) for t in await Trade.find(fill_q).to_list()),
        Decimal(0),
    )

    positions = await Position.find(Position.user_id == uid).sort("+opened_at").to_list()

    # ── Brought / Carry Forward: which positions off the Position docs, their
    # units and price off the walk's book at the boundary (`_deal`).
    # Brought Forward: open on the window's first instant, even if it closed
    # inside it.
    brought_forward = []
    day_one = _naive(from_date - timedelta(microseconds=1)) if from_date else None
    if day_one is not None:
        bf_lots: list[dict[str, Any]] = []
        # ponytail: a second walk of the whole history for the book at
        # day one; snapshot it in the one walk if statements get slow.
        await position_service.list_closed_trade_events_fifo(
            uid, skip=0, limit=1, lots=bf_lots, until=day_one
        )
        for p in positions:
            if _units(p) and _held(p, day_one):
                brought_forward.append(_deal(p, bf_lots, _units(p))[0])

    hi = _naive(to_date)
    carry_forward: list[dict[str, Any]] = []
    for p in positions:
        if not (p.status == PositionStatus.OPEN if hi is None else _held(p, hi)):
            continue
        row, mine = _deal(p, lots, _units(p))
        # The opening legs of those units charged inside the window.
        commission = sum(
            (
                to_decimal(f.get("open_brk", 0.0) * float(f["qty"]) / f["original_qty"])
                for f in mine
                if f.get("original_qty") and inside(f["opened_at"])
            ),
            Decimal(0),
        )
        # Units × price: quantity is already in units, not lots.
        value = to_decimal(row["qty"]) * to_decimal(row["price"])
        carry_forward.append({**row, "commission": commission, "value": float(value)})

    # ── To the paisa, rounding carried forward, so the printed rows add up
    # to the printed totals and to Financial Standings. Brokerage runs on
    # through Carry Forward: their sum is everything charged in the window.
    grossed = _paise([to_decimal(r["gross_pnl"]) for r in closed_rows])
    billed = _paise(
        [to_decimal(r["brokerage"]) for r in closed_rows] + [d["commission"] for d in carry_forward]
    )
    for r, g, b in zip(closed_rows, grossed, billed, strict=False):
        r.update(gross_pnl=float(g), brokerage=float(b), net_pnl=float(g - b))
    for d, b in zip(carry_forward, billed[len(closed_rows) :], strict=True):
        d["commission"] = float(b)
    gross = sum(grossed, Decimal(0))

    # ── Financial Standings: four figures that reconcile with the above ─
    financial = {
        "balance": _money(balance),
        "gross_pnl": _money(gross),
        "brokerage": _money(brokerage),
        "net_pnl": _money(gross - brokerage),
    }

    return {
        "from_label": to_ist(from_date).date().isoformat() if from_date else "Beginning",
        "to_label": to_ist(to_date).date().isoformat() if to_date else "Now",
        "generated_at": _fmt_dt(now_utc()),
        "brought_forward": brought_forward,
        "funds": {"opening_balance": _money(opening), "rows": funds_rows},
        "adjustments": adjustments,
        "closed_transactions": closed_rows,
        "script_wise": script_wise(closed_rows),
        "carry_forward": carry_forward,
        "financial": financial,
        "admin_brand_name": brand,
    }
