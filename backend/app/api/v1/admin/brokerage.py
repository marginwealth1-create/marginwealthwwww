"""Admin brokerage plan management."""

from __future__ import annotations

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import CurrentAdmin, SuperAdmin, require_perm
from app.models.brokerage_plan import BrokeragePlan, PlanDetail
from app.schemas.common import APIResponse

router = APIRouter(prefix="/brokerage", tags=["admin-brokerage"])


@router.get("/plans", response_model=APIResponse[list])
async def list_plans(
    admin: CurrentAdmin,
    _: None = Depends(require_perm("brokerage", "read")),
):
    rows = await BrokeragePlan.find_all().to_list()
    return APIResponse(
        data=[
            {
                "id": str(p.id),
                "plan_name": p.plan_name,
                "description": p.description,
                "is_default": p.is_default,
                "is_active": p.is_active,
                "details_count": len(p.details),
                "details": [d.model_dump() for d in p.details],
            }
            for p in rows
        ]
    )


@router.post("/plans", response_model=APIResponse[dict])
async def create_plan(payload: dict, admin: SuperAdmin):
    if payload.get("is_default"):
        async for p in BrokeragePlan.find(BrokeragePlan.is_default == True):  # noqa: E712
            p.is_default = False
            await p.save()
    plan = BrokeragePlan(
        plan_name=payload["plan_name"],
        description=payload.get("description", ""),
        is_default=bool(payload.get("is_default", False)),
        is_active=bool(payload.get("is_active", True)),
        details=[PlanDetail(**d) for d in payload.get("details", [])],
    )
    await plan.insert()
    return APIResponse(data={"id": str(plan.id)})


@router.put("/plans/{plan_id}", response_model=APIResponse[dict])
async def update_plan(plan_id: str, payload: dict, admin: SuperAdmin):
    p = await BrokeragePlan.get(PydanticObjectId(plan_id))
    if p is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    for k in ("plan_name", "description", "is_active"):
        if k in payload:
            setattr(p, k, payload[k])
    if "is_default" in payload and payload["is_default"]:
        async for q in BrokeragePlan.find(BrokeragePlan.is_default == True):  # noqa: E712
            if q.id != p.id:
                q.is_default = False
                await q.save()
        p.is_default = True
    if "details" in payload:
        p.details = [PlanDetail(**d) for d in payload["details"]]
    await p.save()
    return APIResponse(data={"id": str(p.id)})


@router.delete("/plans/{plan_id}", response_model=APIResponse[dict])
async def delete_plan(plan_id: str, admin: SuperAdmin):
    p = await BrokeragePlan.get(PydanticObjectId(plan_id))
    if p is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    if p.is_default:
        raise HTTPException(status_code=400, detail="Cannot delete the default plan")
    await p.delete()
    return APIResponse(data={"ok": True})
