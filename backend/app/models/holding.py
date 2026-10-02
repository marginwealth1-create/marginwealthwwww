"""Holdings — long-term CNC equity positions (delivery).

A Holding is created/incremented when a CNC buy is fulfilled, decremented
on CNC sell. Settlement (T+1/T+2) is handled by EOD jobs.
"""

from __future__ import annotations

from beanie import PydanticObjectId
from bson import Decimal128
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models._base import TimestampMixin
from app.models._types import Money
from app.models.order import InstrumentRef


def _zero() -> Decimal128:
    return Decimal128("0")


class Holding(TimestampMixin):
    user_id: PydanticObjectId
    instrument: InstrumentRef

    quantity: float = 0
    avg_price: Money = Field(default_factory=_zero)
    ltp: Money = Field(default_factory=_zero)

    invested_value: Money = Field(default_factory=_zero)  # qty * avg_price
    current_value: Money = Field(default_factory=_zero)  # qty * ltp
    pnl: Money = Field(default_factory=_zero)
    pnl_percentage: float = 0.0

    # Settlement bucket — T+1 in process, T+2 ready, etc.
    pending_settlement_qty: float = 0

    class Settings:
        name = "holdings"
        indexes = [
            IndexModel(
                [("user_id", ASCENDING), ("instrument.token", ASCENDING)], unique=True
            ),
            IndexModel([("user_id", ASCENDING)]),
        ]
