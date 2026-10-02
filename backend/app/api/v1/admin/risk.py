"""Admin Risk Management — global default + per-user overrides."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import (
    CurrentAdmin,
    assert_user_in_scope,
    require_perm,
    scoped_user_ids,
)
from app.models.user import UserRole
from app.schemas.common import APIResponse
from app.services import netting_service as svc

router = APIRouter(prefix="/risk", tags=["admin-risk"])


def _merge_risk(glob, own, scope: str) -> dict:
    """Returns the platform-default risk dict with this pool's overrides
    layered on top. `scope` is one of GLOBAL / SUPER_ADMIN / SUB_ADMIN / BROKER."""
    merged: dict = glob.model_dump(exclude={"id", "revision_id"})
    if own is not None:
        for f in svc.RISK_FIELDS:
            v = getattr(own, f, None)
            if v is not None:
                merged[f] = v
        merged["id"] = str(own.id)
    else:
        merged["id"] = None
    merged["scope"] = scope
    return merged


@router.get("/global", response_model=APIResponse[dict])
async def get_global(
    admin: CurrentAdmin,
    _: None = Depends(require_perm("risk", "read")),
):
    """Role-aware read. Each tier sees their own pool's risk knobs (or the
    platform fallback) — never another tier's settings.
    """
    glob = await svc.get_global_risk()
    if admin.role == UserRole.SUPER_ADMIN:
        own = await svc.get_super_admin_risk(admin.id)
        return APIResponse(data=_merge_risk(glob, own, "SUPER_ADMIN"))
    if admin.role == UserRole.BROKER:
        own = await svc.get_broker_risk(admin.id)
        return APIResponse(data=_merge_risk(glob, own, "BROKER"))
    own = await svc.get_sub_admin_risk(admin.id)
    return APIResponse(data=_merge_risk(glob, own, "SUB_ADMIN"))


@router.put("/global", response_model=APIResponse[dict])
async def update_global(
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("risk", "write")),
):
    """Tier-isolated write — each tier has its own pool-default override
    table so changes never leak into other tiers' pools:

    - Super-admin → `SuperAdminRiskSettings`
    - Admin     → `SubAdminRiskSettings`
    - Broker    → `BrokerRiskSettings`

    Platform-wide `RiskSettings` is treated as immutable seed defaults
    (touched only at boot by the seed script).
    """
    patch = payload.get("patch") or {k: v for k, v in payload.items() if k != "patch"}
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="patch must be an object")

    glob = await svc.get_global_risk()
    if admin.role == UserRole.SUPER_ADMIN:
        doc = await svc.upsert_super_admin_risk(admin.id, patch)
        return APIResponse(data=_merge_risk(glob, doc, "SUPER_ADMIN"))
    if admin.role == UserRole.BROKER:
        doc = await svc.upsert_broker_risk(admin.id, patch)
        return APIResponse(data=_merge_risk(glob, doc, "BROKER"))
    doc = await svc.upsert_sub_admin_risk(admin.id, patch)
    return APIResponse(data=_merge_risk(glob, doc, "SUB_ADMIN"))


@router.get("/user/{user_id}", response_model=APIResponse[dict])
async def get_user(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("risk", "read")),
):
    await assert_user_in_scope(admin, user_id)
    doc = await svc.get_user_risk(user_id)
    glob = await svc.get_global_risk()
    # `global_settings` powers the "inherits XXX" hint on the per-user
    # override form. The user's real inherit value is what they'd get if
    # the per-user override row were deleted — i.e. the GLOBAL → POOL
    # merge (broker / sub-admin / super-admin), NOT just platform GLOBAL.
    # Without this resolution a sub-admin whose pool default raised
    # `stopOutPercent` to 50 would see the hint say "inherits 0" (the
    # untouched platform seed), and the admin would assume the field is
    # off when it's actually 50 in effect.
    #
    # `get_effective_risk` merges GLOBAL → POOL → USER. For any field where
    # the user has no override the effective value IS the pool/global
    # fallback (exactly what the hint wants). Where the user DOES override,
    # we fall back to platform global — close enough for the hint without
    # adding a separate pool-only resolver.
    effective = await svc.get_effective_risk(user_id)
    sources = effective.get("sources") or {}
    settings = effective.get("settings") or {}
    inherit_payload = glob.model_dump(exclude={"id", "revision_id"}) | {"id": str(glob.id)}
    for f in svc.RISK_FIELDS:
        if sources.get(f) != "USER" and f in settings:
            inherit_payload[f] = settings[f]
    return APIResponse(
        data={
            "user_settings": (doc.model_dump(exclude={"id", "revision_id", "user_id"}) | {"id": str(doc.id), "user_id": str(doc.user_id)}) if doc else None,
            "global_settings": inherit_payload,
        }
    )


@router.put("/user/{user_id}", response_model=APIResponse[dict])
async def upsert_user(
    user_id: str,
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("risk", "write")),
):
    await assert_user_in_scope(admin, user_id)
    patch = payload.get("patch") or {k: v for k, v in payload.items() if k != "patch"}
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="patch must be an object")
    doc = await svc.upsert_user_risk(user_id, patch)
    return APIResponse(data=doc.model_dump(exclude={"id", "revision_id", "user_id"}) | {"id": str(doc.id), "user_id": str(doc.user_id)})


@router.delete("/user/{user_id}", response_model=APIResponse[dict])
async def delete_user(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("risk", "write")),
):
    await assert_user_in_scope(admin, user_id)
    await svc.delete_user_risk(user_id)
    return APIResponse(data={"ok": True})


@router.post("/user/{user_id}/copy-from/{source_user_id}", response_model=APIResponse[dict])
async def copy_from(
    user_id: str,
    source_user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("risk", "write")),
):
    """Clone source user's risk override onto this user. If source has no
    override (inherits global), the destination's override is removed and
    we return `{user_settings: None}` so the frontend knows the user is
    now back to inheriting."""
    await assert_user_in_scope(admin, user_id)
    await assert_user_in_scope(admin, source_user_id)
    try:
        doc = await svc.copy_user_risk(source_user_id, user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # `copy_user_risk` returns an UNSAVED placeholder when the source had
    # no override (it deletes the destination row to revert to inherit).
    # That placeholder has `doc.id is None` — detect and return null so
    # the frontend doesn't render an empty override card.
    if doc.id is None:
        return APIResponse(data={"user_settings": None})
    return APIResponse(
        data=doc.model_dump(exclude={"id", "revision_id", "user_id"})
        | {"id": str(doc.id), "user_id": str(doc.user_id)}
    )


@router.get("/user/{user_id}/effective", response_model=APIResponse[dict])
async def get_effective(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("risk", "read")),
):
    await assert_user_in_scope(admin, user_id)
    return APIResponse(data=await svc.get_effective_risk(user_id))


@router.get("/users-with-overrides", response_model=APIResponse[list])
async def list_users_with_overrides(
    admin: CurrentAdmin,
    _: None = Depends(require_perm("risk", "read")),
):
    """Distinct users with a UserRiskSettings override doc, plus a count of
    how many of the 5 fields they actually customised. Powers the quick-pick
    list on the admin Risk Management page so admins can see at a glance who
    has custom risk rules without having to search."""
    from app.models.netting import UserRiskSettings
    from app.models.user import User
    from app.services.netting_service import RISK_FIELDS

    scope = await scoped_user_ids(admin)
    if scope is not None:
        if not scope:
            return APIResponse(data=[])
        docs = await UserRiskSettings.find({"user_id": {"$in": scope}}).to_list()
    else:
        docs = await UserRiskSettings.find_all().to_list()
    if not docs:
        return APIResponse(data=[])

    by_uid = {str(d.user_id): d for d in docs}
    users = await User.find({"_id": {"$in": [d.user_id for d in docs]}}).to_list()

    out = []
    for u in users:
        d = by_uid.get(str(u.id))
        if d is None:
            continue
        overridden = sum(1 for f in RISK_FIELDS if getattr(d, f, None) is not None)
        out.append(
            {
                "id": str(u.id),
                "user_code": u.user_code,
                "full_name": u.full_name,
                "override_count": overridden,
            }
        )
    return APIResponse(data=out)
