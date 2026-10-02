"""Admin-defined horizontal price lines drawn on the user's chart.

One document per (owner, instrument). The admin fills these in through an
Excel round-trip: download a template for a segment, type prices and colours
next to each symbol, upload it back. The chart then draws each price as a
horizontal line in the colour that was given.

Ownership mirrors ``AdminCryptoConfig`` exactly so the resolver cascade is the
same one used everywhere else: an admin's own rows, else their parent's, else
the platform default (both owner ids null).
"""

from __future__ import annotations

from beanie import PydanticObjectId
from bson import Decimal128
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, IndexModel

from app.models._base import TimestampMixin


class ChartLevelEntry(BaseModel):
    """One horizontal line: a price and the colour to draw it in."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    price: Decimal128
    # #RRGGBB. Validated on import so a typo can't reach the chart as a
    # broken CSS colour, which TradingView silently renders as black.
    color: str = "#E31E24"
    label: str | None = None


class ChartLevel(TimestampMixin):
    owner_admin_id: PydanticObjectId | None = None
    owner_broker_id: PydanticObjectId | None = None

    token: str
    symbol: str
    segment: str

    levels: list[ChartLevelEntry] = Field(default_factory=list)

    class Settings:
        name = "chart_levels"
        indexes = [
            # One row per owner per instrument — the Excel import upserts on
            # this, so re-uploading a corrected sheet replaces rather than
            # duplicates.
            IndexModel(
                [
                    ("owner_admin_id", ASCENDING),
                    ("owner_broker_id", ASCENDING),
                    ("token", ASCENDING),
                ],
                unique=True,
            ),
            # The user-side read is always "this owner, this segment".
            IndexModel([("owner_admin_id", ASCENDING), ("segment", ASCENDING)]),
        ]
