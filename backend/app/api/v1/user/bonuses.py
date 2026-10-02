"""User-facing Bonus Management API — read-only. Every route gated by
BONUSES_ENABLED (503 when off).
"""

from __future__ import annotations

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException, Query

from app.core.config import settings
from app.core.dependencies import CurrentUser
from app.models.bonus_transaction import BonusTransaction
from app.models.user_bonus import UserBonus
from app.schemas.common import APIResponse
from app.services import bonus_service
from app.utils.decimal_utils import to_decimal

router = APIRouter(prefix="/bonuses", tags=["user-bonuses"])


def _gate() -> None:
    if not settings.BONUSES_ENABLED:
        raise HTTPException(status_code=503, detail="Bonus feature is disabled.")


def _pct(progress, target) -> float:
    t = to_decimal(target)
    if t <= 0:
        return 1.0
    return float(min(max(to_decimal(progress) / t, 0), 1))


@router.get("", response_model=APIResponse[list])
async def my_bonuses(user: CurrentUser):
    _gate()
    rows = await UserBonus.find(UserBonus.user_id == user.id).sort("-granted_at").to_list()
    return APIResponse(data=[{
        "id": str(b.id),
        "template_name": b.template_name_snapshot,
        "type": b.type,
        "original_amount": str(b.original_amount),
        "current_credit": str(b.current_credit),
        "wager_target_volume": str(b.wager_target_volume),
        "wager_progress_volume": str(b.wager_progress_volume),
        "wager_progress_pct": _pct(b.wager_progress_volume, b.wager_target_volume),
        "status": str(b.status),
        "granted_at": b.granted_at,
        "expires_at": b.expires_at,
    } for b in rows])


@router.get("/eligible", response_model=APIResponse[dict])
async def eligible(user: CurrentUser, amount: str = Query(...)):
    _gate()
    try:
        amt = to_decimal(amount)
        if amt <= 0:
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid amount")
    return APIResponse(data=await bonus_service.preview_eligible(user, amt))


@router.get("/{bonus_id}/ledger", response_model=APIResponse[list])
async def my_bonus_ledger(bonus_id: str, user: CurrentUser):
    _gate()
    try:
        b = await UserBonus.get(PydanticObjectId(bonus_id))
    except Exception:
        b = None
    if b is None or b.user_id != user.id:
        raise HTTPException(status_code=404, detail="Bonus not found")
    rows = await BonusTransaction.find(BonusTransaction.bonus_id == b.id).sort("-created_at").to_list()
    return APIResponse(data=[{
        "id": str(r.id),
        "action": str(r.action),
        "credit_delta": str(r.credit_delta),
        "created_at": r.created_at,
    } for r in rows])
