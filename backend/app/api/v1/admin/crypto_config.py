"""Admin self-service crypto deposit config.

Each admin (or super-admin / broker) sets their OWN crypto deposit here. The
oxapay API key is write-only — it is stored encrypted and never returned; the
GET only reports whether a key is present. The webhook URL to paste into the
oxapay dashboard is returned for convenience.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.dependencies import CurrentAdmin
from app.schemas.common import APIResponse
from app.services import crypto_config_service

router = APIRouter(prefix="/crypto-config", tags=["admin-crypto"])


class CryptoConfigUpdate(BaseModel):
    enabled: bool | None = None
    mode: str | None = None  # manual | gateway | both
    wallet_address: str | None = Field(default=None, max_length=200)
    network: str | None = Field(default=None, max_length=40)
    asset: str | None = Field(default=None, max_length=20)
    gateway: str | None = None  # oxapay | none
    # Raw oxapay key — encrypted server-side; "" clears it.
    oxapay_api_key: str | None = Field(default=None, max_length=400)


def _webhook_url(admin) -> str:
    base = (settings.BACKEND_PUBLIC_URL or "http://localhost:8000").rstrip("/")
    return f"{base}/api/v1/webhooks/oxapay/{admin.user_code}"


@router.get("", response_model=APIResponse[dict])
async def get_crypto_config(admin: CurrentAdmin):
    cfg = await crypto_config_service.get_for_admin(admin)
    data = crypto_config_service.to_admin_dict(cfg)
    data["webhook_url"] = _webhook_url(admin)
    return APIResponse(data=data)


@router.put("", response_model=APIResponse[dict])
async def update_crypto_config(payload: CryptoConfigUpdate, admin: CurrentAdmin):
    cfg = await crypto_config_service.upsert_for_admin(
        admin, payload.model_dump(exclude_unset=True)
    )
    data = crypto_config_service.to_admin_dict(cfg)
    data["webhook_url"] = _webhook_url(admin)
    return APIResponse(data=data, message="Crypto payment settings saved.")
