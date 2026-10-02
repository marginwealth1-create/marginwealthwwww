"""Admin KYC review queue — list submissions, approve / reject, push live
updates to the user's terminal so the status banner flips without refresh."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import (
    CurrentAdmin,
    assert_user_in_scope,
    require_perm,
    scoped_user_ids,
)
from app.core.redis_client import publish
from app.models.audit_log import AuditAction
from app.models.kyc import KycStatus, KycSubmission
from app.models.user import User
from app.schemas.common import APIResponse
from app.services.audit_service import log_event
from app.utils.time_utils import now_utc

router = APIRouter(prefix="/kyc", tags=["admin-kyc"])


def _serialise(s: KycSubmission, user: User | None = None) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "user_id": str(s.user_id),
        "user_code": user.user_code if user else None,
        "user_name": user.full_name if user else None,
        "user_email": user.email if user else None,
        "id_proof_type": s.id_proof_type.value,
        "id_proof_number": s.id_proof_number,
        "id_proof_url": s.id_proof_url,
        "address_proof_type": s.address_proof_type.value,
        "address_proof_url": s.address_proof_url,
        "address_text": s.address_text,
        "status": s.status.value,
        "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
        "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
        "reviewed_by": str(s.reviewed_by) if s.reviewed_by else None,
        "admin_remark": s.admin_remark,
        "rejection_reason": s.rejection_reason,
    }


async def _publish(user_id: PydanticObjectId, event: str, submission: KycSubmission) -> None:
    """Push a kyc_update event to the user's WS channel so the status flips
    live on the profile page without a manual refresh, and fan the same
    event out to the admin dashboard so a colleague's KYC inbox refreshes
    immediately when the current admin approves / rejects."""
    try:
        await publish(
            f"user:{user_id}:kyc",
            {
                "type": "kyc_update",
                "event": event,
                "status": submission.status.value,
                "id": str(submission.id),
                "admin_remark": submission.admin_remark,
                "rejection_reason": submission.rejection_reason,
            },
        )
    except Exception:  # pragma: no cover — never fail the API on a publish error
        pass
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "kyc_update",
            {
                "event": event,
                "user_id": str(user_id),
                "kyc_id": str(submission.id),
                "status": submission.status.value,
            },
        )
    except Exception:  # pragma: no cover
        pass


@router.get("", response_model=APIResponse[list])
async def list_kyc(
    admin: CurrentAdmin,
    status: str | None = Query(default=None, description="Filter by status (PENDING / APPROVED / REJECTED)"),
    limit: int = Query(default=200, le=500),
    _: None = Depends(require_perm("kyc", "read")),
):
    q: dict[str, Any] = {}
    if status:
        try:
            q["status"] = KycStatus(status.upper()).value
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    # Sub-admin: limit to their assigned users. SUPER_ADMIN: no filter.
    scope = await scoped_user_ids(admin)
    if scope is not None:
        if not scope:
            return APIResponse(data=[])
        q["user_id"] = {"$in": scope}
    rows = await KycSubmission.find(q).sort("-submitted_at").limit(limit).to_list()

    user_ids = list({r.user_id for r in rows})
    users = await User.find({"_id": {"$in": user_ids}}).to_list() if user_ids else []
    user_map = {str(u.id): u for u in users}

    return APIResponse(
        data=[_serialise(r, user_map.get(str(r.user_id))) for r in rows]
    )


@router.get("/{submission_id}", response_model=APIResponse[dict])
async def get_kyc(
    submission_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("kyc", "read")),
):
    s = await KycSubmission.get(PydanticObjectId(submission_id))
    if s is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    await assert_user_in_scope(admin, s.user_id)
    user = await User.get(s.user_id)
    return APIResponse(data=_serialise(s, user))


@router.post("/{submission_id}/approve", response_model=APIResponse[dict])
async def approve_kyc(
    submission_id: str,
    payload: dict[str, Any],
    admin: CurrentAdmin,
    _: None = Depends(require_perm("kyc", "write")),
):
    s = await KycSubmission.get(PydanticObjectId(submission_id))
    if s is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    await assert_user_in_scope(admin, s.user_id)
    if s.status != KycStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"Submission is {s.status.value}, only PENDING can be approved",
        )

    remark = (payload.get("admin_remark") or "").strip() or None

    s.status = KycStatus.APPROVED
    s.reviewed_at = now_utc()
    s.reviewed_by = admin.id
    s.admin_remark = remark
    s.rejection_reason = None
    await s.save()

    # Mirror the approval onto the User doc so trading flows that gate on
    # KYC (deposits/withdrawals etc.) read a single source of truth.
    target_user = await User.get(s.user_id)
    if target_user is not None:
        target_user.kyc.is_verified = True
        target_user.kyc.verified_at = now_utc()
        if s.id_proof_type.value == "PAN" and s.id_proof_number:
            target_user.kyc.pan = s.id_proof_number.upper()
        if s.address_text:
            target_user.kyc.address_line1 = s.address_text[:200]
        await target_user.save()

    await log_event(
        action=AuditAction.APPROVE,
        entity_type="KycSubmission",
        entity_id=s.id,
        actor_id=admin.id,
        target_user_id=s.user_id,
        new_values={"admin_remark": remark},
    )
    await _publish(s.user_id, "approved", s)
    user = target_user or await User.get(s.user_id)
    return APIResponse(data=_serialise(s, user))


@router.post("/{submission_id}/reject", response_model=APIResponse[dict])
async def reject_kyc(
    submission_id: str,
    payload: dict[str, Any],
    admin: CurrentAdmin,
    _: None = Depends(require_perm("kyc", "write")),
):
    reason = (payload.get("rejection_reason") or "").strip()
    if len(reason) < 5:
        raise HTTPException(status_code=400, detail="Provide a rejection reason (min 5 chars)")

    s = await KycSubmission.get(PydanticObjectId(submission_id))
    if s is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    await assert_user_in_scope(admin, s.user_id)
    if s.status != KycStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"Submission is {s.status.value}, only PENDING can be rejected",
        )

    s.status = KycStatus.REJECTED
    s.reviewed_at = now_utc()
    s.reviewed_by = admin.id
    s.rejection_reason = reason
    s.admin_remark = (payload.get("admin_remark") or "").strip() or None
    await s.save()

    await log_event(
        action=AuditAction.REJECT,
        entity_type="KycSubmission",
        entity_id=s.id,
        actor_id=admin.id,
        target_user_id=s.user_id,
        new_values={"rejection_reason": reason, "admin_remark": s.admin_remark},
    )
    await _publish(s.user_id, "rejected", s)

    user = await User.get(s.user_id)
    return APIResponse(data=_serialise(s, user))
