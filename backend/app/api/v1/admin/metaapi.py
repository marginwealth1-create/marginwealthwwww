"""Super-admin endpoints for the MetaAPI (MetaTrader) feed.

Mirrors the Zerodha flow: credentials and symbols are saved here instead of
`.env`, and connect / disconnect happen from the panel.

The feed itself runs only in the feed process, so those two are published on
`metaapi:cmd` for that process to execute, and the status the panel shows is
the snapshot that process writes to Redis.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.dependencies import SuperAdmin
from app.core.redis_client import cache_get, publish
from app.models.audit_log import AuditAction
from app.models.metaapi_settings import MetaApiSettings
from app.services import audit_service
from app.services.metaapi_service import (
    METAAPI_CMD_CHANNEL,
    METAAPI_STATUS_KEY,
    metaapi,
)
from app.utils.crypto import CryptoError, encrypt

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/metaapi", tags=["admin-metaapi"])

class SettingsBody(BaseModel):
    token: str | None = None  # "" or "***" keeps the stored token
    account_id: str | None = None
    region: str | None = None
    max_symbols: int | None = Field(default=None, ge=1, le=200)
    symbol_map: dict[str, str] | None = None


class SymbolsBody(BaseModel):
    symbols: list[str]


async def _get_or_create() -> MetaApiSettings:
    doc = await MetaApiSettings.find_one()
    if doc is None:
        doc = MetaApiSettings()
        await doc.insert()
    return doc


async def _settings_payload() -> dict:
    """Never returns the token — only whether one is stored."""
    doc = await MetaApiSettings.find_one()
    cfg = await metaapi.load_config()
    return {
        "enabled": bool(doc.enabled) if doc is not None else cfg["enabled"],
        "has_token": bool(cfg["token"]),
        "account_id": cfg["account_id"],
        "region": cfg["region"],
        "symbols": cfg["symbols"],
        "symbol_map": cfg["alias"],
        "max_symbols": cfg["max_symbols"],
        "source": cfg["source"],
    }


async def _live_status() -> dict:
    """The feed process publishes its status every few seconds. `live: false`
    means nothing has reported recently — the feed process is down or the
    feed was never started."""
    snap = await cache_get(METAAPI_STATUS_KEY)
    if snap:
        return {**snap, "live": True}
    return {**metaapi.status(), "live": False}


async def _notify(action: str) -> None:
    try:
        await publish(METAAPI_CMD_CHANNEL, {"action": action})
    except Exception:
        logger.exception("metaapi_cmd_publish_failed action=%s", action)


@router.get("/settings")
async def get_settings(admin: SuperAdmin) -> dict:
    return {"success": True, "settings": await _settings_payload()}


@router.put("/settings")
async def save_settings(body: SettingsBody, admin: SuperAdmin) -> dict:
    doc = await _get_or_create()
    if body.token is not None and body.token.strip() not in ("", "***"):
        try:
            doc.encrypted_token, doc.encrypted_token_iv = encrypt(body.token.strip())
        except CryptoError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Could not encrypt the token — is ZERODHA_CREDS_KEY set? ({exc})",
            ) from exc
    if body.account_id is not None:
        doc.account_id = body.account_id.strip()
    if body.region is not None:
        doc.region = body.region.strip()
    if body.max_symbols is not None:
        doc.max_symbols = body.max_symbols
    if body.symbol_map is not None:
        doc.symbol_map = {
            k.strip().upper(): v.strip()
            for k, v in body.symbol_map.items()
            if k.strip() and v.strip()
        }
    await doc.save()
    await audit_service.log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="MetaApiSettings",
        entity_id=str(doc.id),
        actor_id=admin.id,
        metadata={"operation": "settings_updated", "account_id": doc.account_id},
    )
    # Credentials changed under a running feed → reconnect with the new ones.
    if doc.enabled:
        await _notify("connect")
    return {"success": True, "settings": await _settings_payload()}


@router.get("/status")
async def status(admin: SuperAdmin) -> dict:
    return {"success": True, "status": await _live_status()}


@router.post("/connect")
async def connect(admin: SuperAdmin) -> dict:
    cfg = await metaapi.load_config()
    if not cfg["configured"]:
        raise HTTPException(
            status_code=400, detail="Save the MetaAPI token and account id first."
        )
    doc = await _get_or_create()
    doc.enabled = True
    await doc.save()
    await _notify("connect")
    await audit_service.log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="MetaApiSettings",
        entity_id=str(doc.id),
        actor_id=admin.id,
        metadata={"operation": "feed_connect", "account_id": doc.account_id},
    )
    return {"success": True, "status": await _live_status()}


@router.post("/disconnect")
async def disconnect(admin: SuperAdmin) -> dict:
    doc = await _get_or_create()
    doc.enabled = False
    await doc.save()
    await _notify("disconnect")
    await audit_service.log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="MetaApiSettings",
        entity_id=str(doc.id),
        actor_id=admin.id,
        metadata={"operation": "feed_disconnect"},
    )
    return {"success": True, "status": await _live_status()}


@router.get("/symbols")
async def broker_symbols(
    admin: SuperAdmin, refresh: bool = Query(default=False)
) -> dict:
    """Every symbol this MT account offers. Served from the feed process's
    published snapshot; `refresh` forces a fresh read from MetaAPI."""
    cfg = await metaapi.load_config()
    if not cfg["configured"]:
        raise HTTPException(
            status_code=400, detail="Save the MetaAPI token and account id first."
        )
    try:
        symbols = await metaapi.list_broker_symbols(refresh=refresh)
    except ImportError as exc:
        raise HTTPException(
            status_code=503, detail="metaapi-cloud-sdk is not installed on this host."
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                f"MetaAPI symbol fetch failed: {exc} — check the token, account id "
                "and that the account is deployed on metaapi.cloud."
            ),
        ) from exc
    return {"success": True, "symbols": symbols, "cached": not refresh}


@router.put("/symbols")
async def save_symbols(body: SymbolsBody, admin: SuperAdmin) -> dict:
    doc = await _get_or_create()
    clean: list[str] = []
    for raw in body.symbols:
        s = str(raw).strip().upper()
        if s and s not in clean:
            clean.append(s)
    if len(clean) > doc.max_symbols:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This MetaAPI plan streams {doc.max_symbols} symbols at once; "
                f"you picked {len(clean)}. Anything not picked still uses the "
                "Infoway feed."
            ),
        )
    doc.symbols = clean
    await doc.save()
    if doc.enabled:
        await _notify("resubscribe")
    return {
        "success": True,
        "settings": await _settings_payload(),
        "status": await _live_status(),
    }
