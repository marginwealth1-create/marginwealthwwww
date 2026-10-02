"""User price alerts."""

from __future__ import annotations

from beanie import PydanticObjectId
from bson import Decimal128
from fastapi import APIRouter, HTTPException

from app.core.dependencies import CurrentUser
from app.models.alert import AlertType, PriceAlert
from app.models.order import InstrumentRef
from app.schemas.common import APIResponse
from app.schemas.trading import AlertCreate
from app.services import instrument_service

router = APIRouter(prefix="/alerts", tags=["user-alerts"])


def _ser(a: PriceAlert) -> dict:
    return {
        "id": str(a.id),
        "instrument_token": a.instrument.token,
        "symbol": a.instrument.symbol,
        "exchange": str(a.instrument.exchange),
        "alert_type": a.alert_type.value,
        "target_price": str(a.target_price) if a.target_price else None,
        "target_percent": a.target_percent,
        "is_active": a.is_active,
        "is_triggered": a.is_triggered,
        "note": a.note,
        "created_at": a.created_at,
        "triggered_at": a.triggered_at,
    }


@router.get("", response_model=APIResponse[list])
async def list_alerts(user: CurrentUser):
    rows = await PriceAlert.find(PriceAlert.user_id == user.id).sort("-created_at").to_list()
    return APIResponse(data=[_ser(a) for a in rows])


@router.post("", response_model=APIResponse[dict])
async def create_alert(payload: AlertCreate, user: CurrentUser):
    inst = await instrument_service.get_by_token(payload.token)
    a = PriceAlert(
        user_id=user.id,
        instrument=InstrumentRef(
            token=inst.token,
            symbol=inst.symbol,
            trading_symbol=inst.trading_symbol,
            exchange=inst.exchange,
            segment=inst.segment,
            lot_size=inst.lot_size,
            tick_size=inst.tick_size,
        ),
        alert_type=AlertType(payload.alert_type),
        target_price=Decimal128(str(payload.target_price)) if payload.target_price else None,
        target_percent=payload.target_percent,
        note=payload.note,
    )
    await a.insert()
    return APIResponse(data={"id": str(a.id)})


@router.delete("/{alert_id}", response_model=APIResponse[dict])
async def delete_alert(alert_id: str, user: CurrentUser):
    a = await PriceAlert.get(PydanticObjectId(alert_id))
    if a is None or a.user_id != user.id:
        raise HTTPException(status_code=404, detail="Alert not found")
    await a.delete()
    return APIResponse(data={"ok": True})
