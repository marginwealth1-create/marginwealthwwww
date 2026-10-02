"""1-minute tick snapshots for Bid/Ask Rate History.

Stores aggregated bid high/low, ask high/low, OHLC per instrument token
per minute. Background `tick_aggregator` service writes these; admin
Rate History modal reads them. 30-day TTL via `expires_at` index.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.utils.time_utils import now_utc


def _expires_default() -> datetime:
    return now_utc() + timedelta(days=30)


class TickSnapshot(Document):
    token: str
    timestamp: datetime

    bid_high: float = 0.0
    bid_low: float = 0.0
    ask_high: float = 0.0
    ask_low: float = 0.0

    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0

    expires_at: datetime = Field(default_factory=_expires_default)

    class Settings:
        name = "tick_snapshots"
        indexes = [
            IndexModel([("token", ASCENDING), ("timestamp", DESCENDING)]),
            IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0),
        ]
