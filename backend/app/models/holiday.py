"""Trading holiday calendar."""

from __future__ import annotations

from datetime import date

from pymongo import ASCENDING, IndexModel

from app.models._base import Exchange, TimestampMixin


class TradingHoliday(TimestampMixin):
    holiday_date: date
    exchange: Exchange = Exchange.NSE
    description: str
    is_full_day: bool = True
    # If half-day:
    open_time: str | None = None
    close_time: str | None = None
    # Special "Muhurat" / settlement-only marker
    is_muhurat: bool = False

    class Settings:
        name = "trading_holidays"
        indexes = [
            IndexModel(
                [("exchange", ASCENDING), ("holiday_date", ASCENDING)], unique=True
            ),
            IndexModel([("holiday_date", ASCENDING)]),
        ]
