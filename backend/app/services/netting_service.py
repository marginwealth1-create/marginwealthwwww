"""Netting Segment + Risk Management service.

Three resolvers:
    NettingSegment   → NettingScriptOverride → UserSegmentOverride
    RiskSettings     → UserRiskSettings
"""

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId

from pymongo.errors import DuplicateKeyError as MongoDuplicateKeyError

from app.core.exceptions import NotFoundError, ValidationFailedError
from app.core.redis_client import cache_delete_pattern, cache_get, cache_set
from app.models.netting import (
    BrokerRiskSettings,
    BrokerSegmentOverride,
    NettingFieldsBase,
    NettingFieldsRequired,
    NettingScriptOverride,
    NettingSegment,
    RiskSettings,
    RiskSettingsBase,
    RiskSettingsRequired,
    SEGMENT_CODES,
    SubAdminRiskSettings,
    SubAdminSegmentOverride,
    SuperAdminRiskSettings,
    SuperAdminSegmentOverride,
    UserRiskSettings,
    UserSegmentOverride,
)
from app.models.user import User

logger = logging.getLogger(__name__)

CACHE_TTL = 300
# Risk gets a MUCH shorter TTL than the rest of netting cache. The
# stop-out enforcer reads `get_effective_risk` every tick (1 s) and a
# stale 5-minute snapshot meant an admin's "Stop-out 20 %" save took
# up to 5 minutes to reach a user already holding an open position —
# during which the enforcer kept reading the old `stop_pct = 0` and
# silently never fired. With 5 s the change propagates by the time
# the user's next price update lands, while still absorbing ~5 ticks
# of DB load per cycle.
RISK_CACHE_TTL = 300

NETTING_FIELDS = list(NettingFieldsRequired.model_fields.keys())
RISK_FIELDS = list(RiskSettingsRequired.model_fields.keys())


# Cached super-admin id — looked up once per process. The resolver hits
# this for every "user in super-admin's pool" lookup, so a Mongo round-
# trip per call would be expensive. The id never changes for a given
# super-admin user, and the SUPER_ADMIN role is platform-singleton in
# practice, so caching is safe. Reset on process restart.
_SUPER_ADMIN_ID_CACHE: PydanticObjectId | None = None


async def _resolve_super_admin_id() -> PydanticObjectId | None:
    """Returns the super-admin's user id. Cached after first hit."""
    global _SUPER_ADMIN_ID_CACHE
    if _SUPER_ADMIN_ID_CACHE is not None:
        return _SUPER_ADMIN_ID_CACHE
    from app.models.user import UserRole

    coll = User.get_motor_collection()
    doc = await coll.find_one(
        {"role": UserRole.SUPER_ADMIN.value}, {"_id": 1}
    )
    if doc is None:
        return None
    _SUPER_ADMIN_ID_CACHE = doc["_id"]
    return _SUPER_ADMIN_ID_CACHE

# Admin matrix rows whose instruments don't settle daily — no separate
# overnight margin exists. The resolver always reads the *Intraday* column
# for these rows and the admin UI greys out the overnight cells.
INTRADAY_ONLY_ADMIN_ROWS = frozenset({"FOREX", "STOCKS", "INDICES", "COMMODITIES", "CRYPTO"})

# Module-local debounce for "netting_eff:*" wipes. The admin Segment Matrix
# fires N parallel PUTs (one per dirty segment); without this each call
# would do its own SCAN-based Redis pattern delete, paying O(N×keys) when
# one wipe is enough. We dedupe by remembering the last wipe timestamp and
# skipping subsequent wipes within `_WIPE_DEDUP_SEC`.
_WIPE_DEDUP_SEC = 1.5
_last_eff_wipe: float = 0.0


async def _wipe_eff_cache_debounced() -> None:
    """Cheap O(1) check before the O(N) SCAN — drops redundant wipes that
    arrive within ~1.5 s of each other (typical for a multi-segment save).

    Also wipes the spread cache (`spread:*`) — admin spread changes need
    to surface on the live quote pump within one tick, and a single SCAN
    pass against two prefixes is cheap. Keeping the wipes co-located here
    means every admin save flow (segment update, script upsert, user
    override) takes care of both caches automatically.
    """
    global _last_eff_wipe
    import time

    now = time.time()
    if now - _last_eff_wipe < _WIPE_DEDUP_SEC:
        return
    _last_eff_wipe = now
    try:
        await cache_delete_pattern("netting_eff:*")
        await cache_delete_pattern(f"{_SPREAD_KEY_PREFIX}*")
        # Strike-far cache (option chain filter) lives under its own key
        # prefix; same wipe semantics so an admin edit shows up on the
        # next chain poll instead of waiting 30 s for the cache to expire.
        await cache_delete_pattern("strike_far:*")
        # Per-user blocked-symbols cache — admin toggling a script's
        # isActive must hide that symbol from every user's search /
        # option-chain / watchlist on the next request, not 30 s
        # later.
        await cache_delete_pattern("blocked_syms:*")
        # Drop the inactive-rows cache too — admin toggling `isActive` on
        # a row must hide/unhide that segment on the user side immediately
        # (this is one key, not a pattern, but `cache_delete_pattern`
        # handles single-key wipes fine).
        await cache_delete_pattern("inactive_admin_rows")
        await cache_delete_pattern("inactive_admin_rows:*")
        # Per-user effective-risk cache. Without this, a "Stop-out 80%"
        # save on the admin Risk page takes up to RISK_CACHE_TTL seconds
        # to reach open-position users. Wiping here ensures the change
        # propagates on the very next enforcer tick (~500 ms).
        await cache_delete_pattern("risk:*")
    except Exception:
        logger.warning("netting_cache_invalidation_failed_redis_down")


# ── Default segment seed metadata (matches bharat reference) ─────────
SEGMENT_DEFAULTS: list[dict[str, Any]] = [
    {"name": "NSE_EQ", "displayName": "NSE EQ", "lotApplies": False, "qtyApplies": True, "optionApplies": False, "expiryHoldApplies": False, "futureApplies": False},
    # NSE F&O split stock-vs-index (was NSE_FUT / NSE_OPT). Same cell-gating
    # flags as the old rows — only the granularity changed.
    {"name": "NSE_STK_FUT", "displayName": "Stock Future", "lotApplies": True, "qtyApplies": False, "optionApplies": False, "expiryHoldApplies": True, "futureApplies": True},
    {"name": "NSE_IDX_FUT", "displayName": "Index Future", "lotApplies": True, "qtyApplies": False, "optionApplies": False, "expiryHoldApplies": True, "futureApplies": True},
    {"name": "NSE_STK_OPT", "displayName": "Stock Option", "lotApplies": True, "qtyApplies": False, "optionApplies": True, "expiryHoldApplies": True, "futureApplies": False},
    {"name": "NSE_IDX_OPT", "displayName": "Index Option", "lotApplies": True, "qtyApplies": False, "optionApplies": True, "expiryHoldApplies": True, "futureApplies": False},
    {"name": "BSE_EQ", "displayName": "BSE EQ", "lotApplies": False, "qtyApplies": True, "optionApplies": False, "expiryHoldApplies": False, "futureApplies": False},
    {"name": "BSE_FUT", "displayName": "BSE FUT", "lotApplies": True, "qtyApplies": False, "optionApplies": False, "expiryHoldApplies": True, "futureApplies": True},
    {"name": "BSE_OPT", "displayName": "BSE OPT", "lotApplies": True, "qtyApplies": False, "optionApplies": True, "expiryHoldApplies": True, "futureApplies": False},
    {"name": "MCX_FUT", "displayName": "MCX FUT", "lotApplies": True, "qtyApplies": False, "optionApplies": False, "expiryHoldApplies": True, "futureApplies": True},
    {"name": "MCX_OPT", "displayName": "MCX OPT", "lotApplies": True, "qtyApplies": False, "optionApplies": True, "expiryHoldApplies": True, "futureApplies": False},
    {"name": "FOREX", "displayName": "Forex", "lotApplies": True, "qtyApplies": False, "optionApplies": False, "expiryHoldApplies": False, "futureApplies": False},
    {"name": "STOCKS", "displayName": "Stocks", "lotApplies": True, "qtyApplies": False, "optionApplies": False, "expiryHoldApplies": False, "futureApplies": False},
    {"name": "INDICES", "displayName": "Indices", "lotApplies": True, "qtyApplies": False, "optionApplies": False, "expiryHoldApplies": False, "futureApplies": False},
    {"name": "COMMODITIES", "displayName": "Commodities", "lotApplies": True, "qtyApplies": False, "optionApplies": False, "expiryHoldApplies": False, "futureApplies": False},
    # Single crypto row — covers spot, perpetual, dated futures and options.
    # The user side only shows one "Crypto" asset-class chip, so the admin
    # matrix mirrors that with one row rather than four sub-segments.
    {"name": "CRYPTO", "displayName": "Crypto", "lotApplies": True, "qtyApplies": False, "optionApplies": False, "expiryHoldApplies": False, "futureApplies": False},
]

# Segment names that were ever retired and need an idempotent cleanup on
# startup. The two-row split (CRYPTO_PERPETUAL + CRYPTO_OPTIONS) was
# collapsed into a single CRYPTO row, so the old names are retired here
# to drop them along with any dangling script / per-user overrides on
# the next boot.
RETIRED_SEGMENT_NAMES: tuple[str, ...] = ("CRYPTO_PERPETUAL", "CRYPTO_OPTIONS")


# ── Seeding ─────────────────────────────────────────────────────────
async def seed_default_segments() -> int:
    inserted = 0
    for spec in SEGMENT_DEFAULTS:
        existing = await NettingSegment.find_one(NettingSegment.name == spec["name"])
        if existing is not None:
            continue
        defaults = NettingFieldsRequired().model_dump()
        # Equity segments: percent margin and per-crore brokerage; tweak a couple
        if spec["name"].endswith("_EQ"):
            defaults["commissionType"] = "per_crore"
            defaults["commission"] = 300.0
        if spec["name"] == "FOREX":
            defaults["spreadType"] = "floating"
            defaults["minLots"] = 0.01
            defaults["orderLots"] = 0.01
        if spec["name"].startswith("CRYPTO"):
            defaults["minLots"] = 0.001
            defaults["orderLots"] = 0.001
        await NettingSegment(**spec, **defaults).insert()
        inserted += 1
    return inserted


async def heal_legacy_percent_seeds() -> int:
    """Idempotent boot heal — resets legacy seed values to NULL so the
    resolver's inheritance / inference paths take over.

    Three things get reset:

    1. ``marginCalcMode == "percent"`` (the old seed default which is no
       longer a valid admin dropdown option). Setting to NULL lets the
       resolver's defensive inference pick Times (if intradayMargin > 100)
       or Fixed (≤ 100) automatically.

    2. ``optionBuyIntraday == 100.0`` / ``optionSellIntraday == 15.0``
       (the old per-side seed defaults). Setting to NULL signals "inherit
       from segment-wide intradayMargin / overnightMargin", which is what
       admins almost always want — typing ``intradayMargin = 300`` on
       NSE_OPT expecting all options to use 300, only to discover the
       option columns silently overrode it.

    3. ``expiryDayIntradayMargin == 100.0`` / ``expiryDayOptionBuyMargin
       == 100.0`` / ``expiryDayOptionSellMargin == 50.0`` (the seed
       defaults from ``NettingFieldsRequired``). Setting to NULL signals
       "inherit from regular intraday / option-side margin", so a Times
       500× segment doesn't silently switch to 100× on every contract's
       expiry day. Admins who DO want a stricter expiry-day tier set
       the value explicitly and that wins over the inherit fallback.

    Safe to run on every boot — no-op once rows are cleaned. Returns the
    count of healed rows.
    """
    rows = await NettingSegment.find_all().to_list()
    SEED_INTRA = 100.0
    SEED_OPT_BUY = 100.0
    SEED_OPT_SELL = 15.0
    SEED_EXPIRY_INTRA = 100.0
    SEED_EXPIRY_OPT_BUY = 100.0
    SEED_EXPIRY_OPT_SELL = 50.0
    healed = 0
    for seg in rows:
        changed = False
        # Reset legacy "percent" mode when row is still at seed defaults.
        if (
            getattr(seg, "marginCalcMode", None) == "percent"
            and float(getattr(seg, "intradayMargin", 0) or 0) == SEED_INTRA
            and float(getattr(seg, "overnightMargin", 0) or 0) == SEED_INTRA
        ):
            seg.marginCalcMode = None
            changed = True
        # Reset per-side option columns when they're at seed defaults —
        # these almost always represent "inherit from segment", not an
        # explicit override.
        if float(getattr(seg, "optionBuyIntraday", 0) or 0) == SEED_OPT_BUY:
            seg.optionBuyIntraday = None
            changed = True
        if float(getattr(seg, "optionBuyOvernight", 0) or 0) == SEED_OPT_BUY:
            seg.optionBuyOvernight = None
            changed = True
        if float(getattr(seg, "optionSellIntraday", 0) or 0) == SEED_OPT_SELL:
            seg.optionSellIntraday = None
            changed = True
        if float(getattr(seg, "optionSellOvernight", 0) or 0) == SEED_OPT_SELL:
            seg.optionSellOvernight = None
            changed = True
        # Reset expiry-day overrides when they're at seed defaults — same
        # "inherit from segment" semantics as the option-side reset. Was
        # the root cause of Times-mode segments (e.g. MCX FUT 500×)
        # silently dropping to 100× on every contract's expiry day, which
        # priced a 1-lot CRUDEOIL margin at ₹10,247 instead of ₹2,049.
        if float(getattr(seg, "expiryDayIntradayMargin", 0) or 0) == SEED_EXPIRY_INTRA:
            seg.expiryDayIntradayMargin = None
            changed = True
        if float(getattr(seg, "expiryDayOptionBuyMargin", 0) or 0) == SEED_EXPIRY_OPT_BUY:
            seg.expiryDayOptionBuyMargin = None
            changed = True
        if float(getattr(seg, "expiryDayOptionSellMargin", 0) or 0) == SEED_EXPIRY_OPT_SELL:
            seg.expiryDayOptionSellMargin = None
            changed = True
        if changed:
            try:
                await seg.save()
                healed += 1
            except Exception:
                logger.exception("heal_seed_save_failed", extra={"segment_name": seg.name})
    if healed:
        logger.info("healed_legacy_seed_rows", extra={"count": healed})
        try:
            await _wipe_eff_cache_debounced()
        except Exception:
            pass
    return healed


async def cleanup_retired_segments() -> int:
    """Idempotent — drop NettingSegment rows whose names are in
    RETIRED_SEGMENT_NAMES, along with any script overrides and per-user
    overrides that still reference them. Safe to run on every startup;
    no-op once the rows are gone."""
    removed = 0
    for name in RETIRED_SEGMENT_NAMES:
        # Drop the segment row itself.
        seg = await NettingSegment.find_one(NettingSegment.name == name)
        if seg is not None:
            await seg.delete()
            removed += 1
        # Drop dangling script overrides for this segment.
        scripts = await NettingScriptOverride.find(
            NettingScriptOverride.segment_name == name
        ).to_list()
        for s in scripts:
            await s.delete()
            removed += 1
        # Drop dangling per-user overrides for this segment.
        user_ovs = await UserSegmentOverride.find(
            UserSegmentOverride.segment_name == name
        ).to_list()
        for u in user_ovs:
            await u.delete()
            removed += 1
    if removed:
        logger.info("netting_retired_segments_cleaned", extra={"count": removed})
    return removed


async def seed_default_risk() -> bool:
    existing = await RiskSettings.find_one(RiskSettings.type == "global")
    if existing is not None:
        return False
    await RiskSettings(**RiskSettingsRequired().model_dump()).insert()
    return True


# ── Risk Management ────────────────────────────────────────────────
async def get_global_risk() -> RiskSettings:
    doc = await RiskSettings.find_one(RiskSettings.type == "global")
    if doc is None:
        await seed_default_risk()
        doc = await RiskSettings.find_one(RiskSettings.type == "global")
    return doc  # type: ignore[return-value]


def _coerce_risk_value(field: str, v: Any) -> Any:
    """Coerce frontend payloads to the model's declared type. Number inputs
    on the form arrive as floats (because of step=0.01); the int-typed hold
    timers must be rounded to int or Pydantic 2.13 strict-mode will reject
    them on the next read and crash the GET endpoint."""
    if v is None:
        return None
    if field in ("profitTradeHoldMinSeconds", "lossTradeHoldMinSeconds", "staleFeedBlockSec"):
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return v
    if field in ("ledgerBalanceClose", "marginCallLevel", "stopOutLevel"):
        try:
            return float(v)
        except (TypeError, ValueError):
            return v
    if field in ("blockLimitAboveBelowHighLow", "blockLimitBetweenHighLow", "exitOnlyMode"):
        if isinstance(v, str):
            return v.lower() in ("true", "1", "yes", "on")
        return bool(v)
    return v


async def update_global_risk(patch: dict[str, Any]) -> RiskSettings:
    doc = await get_global_risk()
    for k, v in patch.items():
        if k in RISK_FIELDS and v is not None:
            setattr(doc, k, _coerce_risk_value(k, v))
    await doc.save()
    await cache_delete_pattern("risk:*")
    return doc


async def get_user_risk(user_id: str | PydanticObjectId) -> UserRiskSettings | None:
    return await UserRiskSettings.find_one(
        UserRiskSettings.user_id == PydanticObjectId(user_id)
    )


async def upsert_user_risk(user_id: str | PydanticObjectId, patch: dict[str, Any]) -> UserRiskSettings:
    uid = PydanticObjectId(user_id)
    existing = await UserRiskSettings.find_one(UserRiskSettings.user_id == uid)
    if existing is None:
        existing = UserRiskSettings(user_id=uid)
    for k, v in patch.items():
        if k in RISK_FIELDS:
            setattr(existing, k, _coerce_risk_value(k, v) if v is not None else None)
    await existing.save()
    await cache_delete_pattern(f"risk:{uid}")
    return existing


async def copy_user_risk(source_user_id: str | PydanticObjectId, dest_user_id: str | PydanticObjectId) -> UserRiskSettings:
    """Clone one user's override doc onto another. If the source has no
    override, the destination ends up inheriting global (override deleted)."""
    src_uid = PydanticObjectId(source_user_id)
    dst_uid = PydanticObjectId(dest_user_id)
    if src_uid == dst_uid:
        raise ValueError("Source and destination users are the same")
    src = await UserRiskSettings.find_one(UserRiskSettings.user_id == src_uid)
    if src is None:
        # Nothing to copy → drop any existing override on dest so it inherits.
        await delete_user_risk(dst_uid)
        return UserRiskSettings(user_id=dst_uid)
    patch = {f: getattr(src, f, None) for f in RISK_FIELDS}
    return await upsert_user_risk(dst_uid, patch)


async def delete_user_risk(user_id: str | PydanticObjectId) -> None:
    uid = PydanticObjectId(user_id)
    await UserRiskSettings.find(UserRiskSettings.user_id == uid).delete()
    await cache_delete_pattern(f"risk:{uid}")


# ── Sub-admin risk default ────────────────────────────────────────────
async def get_sub_admin_risk(
    sub_admin_id: str | PydanticObjectId,
) -> SubAdminRiskSettings | None:
    return await SubAdminRiskSettings.find_one(
        SubAdminRiskSettings.sub_admin_id == PydanticObjectId(sub_admin_id)
    )


async def upsert_sub_admin_risk(
    sub_admin_id: str | PydanticObjectId, patch: dict[str, Any]
) -> SubAdminRiskSettings:
    sid = PydanticObjectId(sub_admin_id)
    existing = await SubAdminRiskSettings.find_one(
        SubAdminRiskSettings.sub_admin_id == sid
    )
    if existing is None:
        existing = SubAdminRiskSettings(sub_admin_id=sid)
    for k, v in patch.items():
        if k in RISK_FIELDS:
            setattr(
                existing, k, _coerce_risk_value(k, v) if v is not None else None
            )
    await existing.save()
    # Invalidate effective-risk cache for every user assigned to this sub-admin.
    await _invalidate_pool_risk_cache(sid)
    # Safety net: also wipe the whole risk:* namespace. The per-pool
    # walk above only catches users whose `assigned_admin_id` exactly
    # matches — users whose pool was just changed, or any indirection
    # bugs, would otherwise carry a stale snapshot for up to
    # RISK_CACHE_TTL seconds. The wildcard sweep is cheap (the cache
    # is small and rebuilds per-user on the next enforcer tick).
    await cache_delete_pattern("risk:*")
    return existing


async def delete_sub_admin_risk(sub_admin_id: str | PydanticObjectId) -> None:
    sid = PydanticObjectId(sub_admin_id)
    await SubAdminRiskSettings.find(
        SubAdminRiskSettings.sub_admin_id == sid
    ).delete()
    await _invalidate_pool_risk_cache(sid)


async def _invalidate_pool_risk_cache(sub_admin_id: PydanticObjectId) -> None:
    """Wipes the per-user `risk:<uid>` cache for every user assigned to
    this sub-admin. Cheaper than wiping the whole `risk:*` pattern when
    only one pool changed."""
    coll = User.get_motor_collection()
    cursor = coll.find({"assigned_admin_id": sub_admin_id}, {"_id": 1})
    async for doc in cursor:
        await cache_delete_pattern(f"risk:{doc['_id']}")


# ── Super-admin risk default (super-admin's own pool) ────────────────
async def get_super_admin_risk(
    super_admin_id: str | PydanticObjectId,
) -> SuperAdminRiskSettings | None:
    return await SuperAdminRiskSettings.find_one(
        SuperAdminRiskSettings.super_admin_id == PydanticObjectId(super_admin_id)
    )


async def upsert_super_admin_risk(
    super_admin_id: str | PydanticObjectId, patch: dict[str, Any]
) -> SuperAdminRiskSettings:
    sid = PydanticObjectId(super_admin_id)
    existing = await SuperAdminRiskSettings.find_one(
        SuperAdminRiskSettings.super_admin_id == sid
    )
    if existing is None:
        existing = SuperAdminRiskSettings(super_admin_id=sid)
    for k, v in patch.items():
        if k in RISK_FIELDS:
            setattr(
                existing, k, _coerce_risk_value(k, v) if v is not None else None
            )
    await existing.save()
    await _invalidate_super_admin_pool_risk_cache()
    await cache_delete_pattern("risk:*")
    return existing


async def delete_super_admin_risk(super_admin_id: str | PydanticObjectId) -> None:
    sid = PydanticObjectId(super_admin_id)
    await SuperAdminRiskSettings.find(
        SuperAdminRiskSettings.super_admin_id == sid
    ).delete()
    await _invalidate_super_admin_pool_risk_cache()


async def _invalidate_super_admin_pool_risk_cache() -> None:
    coll = User.get_motor_collection()
    cursor = coll.find(
        {"assigned_admin_id": None, "assigned_broker_id": None},
        {"_id": 1},
    )
    async for doc in cursor:
        await cache_delete_pattern(f"risk:{doc['_id']}")


# ── Broker risk default (broker's own pool) ──────────────────────────
async def get_broker_risk(
    broker_id: str | PydanticObjectId,
) -> BrokerRiskSettings | None:
    return await BrokerRiskSettings.find_one(
        BrokerRiskSettings.broker_id == PydanticObjectId(broker_id)
    )


async def upsert_broker_risk(
    broker_id: str | PydanticObjectId, patch: dict[str, Any]
) -> BrokerRiskSettings:
    bid = PydanticObjectId(broker_id)
    existing = await BrokerRiskSettings.find_one(
        BrokerRiskSettings.broker_id == bid
    )
    if existing is None:
        existing = BrokerRiskSettings(broker_id=bid)
    for k, v in patch.items():
        if k in RISK_FIELDS:
            setattr(
                existing, k, _coerce_risk_value(k, v) if v is not None else None
            )
    await existing.save()
    await _invalidate_broker_pool_risk_cache(bid)
    await cache_delete_pattern("risk:*")
    return existing


async def delete_broker_risk(broker_id: str | PydanticObjectId) -> None:
    bid = PydanticObjectId(broker_id)
    await BrokerRiskSettings.find(BrokerRiskSettings.broker_id == bid).delete()
    await _invalidate_broker_pool_risk_cache(bid)


async def _invalidate_broker_pool_risk_cache(broker_id: PydanticObjectId) -> None:
    coll = User.get_motor_collection()
    cursor = coll.find({"assigned_broker_id": broker_id}, {"_id": 1})
    async for doc in cursor:
        await cache_delete_pattern(f"risk:{doc['_id']}")


async def get_effective_risk(user_id: str | PydanticObjectId) -> dict[str, Any]:
    uid = str(user_id)
    cache_key = f"risk:{uid}"
    cached = await cache_get(cache_key)
    if cached is not None:
        return cached

    # Layer 1: platform global (super-admin)
    g = await get_global_risk()
    merged: dict[str, Any] = {f: getattr(g, f) for f in RISK_FIELDS}
    sources = {f: "GLOBAL" for f in RISK_FIELDS}

    # Layer 2: pool CASCADE — collect the override from EVERY tier in the
    # user's ownership chain and merge PER-FIELD with priority:
    #     broker (most specific) > sub-admin > super-admin > global.
    # Previously this picked EXACTLY ONE tier, so a broker-nested user skipped
    # the admin's risk entirely (broker → straight to global). Now a field the
    # broker/sub-broker didn't set falls through to the admin's value, then the
    # super-admin's, then global — the SAME cascade the segment settings use,
    # so risk + segments resolve consistently. Operator ask: "if a broker /
    # sub-broker sets their own risk it overrides the admin, otherwise the
    # admin's setting still applies to that broker's users."
    user_doc = await User.get(PydanticObjectId(uid))
    if user_doc is not None:
        broker_risk = None
        admin_risk = None
        super_admin_risk = None
        broker_anc = user_doc.broker_ancestry or []
        if broker_anc:
            # Immediate broker (== sub-broker for a sub-broker's client).
            broker_risk = await get_broker_risk(broker_anc[-1])
        if user_doc.assigned_admin_id is not None:
            admin_risk = await get_sub_admin_risk(user_doc.assigned_admin_id)
        sa_id = await _resolve_super_admin_id()
        if sa_id is not None:
            super_admin_risk = await get_super_admin_risk(sa_id)
        # First non-None per field wins, in priority order. A field is only
        # filled while it's still at its GLOBAL default (sources[f] == "GLOBAL"),
        # so a higher-priority tier that already set it is never overwritten.
        for source, pool in (
            ("BROKER", broker_risk),
            ("SUB_ADMIN", admin_risk),
            ("SUPER_ADMIN", super_admin_risk),
        ):
            if pool is None:
                continue
            for f in RISK_FIELDS:
                if sources[f] != "GLOBAL":
                    continue  # already set by a more-specific tier
                v = getattr(pool, f, None)
                if v is not None:
                    merged[f] = v
                    sources[f] = source

    # Layer 3: per-user override (highest priority)
    u = await get_user_risk(uid)
    if u is not None:
        for f in RISK_FIELDS:
            v = getattr(u, f, None)
            if v is not None:
                merged[f] = v
                sources[f] = "USER"

    payload = {"settings": merged, "sources": sources}
    # Spread each user's cache expiry across a RISK_CACHE_TTL-second window
    # using a deterministic per-uid offset. Without this, all 92 open-position
    # users expire simultaneously (30 s after restart), triggering 92×4
    # concurrent MongoDB queries that spike sweep_ms to 3-8 s once per period.
    import hashlib as _hl
    _stagger = int(_hl.md5(uid.encode()).hexdigest()[:4], 16) % RISK_CACHE_TTL
    await cache_set(cache_key, payload, ttl_sec=RISK_CACHE_TTL + _stagger)
    return payload


# ── Netting Segments ───────────────────────────────────────────────
async def list_segments() -> list[NettingSegment]:
    rows = await NettingSegment.find_all().to_list()
    if not rows:
        await seed_default_segments()
        rows = await NettingSegment.find_all().to_list()
    # Stable order matching SEGMENT_CODES
    order = {n: i for i, n in enumerate(SEGMENT_CODES)}
    rows.sort(key=lambda r: order.get(r.name, 99))
    return rows


def _split_pattern(script_symbol: str) -> tuple[str, str] | None:
    """If `script_symbol` is a derivative shorthand (e.g. `NIFTYFUT`,
    `BANKNIFTYCE`, `SBINPE`), return `(base, suffix)` where suffix is one
    of `FUT` / `CE` / `PE` and base is everything before it. Otherwise
    return None — caller should treat the script as an exact-match row.

    A symbol counts as a pattern when it ends in one of the three
    derivative suffixes AND contains no digit characters. Real exchange
    symbols always carry an expiry / strike digit chunk (NIFTY26JANFUT,
    NIFTY26JAN22500CE), so the digit-free rule cleanly separates the two.
    """
    s = (script_symbol or "").upper()
    if not s or any(c.isdigit() for c in s):
        return None
    for suf in ("FUT", "CE", "PE"):
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)], suf
    return None


def _instrument_matches_pattern(instrument_sym: str, base: str, suffix: str) -> bool:
    """True when an instrument's symbol matches a `<base><suffix>` pattern.

    Match rule: starts with `base` AND ends with `suffix`. So `NIFTYFUT`
    matches `NIFTY26JANFUT`, `NIFTY26FEBFUT`, `NIFTYNXT50FUT` (anything that
    starts with `NIFTY` and ends with `FUT`). The user wanted the broadest
    possible cover across expiries, so deliberately no expiry-digit gate
    on the middle section.
    """
    s = (instrument_sym or "").upper()
    return s.startswith(base) and s.endswith(suffix)


def _best_pattern_row(rows: list, instrument_sym: str):
    """Pick the most specific shorthand row that covers this contract.

    Longest matching base wins. `GOLDFUT` and `GOLDMFUT` BOTH match
    GOLDM26DECFUT under the start/end rule — and they carry very different
    margins (₹1L vs ₹3k per lot in production), so a first-wins scan hands
    the mini contract the big contract's margin depending on document
    order. Same for SILVER / SILVERM, ZINC / ZINCMINI, CRUDEOIL /
    CRUDEOILM.
    """
    best = None
    best_len = -1
    for row in rows:
        split = _split_pattern(getattr(row, "symbol", "") or "")
        if split is None:
            continue
        base, suffix = split
        if _instrument_matches_pattern(instrument_sym, base, suffix) and len(base) > best_len:
            best, best_len = row, len(base)
    return best


async def _match_pattern_script(
    seg_name: str,
    instrument_sym: str,
    *,
    scope_admin_id: PydanticObjectId | None = None,
    scope_broker_id: PydanticObjectId | None = None,
) -> "NettingScriptOverride | None":
    """Scan every script override in the segment for one whose symbol is
    a pattern that matches `instrument_sym`. Falls through to None when
    no pattern matches — caller keeps the segment defaults.

    Scope-aware: only considers rows where (scope_admin_id, scope_broker_id)
    match the args. Caller cascades through tiers (broker → admin →
    platform) by calling this with the appropriate scope each pass.
    Default args (both None) preserve the historical platform-only
    behaviour so older callers don't change semantics.

    O(N) over the segment's script count in that tier; in practice N is
    small (a few dozen) so this is fine even on the resolver hot path.
    The 5-min `netting_eff:*` cache absorbs the lookup cost for repeat
    orders on the same instrument.
    """
    rows = await NettingScriptOverride.find(
        NettingScriptOverride.segment_name == seg_name,
        NettingScriptOverride.scope_admin_id == scope_admin_id,
        NettingScriptOverride.scope_broker_id == scope_broker_id,
    ).to_list()
    return _best_pattern_row(rows, instrument_sym)


async def _match_pattern_user_override(
    user_id: PydanticObjectId, seg_name: str, instrument_sym: str
) -> "UserSegmentOverride | None":
    """The per-user twin of `_match_pattern_script`.

    Admins type the same derivative shorthand on the Script — User-wise tab
    that they type on Script — Global: `CRUDEOILFUT`, `GOLDFUT`,
    `SILVERMFUT`. The per-user lookup was exact-match only, so none of
    those ever matched a real contract (`CRUDEOIL26OCTFUT`) and the
    override silently did nothing — production had 19 such rows, every one
    of them dead.

    Exact rows are matched by the caller first; this only runs when that
    misses, so an exact symbol still wins over a pattern.
    """
    rows = await UserSegmentOverride.find(
        UserSegmentOverride.user_id == user_id,
        UserSegmentOverride.segment_name == seg_name,
    ).to_list()
    return _best_pattern_row(rows, instrument_sym)


async def get_segment(segment_id: str | PydanticObjectId) -> NettingSegment:
    doc = await NettingSegment.get(PydanticObjectId(segment_id))
    if doc is None:
        raise NotFoundError("Segment not found")
    return doc


# ── Spread resolver (no user context) ────────────────────────────────
# Called from the quote pump on every tick — must be cheap. Walks just the
# segment-default + per-script-override layers (UserSegmentOverride is
# skipped: spreads are admin-set markups, not per-user adjustments, and
# routing every tick through a user lookup would torpedo latency).
#
# Cache: 30 s in Redis keyed on `spread:{seg_name}:{symbol|_}`. The admin
# save flow wipes this key set alongside `netting_eff:*` so an admin edit
# propagates to the live feed within one tick after save.
SPREAD_CACHE_TTL = 30
_SPREAD_KEY_PREFIX = "spread:"


async def _wipe_spread_cache_debounced() -> None:
    try:
        await cache_delete_pattern(f"{_SPREAD_KEY_PREFIX}*")
    except Exception:
        logger.warning("spread_cache_invalidation_failed_redis_down")


async def inactive_admin_rows(
    user_id: str | PydanticObjectId | None = None,
) -> set[str]:
    """Names of admin matrix rows currently flagged `isActive = false`.

    Tier resolution (highest wins — same chain as the rest of segment
    settings):
      1. Base NettingSegment.isActive  (platform seed default = True)
      2. SuperAdminSegmentOverride     (platform-wide)
      3. **Pool-tier override for THIS user** when `user_id` is given:
            • broker override if user has `assigned_broker_id`
            • sub-admin override if user has `assigned_admin_id`
         A pool-tier `isActive = True` UN-hides a segment blocked at a
         lower tier; `isActive = False` hides it.

    Without `user_id` (admin / global) → returns only base + super-admin.
    With `user_id` (user-side) → adds that user's sub-admin or broker
    pool overrides so a sub-admin's "Block → No" reaches their pool
    members without accidentally hiding the segment for OTHER pools.
    """
    cache_key = (
        f"inactive_admin_rows:{user_id}"
        if user_id is not None
        else "inactive_admin_rows"
    )
    try:
        cached = await cache_get(cache_key)
        if cached is not None and isinstance(cached, list):
            return set(cached)
    except Exception:
        pass

    # Layer 1: base seed
    base_rows = await NettingSegment.find({"isActive": False}).to_list()
    names = {r.name for r in base_rows if r.name}

    # Layer 2: super-admin override (platform-wide)
    try:
        sa_id = await _resolve_super_admin_id()
        if sa_id is not None:
            sa_overrides = await SuperAdminSegmentOverride.find(
                SuperAdminSegmentOverride.super_admin_id == sa_id,
            ).to_list()
            for row in sa_overrides:
                if not row.segment_name:
                    continue
                if row.isActive is False:
                    names.add(row.segment_name)
                elif row.isActive is True:
                    names.discard(row.segment_name)
    except Exception:
        logger.debug("inactive_admin_rows_super_admin_lookup_failed", exc_info=True)

    # Layer 3: user's pool-tier override (broker > sub-admin).
    # Same priority chain as get_effective_risk / get_effective_settings
    # so block semantics stay consistent across the platform. Only one
    # tier per user — never both.
    #
    # Layer 4: per-user override (UserSegmentOverride). Highest priority
    # — admin can pause a segment for ONE user without touching the
    # whole sub-admin pool. `isActive = False` on the user override hides
    # the segment for that user; `isActive = True` un-hides it even if a
    # higher tier blocked it.
    if user_id is not None:
        try:
            uid = (
                user_id
                if isinstance(user_id, PydanticObjectId)
                else PydanticObjectId(user_id)
            )
            user_doc = await User.get(uid)
            if user_doc is not None:
                pool_rows: list = []
                broker_anc = user_doc.broker_ancestry or []
                if broker_anc:
                    broker_id = broker_anc[-1]
                    pool_rows = await BrokerSegmentOverride.find(
                        BrokerSegmentOverride.broker_id == broker_id,
                    ).to_list()
                elif user_doc.assigned_admin_id is not None:
                    pool_rows = await SubAdminSegmentOverride.find(
                        SubAdminSegmentOverride.sub_admin_id
                        == user_doc.assigned_admin_id,
                    ).to_list()
                for row in pool_rows:
                    if not row.segment_name:
                        continue
                    if row.isActive is False:
                        names.add(row.segment_name)
                    elif row.isActive is True:
                        names.discard(row.segment_name)

            # Layer 4: per-user overrides
            user_overrides = await UserSegmentOverride.find(
                UserSegmentOverride.user_id == uid,
            ).to_list()
            for row in user_overrides:
                if not row.segment_name:
                    continue
                if row.isActive is False:
                    names.add(row.segment_name)
                elif row.isActive is True:
                    names.discard(row.segment_name)
        except Exception:
            logger.debug(
                "inactive_admin_rows_pool_lookup_failed", exc_info=True
            )

    try:
        await cache_set(cache_key, sorted(names), ttl_sec=30)
    except Exception:
        pass
    return names


async def inactive_instrument_segments(
    user_id: str | PydanticObjectId | None = None,
) -> set[str]:
    """Translate inactive admin-row names back into the SegmentType values
    instruments are tagged with — so the user search can filter by
    `instrument.segment NOT IN this_set`.

    Pass `user_id` to include the user's sub-admin / broker pool block
    overrides; omit for the global (super-admin-only) view.
    """
    admin_rows = await inactive_admin_rows(user_id=user_id)
    if not admin_rows:
        return set()
    out: set[str] = set()
    for seg_type, admin_row in _SEGMENT_NAME_MAP.items():
        if admin_row in admin_rows:
            out.add(seg_type)
    # Admin rows whose name matches the instrument segment 1:1 (FOREX,
    # STOCKS, INDICES, COMMODITIES, CRYPTO and similar) — `_SEGMENT_NAME_MAP`
    # also maps these to themselves, so they're already covered above.
    # Include the raw admin-row names too as a safety net.
    out |= admin_rows
    return out


async def resolve_strike_far(segment_name: str) -> float:
    """Return the `strikeFarPercent` for an OPT admin row (NSE_OPT /
    BSE_OPT / MCX_OPT). Cached in Redis for 30 s under
    `strike_far:{seg}`. Same wipe pattern as the spread cache — segment
    save flow drops both keys together.

    No per-symbol / per-user layer: the option chain dialog is shared,
    so the filter must be deterministic across viewers. If admin needs
    a different cap for a single index later, a per-script override
    layer can land alongside the spread one.
    """
    seg_name = (segment_name or "").strip()
    if not seg_name:
        return 0.0
    cache_key = f"strike_far:{seg_name}"
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return float(cached)
    except Exception:
        pass
    seg = await NettingSegment.find_one(NettingSegment.name == seg_name)
    pct = 0.0
    if seg is not None:
        pct = float(getattr(seg, "strikeFarPercent", 0.0) or 0.0)
    try:
        await cache_set(cache_key, pct, ttl_sec=30)
    except Exception:
        pass
    return pct


async def resolve_spread(segment_name: str, symbol: str | None) -> dict[str, Any]:
    """Return the admin-resolved `{spread_type, spread_pips}` for the
    segment + symbol pair. Cached in Redis for ~30 s.

    `segment_name` must be the ADMIN-row name (NSE_EQ / FOREX / CRYPTO …),
    NOT the instrument SegmentType. Callers translate via _SEGMENT_NAME_MAP
    before reaching here.

    Resolution order (highest wins):
      1. Script-level override (NettingScriptOverride)
      2. Super-admin's segment override (SuperAdminSegmentOverride)
      3. Base NettingSegment seed defaults

    Sub-admin / broker tier overrides are NOT consulted here because the
    spread is applied to the shared market-tick broadcast (one quote
    fans out to every connected user), not a per-user resolution. If
    multi-pool spreads are needed in the future the WS pump will have
    to publish per-pool channels.
    """
    seg_name = (segment_name or "").strip()
    sym_key = (symbol or "").strip().upper() or "_"
    cache_key = f"{_SPREAD_KEY_PREFIX}{seg_name}:{sym_key}"
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass

    seg = await NettingSegment.find_one(NettingSegment.name == seg_name)
    spread_type: str = "fixed"
    spread_pips: float = 0.0
    if seg is not None:
        spread_type = str(getattr(seg, "spreadType", "fixed") or "fixed")
        spread_pips = float(getattr(seg, "spreadPips", 0.0) or 0.0)

    # Super-admin's platform-wide override sits above the seed default. The
    # admin Segment Settings page writes to `SuperAdminSegmentOverride` when
    # the signed-in admin is SUPER_ADMIN — without consulting that table,
    # every spread save was invisible to the resolver and the live overlay
    # kept using the base 0.0 (the user-reported "spread save nahi ho raha"
    # bug, where the admin form showed the typed value but BID/ASK never
    # widened on the user feed).
    try:
        sa_id = await _resolve_super_admin_id()
        if sa_id is not None:
            sa_over = await SuperAdminSegmentOverride.find_one(
                SuperAdminSegmentOverride.super_admin_id == sa_id,
                SuperAdminSegmentOverride.segment_name == seg_name,
            )
            if sa_over is not None:
                sa_type = getattr(sa_over, "spreadType", None)
                sa_pips = getattr(sa_over, "spreadPips", None)
                if sa_type is not None:
                    spread_type = str(sa_type)
                if sa_pips is not None:
                    spread_pips = float(sa_pips)
    except Exception:
        # Super-admin lookup is best-effort — a missing tier override is
        # always safe to fall back to the base seg row.
        logger.debug("resolve_spread_super_admin_lookup_failed", exc_info=True)

    if symbol:
        # The market-tick spread overlay is applied to the SHARED broadcast
        # (one quote → every user). So ANY admin's script override — whether
        # scoped to the platform (super-admin), a broker, or a sub-admin —
        # should take effect here. We fetch ALL script rows for this segment
        # (no scope filter) and prefer the most-specific match:
        #   priority: exact match > pattern match
        #   within the same specificity, first match wins (arbitrary but
        #   stable; conflicts should be resolved by the admin).
        #
        # Previously this code called find_one(…symbol==sym_key) without a
        # scope filter (so all scopes were scanned) then fell back to
        # _match_pattern_script(scope=None,None) which ONLY scanned
        # platform-level rows — so a script saved by a non-super-admin
        # (scope_admin_id=admin.id) would match the exact-find but then the
        # pattern fallback silently skipped the admin-scoped rows, resulting
        # in no override being applied for pattern-based scripts.
        all_scripts = await NettingScriptOverride.find(
            NettingScriptOverride.segment_name == seg_name,
        ).to_list()

        script = None
        # 1. Exact match (any scope)
        for row in all_scripts:
            if (row.symbol or "").upper() == sym_key:
                script = row
                break
        # 2. Pattern match (any scope) — e.g. SILVERFUT matches SILVER26JULFUT
        if script is None:
            for row in all_scripts:
                split = _split_pattern(row.symbol)
                if split is None:
                    continue
                base, suffix = split
                if _instrument_matches_pattern(sym_key, base, suffix):
                    script = row
                    break

        if script is not None:
            override_type = getattr(script, "spreadType", None)
            override_pips = getattr(script, "spreadPips", None)
            if override_type is not None:
                spread_type = str(override_type)
            if override_pips is not None:
                spread_pips = float(override_pips)

    payload = {"spread_type": spread_type, "spread_pips": spread_pips}
    try:
        await cache_set(cache_key, payload, ttl_sec=SPREAD_CACHE_TTL)
    except Exception:
        pass
    return payload


async def update_segment(segment_id: str | PydanticObjectId, patch: dict[str, Any]) -> NettingSegment:
    doc = await get_segment(segment_id)
    for k, v in patch.items():
        if k in NETTING_FIELDS and v is not None:
            setattr(doc, k, v)
    await doc.save()
    # Clear per-user effective-settings caches so admin edits take effect
    # immediately on the user terminal. The resolver's cache key has the
    # form `netting_eff:{user_id}:{seg_name}:...` — the old
    # `netting:NAME:*` pattern never matched and made admin edits invisible.
    # `_wipe_eff_cache_debounced` collapses bursts so a 14-segment Save All
    # pays one O(N) SCAN, not fourteen.
    await _wipe_eff_cache_debounced()
    try:
        await cache_delete_pattern(f"netting:{doc.name}:*")
    except Exception:
        logger.warning("netting_cache_invalidation_failed_redis_down")
    return doc


# ── Script overrides ──────────────────────────────────────────────
async def list_scripts(
    segment: str | None = None,
    *,
    scope_admin_id: PydanticObjectId | None = None,
    scope_broker_id: PydanticObjectId | None = None,
    include_platform: bool = True,
) -> list[NettingScriptOverride]:
    """List script overrides, filtered by tier scope.

    Behaviour by caller role:
      • super-admin → scope_admin_id=None + scope_broker_id=None +
        include_platform=True → returns ALL overrides (platform + every
        admin / broker tier row), so super-admin can audit / clean up
        any row.
      • admin → scope_admin_id=admin.id + include_platform=True →
        returns the admin's own per-symbol overrides plus the
        platform-wide ones (so admin can SEE what's inherited without
        being allowed to edit those platform rows).
      • broker → same shape with scope_broker_id=broker.id.
    """
    base_filters: list[Any] = []
    if segment:
        base_filters.append(NettingScriptOverride.segment_name == segment)
    if scope_admin_id is None and scope_broker_id is None:
        if include_platform:
            return await NettingScriptOverride.find(*base_filters).to_list()
        return []
    # Tier caller — fetch their tier rows, optionally include platform.
    or_clauses: list[Any] = []
    if scope_broker_id is not None:
        or_clauses.append({"scope_broker_id": scope_broker_id})
    if scope_admin_id is not None:
        or_clauses.append({"scope_admin_id": scope_admin_id})
    if include_platform:
        or_clauses.append({"scope_admin_id": None, "scope_broker_id": None})
    if not or_clauses:
        return []
    coll = NettingScriptOverride.get_motor_collection()
    query: dict[str, Any] = {"$or": or_clauses}
    if segment:
        query["segment_name"] = segment
    rows = await coll.find(query).to_list(length=None)
    return [NettingScriptOverride.model_validate(r) for r in rows]


async def create_script(
    payload: dict[str, Any],
    *,
    scope_admin_id: PydanticObjectId | None = None,
    scope_broker_id: PydanticObjectId | None = None,
) -> NettingScriptOverride:
    seg_name = payload.get("segment_name")
    seg_id = payload.get("segment_id")
    if not seg_name or not seg_id:
        raise ValidationFailedError("segment_name and segment_id required")
    symbol = (payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValidationFailedError("symbol required")

    # ── Symbol existence validation ──────────────────────────────────
    # Block typo / fake symbols (e.g. "VIBHOOTI") from silently creating
    # dead override rows that never match any instrument. A symbol is valid
    # when it is EITHER a derivative-pattern shorthand (NIFTYFUT / BANKNIFTYCE
    # — admin shorthand the resolver expands across expiries/strikes) OR an
    # instrument that actually exists in this segment's catalog. The admin UI
    # only lets you Add a symbol picked from the typeahead, so a rejection
    # here means the caller bypassed it. `skip_symbol_validation` is an
    # un-surfaced escape hatch for the rare "pre-create an override for a
    # not-yet-listed instrument" case.
    if not payload.get("skip_symbol_validation"):
        if _split_pattern(symbol) is None:  # exact-match row → must exist
            from app.models.instrument import Instrument

            seg_values = instrument_segments_for(seg_name)
            iq: dict[str, Any] = {"symbol": symbol}
            if seg_values:
                iq["segment"] = {"$in": seg_values}
            if await Instrument.find_one(iq) is None:
                raise ValidationFailedError(
                    f"'{symbol}' is not a known instrument in {seg_name}. "
                    f"Pick a symbol from the suggestions."
                )

    # Upsert: if a row for (segment, symbol, scope) already exists — from
    # any previous admin session or a race — update it instead of failing.
    # This prevents the DuplicateKeyError "Network Error" that happened when
    # a script was created by one admin session and another tried to re-add
    # it (or when the old 2-field unique index blocked a scoped re-add).
    existing = await NettingScriptOverride.find_one(
        NettingScriptOverride.segment_name == seg_name,
        NettingScriptOverride.symbol == symbol,
        NettingScriptOverride.scope_admin_id == scope_admin_id,
        NettingScriptOverride.scope_broker_id == scope_broker_id,
    )
    if existing is not None:
        for k in NETTING_FIELDS:
            if k in payload and payload[k] is not None:
                setattr(existing, k, payload[k])
        await existing.save()
        await _wipe_eff_cache_debounced()
        return existing

    clean: dict[str, Any] = {
        "segment_id": PydanticObjectId(seg_id),
        "segment_name": seg_name,
        "symbol": symbol,
        "tradingSymbol": payload.get("tradingSymbol") or symbol,
        "instrumentToken": payload.get("instrumentToken"),
        "lotSize": payload.get("lotSize") or 1.0,
        "scope_admin_id": scope_admin_id,
        "scope_broker_id": scope_broker_id,
    }
    for k in NETTING_FIELDS:
        if k in payload and payload[k] is not None:
            clean[k] = payload[k]
    doc = NettingScriptOverride(**clean)
    try:
        await doc.insert()
    except MongoDuplicateKeyError:
        # Race condition or stale index — fetch the now-existing row and
        # apply the payload fields to it so the caller always gets a valid doc.
        existing = await NettingScriptOverride.find_one(
            NettingScriptOverride.segment_name == seg_name,
            NettingScriptOverride.symbol == symbol,
        )
        if existing is None:
            raise
        for k in NETTING_FIELDS:
            if k in payload and payload[k] is not None:
                setattr(existing, k, payload[k])
        await existing.save()
        await _wipe_eff_cache_debounced()
        return existing
    await _wipe_eff_cache_debounced()
    return doc


async def create_scripts_bulk(
    *,
    segment_id: str | PydanticObjectId | None,
    segment_name: str | None,
    symbols: list[str],
    scope_admin_id: PydanticObjectId | None = None,
    scope_broker_id: PydanticObjectId | None = None,
) -> dict[str, int]:
    """Create one per-symbol override row per symbol in a single action — the
    admin "Select all" on the Scripts tab. The symbols come from the
    instrument catalog / underlying list the UI already searched, so the
    per-symbol existence check is skipped (``skip_symbol_validation``).
    Upserts, so re-running is idempotent and never duplicates a row. Hard
    capped to guard against a runaway add on a huge segment (NSE_EQ etc) —
    for whole-segment values the Segments tab is the right tool."""
    if not segment_id or not segment_name:
        raise ValidationFailedError("segment_id and segment_name required")
    seen: set[str] = set()
    clean: list[str] = []
    for s in symbols or []:
        u = (s or "").strip().upper()
        if u and u not in seen:
            seen.add(u)
            clean.append(u)
    if len(clean) > 500:
        raise ValidationFailedError(
            "Too many symbols to bulk-add (>500). Use the Segments tab to set "
            "one value for the whole segment instead."
        )
    created = 0
    for sym in clean:
        try:
            await create_script(
                {
                    "segment_id": segment_id,
                    "segment_name": segment_name,
                    "symbol": sym,
                    "skip_symbol_validation": True,
                },
                scope_admin_id=scope_admin_id,
                scope_broker_id=scope_broker_id,
            )
            created += 1
        except Exception:  # noqa: BLE001
            logger.exception(
                "bulk_script_create_failed sym=%s seg=%s", sym, segment_name
            )
    return {"created": created, "total": len(clean)}


async def update_script(script_id: str | PydanticObjectId, patch: dict[str, Any]) -> NettingScriptOverride:
    doc = await NettingScriptOverride.get(PydanticObjectId(script_id))
    if doc is None:
        raise NotFoundError("Script override not found")
    for k, v in patch.items():
        if k in NETTING_FIELDS:
            setattr(doc, k, v)
    if "lotSize" in patch and patch["lotSize"] is not None:
        doc.lotSize = float(patch["lotSize"])
    await doc.save()
    await _wipe_eff_cache_debounced()
    return doc


async def delete_script(script_id: str | PydanticObjectId) -> None:
    doc = await NettingScriptOverride.get(PydanticObjectId(script_id))
    if doc is not None:
        await doc.delete()
        await _wipe_eff_cache_debounced()


async def get_script(
    script_id: str | PydanticObjectId,
) -> NettingScriptOverride | None:
    """Fetch a script override by id — exposed so the API layer can do
    a scope check before allowing PUT/DELETE."""
    return await NettingScriptOverride.get(PydanticObjectId(script_id))


# ── Sub-admin segment defaults ────────────────────────────────────────
async def list_sub_admin_segment_overrides(
    sub_admin_id: str | PydanticObjectId,
) -> list[SubAdminSegmentOverride]:
    return await SubAdminSegmentOverride.find(
        SubAdminSegmentOverride.sub_admin_id == PydanticObjectId(sub_admin_id)
    ).to_list()


async def get_sub_admin_segment_override(
    sub_admin_id: str | PydanticObjectId, segment_name: str
) -> SubAdminSegmentOverride | None:
    return await SubAdminSegmentOverride.find_one(
        SubAdminSegmentOverride.sub_admin_id == PydanticObjectId(sub_admin_id),
        SubAdminSegmentOverride.segment_name == segment_name,
    )


# ── Hide-blocked-from-user resolver ──────────────────────────────
#
# Returns a structured view of which symbols/patterns the user
# shouldn't see in search / option chain / watchlist results because
# an admin has flipped `isActive` or `tradingEnabled` to False on a
# script-level OR user-specific per-symbol override.
#
# We DON'T filter on segment-level isActive here — that's handled
# elsewhere via `cache_inactive_admin_rows()` so the side-panel
# Browse chips show nothing from a fully-shut segment. This helper
# focuses on per-symbol blocks specifically.
#
# Returned shape:
#   {
#     "symbols": {"SBIN", "RELIANCE"},           # exact-match block set
#     "patterns": [("NIFTY", "FUT"), ("NIFTY", "CE")],  # base + suffix
#   }
#
# Caller checks each candidate instrument:
#   symbol in symbols → block
#   OR any pattern (base, suffix) matches via _instrument_matches_pattern
_BLOCKED_USER_CACHE_TTL = 30  # seconds


def _is_blocked_row(row: Any) -> bool:
    """A row blocks the symbol when EITHER `isActive` is explicitly
    False OR `tradingEnabled` is explicitly False. NULL stays inherit-
    able (not a block)."""
    ia = getattr(row, "isActive", None)
    te = getattr(row, "tradingEnabled", None)
    return ia is False or te is False


async def get_user_blocked_symbols(
    user_id: str | PydanticObjectId,
) -> dict[str, Any]:
    """Compute symbol / pattern lists for hiding from a specific user's
    search / option-chain / watchlist results.

    REAL cascade (most-specific wins — admin reported "I allowed one
    user and it leaked to everyone"; root cause was the older helper
    only INGESTED blocks and never honoured per-user ALLOW overrides
    that should reverse a tier-level block):

        priority 0 (most specific)  user override symbol-specific
        priority 1                  broker exact-symbol script row
        priority 2                  admin exact-symbol script row
        priority 3                  platform exact-symbol script row
        priority 4                  broker pattern script row
        priority 5                  admin pattern script row
        priority 6                  platform pattern script row

    For each candidate symbol, the highest-priority row's
    `isActive` / `tradingEnabled` decides:
      • explicit False  → blocked
      • explicit True   → ALLOWED (overrides any lower-priority block)
      • None on both    → fall through to next layer

    Cached in Redis under `blocked_syms:<user_id>` for 30 s; admin
    edits invalidate via `_wipe_eff_cache_debounced()` at the
    segment / script / pool override save paths and via the
    targeted `cache_delete_pattern("blocked_syms:{uid}")` on
    user-override save / delete.

    Returns a structured payload the filter functions consume:
      {
        "symbols": [...],        # exact-match blocks
        "patterns": [[b, s]...], # pattern blocks (base + suffix)
        "allow_symbols": [...],  # symbols a more-specific layer
                                 # explicitly allowed (must take
                                 # precedence over pattern hits at
                                 # filter time)
      }
    """
    cache_key = f"blocked_syms:{user_id}"
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass

    user_doc = await User.get(PydanticObjectId(user_id))
    if user_doc is None:
        return {"symbols": [], "patterns": [], "allow_symbols": []}

    user_admin_id = user_doc.assigned_admin_id
    broker_anc = user_doc.broker_ancestry or []
    user_broker_id = broker_anc[-1] if broker_anc else None

    or_conditions: list[dict[str, Any]] = [
        {"scope_admin_id": None, "scope_broker_id": None},  # platform
    ]
    if user_admin_id is not None:
        or_conditions.append(
            {"scope_admin_id": user_admin_id, "scope_broker_id": None}
        )
    if user_broker_id is not None:
        or_conditions.append(
            {"scope_admin_id": None, "scope_broker_id": user_broker_id}
        )

    script_rows_coll = NettingScriptOverride.get_motor_collection()
    raw_script_rows = await script_rows_coll.find(
        {"$or": or_conditions}
    ).to_list(length=None)
    script_rows = [NettingScriptOverride.model_validate(r) for r in raw_script_rows]

    user_rows = await UserSegmentOverride.find(
        UserSegmentOverride.user_id == PydanticObjectId(str(user_id)),
    ).to_list()

    # Layered store: for each (segment_name, symbol_upper) keep the
    # highest-priority row's block / allow signal. Patterns get their
    # own layered store keyed by (segment_name, base, suffix).
    def _row_signal(r: Any) -> str | None:
        """Return 'block' / 'allow' / None for this row."""
        ia = getattr(r, "isActive", None)
        te = getattr(r, "tradingEnabled", None)
        # Explicit False on EITHER field → block. An explicit allow
        # only counts when BOTH (or the relevant) fields aren't False.
        if ia is False or te is False:
            return "block"
        if ia is True or te is True:
            return "allow"
        return None

    # Priority lookup for script overrides
    def _script_priority(r: Any, is_pattern: bool) -> int:
        scope_broker = getattr(r, "scope_broker_id", None)
        scope_admin = getattr(r, "scope_admin_id", None)
        if is_pattern:
            if scope_broker:
                return 4
            if scope_admin:
                return 5
            return 6
        # exact
        if scope_broker:
            return 1
        if scope_admin:
            return 2
        return 3

    # First pass: aggregate per (segment, symbol_upper) — user rows first
    by_exact: dict[tuple[str, str], tuple[int, str]] = {}
    patterns_layered: dict[tuple[str, str, str], tuple[int, str]] = {}

    # User overrides have priority 0 (symbol-specific). Segment-wide
    # user overrides apply to every symbol in the segment but we
    # can't enumerate; they're checked at filter time via
    # allow_segments / block_segments.
    for r in user_rows:
        if not r.symbol:
            continue
        sig = _row_signal(r)
        if sig is None:
            continue
        sym = r.symbol.strip().upper()
        split = _split_pattern(sym)
        if split is not None:
            # Shorthand row (`CRUDEOILFUT`) — it has to cover every expiry,
            # exactly like the script layer's patterns, and it outranks them
            # all at priority 0. NOTE only a block travels this path; an
            # ALLOW by pattern would need `is_symbol_blocked_for` to match
            # patterns too, which it doesn't — allow stays exact-only.
            base, suffix = split
            pkey = (r.segment_name, base, suffix)
            prev_p = patterns_layered.get(pkey)
            if prev_p is None or 0 < prev_p[0]:
                patterns_layered[pkey] = (0, sig)
            continue
        key = (r.segment_name, sym)
        prev = by_exact.get(key)
        if prev is None or 0 < prev[0]:
            by_exact[key] = (0, sig)

    # Script overrides
    for r in script_rows:
        if not r.symbol:
            continue
        sig = _row_signal(r)
        if sig is None:
            continue
        sym_raw = r.symbol.strip().upper()
        split = _split_pattern(sym_raw)
        is_pat = split is not None
        prio = _script_priority(r, is_pat)
        if is_pat:
            assert split is not None
            base, suffix = split
            pkey = (r.segment_name, base, suffix)
            prev_p = patterns_layered.get(pkey)
            if prev_p is None or prio < prev_p[0]:
                patterns_layered[pkey] = (prio, sig)
        else:
            key = (r.segment_name, sym_raw)
            prev = by_exact.get(key)
            if prev is None or prio < prev[0]:
                by_exact[key] = (prio, sig)

    symbols: set[str] = set()
    patterns: list[tuple[str, str]] = []
    allow_symbols: set[str] = set()
    for (_seg, sym), (_prio, sig) in by_exact.items():
        if sig == "block":
            symbols.add(sym)
        else:
            # explicit allow at the most-specific layer for this
            # (segment, symbol) → carry forward as an allow that
            # the filter checks BEFORE any pattern block.
            allow_symbols.add(sym)
    for (_seg, base, suffix), (_prio, sig) in patterns_layered.items():
        if sig == "block":
            patterns.append((base, suffix))

    payload: dict[str, Any] = {
        "symbols": sorted(symbols),
        "patterns": [list(p) for p in patterns],
        "allow_symbols": sorted(allow_symbols),
    }
    try:
        await cache_set(cache_key, payload, ttl_sec=_BLOCKED_USER_CACHE_TTL)
    except Exception:
        pass
    return payload


def is_symbol_blocked_for(
    instrument_symbol: str, blocked: dict[str, Any]
) -> bool:
    """Cheap O(P) check against the result of `get_user_blocked_symbols`.

    Filter order:
      1. If symbol appears in the allow set → NOT blocked (explicit
         user-level allow wins over any tier-level pattern block).
      2. If symbol appears in the block set → blocked.
      3. If symbol matches any pattern → blocked.
      4. Otherwise → not blocked.
    """
    if not instrument_symbol:
        return False
    sym = instrument_symbol.strip().upper()
    allow_set = blocked.get("allow_symbols") or []
    if isinstance(allow_set, list):
        allow_set = set(allow_set)
    if sym in allow_set:
        return False
    symbols_set = blocked.get("symbols") or []
    if isinstance(symbols_set, list):
        symbols_set = set(symbols_set)
    if sym in symbols_set:
        return True
    for entry in blocked.get("patterns") or []:
        if not entry or len(entry) != 2:
            continue
        base, suffix = entry[0], entry[1]
        if _instrument_matches_pattern(sym, base, suffix):
            return True
    return False


async def upsert_sub_admin_segment_override(
    sub_admin_id: str | PydanticObjectId,
    segment_name: str,
    patch: dict[str, Any],
) -> SubAdminSegmentOverride:
    sid = PydanticObjectId(sub_admin_id)
    existing = await SubAdminSegmentOverride.find_one(
        SubAdminSegmentOverride.sub_admin_id == sid,
        SubAdminSegmentOverride.segment_name == segment_name,
    )
    if existing is None:
        existing = SubAdminSegmentOverride(
            sub_admin_id=sid, segment_name=segment_name
        )
    for k, v in patch.items():
        if k in NETTING_FIELDS:
            setattr(existing, k, v)
    await existing.save()
    await _invalidate_pool_netting_cache(sid)
    return existing


async def delete_sub_admin_segment_override(
    sub_admin_id: str | PydanticObjectId, segment_name: str
) -> None:
    sid = PydanticObjectId(sub_admin_id)
    await SubAdminSegmentOverride.find(
        SubAdminSegmentOverride.sub_admin_id == sid,
        SubAdminSegmentOverride.segment_name == segment_name,
    ).delete()
    await _invalidate_pool_netting_cache(sid)


async def _invalidate_pool_netting_cache(sub_admin_id: PydanticObjectId) -> None:
    """Wipes the per-user `netting_eff:*` cache for all users (one SCAN),
    plus spread + inactive_admin_rows caches. Previously iterated N users
    and called cache_delete_pattern once per user — 106 SCAN operations
    caused ~10 s save latency. A single broad wipe is safe: netting_eff
    keys have a 30 s TTL and each user re-resolves their own slice on the
    next request, so wiping all users costs nothing beyond a brief cache-
    cold period."""
    await cache_delete_pattern("netting_eff:*")
    await cache_delete_pattern("spread:*")
    await cache_delete_pattern("inactive_admin_rows")
    await cache_delete_pattern("inactive_admin_rows:*")


# ── Super-admin segment defaults (super-admin's own pool) ────────────
async def list_super_admin_segment_overrides(
    super_admin_id: str | PydanticObjectId,
) -> list[SuperAdminSegmentOverride]:
    return await SuperAdminSegmentOverride.find(
        SuperAdminSegmentOverride.super_admin_id == PydanticObjectId(super_admin_id)
    ).to_list()


async def get_super_admin_segment_override(
    super_admin_id: str | PydanticObjectId, segment_name: str
) -> SuperAdminSegmentOverride | None:
    return await SuperAdminSegmentOverride.find_one(
        SuperAdminSegmentOverride.super_admin_id == PydanticObjectId(super_admin_id),
        SuperAdminSegmentOverride.segment_name == segment_name,
    )


async def upsert_super_admin_segment_override(
    super_admin_id: str | PydanticObjectId,
    segment_name: str,
    patch: dict[str, Any],
) -> SuperAdminSegmentOverride:
    sid = PydanticObjectId(super_admin_id)
    existing = await SuperAdminSegmentOverride.find_one(
        SuperAdminSegmentOverride.super_admin_id == sid,
        SuperAdminSegmentOverride.segment_name == segment_name,
    )
    if existing is None:
        existing = SuperAdminSegmentOverride(
            super_admin_id=sid, segment_name=segment_name
        )
    for k, v in patch.items():
        if k in NETTING_FIELDS:
            setattr(existing, k, v)
    await existing.save()
    await _invalidate_super_admin_pool_netting_cache()
    return existing


async def delete_super_admin_segment_override(
    super_admin_id: str | PydanticObjectId, segment_name: str
) -> None:
    sid = PydanticObjectId(super_admin_id)
    await SuperAdminSegmentOverride.find(
        SuperAdminSegmentOverride.super_admin_id == sid,
        SuperAdminSegmentOverride.segment_name == segment_name,
    ).delete()
    await _invalidate_super_admin_pool_netting_cache()


async def _invalidate_super_admin_pool_netting_cache() -> None:
    """Wipes per-user netting cache for all users (one SCAN) plus spread
    and inactive_admin_rows caches. Single broad wipe replaces the old
    per-user SCAN loop that caused ~10 s save latency with 100+ users."""
    await cache_delete_pattern("netting_eff:*")
    await cache_delete_pattern("spread:*")
    await cache_delete_pattern("inactive_admin_rows")
    await cache_delete_pattern("inactive_admin_rows:*")


# ── Broker segment defaults (broker's own pool) ──────────────────────
async def list_broker_segment_overrides(
    broker_id: str | PydanticObjectId,
) -> list[BrokerSegmentOverride]:
    return await BrokerSegmentOverride.find(
        BrokerSegmentOverride.broker_id == PydanticObjectId(broker_id)
    ).to_list()


async def get_broker_segment_override(
    broker_id: str | PydanticObjectId, segment_name: str
) -> BrokerSegmentOverride | None:
    return await BrokerSegmentOverride.find_one(
        BrokerSegmentOverride.broker_id == PydanticObjectId(broker_id),
        BrokerSegmentOverride.segment_name == segment_name,
    )


async def upsert_broker_segment_override(
    broker_id: str | PydanticObjectId,
    segment_name: str,
    patch: dict[str, Any],
) -> BrokerSegmentOverride:
    bid = PydanticObjectId(broker_id)
    existing = await BrokerSegmentOverride.find_one(
        BrokerSegmentOverride.broker_id == bid,
        BrokerSegmentOverride.segment_name == segment_name,
    )
    if existing is None:
        existing = BrokerSegmentOverride(broker_id=bid, segment_name=segment_name)
    for k, v in patch.items():
        if k in NETTING_FIELDS:
            setattr(existing, k, v)
    await existing.save()
    await _invalidate_broker_pool_netting_cache(bid)
    return existing


async def delete_broker_segment_override(
    broker_id: str | PydanticObjectId, segment_name: str
) -> None:
    bid = PydanticObjectId(broker_id)
    await BrokerSegmentOverride.find(
        BrokerSegmentOverride.broker_id == bid,
        BrokerSegmentOverride.segment_name == segment_name,
    ).delete()
    await _invalidate_broker_pool_netting_cache(bid)


async def _invalidate_broker_pool_netting_cache(broker_id: PydanticObjectId) -> None:
    """Wipes per-user netting cache for all users (one SCAN) plus spread
    and inactive_admin_rows caches. Single broad wipe replaces the old
    per-user SCAN loop that caused ~10 s save latency with 100+ users."""
    await cache_delete_pattern("netting_eff:*")
    await cache_delete_pattern("spread:*")
    await cache_delete_pattern("inactive_admin_rows")
    await cache_delete_pattern("inactive_admin_rows:*")


# ── Per-user segment overrides ────────────────────────────────────
async def list_user_overrides(user_id: str | PydanticObjectId) -> list[UserSegmentOverride]:
    return await UserSegmentOverride.find(
        UserSegmentOverride.user_id == PydanticObjectId(user_id)
    ).to_list()


async def upsert_user_override(
    user_id: str | PydanticObjectId,
    segment_name: str,
    patch: dict[str, Any],
    symbol: str | None = None,
) -> UserSegmentOverride:
    uid = PydanticObjectId(user_id)
    sym = (symbol or "").strip().upper() or None
    existing = await UserSegmentOverride.find_one(
        UserSegmentOverride.user_id == uid,
        UserSegmentOverride.segment_name == segment_name,
        UserSegmentOverride.symbol == sym,
    )
    if existing is None:
        existing = UserSegmentOverride(user_id=uid, segment_name=segment_name, symbol=sym)
    for k, v in patch.items():
        if k in NETTING_FIELDS:
            setattr(existing, k, v)
    await existing.save()
    # Wipe per-user effective-settings + per-user inactive-rows +
    # blocked-symbols cache so next read reflects this override.
    # `isActive` / `tradingEnabled` are in NETTING_FIELDS, so a
    # per-user allow / block lands on UserSegmentOverride and must
    # immediately re-resolve the user's:
    #   • netting_eff:{uid}:*       — effective margins / lots / etc.
    #   • inactive_admin_rows:{uid} — segment-level inactive set
    #   • blocked_syms:{uid}        — per-symbol block set (drives
    #     search / option-chain / watchlist filtering)
    # User-flagged regression: admin's "allow CRUDEOIL for U1" wasn't
    # honoured because `blocked_syms:U1` stayed stale on a 30 s TTL
    # and the helper itself wasn't honouring per-user allow rows;
    # both ends are fixed now.
    await cache_delete_pattern(f"netting_eff:{uid}:*")
    await cache_delete_pattern(f"inactive_admin_rows:{uid}")
    await cache_delete_pattern(f"blocked_syms:{uid}")
    return existing


async def delete_user_override(
    user_id: str | PydanticObjectId,
    segment_name: str,
    symbol: str | None = None,
) -> None:
    uid = PydanticObjectId(user_id)
    sym = (symbol or "").strip().upper() or None
    await UserSegmentOverride.find(
        UserSegmentOverride.user_id == uid,
        UserSegmentOverride.segment_name == segment_name,
        UserSegmentOverride.symbol == sym,
    ).delete()
    # Same triple-wipe as upsert — removing a per-user allow / block
    # must immediately re-resolve the affected user's block set.
    await cache_delete_pattern(f"netting_eff:{uid}:*")
    await cache_delete_pattern(f"inactive_admin_rows:{uid}")
    await cache_delete_pattern(f"blocked_syms:{uid}")


async def clear_all_user_overrides(
    user_id: str | PydanticObjectId,
) -> int:
    """Wipe every UserSegmentOverride doc for `user_id` so the user
    snaps cleanly back to the inherited cascade (broker / admin /
    super-admin / platform defaults). Returns the count removed for
    the operator's toast.

    Admin-flagged: "user me ek baar setting karne ke baad delete
    karne ka option nahi hai taki user wapas global settings me a
    jaye". Existing `delete_user_override` only takes ONE segment +
    symbol — to fully reset a user the admin had to click reset on
    every row, one by one. This is the one-shot version.

    Also busts every per-user cache key (netting_eff, inactive
    admin rows, blocked_syms) so the next order resolves cleanly
    instead of waiting on the 30 s / 5 min TTLs.
    """
    uid = PydanticObjectId(str(user_id))
    rows = await UserSegmentOverride.find(
        UserSegmentOverride.user_id == uid
    ).to_list()
    deleted = len(rows)
    if deleted:
        await UserSegmentOverride.find(
            UserSegmentOverride.user_id == uid
        ).delete()
    await cache_delete_pattern(f"netting_eff:{uid}:*")
    await cache_delete_pattern(f"inactive_admin_rows:{uid}")
    await cache_delete_pattern(f"blocked_syms:{uid}")
    return deleted


# ── Effective resolver (legacy field-name shim for order_validator) ─
# Map legacy SegmentType strings (NSE_EQUITY, NSE_FUTURE, …) to NettingSegment
# names (NSE_EQ, NSE_FUT, …). Multiple legacy types fold into one netting row.
_SEGMENT_NAME_MAP: dict[str, str] = {
    "NSE_EQUITY": "NSE_EQ",
    # NSE F&O split stock-vs-index. The instrument SegmentType enum already
    # distinguishes stock vs index, so each canonical value routes to its own
    # granular settings row.
    "NSE_FUTURE": "NSE_STK_FUT",
    "NSE_INDEX_FUTURE": "NSE_IDX_FUT",
    "NSE_STOCK_OPTION_BUY": "NSE_STK_OPT",
    "NSE_STOCK_OPTION_SELL": "NSE_STK_OPT",
    "NSE_INDEX_OPTION_BUY": "NSE_IDX_OPT",
    "NSE_INDEX_OPTION_SELL": "NSE_IDX_OPT",
    "BSE_EQUITY": "BSE_EQ",
    "BSE_FUTURE": "BSE_FUT",
    "BSE_INDEX_FUTURE": "BSE_FUT",
    "BSE_OPTION_BUY": "BSE_OPT",
    "BSE_OPTION_SELL": "BSE_OPT",
    "MCX_FUTURE": "MCX_FUT",
    "MCX_OPTION_BUY": "MCX_OPT",
    "MCX_OPTION_SELL": "MCX_OPT",
    "CDS_FUTURE": "FOREX",
    "CDS_OPTION_BUY": "FOREX",
    "CDS_OPTION_SELL": "FOREX",
    # Every crypto instrument resolves to the single CRYPTO admin row.
    "CRYPTO_SPOT": "CRYPTO",
    "CRYPTO_FUTURE": "CRYPTO",
    "CRYPTO_PERPETUAL": "CRYPTO",
    # Infoway-fed international markets resolve to their own admin rows.
    # The instrument segment value already matches the admin row name —
    # we map them through explicitly so the resolver doesn't fall back
    # to the synthetic permissive defaults.
    "FOREX": "FOREX",
    "STOCKS": "STOCKS",
    "INDICES": "INDICES",
    "COMMODITIES": "COMMODITIES",
    # Legacy compatibility: instrument rows created before the
    # _auto_create_instrument / _mirror_from_zerodha segment-naming fix
    # (commit landed 2026-05) stored segment as the Kite exchange code
    # + suffix (e.g. "BFO_FUT") instead of the canonical SegmentType.
    # Map them through so admin's per-segment settings still apply on
    # those legacy positions/orders without forcing a data migration.
    "BFO_FUT": "BSE_FUT",
    "BFO_OPT": "BSE_OPT",
    # Legacy/ambiguous NFO aliases can't self-distinguish stock vs index, so
    # they route to the STOCK row; the instrument boot-remap canonicalises
    # them to NSE_STOCK_*/NSE_INDEX_* over time (→ correct granular row).
    "NFO_FUT": "NSE_STK_FUT",
    "NFO_OPT": "NSE_STK_OPT",
    "MCX_FUT": "MCX_FUT",
    "MCX_OPT": "MCX_OPT",
    "BSE_FUT": "BSE_FUT",
    "BSE_OPT": "BSE_OPT",
    "NSE_FUT": "NSE_STK_FUT",
    "NSE_OPT": "NSE_STK_OPT",
    "NSE_EQ": "NSE_EQ",
    "BSE_EQ": "BSE_EQ",
    # Even-older mirror format (pre-2025) — used singular "OPTION" /
    # "FUTURE" suffix without exchange-side disambiguation. Found in
    # the wild on COPPER26MAY*CE (MCX_OPTION) and BANKNIFTY26MAY*CE
    # (NFO_OPTION). These collisions cause the resolver to fall
    # through to synthetic permissive defaults (marginCalcMode=None,
    # intradayMargin=100) which renders as "Fixed · ₹100/lot" on the
    # user-side panel regardless of what admin sets for the row.
    "MCX_OPTION": "MCX_OPT",
    "MCX_FUTURE": "MCX_FUT",
    "NFO_OPTION": "NSE_STK_OPT",
    "NFO_FUTURE": "NSE_STK_FUT",
    "BFO_OPTION": "BSE_OPT",  # (BSE stays one option row — not split)
    "BFO_FUTURE": "BSE_FUT",
}


# Index underlyings — an option / future on one of these is an INDEX
# derivative. Used to disambiguate the legacy/ambiguous NFO/BFO segment names
# (which the map routes to the STOCK row by default) so the Stock vs Index
# settings rows stay independent.
_INDEX_UNDERLYING_ROOTS: tuple[str, ...] = (
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "MIDCAPNIFTY", "SENSEX", "BANKEX",
)


def resolve_admin_row(segment_type: str, symbol: str | None = None) -> str:
    """Instrument SegmentType → admin settings-row name, SYMBOL-AWARE.

    `_SEGMENT_NAME_MAP` maps the ambiguous legacy NFO/BFO option+future
    segments (``NFO_OPTION`` / ``NFO_OPT`` / ``NSE_OPT`` / ``NFO_FUTURE`` …) to
    the STOCK row because the segment string alone can't tell stock from index
    — and many index-option instruments are still stored with the legacy
    ``NFO_OPTION`` segment. When the SYMBOL is an index underlying
    (NIFTY / BANKNIFTY / SENSEX …) we re-route to the INDEX row, so the four
    NSE F&O settings rows stay fully independent: blocking / margining Stock
    Options never touches Index Options and vice-versa. Canonical instruments
    (``NSE_INDEX_OPTION_*``) already map straight to the index row, so this
    only kicks in for the ambiguous stock-default cases.
    """
    row = _SEGMENT_NAME_MAP.get(segment_type, segment_type)
    if symbol and row in ("NSE_STK_OPT", "NSE_STK_FUT"):
        if str(symbol).upper().lstrip().startswith(_INDEX_UNDERLYING_ROOTS):
            return "NSE_IDX_OPT" if row == "NSE_STK_OPT" else "NSE_IDX_FUT"
    return row


def instrument_segments_for(admin_segment_name: str) -> list[str]:
    """Reverse of ``_SEGMENT_NAME_MAP``: the instrument ``segment`` values that
    resolve to a given admin segment row (e.g. "NSE_FUT" →
    ["NSE_FUTURE", "NSE_INDEX_FUTURE", "NFO_FUT", …]). Used to scope
    Instrument-catalog queries — the script-override symbol picker and its
    server-side validator — to exactly the segment the admin selected, so a
    FOREX/COMMODITIES/STOCKS/INDICES (Infoway) segment can be searched the
    same way as the Zerodha-backed ones."""
    name = (admin_segment_name or "").upper()
    hits = [k for k, v in _SEGMENT_NAME_MAP.items() if v == name]
    return hits or ([name] if name else [])


def _to_legacy_dict(
    seg,
    override,
    *,
    action: str | None = None,
    option_type: str | None = None,
    product_type: str | None = None,
    is_expiry_day: bool = False,
) -> dict[str, Any]:
    """Map NettingSegment + optional UserSegmentOverride → legacy field names
    that order_validator + brokerage_calculator consume.

    When `option_type` ∈ {"CE", "PE"} and `action` ∈ {"BUY","SELL"}, the
    resolver picks `optionBuyIntraday` / `optionSellIntraday` (and equivalent
    overnight + commission fields) instead of the segment-wide values. This
    lets admins tune option-buy and option-sell margins separately, the way
    the netting UI advertises.
    """

    def pick(field: str, default=None):
        if override is not None:
            v = getattr(override, field, None)
            if v is not None:
                return v
        return getattr(seg, field, default)

    margin_mode = pick("marginCalcMode", None)
    # Defensive default when the admin row has never been saved with an
    # explicit mode. The legacy fallback was `"percent"`, which silently
    # interpreted Intraday=700 as "700% margin" and capped the display at
    # 100% via the OrderPanel's default. After dropping the percent mode
    # from the admin dropdown, any unset value should infer mode from the
    # configured number:
    #   • intradayMargin > 100 → almost certainly a leverage multiplier
    #     (a percentage above 100 doesn't make sense); treat as Times.
    #   • otherwise → flat ₹/lot (Fixed).
    # Admins who explicitly chose Times / Fixed get whatever they picked.
    if margin_mode not in ("fixed", "times", "percent"):
        sniff_value = float(pick("intradayMargin", 0.0) or 0.0)
        margin_mode = "times" if sniff_value > 100.0 else "fixed"
    is_option = (option_type or "").upper() in ("CE", "PE")
    is_option_buy = is_option and (action or "").upper() == "BUY"
    is_option_sell = is_option and (action or "").upper() == "SELL"

    # Per-side override for option BUY / SELL. NULL = inherit segment-level
    # marginCalcMode resolved above. When explicitly set, it overrides
    # the mode for that side only — admin can run option BUY in Fixed
    # (flat ₹/lot) while option SELL stays on Times (multiplier).
    if is_option_buy:
        side_mode = pick("optionBuyMarginCalcMode", None)
        if side_mode in ("fixed", "times", "percent"):
            margin_mode = side_mode
    elif is_option_sell:
        side_mode = pick("optionSellMarginCalcMode", None)
        if side_mode in ("fixed", "times", "percent"):
            margin_mode = side_mode

    # `Times` mode quotes a leverage multiplier (e.g. 700×), which is symmetric
    # across intraday and overnight — telling a user "you have 700× intraday
    # leverage but only 100× overnight" doesn't match how brokers price
    # leverage. So in Times mode we always read the `*Intraday*` field and
    # use it for any product type. The intraday/overnight split only matters
    # for `Percent` / `Fixed` mode, where margin actually carries more cost
    # to hold overnight.
    #
    # Infoway-fed segments (FOREX / STOCKS / INDICES / COMMODITIES / CRYPTO)
    # have no daily settlement — there's no separate "overnight" cost. We
    # always read the *Intraday* margin for those rows regardless of the
    # product_type sent by the order. Pairs with the admin matrix UI
    # gating in nettingMatrixConfig.ts so the overnight columns render as
    # N/A (—) and aren't editable.
    seg_name_for_check = getattr(seg, "name", "")
    if seg_name_for_check in INTRADAY_ONLY_ADMIN_ROWS:
        is_overnight = False
    else:
        is_overnight = (
            False if margin_mode == "times" else (product_type or "").upper() in ("CNC", "NRML")
        )

    # Resolve effective margin %. Order matters: expiry-day → option BUY/SELL
    # specifics → segment-wide intraday/overnight.
    #
    # Segment-wide fallback for OPT rows: the admin matrix exposes
    # `intradayMargin` on every row including options. If the admin sets
    # it to a non-default value (e.g. Times=700 for MCX OPT) but never
    # touches `optionBuyIntraday` / `optionSellIntraday` (still at the
    # seed default of 100/15), the resolver previously ignored their
    # intent and returned the option-specific default. Treat the
    # option-specific columns as overrides ONLY when admin has set them
    # to a value different from the seed default; otherwise inherit the
    # row's segment-wide intraday/overnight value.
    seg_intraday = float(pick("intradayMargin", 100.0) or 100.0)
    seg_overnight = float(pick("overnightMargin", 100.0) or 100.0)
    seg_value_for_now = seg_overnight if is_overnight else seg_intraday

    def _opt_pick(field: str, ovn_field: str) -> float:
        """Option-specific margin override. NULL or 0 = inherit from
        segment-wide intraday/overnight; any other number is an explicit
        per-side override. Treat 0 as inherit since the matrix UI uses
        plain number inputs with no "blank/inherit" affordance — typing
        0 is the only way for admin to signal "don't override".
        """
        opt_ovn = pick(ovn_field, None) if is_overnight else None
        opt_intra = pick(field, None)
        chosen = opt_ovn if opt_ovn is not None else opt_intra
        if chosen is None or float(chosen) == 0.0:
            return seg_value_for_now
        return float(chosen)

    if is_expiry_day:
        # Expiry-day margin. An expiry-day field that is NULL, 0, OR still at
        # its SEED DEFAULT (intraday/opt-buy = 100, opt-sell = 50) means
        # "admin never set a stricter expiry-day tier" → INHERIT the regular
        # resolved margin (exactly what the non-expiry path below computes),
        # NOT the seed default.
        #
        # Why the seed-default check is essential (the ₹50/lot bug): the old
        # code did `pick("expiryDayOptionSellMargin", 50.0) or 50.0`, so on a
        # contract's own expiry day EVERY weekly option SELL collapsed to
        # ₹50/lot in fixed mode — the admin's real Opt-Sell Fixed ₹25,00,000
        # was ignored, letting users short expiry options for almost no
        # margin. `normalize_seed_defaults` is supposed to NULL those seed
        # values on boot so an inherit fires, but if it hasn't run (or the DB
        # row still literally holds 50/100), a plain `is None` check treats
        # the seed value as an explicit admin override and re-applies it. So
        # we treat the seed value ITSELF as "inherit" here — same rule
        # normalize_seed_defaults uses — making the fix independent of whether
        # the heal migration ran. An admin who DOES want a stricter expiry-day
        # tier sets a value ≠ 0 and ≠ the seed default, and that wins.
        _SEED_EXP_INTRA = 100.0
        _SEED_EXP_OPT_BUY = 100.0
        _SEED_EXP_OPT_SELL = 50.0

        def _expiry_is_unset(val, seed: float) -> bool:
            if val is None:
                return True
            try:
                fv = float(val)
            except (TypeError, ValueError):
                return True
            return fv == 0.0 or fv == seed

        if is_option_buy:
            _ed = pick("expiryDayOptionBuyMargin", None)
            effective_margin_pct = (
                _opt_pick("optionBuyIntraday", "optionBuyOvernight")
                if _expiry_is_unset(_ed, _SEED_EXP_OPT_BUY)
                else float(_ed)
            )
        elif is_option_sell:
            _ed = pick("expiryDayOptionSellMargin", None)
            effective_margin_pct = (
                _opt_pick("optionSellIntraday", "optionSellOvernight")
                if _expiry_is_unset(_ed, _SEED_EXP_OPT_SELL)
                else float(_ed)
            )
        else:
            _ed = pick("expiryDayIntradayMargin", None)
            effective_margin_pct = (
                seg_value_for_now
                if _expiry_is_unset(_ed, _SEED_EXP_INTRA)
                else float(_ed)
            )
    elif is_option_buy:
        effective_margin_pct = _opt_pick("optionBuyIntraday", "optionBuyOvernight")
    elif is_option_sell:
        effective_margin_pct = _opt_pick("optionSellIntraday", "optionSellOvernight")
    else:
        effective_margin_pct = seg_value_for_now

    # Always resolve the OVERNIGHT (carry-forward) variant too — even when
    # the current order's product_type is intraday. The frontend trade
    # panel displays both Intraday + Carry-forward tiles side-by-side so
    # the user can see what the position would cost if they held it
    # overnight before they place the trade.
    if is_expiry_day:
        # Expiry-day rates apply to BOTH legs of the day; no separate
        # "overnight" exists because positions don't survive expiry.
        effective_overnight_pct = effective_margin_pct
    elif is_option_buy:
        opt_ovn = pick("optionBuyOvernight", None)
        opt_intra = pick("optionBuyIntraday", None)
        chosen = opt_ovn if opt_ovn is not None else opt_intra
        effective_overnight_pct = (
            seg_overnight
            if chosen is None or float(chosen) == 0.0
            else float(chosen)
        )
    elif is_option_sell:
        opt_ovn = pick("optionSellOvernight", None)
        opt_intra = pick("optionSellIntraday", None)
        chosen = opt_ovn if opt_ovn is not None else opt_intra
        effective_overnight_pct = (
            seg_overnight
            if chosen is None or float(chosen) == 0.0
            else float(chosen)
        )
    else:
        # INTRADAY_ONLY admin rows (Forex / Crypto / spot Commodity / etc.)
        # have no separate carry-forward concept; the overnight column is
        # explicitly N/A in the admin matrix. Mirror that here by reusing
        # the intraday pct rather than a stale `seg_overnight` default.
        effective_overnight_pct = (
            effective_margin_pct
            if seg_name_for_check in INTRADAY_ONLY_ADMIN_ROWS
            else seg_overnight
        )

    # Translate the admin's chosen mode into the legacy
    # {leverage, margin_percentage, fixed_margin_per_lot} triple consumed
    # by order_validator + OrderPanel.
    #
    #   times → effective value is the leverage multiplier (100 → 100×).
    #           margin_required = notional × 100% ÷ leverage
    #
    #   fixed → effective value is a flat rupee amount per lot. Price and
    #           lot_size don't enter the formula. margin_required = lots ×
    #           fixed_margin_per_lot. The legacy `margin_percentage` /
    #           `leverage` pair is zeroed out so any older consumer that
    #           still uses them produces 0 (and falls through to the new
    #           fixed-per-lot path).
    #
    #   percent (legacy) → kept working for migration. effective value is
    #           a percent of notional. New rows can't be created in this
    #           mode (admin dropdown no longer offers it) but existing
    #           docs continue to resolve.
    fixed_margin_per_lot = 0.0
    overnight_fixed_margin_per_lot = 0.0
    if margin_mode == "times":
        leverage = max(1.0, effective_margin_pct)
        margin_pct = 100.0
        overnight_leverage = max(1.0, effective_overnight_pct)
        overnight_margin_pct = 100.0
    elif margin_mode == "fixed":
        fixed_margin_per_lot = float(effective_margin_pct or 0.0)
        leverage = 1.0
        margin_pct = 0.0
        overnight_fixed_margin_per_lot = float(effective_overnight_pct or 0.0)
        overnight_leverage = 1.0
        overnight_margin_pct = 0.0
    else:  # legacy "percent"
        leverage = 1.0
        margin_pct = effective_margin_pct
        overnight_leverage = 1.0
        overnight_margin_pct = effective_overnight_pct

    # Diagnostic log — one line per resolution. Lets us answer "is the
    # running process on the symmetric-Times patch?" by tailing the backend
    # console: a `is_ovn=False mode=times` line for an NRML order proves
    # the patch is live; `is_ovn=True mode=times` means it's not.
    logger.info(
        "netting_resolve seg=%s mode=%s product=%s is_ovn=%s eff_pct=%s leverage=%s margin_pct=%s",
        getattr(seg, "name", "?"),
        margin_mode,
        (product_type or "?"),
        is_overnight,
        effective_margin_pct,
        leverage,
        margin_pct,
    )

    # Action-aware commission (option leg vs everything else).
    # NOTE: use explicit `is not None` checks — Python's `or` treats 0 as
    # falsy, so `0 or fallback` would silently ignore an admin-set zero
    # commission and apply the next tier's non-zero value instead.
    commission_type_raw = pick("commissionType", "per_lot")
    if is_option_buy:
        _obc = pick("optionBuyCommission", None)
        commission_value = float(_obc if _obc is not None else pick("commission", 0.0) or 0.0)
    elif is_option_sell:
        _osc = pick("optionSellCommission", None)
        commission_value = float(_osc if _osc is not None else pick("commission", 0.0) or 0.0)
    else:
        commission_value = float(pick("commission", 0.0) or 0.0)
    if commission_type_raw == "per_crore":
        legacy_commission_type = "PER_CRORE"
    elif commission_type_raw == "per_lot":
        legacy_commission_type = "PER_LOT"
    else:
        legacy_commission_type = "PERCENTAGE"

    # Segment metadata — these flags decide whether lot-based or qty-based
    # caps apply. NSE_OPT, NSE_FUT etc. are LOT-based: 1 lot = N units, the
    # qty caps are meaningless and just block legitimate orders. NSE_EQ is
    # QTY-based: every share is one unit, the lot caps don't apply.
    lot_applies = getattr(seg, "lotApplies", True)
    qty_applies = getattr(seg, "qtyApplies", False)

    # Min-lot is whatever the admin resolved for the segment (per-user → script
    # → broker → admin → super-admin → global). We do NOT floor it: this is a
    # B-book, so an admin may deliberately allow a fractional lot (e.g. MCX FUT
    # min 0.1). The earlier stray-0.01 problem from the NSE F&O segment split is
    # handled by the one-shot data cleanup (scripts/fix_fno_min_lot), not by
    # overriding the admin's explicit choice here. Falls back to 1.0 only when
    # the value is genuinely unset (0 / None).
    _min_lot = float(pick("minLots", 1.0) if pick("minLots", 1.0) else 1.0) if lot_applies else 0.0

    return {
        # legacy 22-field shape (and a few netting-only extras)
        # `allow` is the combined gate for backwards-compat (OrderPanel reads
        # it for the "no trading" warning). Two flags are also exposed
        # separately so the validator can permit closing trades even when
        # `tradingEnabled = false` — see admin Block settings spec.
        "allow": bool(pick("tradingEnabled", True)) and bool(pick("isActive", True)),
        "is_active": bool(pick("isActive", True)),
        "trading_enabled": bool(pick("tradingEnabled", True)),
        "commission_type": legacy_commission_type,
        "commission_value": commission_value,
        "min_brokerage": 0.0,
        "min_lot": _min_lot,
        "max_lot": float(pick("maxLots", 0.0) or 0.0) if lot_applies else 0.0,
        "order_lot": float(pick("orderLots", 0.0) or 0.0) if lot_applies else 0.0,
        "intraday_lot_limit": float(pick("maxExchangeLots", 0.0) or 0.0) if lot_applies else 0.0,
        "holding_lot_limit": float(pick("maxExchangeLots", 0.0) or 0.0) if lot_applies else 0.0,
        "selling_overnight": bool(pick("allowOvernight", True)),
        "limit_percentage": float(pick("limitAwayPercent", 0.0) or 0.0),
        "limit_within_day_range": bool(pick("limitWithinDayRange", False) or False),
        "block_pending_orders": bool(pick("blockPendingOrders", False) or False),
        # Per-segment bracket toggles. Default True so untouched segments
        # keep the historical "SL & TP always allowed" behaviour. When an
        # admin turns one OFF: the order validator + update-SL/TP endpoint
        # reject setting that leg, and the risk enforcer won't auto-fire an
        # existing leg of that kind on positions in this segment.
        "sl_enabled": bool(pick("slEnabled", True) if pick("slEnabled", True) is not None else True),
        "tp_enabled": bool(pick("tpEnabled", True) if pick("tpEnabled", True) is not None else True),
        "strike_difference": 5,
        "max_each_lot": float(pick("maxLots", 0.0) or 0.0) if lot_applies else 0.0,
        "otm_max_each_lot": float(pick("maxLots", 0.0) or 0.0) if lot_applies else 0.0,
        "expiry_loss_holding": float(pick("expiryLossHoldMinSeconds", 0) or 0),
        "expiry_profit_hold": float(pick("expiryProfitHoldMinSeconds", 0) or 0),
        "expiry_intraday_margin": float(pick("expiryDayIntradayMargin", effective_margin_pct) or effective_margin_pct),
        # When True the three `expiry_*_margin` numbers in this dict are
        # % of notional; when False they're flat ₹ per lot (same shape as
        # `fixed_margin_per_lot`). The validator short-circuits on this
        # flag the same way it does for the segment-level `marginCalcMode`.
        "expiry_margin_as_percent": bool(
            pick("expiryDayMarginAsPercent", True) if pick("expiryDayMarginAsPercent", True) is not None else True
        ),
        "margin_percentage": margin_pct,
        "leverage": leverage,
        "margin_calc_mode": margin_mode,
        # Flat ₹/lot — only non-zero when mode == "fixed". Validator + UI
        # short-circuit on this and skip the notional × pct ÷ leverage
        # path entirely, so the configured value is the literal margin
        # locked per lot.
        "fixed_margin_per_lot": float(fixed_margin_per_lot),
        # ── Carry-forward (overnight) equivalents ─────────────────────
        # Computed in parallel with the intraday set above so the trade
        # panel can render both tiles ("Intraday ₹X" / "Carry-forward ₹Y")
        # without any frontend-side multiplier guesses. For intraday-only
        # segments (Forex / Crypto / spot Commodity) overnight equals
        # intraday — there's no separate carry tier on those instruments.
        "overnight_margin_percentage": overnight_margin_pct,
        "overnight_leverage": overnight_leverage,
        "overnight_fixed_margin_per_lot": float(overnight_fixed_margin_per_lot),
        "auto_squareoff_time": "15:15",
        "m2m_squareoff_percent": 80.0,
        "stop_loss_mandatory": False,
        # ── Netting-only fields exposed for validator ──────────────
        # `lot_applies` / `qty_applies` let the validator skip the caps that
        # don't make sense for this segment kind. Without this gating the
        # default `perOrderQty=1` on a lot-based segment (NFO_OPT) blocks
        # every legitimate option order because lot_size×lots > 1.
        "lot_applies": bool(lot_applies),
        "qty_applies": bool(qty_applies),
        "max_value": float(pick("maxValue", 0.0) or 0.0),
        "min_qty": float(pick("minQty", 0.0) or 0.0) if qty_applies else 0.0,
        "per_order_qty": float(pick("perOrderQty", 0.0) or 0.0) if qty_applies else 0.0,
        "max_qty_per_script": float(pick("maxQtyPerScript", 0.0) or 0.0) if qty_applies else 0.0,
        # Single percent that gates option-leg orders on BOTH sides and
        # filters the option chain dialog. 0 = no cap.
        "strike_far_percent": float(pick("strikeFarPercent", 0.0) or 0.0),
        "spread_type": str(pick("spreadType", "fixed")),
        "spread_pips": float(pick("spreadPips", 0.0) or 0.0),
        "swap_type": str(pick("swapType", "points")),
        "swap_long": float(pick("swapLong", 0.0) or 0.0),
        "swap_short": float(pick("swapShort", 0.0) or 0.0),
        "swap_time": str(pick("swapTime", "22:30")),
        "charge_on": str(pick("chargeOn", "both")),
    }


async def get_effective_settings(
    user_id: str | PydanticObjectId,
    segment_type: str,
    *,
    action: str | None = None,
    option_type: str | None = None,
    product_type: str | None = None,
    is_expiry_day: bool = False,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Legacy-compat resolver. Returns the merged `NettingSegment +
    NettingScriptOverride (per-symbol) + UserSegmentOverride (per-user)` view
    in the field shape ``order_validator`` + ``brokerage_calculator`` consume.

    When ``action`` / ``option_type`` / ``product_type`` are passed we pick
    option-buy vs option-sell margin and commission, intraday vs overnight,
    expiry-day vs normal — so the order validator gets the exact margin %
    and commission that should be applied to **this** specific order.
    """
    # Symbol-aware so an index option (legacy segment NFO_OPTION → stock row)
    # resolves to NSE_IDX_OPT, keeping Stock vs Index settings independent.
    seg_name = resolve_admin_row(segment_type, symbol)
    sym_key = (symbol or "").strip().upper() or "_"
    cache_key = (
        f"netting_eff:{user_id}:{seg_name}:{sym_key}:"
        f"{(action or '_').upper()}:{(option_type or '_').upper()}:"
        f"{(product_type or '_').upper()}:{int(is_expiry_day)}"
    )
    try:
        cached = await cache_get(cache_key)
        if cached is not None:
            return cached
    except Exception:
        logger.warning("netting_cache_get_failed", extra={"key": cache_key})

    seg = await NettingSegment.find_one(NettingSegment.name == seg_name)
    if seg is None:
        # No row yet (un-seeded segment) — return all-permissive defaults
        seg = NettingSegment(
            name=seg_name, displayName=seg_name, **NettingFieldsRequired().model_dump()
        )

    # Resolve per-symbol script override (tier-aware).
    #
    # Match modes — exact wins over pattern; within each match mode the
    # most-specific tier wins (broker > admin > platform). Tier scope was
    # added 2026-05-20 so admin/broker could create their own per-symbol
    # overrides without touching the platform-wide row. User-flagged:
    # "sub-admin jab script block kar raha hai to super-admin required
    # aa raha hai".
    #
    # Resolution order, fall through to next on no match:
    #   1. (broker scope, exact symbol)
    #   2. (admin scope, exact symbol)
    #   3. (platform, exact symbol)
    #   4. (broker scope, pattern symbol)   — NIFTYFUT / BANKNIFTYCE etc.
    #   5. (admin scope, pattern symbol)
    #   6. (platform, pattern symbol)
    # First hit wins. user_doc has to be fetched first now so we know
    # which tier scopes are applicable; the user_doc / pool_override
    # block below was inlined here to avoid two User.get calls.
    user_doc = await User.get(PydanticObjectId(user_id))
    _user_broker_id: PydanticObjectId | None = None
    _user_admin_id: PydanticObjectId | None = None
    if user_doc is not None:
        broker_anc = user_doc.broker_ancestry or []
        if broker_anc:
            _user_broker_id = broker_anc[-1]
        if user_doc.assigned_admin_id is not None:
            _user_admin_id = user_doc.assigned_admin_id

    script_override = None
    if symbol:
        sym_normalised = symbol.strip().upper()

        async def _find_exact(
            ad: PydanticObjectId | None, br: PydanticObjectId | None
        ) -> "NettingScriptOverride | None":
            return await NettingScriptOverride.find_one(
                NettingScriptOverride.segment_name == seg_name,
                NettingScriptOverride.symbol == sym_normalised,
                NettingScriptOverride.scope_admin_id == ad,
                NettingScriptOverride.scope_broker_id == br,
            )

        # Exact-match cascade
        if _user_broker_id is not None:
            script_override = await _find_exact(None, _user_broker_id)
        if script_override is None and _user_admin_id is not None:
            script_override = await _find_exact(_user_admin_id, None)
        if script_override is None:
            script_override = await _find_exact(None, None)

        # Pattern-match cascade — same tier order
        if script_override is None and _user_broker_id is not None:
            script_override = await _match_pattern_script(
                seg_name, sym_normalised, scope_broker_id=_user_broker_id
            )
        if script_override is None and _user_admin_id is not None:
            script_override = await _match_pattern_script(
                seg_name, sym_normalised, scope_admin_id=_user_admin_id
            )
        if script_override is None:
            script_override = await _match_pattern_script(seg_name, sym_normalised)

    user_override_symbol = await UserSegmentOverride.find_one(
        UserSegmentOverride.user_id == PydanticObjectId(user_id),
        UserSegmentOverride.segment_name == seg_name,
        UserSegmentOverride.symbol == (symbol.strip().upper() if symbol else None),
    )
    # Exact miss → try the shorthand rows (`CRUDEOILFUT`), same as the
    # script layer does. Without this a per-user script override only ever
    # applied if the admin typed the full contract, which nobody does
    # because it changes every month.
    if user_override_symbol is None and symbol:
        user_override_symbol = await _match_pattern_user_override(
            PydanticObjectId(user_id), seg_name, symbol.strip().upper()
        )
    user_override_segment = await UserSegmentOverride.find_one(
        UserSegmentOverride.user_id == PydanticObjectId(user_id),
        UserSegmentOverride.segment_name == seg_name,
        UserSegmentOverride.symbol == None,  # noqa: E711
    )

    # Pool-default cascade — collect overrides from EVERY tier in the
    # user's ownership chain (broker → admin → super-admin). Previously
    # this was an `if/elif/else` that picked EXACTLY ONE tier — so when
    # a user was under a broker with no override, the admin's override
    # was never checked and the resolver fell straight through to the
    # global segment defaults. Now each tier is a separate `if` that
    # runs independently; all three overrides feed into the composite
    # layer list below so the merge loop fills in per-field gaps:
    #
    #   broker.field > admin.field > super_admin.field > global.field
    #
    # This matches how operators think: "admin sets the pool baseline,
    # broker overrides where needed, user overrides on top." If broker
    # doesn't set `overnightMargin`, admin's value should apply —
    # not the global default.
    broker_pool_override = None
    admin_pool_override = None
    super_admin_pool_override = None

    if user_doc is not None:
        broker_anc = user_doc.broker_ancestry or []

        # 1. Broker pool (most specific)
        if broker_anc:
            broker_id = broker_anc[-1]
            broker_pool_override = await BrokerSegmentOverride.find_one(
                BrokerSegmentOverride.broker_id == broker_id,
                BrokerSegmentOverride.segment_name == seg_name,
            )

        # 2. Admin pool (fills in what broker doesn't set)
        if user_doc.assigned_admin_id is not None:
            admin_pool_override = await SubAdminSegmentOverride.find_one(
                SubAdminSegmentOverride.sub_admin_id == user_doc.assigned_admin_id,
                SubAdminSegmentOverride.segment_name == seg_name,
            )

        # 3. Super-admin pool (fills in what admin doesn't set)
        sa_id = await _resolve_super_admin_id()
        if sa_id is not None:
            super_admin_pool_override = await SuperAdminSegmentOverride.find_one(
                SuperAdminSegmentOverride.super_admin_id == sa_id,
                SuperAdminSegmentOverride.segment_name == seg_name,
            )

    # Walk in priority order (first-wins per field):
    #   user-symbol > user-segment > script-override >
    #   broker-pool > admin-pool > super-admin-pool > segment
    #
    # The composite merge loop (below) reads each layer in order and
    # only sets a field if no higher-priority layer already set it.
    # This means broker.overnightMargin beats admin.overnightMargin,
    # but if broker doesn't set it, admin's value flows through.
    composite_override = None
    layers = [
        user_override_symbol,
        user_override_segment,
        script_override,
        broker_pool_override,
        admin_pool_override,
        super_admin_pool_override,
    ]
    if any(layers):
        composite_override = NettingFieldsBase()
        for layer in layers:
            if layer is None:
                continue
            for f in NETTING_FIELDS:
                v = getattr(layer, f, None)
                if v is not None and getattr(composite_override, f, None) is None:
                    setattr(composite_override, f, v)

    settings_dict = _to_legacy_dict(
        seg,
        composite_override,
        action=action,
        option_type=option_type,
        product_type=product_type,
        is_expiry_day=is_expiry_day,
    )
    sources = {
        "segment": seg_name,
        "script_override": bool(script_override),
        "broker_pool_override": bool(broker_pool_override),
        "admin_pool_override": bool(admin_pool_override),
        "super_admin_pool_override": bool(super_admin_pool_override),
        "user_override": bool(user_override_symbol or user_override_segment),
    }
    payload = {"segment_type": segment_type, "settings": settings_dict, "sources": sources}
    try:
        await cache_set(cache_key, payload, ttl_sec=CACHE_TTL)
    except Exception:
        logger.warning("netting_cache_set_failed", extra={"key": cache_key})
    return payload


# ── Segment-level bracket (SL/TP) toggle — hot-path cache ─────────────
# The risk enforcer's bracket loop reads these flags for EVERY open
# position on EVERY tick (1 s). Resolving the full per-user effective
# settings there would add a Mongo/Redis round-trip per position per
# tick — exactly the kind of cost the sharding memories warn against.
# The SL/TP enable/disable knobs are edited in the admin SegmentMatrix,
# which writes the segment-level NettingSegment row, so a plain
# segment-name → (sl_enabled, tp_enabled) map is the correct granularity
# for the enforcer. Cached in-process with a short TTL; a dict lookup on
# a warm cache is effectively free.
_SEG_BRACKET_FLAGS_CACHE: dict[str, tuple[bool, bool]] = {}
_SEG_BRACKET_FLAGS_TS: float = 0.0
_SEG_BRACKET_FLAGS_TTL = 30.0


async def get_segment_bracket_flags(segment_type: str) -> tuple[bool, bool]:
    """Return ``(sl_enabled, tp_enabled)`` for an instrument ``segment``.

    Segment-level only (no per-user override) so it stays cheap enough to
    call from the risk enforcer's per-position bracket loop. Defaults to
    ``(True, True)`` for any un-seeded / unknown segment so behaviour is
    unchanged until an admin explicitly turns a leg off.
    """
    import time as _time

    global _SEG_BRACKET_FLAGS_TS
    now = _time.monotonic()
    if now - _SEG_BRACKET_FLAGS_TS >= _SEG_BRACKET_FLAGS_TTL or not _SEG_BRACKET_FLAGS_CACHE:
        try:
            fresh: dict[str, tuple[bool, bool]] = {}
            async for seg in NettingSegment.find_all():
                fresh[seg.name] = (
                    bool(getattr(seg, "slEnabled", True)),
                    bool(getattr(seg, "tpEnabled", True)),
                )
            _SEG_BRACKET_FLAGS_CACHE.clear()
            _SEG_BRACKET_FLAGS_CACHE.update(fresh)
            _SEG_BRACKET_FLAGS_TS = now
        except Exception:
            logger.warning("segment_bracket_flags_refresh_failed")
            # Fall through to whatever's cached (or the default below).
    seg_name = _SEGMENT_NAME_MAP.get(segment_type, segment_type)
    return _SEG_BRACKET_FLAGS_CACHE.get(seg_name, (True, True))


async def inherited_segment_fields(
    user_id: str | PydanticObjectId,
) -> dict[str, dict[str, Any]]:
    """For each segment, the camelCase field values a user INHERITS — i.e. the
    pool cascade BELOW their own per-user override: broker > sub-admin >
    super-admin > base segment default. Powers the admin User-Overrides UI so
    a blank cell shows the value currently in effect (what the user actually
    gets) instead of the bare word "inherit". The user's OWN override is
    deliberately excluded — the cell's own value already shows that, and the
    placeholder should show what they'd fall back to.
    """
    user_doc = await User.get(PydanticObjectId(user_id))
    broker_id: PydanticObjectId | None = None
    admin_id: PydanticObjectId | None = None
    if user_doc is not None:
        anc = user_doc.broker_ancestry or []
        if anc:
            broker_id = anc[-1]
        admin_id = user_doc.assigned_admin_id
    try:
        sa_id = await _resolve_super_admin_id()
    except Exception:
        sa_id = None

    segs = await NettingSegment.find_all().to_list()
    out: dict[str, dict[str, Any]] = {}
    for seg in segs:
        # Most-specific first; first non-null per field wins, else base seg.
        layers: list[Any] = []
        if broker_id is not None:
            layers.append(
                await BrokerSegmentOverride.find_one(
                    BrokerSegmentOverride.broker_id == broker_id,
                    BrokerSegmentOverride.segment_name == seg.name,
                )
            )
        if admin_id is not None:
            layers.append(
                await SubAdminSegmentOverride.find_one(
                    SubAdminSegmentOverride.sub_admin_id == admin_id,
                    SubAdminSegmentOverride.segment_name == seg.name,
                )
            )
        if sa_id is not None:
            layers.append(
                await SuperAdminSegmentOverride.find_one(
                    SuperAdminSegmentOverride.super_admin_id == sa_id,
                    SuperAdminSegmentOverride.segment_name == seg.name,
                )
            )
        resolved: dict[str, Any] = {}
        for f in NETTING_FIELDS:
            val = None
            for layer in layers:
                if layer is None:
                    continue
                lv = getattr(layer, f, None)
                if lv is not None:
                    val = lv
                    break
            if val is None:
                val = getattr(seg, f, None)
            resolved[f] = val
        out[seg.name] = resolved
    return out


# ── Bulk copy ──────────────────────────────────────────────────────
async def copy_user_overrides(
    *,
    source_user_id: str | PydanticObjectId,
    target_user_ids: list[str],
    overwrite: bool = True,
) -> dict[str, Any]:
    src_rows = await list_user_overrides(source_user_id)
    if not src_rows:
        return {"applied_users": 0, "applied_rows": 0, "skipped": len(target_user_ids), "reason": "Source has no overrides"}

    applied_users = 0
    applied_rows = 0
    skipped = 0
    for uid_raw in target_user_ids:
        try:
            uid = PydanticObjectId(uid_raw)
        except Exception:
            skipped += 1
            continue
        if str(uid) == str(source_user_id):
            skipped += 1
            continue
        if await User.get(uid) is None:
            skipped += 1
            continue
        touched = 0
        for src in src_rows:
            existing = await UserSegmentOverride.find_one(
                UserSegmentOverride.user_id == uid,
                UserSegmentOverride.segment_name == src.segment_name,
                UserSegmentOverride.symbol == src.symbol,
            )
            if existing is None:
                existing = UserSegmentOverride(
                    user_id=uid, segment_name=src.segment_name, symbol=src.symbol
                )
            elif not overwrite:
                if any(getattr(existing, f, None) is not None for f in NETTING_FIELDS):
                    continue
            for f in NETTING_FIELDS:
                v = getattr(src, f, None)
                if v is not None:
                    setattr(existing, f, v)
            await existing.save()
            touched += 1
        if touched > 0:
            applied_users += 1
            applied_rows += touched
            # Bust the TARGET user's caches so the copied overrides take
            # effect on their next order immediately. Without this the copy
            # only showed in the admin UI (frontend invalidates its own
            # query) while the user's live session kept resolving STALE
            # settings until the 30–300 s TTL — the "copy doesn't work" bug.
            # Mirrors the triple-wipe in upsert_user_override / delete.
            await cache_delete_pattern(f"netting_eff:{uid}:*")
            await cache_delete_pattern(f"inactive_admin_rows:{uid}")
            await cache_delete_pattern(f"blocked_syms:{uid}")
    return {"applied_users": applied_users, "applied_rows": applied_rows, "skipped": skipped, "source_rows": len(src_rows)}
