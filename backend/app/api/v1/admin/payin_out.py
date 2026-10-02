"""Admin payin-out — deposit + withdrawal approvals + bank accounts + W/D rules."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException

from app.api.v1.admin._owner import build_owner_map, owner_fields
from app.core.dependencies import (
    CurrentAdmin,
    SuperAdmin,
    assert_user_in_scope,
    require_perm,
    scoped_user_ids,
)
from app.models.audit_log import AuditAction
from app.models.user import UserRole
from app.models.bank_account import CompanyBankAccount
from app.models.transaction import (
    DepositRequest,
    DepositStatus,
    SettlementRequest,
    SettlementStatus,
    TransactionType,
    WdRule,
    WithdrawalRequest,
    WithdrawalStatus,
)
from app.schemas.admin.common import (
    ApproveDepositRequest,
    ApproveWithdrawalRequest,
    RejectDepositRequest,
    RejectWithdrawalRequest,
)
from app.schemas.common import APIResponse
from app.services import wallet_service
from app.services.audit_service import log_event
from app.utils.decimal_utils import to_decimal
from app.utils.time_utils import now_utc

router = APIRouter(tags=["admin-payin-out"])


async def _user_search_ids(search: str | None) -> list | None:
    """Resolve a free-text client search to matching user_ids — by user_code,
    full name, email or mobile (case-insensitive). Returns None when no term is
    given (caller applies no user filter); otherwise a possibly-empty id list
    (empty ⇒ the query naturally returns nothing)."""
    if not search or not search.strip():
        return None
    from app.models.user import User as _User

    term = re.escape(search.strip())
    rx = {"$regex": term, "$options": "i"}
    matched = await _User.find(
        {"$or": [
            {"user_code": rx},
            {"full_name": rx},
            {"email": rx},
            {"mobile": rx},
        ]}
    ).to_list()
    return [u.id for u in matched]


def _apply_search_scope(q: dict, search_ids: list | None) -> None:
    """Fold a client-search id list into query `q`, intersecting with any
    existing scope filter already on `q["user_id"]`."""
    if search_ids is None:
        return
    if "user_id" in q and isinstance(q["user_id"], dict) and "$in" in q["user_id"]:
        scope_set = {str(x) for x in q["user_id"]["$in"]}
        search_ids = [i for i in search_ids if str(i) in scope_set]
    q["user_id"] = {"$in": search_ids}


# ── Deposits ────────────────────────────────────────────────────────
@router.get("/deposits", response_model=APIResponse[dict])
async def list_deposits(
    admin: CurrentAdmin,
    status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 15,
    gateway: bool = False,
    _: None = Depends(require_perm("deposits", "read")),
):
    """Admin deposit inbox.

    Status defaults to NONE (i.e. all statuses) instead of the older
    "PENDING" default. The frontend dropdown shows PENDING / APPROVED
    / REJECTED / All; landing on "All" by default surfaces the full
    recent history so a "No data" empty state means a genuinely quiet
    queue, not a hidden filter (operator-flagged 21-May).

    Paginated at 15 rows per page by default to match the deposits
    panel's UI pager. `page_size` is capped at 200 so a buggy client
    can't ask for a million rows.
    """
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    q: dict[str, Any] = {}
    # Gateway-initiated (unpaid) and failed/abandoned crypto deposits are never
    # shown to the admin — only real, paid/pending money enters the queue.
    _hidden = ["INITIATED", "FAILED"]
    if gateway:
        # Divinepay gateway inbox — show EVERY status (pending / approved /
        # failed) so the admin sees which online payments succeeded and which
        # are still pending, per the dedicated "Gateway Payments" tab.
        q["gateway"] = "divinepay"
        if status:
            q["status"] = status
    elif status and status not in _hidden:
        q["status"] = status
    else:
        q["status"] = {"$nin": _hidden}
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

    # Client-wise search (#5) — narrow to users matching the free-text term,
    # intersected with the admin's scope above.
    _apply_search_scope(q, await _user_search_ids(search))

    total = await DepositRequest.find(q).count()
    rows = (
        await DepositRequest.find(q)
        .sort("-created_at")
        .skip((page - 1) * page_size)
        .limit(page_size)
        .to_list()
    )
    owner_map = await build_owner_map([r.user_id for r in rows])

    # Batch-lookup settlement_outstanding per user so admin can see at
    # approval time how much of this deposit will be recovered first.
    from app.models.wallet import Wallet
    user_ids = list({r.user_id for r in rows})
    wallets = (
        await Wallet.find({"user_id": {"$in": user_ids}}).to_list()
        if user_ids
        else []
    )
    outstanding_map = {str(w.user_id): str(w.settlement_outstanding) for w in wallets}

    return APIResponse(
        data={
            "items": [
                {
                    "id": str(r.id),
                    "user_id": str(r.user_id),
                    "amount": str(r.amount),
                    "payment_mode": r.payment_mode.value,
                    "utr_number": r.utr_number,
                    # Crypto details so the admin sees the user's on-chain tx id
                    # (manual) or the oxapay ref (gateway) instead of a blank UTR.
                    "crypto_asset": r.crypto_asset,
                    "crypto_network": r.crypto_network,
                    "crypto_address": r.crypto_address,
                    "crypto_tx_hash": r.crypto_tx_hash,
                    "gateway": r.gateway,
                    "gateway_ref": r.gateway_ref,
                    "gateway_status": r.gateway_status,
                    "screenshot_url": r.screenshot_url,
                    "status": r.status.value,
                    "user_remark": r.user_remark,
                    "admin_remark": r.admin_remark,
                    "created_at": r.created_at,
                    "processed_at": r.processed_at,
                    "user_settlement_outstanding": outstanding_map.get(str(r.user_id), "0"),
                    **owner_fields(owner_map.get(str(r.user_id))),
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


@router.get("/gateway-summary", response_model=APIResponse[dict])
async def gateway_summary(
    admin: CurrentAdmin,
    _: None = Depends(require_perm("deposits", "read")),
):
    """Divinepay gateway deposit rollup for the "Gateway Payments" tab —
    `enabled` (is the gateway on for this admin, so the tab shows) plus today's
    and the last-7-days credited totals. Only APPROVED (successfully credited)
    deposits count toward the money totals."""
    from datetime import timedelta

    from app.utils.decimal_utils import to_decimal
    from app.utils.time_utils import start_of_day_ist, to_utc

    enabled = admin.role == UserRole.SUPER_ADMIN or bool(
        getattr(admin, "payment_gateway_enabled", False)
    )
    base: dict[str, Any] = {"gateway": "divinepay", "status": DepositStatus.APPROVED.value}
    scope = await scoped_user_ids(admin)
    if scope is not None:
        if not scope:
            return APIResponse(
                data={
                    "enabled": enabled,
                    "today_total": "0", "today_count": 0,
                    "week_total": "0", "week_count": 0,
                }
            )
        base["user_id"] = {"$in": scope}

    today_start = to_utc(start_of_day_ist())
    week_start = today_start - timedelta(days=6)  # today + previous 6 = 7-day window
    today_rows = await DepositRequest.find(
        {**base, "processed_at": {"$gte": today_start}}
    ).to_list()
    week_rows = await DepositRequest.find(
        {**base, "processed_at": {"$gte": week_start}}
    ).to_list()
    today_total = sum((to_decimal(r.amount) for r in today_rows), to_decimal(0))
    week_total = sum((to_decimal(r.amount) for r in week_rows), to_decimal(0))
    return APIResponse(
        data={
            "enabled": enabled,
            "today_total": str(today_total), "today_count": len(today_rows),
            "week_total": str(week_total), "week_count": len(week_rows),
        }
    )


@router.post("/deposits/{deposit_id}/approve", response_model=APIResponse[dict])
async def approve_deposit(
    deposit_id: str,
    payload: ApproveDepositRequest,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("deposits", "write")),
):
    r = await DepositRequest.get(PydanticObjectId(deposit_id))
    if r is None:
        raise HTTPException(status_code=404, detail="Deposit not found")
    await assert_user_in_scope(admin, r.user_id)
    if r.status != DepositStatus.PENDING:
        raise HTTPException(status_code=400, detail="Already processed")

    # ── Atomic claim (double-credit guard) ───────────────────────────
    # Flip PENDING→APPROVED in ONE conditional update BEFORE crediting.
    # A slow-network double-click (or two admins) fires two near-
    # simultaneous approve requests; both passed the read-time status
    # check above while the row was still PENDING, so the old code ran
    # `wallet_service.adjust()` twice and the user's wallet was credited
    # twice. The fix: only the request that WINS this compare-and-set
    # (matches a still-PENDING row) proceeds to credit — the loser
    # matches zero docs and bails out as "Already processed".
    processed_at = now_utc()
    claimed = await DepositRequest.get_motor_collection().find_one_and_update(
        {"_id": r.id, "status": DepositStatus.PENDING.value},
        {
            "$set": {
                "status": DepositStatus.APPROVED.value,
                "processed_by": admin.id,
                "processed_at": processed_at,
                "admin_remark": payload.admin_remark,
                "updated_at": processed_at,
            }
        },
    )
    if claimed is None:
        # Lost the race — another request (the first click) already
        # claimed and credited this deposit.
        raise HTTPException(status_code=400, detail="Already processed")

    amount = to_decimal(r.amount)
    try:
        await wallet_service.adjust(
            r.user_id,
            amount,
            transaction_type=TransactionType.DEPOSIT,
            narration=f"Deposit approved (ref {r.utr_number or r.id})",
            reference_type="DEPOSIT",
            reference_id=str(r.id),
            actor_id=admin.id,
        )
    except Exception:
        # Credit failed AFTER we claimed the row — revert to PENDING so
        # the deposit drops back into the queue instead of showing
        # APPROVED with no money moved. Re-raise the real error.
        await DepositRequest.get_motor_collection().update_one(
            {"_id": r.id},
            {
                "$set": {
                    "status": DepositStatus.PENDING.value,
                    "processed_by": None,
                    "processed_at": None,
                    "updated_at": now_utc(),
                }
            },
        )
        raise

    # Status already persisted by the atomic claim above — keep the
    # in-memory copy in sync for the audit log + response.
    r.status = DepositStatus.APPROVED
    r.processed_by = admin.id
    r.processed_at = processed_at
    r.admin_remark = payload.admin_remark

    # ── Bonus auto-grant (Bonus Management, gated + best-effort) ──────
    # Stamp first_deposit_at on the very first approved deposit, then try to
    # auto-grant a matching bonus. Idempotent by deposit_id. Never let a bonus
    # failure block the deposit approval / credit that already succeeded.
    try:
        from app.core.config import settings as _bset

        if _bset.BONUSES_ENABLED:
            from app.models.user import User as _User
            from app.services import bonus_service as _bonus

            _u = await _User.get(r.user_id)
            if _u is not None:
                _was_first = getattr(_u, "first_deposit_at", None) is None
                if _was_first:
                    _u.first_deposit_at = processed_at
                    await _u.save()
                _granted = await _bonus.maybe_auto_grant_on_deposit(
                    _u, amount, deposit_id=r.id, is_first=_was_first
                )
                if _granted is not None:
                    await DepositRequest.get_motor_collection().update_one(
                        {"_id": r.id},
                        {"$set": {"bonus_id": _granted.id, "bonus_amount": _granted.original_amount}},
                    )
                    try:
                        from app.core.redis_client import publish as _pub

                        await _pub(f"user:{r.user_id}:wallet", {
                            "type": "bonus_granted",
                            "payload": {
                                "amount": str(_granted.original_amount),
                                "name": _granted.template_name_snapshot,
                            },
                        })
                    except Exception:
                        pass
    except Exception:  # pragma: no cover — bonus must never break approval
        pass

    await log_event(
        action=AuditAction.APPROVE,
        entity_type="DepositRequest",
        entity_id=r.id,
        actor_id=admin.id,
        target_user_id=r.user_id,
        metadata={"amount": str(amount)},
    )
    # Notify every other admin dashboard so a colleague watching the same
    # Deposits inbox sees the row move from PENDING → APPROVED without F5.
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "deposit_update",
            {"event": "approved", "user_id": str(r.user_id), "deposit_id": str(r.id)},
        )
    except Exception:  # pragma: no cover
        pass
    return APIResponse(data={"id": str(r.id), "status": r.status.value})


@router.post("/deposits/{deposit_id}/reject", response_model=APIResponse[dict])
async def reject_deposit(
    deposit_id: str,
    payload: RejectDepositRequest,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("deposits", "write")),
):
    r = await DepositRequest.get(PydanticObjectId(deposit_id))
    if r is None:
        raise HTTPException(status_code=404, detail="Deposit not found")
    await assert_user_in_scope(admin, r.user_id)
    if r.status != DepositStatus.PENDING:
        raise HTTPException(status_code=400, detail="Already processed")
    r.status = DepositStatus.REJECTED
    r.admin_remark = payload.admin_remark
    r.processed_by = admin.id
    r.processed_at = now_utc()
    await asyncio.gather(
        r.save(),
        log_event(
            action=AuditAction.REJECT,
            entity_type="DepositRequest",
            entity_id=r.id,
            actor_id=admin.id,
            target_user_id=r.user_id,
        ),
    )
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "deposit_update",
            {"event": "rejected", "user_id": str(r.user_id), "deposit_id": str(r.id)},
        )
    except Exception:  # pragma: no cover
        pass
    # User-side push — reject has no wallet move so we can't ride the
    # wallet_event hook. Fire an explicit push so the trader's tray
    # pings even when their PWA is closed.
    try:
        import asyncio as _asyncio

        from app.services.push_service import send_to_user as _push_user

        amt_label = f"₹{r.amount}" if r.amount else ""
        reason_label = (
            f" — {payload.admin_remark}" if payload.admin_remark else ""
        )
        _asyncio.create_task(
            _push_user(
                r.user_id,
                title="❌ Deposit rejected",
                body=f"{amt_label} was rejected{reason_label}",
                url="/wallet",
                tag=f"deposit-rejected-{r.id}",
            )
        )
    except Exception:  # pragma: no cover
        pass
    return APIResponse(data={"id": str(r.id), "status": r.status.value})


# ── Settlement Requests ─────────────────────────────────────────────
# Pending settlements queued by `wallet_service.adjust()` / `force_debit`
# when a user with `User.auto_settlement = False` incurs a debit that
# pushes available_balance below 0. Admin approves from the Payments
# → Settlement Requests tab. Reuses the deposit `deposits` permission
# key — same operator group already manages cash-flow approvals.
@router.get("/settlement-requests", response_model=APIResponse[list])
async def list_settlement_requests(
    admin: CurrentAdmin,
    status: str | None = "PENDING",
    limit: int = 200,
    _: None = Depends(require_perm("deposits", "read")),
):
    q: dict[str, Any] = {}
    if status:
        q["status"] = status
    scope = await scoped_user_ids(admin)
    if scope is not None:
        if not scope:
            return APIResponse(data=[])
        q["user_id"] = {"$in": scope}
    rows = (
        await SettlementRequest.find(q)
        .sort("-created_at")
        .limit(limit)
        .to_list()
    )
    owner_map = await build_owner_map([r.user_id for r in rows])

    # Pull each user's CURRENT available_balance so the admin sees the
    # live shortfall (the request's `requested_amount` is updated on
    # every debit but a fresh read is safer at approval time).
    from app.models.wallet import Wallet

    user_ids = list({r.user_id for r in rows})
    wallets = (
        await Wallet.find({"user_id": {"$in": user_ids}}).to_list()
        if user_ids
        else []
    )
    wallet_map = {str(w.user_id): w for w in wallets}

    return APIResponse(
        data=[
            {
                "id": str(r.id),
                "user_id": str(r.user_id),
                "requested_amount": str(r.requested_amount),
                "available_at_request": str(r.available_at_request),
                "current_available": (
                    str(wallet_map[str(r.user_id)].available_balance)
                    if str(r.user_id) in wallet_map
                    else None
                ),
                "settlement_outstanding_at_request": str(
                    r.settlement_outstanding_at_request
                ),
                "reference_type": r.reference_type,
                "reference_id": r.reference_id,
                "narration": r.narration,
                "status": r.status.value,
                "approved_by": str(r.approved_by) if r.approved_by else None,
                "approved_at": r.approved_at,
                "rejected_reason": r.rejected_reason,
                "created_at": r.created_at,
                **owner_fields(owner_map.get(str(r.user_id))),
            }
            for r in rows
        ]
    )


@router.post(
    "/settlement-requests/{request_id}/approve",
    response_model=APIResponse[dict],
)
async def approve_settlement(
    request_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("deposits", "write")),
):
    r = await SettlementRequest.get(PydanticObjectId(request_id))
    if r is None:
        raise HTTPException(status_code=404, detail="Settlement request not found")
    await assert_user_in_scope(admin, r.user_id)
    if r.status != SettlementStatus.PENDING:
        raise HTTPException(status_code=400, detail="Already processed")

    # Delegated to wallet_service so the floor-to-0 + ledger write +
    # SettlementRequest stamp + WS push all happen atomically (as a
    # unit — Mongo here is single-replica so there's no transaction
    # guard either way).
    try:
        await wallet_service.approve_settlement_request(
            r.id, admin.id, admin_user_code=admin.user_code
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await log_event(
        action=AuditAction.APPROVE,
        entity_type="SettlementRequest",
        entity_id=r.id,
        actor_id=admin.id,
        target_user_id=r.user_id,
        metadata={"requested_amount": str(r.requested_amount)},
    )

    # Fan-out so any other admin watching the Settlement Requests tab
    # sees the row flip from PENDING → APPROVED without F5.
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "settlement_update",
            {
                "event": "approved",
                "user_id": str(r.user_id),
                "request_id": str(r.id),
            },
        )
    except Exception:  # pragma: no cover
        pass

    return APIResponse(data={"id": str(r.id), "status": "APPROVED"})


@router.post(
    "/settlement-requests/{request_id}/reject",
    response_model=APIResponse[dict],
)
async def reject_settlement(
    request_id: str,
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("deposits", "write")),
):
    reason = (payload.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Reason is required")
    r = await SettlementRequest.get(PydanticObjectId(request_id))
    if r is None:
        raise HTTPException(status_code=404, detail="Settlement request not found")
    await assert_user_in_scope(admin, r.user_id)
    if r.status != SettlementStatus.PENDING:
        raise HTTPException(status_code=400, detail="Already processed")

    try:
        await wallet_service.reject_settlement_request(r.id, admin.id, reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    await log_event(
        action=AuditAction.REJECT,
        entity_type="SettlementRequest",
        entity_id=r.id,
        actor_id=admin.id,
        target_user_id=r.user_id,
        metadata={"reason": reason},
    )

    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "settlement_update",
            {
                "event": "rejected",
                "user_id": str(r.user_id),
                "request_id": str(r.id),
            },
        )
    except Exception:  # pragma: no cover
        pass

    return APIResponse(data={"id": str(r.id), "status": "REJECTED"})


# ── Withdrawals ─────────────────────────────────────────────────────
@router.get("/withdrawals", response_model=APIResponse[dict])
async def list_withdrawals(
    admin: CurrentAdmin,
    status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 15,
    _: None = Depends(require_perm("withdrawals", "read")),
):
    """Admin withdrawal inbox. Same shape as `list_deposits` — status
    defaults to None (all statuses) and pagination at 15 rows / page.
    Switched from the older "PENDING" default for the same reason
    (landing on a hidden filter looked like a broken queue).
    """
    page = max(1, page)
    page_size = max(1, min(200, page_size))
    q: dict[str, Any] = {}
    if status:
        q["status"] = status
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

    # Client-wise search (#5) — same as deposits.
    _apply_search_scope(q, await _user_search_ids(search))

    total = await WithdrawalRequest.find(q).count()
    rows = (
        await WithdrawalRequest.find(q)
        .sort("-created_at")
        .skip((page - 1) * page_size)
        .limit(page_size)
        .to_list()
    )
    owner_map = await build_owner_map([r.user_id for r in rows])

    # Batch-lookup wallet balances so admin can see at approval time
    # whether the user actually has enough funds to withdraw.
    from app.models.wallet import Wallet

    wd_user_ids = list({r.user_id for r in rows})
    wd_wallets = (
        await Wallet.find({"user_id": {"$in": wd_user_ids}}).to_list()
        if wd_user_ids
        else []
    )
    wd_wallet_map = {str(w.user_id): w for w in wd_wallets}

    return APIResponse(
        data={
            "items": [
                {
                    "id": str(r.id),
                    "user_id": str(r.user_id),
                    "amount": str(r.amount),
                    "bank": r.bank.model_dump(),
                    "status": r.status.value,
                    "remarks": r.remarks,
                    "utr_number": r.utr_number,
                    "rejection_reason": r.rejection_reason,
                    "created_at": r.created_at,
                    "processed_at": r.processed_at,
                    "user_available_balance": (
                        str(wd_wallet_map[str(r.user_id)].available_balance)
                        if str(r.user_id) in wd_wallet_map
                        else "0"
                    ),
                    "user_settlement_outstanding": (
                        str(wd_wallet_map[str(r.user_id)].settlement_outstanding)
                        if str(r.user_id) in wd_wallet_map
                        else "0"
                    ),
                    **owner_fields(owner_map.get(str(r.user_id))),
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


@router.post("/withdrawals/{withdrawal_id}/approve", response_model=APIResponse[dict])
async def approve_withdrawal(
    withdrawal_id: str,
    payload: ApproveWithdrawalRequest,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("withdrawals", "write")),
):
    r = await WithdrawalRequest.get(PydanticObjectId(withdrawal_id))
    if r is None:
        raise HTTPException(status_code=404, detail="Withdrawal not found")
    await assert_user_in_scope(admin, r.user_id)
    if r.status != WithdrawalStatus.PENDING:
        raise HTTPException(status_code=400, detail="Already processed")

    # ── Balance pre-check ────────────────────────────────────────
    from app.models.wallet import Wallet

    amount = to_decimal(r.amount)
    wallet = await Wallet.find_one(Wallet.user_id == r.user_id)
    if wallet is None:
        raise HTTPException(status_code=400, detail="User wallet not found")
    available = to_decimal(wallet.available_balance)
    if available < amount:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Insufficient balance: user has ₹{available:,.2f} available "
                f"but withdrawal is ₹{amount:,.2f}. "
                f"Reject this request or wait for a deposit."
            ),
        )

    # ── Atomic claim (double-debit guard) ────────────────────────────
    # Same TOCTOU as the deposit approve: a double-click fires two
    # approves that both pass the read-time PENDING check, so the old
    # code debited the wallet twice. Only the request that wins this
    # compare-and-set proceeds to debit; the loser bails out.
    processed_at = now_utc()
    claimed = await WithdrawalRequest.get_motor_collection().find_one_and_update(
        {"_id": r.id, "status": WithdrawalStatus.PENDING.value},
        {
            "$set": {
                "status": WithdrawalStatus.COMPLETED.value,
                "utr_number": payload.utr_number,
                "processed_by": admin.id,
                "processed_at": processed_at,
                "updated_at": processed_at,
            }
        },
    )
    if claimed is None:
        raise HTTPException(status_code=400, detail="Already processed")

    # Debit user wallet
    try:
        await wallet_service.adjust(
            r.user_id,
            -amount,
            transaction_type=TransactionType.WITHDRAWAL,
            narration=f"Withdrawal approved (UTR {payload.utr_number or 'pending'})",
            reference_type="WITHDRAWAL",
            reference_id=str(r.id),
            actor_id=admin.id,
        )
    except Exception:
        # Debit failed after the claim — revert to PENDING so the
        # request returns to the queue instead of showing COMPLETED
        # with no money moved.
        await WithdrawalRequest.get_motor_collection().update_one(
            {"_id": r.id},
            {
                "$set": {
                    "status": WithdrawalStatus.PENDING.value,
                    "utr_number": None,
                    "processed_by": None,
                    "processed_at": None,
                    "updated_at": now_utc(),
                }
            },
        )
        raise

    r.status = WithdrawalStatus.COMPLETED
    r.utr_number = payload.utr_number
    r.processed_by = admin.id
    r.processed_at = processed_at

    await log_event(
        action=AuditAction.APPROVE,
        entity_type="WithdrawalRequest",
        entity_id=r.id,
        actor_id=admin.id,
        target_user_id=r.user_id,
    )
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "withdrawal_update",
            {"event": "approved", "user_id": str(r.user_id), "withdrawal_id": str(r.id)},
        )
    except Exception:  # pragma: no cover
        pass
    return APIResponse(data={"id": str(r.id), "status": r.status.value})


@router.post("/withdrawals/{withdrawal_id}/reject", response_model=APIResponse[dict])
async def reject_withdrawal(
    withdrawal_id: str,
    payload: RejectWithdrawalRequest,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("withdrawals", "write")),
):
    r = await WithdrawalRequest.get(PydanticObjectId(withdrawal_id))
    if r is None:
        raise HTTPException(status_code=404, detail="Withdrawal not found")
    await assert_user_in_scope(admin, r.user_id)
    if r.status != WithdrawalStatus.PENDING:
        raise HTTPException(status_code=400, detail="Already processed")
    r.status = WithdrawalStatus.REJECTED
    r.rejection_reason = payload.rejection_reason
    r.processed_by = admin.id
    r.processed_at = now_utc()
    await r.save()
    await log_event(
        action=AuditAction.REJECT,
        entity_type="WithdrawalRequest",
        entity_id=r.id,
        actor_id=admin.id,
        target_user_id=r.user_id,
    )
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "withdrawal_update",
            {"event": "rejected", "user_id": str(r.user_id), "withdrawal_id": str(r.id)},
        )
    except Exception:  # pragma: no cover
        pass
    # User-side push — same rationale as the deposit reject hook above.
    try:
        import asyncio as _asyncio

        from app.services.push_service import send_to_user as _push_user

        amt_label = f"₹{r.amount}" if r.amount else ""
        reason_label = (
            f" — {payload.rejection_reason}" if payload.rejection_reason else ""
        )
        _asyncio.create_task(
            _push_user(
                r.user_id,
                title="❌ Withdrawal rejected",
                body=f"{amt_label} was rejected{reason_label}",
                url="/wallet",
                tag=f"withdrawal-rejected-{r.id}",
            )
        )
    except Exception:  # pragma: no cover
        pass
    return APIResponse(data={"id": str(r.id), "status": r.status.value})


# ── Company bank accounts ───────────────────────────────────────────
# Scoped by ownership tier:
#   • super-admin owns the platform-default pool (both owner_* IS NULL)
#   • each sub-admin owns their pool (owner_admin_id == sub_admin.id,
#     owner_broker_id IS NULL)
#   • each broker owns their pool (owner_broker_id == broker.id,
#     owner_admin_id may or may not be set — broker is the
#     most-specific owner)
# A user sees only their pool's banks on the deposit form — that filter
# is wired in user-side `/wallet/company-banks` using the cascade
# broker > admin > platform.
def _owner_filter(admin) -> dict:
    if admin.role == UserRole.SUPER_ADMIN:
        return {"owner_admin_id": None, "owner_broker_id": None}
    if admin.role == UserRole.BROKER:
        return {"owner_broker_id": admin.id}
    # ADMIN
    return {"owner_admin_id": admin.id, "owner_broker_id": None}


def _ser_bank(r: CompanyBankAccount, *, editable: bool = True) -> dict:
    """Serialise a bank row. `editable` lets the caller mark inherited rows
    (e.g. parent admin's banks shown to a broker as fallback) so the
    frontend renders them read-only with a clear 'Inherited' badge."""
    return {
        "id": str(r.id),
        "bank_name": r.bank_name,
        "account_holder": r.account_holder,
        "account_number": r.account_number,
        "ifsc_code": r.ifsc_code,
        "upi_id": r.upi_id,
        "qr_code_url": r.qr_code_url,
        "is_active": r.is_active,
        "is_default": r.is_default,
        "owner_admin_id": str(r.owner_admin_id) if r.owner_admin_id else None,
        "owner_broker_id": str(r.owner_broker_id) if r.owner_broker_id else None,
        "editable": editable,
    }


async def _invalidate_company_banks_cache(
    owner_admin_id, owner_broker_id=None
) -> None:
    """Wipe the user-side deposit-form bank cache for the pool that owns
    this row. Keys are namespaced per pool so edits in one pool don't
    flush another. Cache key shape mirrors the cascade in
    /wallet/company-banks: broker:<id> > admin:<id> > default."""
    from app.core.redis_client import cache_delete_pattern

    if owner_broker_id is not None:
        suffix = f"broker:{owner_broker_id}"
    elif owner_admin_id is not None:
        suffix = f"admin:{owner_admin_id}"
    else:
        suffix = "default"
    await cache_delete_pattern(f"wallet:company-banks:{suffix}")


@router.get("/bank-accounts", response_model=APIResponse[list])
async def list_bank_accounts(
    admin: CurrentAdmin, _: None = Depends(require_perm("banks", "read"))
):
    # Broker view: own pool (editable) + parent admin's pool (inherited,
    # read-only). The frontend renders an "Inherited" badge on rows where
    # editable is False so the broker knows those came from their admin
    # and can't be modified from here. If broker has no own banks, the
    # admin's banks still show as fallback so the broker can see what
    # their users see on the deposit form.
    own_rows = await CompanyBankAccount.find(_owner_filter(admin)).to_list()
    items = [_ser_bank(r, editable=True) for r in own_rows]

    if admin.role == UserRole.BROKER and admin.assigned_admin_id is not None:
        inherited = await CompanyBankAccount.find(
            {
                "owner_admin_id": admin.assigned_admin_id,
                "owner_broker_id": None,
            }
        ).to_list()
        items.extend(_ser_bank(r, editable=False) for r in inherited)

    return APIResponse(data=items)


@router.post("/bank-accounts", response_model=APIResponse[dict])
async def create_bank(
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("banks", "write")),
):
    # Owner stamps follow caller role: super-admin → both None;
    # sub-admin → owner_admin_id only; broker → owner_broker_id only.
    owner_admin_id = None
    owner_broker_id = None
    if admin.role == UserRole.ADMIN:
        owner_admin_id = admin.id
    elif admin.role == UserRole.BROKER:
        owner_broker_id = admin.id
    row = CompanyBankAccount(
        bank_name=payload.get("bank_name", ""),
        account_holder=payload.get("account_holder", ""),
        account_number=payload.get("account_number", ""),
        ifsc_code=payload.get("ifsc_code", ""),
        upi_id=payload.get("upi_id"),
        qr_code_url=payload.get("qr_code_url"),
        is_active=bool(payload.get("is_active", True)),
        is_default=bool(payload.get("is_default", False)),
        owner_admin_id=owner_admin_id,
        owner_broker_id=owner_broker_id,
    )
    # Catch the duplicate-key explicitly so the response is a clean
    # 400 WITH cors headers attached (FastAPI's HTTPException flows
    # through the exception handler chain). Without this catch the
    # raw pymongo DuplicateKeyError bubbled out as a 500 without
    # CORS headers, which the browser then rendered as a misleading
    # "CORS policy" error in the console. The new compound index
    # makes this scenario rare (different owners can reuse the same
    # account_number now), but it can still fire within ONE owner
    # accidentally registering the same number twice — and the user
    # deserves a clear message in that case too.
    try:
        await row.insert()
    except Exception as e:
        from pymongo.errors import DuplicateKeyError

        if isinstance(e, DuplicateKeyError) or "duplicate key" in str(e).lower():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Account {row.account_number} is already in your bank list. "
                    "Edit the existing entry instead of adding a duplicate."
                ),
            )
        raise
    await _invalidate_company_banks_cache(owner_admin_id, owner_broker_id)
    return APIResponse(data={"id": str(row.id)})


def _assert_bank_in_scope(r: CompanyBankAccount, admin) -> None:
    """Rejects an admin operating on a bank outside their pool.

    Ownership rules:
      - super-admin owns platform-default rows (both owner_* IS NULL)
      - admin owns rows where owner_admin_id == self.id AND
        owner_broker_id IS NULL (broker pools are independent)
      - broker owns rows where owner_broker_id == self.id
    """
    if admin.role == UserRole.SUPER_ADMIN:
        if r.owner_admin_id is not None or r.owner_broker_id is not None:
            raise HTTPException(
                status_code=403,
                detail="Bank belongs to a sub-admin's or broker's pool",
            )
        return
    if admin.role == UserRole.BROKER:
        if r.owner_broker_id != admin.id:
            raise HTTPException(
                status_code=403, detail="Bank not in your scope"
            )
        return
    # ADMIN
    if r.owner_admin_id != admin.id or r.owner_broker_id is not None:
        raise HTTPException(
            status_code=403, detail="Bank not in your scope"
        )


@router.put("/bank-accounts/{bank_id}", response_model=APIResponse[dict])
async def update_bank(
    bank_id: str,
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("banks", "write")),
):
    r = await CompanyBankAccount.get(PydanticObjectId(bank_id))
    if r is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    _assert_bank_in_scope(r, admin)
    for k in (
        "bank_name",
        "account_holder",
        "account_number",
        "ifsc_code",
        "upi_id",
        "qr_code_url",
        "is_active",
        "is_default",
    ):
        if k in payload:
            setattr(r, k, payload[k])
    await r.save()
    await _invalidate_company_banks_cache(r.owner_admin_id, r.owner_broker_id)
    return APIResponse(data={"id": str(r.id)})


@router.delete("/bank-accounts/{bank_id}", response_model=APIResponse[dict])
async def delete_bank(
    bank_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("banks", "write")),
):
    r = await CompanyBankAccount.get(PydanticObjectId(bank_id))
    if r is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    _assert_bank_in_scope(r, admin)
    owner_admin_id = r.owner_admin_id
    owner_broker_id = r.owner_broker_id
    await r.delete()
    await _invalidate_company_banks_cache(owner_admin_id, owner_broker_id)
    return APIResponse(data={"ok": True})


# ── W/D rules ───────────────────────────────────────────────────────
#
# Tier-aware: each caller reads / writes THEIR OWN tier's override row.
#   role = SUPER_ADMIN  → SuperAdminWdRule (their own pool)
#   role = ADMIN        → SubAdminWdRule (their own pool)
#   role = BROKER       → BrokerWdRule (their own pool)
#
# Super-admin can ALSO edit the platform-global WdRule via the explicit
# `?tier=global` query param — keeps the historical "edit base default"
# affordance for the lone super-admin user.
#
# GET returns BOTH (a) the caller's own override row (sparse — None
# means inherit) AND (b) the resolved effective values via the cascade.
# The frontend uses (a) for the form inputs and (b) for "currently
# applied" hints. Operator spec: "super admin apne user, admin apne
# user, broker apne user — har tier free".


def _admin_tier(admin) -> tuple[str, PydanticObjectId]:
    """Map the caller's role to (tier_name, owner_id) for the WdRule
    cascade. Raises a 400 on roles that don't own end users (DEALER /
    MASTER etc.) so the UI knows to hide the rule editor for them."""
    role = getattr(admin.role, "value", str(admin.role))
    if role == "SUPER_ADMIN":
        return ("super_admin", admin.id)
    if role == "ADMIN":
        return ("admin", admin.id)
    if role == "BROKER":
        return ("broker", admin.id)
    raise HTTPException(
        status_code=403,
        detail=f"Role {role} does not have its own user pool — rule editor not applicable",
    )


@router.get("/wd-rules", response_model=APIResponse[dict])
async def list_wd_rules(admin: CurrentAdmin):
    """Returns the caller's own tier override (for the form) PLUS the
    fully-resolved effective values (for "currently applied" hints).

    Shape:
        {
          "tier": "admin",
          "owner_id": "...",
          "rules": [
            {
              "rule_type": "DEPOSIT",
              "own":       {field: value-or-null, ...},   ← form starts here
              "effective": {field: resolved-value, ...},  ← currently-applied
              "sources":   {field: "broker|admin|super_admin|global", ...}
            },
            {"rule_type": "WITHDRAWAL", ...}
          ]
        }
    """
    from app.services import wd_rules_service
    from app.models.transaction import (
        BrokerWdRule as _BrokerWdRule,
        SubAdminWdRule as _SubAdminWdRule,
        SuperAdminWdRule as _SuperAdminWdRule,
    )

    tier, owner_id = _admin_tier(admin)

    def _serialize_own(row) -> dict[str, Any]:
        if row is None:
            return {f: None for f in wd_rules_service._RULE_FIELDS}
        d: dict[str, Any] = {}
        for f in wd_rules_service._RULE_FIELDS:
            v = getattr(row, f, None)
            if v is None:
                d[f] = None
            elif f == "allowed_days":
                d[f] = list(v) if v else None
            elif f == "allowed_times":
                d[f] = [w.model_dump() for w in v] if v else None
            elif f == "charges_percent":
                d[f] = float(v)
            elif f in (
                "mandatory_remark",
                "block_withdrawal_with_open_positions",
                "block_duplicate_pending",
                "require_bank_details",
            ):
                d[f] = bool(v)
            elif f == "max_requests_per_day":
                d[f] = int(v)
            else:
                d[f] = str(v)
        return d

    def _serialize_effective(values: dict[str, Any]) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for f, v in values.items():
            if v is None:
                d[f] = None
            elif f == "allowed_days":
                d[f] = list(v) if v else None
            elif f == "allowed_times":
                d[f] = [
                    w.model_dump() if hasattr(w, "model_dump") else dict(w)
                    for w in v
                ] if v else None
            elif f == "charges_percent":
                d[f] = float(v)
            elif f in (
                "mandatory_remark",
                "block_withdrawal_with_open_positions",
                "block_duplicate_pending",
                "require_bank_details",
            ):
                d[f] = bool(v)
            elif f == "max_requests_per_day":
                d[f] = int(v)
            else:
                d[f] = str(v)
        return d

    results: list[dict[str, Any]] = []
    for rule_type in ("DEPOSIT", "WITHDRAWAL"):
        # Caller's own tier override row (may not exist yet).
        own_row = None
        if tier == "super_admin":
            own_row = await _SuperAdminWdRule.find_one(
                _SuperAdminWdRule.super_admin_id == owner_id,
                _SuperAdminWdRule.rule_type == rule_type,
            )
        elif tier == "admin":
            own_row = await _SubAdminWdRule.find_one(
                _SubAdminWdRule.sub_admin_id == owner_id,
                _SubAdminWdRule.rule_type == rule_type,
            )
        elif tier == "broker":
            own_row = await _BrokerWdRule.find_one(
                _BrokerWdRule.broker_id == owner_id,
                _BrokerWdRule.rule_type == rule_type,
            )

        # Effective view — pick ANY user the caller owns and resolve
        # against them. For super-admin / fresh admin with no users yet,
        # resolve against the caller's own id (acts as a no-user-pool
        # probe — returns the cascade as if a hypothetical user under
        # this admin asked for the effective rule).
        from app.models.user import User as _User
        scope_user = None
        if tier == "admin":
            scope_user = await _User.find_one(_User.assigned_admin_id == owner_id)
        elif tier == "broker":
            scope_user = await _User.find_one({"broker_ancestry": owner_id})
        # super_admin: no scoping needed — global fallback drives the
        # value if no broker / admin / super-admin override is set.

        # If no representative user exists, just use a synthetic-ish
        # cascade by passing the admin's own id; the resolver will fall
        # through all tiers cleanly with the admin's broker/admin
        # ancestry empty.
        probe_id = scope_user.id if scope_user is not None else owner_id

        with_sources = await wd_rules_service.get_effective_rule_with_sources(
            probe_id, rule_type
        )

        results.append({
            "rule_type": rule_type,
            "own": _serialize_own(own_row),
            "effective": _serialize_effective(with_sources["values"]),
            "sources": with_sources["sources"],
        })

    return APIResponse(data={
        "tier": tier,
        "owner_id": str(owner_id),
        "rules": results,
    })


@router.put("/wd-rules/{rule_type}", response_model=APIResponse[dict])
async def update_wd_rule(
    rule_type: str,
    payload: dict,
    admin: CurrentAdmin,
    tier: str | None = None,
):
    """Save the caller's tier-specific override for `rule_type`.

    Body is sparse — only the fields the admin wants to set / clear.
    Sending `null` for a field explicitly REMOVES the override at this
    tier (so the field starts inheriting from the layer below). Sending
    a value SETS it.

    Super-admin can pass `?tier=global` to edit the platform default
    instead of their own super-admin override row.
    """
    if rule_type not in ("DEPOSIT", "WITHDRAWAL"):
        raise HTTPException(status_code=400, detail="rule_type must be DEPOSIT or WITHDRAWAL")

    # Resolve target tier + owner_id.
    role = getattr(admin.role, "value", str(admin.role))
    if tier == "global":
        if role != "SUPER_ADMIN":
            raise HTTPException(
                status_code=403,
                detail="Only super-admin can edit the platform-global rule",
            )
        target_tier = "global"
        owner_id: PydanticObjectId | None = None
    else:
        target_tier, owner_id = _admin_tier(admin)

    from app.services import wd_rules_service
    from app.models.audit_log import AuditAction
    from app.services.audit_service import log_event

    try:
        result = await wd_rules_service.upsert_for_tier(
            rule_type=rule_type,
            tier=target_tier,
            owner_id=owner_id,
            payload=payload,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="WdRule",
        entity_id=None,
        actor_id=admin.id,
        target_user_id=None,
        metadata={
            "tier": target_tier,
            "rule_type": rule_type,
            "owner_id": str(owner_id) if owner_id else None,
            "payload": {k: (str(v) if v is not None else None) for k, v in payload.items()},
        },
    )
    return APIResponse(data={"ok": True, "tier": target_tier, "rule_type": rule_type})
