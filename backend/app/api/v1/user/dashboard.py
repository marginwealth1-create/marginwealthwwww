"""User dashboard summary endpoint."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter

from app.core.dependencies import CurrentUser
from app.models.holding import Holding
from app.models.order import Order, OrderStatus
from app.models.position import Position, PositionStatus
from app.schemas.common import APIResponse
from app.services import market_data_service, wallet_service
from app.utils.decimal_utils import to_decimal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["user-dashboard"])


def _wallet_to_jsonable(w) -> dict:
    """`wallet_service.summary()` returns a Pydantic model on some paths and a
    plain dict on others — normalise so the front-end always sees the same shape."""
    if w is None:
        return {}
    if hasattr(w, "model_dump"):
        return w.model_dump(mode="json")
    if hasattr(w, "dict"):
        return w.dict()
    return dict(w)


@router.get("/summary", response_model=APIResponse[dict])
async def summary(user: CurrentUser):
    wallet = await wallet_service.summary(user.id)

    # Open positions
    try:
        open_positions = await Position.find(
            Position.user_id == user.id,
            Position.status == PositionStatus.OPEN,
        ).to_list()
    except Exception:
        logger.exception("dashboard_open_positions_failed")
        open_positions = []

    # Pending orders — Beanie's `.in_()` on enum-backed fields is unreliable,
    # so query with a raw Mongo `$in` dict instead.
    try:
        pending_orders = await Order.find(
            Order.user_id == user.id,
            {"status": {"$in": [OrderStatus.OPEN.value, OrderStatus.PENDING.value, OrderStatus.PARTIAL.value]}},
        ).count()
    except Exception:
        logger.exception("dashboard_pending_orders_failed")
        pending_orders = 0

    # Holdings
    try:
        holdings = await Holding.find(Holding.user_id == user.id).to_list()
    except Exception:
        logger.exception("dashboard_holdings_failed")
        holdings = []

    # Fetch all LTPs in one parallel gather (holdings + positions tokens combined)
    all_tokens = list(
        {h.instrument.token for h in holdings}
        | {p.instrument.token for p in open_positions}
    )
    if all_tokens:
        _ltp_results = await asyncio.gather(
            *[market_data_service.get_ltp(tok) for tok in all_tokens],
            return_exceptions=True,
        )
        ltp_map: dict[str, float] = {
            tok: (float(v) if not isinstance(v, Exception) and v else 0.0)
            for tok, v in zip(all_tokens, _ltp_results)
        }
    else:
        ltp_map = {}

    holdings_value = 0.0
    holdings_invested = 0.0
    for h in holdings:
        try:
            ltp = ltp_map.get(h.instrument.token, 0.0)
            holdings_value += ltp * float(h.quantity)
            holdings_invested += float(to_decimal(h.avg_price)) * float(h.quantity)
        except Exception:
            logger.exception("dashboard_holding_calc_failed")

    today_pnl = 0.0
    for p in open_positions:
        try:
            ltp = ltp_map.get(p.instrument.token, 0.0)
            today_pnl += (ltp - float(to_decimal(p.avg_price))) * float(p.quantity)
            today_pnl += float(to_decimal(p.realized_pnl))
        except Exception:
            logger.exception("dashboard_position_pnl_failed")

    return APIResponse(
        data={
            "wallet": _wallet_to_jsonable(wallet),
            "open_positions": len(open_positions),
            "pending_orders": pending_orders,
            "holdings_count": len(holdings),
            "holdings_value": round(holdings_value, 2),
            "holdings_invested": round(holdings_invested, 2),
            "holdings_pnl": round(holdings_value - holdings_invested, 2),
            "today_pnl": round(today_pnl, 2),
        }
    )
