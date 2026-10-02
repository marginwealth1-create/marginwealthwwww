"""Price alerts — fired by the market-data WS pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from beanie import PydanticObjectId
from pymongo import ASCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin
from app.models._types import Money
from app.models.order import InstrumentRef


class AlertType(StrEnum):
    LTP_ABOVE = "LTP_ABOVE"
    LTP_BELOW = "LTP_BELOW"
    PERCENT_CHANGE = "PERCENT_CHANGE"
    VOLUME_SPIKE = "VOLUME_SPIKE"


class AlertFrequency(StrEnum):
    ONCE = "ONCE"
    EVERY_TIME = "EVERY_TIME"


class PriceAlert(TimestampMixin):
    user_id: PydanticObjectId
    instrument: InstrumentRef
    alert_type: AlertType = AlertType.LTP_ABOVE
    target_price: Money | None = None
    target_percent: float | None = None
    frequency: AlertFrequency = AlertFrequency.ONCE
    note: str | None = None

    is_active: bool = True
    is_triggered: bool = False
    triggered_at: datetime | None = None
    triggered_count: int = 0

    class Settings:
        name = "price_alerts"
        indexes = [
            IndexModel(
                [("is_active", ASCENDING), ("instrument.token", ASCENDING)]
            ),
            IndexModel([("user_id", ASCENDING), ("is_active", ASCENDING)]),
            IndexModel([("is_triggered", ASCENDING)]),
        ]
