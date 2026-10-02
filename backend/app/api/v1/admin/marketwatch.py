"""Admin Market Watch — per-segment instrument lists + place orders on
behalf of users in the admin's scope.

Operator workflow:
    1. Admin picks a segment chip (NSE Equity / NSE Futures / … /
       Commodities).
    2. Searches the symbol they want, taps "+" to add it under that
       segment for THIS admin. Stored as a WatchlistItem under a
       synthetic admin-scoped Watchlist named ``__adminseg_<SEG>``.
    3. Live bid / ask / LTP show in the table via the same quote
       pipeline as the user app.
    4. Tap a row → Place Order modal opens. Admin multi-selects users
       from their pool, picks Market / Manual + MIS / NRML + qty, and
       hits BUY or SELL — one order per selected user goes through the
       regular order_service.place_order path with placed_from="ADMIN".

Why reuse the Watchlist / WatchlistItem collections instead of new
admin-specific tables: the per-segment management semantics are
identical to the user side (add / remove / live quote), the existing
indexes already enforce uniqueness per (owner, instrument), and the
admin's user_id is just another ObjectId from the same User collection
where ADMIN / SUPER_ADMIN / BROKER rows live. Distinct prefix
(``__adminseg_``) keeps the rows visually separate in Compass and means
the user-side endpoints never accidentally surface admin watchlists.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.dependencies import CurrentAdmin, assert_user_in_scope
from app.models._base import Exchange
from app.models.audit_log import AuditAction
from app.models.user import UserRole, UserStatus
from app.models.watchlist import Watchlist, WatchlistItem
from app.schemas.common import APIResponse
from app.services import instrument_service, market_data_service, order_service
from app.services.audit_service import log_event
from app.utils.time_utils import now_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketwatch", tags=["admin-marketwatch"])


# ── Segment allowlist ────────────────────────────────────────────────
# Admin chips are broader than user-side: they include the Infoway-fed
# buckets (Forex / Crypto spot+futures / Stocks / Indices / Commodities)
# alongside the Indian Zerodha segments. Each key maps to the underlying
# Instrument.segment values the search filter passes to MongoDB.
_SEG_PREFIX = "__adminseg_"

_SEG_MAP: dict[str, list[str]] = {
    "NSE_EQUITY":     ["NSE_EQUITY"],
    "NSE_FUTURES":    ["NSE_FUTURE", "NSE_INDEX_FUTURE"],
    "NSE_OPTIONS":    [
        "NSE_INDEX_OPTION_BUY", "NSE_INDEX_OPTION_SELL",
        "NSE_STOCK_OPTION_BUY", "NSE_STOCK_OPTION_SELL",
    ],
    "BSE_EQUITY":     ["BSE_EQUITY"],
    "BSE_FUTURES":    ["BSE_FUTURE", "BSE_INDEX_FUTURE"],
    "BSE_OPTIONS":    ["BSE_OPTION_BUY", "BSE_OPTION_SELL"],
    "MCX_FUTURES":    ["MCX_FUTURE"],
    "MCX_OPTIONS":    ["MCX_OPTION_BUY", "MCX_OPTION_SELL"],
    "CRYPTO_OPTIONS": ["CRYPTO_OPTION_BUY", "CRYPTO_OPTION_SELL"],
    "CRYPTO":         ["CRYPTO_SPOT", "CRYPTO_FUTURE", "CRYPTO_PERPETUAL"],
    "FOREX":          ["FOREX"],
    "STOCKS":         ["STOCKS"],
    "INDICES":        ["INDICES"],
    "COMMODITIES":    ["COMMODITIES"],
}


def _validate_segment(seg: str) -> str:
    s = (seg or "").upper()
    if s not in _SEG_MAP:
        raise HTTPException(status_code=400, detail=f"Unsupported segment: {seg}")
    return s


async def _get_or_create_segment_watchlist(
    admin_id: PydanticObjectId, segment_name: str
) -> Watchlist:
    name = _SEG_PREFIX + segment_name
    wl = await Watchlist.find_one(Watchlist.user_id == admin_id, Watchlist.name == name)
    if wl is not None:
        return wl
    wl = Watchlist(user_id=admin_id, name=name, sort_order=0, is_default=False)
    await wl.insert()
    return wl


# ── Segment items: list / add / remove ───────────────────────────────


@router.get("/segment/{segment_name}/items", response_model=APIResponse[list])
async def list_items(segment_name: str, admin: CurrentAdmin):
    """Items the admin has explicitly added under this segment chip.

    Empty list on first access — admin populates it via the search
    flow. No admin-block filter here: this is the operator's own
    surface, segment blocks at the trader level don't apply.
    """
    seg = _validate_segment(segment_name)
    wl = await _get_or_create_segment_watchlist(admin.id, seg)
    items = (
        await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id)
        .sort("sort_order")
        .to_list()
    )
    return APIResponse(
        data=[
            {
                "id": str(it.id),
                "instrument_token": it.instrument_token,
                "symbol": it.symbol,
                "exchange": str(it.exchange),
            }
            for it in items
        ]
    )


class _AddItemBody(BaseModel):
    token: str


@router.post("/segment/{segment_name}/items", response_model=APIResponse[dict])
async def add_item(segment_name: str, payload: _AddItemBody, admin: CurrentAdmin):
    seg = _validate_segment(segment_name)
    wl = await _get_or_create_segment_watchlist(admin.id, seg)
    inst = await instrument_service.get_by_token(payload.token)
    existing = await WatchlistItem.find_one(
        WatchlistItem.watchlist_id == wl.id,
        WatchlistItem.instrument_token == inst.token,
    )
    if existing is not None:
        return APIResponse(data={"id": str(existing.id), "duplicate": True})
    count = await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id).count()
    item = WatchlistItem(
        watchlist_id=wl.id,
        instrument_token=inst.token,
        symbol=inst.symbol,
        exchange=Exchange(inst.exchange),
        sort_order=count,
    )
    await item.insert()
    return APIResponse(data={"id": str(item.id)})


@router.delete("/segment/{segment_name}/items/{token}", response_model=APIResponse[dict])
async def remove_item(segment_name: str, token: str, admin: CurrentAdmin):
    seg = _validate_segment(segment_name)
    wl = await _get_or_create_segment_watchlist(admin.id, seg)
    item = await WatchlistItem.find_one(
        WatchlistItem.watchlist_id == wl.id, WatchlistItem.instrument_token == token
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    await item.delete()
    return APIResponse(data={"ok": True})


# ── Quotes ───────────────────────────────────────────────────────────


@router.get("/segment/{segment_name}/quotes", response_model=APIResponse[list])
async def segment_quotes(segment_name: str, admin: CurrentAdmin):
    """Items + their live snapshot. Same payload shape the user-side
    /user/marketwatch/{wl_id}/quotes returns so the frontend can reuse
    the existing row renderer."""
    seg = _validate_segment(segment_name)
    wl = await _get_or_create_segment_watchlist(admin.id, seg)
    items = await WatchlistItem.find(WatchlistItem.watchlist_id == wl.id).to_list()
    if not items:
        return APIResponse(data=[])
    quotes = await market_data_service.get_quotes([it.instrument_token for it in items])
    return APIResponse(
        data=[
            {
                "instrument_token": it.instrument_token,
                "symbol": it.symbol,
                "exchange": str(it.exchange),
                **q,
            }
            for it, q in zip(items, quotes)
        ]
    )


# ── Instrument search (segment-scoped) ───────────────────────────────


@router.get("/segment/{segment_name}/search", response_model=APIResponse[list])
async def search_segment(
    segment_name: str,
    admin: CurrentAdmin,
    q: str | None = None,
    limit: int = 30,
):
    """Filter instrument search by the segment's underlying SegmentType
    values. Powers the "+ add to NSE Equity" search flow on the admin
    Market Watch page."""
    seg = _validate_segment(segment_name)
    segs = _SEG_MAP[seg]

    # Pull a small page from the existing instrument cache + DB. The
    # search service already handles the trigram match + ranking.
    from app.models.instrument import Instrument

    base_query: dict[str, Any] = {
        "is_active": True,
        "segment": {"$in": segs},
    }
    if q and q.strip():
        import re as _re
        rx = _re.compile(_re.escape(q.strip()), _re.IGNORECASE)
        base_query["$or"] = [{"symbol": rx}, {"trading_symbol": rx}, {"name": rx}]

    rows = await Instrument.find(base_query).limit(max(1, min(limit, 100))).to_list()
    return APIResponse(
        data=[
            {
                "token": r.token,
                "symbol": r.symbol,
                "name": r.name,
                "exchange": str(r.exchange),
                "segment": r.segment,
                "lot_size": r.lot_size,
            }
            for r in rows
        ]
    )


# ── Place orders on behalf of one or more users ──────────────────────


class _PlaceOrdersBody(BaseModel):
    token: str
    user_ids: list[str] = Field(min_length=1, max_length=100)
    action: str  # BUY | SELL
    order_type: str = "MARKET"  # MARKET (live LTP) | MANUAL (operator price)
    product_type: str = "MIS"  # MIS | NRML | CNC
    lots: float = Field(gt=0)
    price: float | None = None  # required for MANUAL — the exact entry price
    # Historical entry: book the trade now, then stamp it as having happened
    # at this moment. Super-admin only and a reason is required, because it
    # rewrites what the client's book says happened.
    executed_at: datetime | None = None
    reason: str | None = None


async def _stamp_history(order: Any, when: datetime) -> None:
    """Move a freshly-booked order, its fills and the position it opened back
    to `when`.

    The money has already moved through the normal path — this only rewrites
    the timestamps so the trade sits where the operator says it happened. A
    position that existed BEFORE this order keeps its own opening time: this
    fill added to it, it did not create it.
    """
    from app.models.position import Position, PositionStatus
    from app.models.trade import Trade

    booked_at = order.created_at
    order.created_at = when
    if getattr(order, "executed_at", None):
        order.executed_at = when
    await order.save()

    for t in await Trade.find(Trade.order_id == order.id).to_list():
        t.executed_at = when
        t.created_at = when
        await t.save()

    pos = await Position.find_one(
        Position.user_id == order.user_id,
        Position.instrument.token == order.instrument.token,
        Position.status == PositionStatus.OPEN,
    )
    if pos is not None and pos.opened_at is not None and booked_at is not None:
        opened = pos.opened_at
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        booked = booked_at if booked_at.tzinfo else booked_at.replace(tzinfo=timezone.utc)
        # Opened by THIS order (within a minute of it) → carry it back too.
        if abs((opened - booked).total_seconds()) <= 60:
            pos.opened_at = when
            await pos.save()


@router.post("/place-orders", response_model=APIResponse[dict])
async def place_orders(payload: _PlaceOrdersBody, admin: CurrentAdmin):
    """Bulk place one order per selected user. Uses the same path the
    user-side order panel takes (order_service.place_order), tagged
    with placed_from="ADMIN" so the audit trail shows operator origin.

    Per-user failures are reported back individually — one bad user
    (insufficient margin, blocked, etc.) does NOT abort the whole batch.
    """
    action = (payload.action or "").upper()
    if action not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="action must be BUY or SELL")
    order_type = (payload.order_type or "MARKET").upper()
    # Legacy alias: an older admin bundle sent "LIMIT" for the Manual toggle.
    # It now means the same operator-priced immediate fill as "MANUAL".
    if order_type == "LIMIT":
        order_type = "MANUAL"
    if order_type not in ("MARKET", "MANUAL"):
        raise HTTPException(status_code=400, detail="order_type must be MARKET or MANUAL")
    product_type = (payload.product_type or "MIS").upper()
    if product_type not in ("MIS", "NRML", "CNC"):
        raise HTTPException(status_code=400, detail="product_type must be MIS, NRML or CNC")
    if order_type == "MANUAL" and (payload.price is None or payload.price <= 0):
        raise HTTPException(status_code=400, detail="MANUAL order requires a positive price")

    backdate_to: datetime | None = None
    if payload.executed_at is not None:
        if admin.role != UserRole.SUPER_ADMIN:
            raise HTTPException(status_code=403, detail="Only a super-admin can backdate a trade")
        if not (payload.reason or "").strip():
            raise HTTPException(status_code=400, detail="A reason is required when backdating a trade")
        backdate_to = payload.executed_at
        if backdate_to.tzinfo is None:
            backdate_to = backdate_to.replace(tzinfo=timezone.utc)
        now = now_utc()
        if backdate_to > now:
            raise HTTPException(status_code=400, detail="Backdated time cannot be in the future")
        if backdate_to < now - timedelta(days=365):
            raise HTTPException(status_code=400, detail="Backdating is limited to the last year")

    # Validate instrument exists once.
    await instrument_service.get_by_token(payload.token)

    placed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for uid in payload.user_ids:
        try:
            # assert_user_in_scope returns the loaded user AND raises if
            # the admin doesn't own them — handles all scope checks
            # (SUPER_ADMIN / ADMIN / BROKER pools) in one shot.
            target = await assert_user_in_scope(admin, uid)
            if target.status != UserStatus.ACTIVE:
                raise ValueError(f"user is {target.status}")
            # A MANUAL order books the position IMMEDIATELY at the operator's
            # price: route it through the MARKET fill path (executes now, not
            # parked) and hand the matching engine an exact force_fill_price.
            # Going through MARKET also sidesteps the limit_percentage band
            # check, which only applies to non-MARKET orders — so the operator
            # can book at ANY price. PnL then accrues from this entry. A plain
            # MARKET order keeps filling at the live LTP as before.
            body: dict[str, Any] = {
                "token": payload.token,
                "action": action,
                "order_type": "MARKET",
                "product_type": product_type,
                "lots": float(payload.lots),
                "placed_from": "ADMIN",
            }
            if order_type == "MANUAL":
                manual_price = float(payload.price or 0)
                body["price"] = manual_price
                body["force_fill_price"] = manual_price
            o = await order_service.place_order(user=target, payload=body)
            if backdate_to is not None:
                await _stamp_history(o, backdate_to)
                await log_event(
                    action=AuditAction.ORDER_PLACE,
                    entity_type="Order",
                    entity_id=str(o.id),
                    actor_id=admin.id,
                    target_user_id=target.id,
                    metadata={
                        "operation": "backdated_trade",
                        "backdated_to": backdate_to.isoformat(),
                        "reason": (payload.reason or "").strip()[:300],
                        "price": float(payload.price or 0),
                        "lots": float(payload.lots),
                        "action": action,
                        "token": payload.token,
                    },
                )
            placed.append({
                "user_id": str(target.id),
                "user_code": target.user_code,
                "order_id": str(o.id),
                "status": o.status.value if hasattr(o.status, "value") else str(o.status),
            })
        except Exception as e:  # noqa: BLE001 — per-user error capture
            failed.append({"user_id": uid, "error": str(e)})

    return APIResponse(data={"placed": placed, "failed": failed})
