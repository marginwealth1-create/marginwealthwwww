"""Admin Bonus Management API. Every route is gated by BONUSES_ENABLED (503
when off) and require_perm("bonuses", ...). Non-super admins are scoped to
their own pool's templates + grants.
"""

from __future__ import annotations

from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.config import settings
from app.core.dependencies import CurrentAdmin, require_perm
from app.models.bonus_transaction import BonusTransaction
from app.models.user import User, UserRole
from app.models.user_bonus import UserBonus, UserBonusStatus
from app.models.wallet import Wallet
from app.schemas.bonus import (
    BonusCancelRequest,
    BonusDeductRequest,
    BonusGrantRequest,
    BonusTemplateCreate,
    BonusTemplatePatch,
)
from app.schemas.common import APIResponse
from app.services import bonus_service, bonus_template_service
from app.utils.decimal_utils import to_decimal

router = APIRouter(prefix="/bonuses", tags=["admin-bonuses"])


def _gate() -> None:
    if not settings.BONUSES_ENABLED:
        raise HTTPException(status_code=503, detail="Bonus feature is disabled.")


def _pct(progress: Any, target: Any) -> float:
    t = to_decimal(target)
    if t <= 0:
        return 1.0  # no wager requirement → already "met"
    p = to_decimal(progress) / t
    return float(min(max(p, 0), 1))


def _template_out(t) -> dict:
    return {
        "id": str(t.id),
        "name": t.name,
        "type": str(t.type),
        "bonus_type": str(t.bonus_type),
        "bonus_value": str(t.bonus_value),
        "min_deposit": str(t.min_deposit),
        "max_bonus": str(t.max_bonus) if t.max_bonus is not None else None,
        "wager_requirement_multiple": t.wager_requirement_multiple,
        "duration_days": t.duration_days,
        "usage_limit": t.usage_limit,
        "used_count": t.used_count,
        "status": str(t.status),
        "description": t.description,
        "admin_id": str(t.admin_id) if t.admin_id else None,
        "created_at": t.created_at,
    }


def _bonus_out(b, user: User | None = None) -> dict:
    return {
        "id": str(b.id),
        "user_id": str(b.user_id),
        "user_code": getattr(user, "user_code", None) if user else None,
        "user_name": getattr(user, "full_name", None) if user else None,
        "template_id": str(b.template_id) if b.template_id else None,
        "template_name": b.template_name_snapshot,
        "type": b.type,
        "deposit_amount": str(b.deposit_amount),
        "original_amount": str(b.original_amount),
        "current_credit": str(b.current_credit),
        "wager_requirement_multiple": b.wager_requirement_multiple,
        "wager_target_volume": str(b.wager_target_volume),
        "wager_progress_volume": str(b.wager_progress_volume),
        "wager_progress_pct": _pct(b.wager_progress_volume, b.wager_target_volume),
        "status": str(b.status),
        "granted_at": b.granted_at,
        "expires_at": b.expires_at,
        "completed_at": b.completed_at,
        "cancelled_at": b.cancelled_at,
        "cancellation_reason": b.cancellation_reason,
        "notes": b.notes,
    }


# ── Templates ────────────────────────────────────────────────────────
@router.get("/templates", response_model=APIResponse[list])
async def list_templates(admin: CurrentAdmin, _: None = Depends(require_perm("bonuses", "read"))):
    _gate()
    rows = await bonus_template_service.list_templates(admin)
    return APIResponse(data=[_template_out(t) for t in rows])


@router.post("/templates", response_model=APIResponse[dict])
async def create_template(
    payload: BonusTemplateCreate, admin: CurrentAdmin,
    _: None = Depends(require_perm("bonuses", "write")),
):
    _gate()
    t = await bonus_template_service.create_template(payload, admin)
    return APIResponse(data=_template_out(t), message="Template created.")


@router.put("/templates/{template_id}", response_model=APIResponse[dict])
async def update_template(
    template_id: str, payload: BonusTemplatePatch, admin: CurrentAdmin,
    _: None = Depends(require_perm("bonuses", "write")),
):
    _gate()
    t = await bonus_template_service.update_template(template_id, payload, admin)
    if t is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return APIResponse(data=_template_out(t), message="Template updated.")


@router.delete("/templates/{template_id}", response_model=APIResponse[dict])
async def delete_template(
    template_id: str, admin: CurrentAdmin,
    _: None = Depends(require_perm("bonuses", "write")),
):
    _gate()
    ok = await bonus_template_service.delete_template(template_id, admin)
    if not ok:
        raise HTTPException(status_code=404, detail="Template not found")
    return APIResponse(data={"deleted": True})


# ── Grants ───────────────────────────────────────────────────────────
def _scope(admin: User) -> dict:
    if admin.role == UserRole.SUPER_ADMIN:
        return {}
    return {"admin_id": admin.id}


async def _load_scoped_bonus(bonus_id: str, admin: User) -> UserBonus | None:
    try:
        b = await UserBonus.get(PydanticObjectId(bonus_id))
    except Exception:
        return None
    if b is None:
        return None
    if admin.role != UserRole.SUPER_ADMIN and b.admin_id != admin.id:
        return None
    return b


@router.get("", response_model=APIResponse[dict])
async def list_grants(
    admin: CurrentAdmin,
    _: None = Depends(require_perm("bonuses", "read")),
    user_id: str | None = None,
    status: str | None = None,
    type: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    _gate()
    q: dict[str, Any] = _scope(admin)
    if user_id:
        try:
            q["user_id"] = PydanticObjectId(user_id)
        except Exception:
            q["user_id"] = None
    if status:
        q["status"] = status
    if type:
        q["type"] = type
    total = await UserBonus.find(q).count()
    rows = (
        await UserBonus.find(q).sort("-granted_at").skip((page - 1) * limit).limit(limit).to_list()
    )
    users = await User.find({"_id": {"$in": list({r.user_id for r in rows})}}).to_list()
    umap = {u.id: u for u in users}
    return APIResponse(data={
        "bonuses": [_bonus_out(b, umap.get(b.user_id)) for b in rows],
        "pagination": {
            "page": page, "limit": limit, "total": total,
            "totalPages": (total + limit - 1) // limit,
        },
    })


@router.post("/grant", response_model=APIResponse[dict])
async def grant(
    payload: BonusGrantRequest, admin: CurrentAdmin,
    _: None = Depends(require_perm("bonuses", "write")),
):
    _gate()
    try:
        user = await User.get(PydanticObjectId(payload.user_id))
    except Exception:
        user = None
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if admin.role != UserRole.SUPER_ADMIN and getattr(user, "assigned_admin_id", None) != admin.id:
        raise HTTPException(status_code=403, detail="User is not in your pool")

    try:
        if payload.template_id:
            tpl = await bonus_template_service.get_template(payload.template_id, admin)
            if tpl is None:
                raise HTTPException(status_code=404, detail="Template not found")
            bonus = await bonus_service.grant_from_template(
                user, tpl, payload.deposit_amount or 0,
                deposit_id=None, granted_by=admin.id,
            )
        elif payload.amount is not None:
            bonus = await bonus_service.grant_custom(
                user, payload.amount, granted_by=admin.id, notes=payload.notes or "",
            )
        else:
            raise HTTPException(status_code=400, detail="Provide a template_id + deposit_amount, or a custom amount.")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return APIResponse(data=_bonus_out(bonus, user), message="Bonus granted.")


@router.post("/deduct", response_model=APIResponse[dict])
async def deduct(
    payload: BonusDeductRequest, admin: CurrentAdmin,
    _: None = Depends(require_perm("bonuses", "write")),
):
    """Manually claw back bonus credit from a user (Add/Deduct-Fund-style
    quick action). Deducts up to `amount` from the user's active bonuses."""
    _gate()
    try:
        user = await User.get(PydanticObjectId(payload.user_id))
    except Exception:
        user = None
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if admin.role != UserRole.SUPER_ADMIN and getattr(user, "assigned_admin_id", None) != admin.id:
        raise HTTPException(status_code=403, detail="User is not in your pool")
    try:
        deducted = await bonus_service.deduct_custom(
            user.id, payload.amount, by=admin.id, reason=payload.reason,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    w = await Wallet.find_one(Wallet.user_id == user.id)
    return APIResponse(
        data={"deducted": str(deducted), "user_wallet_credit": str(w.credit) if w else "0"},
        message=f"Deducted ₹{deducted} bonus credit.",
    )


@router.post("/{bonus_id}/cancel", response_model=APIResponse[dict])
async def cancel_grant(
    bonus_id: str, payload: BonusCancelRequest, admin: CurrentAdmin,
    _: None = Depends(require_perm("bonuses", "write")),
):
    _gate()
    b = await _load_scoped_bonus(bonus_id, admin)
    if b is None:
        raise HTTPException(status_code=404, detail="Bonus not found")
    try:
        b = await bonus_service.cancel(b, cancelled_by=admin.id, reason=payload.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return APIResponse(data=_bonus_out(b), message="Bonus cancelled.")


@router.get("/{bonus_id}/ledger", response_model=APIResponse[list])
async def bonus_ledger(
    bonus_id: str, admin: CurrentAdmin,
    _: None = Depends(require_perm("bonuses", "read")),
):
    _gate()
    b = await _load_scoped_bonus(bonus_id, admin)
    if b is None:
        raise HTTPException(status_code=404, detail="Bonus not found")
    rows = await BonusTransaction.find(BonusTransaction.bonus_id == b.id).sort("-created_at").to_list()
    return APIResponse(data=[{
        "id": str(r.id),
        "action": str(r.action),
        "credit_delta": str(r.credit_delta),
        "created_at": r.created_at,
        "metadata": r.metadata,
    } for r in rows])


@router.post("/{bonus_id}/recompute", response_model=APIResponse[dict])
async def recompute(
    bonus_id: str, admin: CurrentAdmin,
    _: None = Depends(require_perm("bonuses", "write")),
):
    _gate()
    b = await _load_scoped_bonus(bonus_id, admin)
    if b is None:
        raise HTTPException(status_code=404, detail="Bonus not found")
    total = await bonus_service.recompute_credit(b.user_id)
    b = await UserBonus.get(b.id)
    return APIResponse(data={"user_wallet_credit": str(total), "bonus": _bonus_out(b)})
