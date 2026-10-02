"""Admin endpoints for the Infoway (forex / crypto / metals / energy) feed."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.dependencies import CurrentAdmin
from app.services.infoway_service import (
    default_symbols,
    infoway,
    mirror_subscribed_to_instruments,
)

router = APIRouter(prefix="/infoway", tags=["admin-infoway"])


@router.get("/status")
async def status(admin: CurrentAdmin):
    return {"success": True, "status": infoway.status()}


@router.post("/connect")
async def connect(admin: CurrentAdmin):
    if not infoway.is_configured:
        raise HTTPException(
            status_code=400,
            detail="INFOWAY_API_KEY not set. Add it to backend/.env and restart.",
        )
    await infoway.start()
    n = await infoway.subscribe(default_symbols())
    mirrored = await mirror_subscribed_to_instruments()
    return {
        "success": True,
        "subscribed": n,
        "mirrored": mirrored,
        "status": infoway.status(),
    }


@router.post("/disconnect")
async def disconnect(admin: CurrentAdmin):
    await infoway.stop()
    return {"success": True, "status": infoway.status()}


@router.post("/subscribe")
async def subscribe(payload: dict, admin: CurrentAdmin):
    codes = payload.get("symbols") or payload.get("codes") or []
    if not isinstance(codes, list):
        raise HTTPException(status_code=400, detail="symbols must be a list")
    n = await infoway.subscribe(codes)
    mirrored = await mirror_subscribed_to_instruments()
    return {
        "success": True,
        "added": n,
        "mirrored": mirrored,
        "status": infoway.status(),
    }


@router.post("/unsubscribe")
async def unsubscribe(payload: dict, admin: CurrentAdmin):
    codes = payload.get("symbols") or payload.get("codes") or []
    if not isinstance(codes, list):
        raise HTTPException(status_code=400, detail="symbols must be a list")
    n = await infoway.unsubscribe(codes)
    return {"success": True, "removed": n, "status": infoway.status()}


@router.get("/ticks")
async def ticks(admin: CurrentAdmin):
    """Latest cached ticks for all subscribed symbols (for debugging)."""
    return {"success": True, "ticks": infoway.get_all_ticks()}
