"""User-side KYC: submit / re-submit / fetch status / upload proof image."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.core.dependencies import CurrentUser
from app.models.kyc import (
    KycAddressProofType,
    KycIdProofType,
    KycStatus,
    KycSubmission,
)
from app.schemas.common import APIResponse
from app.utils.time_utils import now_utc

router = APIRouter(prefix="/kyc", tags=["user-kyc"])

# Proof images saved to ./uploads/kyc/<user_id>/<uuid>.<ext> and served via
# the same StaticFiles mount used by deposit screenshots.
UPLOAD_ROOT = Path("uploads") / "kyc"
ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
MAX_BYTES = 8 * 1024 * 1024  # 8 MB — KYC docs can be larger than screenshots


def _serialise(s: KycSubmission) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "user_id": str(s.user_id),
        "id_proof_type": s.id_proof_type.value,
        "id_proof_number": s.id_proof_number,
        "id_proof_url": s.id_proof_url,
        "address_proof_type": s.address_proof_type.value,
        "address_proof_url": s.address_proof_url,
        "address_text": s.address_text,
        "status": s.status.value,
        "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
        "reviewed_at": s.reviewed_at.isoformat() if s.reviewed_at else None,
        "admin_remark": s.admin_remark,
        "rejection_reason": s.rejection_reason,
    }


@router.post("/upload", response_model=APIResponse[dict])
async def upload_proof(user: CurrentUser, file: UploadFile = File(...)):
    """Stage a single proof image. Returns ``{ url }`` to attach to the
    submit payload. Accepts PNG / JPG / WEBP / PDF up to 8 MB."""
    ext = (Path(file.filename or "").suffix or "").lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {sorted(ALLOWED_EXTS)}",
        )

    contents = await file.read()
    if len(contents) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_BYTES // (1024*1024)} MB)")
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    user_dir = UPLOAD_ROOT / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = user_dir / fname
    out_path.write_bytes(contents)

    url = f"/uploads/kyc/{user.id}/{fname}"
    return APIResponse(data={"url": url, "size": len(contents)})


@router.get("", response_model=APIResponse[dict])
async def get_kyc(user: CurrentUser):
    """Latest submission for the user — frontend uses status to decide
    between showing the submit form, the pending banner, or the verified
    badge. Falls back to ``{ status: "NONE" }`` when nothing's submitted yet."""
    latest = (
        await KycSubmission.find(KycSubmission.user_id == user.id)
        .sort("-created_at")
        .limit(1)
        .to_list()
    )
    if not latest:
        return APIResponse(data={"status": "NONE", "user_kyc": user.kyc.model_dump()})
    return APIResponse(data={**_serialise(latest[0]), "user_kyc": user.kyc.model_dump()})


@router.post("/submit", response_model=APIResponse[dict])
async def submit_kyc(payload: dict[str, Any], user: CurrentUser):
    """Create a new submission (or replace a rejected one). Required body
    fields: ``id_proof_type, id_proof_url, address_proof_type,
    address_proof_url, address_text``. Optional: ``id_proof_number``."""
    # If there's already an APPROVED submission, refuse to overwrite — admin
    # must explicitly reject the previous one first.
    existing = (
        await KycSubmission.find(KycSubmission.user_id == user.id)
        .sort("-created_at")
        .limit(1)
        .to_list()
    )
    if existing and existing[0].status == KycStatus.APPROVED:
        raise HTTPException(status_code=409, detail="KYC is already approved")
    if existing and existing[0].status == KycStatus.PENDING:
        raise HTTPException(status_code=409, detail="A submission is already under review")

    try:
        id_type = KycIdProofType(str(payload.get("id_proof_type") or "").upper())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id_proof_type")
    try:
        addr_type = KycAddressProofType(str(payload.get("address_proof_type") or "").upper())
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid address_proof_type")

    id_url = (payload.get("id_proof_url") or "").strip()
    addr_url = (payload.get("address_proof_url") or "").strip()
    addr_text = (payload.get("address_text") or "").strip()
    if not id_url or not addr_url:
        raise HTTPException(status_code=400, detail="Both proof images are required")
    if len(addr_text) < 10:
        raise HTTPException(status_code=400, detail="Address must be at least 10 characters")

    submission = KycSubmission(
        user_id=user.id,
        id_proof_type=id_type,
        id_proof_number=(payload.get("id_proof_number") or "").strip() or None,
        id_proof_url=id_url,
        address_proof_type=addr_type,
        address_proof_url=addr_url,
        address_text=addr_text,
        status=KycStatus.PENDING,
        submitted_at=now_utc(),
    )
    await submission.insert()
    # Surface to the admin KYC inbox in real time.
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "kyc_update",
            {"event": "submitted", "user_id": str(user.id), "kyc_id": str(submission.id)},
        )
    except Exception:  # pragma: no cover
        pass
    # Admin notification bell — fan out a row per recipient up the
    # tier chain (super-admin + assigned admin + every broker).
    try:
        from app.models.notification import (
            AdminNotificationEventType,
            NotificationLevel,
        )
        from app.services import notification_service

        await notification_service.create_for_admins(
            source_user_id=user.id,
            event_type=AdminNotificationEventType.KYC_SUBMITTED,
            level=NotificationLevel.INFO,
            title=f"KYC submitted by {user.full_name}",
            message=f"PAN {payload.get('pan') or user.kyc.pan or '—'} · review pending",
            link="/kyc",
            reference_type="KycSubmission",
            reference_id=str(submission.id),
        )
    except Exception:  # pragma: no cover
        pass
    return APIResponse(data=_serialise(submission))
