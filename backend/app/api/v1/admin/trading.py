"""Admin trading views: orders, positions, trades, holdings, instruments."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

from beanie import PydanticObjectId
from bson import Decimal128
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.dependencies import (
    CurrentAdmin,
    SuperAdmin,
    assert_user_in_scope,
    require_perm,
    scoped_user_ids,
    super_admin_direct_user_ids,
)
from app.core.redis_client import publish
from app.models._base import OrderAction, OrderType
from app.models.audit_log import AuditAction
from app.models.holding import Holding
from app.models.order import Order, order_reason_code
from app.models.position import Position, PositionStatus
from app.models.trade import Trade
from app.models.user import User, UserRole
from app.schemas.common import APIResponse
from app.services import market_data_service, order_service, position_service
from app.services.audit_service import log_event


async def _publish_position_event(
    user_id: PydanticObjectId,
    event: str,
    position: Position | None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Push a position-update message to the user's Redis pub/sub channel so
    open browsers refresh their positions strip without a page reload."""
    try:
        payload: dict[str, Any] = {"type": "position_update", "event": event}
        if position is not None:
            payload["position"] = {
                "id": str(position.id),
                "symbol": position.instrument.symbol,
                "instrument_token": position.instrument.token,
                "segment_type": position.segment_type,
                "product_type": position.product_type.value,
                "quantity": position.quantity,
                "avg_price": str(position.avg_price),
                "stop_loss": str(position.stop_loss) if position.stop_loss is not None else None,
                "target": str(position.target) if position.target is not None else None,
                "status": position.status.value,
                "opened_at": position.opened_at.isoformat() if position.opened_at else None,
                "closed_at": position.closed_at.isoformat() if position.closed_at else None,
            }
        if extra:
            payload.update(extra)
        await publish(f"user:{user_id}:positions", payload)
        # Also fan out to the admin dashboard's WS so every admin / broker
        # currently watching Position Management refreshes the affected row
        # without hitting F5. Cheap one-line fanout — same payload, one
        # extra channel.
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "position_update",
            {"event": event, "user_id": str(user_id), "position_id": str(position.id) if position else None},
        )
    except Exception:  # pragma: no cover — never fail the API call on a publish error
        pass

router = APIRouter(tags=["admin-trading"])


# ── Orders ──────────────────────────────────────────────────────────
@router.get("/orders", response_model=APIResponse[dict])
async def list_orders(
    admin: CurrentAdmin,
    status: str | None = None,
    statuses: str | None = None,
    sl_tp: bool = False,
    user_id: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    _: None = Depends(require_perm("trading_view", "read")),
):
    """Admin orders monitor — paginated.

    Filter params:
      • `status`    — single status filter (back-compat with old UI)
      • `statuses`  — CSV of statuses; drives the new tab UI which
                      bundles "Pending" = PENDING + OPEN + PARTIAL,
                      "Executed" = EXECUTED, "Rejected" = REJECTED,
                      so a single tab maps to multiple wire statuses.
      • `sl_tp`     — when true, returns OPEN POSITIONS that carry a
                      stop_loss or target the user has set (the operator
                      reads this tab as "kis client ne SL/TP lagaya
                      hai"). The shape is normalised to look like an
                      order row so the same DataTable renders both.
      • `user_id`   — scope to one user (used by user-detail deep links).
      • `q`         — free-text search across user full_name, user_code,
                      and instrument symbol (case-insensitive). Powers
                      the Orders monitor's search box so the operator
                      can find rows across every tab without picking a
                      user first. Resolves matching user_ids in one
                      lookup, then applies an `$or` against user_id +
                      symbol so a query like "AJAY" matches both his
                      orders AND any order on instrument "AJAY...".
    """
    # Resolve free-text search to a list of matching user_ids + a
    # symbol regex so both the orders branch and the SL/TP positions
    # branch can splice the same `$or` filter into their queries.
    # Skipped when the operator passed `user_id` directly (a deep-link
    # from the user-detail page already pins the scope) or when `q` is
    # too short to be useful (< 2 chars would match almost everything
    # and just hammer Mongo with a regex scan).
    search_user_ids: list[PydanticObjectId] | None = None
    search_symbol_regex: dict[str, Any] | None = None
    if q and len(q.strip()) >= 2 and not user_id:
        needle = re.escape(q.strip())
        regex = {"$regex": needle, "$options": "i"}
        # Scope the user search to the admin's visible set so a
        # sub-admin's search can't leak rows from other brokers'
        # books. `scoped_user_ids` returns None for unrestricted
        # super-admins.
        scope_ids = await scoped_user_ids(admin)
        u_filter: dict[str, Any] = {
            "$or": [
                {"full_name": regex},
                {"user_code": regex},
            ]
        }
        if scope_ids is not None:
            u_filter["_id"] = {"$in": scope_ids}
        matched = await User.find(u_filter).to_list()
        search_user_ids = [m.id for m in matched]
        search_symbol_regex = regex
    # SL/TP tab — sourced from POSITIONS (not orders). User-side SL/TP is
    # set on the Position document via the per-position edit endpoint
    # AFTER the entry fills, so it never lives on an Order row. Filtering
    # orders for bracket_* fields only ever shows orders placed WITH
    # bracket legs at order time, which is a tiny subset and was always
    # empty for this operator's flow. Operator-flagged 21-May:
    # "SL/TP ka data nahi aa raha".
    if sl_tp:
        # The status + SL/TP `$or` is the structural filter; the
        # free-text search adds a SECOND `$or` for user/symbol matches.
        # Mongo `$and`-merges multiple top-level `$or` clauses, so we
        # wrap both inside one `$and` to keep them composable instead
        # of one clobbering the other.
        pq_and: list[dict[str, Any]] = [
            {
                "$or": [
                    {"stop_loss": {"$ne": None}},
                    {"target": {"$ne": None}},
                ]
            }
        ]
        pq: dict[str, Any] = {
            "status": PositionStatus.OPEN.value,
            "$or": [{"is_demo": {"$ne": True}}, {"is_demo": {"$exists": False}}],
        }
        if user_id:
            await assert_user_in_scope(admin, user_id)
            pq["user_id"] = PydanticObjectId(user_id)
        else:
            scope = await scoped_user_ids(admin)
            if scope is not None:
                if not scope:
                    return APIResponse(
                        data={
                            "items": [],
                            "meta": {"page": page, "page_size": page_size, "total": 0, "total_pages": 0},
                        }
                    )
                pq["user_id"] = {"$in": scope}
        # Free-text search splice: match user_id ∈ matched_users OR
        # the position's symbol matches the regex. When `q` is set but
        # nothing matched on either axis, we still leave the empty
        # user_id list in so the resulting `$in: []` returns zero rows
        # (correct — no matches found).
        if search_user_ids is not None and search_symbol_regex is not None:
            pq_and.append(
                {
                    "$or": [
                        {"user_id": {"$in": search_user_ids}},
                        {"instrument.symbol": search_symbol_regex},
                    ]
                }
            )
        pq["$and"] = pq_and
        pos_total = await Position.find(pq).count()
        pos_rows = (
            await Position.find(pq)
            .sort("-opened_at")
            .skip((page - 1) * page_size)
            .limit(page_size)
            .to_list()
        )
        user_ids_p = list({p.user_id for p in pos_rows})
        users_p = await User.find({"_id": {"$in": user_ids_p}}).to_list() if user_ids_p else []
        user_map_p = {
            str(u.id): {"user_code": u.user_code, "full_name": u.full_name} for u in users_p
        }
        return APIResponse(
            data={
                "items": [
                    {
                        "id": str(p.id),
                        # Position rows don't have an order_number — the UI
                        # tab doesn't render this column anymore, but other
                        # consumers may peek at it, so synthesise from id.
                        "order_number": f"POS-{str(p.id)[-8:].upper()}",
                        "user_id": str(p.user_id),
                        "user_code": user_map_p.get(str(p.user_id), {}).get("user_code"),
                        "user_name": user_map_p.get(str(p.user_id), {}).get("full_name"),
                        "symbol": p.instrument.symbol,
                        "exchange": str(p.instrument.exchange),
                        "segment": p.instrument.segment,
                        "token": p.instrument.token,
                        "instrument_token": p.instrument.token,
                        "action": (
                            p.opened_side.value
                            if p.opened_side is not None
                            else ("BUY" if p.quantity >= 0 else "SELL")
                        ),
                        # Map product_type into the order_type column so the
                        # generic table still labels the row with something
                        # meaningful (MIS / NRML / CNC).
                        "order_type": p.product_type.value,
                        "product_type": p.product_type.value,
                        "lots": abs(p.quantity) / max(1, int(p.instrument.lot_size or 1)),
                        "quantity": abs(p.quantity),
                        "filled_quantity": abs(p.quantity),
                        "price": str(p.avg_price),
                        "average_price": str(p.avg_price),
                        "trigger_price": None,
                        # SL/TP from the Position document — the actual
                        # values the user has set on the open trade.
                        "bracket_stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
                        "bracket_target": str(p.target) if p.target is not None else None,
                        "rejection_reason": None,
                        "status": p.status.value,
                        "created_at": p.opened_at,
                        "executed_at": p.opened_at,
                        "cancelled_at": None,
                        "realized_pnl_inr": None,
                    }
                    for p in pos_rows
                ],
                "meta": {
                    "page": page,
                    "page_size": page_size,
                    "total": pos_total,
                    "total_pages": (pos_total + page_size - 1) // page_size,
                },
            }
        )

    # Local mongo query dict — renamed from `q` to `query` because the
    # endpoint now also accepts `q` as the URL search parameter (see
    # signature above). Without the rename the new parameter would
    # shadow this dict and break every subsequent reference.
    query: dict[str, Any] = {"$or": [{"is_demo": {"$ne": True}}, {"is_demo": {"$exists": False}}]}
    if status:
        query["status"] = status
    elif statuses:
        status_list = [s.strip() for s in statuses.split(",") if s.strip()]
        if status_list:
            query["status"] = {"$in": status_list}
    if user_id:
        await assert_user_in_scope(admin, user_id)
        query["user_id"] = PydanticObjectId(user_id)
    else:
        scope = await scoped_user_ids(admin)
        if scope is not None:
            if not scope:
                return APIResponse(
                    data={
                        "items": [],
                        "meta": {"page": page, "page_size": page_size, "total": 0, "total_pages": 0},
                    }
                )
            query["user_id"] = {"$in": scope}
    # Free-text search splice — match user_id ∈ matched_users OR
    # `instrument.symbol` matches the regex. Keeps any existing
    # `user_id`/`status` filters intact via top-level merge: Mongo
    # implicitly `$and`s sibling keys, so the search `$or` narrows the
    # result without clobbering scope/status filters set above. Empty
    # `search_user_ids` is fine — `$in: []` matches nothing, and the
    # symbol regex still gets a chance to match.
    if search_user_ids is not None and search_symbol_regex is not None:
        query["$or"] = [
            {"user_id": {"$in": search_user_ids}},
            {"instrument.symbol": search_symbol_regex},
        ]
    total = await Order.find(query).count()
    rows = await Order.find(query).sort("-created_at").skip((page - 1) * page_size).limit(page_size).to_list()

    user_ids = list({r.user_id for r in rows})
    users = await User.find({"_id": {"$in": user_ids}}).to_list() if user_ids else []
    user_map = {str(u.id): {"user_code": u.user_code, "full_name": u.full_name} for u in users}

    # Realized P&L is FROZEN on the closing-leg Trade at fill time (in INR,
    # net of brokerage). The Orders page used to recompute (ltp - avg) × qty
    # live every 5 s for every row, which made the P&L cell jitter for
    # already-closed trades — admins kept asking "trade close ho gaya, ye
    # P&L kyon move kar raha hai?". One batched lookup grouped by order_id
    # gives the stable per-order realized number; opening-leg orders have
    # no closing trade yet so they get None and the UI renders "—".
    order_ids = [r.id for r in rows]
    realized_by_order: dict[str, float] = {}
    if order_ids:
        related_trades = await Trade.find(
            {"order_id": {"$in": order_ids}, "pnl_inr": {"$ne": None}}
        ).to_list()
        for t in related_trades:
            if t.pnl_inr is None:
                continue
            key = str(t.order_id)
            realized_by_order[key] = realized_by_order.get(key, 0.0) + float(str(t.pnl_inr))

    return APIResponse(
        data={
            "items": [
                {
                    "id": str(r.id),
                    "order_number": r.order_number,
                    "user_id": str(r.user_id),
                    "user_code": user_map.get(str(r.user_id), {}).get("user_code"),
                    "user_name": user_map.get(str(r.user_id), {}).get("full_name"),
                    "symbol": r.instrument.symbol,
                    "exchange": str(r.instrument.exchange),
                    "segment": r.instrument.segment,
                    "token": r.instrument.token,
                    "instrument_token": r.instrument.token,
                    "action": r.action.value,
                    "order_type": r.order_type.value,
                    "product_type": r.product_type.value,
                    "lots": r.lots,
                    "quantity": r.quantity,
                    "filled_quantity": r.filled_quantity,
                    "price": str(r.price),
                    "average_price": str(r.average_price),
                    "trigger_price": str(r.trigger_price) if r.trigger_price is not None else None,
                    "bracket_stop_loss": str(r.bracket_stop_loss) if r.bracket_stop_loss is not None else None,
                    "bracket_target": str(r.bracket_target) if r.bracket_target is not None else None,
                    "rejection_reason": r.rejection_reason,
                    # Why this order happened (Orders monitor "Reason" column):
                    # SL_HIT / TP_HIT / STOP_OUT / AUTO / ADMIN_CLOSE / ADMIN / USER.
                    "reason": order_reason_code(r),
                    "status": r.status.value,
                    "created_at": r.created_at,
                    "executed_at": r.executed_at,
                    "cancelled_at": getattr(r, "cancelled_at", None),
                    # Frozen realized P&L from the closing-leg trade(s).
                    # None for opening legs whose position is still open —
                    # the UI then renders "—" rather than a live mark.
                    "realized_pnl_inr": realized_by_order.get(str(r.id)),
                }
                for r in rows
            ],
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
            },
        }
    )


def _today_window_utc() -> tuple[datetime, datetime]:
    """(start, end) UTC instants bounding the CURRENT IST calendar day.

    Trades store `executed_at` in UTC; the operator thinks in IST, so we
    anchor the window to IST-midnight → now and convert back to UTC for the
    query. Matches the IST day used by the daily-stats mongosh queries.
    """
    from datetime import timedelta as _td, timezone as _tz

    IST = _tz(_td(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_ist.astimezone(_tz.utc), now_ist.astimezone(_tz.utc)


@router.get("/orders/stats", response_model=APIResponse[dict])
async def orders_stats(
    admin: CurrentAdmin,
    _: None = Depends(require_perm("trading_view", "read")),
):
    """Today's trading summary for the Orders monitor header.

    Returns the count of EXECUTED trades (fills) so far today split by
    BUY / SELL, the combined total, and the number of orders currently
    awaiting trigger/fill (PENDING + OPEN + PARTIAL). All numbers respect
    the admin's user scope and exclude demo accounts, so they match the
    rows the operator actually sees in the table below.
    """
    from datetime import timedelta as _td, timezone as _tz

    start, end = _today_window_utc()
    date_ist = start.astimezone(_tz(_td(hours=5, minutes=30))).strftime("%Y-%m-%d")

    # Resolve the scope → a concrete user_id list with demo accounts removed.
    # scoped_user_ids returns None for an unrestricted super-admin; in that
    # case we only exclude demo users (no positive scope filter).
    scope = await scoped_user_ids(admin)
    demo_ids = {u.id async for u in User.find(User.is_demo == True)}  # noqa: E712

    trade_match: dict[str, Any] = {"executed_at": {"$gte": start, "$lt": end}}
    pending_match: dict[str, Any] = {
        "status": {"$in": ["PENDING", "OPEN", "PARTIAL"]},
        "$or": [{"is_demo": {"$ne": True}}, {"is_demo": {"$exists": False}}],
    }
    if scope is not None:
        if not scope:
            return APIResponse(
                data={
                    "date_ist": date_ist,
                    "total_trades": 0,
                    "buy_trades": 0,
                    "sell_trades": 0,
                    "pending_orders": 0,
                }
            )
        allowed = [uid for uid in scope if uid not in demo_ids]
        trade_match["user_id"] = {"$in": allowed}
        pending_match["user_id"] = {"$in": allowed}
    elif demo_ids:
        # Super-admin: keep everything except demo accounts.
        trade_match["user_id"] = {"$nin": list(demo_ids)}
        # `is_demo` clause on Order already excludes demo; the $nin keeps the
        # two collections consistent in the rare case an Order row lacks the
        # flag but its user is flagged demo.
        pending_match["user_id"] = {"$nin": list(demo_ids)}

    buy_trades = sell_trades = 0
    agg = (
        await Trade.get_motor_collection()
        .aggregate(
            [
                {"$match": trade_match},
                {"$group": {"_id": "$action", "count": {"$sum": 1}}},
            ]
        )
        .to_list(length=None)
    )
    for row in agg:
        if row["_id"] == OrderAction.BUY.value:
            buy_trades = int(row["count"])
        elif row["_id"] == OrderAction.SELL.value:
            sell_trades = int(row["count"])

    pending_orders = await Order.find(pending_match).count()

    return APIResponse(
        data={
            "date_ist": date_ist,
            "total_trades": buy_trades + sell_trades,
            "buy_trades": buy_trades,
            "sell_trades": sell_trades,
            "pending_orders": pending_orders,
        }
    )


@router.get("/orders/quotes", response_model=APIResponse[list])
async def order_quotes(
    admin: CurrentAdmin,
    tokens: str = Query(default=""),
    _: None = Depends(require_perm("trading_view", "read")),
):
    """Tiny LTP batch endpoint so the admin Orders page can compute live P&L
    for every order, including ones whose position is already closed.

    Fan-out is parallel via `asyncio.gather` — the Orders page passes
    every unique token on the visible page at once, so the old serial
    loop turned a 30-row page into a 30 × feed-latency stall (~3 s) on
    every refresh. Concurrent dispatch collapses that to the slowest
    single fetch."""
    tok_list = [t.strip() for t in (tokens or "").split(",") if t.strip()]
    if not tok_list:
        return APIResponse(data=[])
    results = await asyncio.gather(
        *[market_data_service.get_display_ltp(tok) for tok in tok_list],
        return_exceptions=True,
    )
    out = []
    for tok, res in zip(tok_list, results):
        if isinstance(res, BaseException):
            out.append({"token": tok, "ltp": 0.0})
        else:
            try:
                out.append({"token": tok, "ltp": float(res)})
            except Exception:
                out.append({"token": tok, "ltp": 0.0})
    return APIResponse(data=out)


@router.delete("/orders/{order_id}", response_model=APIResponse[dict])
async def force_cancel(
    order_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("trading_view", "write")),
):
    # Scope check: load the order first to confirm it belongs to a user
    # in the caller's pool.
    existing = await Order.get(PydanticObjectId(order_id))
    if existing is None:
        raise HTTPException(status_code=404, detail="Order not found")
    await assert_user_in_scope(admin, existing.user_id)
    o = await order_service.admin_force_cancel(order_id)
    await log_event(
        action=AuditAction.ORDER_CANCEL,
        entity_type="Order",
        entity_id=o.id,
        actor_id=admin.id,
        target_user_id=o.user_id,
    )
    return APIResponse(data={"id": str(o.id), "status": o.status.value})


# ── Positions ────────────────────────────────────────────────────────
# `Any` not `list` — this endpoint returns a flat array in legacy mode but a
# `{rows, total, …}` object in paginated mode (Closed tab). A `list` response
# model would reject the dict with a 500 (which then loses CORS headers and
# surfaces in the browser as a misleading "blocked by CORS / ERR_FAILED").
def _last_week_start_utc() -> datetime:
    """Start (UTC) of LAST week — the most recent IST-Sunday 00:00 minus 7
    days. The admin Closed-Trades view uses this as a lower bound so it shows
    only LAST week + the CURRENT week (the same window as the two Net-P&L
    cards), instead of the entire all-time closed history the admin asked to
    stop scrolling through."""
    from datetime import timedelta as _td, timezone as _tz

    IST = _tz(_td(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    # Monday-anchored week (Mon=0 … Sun=6) — matches the Net-P&L cards and the
    # Accounts dashboard so the Closed tab spans the same current+last week.
    days_back = now_ist.weekday()
    week_start_ist = today_start_ist - _td(days=days_back)
    last_week_start_ist = week_start_ist - _td(days=7)
    return last_week_start_ist.astimezone(_tz.utc)


def _format_closed_fifo_rows(fifo_events: list[dict], user_id: str) -> list[dict]:
    """Map FIFO closed events → the SAME per-fill row shape the USER sees in
    their Closed history, so the admin reviews the identical FIFO flow (one
    row per opening-fill × closing-fill pairing) instead of one aggregated
    row per position. Mirrors the user `closed_positions` mapping exactly."""
    out: list[dict] = []
    for ev in fifo_events:
        inst = ev["instrument"]
        seg = inst.segment
        is_usd = market_data_service.is_usd_quoted_segment(seg)
        lot_size = int(getattr(inst, "lot_size", 0) or 0)
        qty = ev["qty"]
        entry_px = ev["entry_price"]
        close_px = ev["close_price"]
        gross = ev["gross_pnl"]
        brk = ev["brokerage"]
        closed_dt = ev["closed_at"]
        opened_dt = ev["opened_at"]
        out.append(
            {
                "id": ev["_row_id"],
                "position_id": ev["_row_id"],
                "user_id": user_id,
                "symbol": inst.symbol,
                "trading_symbol": getattr(inst, "trading_symbol", None) or inst.symbol,
                "exchange": str(inst.exchange),
                "segment_type": seg,
                "product_type": ev["product_type"].value,
                "quantity": 0.0,
                "opening_quantity": qty,
                "opened_side": ev["opened_side"],
                "lots": (qty / lot_size) if lot_size else qty,
                "lot_size": lot_size,
                "avg_price": f"{entry_px:.4f}" if is_usd else f"{entry_px:.2f}",
                "ltp": f"{close_px:.4f}" if is_usd else f"{close_px:.2f}",
                "realized_pnl": f"{gross:.2f}",
                "unrealized_pnl": "0.00",
                "margin_used": "0.00",
                "charges": f"{brk:.2f}",
                "currency_quote": "USD" if is_usd else "INR",
                "status": "CLOSED",
                "instrument_token": ev["instrument_token"],
                "opened_at": opened_dt.isoformat() if opened_dt else None,
                "closed_at": closed_dt.isoformat() if closed_dt else None,
                "close_reason": ev.get("close_reason", "USER"),
            }
        )
    return out


@router.get("/positions/closed-fifo", response_model=APIResponse[Any])
async def list_closed_positions_fifo(
    admin: CurrentAdmin,
    user_id: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    _: None = Depends(require_perm("trading_view", "read")),
):
    """A user's FIFO closed blotter, FOR THE ADMIN — the exact same
    per-opening-fill rows the user sees in their own Closed history
    (`position_service.list_closed_trade_events_fifo`). Each closing trade is
    matched FIFO against the opening fills, so a 150-qty close of two 100-qty
    buys shows as two rows (100 @ entry₁, 50 @ entry₂) — NOT one aggregated
    position row. Lets the admin review the identical FIFO flow the user sees.
    """
    await assert_user_in_scope(admin, user_id)
    skip = (page - 1) * page_size
    fifo_events, total = await position_service.list_closed_trade_events_fifo(
        user_id, skip=skip, limit=page_size
    )
    return APIResponse(
        data={
            "rows": _format_closed_fifo_rows(fifo_events, user_id),
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.get("/exposure", response_model=APIResponse[Any])
async def house_exposure(
    admin: CurrentAdmin,
    segment: str | None = Query(default=None, description="Filter to one segment_type"),
    _: None = Depends(require_perm("trading_view", "read")),
):
    """The house book: every open position folded together per symbol.

    This is the B-book's own risk position: clients net long 40 lots of GOLD
    means the house is short 40, and `net_qty` is what the house would have to
    hedge to be flat.

    No P&L column: open P&L is only persisted when the risk loop marks a row,
    and notional/P&L are in each instrument's own currency (USD for CFDs, INR
    for MCX), so one summed figure across symbols would be a wrong number
    rather than a useful one. `margin_used` is INR everywhere and is summed.

    Aggregated in Mongo rather than in Python: the per-user Positions page
    already pulls rows one user at a time, and folding thousands of rows in
    the API worker just to sum them is the kind of thing that makes an admin
    page time out once the book grows.
    """
    match: dict[str, Any] = {
        "status": PositionStatus.OPEN.value,
        "$or": [{"is_demo": {"$ne": True}}, {"is_demo": {"$exists": False}}],
    }
    scope = await scoped_user_ids(admin)
    if scope is not None:
        if not scope:
            return APIResponse(data={"rows": [], "totals": {}})
        match["user_id"] = {"$in": scope}
    if segment:
        match["segment_type"] = segment

    pipeline: list[dict[str, Any]] = [
        {"$match": match},
        {
            "$group": {
                "_id": {
                    "token": "$instrument.token",
                    "symbol": "$instrument.symbol",
                    "exchange": "$instrument.exchange",
                    "segment": "$segment_type",
                },
                # `quantity` is signed: long positive, short negative.
                "net_qty": {"$sum": "$quantity"},
                "long_qty": {
                    "$sum": {"$cond": [{"$gt": ["$quantity", 0]}, "$quantity", 0]}
                },
                "short_qty": {
                    "$sum": {"$cond": [{"$lt": ["$quantity", 0]}, {"$abs": "$quantity"}, 0]}
                },
                # How many open trades sit on each side, as distinct from how
                # much quantity: "3 clients are long XAUUSD, 1 is short".
                "long_positions": {
                    "$sum": {"$cond": [{"$gt": ["$quantity", 0]}, 1, 0]}
                },
                "short_positions": {
                    "$sum": {"$cond": [{"$lt": ["$quantity", 0]}, 1, 0]}
                },
                "users": {"$addToSet": "$user_id"},
                "positions": {"$sum": 1},
                "margin_used": {"$sum": {"$toDouble": "$margin_used"}},
                "notional": {
                    "$sum": {
                        "$multiply": [{"$abs": "$quantity"}, {"$toDouble": "$avg_price"}]
                    }
                },
                # Every open row on a symbol carries the same ticker-updated
                # price, so the max is simply the current one (0 when cold).
                "ltp": {"$max": {"$toDouble": "$ltp"}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "token": "$_id.token",
                "symbol": "$_id.symbol",
                "exchange": "$_id.exchange",
                "segment": "$_id.segment",
                "net_qty": 1,
                "long_qty": 1,
                "short_qty": 1,
                "long_positions": 1,
                "short_positions": 1,
                "positions": 1,
                "users": {"$size": "$users"},
                "margin_used": {"$round": ["$margin_used", 2]},
                "notional": {"$round": ["$notional", 2]},
                "ltp": 1,
            }
        },
        {"$sort": {"notional": -1}},
    ]
    rows = await Position.aggregate(pipeline).to_list()

    totals = {
        "symbols": len(rows),
        "positions": sum(r["positions"] for r in rows),
        "long_positions": sum(r["long_positions"] for r in rows),
        "short_positions": sum(r["short_positions"] for r in rows),
        # INR everywhere, so this one total is comparable across symbols.
        "margin_used": round(sum(r["margin_used"] for r in rows), 2),
        "users": 0,
    }
    if rows:
        # Distinct users across the whole book, not the sum of per-symbol
        # counts (one client holding three symbols is still one client).
        distinct = await Position.aggregate(
            [{"$match": match}, {"$group": {"_id": "$user_id"}}, {"$count": "n"}]
        ).to_list()
        totals["users"] = distinct[0]["n"] if distinct else 0

    return APIResponse(data={"rows": rows, "totals": totals})


@router.get("/positions", response_model=APIResponse[Any])
async def list_positions(
    admin: CurrentAdmin,
    user_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
    product: str | None = None,
    from_date: str | None = Query(default=None, description="IST YYYY-MM-DD; CLOSED tab lower bound (closed_at)"),
    to_date: str | None = Query(default=None, description="IST YYYY-MM-DD; CLOSED tab upper bound (closed_at)"),
    max_hold_sec: int | None = Query(
        default=None,
        ge=1,
        le=86400,
        description="CLOSED tab: only positions held at most this long (fast-close / scalp view)",
    ),
    own_scope: bool = Query(default=False, description="SUPER_ADMIN only: restrict to the super-admin's OWN direct users (no admin's clients)"),
    page: int | None = Query(default=None, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
    _: None = Depends(require_perm("trading_view", "read")),
):
    # Two response shapes, switched by whether `page` is supplied:
    #   • `page` omitted (legacy) → return a flat array of up to 500 rows.
    #     The Open Trades tab + any other caller still rely on this: they
    #     need the WHOLE set in one shot (live M2M aggregate, WS token
    #     subscription) so server pagination would break their header sum.
    #   • `page` supplied → server-side pagination: count + skip/limit so
    #     ONLY that page's rows are fetched AND enriched. The Closed Trades
    #     tab uses this — on books with thousands of closed positions the
    #     old "fetch 500 + bulk-charges over a weeks-wide trade window"
    #     path made the tab sit blank for seconds. Paginating shrinks both
    #     the row set and (because charges only spans the page's opened_at
    #     range) the trade fan-out, so the first 25 land near-instantly.
    paginated = page is not None
    # `qfilter` is the Mongo filter dict — named to avoid colliding with the
    # `q` URL search-text parameter added above.
    qfilter: dict[str, Any] = {}
    and_clauses: list[dict[str, Any]] = [
        {"$or": [{"is_demo": {"$ne": True}}, {"is_demo": {"$exists": False}}]}
    ]

    def _empty():
        # Shape-correct empty response for the requested mode.
        if paginated:
            return APIResponse(
                data={"rows": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}
            )
        return APIResponse(data=[])

    if user_id:
        await assert_user_in_scope(admin, user_id)
        qfilter["user_id"] = PydanticObjectId(user_id)
    else:
        # own_scope (super-admin Positions page): the super-admin's OWN direct
        # users only — never any sub-admin's clients.
        if own_scope and admin.role == UserRole.SUPER_ADMIN:
            scope = await super_admin_direct_user_ids()
        else:
            scope = await scoped_user_ids(admin)
        if scope is not None:
            if not scope:
                return _empty()
            qfilter["user_id"] = {"$in": scope}
    # status="ALL" (or "*") → return both OPEN and CLOSED. Empty → default
    # to OPEN-only so the page is fast on load.
    norm_status = (status or "").strip().upper()
    if norm_status and norm_status not in ("ALL", "*"):
        qfilter["status"] = norm_status
    elif not norm_status:
        qfilter["status"] = PositionStatus.OPEN.value

    # CLOSED view: default window = LAST week + CURRENT week (same as the two
    # Net-P&L cards) so admins don't scroll months of old closed trades. When
    # the admin sets an explicit From/To date filter, that OVERRIDES the
    # default — closed_at is bounded by the picked IST dates instead. Guarded
    # to CLOSED-only so OPEN / ALL views (no closed_at) are never filtered out.
    if qfilter.get("status") == PositionStatus.CLOSED.value:
        from datetime import timedelta as _td_cl, timezone as _tz_cl

        _IST = _tz_cl(_td_cl(hours=5, minutes=30))
        cl_range: dict[str, Any] = {}
        if from_date or to_date:
            try:
                if from_date:
                    _s = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=_IST)
                    cl_range["$gte"] = _s.astimezone(_tz_cl.utc)
                if to_date:
                    _e = datetime.strptime(to_date, "%Y-%m-%d").replace(
                        hour=23, minute=59, second=59, tzinfo=_IST
                    )
                    cl_range["$lte"] = _e.astimezone(_tz_cl.utc)
            except ValueError:
                cl_range = {}
        if not cl_range:
            cl_range = {"$gte": _last_week_start_utc()}
        qfilter["closed_at"] = cl_range

    # Product filter (MIS / NRML / CNC) — Position carries product_type, so
    # this narrows server-side (the order-type variants MARKET/LIMIT/SL_M
    # are derived from trades and stay client-side on the returned page).
    norm_product = (product or "").strip().upper()
    if norm_product in ("MIS", "NRML", "CNC"):
        qfilter["product_type"] = norm_product

    # Free-text search (user full_name / user_code / symbol), mirroring the
    # orders monitor. Resolve matching users within the admin's scope, then
    # splice an `$or` over user_id + symbol regex via `$and` so it composes
    # with the is_demo / scope / status clauses instead of clobbering them.
    if q and len(q.strip()) >= 2 and not user_id:
        needle = re.escape(q.strip())
        regex = {"$regex": needle, "$options": "i"}
        scope_ids = await scoped_user_ids(admin)
        u_filter: dict[str, Any] = {"$or": [{"full_name": regex}, {"user_code": regex}]}
        if scope_ids is not None:
            u_filter["_id"] = {"$in": scope_ids}
        matched = await User.find(u_filter).to_list()
        and_clauses.append(
            {
                "$or": [
                    {"user_id": {"$in": [m.id for m in matched]}},
                    {"instrument.symbol": regex},
                ]
            }
        )

    # Fast-close view — "trades closed within N minutes". Mongo subtracts the
    # two timestamps server-side because the CLOSED tab is paginated: filtering
    # in the page would only narrow the 25 rows already fetched, not the book.
    if max_hold_sec is not None:
        and_clauses.append(
            {
                "$expr": {
                    "$and": [
                        {"$gt": ["$closed_at", None]},
                        {"$gt": ["$opened_at", None]},
                        {
                            "$lte": [
                                {"$subtract": ["$closed_at", "$opened_at"]},
                                max_hold_sec * 1000,
                            ]
                        },
                    ]
                }
            }
        )

    qfilter["$and"] = and_clauses

    if paginated:
        total = await Position.find(qfilter).count()
        if total == 0:
            return _empty()
        # CLOSED tab: sort by closed_at DESC so recently settled/closed positions
        # land on page 1. opened_at sort buried weekly-settlement rows (closed
        # last night but opened weeks ago) on page 200+ behind old USER-closed rows.
        sort_field = "-closed_at" if norm_status == PositionStatus.CLOSED.value else "-opened_at"
        rows = (
            await Position.find(qfilter)
            .sort(sort_field)
            .skip((page - 1) * page_size)
            .limit(page_size)
            .to_list()
        )
    else:
        rows = await Position.find(qfilter).sort("-opened_at").limit(500).to_list()

    from app.api.v1.admin._owner import build_owner_map

    user_ids = list({r.user_id for r in rows})
    # Build owner map (user_name + assigned admin/broker) so the positions
    # table can render Self vs. Broker: <name> badges per row.
    user_map = await build_owner_map(user_ids)

    # Snapshot the live USD/INR rate once so every USD-quoted row in this
    # response is converted using a consistent reference. Infoway keeps this
    # tick fresh; on cold start we fall back to the constant.
    current_usd_inr = market_data_service.get_usd_inr_rate()

    # Parallel LTP fan-out. Previously this loop did `await get_ltp(...)`
    # serially inside the per-row body, which meant for a typical 50-
    # position cap the endpoint blocked for ~5 s on Redis/feed lookups
    # alone — and the entire admin Positions page sat blank that whole
    # time. asyncio.gather hits them concurrently so the total wait
    # collapses to roughly the slowest single fetch (~50-100 ms).
    # Duplicate tokens are resolved once via a dict so we don't double-
    # ping the feed when several rows share a symbol.
    unique_tokens = list({r.instrument.token for r in rows})
    ltp_map: dict[str, float] = {}
    # CLOSED rows freeze their close-price onto `r.ltp`, so the live feed
    # is never read for them — skip the Redis/feed fan-out entirely when
    # the query is CLOSED-only. Saves the slowest part of the Closed tab.
    if norm_status != PositionStatus.CLOSED.value:
        ltp_results = await asyncio.gather(
            *[market_data_service.get_display_ltp(tok) for tok in unique_tokens],
            return_exceptions=True,
        )
        for tok, res in zip(unique_tokens, ltp_results):
            try:
                ltp_map[tok] = float(res) if not isinstance(res, BaseException) else 0.0
            except Exception:
                ltp_map[tok] = 0.0

    # Bulk-fetch every trade that touches these positions so we can attach
    # a per-position `charges` total without an N+1 query. Mirrors the
    # bucketing user-facing /positions/closed uses (start − slack ≤
    # executed_at ≤ end + slack) so charges land on the right position
    # lifecycle even when two CLOSED rows share (user, token, product).
    # Same key the user endpoint uses keeps the math aligned across views.
    from datetime import timedelta as _td_charges

    by_charges_key: dict[tuple[str, str, str], list[Trade]] = {}
    # Map order_id → order_type (MARKET / LIMIT / SL_M) so each position row
    # can show HOW it was opened. The Position has no order_type (it's an
    # order property); we recover it from the opening fill's order below.
    order_type_by_id: dict[Any, str] = {}
    if rows:
        user_ids_for_trades = list({r.user_id for r in rows})
        trade_q: dict[str, Any] = {
            "user_id": {"$in": user_ids_for_trades},
            "instrument.token": {"$in": unique_tokens},
        }
        oldest_open = min((r.opened_at for r in rows if r.opened_at), default=None)
        if oldest_open is not None:
            trade_q["executed_at"] = {"$gte": oldest_open - _td_charges(seconds=5)}
        trade_rows = await Trade.find(trade_q).sort("+executed_at").to_list()
        for t in trade_rows:
            key = (str(t.user_id), t.instrument.token, t.product_type.value)
            by_charges_key.setdefault(key, []).append(t)
        # One bulk lookup of the orders behind those trades → order_type.
        _oid_list = list({t.order_id for t in trade_rows if getattr(t, "order_id", None)})
        if _oid_list:
            _orders = await Order.find({"_id": {"$in": _oid_list}}).to_list()
            order_type_by_id = {o.id: o.order_type.value for o in _orders}

    def _bucket_for(p: Position) -> list[Trade]:
        """Trades belonging to this position's lifecycle.

        A position carried overnight is converted MIS→NRML (or MIS→CNC) by
        `convert_intraday_to_carry`, which flips ONLY the Position doc — the
        opening Trade rows keep their original MIS `product_type`. So a
        carried NRML/CNC position's `(user, token, NRML)` bucket misses its
        own opening fills, leaving the admin blotter's Order-Type column
        blank and its Charges column under-counted. For carry product types
        we therefore merge in the MIS bucket (re-sorted by executed_at so the
        earliest opening fill still wins) to recover the opening order.
        """
        key = (str(p.user_id), p.instrument.token, p.product_type.value)
        bucket = list(by_charges_key.get(key, []))
        if p.product_type.value in ("NRML", "CNC"):
            mis_key = (str(p.user_id), p.instrument.token, "MIS")
            mis_bucket = by_charges_key.get(mis_key)
            if mis_bucket:
                bucket = sorted(bucket + list(mis_bucket), key=lambda t: t.executed_at)
        return bucket

    def _charges_for(p: Position) -> float:
        bucket = _bucket_for(p)
        if not bucket:
            return 0.0
        if not p.opened_at:
            return sum(
                float(str(getattr(t, "total_charges", None) or t.brokerage or 0))
                for t in bucket
            )
        # Opening fills (pnl_inr=None): count from opened_at onwards.
        #   Upper bound = closed_at for CLOSED positions (so later same-token
        #   positions don't bleed in). No upper bound for OPEN positions
        #   (more lots can be added at any time).
        # Closing fills (pnl_inr set): only count from the current reopen
        #   cycle (>= reopened_at) so stale closing rows don't accumulate.
        cycle_start = p.reopened_at or p.opened_at
        pos_open = p.opened_at
        pos_end = p.closed_at  # None when OPEN — means no upper bound
        slack = _td_charges(seconds=5)
        total = 0.0
        for t in bucket:
            charge = float(str(getattr(t, "total_charges", None) or t.brokerage or 0))
            if getattr(t, "pnl_inr", None) is not None:
                # closing fill — must be in current lifecycle window.
                # Upper bound: pos_end (closed_at) prevents later same-token
                # positions' closing fills from bleeding into this row.
                if t.executed_at < cycle_start - slack:
                    continue
                if pos_end is not None and t.executed_at > pos_end + slack:
                    continue
                total += charge
            else:
                # opening fill — lower bound: opened_at; upper bound: closed_at or none
                if t.executed_at < pos_open - slack:
                    continue
                if pos_end is not None and t.executed_at > pos_end + slack:
                    continue
                total += charge
        return total

    def _order_type_for(p: Position) -> str | None:
        """Order type (MARKET / LIMIT / SL_M) of the order that OPENED this
        position — recovered from the earliest opening fill's order. Returns
        None if the linking order isn't found."""
        bucket = _bucket_for(p)
        if not bucket:
            return None
        slack = _td_charges(seconds=5)
        pos_open = p.opened_at
        pos_end = p.closed_at
        for t in bucket:  # already sorted +executed_at → first opening fill wins
            if getattr(t, "pnl_inr", None) is not None:
                continue  # closing fill, skip
            if pos_open is not None and t.executed_at < pos_open - slack:
                continue
            if pos_end is not None and t.executed_at > pos_end + slack:
                continue
            ot = order_type_by_id.get(getattr(t, "order_id", None))
            if ot:
                return ot
        return None

    # Wallet balance AFTER each close — the user's available_balance right
    # after this trade settled. WalletTransaction.balance_after already tracks
    # available_balance, so the latest txn at/just-after closed_at IS the
    # post-close wallet balance. Batched over the (≤ page_size) CLOSED rows.
    from app.models.transaction import WalletTransaction as _WTx  # noqa: PLC0415
    from datetime import timedelta as _td_wa  # noqa: PLC0415

    wallet_after_map: dict[str, float] = {}

    async def _wallet_after(p: Position) -> None:
        if p.status != PositionStatus.CLOSED or not p.closed_at:
            return
        txn = (
            await _WTx.find(
                {"user_id": p.user_id, "created_at": {"$lte": p.closed_at + _td_wa(seconds=5)}}
            )
            .sort("-created_at")
            .limit(1)
            .to_list()
        )
        if txn:
            try:
                wallet_after_map[str(p.id)] = float(str(txn[0].balance_after))
            except Exception:
                pass

    await asyncio.gather(*[_wallet_after(r) for r in rows])

    out = []
    for r in rows:
        # For CLOSED rows the price + P&L must be FROZEN — the user
        # explicitly flagged this ("close trade me pnl move mat karna
        # thoda sa bhi"). Use the close-price that
        # position_service.apply_trade stamped onto `r.ltp` at the
        # closing fill, never the live feed. For OPEN rows keep the
        # live LTP so M2M ticks per refresh.
        is_closed = r.status == PositionStatus.CLOSED
        if is_closed:
            stored_ltp = float(str(r.ltp)) if r.ltp is not None else 0.0
            ltp_f = stored_ltp
        else:
            ltp = ltp_map.get(r.instrument.token, 0.0)
            ltp_f = float(ltp)
        avg = float(str(r.avg_price))
        qty = r.quantity
        margin = float(str(r.margin_used))
        realized = float(str(r.realized_pnl))

        is_usd = market_data_service.is_usd_quoted_segment(r.segment_type) or \
            market_data_service.is_usd_quoted_segment(r.instrument.segment)

        # Prices stay in source currency (USD for crypto/forex, INR for the
        # rest) — that's what the live feed quotes. Only realised + unrealised
        # P&L gets converted to INR so the wallet/M2M columns are consistent.
        if is_usd:
            open_rate = (
                float(str(r.open_usd_inr_rate))
                if r.open_usd_inr_rate is not None
                else current_usd_inr
            )
            # Realised P&L was crystallised at close time, so the user-side
            # trade history shows it converted at the CLOSE-time USDINR
            # (matching_engine stamps `trade.pnl_inr` using that rate).
            # Use the same close-rate snapshot here so the admin column
            # matches what the user sees in their History tab. Partial
            # closes on a still-open position have no close_rate yet — fall
            # back to open_rate as a reasonable approximation.
            close_rate = (
                float(str(r.close_usd_inr_rate))
                if r.close_usd_inr_rate is not None
                else open_rate
            )
            # CLOSED → frozen 0 (qty is 0 anyway; making it explicit so
            # any future code that touches this branch can't drift).
            # OPEN → live FX × live LTP delta so M2M ticks per refresh.
            if is_closed or ltp_f <= 0:
                # Stale-feed guard: if LTP feed flatlined to 0 don't
                # compute M2M against zero — phantom losses like
                # (0 − 8631) × 300 = -25,90,007 were rendering on the
                # dashboard until the feed recovered.
                unrealized_pnl_inr = 0.0
            else:
                unrealized_pnl_inr = (ltp_f - avg) * qty * current_usd_inr
            realized_pnl_inr = realized * close_rate
            # margin_used was locked from the wallet at order time (validator
            # computed it as a wallet-currency number), so DON'T re-apply FX
            # here — that's why the position view used to show ~80× the
            # wallet's used_margin.
            margin_inr = margin
        else:
            # Same stale-feed guard for INR-quoted segments — see USD
            # branch above for the rationale.
            unrealized_pnl_inr = (
                0.0 if (is_closed or ltp_f <= 0) else (ltp_f - avg) * qty
            )
            realized_pnl_inr = realized
            margin_inr = margin
            open_rate = 1.0

        oi = user_map.get(str(r.user_id)) or {}
        out.append(
            {
                "id": str(r.id),
                "user_id": str(r.user_id),
                "user_code": oi.get("user_code"),
                "user_name": oi.get("user_name"),
                "assigned_admin_id": oi.get("assigned_admin_id"),
                "assigned_admin_name": oi.get("assigned_admin_name"),
                "assigned_broker_id": oi.get("assigned_broker_id"),
                "assigned_broker_name": oi.get("assigned_broker_name"),
                "assigned_broker_is_sub": oi.get("assigned_broker_is_sub", False),
                "symbol": r.instrument.symbol,
                "instrument_token": r.instrument.token,
                "exchange": str(r.instrument.exchange),
                "segment_type": r.segment_type,
                "product_type": r.product_type.value,
                # How the position was opened (MARKET / LIMIT / SL_M) — for the
                # admin blotter's Order-Type column + filter.
                "order_type": _order_type_for(r),
                # Lot size of the instrument at the time the position is
                # observed. Lets the admin blotter compute Volume column
                # (= qty/lot_size) without a separate /instruments lookup.
                "lot_size": int(getattr(r.instrument, "lot_size", 0) or 0),
                "quantity": qty,
                # Original trade size at peak of this position's lifecycle.
                # `quantity` drops to 0 on full close, so the Closed-tab UI
                # falls back to this to render "user ne kitni qty li thi".
                # Captured in apply_fill; never decremented on close.
                "opening_quantity": r.opening_quantity,
                # Direction the user opened on. Stable across the position's
                # lifecycle — the Closed-tab needs this to colour the qty
                # cell (BUY = green, SELL = red) since the signed `quantity`
                # is 0 after the closing leg.
                "opened_side": r.opened_side.value if r.opened_side is not None else None,
                # Prices in source currency — UI renders with $ or ₹ based on
                # the `currency_quote` flag below.
                "avg_price": f"{avg:.4f}" if is_usd else f"{avg:.2f}",
                "ltp": f"{ltp_f:.4f}" if is_usd else f"{ltp_f:.2f}",
                # P&L + margin are always INR (wallet currency).
                "unrealized_pnl": f"{unrealized_pnl_inr:.2f}",
                "realized_pnl": f"{realized_pnl_inr:.2f}",
                # Sum of brokerage + every other charge stamped on this
                # position's lifecycle trades. Admin frontend subtracts
                # this from `realized_pnl` so the displayed P&L matches
                # the NET number the user sees on their APK (which the
                # user-facing /closed endpoint already nets — see
                # user/positions.py:closed_positions). Without this the
                # admin and user views always disagreed by the brokerage
                # amount on every closed trade.
                "charges": f"{_charges_for(r):.2f}",
                "margin_used": f"{margin_inr:.2f}",
                # Currency tag so the UI can prefix avg/ltp with $ instead of ₹
                "currency_quote": "USD" if is_usd else "INR",
                "open_usd_inr_rate": f"{open_rate:.4f}" if is_usd else None,
                "current_usd_inr_rate": f"{current_usd_inr:.4f}" if is_usd else None,
                "status": r.status.value,
                "opened_at": r.opened_at,
                "closed_at": r.closed_at.isoformat() if r.closed_at else None,
                # Compact tag set by the squareoff path that flipped this
                # row to CLOSED. SL_HIT / TP_HIT / STOP_OUT / USER / AUTO.
                # Admin trades table renders it as a chip so super-admins
                # can see which closes were auto-fires vs user-initiated.
                "close_reason": r.close_reason,
                # User's wallet available_balance right AFTER this trade closed
                # (None for OPEN rows). Rendered bold on the Closed tab.
                "wallet_after": wallet_after_map.get(str(r.id)),
            }
        )
    if paginated:
        return APIResponse(
            data={
                "rows": out,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
            }
        )
    return APIResponse(data=out)


def _pos_oid(position_id: str) -> PydanticObjectId:
    """Path ``position_id`` → ObjectId, raising a clean 400 (never a 500) when
    it isn't a real position id. The admin Closed tab's FIFO per-fill view
    uses synthetic ids like ``fifo_<trade>_<idx>`` / ``wsettle_<id>`` which are
    NOT positions — squareoff / edit / reopen / delete operate on a real
    Position doc, so those actions must be taken from the aggregated view. A
    bare ``PydanticObjectId(...)`` on a synthetic id raised bson.InvalidId →
    500 → the admin saw a bare "Network Error" (500s skip CORS headers)."""
    try:
        return PydanticObjectId(position_id)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail=(
                "This is a per-fill row (FIFO view). Turn FIFO view OFF to "
                "edit / reopen / delete the position."
            ),
        )


@router.post("/positions/{position_id}/squareoff", response_model=APIResponse[dict])
async def admin_squareoff(
    position_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("trading_view", "write")),
):
    p = await Position.get(_pos_oid(position_id))
    if p is None or p.status != PositionStatus.OPEN or p.quantity == 0:
        raise HTTPException(status_code=400, detail="Position is not open")
    target_user = await assert_user_in_scope(admin, p.user_id)
    action = OrderAction.SELL if p.quantity > 0 else OrderAction.BUY
    # Flatten the EXACT open quantity. Using `force_quantity` mirrors the
    # user-side squareoff path — it avoids the integer-floor bug that
    # used to leave a tiny residual on crypto/USD positions where
    # `qty (96) // lot_size (100) = 0` then `max(1, 0) = 1 lot = 100 units`,
    # so a -96 short was BUY-1-lot'd back to +4 instead of flat.
    full_qty = abs(p.quantity)
    full_lots = max(0.01, full_qty / max(1, p.instrument.lot_size or 1))
    # `is_squareoff=True` tells the validator (a) margin lock is
    # zero, (b) lot-size / max-lots / utilisation caps don't apply,
    # and (c) market-hours guard is bypassed — admins must be able to
    # flatten any position 24×7, including weekends and Indian
    # exchange off-hours.
    o = await order_service.place_order(
        user=target_user,
        payload={
            "token": p.instrument.token,
            "action": action.value,
            "order_type": OrderType.MARKET.value,
            "product_type": p.product_type.value,
            "lots": full_lots,
            "force_quantity": full_qty,
            "placed_from": "ADMIN",
            "is_squareoff": True,
            "close_reason": "ADMIN_CLOSE",  # Orders monitor → "Admin close"
        },
    )
    await log_event(
        action=AuditAction.SQUAREOFF_FORCE,
        entity_type="Position",
        entity_id=p.id,
        actor_id=admin.id,
        target_user_id=p.user_id,
    )
    # Stamp close_reason="AUTO" if the admin force-close actually flattened
    # the row — the matching engine wrote the new state in place. Marks
    # the close as "not user-initiated" on every Closed-tab view (user
    # app, web, admin trades).
    try:
        fresh = await Position.get(_pos_oid(position_id))
        if (
            fresh is not None
            and fresh.status == PositionStatus.CLOSED
            and not fresh.close_reason
        ):
            fresh.close_reason = "AUTO"
            await fresh.save()
    except Exception:
        pass
    # Reload the position so the published payload reflects the closed state
    refreshed = await Position.get(_pos_oid(position_id))
    await _publish_position_event(p.user_id, "force_close", refreshed or p, {"by": "admin"})
    return APIResponse(data={"order_id": str(o.id), "status": o.status.value})


@router.patch("/positions/{position_id}", response_model=APIResponse[dict])
async def admin_edit_position(
    position_id: str,
    payload: dict[str, Any],
    admin: CurrentAdmin,
    _: None = Depends(require_perm("trading_view", "write")),
):
    """Admin-only: edit a position's entry / exit details.

    OPEN-position fields:
        avg_price, quantity, opened_at, stop_loss, target

    CLOSED-position fields (admin correction of a bad close):
        realized_pnl  — override the booked realised. Difference vs
                        the previous value is posted to the user's
                        wallet as a REVERSAL transaction so the
                        ledger always reconciles.
        close_reason  — relabel (USER / SL_HIT / STOP_OUT / ADMIN).
                        Cosmetic, no money movement.

    Patch is fanned out via Redis pub/sub so the user's terminal
    re-renders the positions strip without a refresh.
    """
    p = await Position.get(_pos_oid(position_id))
    if p is None:
        raise HTTPException(status_code=404, detail="Position not found")
    await assert_user_in_scope(admin, p.user_id)

    old_values: dict[str, Any] = {
        "avg_price": str(p.avg_price),
        # Close price (Position.ltp on a CLOSED row) so the Admin Actions
        # → Edited Positions audit can show close-price corrections, not
        # just avg/qty. Without this a close-price-only edit logged no
        # visible change on that page (operator-flagged 22-Jun).
        "close_price": str(p.ltp) if p.ltp is not None else None,
        "quantity": p.quantity,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        "stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
        "target": str(p.target) if p.target is not None else None,
        "realized_pnl": str(p.realized_pnl) if p.realized_pnl is not None else None,
        "close_reason": p.close_reason,
        "status": p.status.value,
    }

    if "avg_price" in payload and payload["avg_price"] is not None:
        try:
            p.avg_price = Decimal128(str(payload["avg_price"]))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid avg_price: {e}")
    if "quantity" in payload and payload["quantity"] is not None:
        try:
            p.quantity = float(payload["quantity"])
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid quantity: {e}")
    if "opened_at" in payload and payload["opened_at"] is not None:
        try:
            p.opened_at = datetime.fromisoformat(str(payload["opened_at"]).replace("Z", "+00:00"))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid opened_at: {e}")
    if "stop_loss" in payload:
        v = payload["stop_loss"]
        p.stop_loss = Decimal128(str(v)) if v not in (None, "", 0) else None
    if "target" in payload:
        v = payload["target"]
        p.target = Decimal128(str(v)) if v not in (None, "", 0) else None

    # ── CLOSED-row corrections ────────────────────────────────────────
    # Allow admin to edit avg_price (open price) and ltp (close price)
    # on CLOSED rows. When either changes, realized_pnl is recalculated
    # automatically so the delta flows through the REVERSAL wallet path.
    # Also supports direct realized_pnl override for edge cases.
    realized_delta: Decimal | None = None
    # Set when a CLOSED position's open/close PRICE is edited — drives the
    # post-save resync that pushes the new prices onto the underlying Trade
    # fills so the user's FIFO Closed blotter reflects the correction (the
    # blotter derives entry/close/P&L from trades, not the Position doc).
    resync_closed_fills = False

    if p.status == PositionStatus.CLOSED:
        from decimal import Decimal as _Decimal
        from app.utils.decimal_utils import to_decimal as _td
        from app.services import market_data_service as _mds

        recalc_pnl = False
        if "avg_price" in payload and payload["avg_price"] is not None:
            try:
                p.avg_price = Decimal128(str(payload["avg_price"]))
                recalc_pnl = True
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid avg_price: {e}")
        if "close_price" in payload and payload["close_price"] is not None:
            try:
                p.ltp = Decimal128(str(payload["close_price"]))
                recalc_pnl = True
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Invalid close_price: {e}")

        if recalc_pnl:
            # Recompute realized_pnl from the new open/close prices.
            # sign = +1 for long (BUY), -1 for short (SELL).
            try:
                open_px = _td(p.avg_price)
                close_px = _td(p.ltp)
                abs_qty = _td(abs(p.opening_quantity or p.quantity or 0))
                sign = _Decimal(1) if str(getattr(p, "opened_side", None) or "BUY").upper() == "BUY" else _Decimal(-1)
                raw = (close_px - open_px) * abs_qty * sign
                is_usd = _mds.is_usd_quoted_segment(p.segment_type) or \
                    _mds.is_usd_quoted_segment(p.instrument.segment)
                if is_usd:
                    fx = _td(p.open_usd_inr_rate or _mds.get_usd_inr_rate())
                    raw = raw * fx
                old_realized = _td(p.realized_pnl or 0)
                from app.utils.decimal_utils import quantize_money as _qm
                new_realized = _qm(raw)
                realized_delta = new_realized - old_realized
                p.realized_pnl = Decimal128(str(new_realized))
                resync_closed_fills = True
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"PnL recalc failed: {e}")

        # Direct realized_pnl override (takes precedence over recalc if both sent)
        if (
            "realized_pnl" in payload
            and payload["realized_pnl"] is not None
            and not recalc_pnl
        ):
            try:
                new_realized = _td(payload["realized_pnl"])
                old_realized = _td(p.realized_pnl or 0)
                realized_delta = new_realized - old_realized
                p.realized_pnl = Decimal128(str(new_realized))
                # Also push the correction onto the underlying fills so the
                # user's FIFO Closed blotter matches the wallet, not just a
                # price edit. (Was: only price edits resynced → a realized
                # override left the user view stale, mismatching the wallet.)
                resync_closed_fills = True
            except Exception as e:
                raise HTTPException(
                    status_code=400, detail=f"Invalid realized_pnl: {e}"
                )

    if "close_reason" in payload and p.status == PositionStatus.CLOSED:
        v = payload["close_reason"]
        p.close_reason = str(v) if v else None

    # Recompute margin_used at the new entry using the SAME formula the
    # order validator runs at order time — fixed-per-lot vs notional × pct
    # ÷ leverage, USD→INR conversion for Infoway segments. The earlier
    # version of this block was `|qty| × avg_price` (the raw notional),
    # which ignored leverage entirely. The downstream
    # `recompute_used_margin` then mirrored that bogus notional into
    # wallet.used_margin and DEBITED the delta from available_balance —
    # so a 500× leverage NFO_FUTURE edit drained the wallet by ~7× the
    # real margin requirement. Operator-flagged 22-May: RAMAN
    # (CL99184090) BHARTIARTL edit zapped his wallet to -₹3.66 L
    # (200 × 1892.70 = ₹3,78,540 locked instead of ₹757).
    # Margin only applies to OPEN rows — a CLOSED position holds none, so
    # editing a closed row's historical qty/avg never touches the wallet.
    # `margin_delta` is set to the (signed) change in locked margin so the
    # block after the save can move it on the wallet; None = no re-balance.
    margin_delta: Any = None
    if ("avg_price" in payload or "quantity" in payload) and p.status == PositionStatus.OPEN:
        from app.services import wallet_service as _ws_chk
        from app.services.market_data_service import (
            get_usd_inr_rate as _get_usd_inr_rate,
            is_usd_quoted_segment as _is_usd_quoted_segment,
        )
        from app.services.netting_service import get_effective_settings
        from app.utils.decimal_utils import (
            quantize_money as _quantize_money,
            to_decimal as _to_decimal,
        )

        prev_margin = _to_decimal(p.margin_used)
        new_margin = prev_margin
        try:
            ref_price = _to_decimal(p.avg_price)
            qty_abs = _to_decimal(abs(p.quantity))
            lot_size = max(1, int(getattr(p.instrument, "lot_size", 1) or 1))
            lots = qty_abs / _to_decimal(lot_size)
            action = "BUY" if p.quantity >= 0 else "SELL"
            # Derive CE/PE from the symbol so the resolver applies the admin's
            # per-side option overrides (Opt Buy/Sell Fixed ₹/lot etc.). With
            # option_type=None the resolver ignores them and recomputes margin
            # off the generic segment Times/% — under-charging option edits.
            _esym = (p.instrument.symbol or "").upper()
            _eotype = (
                ("CE" if _esym.endswith("CE") else "PE" if _esym.endswith("PE") else None)
                if len(_esym) >= 3 and _esym[-3].isdigit()
                else None
            )
            resolved = await get_effective_settings(
                p.user_id,
                p.instrument.segment,
                action=action,
                option_type=_eotype,
                product_type=p.product_type.value,
                symbol=p.instrument.symbol,
            )
            s = (resolved or {}).get("settings") or {}
            fixed_per_lot = _to_decimal(s.get("fixed_margin_per_lot") or 0)
            if (s.get("margin_calc_mode") == "fixed") and fixed_per_lot > 0:
                new_margin = lots * fixed_per_lot
            else:
                margin_pct = _to_decimal(s.get("margin_percentage") or 100.0) / _to_decimal(100)
                leverage = _to_decimal(s.get("leverage") or 1.0) or _to_decimal(1)
                new_margin = qty_abs * ref_price * margin_pct / leverage
            # USD-quoted segments lock in INR (skip for fixed ₹/lot mode).
            if _is_usd_quoted_segment(p.segment_type) or _is_usd_quoted_segment(p.instrument.segment):
                if not ((s.get("margin_calc_mode") == "fixed") and fixed_per_lot > 0):
                    new_margin = new_margin * _to_decimal(_get_usd_inr_rate())
            new_margin = _quantize_money(new_margin)
        except Exception:
            # Resolver failure must NEVER fall back to the raw notional —
            # leave margin untouched so a transient error can't drain the
            # wallet, and skip the re-balance this time.
            logger.exception("admin_edit_position_margin_recompute_failed", extra={"pos_id": str(p.id)})
            new_margin = prev_margin

        margin_delta = new_margin - prev_margin
        # If the edit RAISES the requirement, the user must have the funds to
        # back the extra exposure. Reject UP-FRONT (before the save) so the
        # admin gets a clear "insufficient margin" popup and the position
        # stays unchanged — no half-edited row, no silent over-leverage.
        if margin_delta > 0:
            _w = await _ws_chk.get_or_create(p.user_id)
            _avail = _to_decimal(_w.available_balance) + _to_decimal(_w.credit_limit)
            if _avail < margin_delta:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"User ke paas itni margin nahi hai — is edit ke liye "
                        f"₹{margin_delta:.2f} aur chahiye, par available sirf "
                        f"₹{_avail:.2f} hai. Qty kam karein ya user ke wallet "
                        f"me funds add karein."
                    ),
                )
        p.margin_used = Decimal128(str(new_margin))

    await p.save()

    # ── Sync underlying Trade fills on a CLOSED price edit ────────────
    # The user's Closed blotter is rebuilt FIFO from Trade fills, so an
    # admin open/close-price correction on the Position doc alone never
    # reached the user's history (admin view changed, user view didn't —
    # operator-flagged). Push the new prices + recomputed P&L onto the
    # matching fills. Best-effort: a targeting miss must not fail the edit
    # (the Position + wallet REVERSAL are already correct), so log + go on.
    if resync_closed_fills:
        from app.services import position_service as _ps_fills

        try:
            n = await _ps_fills.resync_closed_position_fills(p)
            logger.info(
                "admin_edit_position_fills_resynced",
                extra={"pos_id": str(p.id), "fills_updated": n},
            )
        except Exception:
            logger.exception(
                "admin_edit_position_fills_resync_failed", extra={"pos_id": str(p.id)}
            )

    # ── Re-balance the wallet + resync derived state on an OPEN qty/avg edit
    # Move the margin delta on the wallet (block when the requirement grew,
    # release when it shrank) so MARGIN USED / available actually reflect the
    # new exposure — not just the per-position field. Then resync the lot
    # tracker (so the edited qty shows everywhere, not only the P&L) and the
    # floating P&L (so the strip is correct immediately, not next tick).
    if margin_delta is not None and margin_delta != 0:
        from app.services import wallet_service as _ws2

        try:
            if margin_delta > 0:
                await _ws2.block_margin(p.user_id, margin_delta)
            else:
                await _ws2.release_margin(p.user_id, -margin_delta)
        except Exception:
            logger.exception(
                "admin_edit_position_wallet_rebalance_failed", extra={"pos_id": str(p.id)}
            )
    if "quantity" in payload and p.status == PositionStatus.OPEN:
        from app.services import market_data_service as _mds_edit
        from app.services import position_service as _ps_edit

        try:
            await _ps_edit._recompute_tracker(
                user_id=p.user_id, segment_type=p.segment_type, token=p.instrument.token
            )
        except Exception:
            logger.exception(
                "admin_edit_position_tracker_resync_failed", extra={"pos_id": str(p.id)}
            )
        try:
            _ltp = await _mds_edit.get_ltp(p.instrument.token)
            await _ps_edit.refresh_unrealized_pnl(p, _ltp)
            await p.save()
        except Exception:
            logger.exception(
                "admin_edit_position_pnl_refresh_failed", extra={"pos_id": str(p.id)}
            )

    # Apply the wallet delta AFTER the position write so an exception in
    # adjust() doesn't leave the position in an inconsistent state. The
    # adjust() call writes its own REVERSAL ledger row.
    if realized_delta is not None and realized_delta != 0:
        from app.models.transaction import TransactionType
        from app.services import wallet_service as _ws

        try:
            await _ws.adjust(
                p.user_id,
                realized_delta,
                transaction_type=TransactionType.REVERSAL,
                narration=(
                    f"Admin {admin.user_code} corrected realised P&L on "
                    f"{p.instrument.symbol} (delta {realized_delta})"
                ),
                reference_type="Position",
                reference_id=str(p.id),
                actor_id=admin.id,
            )
        except Exception as e:
            # The position row is already saved with the new realized.
            # Surface the wallet failure so the operator knows the
            # ledger didn't catch up.
            raise HTTPException(
                status_code=500,
                detail=f"Position updated but wallet reversal failed: {e}",
            )

    new_values: dict[str, Any] = {
        "avg_price": str(p.avg_price),
        "close_price": str(p.ltp) if p.ltp is not None else None,
        "quantity": p.quantity,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        "stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
        "target": str(p.target) if p.target is not None else None,
        "realized_pnl": str(p.realized_pnl) if p.realized_pnl is not None else None,
        "close_reason": p.close_reason,
        "status": p.status.value,
    }
    # Stash the instrument identity + sizing in metadata so the Admin
    # Actions → Edited Positions audit can name WHICH position was edited
    # (the page showed "—" for symbol because nothing recorded it). qty
    # here is the position's opening size — the absolute lot count behind
    # the edit, independent of the signed `quantity` before/after fields.
    edit_metadata: dict[str, Any] = {
        "symbol": p.instrument.symbol,
        "trading_symbol": getattr(p.instrument, "trading_symbol", None) or p.instrument.symbol,
        "exchange": str(p.instrument.exchange),
        "segment": p.instrument.segment,
        "product_type": p.product_type.value,
        "opened_side": str(getattr(p, "opened_side", None) or ""),
        "opening_quantity": p.opening_quantity if p.opening_quantity is not None else abs(p.quantity or 0),
        "lot_size": getattr(p.instrument, "lot_size", None),
    }
    await log_event(
        action=AuditAction.POSITION_EDIT,
        entity_type="Position",
        entity_id=p.id,
        actor_id=admin.id,
        target_user_id=p.user_id,
        old_values=old_values,
        new_values=new_values,
        metadata=edit_metadata,
    )
    await _publish_position_event(p.user_id, "edit", p, {"by": "admin"})
    return APIResponse(data={"id": str(p.id), "status": p.status.value, **new_values})


@router.post("/positions/{position_id}/reopen", response_model=APIResponse[dict])
async def admin_reopen_position(
    position_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("trading_view", "write")),
):
    """Flip a CLOSED position back to OPEN — admin override.

    Used when a close was triggered by mistake (false stop-out, user
    misclicked, bracket fired on a phantom tick). The endpoint:

      1) Reverses the cumulative `realized_pnl` against the user's
         wallet via a REVERSAL transaction. A profit close that's
         being undone debits the wallet; a loss close credits it
         back. Either way, the running wallet balance ends at what
         it was just before the close fill landed.

      2) Rehydrates the position to its OPEN state:
            status        = OPEN
            quantity      = ±opening_quantity (sign from opened_side)
            closed_at     = None
            close_reason  = None
            realized_pnl  = 0
            margin_used   = abs(qty) × avg_price  (re-block)

      3) Re-blocks the now-required margin on the wallet so used_margin
         reflects the reopened exposure.

      4) Refuses to reopen if a different OPEN position already exists
         for the same (user, token, product_type) — that would create
         two parallel positions the apply_fill resolver can't pick
         between.

    Audit-logged + pub/sub fanned out so the user's terminal re-renders
    the Positions tab live.
    """
    p = await Position.get(_pos_oid(position_id))
    if p is None:
        raise HTTPException(status_code=404, detail="Position not found")
    await assert_user_in_scope(admin, p.user_id)

    if p.status != PositionStatus.CLOSED:
        raise HTTPException(
            status_code=400, detail="Only CLOSED positions can be reopened"
        )

    # Refuse if a parallel OPEN position exists for the same
    # (user, token, product_type) — apply_fill assumes one OPEN row per
    # such tuple. Reopening on top of that would create two and break
    # downstream fills.
    existing_open = await Position.find_one(
        Position.user_id == p.user_id,
        Position.instrument.token == p.instrument.token,  # type: ignore[union-attr]
        Position.product_type == p.product_type,
        Position.status == PositionStatus.OPEN,
    )
    if existing_open is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot reopen — a different open position already exists "
                f"for {p.instrument.symbol} ({p.product_type.value}). "
                f"Square that off first."
            ),
        )

    from decimal import Decimal as _Decimal

    from app.models.transaction import TransactionType
    from app.services import position_service as _ps
    from app.services import wallet_service as _ws
    from app.utils.decimal_utils import to_decimal as _td
    from app.utils.time_utils import now_utc as _now_utc

    # ── 1) Wallet reversal of the realised P&L ──────────────────────
    # When the original close booked into `settlement_outstanding`
    # (because the user's wallet didn't have enough to cover the full
    # loss — auto_settlement=ON flow in wallet_service.adjust), the
    # original deduction was SPLIT: cash portion drained `available_
    # balance`, shortfall went to `settlement_outstanding`. A naive
    # reverse-the-full-realized restored ONLY the available leg and
    # left the settlement debt hanging, double-counting the shortfall
    # against the user. Operator-flagged 22-May: CL35171433 closed at
    # ₹20,200 loss (₹12,617 cash + ₹7,583 settlement) → reopen credited
    # full ₹20,200 to wallet but settlement stayed at ₹7,583 — user
    # got the shortfall amount as a hidden second refund.
    #
    # Fix: look up the SETTLEMENT_OUTSTANDING_BOOKED transaction(s)
    # that were written when this position's close path ran. Sum the
    # booked amounts, reduce settlement_outstanding by that total,
    # and reverse only the (realized - booked) portion back into
    # available_balance.
    realized = _td(p.realized_pnl or 0)
    if realized != _Decimal("0"):
        from app.models.transaction import WalletTransaction, TransactionStatus
        from app.models.wallet import Wallet as _Wallet
        from bson import Decimal128 as _Dec128

        booked_total = _Decimal("0")
        # Find SETTLEMENT_OUTSTANDING_BOOKED txns that match this
        # position's close. The booking inherits reference_type="ORDER"
        # (the close order id) — we don't have a direct Position link,
        # so match by user + time window around closed_at + symbol in
        # narration. Tolerant of small clock skew (±10s).
        from datetime import timedelta as _td_delta
        try:
            if p.closed_at is not None:
                lo = p.closed_at - _td_delta(seconds=10)
                hi = p.closed_at + _td_delta(seconds=10)
                booked_rows = await WalletTransaction.find(
                    WalletTransaction.user_id == p.user_id,
                    WalletTransaction.transaction_type == TransactionType.SETTLEMENT_OUTSTANDING_BOOKED,
                    WalletTransaction.status == TransactionStatus.COMPLETED,
                    WalletTransaction.created_at >= lo,
                    WalletTransaction.created_at <= hi,
                ).to_list()
                sym = p.instrument.symbol
                for r in booked_rows:
                    narr = (r.narration or "")
                    if sym not in narr:
                        continue
                    # Stored amount is negative (debit-style for the user).
                    # We add the magnitude back when computing the unwind.
                    booked_total += abs(_td(r.amount or 0))
        except Exception:
            logger.exception("reopen_lookup_settlement_failed", extra={"pos_id": str(p.id)})
            booked_total = _Decimal("0")

        # Reduce settlement_outstanding first (atomic-ish — wallet save
        # before the cash reversal so a mid-flow crash never doubles
        # the user's available_balance).
        if booked_total > _Decimal("0"):
            try:
                wallet_doc = await _ws.get_or_create(p.user_id)
                cur_settle = _td(wallet_doc.settlement_outstanding or 0)
                new_settle = max(_Decimal("0"), cur_settle - booked_total)
                wallet_doc.settlement_outstanding = _Dec128(str(new_settle))
                wallet_doc.version = (wallet_doc.version or 0) + 1
                await wallet_doc.save()
                # Audit-bearing ledger row so the trail explains why
                # settlement_outstanding dropped without a deposit
                # arriving. balance_before == balance_after — this
                # transaction only touches the settlement field.
                avail_str = str(wallet_doc.available_balance)
                await WalletTransaction(
                    user_id=p.user_id,
                    transaction_type=TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY,
                    amount=_Dec128(str(booked_total)),
                    balance_before=_Dec128(avail_str),
                    balance_after=_Dec128(avail_str),
                    reference_type="Position",
                    reference_id=str(p.id),
                    narration=(
                        f"Reopen {p.instrument.symbol} — settlement unbooked "
                        f"(₹{booked_total} was originally shortfall on the "
                        f"closing leg; reversing back so the cash refund "
                        f"doesn't double-credit the user)"
                    ),
                    status=TransactionStatus.COMPLETED,
                    created_by=admin.id,
                ).insert()
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Settlement-outstanding unwind failed; reopen aborted: {e}",
                )

        # Cash refund = full wallet impact of the closing trade, reversed.
        # The close booked TWO wallet transactions:
        #   1. CHARGES: -brokerage (always a debit regardless of P&L direction)
        #   2. PNL:     raw_realized (negative = loss, positive = profit)
        # For a loss-close the PNL debit may have been split between cash and
        # settlement_outstanding (handled by booked_total above). So:
        #   cash_refund = raw_realized + booked_total   (PNL cash portion)
        #               - closing_brokerage             (brokerage refund)
        # Negating cash_refund gives the wallet credit:
        #   e.g. realized=-392, booked=0, brokerage=210
        #        cash_refund = -392 + 0 - 210 = -602 → -(-602) = +602 ✓
        # Brokerage MUST be included: pnl_summary._realised_in sums
        # Trade.pnl_inr (already NET of brokerage) and offsets it with
        # the REVERSAL amount — if REVERSAL = only raw_pnl, brokerage
        # stays counted in the weekly card even after the trade is undone.
        closing_brokerage = _Decimal("0")
        try:
            from app.models.trade import Trade as _TradeModel
            ct = await _TradeModel.find_one(
                {
                    "user_id": p.user_id,
                    "instrument.token": p.instrument.token,
                    "pnl_inr": {"$ne": None},
                    "executed_at": {
                        "$gte": p.closed_at - _td_delta(seconds=15),
                        "$lte": p.closed_at + _td_delta(seconds=15),
                    },
                }
            )
            if ct is not None:
                closing_brokerage = _td(ct.total_charges or ct.brokerage or 0)
        except Exception:
            pass

        cash_refund = (realized + booked_total if realized < 0 else realized) - closing_brokerage
        if cash_refund != _Decimal("0"):
            try:
                await _ws.adjust(
                    p.user_id,
                    -cash_refund,
                    transaction_type=TransactionType.REVERSAL,
                    narration=(
                        f"Reopen {p.instrument.symbol} — reverse close P&L + brokerage "
                        f"(closed by {p.close_reason or 'unknown'}; reopened by "
                        f"{admin.user_code})"
                        + (f" [PnL cash ₹{abs(realized + booked_total if realized < 0 else realized)}"
                           f", brokerage ₹{closing_brokerage}"
                           + (f", settlement ₹{booked_total} unwound separately" if booked_total > _Decimal("0") else "")
                           + "]")
                    ),
                    reference_type="Position",
                    reference_id=str(p.id),
                    actor_id=admin.id,
                )
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Wallet reversal failed; reopen aborted: {e}",
                )

    # ── 2) Restore the position to OPEN state ───────────────────────
    # Reconstruct quantity from the snapshot we took at open. If
    # `opened_side` is missing (legacy rows), infer from the sign of
    # the last non-zero quantity by reading the latest Trade row.
    opening_qty = float(p.opening_quantity or 0) or abs(float(p.quantity or 0))
    if opening_qty <= 0:
        raise HTTPException(
            status_code=400,
            detail="Cannot reopen — original opening quantity is unknown",
        )
    sign = 1
    if p.opened_side is not None:
        sign = 1 if str(p.opened_side.value).upper() == "BUY" else -1

    # ── Supersede the closing fills this reopen undoes ──────────────────
    # The user Closed blotter is rebuilt from Trade rows, so the closes of
    # the cycle we're about to undo must be flagged or they'd keep showing
    # (operator: "reopen karta hu fir bhi user history me dikhta rehta hai").
    # Window = THIS cycle only: [cycle_start, closed_at]. cycle_start is the
    # previous reopen (or the open), so the span is tight — a different
    # position on the SAME token that closed later (e.g. a parallel stop-out)
    # falls outside and keeps its row. Only closing legs (pnl_inr set) are
    # flagged; opening fills stay valid for FIFO pairing. The +3s upper slack
    # absorbs the tiny skew between Trade.executed_at and Position.closed_at
    # without reaching the next same-token close.
    try:
        from datetime import timedelta as _td_sup
        from app.models.trade import Trade as _TradeSup
        _cyc_start = p.reopened_at or p.opened_at
        if p.closed_at is not None:
            _sup_q: dict[str, Any] = {
                "user_id": p.user_id,
                "instrument.token": p.instrument.token,
                "product_type": p.product_type.value,
                "pnl_inr": {"$ne": None},
                "executed_at": {"$lte": p.closed_at + _td_sup(seconds=3)},
            }
            if _cyc_start is not None:
                _sup_q["executed_at"]["$gte"] = _cyc_start - _td_sup(seconds=1)
            await _TradeSup.get_motor_collection().update_many(
                _sup_q, {"$set": {"superseded_by_reopen": True}}
            )
    except Exception:
        logger.exception("reopen_supersede_trades_failed", extra={"pos_id": str(p.id)})

    p.status = PositionStatus.OPEN
    p.quantity = opening_qty * sign
    p.closed_at = None
    p.close_reason = None
    p.realized_pnl = Decimal128("0")
    if p.close_usd_inr_rate is not None:
        p.close_usd_inr_rate = None

    # ── Margin re-block — read the ORIGINAL opening order's
    # `margin_blocked` instead of recomputing.
    #
    # Previously this branch set `margin_used = opening_qty × avg_price`
    # which is the NOTIONAL value of the position — for futures /
    # options that's 5-20× more than the actual margin admin's
    # netting settings would lock. Production hit on 21-May:
    # reopening a CGPOWER26MAYFUT 340-lot @ 865.90 set margin_used
    # to ₹2,94,406 (340 × 865.90), drained the user's available_balance
    # to −₹2,79,422.
    #
    # The fix: query the most-recent EXECUTED opening order for this
    # (user, token, product_type) and reuse its `margin_blocked` —
    # that's what `order_validator.validate` computed at the original
    # open time using the correct percent / times / fixed margin
    # formula via netting_service. If multiple orders contributed to
    # the position (pyramid), sum their margin_blocked values up to
    # the opening_quantity. Falls back to 0 if no opening orders
    # exist (very rare; position created directly without going
    # through place_order) — operator can use the existing
    # `recompute-wallet-margin` endpoint to repair.
    try:
        from app.models.order import OrderStatus

        opening_orders = (
            await Order.find(
                Order.user_id == p.user_id,
                Order.instrument.token == p.instrument.token,  # type: ignore[union-attr]
                Order.product_type == p.product_type,
                Order.action == p.opened_side,
                Order.status == OrderStatus.EXECUTED,
            )
            .sort("-executed_at")
            .limit(10)
            .to_list()
        )
        total_margin = _Decimal("0")
        qty_covered = _Decimal("0")
        target_qty = _Decimal(str(opening_qty))
        for o in opening_orders:
            if qty_covered >= target_qty:
                break
            order_qty = _Decimal(str(getattr(o, "quantity", 0) or 0))
            order_margin = _td(o.margin_blocked or 0)
            if order_qty <= 0 or order_margin <= 0:
                continue
            need = target_qty - qty_covered
            if order_qty <= need:
                total_margin += order_margin
                qty_covered += order_qty
            else:
                # Partial slice of this order's margin.
                total_margin += order_margin * need / order_qty
                qty_covered = target_qty
        if total_margin > 0:
            p.margin_used = Decimal128(str(total_margin))
        else:
            # No opening orders found — set to 0 and let the
            # downstream `recompute_used_margin` reconcile from the
            # canonical Position docs. Better to under-block than to
            # accidentally double-lock notional.
            p.margin_used = Decimal128("0")
    except Exception:  # pragma: no cover
        # Defensive — never let a margin recompute failure block the
        # reopen itself. The next reconcile pass cleans up.
        p.margin_used = Decimal128("0")

    # Stamp reopened_at so _charges_for uses this as the Trade window
    # start instead of opened_at — prevents brokerage from accumulating
    # across multiple close/reopen/close cycles.
    p.reopened_at = _now_utc()

    await p.save()

    # ── 3) Re-block the margin on the wallet so used_margin reflects
    #       the restored exposure. block_margin handles the
    #       insufficient-funds case but for an admin override we want
    #       the position to come back even if the user is short on
    #       margin — so swallow that and let the operator reconcile.
    try:
        await _ws.block_margin(p.user_id, _td(p.margin_used or 0))
    except Exception:
        pass

    # Tracker recompute (intraday / holding lots).
    try:
        await _ps._recompute_tracker(
            user_id=p.user_id,
            segment_type=p.segment_type,
            token=p.instrument.token,
        )
    except Exception:
        pass

    await log_event(
        action=AuditAction.POSITION_REOPEN,
        entity_type="Position",
        entity_id=p.id,
        actor_id=admin.id,
        target_user_id=p.user_id,
        metadata={
            "reversed_realized_pnl": str(realized),
            "restored_quantity": opening_qty * sign,
        },
    )
    # Best-effort realtime nudge — reopen + wallet reversal are already
    # committed, so a pub/sub hiccup must never 500 a done reopen (same guard
    # as delete_position; redis publish also no-ops when Redis isn't ready).
    try:
        await _publish_position_event(p.user_id, "reopen", p, {"by": "admin"})
    except Exception:
        logger.exception("reopen_publish_failed", extra={"pos_id": str(p.id)})

    return APIResponse(
        data={
            "id": str(p.id),
            "status": p.status.value,
            "quantity": p.quantity,
            "realized_pnl_reversed": str(realized),
        }
    )


import time as _admin_pnl_time

# Short-TTL cache for the (very heavy) admin pnl-summary. For a super-admin the
# scope is EVERY client, so each call ran 3 windowed Trade aggregations + an
# open-position LTP fan-out across the whole book — and the Dashboard /
# Positions / Orders pages all poll it every 10 s. A 2 s TTL collapses
# concurrent hits into one compute. Keyed by (admin, user_id filter) so a
# filtered view and the all-users view never share a result. Live per-row M2M
# in the positions table is recomputed separately, so this only affects the
# aggregate header tiles (which poll anyway).
_ADMIN_PNL_CACHE: dict[str, tuple[float, dict]] = {}
_ADMIN_PNL_TTL = 2.0


@router.get("/positions/pnl-summary", response_model=APIResponse[dict])
async def positions_pnl_summary(
    admin: CurrentAdmin,
    user_id: str | None = None,
    own_scope: bool = Query(default=False, description="SUPER_ADMIN only: restrict to the super-admin's OWN direct users"),
    _: None = Depends(require_perm("trading_view", "read")),
):
    """Aggregate PnL windows for the admin dashboard cards.

    today_pnl    — sum of realised P&L from trades + unrealised on open
                   positions, since IST midnight.
    week_pnl     — same, since the most recent IST Sunday 00:00.
    last_week_pnl — total realised P&L of the previous Sun→Sat window.

    `user_id` (optional) narrows the aggregate to a single user's
    positions only — passed by the admin Positions page when a user
    filter is active so the tile matches the filtered table.
    """
    # Short-TTL cache hit — skip the heavy whole-book recompute.
    _ck = f"{admin.id}:{user_id or ''}:{'own' if own_scope else ''}"
    _hit = _ADMIN_PNL_CACHE.get(_ck)
    if _hit is not None and _hit[0] > _admin_pnl_time.monotonic():
        return APIResponse(data=_hit[1])

    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    IST = _tz(_td(hours=5, minutes=30))
    now_ist = _dt.now(IST)
    today_start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    # Monday-anchored week — MUST match the Accounts dashboard / P&L-sharing
    # report (both use `now - weekday`, Mon=0). Sunday-anchoring here made the
    # Positions cards span a different 7-day window than Accounts, so the two
    # pages showed different "This Week" numbers for the same book.
    days_back = now_ist.weekday()
    week_start_ist = today_start_ist - _td(days=days_back)
    last_week_start_ist = week_start_ist - _td(days=7)
    last_week_end_ist = week_start_ist  # exclusive
    # Full Mon→Sun span for the card label (data still runs only to `now`, but
    # the header shows the whole week like Accounts' "17 Aug – 23 Aug").
    week_end_ist = week_start_ist + _td(days=6)
    last_week_end_disp_ist = last_week_start_ist + _td(days=6)

    def _fmt_range(a: _dt, b: _dt) -> str:
        return f"{a.day} {a.strftime('%b')} – {b.day} {b.strftime('%b')}"

    today_start = today_start_ist.astimezone(_tz.utc)
    week_start = week_start_ist.astimezone(_tz.utc)
    last_week_start = last_week_start_ist.astimezone(_tz.utc)
    last_week_end = last_week_end_ist.astimezone(_tz.utc)

    # Realised P&L lives on each Position (set on SELL closes/flips). We sum
    # across positions whose closed_at OR updated_at falls in the window —
    # covers fully-closed and partially-closed-but-still-open positions in
    # one query (positions that closed in window have closed_at set; ones
    # still open with realised slices booked have updated_at in window).
    #
    # FX: realized_pnl + unrealized_pnl are stored in NATIVE currency. For
    # USD-quoted (crypto/forex) we convert to INR via the locked open rate
    # (realised) or live rate (unrealised) — same logic as _pos() view.
    current_usd_inr = market_data_service.get_usd_inr_rate()

    def _is_usd(p: Position) -> bool:
        return market_data_service.is_usd_quoted_segment(p.segment_type) or \
            market_data_service.is_usd_quoted_segment(p.instrument.segment)

    def _realised_inr(p: Position) -> float:
        raw = float(str(p.realized_pnl))
        if not _is_usd(p):
            return raw
        rate = (
            float(str(p.open_usd_inr_rate))
            if p.open_usd_inr_rate is not None
            else current_usd_inr
        )
        return raw * rate

    # Scope user pool for sub-admins. None for SUPER_ADMIN = no filter.
    # own_scope (super-admin Positions page): the super-admin's OWN direct
    # users only — never any sub-admin's clients. A per-user drill-down
    # (user_id) still takes precedence over own_scope below.
    if own_scope and admin.role == UserRole.SUPER_ADMIN and not user_id:
        scope = await super_admin_direct_user_ids()
    else:
        scope = await scoped_user_ids(admin)

    # Optional per-user narrowing — used by the admin Positions page when
    # a user filter is active so the dashboard cards match the table.
    # We intersect with `scope` so a sub-admin can't query users outside
    # their pool by guessing the user_id.
    user_filter_oid: PydanticObjectId | None = None
    if user_id:
        try:
            user_filter_oid = PydanticObjectId(user_id)
        except Exception:
            user_filter_oid = None
        if user_filter_oid is not None:
            if scope is not None and user_filter_oid not in scope:
                # Out of scope → empty tile data (sub-admin probing
                # a user_id outside their pool). Shape matches the
                # normal return so the frontend never sees `undefined`.
                return APIResponse(
                    data={
                        "today_pnl": 0.0,
                        "today_realised": 0.0,
                        "open_unrealised": 0.0,
                        "week_pnl": 0.0,
                        "week_realised": 0.0,
                        "last_week_pnl": 0.0,
                    }
                )
            scope = [user_filter_oid]

    # Sum charges (brokerage + other) across all trades that belong to a
    # given position. Mirrors the per-row attribution in the /positions
    # endpoint so the aggregate tile and the per-row table never disagree.
    # Without this, the dashboard's "This Week's Closed PNL" stayed at
    # gross while every trade card on the APK showed net — the difference
    # equalled the brokerage bill for the week.
    from datetime import timedelta as _td_sum

    async def _charges_for_positions(positions: list[Position]) -> dict[str, float]:
        if not positions:
            return {}
        user_ids = list({p.user_id for p in positions})
        tokens = list({p.instrument.token for p in positions})
        oldest = min((p.opened_at for p in positions if p.opened_at), default=None)
        tq: dict[str, Any] = {
            "user_id": {"$in": user_ids},
            "instrument.token": {"$in": tokens},
        }
        if oldest is not None:
            tq["executed_at"] = {"$gte": oldest - _td_sum(seconds=5)}
        trades = await Trade.find(tq).sort("+executed_at").to_list()
        bucket: dict[tuple[str, str, str], list[Trade]] = {}
        for t in trades:
            bucket.setdefault(
                (str(t.user_id), t.instrument.token, t.product_type.value), []
            ).append(t)
        slack = _td_sum(seconds=5)
        out: dict[str, float] = {}
        for p in positions:
            key = (str(p.user_id), p.instrument.token, p.product_type.value)
            ts = bucket.get(key, [])
            if not ts:
                out[str(p.id)] = 0.0
                continue
            if not p.opened_at:
                out[str(p.id)] = sum(
                    float(str(getattr(t, "total_charges", None) or t.brokerage or 0))
                    for t in ts
                )
                continue
            cycle_start = p.reopened_at or p.opened_at
            pos_open = p.opened_at
            pos_end = p.closed_at  # None = OPEN, no upper bound for opening fills
            total = 0.0
            for t in ts:
                charge = float(str(getattr(t, "total_charges", None) or t.brokerage or 0))
                if getattr(t, "pnl_inr", None) is not None:
                    if t.executed_at < cycle_start - slack:
                        continue
                    if pos_end is not None and t.executed_at > pos_end + slack:
                        continue
                    total += charge
                else:
                    if t.executed_at < pos_open - slack:
                        continue
                    if pos_end is not None and t.executed_at > pos_end + slack:
                        continue
                    total += charge
            out[str(p.id)] = total
        return out

    async def _realised_in(window_start, window_end=None):
        # CORRECT source for window realized P&L = Trade.pnl_inr summed
        # over Trade.executed_at in the window. Trade.pnl_inr is the
        # per-fill closing P&L frozen at execution time (matching engine
        # writes it on the closing leg), so it's a true DELTA per event.
        #
        # The earlier implementation queried Position rows where
        # `closed_at OR updated_at` fell in the window and summed
        # Position.realized_pnl. That cumulative field plus the
        # updated_at branch caused massive over-counting: every open
        # position's updated_at refreshes on every tick / risk-enforcer
        # cycle / SL-TP edit, which dragged its FULL running realized
        # (booked weeks earlier) into "This Week's Net P&L". A 67-open
        # admin pool was showing ₹-17.7 lakh for a 2-day window.
        rng: dict[str, Any] = {"$gte": window_start}
        if window_end is not None:
            rng["$lt"] = window_end
        # Filter out bogus zero-priced fills.  Before the matching engine
        # got the STALE_FEED guard, a stale Zerodha WS could push LTP = 0
        # into get_ltp() and a market order would execute at ₹0.00, booking
        # a phantom loss equal to the entire notional.  Those rows are
        # still in `trades` for the audit trail but they MUST NOT
        # contribute to the admin's headline P&L — they aren't real
        # market activity.  Same exclusion keeps settlement_outstanding
        # off the card too: settlement is tracked on Wallet, not Trade,
        # so dropping zero-priced trades from the realised sum is the
        # only filter needed here.
        # Trade.pnl_inr is already NET of closing brokerage — matching_engine
        # stores pnl_inr_dec = raw_realized - brokerage. Do NOT subtract
        # total_charges again (that double-counts brokerage).
        #
        # REOPEN / DELETE handling — `superseded_by_reopen`:
        # When an admin reopens or deletes a closed position, every fill of the
        # undone close is flagged `superseded_by_reopen=True`. Excluding those
        # fills here drops the reopened/deleted position's P&L **and** brokerage
        # straight out of the card (pnl_inr is net of brokerage, so one filter
        # removes both). This REPLACES the old `gross + REVERSAL/RECOVERY
        # correction` approach, which over-corrected on CROSS-WEEK reopens — a
        # position closed last week (loss in last week's gross) and reopened
        # this week added a phantom +reversal to THIS week's card with no
        # matching loss (operator: "card me se nahi hatta"). Superseded-
        # exclusion nets reopens/deletes cleanly with no window mismatch, and
        # it's the SAME rule reports.py + accounts_dashboard already use, so
        # every page now agrees. The reopen/delete WALLET reversal is untouched.
        query: dict[str, Any] = {
            "executed_at": rng,
            "pnl_inr": {"$ne": None},
            "price": {"$gt": 0},
            "superseded_by_reopen": {"$ne": True},
        }
        if scope is not None:
            if not scope:
                return 0.0
            query["user_id"] = {"$in": scope}
        trades = await Trade.find(query).to_list()
        gross = sum(float(str(t.pnl_inr)) for t in trades if t.pnl_inr is not None)

        from app.models.transaction import (  # noqa: PLC0415
            WalletTransaction as _WT,
            TransactionType as _TT,
        )

        # Settlement PnL — weekly settlement books directly to wallet via
        # wallet_service.adjust(TransactionType.PNL, reference_type="POSITION_SETTLEMENT").
        # No Trade record is created, so it never appears in `gross` above.
        # Add it here so the card shows the TRUE net including settlement booking.
        settle_q: dict[str, Any] = {
            "transaction_type": _TT.PNL.value,
            "reference_type": "POSITION_SETTLEMENT",
            "created_at": {"$gte": window_start},
        }
        if window_end is not None:
            settle_q["created_at"]["$lt"] = window_end
        if scope is not None:
            settle_q["user_id"] = {"$in": scope}
        settle_txns = await _WT.find(settle_q).to_list()
        settlement_pnl = sum(float(str(t.amount)) for t in settle_txns)

        return gross + settlement_pnl

    today_realised = await _realised_in(today_start)
    week_realised = await _realised_in(week_start)
    last_week_realised = await _realised_in(last_week_start, last_week_end)

    # Recompute unrealised LIVE per position rather than reading the stored
    # `p.unrealized_pnl` field — that field is only refreshed when the
    # position is touched (new fill, partial close, manual edit). For an
    # open position sitting idle between fills the stored number is stale
    # (often 0 on a freshly opened position), which is what made the
    # admin's "Open PNL" card stick at ₹0.00 while the per-row M2M column
    # showed the correct live number. Mirror the /positions list view's
    # (ltp - avg) * qty math so both reads stay in lockstep.
    open_q: dict[str, Any] = {"status": PositionStatus.OPEN.value}
    if scope is not None:
        if not scope:
            open_positions: list[Position] = []
        else:
            open_q["user_id"] = {"$in": scope}
            open_positions = await Position.find(open_q).to_list()
    else:
        open_positions = await Position.find(open_q).to_list()

    # Parallel LTP fan-out (see /admin/positions for rationale). This
    # endpoint is hit by the Dashboard, Positions, and Orders pages every
    # 10 s, so the old serial loop multiplied across N open positions was
    # adding seconds of blank time to every admin navigation.
    unique_tokens = list({p.instrument.token for p in open_positions if p.quantity != 0})
    ltp_results = await asyncio.gather(
        *[market_data_service.get_display_ltp(tok) for tok in unique_tokens],
        return_exceptions=True,
    )
    ltp_map: dict[str, float | None] = {}
    for tok, res in zip(unique_tokens, ltp_results):
        if isinstance(res, BaseException):
            ltp_map[tok] = None  # signal "feed hiccup" → fall back to stored
            continue
        try:
            ltp_map[tok] = float(res)
        except Exception:
            ltp_map[tok] = None

    total_unrealised = 0.0
    for p in open_positions:
        if p.quantity == 0:
            continue
        ltp_f = ltp_map.get(p.instrument.token)
        if ltp_f is None or ltp_f <= 0:
            # Feed hiccup OR a zero / non-positive mark (closed market,
            # stale cache, failed fetch). NEVER compute (ltp - avg) * qty
            # against a 0 LTP — that returns the WHOLE notional as a phantom
            # loss. On 19-Jun this made CL20371190's "This Week's Net P&L"
            # read -₹3.39 Cr (≈ the sum of his open positions' notional) once
            # the F&O feed went flat after market close, because get_ltp
            # handed back 0 (not None, so the old `is None` guard missed it).
            # Mirror refresh_unrealized_pnl's zero-mark guard: fall back to
            # the last stored unrealised so the card never blows up.
            stored = float(str(p.unrealized_pnl))
            total_unrealised += stored * (current_usd_inr if _is_usd(p) else 1.0)
            continue
        avg = float(str(p.avg_price))
        raw = (ltp_f - avg) * p.quantity
        if _is_usd(p):
            raw *= current_usd_inr
        total_unrealised += raw

    # ── Accounts-dashboard parity ───────────────────────────────────────
    # The two Net-P&L cards must show the SAME Net Client PNL / Brokerage /
    # Total-of-Both as /accounts-dashboard for the identical Mon→Sun window.
    # Reuse the dashboard's own aggregator over its own pool resolver so the
    # numbers can't drift: whole-book uses `_entity_pool_ids(admin)` (exactly
    # what the "All Users" KPI card queries via compute_broker_totals); a
    # user-filtered view narrows to that single client. Both pools are
    # demo-free + non-CLOSED, so no demo data leaks in.
    from app.services.accounts_dashboard_service import (  # noqa: PLC0415
        client_totals_for_users as _client_totals,
        _entity_pool_ids as _acct_pool,
    )

    if user_filter_oid is not None:
        _acct_ids = [user_filter_oid]
    elif own_scope and admin.role == UserRole.SUPER_ADMIN:
        # Super-admin Positions page: own direct users only (same pool as the
        # table above), so the Total-of-Both cards match that filtered book.
        _acct_ids = await super_admin_direct_user_ids()
    else:
        _acct_ids = await _acct_pool(admin.id, admin.role.value)
    _wk_tot = await _client_totals(_acct_ids, week_start, None)
    _lw_tot = await _client_totals(_acct_ids, last_week_start, last_week_end)

    def _f(d) -> float:
        return round(float(d), 2)

    _data = {
        "today_pnl": round(today_realised + total_unrealised, 2),
        "today_realised": round(today_realised, 2),
        "open_unrealised": round(total_unrealised, 2),
        "week_pnl": round(week_realised + total_unrealised, 2),
        "week_realised": round(week_realised, 2),
        "last_week_pnl": round(last_week_realised, 2),
        # Accounts-parity trio (current + last week) — headline is Total-of-Both.
        "week_net_client_pnl": _f(_wk_tot["net_client_pnl"]),
        "week_brokerage": _f(_wk_tot["net_client_bkg"]),
        "week_total_of_both": _f(_wk_tot["total_of_both"]),
        "last_week_net_client_pnl": _f(_lw_tot["net_client_pnl"]),
        "last_week_brokerage": _f(_lw_tot["net_client_bkg"]),
        "last_week_total_of_both": _f(_lw_tot["total_of_both"]),
        "today_start": today_start.isoformat(),
        "week_start": week_start.isoformat(),
        "last_week_start": last_week_start.isoformat(),
        "last_week_end": last_week_end.isoformat(),
        "week_label": _fmt_range(week_start_ist, week_end_ist),
        "last_week_label": _fmt_range(last_week_start_ist, last_week_end_disp_ist),
        "usd_inr_rate": round(current_usd_inr, 4),
    }
    # Cache for the next 2 s so the Dashboard/Positions/Orders polls share it.
    _ADMIN_PNL_CACHE[_ck] = (_admin_pnl_time.monotonic() + _ADMIN_PNL_TTL, _data)
    return APIResponse(data=_data)


@router.get("/positions/{position_id}/rate-history", response_model=APIResponse[dict])
async def position_rate_history(
    position_id: str,
    admin: CurrentAdmin,
    from_dt: str | None = Query(
        default=None, description="ISO datetime, inclusive; defaults to position open"
    ),
    to_dt: str | None = Query(
        default=None, description="ISO datetime, inclusive; defaults to position close / now"
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=60, ge=1, le=500),
    _: None = Depends(require_perm("trading_view", "read")),
):
    """Per-minute Bid/Ask "Rate History" for a position's instrument.

    Defaults to the position's entry→exit window (for an OPEN position `to`
    defaults to now; for CLOSED it's `closed_at`). The `from_dt` / `to_dt`
    query params override that window — this powers the modal's custom time
    filter. Rows are newest-first, paginated.

    Data is recorded FORWARD-ONLY by the `tick_aggregator` (bid/ask cannot be
    back-filled from any feed), so a window before the recorder was deployed —
    or a position that never ticked — returns an empty `rows` list.
    """
    from datetime import timezone as _tz

    from app.models.tick_snapshot import TickSnapshot
    from app.utils.time_utils import now_utc as _now_utc

    p = await Position.get(_pos_oid(position_id))
    if p is None:
        raise HTTPException(status_code=404, detail="Position not found")
    await assert_user_in_scope(admin, p.user_id)

    def _parse(dt_str: str | None) -> datetime | None:
        if not dt_str:
            return None
        try:
            d = datetime.fromisoformat(dt_str.strip().replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=_tz.utc)
        except Exception:
            return None

    win_from = _parse(from_dt) or p.opened_at or getattr(p, "created_at", None)
    win_to = _parse(to_dt) or p.closed_at or _now_utc()
    # DB timestamps may be naive UTC — normalize so the range compare is sound.
    if win_from is not None and win_from.tzinfo is None:
        win_from = win_from.replace(tzinfo=_tz.utc)
    if win_to is not None and win_to.tzinfo is None:
        win_to = win_to.replace(tzinfo=_tz.utc)

    token = str(p.instrument.token)
    q: dict = {"token": token}
    ts_q: dict = {}
    if win_from is not None:
        ts_q["$gte"] = win_from
    if win_to is not None:
        ts_q["$lte"] = win_to
    if ts_q:
        q["timestamp"] = ts_q

    total = await TickSnapshot.find(q).count()
    rows = (
        await TickSnapshot.find(q)
        .sort("-timestamp")
        .skip((page - 1) * page_size)
        .limit(page_size)
        .to_list()
    )
    data_rows = [
        {
            "timestamp": r.timestamp.isoformat(),
            "bid_high": r.bid_high,
            "bid_low": r.bid_low,
            "ask_high": r.ask_high,
            "ask_low": r.ask_low,
        }
        for r in rows
    ]
    return APIResponse(
        data={
            "token": token,
            "symbol": p.instrument.symbol,
            "window_from": win_from.isoformat() if win_from else None,
            "window_to": win_to.isoformat() if win_to else None,
            "page": page,
            "page_size": page_size,
            "total": total,
            "rows": data_rows,
        }
    )


@router.get("/positions/{position_id}/netting", response_model=APIResponse[dict])
async def position_netting_entries(
    position_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("trading_view", "read")),
):
    """Drill-down on a single position: the chronological list of fills
    that built it up, plus a header summary (side, total volume, average
    entry, current price, total P/L) and an `avg_calc_formula` string for
    the dialog footer.

    Used by the admin Positions blotter row-click to show the same view
    the user reported as the "Netting Entries — BSE (426450)" mockup.
    Works for both OPEN and CLOSED positions:
      - OPEN  → returns every fill from `opened_at` onward whose
                (user, token, product_type) matches the position
      - CLOSED → bounded by `[opened_at, closed_at]`
    """
    from decimal import Decimal as _D

    from app.utils.decimal_utils import to_decimal as _to_dec
    from app.services import market_data_service as _mds

    p = await Position.get(_pos_oid(position_id))
    if p is None:
        raise HTTPException(status_code=404, detail="Position not found")
    await assert_user_in_scope(admin, p.user_id)

    # Find every Trade that contributed to this position. `position_id` is
    # not stored on Trade, so we match by user + token + product_type +
    # time window. For a re-opened position (close then open again on the
    # same instrument) the time bounds keep us inside the CURRENT
    # incarnation's fills.
    #
    # Grace window: position.opened_at is stamped AFTER the opening
    # Trade.insert() in position_service.apply_fill, so a millisecond of
    # clock-skew between the two writes makes `executed_at >= opened_at`
    # silently drop the opening Trade — exactly the user-reported "sirf
    # ek entry dikh raha hai (Exit), Entry missing" bug. A small grace
    # absorbs that sub-second skew.
    #
    # The grace MUST stay tight: when a user closes a position and reopens
    # the SAME instrument seconds later, a wide grace makes the two cycles'
    # windows overlap, so each closed-position card bleeds in the adjacent
    # cycle's legs (CL33333046 GOLD26AUGFUT, 2026-06-24: close 11:42:59 →
    # reopen 11:43:14, only 15 s apart — the old 60 s grace pulled cycle-2's
    # opening BUY into cycle-1's card and cycle-1's closing SELL into
    # cycle-2's card, so both netting popups showed phantom extra legs even
    # though the books were correct). The real clock-skew is milliseconds,
    # so 2 s is ~1000× the worst case while no longer overlapping any
    # realistic same-instrument re-entry.
    from datetime import timedelta as _td

    # A carried-forward position flips MIS→NRML at the EOD rollover, but its
    # OPENING trades keep product_type=MIS — only the position row + the
    # closing fill become NRML. A strict `product_type == p.product_type`
    # filter then dropped every entry leg, so the dialog showed ONLY the exit
    # (operator: "sirf exit dikh raha, saare entry missing" on a carried NFO
    # position). Accept the position's product type PLUS its MIS intraday
    # origin so a carried position lists all of its legs. The time window
    # already isolates this incarnation, so this can't pull in a later cycle.
    _pt = p.product_type.value if hasattr(p.product_type, "value") else str(p.product_type)
    query: dict = {
        "user_id": p.user_id,
        "instrument.token": p.instrument.token,
        "product_type": {"$in": list({_pt, "MIS"})},
    }
    time_q: dict = {}
    if p.opened_at:
        time_q["$gte"] = p.opened_at - _td(seconds=2)
    if p.closed_at:
        # Same grace on the upper bound so a closing trade whose
        # executed_at lands a few milliseconds AFTER position.closed_at
        # still shows up.
        time_q["$lte"] = p.closed_at + _td(seconds=2)
    if time_q:
        query["executed_at"] = time_q

    trades = (
        await Trade.find(query).sort("+executed_at").to_list()
    )

    # Build the per-row entries the dialog renders.
    open_side = (
        p.opened_side.value
        if p.opened_side and hasattr(p.opened_side, "value")
        else str(p.opened_side or "")
    ).upper() or None

    entries: list[dict] = []
    formula_parts: list[str] = []
    total_volume = _D("0")
    weighted = _D("0")
    for idx, t in enumerate(trades, start=1):
        side = (
            t.action.value if hasattr(t.action, "value") else str(t.action)
        ).upper()
        # An "Entry" leg adds same-direction exposure; "Exit" reduces.
        # If we know the original opened_side, anything matching it is
        # Entry, opposite is Exit. Fallback: BUY=Entry / SELL=Exit when
        # opened_side is unknown.
        entry_kind = "Exit"
        if open_side is None:
            entry_kind = "Entry" if side == "BUY" else "Exit"
        elif side == open_side:
            entry_kind = "Entry"
        else:
            entry_kind = "Exit"

        qty = _to_dec(t.quantity)
        price = _to_dec(t.price)
        if entry_kind == "Entry":
            total_volume += qty
            weighted += qty * price
            formula_parts.append(f"{qty}×₹{price}")
        pnl_inr = (
            _to_dec(t.pnl_inr) if getattr(t, "pnl_inr", None) is not None else None
        )
        entries.append(
            {
                "row": idx,
                "type": entry_kind,
                "side": side,
                "executed_at": t.executed_at.isoformat() if t.executed_at else None,
                "volume": float(qty),
                "price": float(price),
                "pnl_inr": float(pnl_inr) if pnl_inr is not None else None,
            }
        )

    avg_entry = (weighted / total_volume) if total_volume > 0 else _to_dec(p.avg_price)
    avg_calc_formula = (
        f"({' + '.join(formula_parts)}) ÷ {total_volume} = ₹{avg_entry:.2f}"
        if formula_parts
        else f"₹{avg_entry:.2f}"
    )

    # Live LTP for OPEN; close price (already stamped onto position.ltp by
    # position_service.apply_fill) for CLOSED.
    if p.status == PositionStatus.OPEN:
        try:
            current_price = float(await _mds.get_ltp(p.instrument.token))
        except Exception:
            current_price = float(_to_dec(p.ltp)) if p.ltp is not None else 0.0
    else:
        current_price = float(_to_dec(p.ltp)) if p.ltp is not None else 0.0

    # Header total P/L: unrealised for OPEN, realised for CLOSED.
    if p.status == PositionStatus.OPEN:
        total_pnl = float(_to_dec(p.unrealized_pnl)) if p.unrealized_pnl is not None else 0.0
    else:
        total_pnl = float(_to_dec(p.realized_pnl)) if p.realized_pnl is not None else 0.0

    return APIResponse(
        data={
            "position_id": str(p.id),
            "symbol": p.instrument.symbol,
            "exchange": str(getattr(p.instrument.exchange, "value", p.instrument.exchange) or ""),
            "token": p.instrument.token,
            "status": p.status.value if hasattr(p.status, "value") else str(p.status),
            "side": open_side or "BUY",
            "volume": float(total_volume),
            "avg_entry": float(avg_entry),
            "current_price": current_price,
            "total_pnl": total_pnl,
            "avg_calc_formula": avg_calc_formula,
            "entries": entries,
        }
    )


@router.delete("/positions/{position_id}", response_model=APIResponse[dict])
async def delete_position(
    position_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("trading_view", "write")),
):
    """Hard-delete a position record AND reverse its wallet impact.

    Admin-flagged: "close trade delete kiya, lekin user ka PnL wallet
    se kam ya zyada nahi hua — agar 10k deposit + 2k profit = 12k
    tha, trade delete ke baad bhi 12k hi rahta hai, jabki 10k hona
    chahiye". The earlier implementation just removed the Position
    row, leaving the realized-PnL credit / debit and brokerage
    deduction on the ledger. That broke the invariant that a deleted
    trade leaves no trace on the wallet.

    Now the delete path also:
      • Posts a REVERSAL ledger entry of `-realized_pnl` so a profit
        deleted DEBITS the wallet (12k → 10k for the +2k example) and
        a loss deleted CREDITS the wallet (8k → 10k for a -2k loss).
      • Recomputes the per-(user, instrument) tracker from live
        Position docs so the stale row is forgotten from
        intraday_lots / holding_lots / margin_blocked.
      • Refuses to delete an OPEN position with non-zero quantity —
        admin must squareoff first. Hard-deleting an open row would
        orphan the locked margin AND skip the standard closing
        ledger entries, putting the wallet into a worse state than
        before.

    Audit-logged with `actor_id = admin.id` so the original credit
    plus the reversal both appear in the user's ledger with their
    own narrations.
    """
    p = await Position.get(_pos_oid(position_id))
    if p is None:
        raise HTTPException(status_code=404, detail="Position not found")
    await assert_user_in_scope(admin, p.user_id)
    user_id = p.user_id

    # ── OPEN-position delete (admin-only escape hatch) ────────────────
    # Previously this endpoint refused to delete OPEN rows ("Square it
    # off first…"). The intent was to protect the wallet from orphaned
    # `used_margin` and a missing close-fill ledger trail. But the
    # operator legitimately needs an escape hatch for stale / corrupt
    # rows (positions left dangling by a crashed matching cycle, or
    # rows the admin wants to nuke without booking PnL against an
    # off-market last price). On 21-May the operator hit this when the
    # zero-LTP false-stop-out incident also left a few rows showing
    # phantom −LAKH M2M that they wanted gone WITHOUT booking that
    # MTM as a real loss.
    #
    # Flow for OPEN rows:
    #   1) Release the position's `margin_used` back to
    #      available_balance via `wallet_service.release_margin` —
    #      no ledger row for the margin lock itself (margin is an
    #      internal slot, not a money movement), matching what a
    #      normal close would have done.
    #   2) Recompute the tracker so intraday / holding lots drop the
    #      row.
    #   3) Delete the Position document.
    #   4) DO NOT book realized PnL — the position never closed at a
    #      real market price; recording the M2M as realised would
    #      poison the user's wallet. `unrealized_pnl` on the row is
    #      simply forgotten with the row.
    #
    # Closed-row flow is unchanged (REVERSAL of the realised PnL).
    from decimal import Decimal as _Decimal

    from app.models.transaction import TransactionType
    from app.services import wallet_service
    from app.utils.decimal_utils import to_decimal

    is_open = p.status == PositionStatus.OPEN and abs(float(p.quantity or 0)) > 1e-9
    realized = to_decimal(p.realized_pnl or 0)
    reversed_amount = _Decimal("0")
    closing_brokerage = _Decimal("0")
    booked_total = _Decimal("0")

    if is_open:
        # Release the locked margin back to available_balance. Best-effort:
        # release_margin is idempotent against drift (caps at used_margin),
        # but if it raises we still let the delete proceed because the
        # final `recompute_used_margin` below will reconcile from the
        # live set of OPEN positions anyway.
        try:
            margin_locked = to_decimal(p.margin_used or 0)
            if margin_locked > _Decimal("0"):
                await wallet_service.release_margin(user_id, margin_locked)
        except Exception:
            pass
    else:
        # ── Full wallet reversal for closed position ─────────────────
        # The close booked up to THREE wallet debits that must all be
        # reversed so the user's balance ends at exactly what it was
        # before the trade:
        #   1. CHARGES:  -brokerage (always deducted on close fill)
        #   2. PNL:      -raw_realized  (loss → debit; profit → credit)
        #   3. SETTLEMENT_OUTSTANDING_BOOKED: -shortfall (only for
        #      stop-out closes where loss > available_balance)
        #
        # Previous code only reversed (2) and missed (1)+(3), leaving
        # the user short by brokerage + any settlement shortfall.

        from datetime import timedelta as _td_delta
        from app.models.transaction import WalletTransaction as _WT, TransactionStatus as _TS
        from bson import Decimal128 as _Dec128

        # ── Step A: unwind settlement_outstanding (if any) ───────────
        # `booked_total` = the slice of this close's realised loss that could
        # NOT be debited from available_balance and was parked in
        # settlement_outstanding instead. Step C credits back only the
        # available-debited portion (realized + booked_total), so if
        # booked_total is undercounted the delete OVER-credits available by
        # exactly the settlement amount AND leaves the settlement standing.
        # Operator-caught (CL33333046): a GOLD26AUGFUT stop-out debited ₹75,325
        # from available + booked ₹38,574 to settlement, but the delete
        # reversed the FULL ₹1,13,900 → ₹38,574 phantom cash + settlement never
        # cleared, because the old symbol-in-narration BOOKED scan missed it.
        #
        # PRIMARY (reliable): derive booked_total from the close's own PNL
        # ledger row — its amount IS the true available debit, so
        #   booked_total = |realized| − |pnl_row_amount|.
        # The PNL row always names the symbol ("Realized loss on {symbol}
        # close"), so this resolves even when the separate BOOKED row's
        # narration/window doesn't. FALLBACK: scan the explicit BOOKED rows.
        booked_total = _Decimal("0")
        if p.closed_at is not None:
            lo = p.closed_at - _td_delta(seconds=90)
            hi = p.closed_at + _td_delta(seconds=90)
            sym = p.instrument.symbol
            # (1) Derive from the close's PNL row = the actual available debit.
            try:
                pnl_rows = await _WT.find(
                    _WT.user_id == p.user_id,
                    _WT.transaction_type == TransactionType.PNL,
                    _WT.created_at >= lo,
                    _WT.created_at <= hi,
                ).to_list()
                cand = [r for r in pnl_rows if sym in (r.narration or "")]
                if cand and realized < _Decimal("0"):
                    cand.sort(
                        key=lambda r: abs((r.created_at - p.closed_at).total_seconds())
                    )
                    actual_debit = abs(to_decimal(cand[0].amount or 0))
                    derived = abs(realized) - actual_debit
                    if derived > _Decimal("0"):
                        booked_total = derived
            except Exception:
                booked_total = _Decimal("0")
            # (2) Fallback: explicit BOOKED rows scanned by symbol + window.
            if booked_total <= _Decimal("0"):
                try:
                    booked_rows = await _WT.find(
                        _WT.user_id == p.user_id,
                        _WT.transaction_type == TransactionType.SETTLEMENT_OUTSTANDING_BOOKED,
                        _WT.status == _TS.COMPLETED,
                        _WT.created_at >= lo,
                        _WT.created_at <= hi,
                    ).to_list()
                    for r in booked_rows:
                        if sym in (r.narration or ""):
                            booked_total += abs(to_decimal(r.amount or 0))
                except Exception:
                    booked_total = _Decimal("0")

        if booked_total > _Decimal("0"):
            try:
                from app.models.wallet import Wallet as _Wallet
                wallet_doc = await wallet_service.get_or_create(user_id)
                cur_settle = to_decimal(wallet_doc.settlement_outstanding or 0)
                new_settle = max(_Decimal("0"), cur_settle - booked_total)
                wallet_doc.settlement_outstanding = _Dec128(str(new_settle))
                wallet_doc.version = (wallet_doc.version or 0) + 1
                await wallet_doc.save()
                avail_str = str(wallet_doc.available_balance)
                await _WT(
                    user_id=p.user_id,
                    transaction_type=TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY,
                    amount=_Dec128(str(booked_total)),
                    balance_before=_Dec128(avail_str),
                    balance_after=_Dec128(avail_str),
                    reference_type="Position",
                    reference_id=str(p.id),
                    narration=(
                        f"Delete {p.instrument.symbol} — settlement unbooked "
                        f"(shortfall from original close reversed)"
                    ),
                    status=_TS.COMPLETED,
                    created_by=admin.id,
                ).insert()
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Settlement unwind failed; delete aborted: {e}",
                )

        # ── Step B: look up brokerage on the closing Trade row ────────
        closing_brokerage = _Decimal("0")
        if p.closed_at is not None:
            try:
                from app.models.trade import Trade as _TradeModel
                ct = await _TradeModel.find_one(
                    {
                        "user_id": p.user_id,
                        "instrument.token": p.instrument.token,
                        "pnl_inr": {"$ne": None},
                        "executed_at": {
                            "$gte": p.closed_at - _td_delta(seconds=15),
                            "$lte": p.closed_at + _td_delta(seconds=15),
                        },
                    }
                )
                if ct is not None:
                    closing_brokerage = to_decimal(ct.total_charges or ct.brokerage or 0)
            except Exception:
                closing_brokerage = _Decimal("0")

        # ── Step B2: opening-leg brokerage ────────────────────────────
        # A DELETE removes the WHOLE position, so the brokerage paid to OPEN
        # it (every opening BUY fill — pnl_inr is None) must be refunded too,
        # not just the closing leg. Reopen deliberately leaves this charged
        # (the position stays open, so the open brokerage still applies), but
        # delete fully unwinds the position. Operator caught the leak: after a
        # delete the user's balance was short by exactly the opening brokerage
        # (₹321 + ₹320 across cycles). Sum every opening fill across the
        # position's lifecycle [opened_at, closed_at].
        opening_brokerage = _Decimal("0")
        if p.closed_at is not None:
            try:
                from app.models.trade import Trade as _TradeOpen
                _olo = (p.opened_at or p.closed_at) - _td_delta(seconds=15)
                _ohi = p.closed_at + _td_delta(seconds=15)
                opens = await _TradeOpen.find(
                    {
                        "user_id": p.user_id,
                        "instrument.token": p.instrument.token,
                        "product_type": p.product_type.value,
                        "pnl_inr": None,  # opening fills carry no realized P&L
                        "executed_at": {"$gte": _olo, "$lte": _ohi},
                    }
                ).to_list()
                for ot in opens:
                    opening_brokerage += to_decimal(ot.total_charges or ot.brokerage or 0)
            except Exception:
                opening_brokerage = _Decimal("0")

        # ── Step C: cash reversal (PnL cash portion + brokerage) ──────
        # cash_refund = signed amount leaving wallet after this reversal
        #   loss:   realized=-392, booked=0, brok=210
        #           cash_refund = (-392+0) - 210 = -602 → -(-602) = +602 ✓
        #   profit: realized=+500, booked=0, brok=100
        #           cash_refund = 500 - 100 = +400 → -(+400) = -400 ✓
        #           (debit back profit, refund brokerage)
        # Subtract BOTH closing + opening brokerage so a deleted position
        # refunds every rupee it ever charged → balance returns to pre-trade.
        cash_refund = (realized + booked_total if realized < 0 else realized) - closing_brokerage - opening_brokerage
        if cash_refund != _Decimal("0"):
            try:
                await wallet_service.adjust(
                    user_id,
                    -cash_refund,
                    transaction_type=TransactionType.REVERSAL,
                    narration=(
                        f"Delete {p.instrument.symbol} — reverse close P&L + brokerage "
                        f"(deleted by {admin.user_code})"
                        f" [PnL ₹{abs(realized + booked_total if realized < 0 else realized)}"
                        f", brokerage ₹{closing_brokerage + opening_brokerage}"
                        f" (close ₹{closing_brokerage} + open ₹{opening_brokerage})"
                        + (f", settlement ₹{booked_total} unwound separately" if booked_total > _Decimal("0") else "")
                        + "]"
                    ),
                    reference_type="Position",
                    reference_id=str(p.id),
                    actor_id=admin.id,
                )
                reversed_amount = realized
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Wallet reversal failed; position not deleted: {e}",
                )

    # Supersede ALL of this position's fills — opening AND closing — so a
    # DELETE wipes the trade out of every aggregation (P&L, brokerage, closed
    # blotter, ledger, admin cards). Unlike reopen (which only undoes the
    # closing leg), a delete removes the whole position, so its opening-leg
    # brokerage must drop too — operator: "delete karne par brokerage bhi har
    # jagah se hat jaye". Window = the position's full life [opened_at,
    # closed_at]; bounded by the doc's own timestamps so a parallel same-token
    # position outside that span keeps its fills.
    try:
        from datetime import timedelta as _td_sup
        from app.models.trade import Trade as _TradeSup
        from app.models.order import Order as _OrderSup
        _lo = (p.opened_at or p.closed_at)
        if p.closed_at is not None and _lo is not None:
            _sup_q: dict[str, Any] = {
                "user_id": p.user_id,
                "instrument.token": p.instrument.token,
                "product_type": p.product_type.value,
                "executed_at": {
                    "$gte": _lo - _td_sup(seconds=5),
                    "$lte": p.closed_at + _td_sup(seconds=3),
                },
            }
            # Collect the fills' parent orders BEFORE flagging, so we can wipe
            # them from the user's order blotter too.
            _sup_trades = await _TradeSup.find(_sup_q).to_list()
            _order_ids = list(
                {t.order_id for t in _sup_trades if getattr(t, "order_id", None)}
            )
            await _TradeSup.get_motor_collection().update_many(
                _sup_q, {"$set": {"superseded_by_reopen": True}}
            )
            # Bug: a deleted position's EXECUTED orders lingered in the user's
            # Orders → Executed tab (the user list reads Order docs directly,
            # which carry no superseded flag). Hard-remove them so a delete
            # wipes the order everywhere, not just the trade/position/ledger.
            # Operator: "trade delete karne par user side ke execute orders se
            # bhi hat jaye."
            if _order_ids:
                await _OrderSup.get_motor_collection().delete_many(
                    {"_id": {"$in": _order_ids}}
                )
    except Exception:
        logger.exception("delete_supersede_trades_failed", extra={"pos_id": str(p.id)})

    instrument_token = p.instrument.token
    segment_type = p.segment_type
    await p.delete()

    # Tracker recompute — drops the now-gone row from intraday /
    # holding lots counters. Same source-of-truth helper that runs on
    # every fill and the 15-min self-heal loop.
    try:
        from app.services.position_service import _recompute_tracker

        await _recompute_tracker(
            user_id=user_id,
            segment_type=segment_type,
            token=instrument_token,
        )
    except Exception:
        # Tracker drift is non-fatal — the periodic reconciler will
        # catch it within 15 min — but log so we notice if it
        # becomes a pattern.
        pass

    # Wallet used_margin recompute — same source-of-truth idea but
    # for the locked-margin counter. Admin-flagged: "0 open positions
    # par USED MARGIN ₹1,728.70 dikh raha". `release_margin` is
    # delta-based and drifts when admin hard-deletes a Position
    # without a closing fill. Now every delete re-syncs the wallet
    # to sum(open positions' margin_used) so the orphan margin is
    # released back to available immediately.
    try:
        from app.services import wallet_service as _ws

        await _ws.recompute_used_margin(user_id)
    except Exception:
        pass

    # Audit trail. The OPEN-delete path is a sharper edge than a
    # closed-row delete (it releases locked margin without a closing
    # trade), so we flag it explicitly in metadata for forensic
    # readability.
    try:
        await log_event(
            action=AuditAction.POSITION_DELETE,
            entity_type="Position",
            entity_id=p.id,
            actor_id=admin.id,
            target_user_id=user_id,
            metadata={
                "realized_pnl_reversed_inr": str(reversed_amount),
                "brokerage_refunded_inr": str(closing_brokerage if not is_open else _Decimal("0")),
                "settlement_unwound_inr": str(booked_total if not is_open else _Decimal("0")),
                "symbol": p.instrument.symbol,
                "status_before_delete": p.status.value,
                "open_force_delete": is_open,
                "margin_released_inr": (
                    str(to_decimal(p.margin_used or 0)) if is_open else "0"
                ),
                # Position lifecycle detail for the Admin Actions card — open /
                # close time, side, and the traded lots (quantity is 0 on a
                # closed row, so carry opening_quantity + lot_size).
                "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                "closed_at": p.closed_at.isoformat() if p.closed_at else None,
                "opened_side": (
                    p.opened_side.value if getattr(p, "opened_side", None) else None
                ),
                "opening_quantity": (
                    p.opening_quantity
                    if p.opening_quantity is not None
                    else abs(float(p.quantity or 0))
                ),
                "lot_size": getattr(p.instrument, "lot_size", None),
                "exchange": (
                    p.instrument.exchange.value
                    if hasattr(p.instrument.exchange, "value")
                    else str(p.instrument.exchange)
                ),
                "product_type": p.product_type.value if p.product_type else None,
            },
        )
    except Exception:
        pass

    # Best-effort realtime nudge — the delete + wallet reversal are ALREADY
    # committed above, so a pub/sub hiccup (Redis mid-restart / down) must
    # NEVER turn a done delete into a 500 that the admin reads as "Network
    # Error" and retries. Every other post-delete step here is already
    # wrapped; this one was the lone unguarded call. (redis_client.publish
    # also no-ops when Redis isn't initialised — this covers genuine
    # connection errors too.)
    try:
        await _publish_position_event(
            user_id,
            "delete",
            None,
            {
                "id": position_id,
                "by": "admin",
                "realized_pnl_reversed_inr": str(reversed_amount),
            },
        )
    except Exception:
        logger.exception("delete_position_publish_failed", extra={"pos_id": position_id})
    return APIResponse(
        data={
            "ok": True,
            "id": position_id,
            "realized_pnl_reversed_inr": str(reversed_amount),
        }
    )


class _BulkDeletePositionsPayload(BaseModel):
    position_ids: list[str]


@router.post("/positions/bulk-delete", response_model=APIResponse[dict])
async def bulk_delete_positions(
    payload: _BulkDeletePositionsPayload,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("trading_view", "write")),
):
    """Delete many (closed) positions in one call. Reuses the SAME single-row
    delete path per id — full wallet reversal of realised P&L + closing/opening
    brokerage + any settlement unwind, trade supersede, executed-order removal,
    tracker/used-margin recompute, and audit — so each row is unwound EXACTLY
    like a one-off delete (no separate, untested bulk math). Continues on a
    per-row error and reports which ids failed so one bad row can't abort the
    batch. Scope is enforced inside `delete_position` (assert_user_in_scope)."""
    ids = [i for i in dict.fromkeys(payload.position_ids or []) if i]  # dedupe, keep order
    if not ids:
        raise HTTPException(status_code=400, detail="No positions selected.")
    if len(ids) > 200:
        raise HTTPException(status_code=400, detail="Too many at once — select up to 200.")

    deleted = 0
    failed: list[dict] = []
    for pid in ids:
        try:
            await delete_position(pid, admin, None)
            deleted += 1
        except HTTPException as he:
            failed.append({"id": pid, "error": str(he.detail)})
        except Exception as e:  # noqa: BLE001
            logger.exception("bulk_delete_position_failed", extra={"pos_id": pid})
            failed.append({"id": pid, "error": str(e)})

    return APIResponse(
        data={"total": len(ids), "deleted": deleted, "failed": failed},
        message=(
            f"Deleted {deleted} of {len(ids)}."
            + (f" {len(failed)} failed." if failed else "")
        ),
    )


@router.post("/positions/reconcile-wallet-margin", response_model=APIResponse[dict])
async def reconcile_wallet_margins(admin: SuperAdmin):
    """Manual trigger for wallet `used_margin` reconciliation across
    every user. Same job runs every 15 minutes alongside the tracker
    reconciler, but admin can force an immediate pass when a user
    reports a stuck used_margin (e.g. "0 open positions but
    USED MARGIN dikh raha").

    Super-admin only because it touches every wallet on the platform.
    """
    from app.services import wallet_service as _ws

    summary = await _ws.reconcile_all_used_margins()
    return APIResponse(data={"ok": True, **summary})


@router.post(
    "/positions/{user_id}/reconcile-wallet-margin",
    response_model=APIResponse[dict],
)
async def reconcile_wallet_margin_for_user(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("trading_view", "write")),
):
    """Per-user manual recompute. Use when a single user reports a
    stuck used_margin and you don't want to wait for the next
    reconcile cycle. Scope-checked so an admin can only reconcile
    their own pool's users.
    """
    await assert_user_in_scope(admin, user_id)
    from app.services import wallet_service as _ws

    summary = await _ws.recompute_used_margin(user_id)
    return APIResponse(data=summary)


@router.post("/positions/reconcile-trackers", response_model=APIResponse[dict])
async def reconcile_trackers(admin: SuperAdmin):
    """Manual trigger for the per-(user, segment, instrument) tracker
    reconciler.

    The same job runs automatically every 15 min in the background
    (`tracker_reconcile_loop`). This endpoint lets an operator force an
    immediate pass — useful after a deploy / when a user reports being
    blocked by a stale `holding_lots` / `intraday_lots` counter.

    Super-admin only because it touches every user's trackers.
    """
    from app.services.position_service import reconcile_all_trackers

    summary = await reconcile_all_trackers()
    return APIResponse(data={"ok": True, **summary})


@router.post("/positions/emergency-squareoff", response_model=APIResponse[dict])
async def emergency_squareoff_all(admin: SuperAdmin):
    """Panic button — squares off every open position across the platform.

    Super-admin only: this is a platform-wide kill switch and must not be
    available to scoped sub-admins.
    """
    rows = await Position.find(Position.status == PositionStatus.OPEN).to_list()
    # Snapshot each affected user's settlement_outstanding BEFORE flattening
    # anything, so the post-batch close-ordering correction nets only the
    # phantom this panic-close books (same fix as the risk_enforcer stop-out
    # path). Self-correcting — no-op when there's no phantom.
    from app.services import wallet_service as _ws
    from app.utils.decimal_utils import to_decimal as _td

    phantom_before: dict = {}
    for _uid in {r.user_id for r in rows}:
        try:
            _w0 = await _ws.get_or_create(_uid)
            phantom_before[_uid] = _td(_w0.settlement_outstanding)
        except Exception:
            pass
    total = 0
    placed = 0
    for r in rows:
        if r.quantity == 0:
            continue
        total += 1
        try:
            target = await User.get(r.user_id)
            if target is None:
                continue
            action = OrderAction.SELL if r.quantity > 0 else OrderAction.BUY
            full_qty = abs(r.quantity)
            full_lots = max(0.01, full_qty / max(1, r.instrument.lot_size or 1))
            # Same `is_squareoff=True` bypass the per-position
            # admin_squareoff uses — emergency panic must work
            # outside market hours / weekends too, otherwise the
            # "panic button" is broken precisely when it's needed.
            # `force_quantity` flattens the exact open size so crypto /
            # forex positions whose qty is smaller than one lot still
            # close fully instead of partial-closing to a residual.
            await order_service.place_order(
                user=target,
                payload={
                    "token": r.instrument.token,
                    "action": action.value,
                    "order_type": OrderType.MARKET.value,
                    "product_type": r.product_type.value,
                    "lots": full_lots,
                    "force_quantity": full_qty,
                    "placed_from": "ADMIN",
                    "is_squareoff": True,
                },
            )
            placed += 1
            refreshed = await Position.get(r.id)
            # Stamp AUTO on every row this panic-button actually flattened.
            if (
                refreshed is not None
                and refreshed.status == PositionStatus.CLOSED
                and not refreshed.close_reason
            ):
                refreshed.close_reason = "AUTO"
                await refreshed.save()
            await _publish_position_event(
                r.user_id, "force_close", refreshed or r, {"by": "admin", "reason": "emergency"}
            )
        except Exception:
            continue
    # Net any close-ordering phantom settlement the batch booked once all
    # margins have been freed back to available.
    if placed and phantom_before:
        try:
            await _ws.net_phantom_settlement_for_users(phantom_before)
        except Exception:
            pass
    await log_event(
        action=AuditAction.SQUAREOFF_FORCE,
        entity_type="Platform",
        entity_id="emergency_all",
        actor_id=admin.id,
        metadata={"total": total, "placed": placed},
    )
    return APIResponse(data={"total": total, "placed": placed})


# ── Trades ──────────────────────────────────────────────────────────
@router.get("/trades", response_model=APIResponse[list])
async def list_trades(
    admin: CurrentAdmin,
    _: None = Depends(require_perm("trading_view", "read")),
    *,
    user_id: str | None = None,
    limit: int = Query(default=200, le=1000),
    from_dt: str | None = Query(default=None, description="ISO datetime, inclusive"),
    to_dt: str | None = Query(default=None, description="ISO datetime, exclusive"),
):
    q: dict[str, Any] = {}
    if user_id:
        await assert_user_in_scope(admin, user_id)
        q["user_id"] = PydanticObjectId(user_id)
    else:
        scope = await scoped_user_ids(admin)
        if scope is not None:
            if not scope:
                return APIResponse(data=[])
            q["user_id"] = {"$in": scope}
    if from_dt or to_dt:
        from datetime import datetime as _dt
        rng: dict[str, Any] = {}
        if from_dt:
            rng["$gte"] = _dt.fromisoformat(from_dt.replace("Z", "+00:00"))
        if to_dt:
            rng["$lt"] = _dt.fromisoformat(to_dt.replace("Z", "+00:00"))
        q["executed_at"] = rng
    rows = await Trade.find(q).sort("-executed_at").limit(limit).to_list()
    user_ids = list({r.user_id for r in rows})
    users = await User.find({"_id": {"$in": user_ids}}).to_list() if user_ids else []
    umap = {str(u.id): u.user_code for u in users}
    return APIResponse(
        data=[
            {
                "id": str(r.id),
                "trade_number": r.trade_number,
                "order_id": str(r.order_id),
                "user_id": str(r.user_id),
                "user_code": umap.get(str(r.user_id)),
                "symbol": r.instrument.symbol,
                "exchange": str(r.instrument.exchange),
                "segment": r.instrument.segment,
                "token": r.instrument.token,
                "instrument_token": r.instrument.token,
                "action": r.action.value,
                "quantity": r.quantity,
                "price": str(r.price),
                "value": str(r.value),
                "brokerage": str(r.brokerage),
                "net_amount": str(r.net_amount),
                "total_charges": str(r.total_charges),
                # Frozen realized P&L (INR, net of brokerage, FX-baked for
                # USD-quoted instruments). None for opening-leg fills — the
                # closing leg is the one that books the realized number.
                "pnl_inr": str(r.pnl_inr) if r.pnl_inr is not None else None,
                "executed_at": r.executed_at,
            }
            for r in rows
        ]
    )


# ── Holdings ────────────────────────────────────────────────────────
@router.get("/holdings", response_model=APIResponse[list])
async def list_holdings(
    admin: CurrentAdmin,
    user_id: str | None = None,
    _: None = Depends(require_perm("trading_view", "read")),
):
    q: dict[str, Any] = {}
    if user_id:
        await assert_user_in_scope(admin, user_id)
        q["user_id"] = PydanticObjectId(user_id)
    else:
        scope = await scoped_user_ids(admin)
        if scope is not None:
            if not scope:
                return APIResponse(data=[])
            q["user_id"] = {"$in": scope}
    rows = await Holding.find(q).limit(500).to_list()
    user_ids = list({r.user_id for r in rows})
    users = await User.find({"_id": {"$in": user_ids}}).to_list() if user_ids else []
    umap = {str(u.id): u.user_code for u in users}
    return APIResponse(
        data=[
            {
                "id": str(r.id),
                "user_id": str(r.user_id),
                "user_code": umap.get(str(r.user_id)),
                "symbol": r.instrument.symbol,
                "exchange": str(r.instrument.exchange),
                "quantity": r.quantity,
                "avg_price": str(r.avg_price),
                "ltp": str(r.ltp),
                "invested_value": str(r.invested_value),
                "current_value": str(r.current_value),
                "pnl": str(r.pnl),
                "pnl_percentage": r.pnl_percentage,
            }
            for r in rows
        ]
    )
