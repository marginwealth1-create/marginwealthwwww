"""Book brokerage the way production does: a MARKET order through
`matching_engine.execute_market_order`, priced by a real netting commission.

No ledger row is seeded. Whatever the fill path writes (a CHARGES row, a dues
row, Trade.brokerage) is what the consumer under test gets to read.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from bson import Decimal128

from app.models._base import Exchange, OrderAction, OrderType, ProductType
from app.models.netting import NettingFieldsRequired, NettingSegment
from app.models.order import InstrumentRef, Order
from app.models.position import Position, PositionStatus
from app.models.position_settlement import SettlementBatch
from app.models.trade import Trade
from app.models.user import User
from app.services import market_data_service, matching_engine, wallet_service, weekly_settlement_service
from app.utils.time_utils import now_utc

# Fake token: no live quote can exist for it, so the fill books at cached_ltp.
INSTRUMENT = InstrumentRef(
    token="TBKG1",
    symbol="TBKG",
    trading_symbol="TBKG-EQ",
    exchange=Exchange.NSE,
    segment="NSE_EQUITY",
    lot_size=1,
)


async def set_commission_per_lot(value: float) -> None:
    """The admin's NSE_EQ Segment Settings row: ₹`value` per lot, both legs."""
    fields = NettingFieldsRequired().model_dump()
    fields.update(commissionType="per_lot", commission=value, chargeOn="both")
    await NettingSegment(name="NSE_EQ", displayName="NSE_EQ", **fields).insert()


async def _no_quote(_token: str) -> dict:
    return {}


async def market(
    user: User,
    action: str,
    qty: float,
    price: str,
    *,
    at: datetime | None = None,
    product: ProductType = ProductType.MIS,
    basis: str | None = None,
    placed_at: datetime | None = None,
    instrument: InstrumentRef = INSTRUMENT,
    margin: str | None = None,
) -> Trade:
    """Fill a MARKET order for `user`. `at` moves only the Trade's clock into a
    test's window; the brokerage on it is whatever the engine charged.
    `product` is the order's product type as order_service stamps it (NRML once
    an open carried position makes a later order adopt it). `basis` is the
    tapped fill's price on an Active-tab specific-lot Exit. `placed_at` backdates
    the order, as for a parked exit that fires now. `margin` is blocked on the
    wallet and carried on the order, as order_service does at placement."""
    order = Order(
        order_number=f"TB{secrets.token_hex(6)}",
        user_id=user.id,
        instrument=instrument,
        action=OrderAction(action),
        order_type=OrderType.MARKET,
        product_type=product,
        lots=qty,
        quantity=qty,
        pending_quantity=qty,
        is_demo=user.is_demo,
        placed_by=user.id,
        cost_basis_override=Decimal(basis) if basis is not None else None,
    )
    if placed_at is not None:
        order.created_at = placed_at
    if margin is not None:
        await wallet_service.block_margin(user.id, Decimal(margin))
        order.margin_blocked = Decimal128(margin)
    await order.insert()
    with patch.object(market_data_service, "get_quote", _no_quote):
        trade = await matching_engine.execute_market_order(
            order, cached_ltp=Decimal(price)
        )
    if at is not None:
        await Trade.get_motor_collection().update_one(
            {"_id": trade.id}, {"$set": {"executed_at": at}}
        )
    return trade


_CLOCKS = {
    "trades": ("executed_at", "created_at"),
    "positions": ("opened_at", "closed_at", "reopened_at", "created_at"),
    "orders": ("created_at", "executed_at"),
    "wallet_transactions": ("created_at",),
    "position_settlements": ("settled_at", "created_at"),
}


async def later(db, *, minutes: float = 0, seconds: float = 0) -> None:
    """Time passes: every stored clock moves back, so what comes next happens
    that much after everything already written."""
    ms = int((minutes * 60 + seconds) * 1000)
    for coll, fields in _CLOCKS.items():
        shift: dict = {
            f: {"$cond": [{"$eq": [{"$type": f"${f}"}, "date"]}, {"$subtract": [f"${f}", ms]}, f"${f}"]}
            for f in fields
        }
        if coll == "positions":  # a reopen's (closed, reopened) spans
            shift["reopen_gaps"] = {
                "$map": {
                    "input": {"$ifNull": ["$reopen_gaps", []]},
                    "as": "g",
                    "in": {"$map": {"input": "$$g", "as": "d", "in": {"$subtract": ["$$d", ms]}}},
                }
            }
        await db[coll].update_many({}, [{"$set": shift}])


async def settle_weekly(user: User, monkeypatch, price: str) -> None:
    """The real weekly settlement of `user`'s open position at `price`: it
    closes the position and re-opens the units there, writing no fill."""

    async def _ltp(_token):
        return Decimal(price)

    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    batch = SettlementBatch(run_key=f"T{price}", week_key=f"2026-W{price}", started_at=now_utc())
    await batch.insert()
    pos = await Position.find_one(Position.user_id == user.id, Position.status == PositionStatus.OPEN)
    settled = await weekly_settlement_service._settle_one_position(pos, batch=batch, fx_rate=Decimal(1))
    assert settled == "settled"
