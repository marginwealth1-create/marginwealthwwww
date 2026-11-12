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

from app.models._base import Exchange, OrderAction, OrderType, ProductType
from app.models.netting import NettingFieldsRequired, NettingSegment
from app.models.order import InstrumentRef, Order
from app.models.trade import Trade
from app.models.user import User
from app.services import market_data_service, matching_engine

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
) -> Trade:
    """Fill a MARKET order for `user`. `at` moves only the Trade's clock into a
    test's window; the brokerage on it is whatever the engine charged.
    `product` is the order's product type as order_service stamps it (NRML once
    an open carried position makes a later order adopt it). `basis` is the
    tapped fill's price on an Active-tab specific-lot Exit."""
    order = Order(
        order_number=f"TB{secrets.token_hex(6)}",
        user_id=user.id,
        instrument=INSTRUMENT,
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
