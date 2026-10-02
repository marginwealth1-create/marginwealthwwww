"""Watchlist + WatchlistItem (max 10 watchlists per user — enforce in service)."""

from __future__ import annotations

from beanie import PydanticObjectId
from pymongo import ASCENDING, IndexModel

from app.models._base import Exchange, TimestampMixin


class Watchlist(TimestampMixin):
    user_id: PydanticObjectId
    name: str
    sort_order: int = 0
    is_default: bool = False

    class Settings:
        name = "watchlists"
        indexes = [
            IndexModel([("user_id", ASCENDING), ("sort_order", ASCENDING)]),
            IndexModel([("user_id", ASCENDING), ("name", ASCENDING)], unique=True),
        ]


class WatchlistItem(TimestampMixin):
    watchlist_id: PydanticObjectId
    instrument_token: str
    symbol: str
    exchange: Exchange
    sort_order: int = 0

    class Settings:
        name = "watchlist_items"
        indexes = [
            IndexModel(
                [("watchlist_id", ASCENDING), ("instrument_token", ASCENDING)], unique=True
            ),
            IndexModel([("watchlist_id", ASCENDING), ("sort_order", ASCENDING)]),
        ]
