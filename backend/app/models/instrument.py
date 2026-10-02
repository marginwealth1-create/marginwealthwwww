"""Master instrument catalogue — equities, futures, options, crypto."""

from __future__ import annotations

from datetime import date

from beanie import Indexed, Insert, Replace, Save, SaveChanges, before_event
from bson import Decimal128
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import Exchange, InstrumentType, OptionType, TimestampMixin
from app.models._types import Money


class Instrument(TimestampMixin):
    token: Indexed(str, unique=True)  # type: ignore[valid-type]
    symbol: str  # e.g., "RELIANCE", "NIFTY24DECFUT"
    trading_symbol: str  # broker-format symbol (NSE: "RELIANCE-EQ")
    name: str  # full company / contract name

    # Lowercased shadow copies of the searchable fields. The `instruments`
    # collection accumulates every on-demand-subscribed contract (mirrored
    # from Zerodha), so it grows to tens of thousands of rows. The old search
    # used a case-insensitive, UN-anchored regex which no B-tree index can
    # serve — every keystroke scanned the WHOLE collection (~15k docs, ~150 ms
    # CPU each), starving the worker event loop at market open. These
    # lowercased fields are indexed and queried with an ANCHORED `^prefix`
    # regex, which IS an index range-scan → the search stays O(log n) no
    # matter how large the catalog grows. Kept in sync automatically by the
    # `_sync_search_keys` hook below, so no call site has to set them.
    symbol_lc: str = ""
    trading_symbol_lc: str = ""
    name_lc: str = ""

    exchange: Exchange
    segment: str  # NSE_EQUITY, NFO_OPT, etc — matches SegmentType where possible
    instrument_type: InstrumentType
    isin: str | None = None

    # Derivatives
    expiry: date | None = None
    strike: Money | None = None
    option_type: OptionType | None = None
    underlying_token: str | None = None  # for derivatives, points to spot

    # Trading params
    lot_size: int = 1
    tick_size: Money = Field(default_factory=lambda: Decimal128("0.05"))
    upper_circuit: Money | None = None
    lower_circuit: Money | None = None

    # Status
    is_active: bool = True
    is_tradable: bool = True
    is_halted: bool = False
    halt_reason: str | None = None

    class Settings:
        name = "instruments"
        indexes = [
            IndexModel([("token", ASCENDING)], unique=True),
            IndexModel([("symbol", ASCENDING)]),
            IndexModel([("exchange", ASCENDING), ("segment", ASCENDING)]),
            IndexModel([("instrument_type", ASCENDING)]),
            IndexModel([("expiry", ASCENDING)]),
            IndexModel([("underlying_token", ASCENDING), ("expiry", ASCENDING)]),
            IndexModel([("is_tradable", ASCENDING), ("is_active", ASCENDING)]),
            IndexModel([("created_at", DESCENDING)]),
            # Text index for search across name + symbol
            IndexModel([("name", "text"), ("symbol", "text"), ("trading_symbol", "text")]),
            # Prefix-search indexes: the `^q` anchored regex the search uses is
            # an index range-scan on these, so autocomplete never full-scans.
            # Compound with is_active so the common `is_active:true` filter is
            # covered too.
            IndexModel([("is_active", ASCENDING), ("symbol_lc", ASCENDING)]),
            IndexModel([("is_active", ASCENDING), ("trading_symbol_lc", ASCENDING)]),
            IndexModel([("is_active", ASCENDING), ("name_lc", ASCENDING)]),
        ]

    @before_event(Insert, Replace, Save, SaveChanges)
    def _sync_search_keys(self) -> None:
        """Keep the lowercased search fields in sync on every write. Runs for
        the on-demand mirror upserts (``Instrument(...).insert()`` /
        ``existing.save()``) and every seed path that goes through Beanie, so
        the prefix-search fields are always populated without any call site
        having to remember. Raw motor ``update_many`` writes bypass this — the
        one-time backfill migration covers those + pre-existing rows."""
        self.symbol_lc = (self.symbol or "").lower()
        self.trading_symbol_lc = (self.trading_symbol or "").lower()
        self.name_lc = (self.name or "").lower()
