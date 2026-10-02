"""Settings snapshot on tier-create.

User policy (admin spec, 21-May):

    Super-admin global settings → when a new ADMIN is created, that
    admin's tier-tables should be seeded with the super-admin's
    current effective settings. The admin can then freely edit; their
    edits NEVER bubble back up to super-admin, and super-admin's
    later edits do NOT cascade down to existing admins.

    Same rule applies for ADMIN → BROKER and BROKER → SUB-BROKER:
    each new tier inherits a SNAPSHOT of the creator's effective
    settings, materialised into the new tier's own override tables.

This is intentionally NOT the live-cascade model that
`netting_service.get_effective_settings()` runs at trade time. The
trade-time cascade still works for end users (clients) — but the
ADMIN UI for each tier reads from that tier's own table directly, so
without an initial snapshot a freshly-created admin / broker sees
blank fields and has no way to understand the platform's baseline.

Public helpers:

    snapshot_for_new_admin(new_admin_id, *, source_super_admin_id)
        Materialise SuperAdminSegmentOverride + SuperAdminRiskSettings
        into SubAdminSegmentOverride + SubAdminRiskSettings rows for
        the new admin. Idempotent — re-runs are no-ops because we use
        upsert semantics on the unique (sub_admin_id, segment_name)
        pair.

    snapshot_for_new_broker(new_broker_id, *, creator)
        Materialise the creator's effective segment + risk settings
        into BrokerSegmentOverride + BrokerRiskSettings rows for the
        new broker. The creator may be SUPER_ADMIN, ADMIN, or BROKER;
        the function resolves which source-tier rows to read from.

    backfill_missing_snapshots()
        One-shot migration helper. Walks every existing ADMIN and
        BROKER user, runs the appropriate snapshot if and only if
        their tier-tables are empty for at least one segment.
        Designed for the post-deploy boot — idempotent, cheap on
        wallets already populated.
"""

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId

from app.models.netting import (
    SEGMENT_CODES,
    BrokerRiskSettings,
    BrokerSegmentOverride,
    NettingFieldsBase,
    NettingSegment,
    RiskSettings,
    RiskSettingsBase,
    SubAdminRiskSettings,
    SubAdminSegmentOverride,
    SuperAdminRiskSettings,
    SuperAdminSegmentOverride,
)
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)


# ── Field lists (used for shallow copy + null-coalesce) ─────────────
_SEGMENT_FIELDS = list(NettingFieldsBase.model_fields.keys())
_RISK_FIELDS = list(RiskSettingsBase.model_fields.keys())


# ── Resolvers ────────────────────────────────────────────────────────
async def _segment_seed_dict(segment_name: str) -> dict[str, Any]:
    """Platform seed values for a segment, dumped as a dict that maps
    directly onto NettingFieldsBase. Source of last resort when a tier
    has no override of its own.

    NB: ``NettingSegment``'s key column is ``name`` (e.g. "NSE_EQ") —
    NOT ``segment_name`` like the per-tier override tables. Using the
    wrong field raises ``AttributeError: segment_name`` at query
    construction because Beanie probes the model class for the
    attribute before issuing the find.
    """
    seg = await NettingSegment.find_one(NettingSegment.name == segment_name)
    if seg is None:
        return {}
    # Only carry fields that are part of the overlay shape — drops
    # `name`, `displayName`, timestamps, etc.
    full = seg.model_dump()
    return {k: full.get(k) for k in _SEGMENT_FIELDS}


async def _resolve_effective_segment(
    *,
    source_user: User,
    segment_name: str,
) -> dict[str, Any]:
    """Walk the cascade for the source tier and produce a complete
    effective settings dict (no None values where the global seed has
    a value). The returned dict is suitable for materialising as the
    next tier's override row.
    """
    seed = await _segment_seed_dict(segment_name)
    override: dict[str, Any] | None = None

    if source_user.role == UserRole.SUPER_ADMIN:
        row = await SuperAdminSegmentOverride.find_one(
            SuperAdminSegmentOverride.super_admin_id == source_user.id,
            SuperAdminSegmentOverride.segment_name == segment_name,
        )
        if row is not None:
            dumped = row.model_dump()
            override = {k: dumped.get(k) for k in _SEGMENT_FIELDS}
    elif source_user.role == UserRole.ADMIN:
        row = await SubAdminSegmentOverride.find_one(
            SubAdminSegmentOverride.sub_admin_id == source_user.id,
            SubAdminSegmentOverride.segment_name == segment_name,
        )
        if row is not None:
            dumped = row.model_dump()
            override = {k: dumped.get(k) for k in _SEGMENT_FIELDS}
    elif source_user.role == UserRole.BROKER:
        row = await BrokerSegmentOverride.find_one(
            BrokerSegmentOverride.broker_id == source_user.id,
            BrokerSegmentOverride.segment_name == segment_name,
        )
        if row is not None:
            dumped = row.model_dump()
            override = {k: dumped.get(k) for k in _SEGMENT_FIELDS}

    # Merge: override wins where set, fall back to seed otherwise.
    merged: dict[str, Any] = dict(seed)
    if override is not None:
        for k, v in override.items():
            if v is not None:
                merged[k] = v
    return merged


async def _resolve_effective_risk(*, source_user: User) -> dict[str, Any]:
    """Cascade-resolve risk for the source tier, returning a dict that
    maps onto RiskSettingsBase."""
    global_row = await RiskSettings.find_one()
    seed: dict[str, Any] = {}
    if global_row is not None:
        dumped = global_row.model_dump()
        seed = {k: dumped.get(k) for k in _RISK_FIELDS}

    override: dict[str, Any] | None = None
    if source_user.role == UserRole.SUPER_ADMIN:
        row = await SuperAdminRiskSettings.find_one(
            SuperAdminRiskSettings.super_admin_id == source_user.id
        )
    elif source_user.role == UserRole.ADMIN:
        row = await SubAdminRiskSettings.find_one(
            SubAdminRiskSettings.sub_admin_id == source_user.id
        )
    elif source_user.role == UserRole.BROKER:
        row = await BrokerRiskSettings.find_one(
            BrokerRiskSettings.broker_id == source_user.id
        )
    else:
        row = None

    if row is not None:
        dumped = row.model_dump()
        override = {k: dumped.get(k) for k in _RISK_FIELDS}

    merged: dict[str, Any] = dict(seed)
    if override is not None:
        for k, v in override.items():
            if v is not None:
                merged[k] = v
    return merged


# ── Writers ──────────────────────────────────────────────────────────
async def _upsert_sub_admin_segment(
    sub_admin_id: PydanticObjectId,
    segment_name: str,
    fields: dict[str, Any],
) -> None:
    existing = await SubAdminSegmentOverride.find_one(
        SubAdminSegmentOverride.sub_admin_id == sub_admin_id,
        SubAdminSegmentOverride.segment_name == segment_name,
    )
    if existing is not None:
        return  # idempotent — never overwrite an admin's already-saved row
    row = SubAdminSegmentOverride(
        sub_admin_id=sub_admin_id,
        segment_name=segment_name,
        **{k: fields.get(k) for k in _SEGMENT_FIELDS},
    )
    await row.insert()


async def _upsert_broker_segment(
    broker_id: PydanticObjectId,
    segment_name: str,
    fields: dict[str, Any],
) -> None:
    existing = await BrokerSegmentOverride.find_one(
        BrokerSegmentOverride.broker_id == broker_id,
        BrokerSegmentOverride.segment_name == segment_name,
    )
    if existing is not None:
        return
    row = BrokerSegmentOverride(
        broker_id=broker_id,
        segment_name=segment_name,
        **{k: fields.get(k) for k in _SEGMENT_FIELDS},
    )
    await row.insert()


async def _upsert_sub_admin_risk(
    sub_admin_id: PydanticObjectId,
    fields: dict[str, Any],
) -> None:
    existing = await SubAdminRiskSettings.find_one(
        SubAdminRiskSettings.sub_admin_id == sub_admin_id
    )
    if existing is not None:
        return
    row = SubAdminRiskSettings(
        sub_admin_id=sub_admin_id,
        **{k: fields.get(k) for k in _RISK_FIELDS},
    )
    await row.insert()


async def _upsert_broker_risk(
    broker_id: PydanticObjectId,
    fields: dict[str, Any],
) -> None:
    existing = await BrokerRiskSettings.find_one(
        BrokerRiskSettings.broker_id == broker_id
    )
    if existing is not None:
        return
    row = BrokerRiskSettings(
        broker_id=broker_id,
        **{k: fields.get(k) for k in _RISK_FIELDS},
    )
    await row.insert()


# ── Public API ───────────────────────────────────────────────────────
async def snapshot_for_new_admin(
    new_admin_id: PydanticObjectId,
    *,
    source_super_admin_id: PydanticObjectId | None = None,
) -> dict[str, int]:
    """Seed a freshly-created ADMIN with a snapshot of super-admin's
    current effective settings. If no super-admin is found we still
    seed from `NettingSegment` global so the admin has values to edit.
    """
    source: User | None = None
    if source_super_admin_id is not None:
        source = await User.get(source_super_admin_id)
    if source is None or source.role != UserRole.SUPER_ADMIN:
        # Fall back to the first super-admin in the system. Most installs
        # have exactly one — but we don't want to silently skip the seed
        # just because the creator forgot to pass an explicit id.
        source = await User.find_one(User.role == UserRole.SUPER_ADMIN)

    if source is None:
        # No super-admin at all — seed straight from NettingSegment so
        # the admin still gets a populated set of rows to edit.
        return await _snapshot_from_seed_only_admin(new_admin_id)

    segs_written = 0
    for seg in SEGMENT_CODES:
        try:
            fields = await _resolve_effective_segment(source_user=source, segment_name=seg)
            await _upsert_sub_admin_segment(new_admin_id, seg, fields)
            segs_written += 1
        except Exception:
            logger.exception(
                "snapshot_admin_segment_failed segment=%s admin=%s",
                seg,
                new_admin_id,
            )

    risk_written = 0
    try:
        risk_fields = await _resolve_effective_risk(source_user=source)
        await _upsert_sub_admin_risk(new_admin_id, risk_fields)
        risk_written = 1
    except Exception:
        logger.exception("snapshot_admin_risk_failed admin=%s", new_admin_id)

    logger.info(
        "settings_snapshot_admin admin=%s source_super_admin=%s segments=%d risk=%d",
        new_admin_id,
        source.id,
        segs_written,
        risk_written,
    )
    return {"segments": segs_written, "risk": risk_written}


async def _snapshot_from_seed_only_admin(
    new_admin_id: PydanticObjectId,
) -> dict[str, int]:
    """Fallback: no super-admin found, seed admin from NettingSegment."""
    segs_written = 0
    for seg in SEGMENT_CODES:
        try:
            fields = await _segment_seed_dict(seg)
            if fields:
                await _upsert_sub_admin_segment(new_admin_id, seg, fields)
                segs_written += 1
        except Exception:
            logger.exception(
                "snapshot_admin_seed_segment_failed segment=%s admin=%s",
                seg,
                new_admin_id,
            )
    # Risk: copy from global RiskSettings if present.
    try:
        risk_global = await RiskSettings.find_one()
        if risk_global is not None:
            dumped = risk_global.model_dump()
            risk_fields = {k: dumped.get(k) for k in _RISK_FIELDS}
            await _upsert_sub_admin_risk(new_admin_id, risk_fields)
    except Exception:
        logger.exception("snapshot_admin_seed_risk_failed admin=%s", new_admin_id)
    return {"segments": segs_written, "risk": 1}


async def snapshot_for_new_broker(
    new_broker_id: PydanticObjectId,
    *,
    creator: User,
) -> dict[str, int]:
    """Seed a freshly-created BROKER (or sub-broker) with a snapshot of
    the creator's effective settings.

      • creator role SUPER_ADMIN → snapshot from super-admin's pool
      • creator role ADMIN       → snapshot from admin's pool
      • creator role BROKER      → snapshot from creator broker's pool
    """
    segs_written = 0
    for seg in SEGMENT_CODES:
        try:
            fields = await _resolve_effective_segment(source_user=creator, segment_name=seg)
            await _upsert_broker_segment(new_broker_id, seg, fields)
            segs_written += 1
        except Exception:
            logger.exception(
                "snapshot_broker_segment_failed segment=%s broker=%s creator=%s",
                seg,
                new_broker_id,
                creator.id,
            )

    risk_written = 0
    try:
        risk_fields = await _resolve_effective_risk(source_user=creator)
        await _upsert_broker_risk(new_broker_id, risk_fields)
        risk_written = 1
    except Exception:
        logger.exception(
            "snapshot_broker_risk_failed broker=%s creator=%s",
            new_broker_id,
            creator.id,
        )

    logger.info(
        "settings_snapshot_broker broker=%s creator=%s creator_role=%s segments=%d risk=%d",
        new_broker_id,
        creator.id,
        creator.role.value,
        segs_written,
        risk_written,
    )
    return {"segments": segs_written, "risk": risk_written}


async def backfill_missing_snapshots() -> dict[str, int]:
    """Boot-time migration. Walks every ADMIN and BROKER user and runs
    the snapshot helper if their tier-tables don't yet cover all 13
    segments. Cheap no-op on tiers that already have the full set
    (the per-row upserts skip existing rows).

    The previous version checked "find_one returns something → skip"
    which left partial-fill rows behind whenever a single segment
    failed during the original snapshot. Counting rows against the
    expected total catches that — re-running the snapshot will fill
    in the missing segments without touching the existing ones.

    Why this exists: when this feature shipped, every existing
    admin / broker had a blank settings page because their tier
    tables were never populated. The backfill brings them in line
    with the new copy-on-create behaviour without forcing the
    operator to recreate each account. The per-segment count check
    also recovers from any boot where the snapshot crashed
    mid-segment (e.g. the 21-May `AttributeError: segment_name`
    bug, which left several admins at 0–3 segments before the fix).
    """
    expected = len(SEGMENT_CODES)
    admins = await User.find(User.role == UserRole.ADMIN).to_list()
    brokers = await User.find(User.role == UserRole.BROKER).to_list()

    admin_filled = 0
    for a in admins:
        try:
            have = await SubAdminSegmentOverride.find(
                SubAdminSegmentOverride.sub_admin_id == a.id
            ).count()
            if have >= expected:
                continue
            await snapshot_for_new_admin(a.id)
            admin_filled += 1
        except Exception:
            logger.exception("backfill_admin_failed admin=%s", a.id)

    broker_filled = 0
    for b in brokers:
        try:
            have = await BrokerSegmentOverride.find(
                BrokerSegmentOverride.broker_id == b.id
            ).count()
            if have >= expected:
                continue
            # Resolve the creator from the broker's ancestry / assignment.
            creator: User | None = None
            if b.broker_ancestry:
                # Immediate parent broker = last in ancestry
                creator = await User.get(b.broker_ancestry[-1])
            if creator is None and b.assigned_admin_id is not None:
                creator = await User.get(b.assigned_admin_id)
            if creator is None:
                # Fall back to the first super-admin so we never skip
                # an orphaned broker entirely.
                creator = await User.find_one(User.role == UserRole.SUPER_ADMIN)
            if creator is None:
                continue
            await snapshot_for_new_broker(b.id, creator=creator)
            broker_filled += 1
        except Exception:
            logger.exception("backfill_broker_failed broker=%s", b.id)

    if admin_filled or broker_filled:
        logger.info(
            "settings_snapshot_backfill admins_filled=%d brokers_filled=%d",
            admin_filled,
            broker_filled,
        )
    return {"admins_filled": admin_filled, "brokers_filled": broker_filled}


async def repair_null_seed_rows() -> dict[str, int]:
    """One-shot repair for rows written by the buggy 21-May boot where
    ``_segment_seed_dict`` returned an empty dict (the
    ``NettingSegment.segment_name`` → ``name`` bug). Those rows have
    ``intradayMargin`` AND ``isActive`` BOTH null — neither value
    survives a properly resolved snapshot, so this combination is a
    reliable fingerprint.

    Walks both SubAdminSegmentOverride and BrokerSegmentOverride,
    deletes rows matching the fingerprint, and lets the next
    ``backfill_missing_snapshots`` run regenerate them from the live
    cascade. Safe: any tier that had a real admin-set override stays
    untouched because at least one of those two fields will be
    populated.
    """
    coll_admin = SubAdminSegmentOverride.get_motor_collection()
    coll_broker = BrokerSegmentOverride.get_motor_collection()
    bad_filter = {"intradayMargin": None, "isActive": None}
    admin_deleted = (await coll_admin.delete_many(bad_filter)).deleted_count
    broker_deleted = (await coll_broker.delete_many(bad_filter)).deleted_count
    if admin_deleted or broker_deleted:
        logger.info(
            "settings_snapshot_repaired_null_seed admin=%d broker=%d",
            admin_deleted,
            broker_deleted,
        )
    return {
        "admin_deleted": admin_deleted,
        "broker_deleted": broker_deleted,
    }
