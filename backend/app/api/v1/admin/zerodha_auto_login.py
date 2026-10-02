"""Super-admin endpoints for Zerodha Kite auto-login configuration.

All endpoints require the SuperAdmin dependency — sub-admins and
brokers cannot touch the platform-level Kite credentials.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.core.dependencies import SuperAdmin
from app.core.redis_client import get_redis
from app.services.zerodha_auto_login import zerodha_auto_login

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/zerodha/auto-login", tags=["admin-zerodha-auto-login"])


class UpdateCredentialsBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)
    totp_secret: str = Field(..., min_length=8, max_length=128)


class ToggleBody(BaseModel):
    enabled: bool


class ScheduleBody(BaseModel):
    schedule_time_ist: str = Field(..., min_length=4, max_length=5)


async def _enforce_rate_limit(
    *,
    request: Request,
    bucket: str,
    max_count: int,
    window_sec: int,
) -> None:
    """Per-IP Redis sliding-window. Survives Redis outage by silently
    skipping rather than blocking the request (degrades gracefully)."""
    ip = request.client.host if request.client else "unknown"
    key = f"rl:zerodha_auto_login:{bucket}:{ip}"
    try:
        redis = get_redis()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window_sec)
        if count > max_count:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Too many {bucket} attempts — try again in "
                    f"{window_sec} seconds."
                ),
            )
    except HTTPException:
        raise
    except Exception:
        logger.warning("zerodha_auto_login_rate_limit_redis_unavailable")


@router.get("")
async def get_status(
    admin: SuperAdmin,
    account: int = Query(default=0, ge=0, le=1),
) -> dict:
    return {
        "success": True,
        "status": await zerodha_auto_login.get_status(account),
    }


@router.put("/credentials")
async def update_credentials(
    body: UpdateCredentialsBody,
    request: Request,
    admin: SuperAdmin,
    account: int = Query(default=0, ge=0, le=1),
) -> dict:
    await _enforce_rate_limit(
        request=request, bucket="credentials", max_count=5, window_sec=60
    )
    try:
        await zerodha_auto_login.save_credentials(
            account_index=account,
            username=body.username,
            password=body.password,
            totp_secret=body.totp_secret,
            actor_id=admin.id,
            ip_address=request.client.host if request.client else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "success": True,
        "status": await zerodha_auto_login.get_status(account),
    }


@router.post("/toggle")
async def toggle(
    body: ToggleBody,
    request: Request,
    admin: SuperAdmin,
    account: int = Query(default=0, ge=0, le=1),
) -> dict:
    try:
        await zerodha_auto_login.set_enabled(
            body.enabled,
            account_index=account,
            actor_id=admin.id,
            ip_address=request.client.host if request.client else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "success": True,
        "status": await zerodha_auto_login.get_status(account),
    }


@router.put("/schedule")
async def set_schedule(
    body: ScheduleBody,
    request: Request,
    admin: SuperAdmin,
    account: int = Query(default=0, ge=0, le=1),
) -> dict:
    try:
        await zerodha_auto_login.set_schedule(
            body.schedule_time_ist,
            account_index=account,
            actor_id=admin.id,
            ip_address=request.client.host if request.client else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "success": True,
        "status": await zerodha_auto_login.get_status(account),
    }


@router.post("/reset-lock")
async def reset_lock(
    admin: SuperAdmin,
    account: int = Query(default=0, ge=0, le=1),
) -> dict:
    """Force-clear a stuck Redis lock and reset in_progress DB state.
    Use when a crashed Playwright run left the lock held and new test
    attempts return 'Another auto-login is already in progress'."""
    await zerodha_auto_login.force_reset_lock(account)
    return {
        "success": True,
        "status": await zerodha_auto_login.get_status(account),
    }


@router.post("/test")
async def test_now(
    request: Request,
    admin: SuperAdmin,
    account: int = Query(default=0, ge=0, le=1),
) -> dict:
    """Queue a one-off login. The feed leader's scheduler runs it within
    ~30 s, so the fresh token and the ticker reconnect happen on the process
    that owns the WS pool — not in this API worker. Poll GET for the outcome."""
    await _enforce_rate_limit(
        request=request, bucket="test", max_count=10, window_sec=3600
    )
    try:
        await zerodha_auto_login.queue_test(account, actor_id=admin.id)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Could not queue the test login (Redis unavailable?): {exc}",
        ) from exc
    return {
        "success": True,
        "queued": True,
        "status": await zerodha_auto_login.get_status(account),
    }
