"""Admin master ledger — every user's wallet transactions + manual entry."""

from __future__ import annotations

from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, Query

from app.core.dependencies import (
    CurrentAdmin,
    assert_user_in_scope,
    require_perm,
    scoped_user_ids,
)
from app.models.audit_log import AuditAction
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User
from app.schemas.common import APIResponse
from app.services import wallet_service
from app.services.audit_service import log_event

router = APIRouter(prefix="/ledger", tags=["admin-ledger"])


@router.get("", response_model=APIResponse[dict])
async def list_all(
    admin: CurrentAdmin,
    user_id: str | None = None,
    transaction_type: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=1000),
    _: None = Depends(require_perm("ledger", "read")),
):
    q: dict[str, Any] = {}
    if user_id:
        # Read-only view: super-admin may inspect ANY user's ledger (even a
        # sub-admin's client); sub-admins stay scoped to their own pool.
        await assert_user_in_scope(admin, user_id, for_read=True)
        q["user_id"] = PydanticObjectId(user_id)
    else:
        scope = await scoped_user_ids(admin)
        if scope is not None:
            if not scope:
                return APIResponse(
                    data={
                        "items": [],
                        "meta": {
                            "page": page,
                            "page_size": page_size,
                            "total": 0,
                            "total_pages": 0,
                        },
                    }
                )
            q["user_id"] = {"$in": scope}
    if transaction_type:
        q["transaction_type"] = transaction_type
    total = await WalletTransaction.find(q).count()
    rows = (
        await WalletTransaction.find(q)
        .sort("-created_at")
        .skip((page - 1) * page_size)
        .limit(page_size)
        .to_list()
    )

    user_ids = list({r.user_id for r in rows})
    users = await User.find({"_id": {"$in": user_ids}}).to_list() if user_ids else []
    umap = {str(u.id): u.user_code for u in users}

    return APIResponse(
        data={
            "items": [
                {
                    "id": str(r.id),
                    "user_id": str(r.user_id),
                    "user_code": umap.get(str(r.user_id)),
                    "transaction_type": r.transaction_type.value,
                    "amount": str(r.amount),
                    "balance_before": str(r.balance_before),
                    "balance_after": str(r.balance_after),
                    "narration": r.narration,
                    "status": r.status.value,
                    "reference_type": r.reference_type,
                    "reference_id": r.reference_id,
                    "created_at": r.created_at,
                }
                for r in rows
            ],
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
            },
        }
    )


@router.post("/manual-entry", response_model=APIResponse[dict])
async def manual_entry(
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("ledger", "write")),
):
    user_id = payload["user_id"]
    await assert_user_in_scope(admin, user_id)
    amount = float(payload["amount"])
    txn_type = payload.get("transaction_type", "ADJUSTMENT")
    narration = payload["narration"]
    txn = await wallet_service.adjust(
        user_id,
        amount,
        transaction_type=TransactionType(txn_type),
        narration=narration,
        actor_id=admin.id,
    )
    await log_event(
        action=AuditAction.WALLET_ADJUST,
        entity_type="WalletTransaction",
        entity_id=str(txn.id),
        actor_id=admin.id,
        target_user_id=user_id,
        metadata={"amount": str(amount), "type": txn_type},
    )
    return APIResponse(data={"transaction_id": str(txn.id)})
