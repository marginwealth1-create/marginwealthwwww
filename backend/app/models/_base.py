"""Common base classes and enums shared across Beanie documents."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from beanie import Document
from pydantic import ConfigDict, Field

from app.utils.time_utils import now_utc


class StrEnum(str, Enum):
    """str-based Enum where str() returns the value (not 'ClassName.MEMBER').

    Python 3.11+ changed Enum.__str__ to qualified-name form even for
    str-mixin enums; this restores the friendly behavior for JSON output.
    """

    def __str__(self) -> str:  # type: ignore[override]
        return self.value


class TimestampMixin(Document):
    """Adds auto-managed created_at / updated_at to any document.

    Beanie's `before_event` hooks (Insert/Update/Save) refresh `updated_at`.
    """

    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    class Settings:
        use_state_management = True
        validate_on_save = True

    async def save_changes(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.updated_at = now_utc()
        return await super().save_changes(*args, **kwargs)


# ── Shared enums ─────────────────────────────────────────────────────
class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"
    MCX = "MCX"
    NFO = "NFO"
    BFO = "BFO"
    CDS = "CDS"
    CRYPTO = "CRYPTO"


class SegmentType(StrEnum):
    """The 20 trading segments for which Settings exist."""

    NSE_EQUITY = "NSE_EQUITY"
    NSE_FUTURE = "NSE_FUTURE"
    NSE_INDEX_FUTURE = "NSE_INDEX_FUTURE"
    NSE_STOCK_OPTION_BUY = "NSE_STOCK_OPTION_BUY"
    NSE_STOCK_OPTION_SELL = "NSE_STOCK_OPTION_SELL"
    NSE_INDEX_OPTION_BUY = "NSE_INDEX_OPTION_BUY"
    NSE_INDEX_OPTION_SELL = "NSE_INDEX_OPTION_SELL"
    BSE_EQUITY = "BSE_EQUITY"
    BSE_FUTURE = "BSE_FUTURE"
    BSE_INDEX_FUTURE = "BSE_INDEX_FUTURE"
    BSE_OPTION_BUY = "BSE_OPTION_BUY"
    BSE_OPTION_SELL = "BSE_OPTION_SELL"
    MCX_FUTURE = "MCX_FUTURE"
    MCX_OPTION_BUY = "MCX_OPTION_BUY"
    MCX_OPTION_SELL = "MCX_OPTION_SELL"
    CDS_FUTURE = "CDS_FUTURE"
    CDS_OPTION_BUY = "CDS_OPTION_BUY"
    CDS_OPTION_SELL = "CDS_OPTION_SELL"
    CRYPTO_SPOT = "CRYPTO_SPOT"
    CRYPTO_FUTURE = "CRYPTO_FUTURE"


ALL_SEGMENTS: tuple[SegmentType, ...] = tuple(SegmentType)


class ProductType(StrEnum):
    MIS = "MIS"  # Margin Intraday Squareoff
    CNC = "CNC"  # Cash and Carry (delivery)
    NRML = "NRML"  # Normal (carry-forward F&O)


class OrderAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"  # Stop Loss limit
    SL_M = "SL_M"  # Stop Loss market


class Validity(StrEnum):
    DAY = "DAY"
    IOC = "IOC"


class CommissionType(StrEnum):
    PER_LOT = "PER_LOT"
    PER_CRORE = "PER_CRORE"
    PERCENTAGE = "PERCENTAGE"
    FLAT = "FLAT"


class InstrumentType(StrEnum):
    EQ = "EQ"
    FUT = "FUT"
    CE = "CE"
    PE = "PE"
    INDEX = "INDEX"
    SPOT = "SPOT"
    PERP = "PERP"


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


# Tri-state permission level — used by broker permissions (admin → broker
# grant, broker → sub-broker grant). Sub-admin permissions (super-admin →
# admin grant) remain boolean — only the broker boundary uses this enum.
# Ordering: OFF < VIEW < EDIT for the comparison helper.
class PermissionLevel(StrEnum):
    OFF = "OFF"
    VIEW = "VIEW"
    EDIT = "EDIT"

    @classmethod
    def at_least(cls, actual: "PermissionLevel", required: "PermissionLevel") -> bool:
        """True when `actual` satisfies the `required` minimum level."""
        order = {cls.OFF: 0, cls.VIEW: 1, cls.EDIT: 2}
        return order.get(actual, 0) >= order.get(required, 0)
