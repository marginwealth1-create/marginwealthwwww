"""Netting Segment + Risk Management models.

Replaces the old `segment_settings` family. Built from the bharat_indian_funded
admin panel as the reference, adapted for FastAPI + Beanie.

Hierarchy:
    NettingSegment (one row per segment, e.g. NSE_EQ, NSE_FUT, FOREX, ...)
        ↓
    NettingScriptOverride (per-symbol override within a segment; null = inherit)
        ↓
    UserSegmentOverride (per-user override; null = inherit)

Plus:
    RiskSettings        — global default risk controls
    UserRiskSettings    — per-user risk overrides (null = inherit)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from beanie import Indexed, PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.models._base import TimestampMixin


# ── Risk Management ────────────────────────────────────────────────
class RiskSettingsBase(BaseModel):
    """Per-user override layer — every field nullable, missing = inherit
    global default. Five knobs total:

      • stopOutWarningPercent — notify user when (-total_pnl) / balance
        crosses this %. Balance = available + used_margin + credit_limit.
      • stopOutPercent        — force-close ALL open positions when the
        same ratio crosses this %.
      • exitOnlyMode          — when True, validator rejects every order
        that would open / increase a position; closing trades pass.
      • profitTradeHoldMinSeconds — minimum seconds a winning trade must
        be held before a user-initiated close is allowed.
      • lossTradeHoldMinSeconds   — same, for losing trades.

    Removed by simplification request: ledgerBalanceClose, marginCallLevel
    (old equity/used-margin formula), stopOutLevel (old formula),
    blockLimitAboveBelowHighLow, blockLimitBetweenHighLow.
    """

    stopOutWarningPercent: float | None = None  # % of balance
    stopOutPercent: float | None = None  # % of balance
    exitOnlyMode: bool | None = None
    profitTradeHoldMinSeconds: int | None = None
    lossTradeHoldMinSeconds: int | None = None
    staleFeedBlockSec: int | None = None


class RiskSettingsRequired(BaseModel):
    """Global default — all required, sane fallbacks. Pair with
    RiskSettingsBase via inheritance below."""

    # 0 here means "feature off" — no warning is sent regardless of P&L.
    # Same for the stop-out: 0 disables the auto-flatten.
    stopOutWarningPercent: float = 0.0
    stopOutPercent: float = 0.0
    exitOnlyMode: bool = False
    profitTradeHoldMinSeconds: int = 0
    lossTradeHoldMinSeconds: int = 0
    # Block new orders on an instrument whose exchange price has not moved
    # for this long. 0 = off. A 60-second version of this was removed in
    # July 2026 for false-blocking during feed hiccups, so it is a setting
    # now: loosen or disable it without a deploy.
    staleFeedBlockSec: int = 30


class RiskSettings(TimestampMixin, RiskSettingsRequired):
    type: Indexed(str, unique=True) = "global"  # type: ignore[valid-type]

    class Settings:
        name = "risk_settings"
        indexes = [IndexModel([("type", ASCENDING)], unique=True)]


class UserRiskSettings(TimestampMixin, RiskSettingsBase):
    user_id: PydanticObjectId

    class Settings:
        name = "user_risk_settings"
        indexes = [IndexModel([("user_id", ASCENDING)], unique=True)]


class SubAdminRiskSettings(TimestampMixin, RiskSettingsBase):
    """Per-sub-admin "global default" risk knobs.

    Layered between the platform `RiskSettings` (super-admin's global) and
    `UserRiskSettings` (per-user override). Each sub-admin gets at most
    one row keyed by their user `_id`. Null fields inherit from the
    platform global; populated fields override it for every user assigned
    to this sub-admin (unless that user has their own override).
    """

    sub_admin_id: PydanticObjectId

    class Settings:
        name = "sub_admin_risk_settings"
        indexes = [IndexModel([("sub_admin_id", ASCENDING)], unique=True)]


class SuperAdminRiskSettings(TimestampMixin, RiskSettingsBase):
    """Super-admin's pool-default risk knobs.

    Symmetric to SubAdminRiskSettings but for the super-admin's pool
    (users with `assigned_admin_id is None`). Decouples super-admin's
    pool risk settings from the platform-wide `RiskSettings` defaults so
    super-admin's edits no longer cascade into admin / broker pools.
    """

    super_admin_id: PydanticObjectId

    class Settings:
        name = "super_admin_risk_settings"
        indexes = [IndexModel([("super_admin_id", ASCENDING)], unique=True)]


class BrokerRiskSettings(TimestampMixin, RiskSettingsBase):
    """Per-broker "pool default" risk knobs.

    Layered between platform RiskSettings and UserRiskSettings for users
    in a broker's pool. Sub-broker chains do NOT cascade — each broker's
    risk settings are independent of their parent broker.
    """

    broker_id: PydanticObjectId

    class Settings:
        name = "broker_risk_settings"
        indexes = [IndexModel([("broker_id", ASCENDING)], unique=True)]


# ── Netting Segment matrix ─────────────────────────────────────────
SEGMENT_CODES = [
    "NSE_EQ",
    # NSE F&O is split stock-vs-index so the admin can price stock and index
    # derivatives independently (NSE_FUT/NSE_OPT retired → 4 granular rows).
    "NSE_STK_FUT", "NSE_IDX_FUT", "NSE_STK_OPT", "NSE_IDX_OPT",
    "BSE_EQ", "BSE_FUT", "BSE_OPT",
    "MCX_FUT", "MCX_OPT",
    "FOREX", "STOCKS",
    "INDICES", "COMMODITIES",
    "CRYPTO",
]


class NettingFieldsBase(BaseModel):
    """All editable fields, all nullable for override layers."""

    # Lot
    minLots: float | None = None
    orderLots: float | None = None
    maxLots: float | None = None
    maxExchangeLots: float | None = None
    # Quantity
    minQty: float | None = None
    perOrderQty: float | None = None
    maxQtyPerScript: float | None = None
    # Value
    maxValue: float | None = None
    # Fixed Margin
    marginCalcMode: Literal["fixed", "times", "percent"] | None = None
    # Per-side margin mode for option Buy and Sell. NULL means "inherit
    # from the segment-level marginCalcMode above". When set, this mode
    # overrides for that side only — lets admin run, e.g., option BUY
    # in Fixed (flat ₹/lot) while option SELL is in Times (multiplier).
    optionBuyMarginCalcMode: Literal["fixed", "times", "percent"] | None = None
    optionSellMarginCalcMode: Literal["fixed", "times", "percent"] | None = None
    intradayMargin: float | None = None
    overnightMargin: float | None = None
    optionBuyIntraday: float | None = None
    optionBuyOvernight: float | None = None
    optionSellIntraday: float | None = None
    optionSellOvernight: float | None = None
    # Options — single % cap that applies to BOTH buy and sell side.
    # Replaces the old `buyingStrikeFarPercent` / `sellingStrikeFarPercent`
    # pair (admin spec: one column for option segments). Also drives the
    # option-chain dialog — strikes outside ±strikeFarPercent of the spot
    # are hidden from the table.
    strikeFarPercent: float | None = None
    # Brokerage
    commissionType: Literal["per_lot", "per_crore"] | None = None
    commission: float | None = None
    optionBuyCommission: float | None = None
    optionSellCommission: float | None = None
    chargeOn: Literal["open", "close", "both"] | None = None
    # Limit away
    limitAwayPercent: float | None = None
    # Block a parked LIMIT/SL order from resting INSIDE the day's traded
    # range (between session high & low). NULL = inherit. Independent of
    # limitAwayPercent (a % band around the current price) — both apply.
    limitWithinDayRange: bool | None = None
    # When True, this segment does NOT accept parked/pending orders at all
    # (LIMIT / SL / SL-M) — only MARKET entries and exits. NULL = inherit.
    blockPendingOrders: bool | None = None
    # Per-segment enable/disable for bracket Stop-Loss and Take-Profit.
    # NULL = inherit from the layer below (segment default is True). When
    # False the segment does NOT allow users to attach an SL / TP and the
    # risk enforcer will NOT auto-fire any existing bracket leg of that
    # kind on positions in this segment. Independent knobs so admin can,
    # e.g., permit SL but block TP on a volatile segment.
    slEnabled: bool | None = None
    tpEnabled: bool | None = None
    # Spread
    spreadType: Literal["fixed", "floating"] | None = None
    spreadPips: float | None = None
    swapType: Literal["points", "percentage"] | None = None
    swapLong: float | None = None
    swapShort: float | None = None
    swapTime: str | None = None  # HH:MM IST
    # Block
    isActive: bool | None = None
    tradingEnabled: bool | None = None
    allowOvernight: bool | None = None
    # Expiry day
    expiryProfitHoldMinSeconds: int | None = None
    expiryLossHoldMinSeconds: int | None = None
    expiryNoNewTradeDays: int | None = None
    # "HH:MM" IST at which this segment's unfilled LIMIT / SL-M orders are
    # cancelled and carried positions lose their SL / TP. Blank = never.
    pendingOrderExpiryTime: str | None = None
    expiryDayIntradayMargin: float | None = None
    expiryDayOptionBuyMargin: float | None = None
    expiryDayOptionSellMargin: float | None = None
    # When ON the three expiry-day margin values above are interpreted as
    # % of notional (just like the regular `marginCalcMode = percent` path).
    # When OFF they're flat ₹/lot — same shape as Fixed margin mode. Lets
    # admin pick the units for expiry day independently from the rest of
    # the segment (e.g. percent during normal trading but a punitive flat
    # ₹ on expiry day to discourage last-minute carries).
    expiryDayMarginAsPercent: bool | None = None


class NettingFieldsRequired(BaseModel):
    """Defaults applied to every newly-seeded segment."""

    # Lot
    minLots: float = 1.0
    orderLots: float = 1.0
    maxLots: float = 100.0
    maxExchangeLots: float = 1000.0
    # Quantity
    minQty: float = 1.0
    perOrderQty: float = 1.0
    maxQtyPerScript: float = 100000.0
    # Value
    maxValue: float = 0.0  # 0 = no cap
    # Fixed Margin
    # Default = None so the resolver's defensive inference path kicks in
    # (sniffs intradayMargin > 100 → Times, else Fixed). Picking "percent"
    # here used to silently lock fresh seeds into legacy percent mode,
    # which then ignored Times intent from the admin matrix unless the
    # admin explicitly re-clicked the dropdown. None lets admin's typed
    # number drive the inference automatically.
    marginCalcMode: Literal["fixed", "times", "percent"] | None = None
    # Per-side margin mode for option Buy and Sell. NULL = inherit from
    # segment-level marginCalcMode above. See NettingFieldsBase docstring.
    optionBuyMarginCalcMode: Literal["fixed", "times", "percent"] | None = None
    optionSellMarginCalcMode: Literal["fixed", "times", "percent"] | None = None
    intradayMargin: float = 100.0
    overnightMargin: float = 100.0
    # Option-specific columns default to None = inherit from segment-wide
    # intradayMargin / overnightMargin. Previously seeded to 100 / 15
    # which silently overrode the segment when admin set NSE_OPT
    # intradayMargin = 300 expecting all options to use 300. Now the
    # admin must explicitly type a number to override; blank/null means
    # "use whatever the segment says".
    optionBuyIntraday: float | None = None
    optionBuyOvernight: float | None = None
    optionSellIntraday: float | None = None
    optionSellOvernight: float | None = None
    # Options
    strikeFarPercent: float = 10.0
    # Brokerage
    commissionType: Literal["per_lot", "per_crore"] = "per_lot"
    commission: float = 20.0
    optionBuyCommission: float = 20.0
    optionSellCommission: float = 20.0
    chargeOn: Literal["open", "close", "both"] = "both"
    # Limit away
    # 0 = no min-distance check (operator removed the admin UI form for this,
    # so new seeds must not silently impose a 10% SL/TP band). Resolver treats
    # limit_percentage <= 0 as "check off".
    limitAwayPercent: float = 0.0
    # Block a parked LIMIT/SL order from resting inside the day's traded
    # range. Default OFF — nothing changes until an admin turns it on.
    limitWithinDayRange: bool = False
    # Block parked/pending orders (LIMIT / SL / SL-M) for this segment —
    # only MARKET entries + exits allowed. Default OFF.
    blockPendingOrders: bool = False
    # SL / TP allowed by default on every segment — preserves the existing
    # behaviour until an admin explicitly turns one off for a segment.
    slEnabled: bool = True
    tpEnabled: bool = True
    # Spread
    spreadType: Literal["fixed", "floating"] = "fixed"
    spreadPips: float = 0.0
    swapType: Literal["points", "percentage"] = "points"
    swapLong: float = 0.0
    swapShort: float = 0.0
    swapTime: str = "22:30"
    # Block
    isActive: bool = True
    tradingEnabled: bool = True
    allowOvernight: bool = True
    # Expiry day
    expiryProfitHoldMinSeconds: int = 0
    expiryLossHoldMinSeconds: int = 0
    # Final N calendar days of a contract's life (expiry day included) where
    # only closing is allowed: no fresh position, no adding lots. 0 = off.
    expiryNoNewTradeDays: int = 0
    pendingOrderExpiryTime: str = ""
    # Default = None so the resolver's "or effective_margin_pct" fallback
    # makes expiry-day margin inherit the regular intraday tier when admin
    # hasn't explicitly set a stricter value. Previously seeded to
    # 100 / 100 / 50 which silently dropped Times-mode leverage on every
    # contract's expiry day (e.g. MCX FUT 500× → 100× → 5× the margin).
    # `heal_legacy_percent_seeds()` resets existing rows that still hold
    # the legacy seed defaults to None on every boot. Admins who DO want
    # a stricter expiry tier type the value explicitly; that wins.
    expiryDayIntradayMargin: float | None = None
    expiryDayOptionBuyMargin: float | None = None
    expiryDayOptionSellMargin: float | None = None
    expiryDayMarginAsPercent: bool = True


class NettingSegment(TimestampMixin, NettingFieldsRequired):
    """One row per segment code."""

    name: Indexed(str, unique=True)  # type: ignore[valid-type]  # e.g. "NSE_EQ"
    displayName: str  # "NSE EQ"
    # UI cell-gating flags
    lotApplies: bool = True
    qtyApplies: bool = False
    optionApplies: bool = False
    expiryHoldApplies: bool = False
    futureApplies: bool = False

    class Settings:
        name = "netting_segments"
        indexes = [IndexModel([("name", ASCENDING)], unique=True)]


class NettingScriptOverride(TimestampMixin, NettingFieldsBase):
    """Per-symbol override within a segment — null fields inherit from segment.

    Tier scope (added 2026-05-20): a script override now optionally belongs
    to ONE tier. Both fields null → platform-wide (the historical default,
    set by super-admin and applies to everyone). `scope_admin_id` set →
    applies only to users whose `assigned_admin_id` matches that admin.
    `scope_broker_id` set → applies only to users whose `broker_ancestry`
    contains that broker. The unique index covers (segment, symbol, scope)
    so the same (segment, symbol) can have different overrides per tier
    without collision.

    Resolver picks the most-specific match (broker > admin > platform)
    when computing effective settings for a given user.
    """

    segment_id: PydanticObjectId
    segment_name: str  # denormalised for filter queries
    symbol: str
    tradingSymbol: str | None = None
    instrumentToken: int | None = None
    lotSize: float = 1.0
    # Tier scope — both null = platform-wide (super-admin authored).
    scope_admin_id: PydanticObjectId | None = None
    scope_broker_id: PydanticObjectId | None = None

    class Settings:
        name = "netting_script_overrides"
        indexes = [
            # Unique per (segment, symbol, scope) — null values are
            # treated as distinct keys by MongoDB, so platform +
            # per-admin + per-broker rows can coexist for the same
            # (segment, symbol).
            IndexModel(
                [
                    ("segment_name", ASCENDING),
                    ("symbol", ASCENDING),
                    ("scope_admin_id", ASCENDING),
                    ("scope_broker_id", ASCENDING),
                ],
                unique=True,
            ),
        ]


class UserSegmentOverride(TimestampMixin, NettingFieldsBase):
    """Per-user override on a segment (or specific symbol within segment)."""

    user_id: PydanticObjectId
    segment_name: str
    symbol: str | None = None  # None = applies to entire segment

    class Settings:
        name = "user_segment_overrides"
        indexes = [
            IndexModel(
                [("user_id", ASCENDING), ("segment_name", ASCENDING), ("symbol", ASCENDING)],
                unique=True,
            ),
        ]


class SubAdminSegmentOverride(TimestampMixin, NettingFieldsBase):
    """Per-sub-admin "global default" segment override.

    Layered between `NettingScriptOverride` (platform per-symbol) and
    `UserSegmentOverride` (per-user) in the resolver. Null fields inherit
    from the platform segment; populated fields apply to every user in
    this sub-admin's pool unless that user has their own override.

    Symbol-level scope is intentionally NOT supported here — that lives
    on the per-user override layer. Sub-admin defaults are segment-wide.
    """

    sub_admin_id: PydanticObjectId
    segment_name: str

    class Settings:
        name = "sub_admin_segment_overrides"
        indexes = [
            IndexModel(
                [("sub_admin_id", ASCENDING), ("segment_name", ASCENDING)],
                unique=True,
            ),
        ]


class SuperAdminSegmentOverride(TimestampMixin, NettingFieldsBase):
    """Super-admin's pool-default segment override.

    Symmetric to SubAdminSegmentOverride but for the super-admin's pool
    (users with `assigned_admin_id is None`). Decouples super-admin's
    "their pool" settings from the platform-wide `NettingSegment` seed
    defaults, so super-admin's edits no longer cascade into admin / broker
    pools as a shared fallback. Keyed by `super_admin_id` so the model
    supports multiple super-admin accounts in the future without a schema
    change. Null fields inherit from `NettingSegment` seeds.
    """

    super_admin_id: PydanticObjectId
    segment_name: str

    class Settings:
        name = "super_admin_segment_overrides"
        indexes = [
            IndexModel(
                [("super_admin_id", ASCENDING), ("segment_name", ASCENDING)],
                unique=True,
            ),
        ]


class BrokerSegmentOverride(TimestampMixin, NettingFieldsBase):
    """Per-broker "pool default" segment override.

    Layered above NettingScriptOverride and below UserSegmentOverride for
    users in a broker's pool (immediate broker = `broker_ancestry[-1]`).
    Sub-broker chains do NOT cascade — each broker's settings are
    independent of their parent broker. Null fields inherit from the
    NettingSegment seed (same as sub-admin override semantics).
    """

    broker_id: PydanticObjectId
    segment_name: str

    class Settings:
        name = "broker_segment_overrides"
        indexes = [
            IndexModel(
                [("broker_id", ASCENDING), ("segment_name", ASCENDING)],
                unique=True,
            ),
        ]
