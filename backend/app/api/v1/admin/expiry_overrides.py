"""Admin CRUD for ExpiryOverride (per-actor option-chain settings)."""

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import CurrentAdmin, require_perm
from app.models.audit_log import AuditAction
from app.models.expiry_override import ExpiryOverride, ExpiryOverrideActor
from app.models.user import User
from app.schemas.common import APIResponse
from app.services.audit_service import log_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/expiry-overrides", tags=["admin-expiry-overrides"])
_VALID_ACTOR_KINDS = {k.value for k in ExpiryOverrideActor}


def _parse_actor(actor_kind: str, actor_id: str) -> tuple[ExpiryOverrideActor, PydanticObjectId]:
    kind_upper = (actor_kind or "").upper()
    if kind_upper not in _VALID_ACTOR_KINDS:
        raise HTTPException(400, detail=f"actor_kind must be one of {sorted(_VALID_ACTOR_KINDS)}")
    try:
        oid = PydanticObjectId(actor_id)
    except Exception as e:
        raise HTTPException(400, detail="Invalid actor_id") from e
    return ExpiryOverrideActor(kind_upper), oid


def _assert_self_or_super(admin: User, oid: PydanticObjectId) -> None:
    """A non-super admin may only manage THEIR OWN override row — each
    admin / broker configures expiry settings for their own pool, never
    for another tier. Super-admin may target anyone (platform-wide tooling).
    """
    role = str(getattr(admin.role, "value", admin.role) or "").upper()
    if role != "SUPER_ADMIN" and str(admin.id) != str(oid):
        raise HTTPException(
            403, detail="You can only manage expiry settings for your own account"
        )


def _serialize(o: ExpiryOverride) -> dict[str, Any]:
    return {
        "id": str(o.id),
        "actor_kind": o.actor_kind.value if hasattr(o.actor_kind, "value") else str(o.actor_kind),
        "actor_id": str(o.actor_id),
        "underlyings": o.underlyings,
        "max_expiries_fallback": o.max_expiries_fallback,
        "max_expiries_by_exchange": getattr(o, "max_expiries_by_exchange", None),
        "strikes_around_atm_by_segment": getattr(o, "strikes_around_atm_by_segment", None),
        "created_at": o.created_at.isoformat() if getattr(o, "created_at", None) else None,
        "updated_at": o.updated_at.isoformat() if getattr(o, "updated_at", None) else None,
    }


@router.get("/{actor_kind}/{actor_id}", response_model=APIResponse[dict])
async def get_override(
    actor_kind: str,
    actor_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "read")),
):
    kind, oid = _parse_actor(actor_kind, actor_id)
    _assert_self_or_super(admin, oid)
    row = await ExpiryOverride.find_one(
        ExpiryOverride.actor_kind == kind, ExpiryOverride.actor_id == oid
    )
    if row is None:
        return APIResponse(data={
            "id": None, "actor_kind": kind.value, "actor_id": str(oid),
            "underlyings": None, "max_expiries_fallback": None,
            "max_expiries_by_exchange": None,
            "strikes_around_atm_by_segment": None, "exists": False,
        })
    payload = _serialize(row)
    payload["exists"] = True
    return APIResponse(data=payload)


@router.put("/{actor_kind}/{actor_id}", response_model=APIResponse[dict])
async def upsert_override(
    actor_kind: str,
    actor_id: str,
    payload: dict[str, Any],
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "write")),
):
    kind, oid = _parse_actor(actor_kind, actor_id)
    _assert_self_or_super(admin, oid)

    target = await User.get(oid)
    if target is None:
        raise HTTPException(404, detail="Actor user not found")
    role = str(getattr(target.role, "value", target.role) or "").upper()
    if kind == ExpiryOverrideActor.USER and role not in ("CLIENT", "DEALER", "MASTER", "USER"):
        raise HTTPException(400, detail=f"actor_id role is {role}, expected an end-user")
    if kind == ExpiryOverrideActor.BROKER and role != "BROKER":
        raise HTTPException(400, detail=f"actor_id role is {role}, expected BROKER")
    if kind == ExpiryOverrideActor.ADMIN and role not in ("ADMIN", "SUPER_ADMIN"):
        raise HTTPException(400, detail=f"actor_id role is {role}, expected ADMIN / SUPER_ADMIN")

    # Normalize underlyings
    underlyings_raw = payload.get("underlyings")
    underlyings: list[dict[str, Any]] | None
    if underlyings_raw is None:
        underlyings = None
    elif isinstance(underlyings_raw, list):
        cleaned: list[dict[str, Any]] = []
        for u in underlyings_raw:
            if not isinstance(u, dict):
                continue
            sym = str(u.get("symbol") or "").strip().upper()
            if not sym:
                continue
            label = str(u.get("label") or sym).strip() or sym
            color = str(u.get("color") or "emerald").strip() or "emerald"
            raw_max = u.get("max_expiries")
            try:
                max_exp = int(raw_max) if raw_max not in (None, "", 0) else None
            except (TypeError, ValueError):
                max_exp = None
            cleaned.append({"label": label, "symbol": sym, "color": color, "max_expiries": max_exp})
        underlyings = cleaned
    else:
        raise HTTPException(400, detail="underlyings must be a list or null")

    # Normalize max_expiries_fallback (legacy single)
    max_fb_raw = payload.get("max_expiries_fallback")
    if max_fb_raw is None or max_fb_raw == "":
        max_fb = None
    else:
        try:
            max_fb = int(max_fb_raw)
        except (TypeError, ValueError):
            raise HTTPException(400, detail="max_expiries_fallback must be an integer or null")

    # Normalize max_expiries_by_exchange {NSE/BSE/MCX: int}. Drop blank/0
    # values; an empty map collapses to None (= inherit from parent tier).
    mbe_raw = payload.get("max_expiries_by_exchange")
    max_by_ex: dict[str, int] | None
    if mbe_raw is None:
        max_by_ex = None
    elif isinstance(mbe_raw, dict):
        cleaned_mbe: dict[str, int] = {}
        for k in ("NSE", "BSE", "MCX"):
            v = mbe_raw.get(k)
            if v in (None, "", 0):
                continue
            try:
                cleaned_mbe[k] = int(v)
            except (TypeError, ValueError):
                continue
        max_by_ex = cleaned_mbe or None
    else:
        raise HTTPException(400, detail="max_expiries_by_exchange must be an object or null")

    # Normalize strikes_around_atm_by_segment
    # {NSE_IDX_OPT/NSE_STK_OPT/MCX_OPT/BSE_OPT: int}. Drop blank/0; clamp
    # 1..100; an empty map collapses to None (= inherit from the parent tier).
    sbs_raw = payload.get("strikes_around_atm_by_segment")
    strikes_by_seg: dict[str, int] | None
    if sbs_raw is None:
        strikes_by_seg = None
    elif isinstance(sbs_raw, dict):
        cleaned_sbs: dict[str, int] = {}
        for k in ("NSE_IDX_OPT", "NSE_STK_OPT", "MCX_OPT", "BSE_OPT"):
            v = sbs_raw.get(k)
            if v in (None, "", 0):
                continue
            try:
                cleaned_sbs[k] = max(1, min(100, int(v)))
            except (TypeError, ValueError):
                continue
        strikes_by_seg = cleaned_sbs or None
    else:
        raise HTTPException(400, detail="strikes_around_atm_by_segment must be an object or null")

    row = await ExpiryOverride.find_one(
        ExpiryOverride.actor_kind == kind, ExpiryOverride.actor_id == oid
    )
    if row is None:
        row = ExpiryOverride(
            actor_kind=kind, actor_id=oid,
            underlyings=underlyings, max_expiries_fallback=max_fb,
            max_expiries_by_exchange=max_by_ex,
            strikes_around_atm_by_segment=strikes_by_seg,
        )
        await row.insert()
    else:
        row.underlyings = underlyings
        row.max_expiries_fallback = max_fb
        row.max_expiries_by_exchange = max_by_ex
        row.strikes_around_atm_by_segment = strikes_by_seg
        await row.save()

    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="ExpiryOverride",
        entity_id=row.id,
        actor_id=admin.id,
        target_user_id=oid,
        metadata={
            "actor_kind": kind.value,
            "underlyings": underlyings,
            "max_expiries_fallback": max_fb,
            "max_expiries_by_exchange": max_by_ex,
            "strikes_around_atm_by_segment": strikes_by_seg,
        },
    )

    try:
        from app.api.v1.user.option_chain import invalidate_settings_cache
        invalidate_settings_cache(None)
    except Exception:
        pass

    return APIResponse(data=_serialize(row))


@router.delete("/{actor_kind}/{actor_id}", response_model=APIResponse[dict])
async def delete_override(
    actor_kind: str,
    actor_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "write")),
):
    kind, oid = _parse_actor(actor_kind, actor_id)
    _assert_self_or_super(admin, oid)
    row = await ExpiryOverride.find_one(
        ExpiryOverride.actor_kind == kind, ExpiryOverride.actor_id == oid
    )
    if row is None:
        return APIResponse(data={"ok": True, "existed": False})
    await row.delete()
    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="ExpiryOverride",
        entity_id=oid,
        actor_id=admin.id,
        target_user_id=oid,
        metadata={"deleted": True, "actor_kind": kind.value},
    )
    try:
        from app.api.v1.user.option_chain import invalidate_settings_cache
        invalidate_settings_cache(None)
    except Exception:
        pass
    return APIResponse(data={"ok": True, "existed": True})


@router.get("/effective/{user_id}", response_model=APIResponse[dict])
async def effective_for_user(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("segment_settings", "read")),
):
    from app.api.v1.user.option_chain import _resolve_expiry_settings_for_user
    try:
        oid = PydanticObjectId(user_id)
    except Exception as e:
        raise HTTPException(400, detail="Invalid user_id") from e
    resolved = await _resolve_expiry_settings_for_user(oid)
    return APIResponse(data=resolved)
