"""User order endpoints — place, list, modify, cancel."""

from __future__ import annotations

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException, Query, Request

from app.core.dependencies import CurrentUser
from app.core.rate_limit import rate_limit
from app.models.audit_log import AuditAction
from app.models.order import Order, OrderStatus, order_reason_code
from app.schemas.common import APIResponse
from app.schemas.trading import ModifyOrderRequest, OrderOut, PlaceOrderRequest
from app.services import audit_service, order_service

router = APIRouter(prefix="/orders", tags=["user-orders"])


def _client_ip(request: Request) -> str:
    """Best-effort client-IP extraction. Honours X-Forwarded-For so nginx /
    CloudFront proxies don't all show up as 127.0.0.1 in the audit log."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _serialize(o: Order, *, pnl_inr: str | None = None, expiry: str | None = None) -> dict:
    return {
        "id": str(o.id),
        "order_number": o.order_number,
        "user_id": str(o.user_id),
        "symbol": o.instrument.symbol,
        "exchange": str(o.instrument.exchange),
        "segment": o.instrument.segment,
        # Real contract expiry (ISO) resolved from the Instrument collection so
        # the UI shows the true expiry day, not the symbol's year-as-day.
        "expiry": expiry,
        # Instrument token — needed by the Orders page to fetch live LTP
        # per symbol so the P&L column works for all executed orders, not
        # just those that still have an open position.
        "token": o.instrument.token,
        "instrument_token": o.instrument.token,
        "action": o.action.value,
        "order_type": o.order_type.value,
        "product_type": o.product_type.value,
        "validity": o.validity.value,
        "lots": o.lots,
        "quantity": o.quantity,
        "filled_quantity": o.filled_quantity,
        "pending_quantity": o.pending_quantity,
        "price": str(o.price),
        "trigger_price": str(o.trigger_price),
        "average_price": str(o.average_price),
        "status": o.status.value,
        "rejection_reason": o.rejection_reason,
        # Why this order happened — SL_HIT / TP_HIT / STOP_OUT / AUTO /
        # ADMIN_CLOSE / ADMIN / USER (shown on the Orders "Reason" column).
        "reason": order_reason_code(o),
        "is_amo": o.is_amo,
        "margin_blocked": str(o.margin_blocked),
        "brokerage": str(o.brokerage),
        "other_charges": str(o.other_charges),
        "bracket_stop_loss": str(o.bracket_stop_loss) if o.bracket_stop_loss is not None else None,
        "bracket_target": str(o.bracket_target) if o.bracket_target is not None else None,
        # Realized P&L in INR, frozen at fill time for closing legs. Used by
        # the History tab so closed-trade P&L stays fixed (instead of
        # floating against live LTP) and is rendered in INR even for
        # USD-quoted instruments (BTCUSD, XAUUSD, …). None for opening
        # fills (they have no realized P&L yet).
        "pnl_inr": pnl_inr,
        "created_at": o.created_at,
        "executed_at": o.executed_at,
        "cancelled_at": getattr(o, "cancelled_at", None),
        "updated_at": getattr(o, "updated_at", None),
    }


async def _pnl_inr_by_order(orders: list[Order]) -> dict[str, str]:
    """Bulk-fetch the `pnl_inr` from each order's associated trade. One
    Mongo round-trip for the whole page instead of N per-order lookups.
    Returns `{order_id_str: pnl_inr_str}` for orders whose trade(s) carry
    a non-null pnl_inr (closing fills only)."""
    from app.models.trade import Trade

    order_ids = [o.id for o in orders if o.id is not None]
    if not order_ids:
        return {}
    trades = await Trade.find({"order_id": {"$in": order_ids}}).to_list()
    out: dict[str, str] = {}
    for t in trades:
        if t.pnl_inr is None:
            continue
        # Sum across multiple fills of the same order (partial closes).
        key = str(t.order_id)
        prev = out.get(key)
        if prev is None:
            out[key] = str(t.pnl_inr)
        else:
            from decimal import Decimal as _D

            out[key] = str(_D(prev) + _D(str(t.pnl_inr)))
    return out


@router.get("", response_model=APIResponse[list[OrderOut]])
async def list_orders(
    user: CurrentUser,
    status: str | None = None,
    limit: int = Query(default=100, le=500),
    skip: int = 0,
):
    rows = await order_service.list_for_user(user.id, status=status, limit=limit, skip=skip)
    pnl_map = await _pnl_inr_by_order(rows)
    # Real contract expiries in one batch so executed / open / rejected order
    # cards show the true expiry day instead of parsing it off the symbol.
    emap: dict[str, str] = {}
    try:
        from app.models.instrument import Instrument

        _toks = list({o.instrument.token for o in rows if o.instrument and o.instrument.token})
        if _toks:
            for _d in await Instrument.find({"token": {"$in": _toks}}).to_list():
                _e = getattr(_d, "expiry", None)
                if _e is not None:
                    emap[_d.token] = _e.isoformat() if hasattr(_e, "isoformat") else str(_e)
    except Exception:
        emap = {}
    return APIResponse(
        data=[
            _serialize(o, pnl_inr=pnl_map.get(str(o.id)), expiry=emap.get(o.instrument.token))
            for o in rows
        ]
    )


@router.post("", response_model=APIResponse[OrderOut], dependencies=[rate_limit("trading")])
async def place(payload: PlaceOrderRequest, user: CurrentUser, request: Request):
    # Convert unexpected exceptions into a structured 400 with the actual
    # cause attached so the mobile/web client doesn't just see a generic
    # "An unexpected error occurred" 500. Known app errors (NotFoundError,
    # ValidationFailedError, etc.) bubble up untouched — they already
    # carry user-friendly messages.
    import logging
    from app.core.exceptions import AppError

    log = logging.getLogger(__name__)
    try:
        o = await order_service.place_order(user=user, payload=payload.model_dump())
    except AppError:
        # AppError subclasses are handled by the app-level handler with
        # specific codes/messages — let them through.
        raise
    except HTTPException:
        raise
    except Exception as e:
        log.exception(
            "place_order_failed user=%s token=%s action=%s lots=%s",
            user.id,
            payload.model_dump().get("token"),
            payload.model_dump().get("action"),
            payload.model_dump().get("lots"),
        )
        # 400 + ORDER_FAILED so the client surfaces the real reason instead
        # of "An unexpected error occurred".
        raise HTTPException(
            status_code=400,
            detail=f"Order failed: {type(e).__name__}: {str(e)[:200]}",
        ) from e
    # Audit the placement so the admin Activity view shows what the user
    # actually did — captures IP + user-agent so reviewers can spot
    # multi-device / cross-IP activity on one account. FIRE-AND-FORGET:
    # the audit row is write-once history, irrelevant to the response, so
    # it must not add a DB round-trip to the trader's place-order latency.
    # Wrapped so an audit-scheduling hiccup can NEVER fail the placement
    # the user already committed (the order is placed at this point).
    try:
        from app.utils.background import fire_and_forget

        fire_and_forget(
            audit_service.log_event(
                action=AuditAction.ORDER_PLACE,
                entity_type="Order",
                entity_id=o.id,
                actor_id=user.id,
                target_user_id=user.id,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                metadata={
                    "symbol": o.instrument.symbol,
                    "action": o.action.value,
                    "order_type": o.order_type.value,
                    "product_type": o.product_type.value,
                    "quantity": o.quantity,
                    "price": str(o.price),
                },
            ),
            label="order_place_audit",
        )
    except Exception:
        logger.exception("order_place_audit_schedule_failed")
    return APIResponse(data=_serialize(o))


@router.get("/{order_id}", response_model=APIResponse[OrderOut])
async def detail(order_id: str, user: CurrentUser):
    o = await Order.get(PydanticObjectId(order_id))
    if o is None or o.user_id != user.id:
        raise HTTPException(status_code=404, detail="Order not found")
    return APIResponse(data=_serialize(o))


@router.delete("/{order_id}", response_model=APIResponse[OrderOut])
async def cancel(order_id: str, user: CurrentUser, request: Request):
    o = await order_service.cancel(user.id, order_id)
    await audit_service.log_event(
        action=AuditAction.ORDER_CANCEL,
        entity_type="Order",
        entity_id=o.id,
        actor_id=user.id,
        target_user_id=user.id,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        metadata={
            "symbol": o.instrument.symbol,
            "order_number": o.order_number,
        },
    )
    return APIResponse(data=_serialize(o))


@router.put("/{order_id}", response_model=APIResponse[OrderOut])
async def modify(order_id: str, payload: ModifyOrderRequest, user: CurrentUser, request: Request):
    o = await Order.get(PydanticObjectId(order_id))
    if o is None or o.user_id != user.id:
        raise HTTPException(status_code=404, detail="Order not found")
    if o.status not in (OrderStatus.OPEN, OrderStatus.PENDING):
        raise HTTPException(status_code=400, detail="Cannot modify a non-open order")
    # Snapshot BEFORE mutating so the audit can show "rate X → Y". Without
    # this a limit-price change (the most common modify) left no trace of
    # the old value, and the admin couldn't see kis rate se kis rate badla.
    old_price = str(o.price) if o.price is not None else None
    old_trigger = str(o.trigger_price) if o.trigger_price is not None else None
    old_lots = o.lots
    if payload.lots is not None:
        o.lots = payload.lots
        o.quantity = payload.lots * max(1, o.instrument.lot_size or 1)
        o.pending_quantity = max(0, o.quantity - o.filled_quantity)
    if payload.price is not None:
        from bson import Decimal128
        o.price = Decimal128(str(payload.price))
    if payload.trigger_price is not None:
        from bson import Decimal128
        o.trigger_price = Decimal128(str(payload.trigger_price))
    await o.save()
    # Audit the modification (fire-and-forget — must not slow / fail the
    # save the user already committed). old_values → new_values lets the
    # admin Activity view render "Pending Order Modified : … Rate 250 → 260".
    try:
        from app.utils.background import fire_and_forget

        fire_and_forget(
            audit_service.log_event(
                action=AuditAction.ORDER_MODIFY,
                entity_type="Order",
                entity_id=o.id,
                actor_id=user.id,
                target_user_id=user.id,
                ip_address=_client_ip(request),
                user_agent=request.headers.get("user-agent"),
                old_values={
                    "price": old_price,
                    "trigger_price": old_trigger,
                    "lots": old_lots,
                },
                new_values={
                    "price": str(o.price) if o.price is not None else None,
                    "trigger_price": str(o.trigger_price) if o.trigger_price is not None else None,
                    "lots": o.lots,
                },
                metadata={
                    "symbol": o.instrument.symbol,
                    "action": o.action.value,
                    "order_type": o.order_type.value,
                    "product_type": o.product_type.value,
                    "quantity": o.quantity,
                    "price": str(o.price) if o.price is not None else None,
                },
            ),
            label="order_modify_audit",
        )
    except Exception:
        logger.exception("order_modify_audit_schedule_failed")
    return APIResponse(data=_serialize(o))
