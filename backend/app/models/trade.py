"""Trade — a single fill produced by the internal matching engine.

One Order may generate multiple Trades (partial fills). Charges are split
proportionally across fills.
"""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed, PydanticObjectId
from bson import Decimal128
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import OrderAction, ProductType, TimestampMixin
from app.models._types import Money
from app.models.order import InstrumentRef
from app.utils.time_utils import now_utc


def _zero() -> Decimal128:
    return Decimal128("0")


class Trade(TimestampMixin):
    trade_number: Indexed(str, unique=True)  # type: ignore[valid-type]
    order_id: PydanticObjectId
    user_id: PydanticObjectId

    instrument: InstrumentRef
    action: OrderAction
    product_type: ProductType

    quantity: float
    price: Money
    value: Money = Field(default_factory=_zero)  # quantity * price

    # The only charge users pay on this platform is the platform's own
    # brokerage. No statutory pass-through (STT / exchange / SEBI / stamp /
    # DP / GST) — admin policy.
    brokerage: Money = Field(default_factory=_zero)
    total_charges: Money = Field(default_factory=_zero)  # = brokerage
    net_amount: Money = Field(default_factory=_zero)  # value ± total_charges

    # Realized P&L in INR, captured at fill time. Set only on closing fills
    # (legs that reduce or flatten a position); opening fills leave it None.
    # For USD-quoted instruments (Infoway feed) we snapshot the USD/INR
    # rate at execute time and bake the conversion in here — the History
    # tab then renders this stored value directly instead of recomputing
    # against a live LTP, so closed-trade P&L is fixed forever and shown
    # in INR regardless of how the underlying instrument is quoted.
    # Already net of the closing-leg brokerage (when `chargeOn` includes
    # the close), so the displayed P&L matches the user's true cost.
    pnl_inr: Money | None = None

    # ── Execution snapshot: what the market looked like at this fill ───
    # Slippage and latency cannot be reconstructed after the fact — for a
    # MARKET order `price` is the fill and nothing recorded what it was
    # measured against, or how fresh the feed was. All optional: trades
    # booked before this existed carry None and are skipped by the reports.
    expected_price: Money | None = None  # the price the client's screen asked for
    reference_ltp: Money | None = None  # LTP before our markup
    bid_at_fill: Money | None = None
    ask_at_fill: Money | None = None
    markup: Money | None = None  # fill − reference: the spread we took
    tick_age_ms: int | None = None  # how old the feed tick was when we filled
    fill_latency_ms: int | None = None  # order accepted → filled

    # Specific-lot close: the tapped fill's entry price this closing trade was
    # booked against (Active-tab Exit). Mirrors the originating Order field so
    # the FIFO closed-blotter (list_closed_trade_events_fifo) can pair this
    # close with the SAME lot the money booked against — not the FIFO front —
    # keeping the Closed-tab entry price and the wallet consistent. None on
    # every other trade → pure FIFO display, unchanged.
    cost_basis_override: Money | None = None

    # Set True when an admin REOPEN (or DELETE) undoes the close this trade
    # belongs to. The close fill stays in the collection for the audit trail,
    # but the user-facing Closed blotter (list_closed_trade_events_fifo) hides
    # superseded fills so a reopened-then-reclosed position shows only its
    # FINAL close — matching the admin Position-doc view. The wallet P&L is
    # already netted via the reopen's REVERSAL; this only governs display.
    superseded_by_reopen: bool = False

    executed_at: datetime = Field(default_factory=now_utc)

    class Settings:
        name = "trades"
        indexes = [
            IndexModel([("trade_number", ASCENDING)], unique=True),
            IndexModel([("user_id", ASCENDING), ("executed_at", DESCENDING)]),
            IndexModel([("order_id", ASCENDING)]),
            IndexModel([("instrument.token", ASCENDING), ("executed_at", DESCENDING)]),
            IndexModel([("executed_at", DESCENDING)]),
        ]
