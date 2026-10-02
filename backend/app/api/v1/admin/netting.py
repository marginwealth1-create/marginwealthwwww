"""Admin Netting Segment Settings — segment matrix, scripts, per-user overrides."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import (
    CurrentAdmin,
    SuperAdmin,
    assert_user_in_scope,
    require_perm,
    scoped_user_ids,
)
from beanie import PydanticObjectId
from typing import Any
from app.models.user import User, UserRole
from app.schemas.common import APIResponse
from app.services import netting_service as svc

router = APIRouter(prefix="/netting", tags=["admin-netting"])


def _ser_segment(s) -> dict:
    return s.model_dump(exclude={"id", "revision_id"}) | {"id": str(s.id)}


def _ser_script(s) -> dict:
    return s.model_dump(
        exclude={
            "id",
            "revision_id",
            "segment_id",
            "scope_admin_id",
            "scope_broker_id",
        }
    ) | {
        "id": str(s.id),
        "segment_id": str(s.segment_id),
        # Cast scope ObjectIds to strings so the frontend gets stable
        # primitives. Null stays null → represents platform-wide rows.
        "scope_admin_id": (
            str(s.scope_admin_id) if getattr(s, "scope_admin_id", None) else None
        ),
        "scope_broker_id": (
            str(s.scope_broker_id) if getattr(s, "scope_broker_id", None) else None
        ),
    }


def _ser_user_override(s) -> dict:
    return s.model_dump(exclude={"id", "revision_id", "user_id"}) | {
        "id": str(s.id),
        "user_id": str(s.user_id),
    }


# ── Segment matrix ────────────────────────────────────────────────
def _merge_with_override(seg, override, scope: str) -> dict:
    """Returns the platform segment dict with this pool's overrides
    layered on top. Override-null fields fall back to the platform value
    so the UI shows what the pool's users actually see. `scope` is one of
    ``GLOBAL`` / ``SUPER_ADMIN`` / ``SUB_ADMIN`` / ``BROKER``."""
    base = _ser_segment(seg)
    base["scope"] = scope
    if override is None:
        return base
    over = override.model_dump(
        exclude={
            "id",
            "revision_id",
            "sub_admin_id",
            "super_admin_id",
            "broker_id",
            "segment_name",
            "created_at",
            "updated_at",
        }
    )
    for k, v in over.items():
        if v is not None:
            base[k] = v
    base["override_id"] = str(override.id)
    return base


async def _load_pool_overrides(admin) -> tuple[dict, str]:
    """Returns `(overrides_by_segment_name, scope_label)` for the caller's
    tier. Each tier owns its own override table; the resolver applies
    them independently so super-admin's edits never leak into admin /
    broker pools (and vice-versa)."""
    if admin.role == UserRole.SUPER_ADMIN:
        rows = await svc.list_super_admin_segment_overrides(admin.id)
        return {o.segment_name: o for o in rows}, "SUPER_ADMIN"
    if admin.role == UserRole.BROKER:
        rows = await svc.list_broker_segment_overrides(admin.id)
        return {o.segment_name: o for o in rows}, "BROKER"
    # ADMIN
    rows = await svc.list_sub_admin_segment_overrides(admin.id)
    return {o.segment_name: o for o in rows}, "SUB_ADMIN"


@router.get("/segments", response_model=APIResponse[list])
async def list_segments(
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "read")),
):
    rows = await svc.list_segments()
    by_name, scope = await _load_pool_overrides(admin)
    return APIResponse(
        data=[_merge_with_override(r, by_name.get(r.name), scope) for r in rows]
    )


@router.get("/segments/dump-all", response_model=APIResponse[list])
async def dump_all_segments(admin: SuperAdmin):
    """One-shot dump of every NettingSegment row's critical margin fields
    as they actually exist in MongoDB right now. Diagnostic — super-admin only.

    NOTE: must be declared BEFORE `/segments/{segment_id}` so FastAPI's
    route matcher doesn't try to parse 'dump-all' as a segment id.
    """
    from app.models.netting import NettingSegment

    rows = await NettingSegment.find_all().to_list()
    rows.sort(key=lambda r: r.name)
    return APIResponse(data=[
        {
            "name": seg.name,
            "displayName": seg.displayName,
            "marginCalcMode": seg.marginCalcMode,
            "optionBuyMarginCalcMode": getattr(seg, "optionBuyMarginCalcMode", None),
            "optionSellMarginCalcMode": getattr(seg, "optionSellMarginCalcMode", None),
            "intradayMargin": seg.intradayMargin,
            "overnightMargin": seg.overnightMargin,
            "optionBuyIntraday": seg.optionBuyIntraday,
            "optionBuyOvernight": seg.optionBuyOvernight,
            "optionSellIntraday": seg.optionSellIntraday,
            "optionSellOvernight": seg.optionSellOvernight,
            "isActive": seg.isActive,
            "tradingEnabled": seg.tradingEnabled,
        }
        for seg in rows
    ])


@router.get("/segments/{segment_id}", response_model=APIResponse[dict])
async def get_segment(
    segment_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "read")),
):
    seg = await svc.get_segment(segment_id)
    if admin.role == UserRole.SUPER_ADMIN:
        over = await svc.get_super_admin_segment_override(admin.id, seg.name)
        return APIResponse(data=_merge_with_override(seg, over, "SUPER_ADMIN"))
    if admin.role == UserRole.BROKER:
        over = await svc.get_broker_segment_override(admin.id, seg.name)
        return APIResponse(data=_merge_with_override(seg, over, "BROKER"))
    over = await svc.get_sub_admin_segment_override(admin.id, seg.name)
    return APIResponse(data=_merge_with_override(seg, over, "SUB_ADMIN"))


@router.put("/segments/{segment_id}", response_model=APIResponse[dict])
async def update_segment(
    segment_id: str,
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "write")),
):
    """Tier-isolated write — each tier has its own pool-default override
    table so changes never leak into other tiers' pools:

    - Super-admin → `SuperAdminSegmentOverride` (only super-admin's users)
    - Admin     → `SubAdminSegmentOverride`  (only admin's pool users)
    - Broker    → `BrokerSegmentOverride`    (only broker's pool users)

    Platform-wide `NettingSegment` rows are now treated as immutable seed
    defaults that everybody falls back to when their pool has no override.
    """
    patch = payload.get("patch") or {k: v for k, v in payload.items() if k != "patch"}
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="patch must be an object")

    seg = await svc.get_segment(segment_id)
    if admin.role == UserRole.SUPER_ADMIN:
        over = await svc.upsert_super_admin_segment_override(admin.id, seg.name, patch)
        scope = "SUPER_ADMIN"
    elif admin.role == UserRole.BROKER:
        over = await svc.upsert_broker_segment_override(admin.id, seg.name, patch)
        scope = "BROKER"
    else:
        over = await svc.upsert_sub_admin_segment_override(admin.id, seg.name, patch)
        scope = "SUB_ADMIN"
    return APIResponse(data=_merge_with_override(seg, over, scope))


@router.get("/diagnose", response_model=APIResponse[dict])
async def diagnose_segment(
    admin: SuperAdmin,
    segment_name: str = Query(..., description="Admin row name e.g. NSE_FUT, MCX_OPT, FOREX"),
    sample_symbol: str | None = Query(default=None, description="Symbol to test resolution (e.g. NIFTY26MAYFUT)"),
):
    """Single-screen diagnostic: shows EXACTLY what the resolver reads
    from DB for a segment, what overrides are applied, and what the
    final resolved settings look like. Compare two segments side-by-
    side (e.g. FOREX which works vs NSE_FUT which doesn't) to spot
    where the chain breaks.
    """
    from app.models.netting import (
        NettingSegment,
        NettingScriptOverride,
        UserSegmentOverride,
    )
    from app.services.netting_service import (
        _SEGMENT_NAME_MAP,
        _to_legacy_dict,
        get_effective_settings,
    )

    seg = await NettingSegment.find_one(NettingSegment.name == segment_name)
    if seg is None:
        return APIResponse(data={
            "error": f"NettingSegment with name='{segment_name}' NOT FOUND in DB",
            "hint": "This means seed_default_segments didn't run, or the admin matrix is editing a different row. The resolver will fall back to permissive defaults (intradayMargin=100, marginCalcMode='percent') for any instrument hitting this segment.",
        })

    # Raw DB dump of the segment row — what the resolver actually sees.
    seg_dump = seg.model_dump(exclude={"id", "revision_id"})
    # Highlight the few fields that drive the OrderPanel display.
    critical = {
        "marginCalcMode": seg_dump.get("marginCalcMode"),
        "optionBuyMarginCalcMode": seg_dump.get("optionBuyMarginCalcMode"),
        "optionSellMarginCalcMode": seg_dump.get("optionSellMarginCalcMode"),
        "intradayMargin": seg_dump.get("intradayMargin"),
        "overnightMargin": seg_dump.get("overnightMargin"),
        "optionBuyIntraday": seg_dump.get("optionBuyIntraday"),
        "optionSellIntraday": seg_dump.get("optionSellIntraday"),
        "isActive": seg_dump.get("isActive"),
        "tradingEnabled": seg_dump.get("tradingEnabled"),
    }

    # Script overrides scoped to this admin row.
    scripts = await NettingScriptOverride.find(
        NettingScriptOverride.segment_name == segment_name
    ).to_list()
    script_summary = [
        {
            "symbol": s.symbol,
            "marginCalcMode": getattr(s, "marginCalcMode", None),
            "intradayMargin": getattr(s, "intradayMargin", None),
        }
        for s in scripts
    ]

    # Resolve a synthetic call against this segment with no user / option
    # context — shows the segment-default path.
    sample_resolved = _to_legacy_dict(seg, None, action="BUY", product_type="MIS")
    sample_summary = {
        "margin_calc_mode": sample_resolved.get("margin_calc_mode"),
        "leverage": sample_resolved.get("leverage"),
        "margin_percentage": sample_resolved.get("margin_percentage"),
        "fixed_margin_per_lot": sample_resolved.get("fixed_margin_per_lot"),
    }

    # Show which SegmentType enum values funnel into this admin row.
    funneled_from = [
        seg_t for seg_t, admin_row in _SEGMENT_NAME_MAP.items()
        if admin_row == segment_name
    ]

    return APIResponse(data={
        "admin_row_name": segment_name,
        "db_row_found": True,
        "critical_fields_in_db": critical,
        "full_db_row": seg_dump,
        "instrument_segment_types_that_map_to_this_row": funneled_from,
        "script_overrides_count": len(scripts),
        "script_overrides_sample": script_summary[:5],
        "resolver_output_for_BUY_MIS": sample_summary,
        "_explanation": (
            "If `critical_fields_in_db.intradayMargin` is NOT what you set in the "
            "admin matrix, then the matrix Save isn't reaching this segment. "
            "If it IS what you set but `resolver_output.leverage` or "
            "`margin_percentage` looks wrong, then a script/user override is "
            "clobbering, or the resolver mode is being chosen wrong."
        ),
    })


@router.post("/segments/quick-set", response_model=APIResponse[dict])
async def quick_set_margin(
    admin: SuperAdmin,
    payload: dict,
):
    """Brute-force margin setter — bypasses the admin matrix UI entirely.

    Writes marginCalcMode + intradayMargin (and optionally overnightMargin,
    optionBuyIntraday, optionSellIntraday) directly to the NettingSegment
    row, then wipes the per-user effective-settings cache so the next
    user-side poll sees the new values immediately.

    Use this when the admin matrix Save isn't reaching the DB and you
    want to verify whether the resolver / cache / user-side panel is
    working. If the user-side panel still shows wrong values after
    calling this endpoint, the bug is in the resolver, the cache, or
    the user-side fetch — not the matrix UI.

    Payload shape::

        {
            "segment_name": "NSE_FUT" | "NSE_OPT" | "MCX_OPT" | ...,
            "mode": "times" | "fixed",
            "intraday": 700,
            "overnight": 700,        // optional
            "option_buy_intra": null, // optional, null = inherit segment
            "option_sell_intra": null // optional
        }
    """
    from app.models.netting import NettingSegment

    seg_name = (payload.get("segment_name") or "").strip().upper()
    mode = (payload.get("mode") or "").strip().lower()
    if mode not in ("fixed", "times"):
        raise HTTPException(status_code=400, detail="mode must be 'fixed' or 'times'")
    intraday = payload.get("intraday")
    if intraday is None:
        raise HTTPException(status_code=400, detail="intraday is required")
    try:
        intraday_f = float(intraday)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="intraday must be numeric")

    seg = await NettingSegment.find_one(NettingSegment.name == seg_name)
    if seg is None:
        raise HTTPException(
            status_code=404,
            detail=f"NettingSegment '{seg_name}' not found in DB. "
                   f"Valid names: {[s['name'] for s in svc.SEGMENT_DEFAULTS]}",
        )

    before = {
        "marginCalcMode": seg.marginCalcMode,
        "intradayMargin": seg.intradayMargin,
        "overnightMargin": seg.overnightMargin,
        "optionBuyIntraday": seg.optionBuyIntraday,
        "optionSellIntraday": seg.optionSellIntraday,
    }

    seg.marginCalcMode = mode
    seg.intradayMargin = intraday_f
    if payload.get("overnight") is not None:
        seg.overnightMargin = float(payload["overnight"])
    if "option_buy_intra" in payload:
        v = payload["option_buy_intra"]
        seg.optionBuyIntraday = float(v) if v is not None else None
    if "option_sell_intra" in payload:
        v = payload["option_sell_intra"]
        seg.optionSellIntraday = float(v) if v is not None else None

    await seg.save()
    # Aggressive cache wipe — both the per-user resolver cache AND the
    # segment-name keyed caches.
    await svc._wipe_eff_cache_debounced()
    try:
        from app.utils.cache import cache_delete_pattern
        await cache_delete_pattern("netting_eff:*")
        await cache_delete_pattern(f"netting:{seg_name}:*")
        await cache_delete_pattern("spread:*")
        await cache_delete_pattern("strike_far:*")
    except Exception:
        pass

    after = {
        "marginCalcMode": seg.marginCalcMode,
        "intradayMargin": seg.intradayMargin,
        "overnightMargin": seg.overnightMargin,
        "optionBuyIntraday": seg.optionBuyIntraday,
        "optionSellIntraday": seg.optionSellIntraday,
    }

    # Verify with the resolver — confirms the same row reads back the
    # expected values immediately (no Mongo replication lag).
    from app.services.netting_service import _to_legacy_dict
    verify = _to_legacy_dict(seg, None, action="BUY", product_type="MIS")

    return APIResponse(data={
        "segment_name": seg_name,
        "before": before,
        "after": after,
        "resolver_verify_for_BUY_MIS": {
            "margin_calc_mode": verify.get("margin_calc_mode"),
            "leverage": verify.get("leverage"),
            "margin_percentage": verify.get("margin_percentage"),
            "fixed_margin_per_lot": verify.get("fixed_margin_per_lot"),
        },
        "cache_wiped": True,
        "_next_step": "Reload the user-side OrderPanel (or wait ~8 s for the next effSettings poll) — it should now show the configured leverage / fixed value.",
    })


@router.post("/segments/repair-margin-mode", response_model=APIResponse[dict])
async def repair_margin_mode(admin: SuperAdmin):
    """Heal rows that got marginCalcMode='fixed' committed accidentally.

    Background: a self-heal effect in the admin matrix used to pre-stage
    `marginCalcMode = "fixed"` (the first dropdown option) on every row
    whose stored value was null/legacy. When the admin saved any field
    on such a row, that pre-staged "fixed" went into the DB even if
    they intended Times. The resolver then respected "fixed" mode and
    rendered the row as `Fixed · ₹{intradayMargin}/lot` regardless of
    the admin's actual intent.

    This endpoint resets `marginCalcMode` to NULL on rows where
    intradayMargin is still the seed default (100) — almost certainly
    means admin never actually meant Fixed. The defensive inference in
    `_to_legacy_dict` then sniffs intradayMargin on next read and picks
    the right mode (Times if > 100, Fixed otherwise).

    Idempotent. Reports per-segment counts so the admin can verify.
    """
    from app.models.netting import NettingSegment

    SEED_DEFAULT = 100.0
    rows = await NettingSegment.find_all().to_list()
    reset = []
    for seg in rows:
        # Only touch rows that smell like accidental commits:
        # mode == "fixed" but intradayMargin still at seed default →
        # admin never customised the margin number, so the mode was
        # almost certainly auto-staged not chosen.
        if (
            getattr(seg, "marginCalcMode", None) == "fixed"
            and float(getattr(seg, "intradayMargin", 0) or 0) == SEED_DEFAULT
        ):
            seg.marginCalcMode = None
            try:
                await seg.save()
                reset.append(seg.name)
            except Exception:
                pass
    # Wipe the per-user effective-settings cache so the heal takes
    # effect immediately on the user side, not after the next 5-min TTL.
    await svc._wipe_eff_cache_debounced()
    return APIResponse(data={
        "reset_count": len(reset),
        "reset_segments": reset,
        "note": (
            "After this reset, re-open the admin matrix and explicitly "
            "pick Times/Fixed + the intended Intraday value on each row "
            "you want customised. Rows you don't touch will be inferred "
            "by the backend at order time."
        ),
    })


# ── Script overrides ──────────────────────────────────────────────
def _scope_for(
    admin: User,
) -> tuple[PydanticObjectId | None, PydanticObjectId | None]:
    """Compute (scope_admin_id, scope_broker_id) for an actor.

    super-admin → (None, None) — platform-wide script override
    admin       → (admin.id, None) — applies to admin's pool
    broker      → (None, broker.id) — applies to broker's subtree

    Used by POST / PUT / DELETE to stamp + scope-check tier-specific
    overrides without giving any actor write access to other tiers'
    rows.
    """
    if admin.role == UserRole.SUPER_ADMIN:
        return None, None
    if admin.role == UserRole.BROKER:
        return None, admin.id
    return admin.id, None


def _can_edit_script(actor: User, script: Any) -> bool:
    """Return True iff `actor` may mutate this script override."""
    sad = getattr(script, "scope_admin_id", None)
    sbr = getattr(script, "scope_broker_id", None)
    if actor.role == UserRole.SUPER_ADMIN:
        return True  # super-admin can touch any row, including tier rows
    if actor.role == UserRole.BROKER:
        return sbr is not None and sbr == actor.id
    # ADMIN
    return sad is not None and sad == actor.id


@router.get("/scripts", response_model=APIResponse[list])
async def list_scripts(
    admin: CurrentAdmin,
    segment: str | None = Query(default=None),
    _: None = Depends(require_perm("segment_settings", "read")),
):
    # Super-admin sees every row (platform + every tier) so they can
    # audit / clean up. Admin / broker see their tier rows + the
    # platform fallback they inherit from (read-only on the platform
    # rows is enforced by `_can_edit_script` on PUT / DELETE).
    if admin.role == UserRole.SUPER_ADMIN:
        rows = await svc.list_scripts(segment)
    elif admin.role == UserRole.BROKER:
        rows = await svc.list_scripts(
            segment, scope_broker_id=admin.id, include_platform=True
        )
    else:
        rows = await svc.list_scripts(
            segment, scope_admin_id=admin.id, include_platform=True
        )
    return APIResponse(data=[_ser_script(r) for r in rows])


@router.post("/scripts", response_model=APIResponse[dict])
async def create_script(
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "write")),
):
    # Tier scope is derived from the caller's role — admin / broker
    # cannot create platform-wide rows (those stay super-admin only)
    # and cannot create rows in another tier's name. Resolver picks
    # the most-specific override for each user at order time.
    scope_admin_id, scope_broker_id = _scope_for(admin)
    doc = await svc.create_script(
        payload,
        scope_admin_id=scope_admin_id,
        scope_broker_id=scope_broker_id,
    )
    return APIResponse(data=_ser_script(doc))


@router.post("/scripts/bulk", response_model=APIResponse[dict])
async def create_scripts_bulk(
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "write")),
):
    """Add a per-symbol override row for every symbol in one shot — the admin
    "Select all" on the Scripts tab. Tier scope is stamped from the caller's
    role, same as the single create. Returns {created, total}."""
    scope_admin_id, scope_broker_id = _scope_for(admin)
    res = await svc.create_scripts_bulk(
        segment_id=payload.get("segment_id"),
        segment_name=payload.get("segment_name"),
        symbols=payload.get("symbols") or [],
        scope_admin_id=scope_admin_id,
        scope_broker_id=scope_broker_id,
    )
    return APIResponse(data=res)


@router.put("/scripts/{script_id}", response_model=APIResponse[dict])
async def update_script(
    script_id: str,
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "write")),
):
    existing = await svc.get_script(script_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Script override not found")
    if not _can_edit_script(admin, existing):
        raise HTTPException(
            status_code=403,
            detail="You can only edit script overrides scoped to your tier.",
        )
    patch = payload.get("patch") or {k: v for k, v in payload.items() if k != "patch"}
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="patch must be an object")
    return APIResponse(data=_ser_script(await svc.update_script(script_id, patch)))


@router.delete("/scripts/{script_id}", response_model=APIResponse[dict])
async def delete_script(
    script_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "write")),
):
    existing = await svc.get_script(script_id)
    if existing is None:
        return APIResponse(data={"ok": True})
    if not _can_edit_script(admin, existing):
        raise HTTPException(
            status_code=403,
            detail="You can only delete script overrides scoped to your tier.",
        )
    await svc.delete_script(script_id)
    return APIResponse(data={"ok": True})


# ── Per-user overrides ────────────────────────────────────────────
@router.get("/user/{user_id}", response_model=APIResponse[list])
async def list_user_overrides(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "read")),
):
    await assert_user_in_scope(admin, user_id)
    rows = await svc.list_user_overrides(user_id)
    return APIResponse(data=[_ser_user_override(r) for r in rows])


@router.get("/user/{user_id}/effective", response_model=APIResponse[dict])
async def user_inherited_settings(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "read")),
):
    """Per-segment camelCase field values this user INHERITS (the pool cascade
    BELOW their own override). Powers the User-Overrides UI so a blank cell
    shows the value currently in effect instead of the word 'inherit'."""
    await assert_user_in_scope(admin, user_id)
    data = await svc.inherited_segment_fields(user_id)
    return APIResponse(data=data)


@router.put("/user/{user_id}/{segment_name}", response_model=APIResponse[dict])
async def upsert_user_override(
    user_id: str,
    segment_name: str,
    payload: dict,
    admin: CurrentAdmin,
    symbol: str | None = Query(default=None),
    _: None = Depends(require_perm("segment_settings", "write")),
):
    await assert_user_in_scope(admin, user_id)
    patch = payload.get("patch") or {k: v for k, v in payload.items() if k not in ("patch", "symbol")}
    sym = symbol or payload.get("symbol")
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="patch must be an object")
    doc = await svc.upsert_user_override(user_id, segment_name, patch, sym)
    return APIResponse(data=_ser_user_override(doc))


@router.delete("/user/{user_id}/{segment_name}", response_model=APIResponse[dict])
async def delete_user_override(
    user_id: str,
    segment_name: str,
    admin: CurrentAdmin,
    symbol: str | None = Query(default=None),
    _: None = Depends(require_perm("segment_settings", "write")),
):
    await assert_user_in_scope(admin, user_id)
    await svc.delete_user_override(user_id, segment_name, symbol)
    return APIResponse(data={"ok": True})


@router.delete("/user/{user_id}", response_model=APIResponse[dict])
async def clear_all_user_overrides(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "write")),
):
    """Remove EVERY per-user segment / script override for `user_id`,
    snapping them back to the inherited cascade (their broker / admin /
    super-admin pool + platform defaults). Admin-flagged: "user me ek
    baar setting karne ke baad usko delete karne ka option nahi hai
    taki user wapas global settings me a jaye".

    Audit-friendly: returns the count removed so the operator can
    confirm the change in the toast.
    """
    await assert_user_in_scope(admin, user_id)
    deleted = await svc.clear_all_user_overrides(user_id)
    return APIResponse(data={"ok": True, "deleted": deleted})


@router.get("/users-with-overrides", response_model=APIResponse[list])
async def list_users_with_overrides(
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "read")),
    kind: str | None = Query(
        default=None,
        description="segment = segment-wide only (symbol null); script = per-symbol only; null = both",
    ),
):
    """Distinct users who have at least one override of the requested KIND.

    ``kind=segment`` lists only users with a SEGMENT-wide override (symbol
    null) — for the Segment · User-wise tab. ``kind=script`` lists only users
    with a per-symbol (script) override — for the Script · User-wise tab. So
    each tab shows ONLY the users relevant to it, not every overridden user.
    ``null`` = any (legacy)."""
    from app.models.netting import UserSegmentOverride
    from app.models.user import User
    from beanie import PydanticObjectId

    symbol_filter: dict = {}
    if kind == "segment":
        symbol_filter = {"symbol": None}
    elif kind == "script":
        symbol_filter = {"symbol": {"$ne": None}}

    match: dict = dict(symbol_filter)
    scope = await scoped_user_ids(admin)
    if scope is not None:
        match["user_id"] = {"$in": [PydanticObjectId(str(s)) for s in scope]}
    pipeline = [
        {"$match": match},
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
    ]
    counts: dict[str, int] = {}
    async for row in UserSegmentOverride.aggregate(pipeline):
        counts[str(row["_id"])] = int(row["count"])
    if not counts:
        return APIResponse(data=[])
    users = await User.find(
        {"_id": {"$in": [PydanticObjectId(u) for u in counts]}}
    ).to_list()
    return APIResponse(
        data=[
            {
                "id": str(u.id),
                "user_code": u.user_code,
                "full_name": u.full_name,
                "override_count": counts.get(str(u.id), 0),
            }
            for u in users
        ]
    )


# ── Diagnostic: trace why a user's order panel shows a particular margin
@router.get("/debug/resolve", response_model=APIResponse[dict])
async def debug_resolve(
    admin: SuperAdmin,
    token: str,
    user_id: str | None = Query(default=None),
    action: str = Query(default="BUY"),
    product_type: str = Query(default="NRML"),
):
    """One-shot probe: takes an instrument token and shows every value the
    netting resolver uses to compute the order panel's margin / leverage.
    Hit this when "I saved 700× but the panel still shows 100×" — the
    response makes it impossible to guess what's wrong:
      • `instrument.segment`  : what's stored on the Instrument row
      • `mapped_segment_name` : after CRYPTO_SPOT → CRYPTO_PERPETUAL mapping
      • `raw_segment_doc`     : the NettingSegment record verbatim — proves
                                whether `marginCalcMode` saved as "times"
                                and `intradayMargin` is the 700 you set
      • `resolved`            : the final dict the order panel consumes
                                (margin_percentage, leverage, etc.)
      • `_resolver_build`     : sentinel proving the running process is on
                                the times-mode-symmetric patch
    """
    from app.models.instrument import Instrument
    from app.models.netting import NettingSegment
    from app.services import netting_service as svc

    inst = await Instrument.find_one(Instrument.token == token)
    if inst is None:
        raise HTTPException(status_code=404, detail=f"Instrument {token} not found")

    seg_name = svc._SEGMENT_NAME_MAP.get(inst.segment, inst.segment)
    raw_seg = await NettingSegment.find_one(NettingSegment.name == seg_name)

    # Use the admin's own id when caller doesn't pass user_id — just so the
    # resolver has a valid ObjectId for its cache key.
    uid = user_id or str(admin.id)
    resolved = await svc.get_effective_settings(
        uid,
        inst.segment,
        action=action,
        product_type=product_type,
        symbol=inst.symbol,
    )

    return APIResponse(data={
        "_resolver_build": "times_mode_symmetric_leverage_v2",
        "instrument": {
            "token": inst.token,
            "symbol": inst.symbol,
            "segment": inst.segment,
            "instrument_type": str(inst.instrument_type),
            "lot_size": inst.lot_size,
        },
        "mapped_segment_name": seg_name,
        "raw_segment_doc": raw_seg.model_dump(exclude={"id", "revision_id"}) if raw_seg else None,
        "resolved": resolved.get("settings", resolved),
    })


# ── Bulk copy ────────────────────────────────────────────────────
@router.post("/copy", response_model=APIResponse[dict])
async def copy(
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "write")),
):
    src = payload.get("source_user_id")
    targets = payload.get("target_user_ids") or []
    overwrite = bool(payload.get("overwrite", True))
    if not src:
        raise HTTPException(status_code=400, detail="source_user_id required")
    if not isinstance(targets, list) or not targets:
        raise HTTPException(status_code=400, detail="target_user_ids must be a non-empty list")
    # Sub-admin can only copy within their own pool.
    await assert_user_in_scope(admin, src)
    for t in targets:
        await assert_user_in_scope(admin, t)
    return APIResponse(data=await svc.copy_user_overrides(
        source_user_id=src, target_user_ids=targets, overwrite=overwrite,
    ))
