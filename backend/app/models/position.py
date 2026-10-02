"""Position + UserPositionTracker.

`Position` represents a *currently open* directional exposure (or today's
closed intraday positions). `UserPositionTracker` is a small denormalised
counter used by the order validator for fast lot-limit checks (no aggregation).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from beanie import PydanticObjectId
from bson import Decimal128
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import OrderAction, ProductType, StrEnum, TimestampMixin
from app.models._types import Money
from app.models.order import InstrumentRef


def _zero() -> Decimal128:
    return Decimal128("0")


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class Position(TimestampMixin):
    user_id: PydanticObjectId
    instrument: InstrumentRef
    segment_type: str
    product_type: ProductType

    quantity: float = 0  # signed; positive = long, negative = short

    # The direction the user opened this position with. STABLE across the
    # position's lifetime — even after the closing leg flattens `quantity`
    # back to 0, this still says BUY (for a long that was sold to close)
    # or SELL (for a short that was bought to close). The Closed-tab card
    # reads this so it can render "SELL BTCUSD" vs "BUY BTCUSD" correctly;
    # without it the UI fell back to `quantity > 0 ? BUY : SELL`, which
    # defaulted every closed row to SELL (the "BUY karta hu but Closed me
    # SELL dikhta hai" symptom). Updated on direction-flips so the active
    # side is always the source of truth.
    opened_side: OrderAction | None = None

    # Peak absolute size this position ever held during its current lifecycle.
    # Captured on first fill, grown on same-side pyramid fills, reset on
    # reopen-after-close / direction-flip. Never decremented on partial or
    # full close — so the History/Closed tab can show the size the user
    # actually had at close-time (where ``quantity`` is 0).
    opening_quantity: float | None = None

    avg_price: Money = Field(default_factory=_zero)
    ltp: Money = Field(default_factory=_zero)

    realized_pnl: Money = Field(default_factory=_zero)
    unrealized_pnl: Money = Field(default_factory=_zero)
    margin_used: Money = Field(default_factory=_zero)

    # Bracket legs — optional SL / target attached to this open position.
    # The auto-squareoff worker compares LTP against these on every tick;
    # the user can also edit them inline from the positions strip.
    stop_loss: Money | None = None
    target: Money | None = None

    # Snapshot of stop_loss / target at the moment the position closed.
    # apply_fill clears the live `stop_loss` / `target` to 0 on full close
    # so a future reopen on the same instrument doesn't inherit stale
    # brackets; we copy them HERE first so the Closed tab can still show
    # "Trade had SL ₹X, TP ₹Y" even after the live fields are wiped.
    # Operator-flagged 22-May: "close trade me bhi user ko save SL/TP
    # dikhe — kitna laga tha pata chale".
    close_stop_loss: Money | None = None
    close_target: Money | None = None

    # FX rates frozen at trade open / close — used to convert USD-quoted
    # P&L (crypto, forex, currency-derivatives) into INR for the wallet.
    # ``None`` for INR-native instruments (NSE / BSE / MCX / NFO / BFO).
    open_usd_inr_rate: Money | None = None
    close_usd_inr_rate: Money | None = None

    status: PositionStatus = PositionStatus.OPEN
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    # How this position was closed. Stamped by the squareoff path that
    # actually flips status → CLOSED. The UI shows this on the Closed tab
    # so the user knows why a position was flattened (especially useful
    # for SL/TP auto-fires that happened while they were away from the app).
    #
    # Known tags:
    #   "SL_HIT"     — bracket stop-loss hit (risk_enforcer auto-squareoff)
    #   "TP_HIT"     — bracket take-profit hit (risk_enforcer auto-squareoff)
    #   "STOP_OUT"   — margin stop-out (risk_enforcer global flatten)
    #   "USER"       — user tapped Close in the app / web
    #   "AUTO"       — other automated close (EOD, expiry, etc.)
    close_reason: str | None = None
    is_demo: bool = False  # True for demo-account positions; excluded from admin views

    # Per-position user Carry-Forward opt-in. Only meaningful when the owning
    # admin enabled `carry_forward_toggle_enabled`. Default False ⇒ the user
    # has NOT opted this position into overnight carry, so the EOD rollover
    # squares it off. Set True from the app's per-position toggle to carry it.
    user_carry_forward: bool = False

    # Set by the admin reopen endpoint. Used by _charges_for to clamp the
    # Trade-row window to only include trades from THIS open cycle, not all
    # cycles since opened_at — prevents brokerage accumulation across
    # multiple close/reopen/close sequences.
    reopened_at: datetime | None = None

    class Settings:
        name = "positions"
        indexes = [
            IndexModel([("user_id", ASCENDING), ("status", ASCENDING)]),
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("instrument.token", ASCENDING),
                    ("product_type", ASCENDING),
                    ("status", ASCENDING),
                ]
            ),
            IndexModel([("status", ASCENDING), ("instrument.token", ASCENDING)]),
            # (status, user_id) — covers the sharded risk loop's per-tick
            # `distinct("user_id", {status: OPEN})`: status prefix selects the
            # open book and user_id is served straight from the index (no doc
            # fetch), so each shard resolves "which users do I own" without
            # deserialising positions it will discard.
            IndexModel([("status", ASCENDING), ("user_id", ASCENDING)]),
            IndexModel([("opened_at", DESCENDING)]),
        ]


class UserPositionTracker(TimestampMixin):
    """Per-(user, segment, instrument) lot counters. Updated atomically on fill.

    Avoids aggregation during the 12-check validator hot path.
    """

    user_id: PydanticObjectId
    segment_type: str
    instrument_token: str

    intraday_lots: float = 0  # MIS lots currently held
    holding_lots: float = 0  # NRML/CNC lots
    total_lots: float = 0  # sum of abs(intraday) + abs(holding)
    margin_blocked: Money = Field(default_factory=_zero)

    class Settings:
        name = "user_position_tracker"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("segment_type", ASCENDING),
                    ("instrument_token", ASCENDING),
                ],
                unique=True,
            ),
        ]
