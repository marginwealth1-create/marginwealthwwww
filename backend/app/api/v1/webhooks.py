"""Public payment webhooks — no JWT; each provider verified by its own HMAC.

Mounted at /api/v1 so the path is /api/v1/webhooks/oxapay/{admin_code}. The
{admin_code} selects WHICH admin's merchant key verifies the signature and
whose users get credited — every admin routes to their own oxapay account.
"""

from __future__ import annotations

import json
import logging

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request

from app.models.transaction import DepositRequest, DepositStatus, TransactionType
from app.services import crypto_config_service, oxapay_service, wallet_service
from app.utils.decimal_utils import to_decimal
from app.utils.time_utils import now_utc

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger("webhooks")


@router.post("/oxapay/{admin_code}")
async def oxapay_webhook(admin_code: str, request: Request):
    raw = await request.body()
    hmac_header = request.headers.get("HMAC") or request.headers.get("hmac") or ""

    cfg = await crypto_config_service.get_by_owner_code(admin_code)
    key = crypto_config_service.decrypted_oxapay_key(cfg) if cfg else None
    # Fail-closed: no key / bad signature → reject, so a forged webhook can
    # never credit a wallet.
    if not oxapay_service.verify_webhook_signature(key, raw, hmac_header):
        logger.warning("oxapay_webhook_bad_signature code=%s", admin_code)
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    order_id = payload.get("order_id") or payload.get("orderId")
    status = payload.get("status")
    track_id = payload.get("track_id") or payload.get("trackId")
    logger.info("oxapay_webhook code=%s order=%s status=%s", admin_code, order_id, status)

    if not order_id:
        return {"status": "ok"}
    try:
        oid = ObjectId(str(order_id))
    except Exception:
        return {"status": "ok"}

    # 'expired'/'failed'/'cancelled' → mark the INITIATED row FAILED so the user
    # sees "payment not completed" and it never shows up on the admin side.
    if oxapay_service.is_dead(status):
        await DepositRequest.get_motor_collection().update_one(
            {"_id": oid, "payment_mode": "CRYPTO", "status": DepositStatus.INITIATED.value},
            {"$set": {"status": DepositStatus.FAILED.value, "gateway_status": str(status).lower(),
                      "gateway_ref": str(track_id) if track_id else None, "updated_at": now_utc()}},
        )
        return {"status": "ok"}

    # Only a fully-'paid' invoice credits. waiting/confirming → no-op.
    if not oxapay_service.is_paid(status):
        return {"status": "ok"}

    # Operator policy: oxapay is a VERIFIED gateway → AUTO-CREDIT, no admin
    # approval. Atomic claim INITIATED→APPROVED (same double-credit guard as the
    # admin approve path); only the winner credits the wallet. A repeat 'paid'
    # webhook matches zero docs and no-ops.
    now = now_utc()
    claimed = await DepositRequest.get_motor_collection().find_one_and_update(
        {"_id": oid, "payment_mode": "CRYPTO", "status": DepositStatus.INITIATED.value},
        {"$set": {"status": DepositStatus.APPROVED.value, "gateway_status": "paid",
                  "gateway_ref": str(track_id) if track_id else None, "processed_at": now,
                  "updated_at": now, "admin_remark": "oxapay: auto-credited (gateway verified)"}},
    )
    if claimed is None:
        return {"status": "ok"}
    try:
        await wallet_service.adjust(
            claimed["user_id"],
            to_decimal(claimed["amount"]),
            transaction_type=TransactionType.DEPOSIT,
            narration=f"Crypto deposit (oxapay ref {track_id or oid})",
            reference_type="DEPOSIT",
            reference_id=str(oid),
        )
    except Exception:
        # Credit failed after the claim — revert to INITIATED so a webhook retry
        # can credit it instead of leaving APPROVED with no money moved.
        await DepositRequest.get_motor_collection().update_one(
            {"_id": oid},
            {"$set": {"status": DepositStatus.INITIATED.value, "processed_at": None, "updated_at": now_utc()}},
        )
        raise
    logger.info("oxapay_webhook_paid_credited order=%s", order_id)
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "deposit_update",
            {"event": "approved", "user_id": str(claimed["user_id"]), "deposit_id": str(oid)},
        )
    except Exception:  # pragma: no cover
        pass
    return {"status": "ok"}
