"""Super-admin-only management surface.

Creates and configures sub-admins, assigns users to them, and runs the
weekly P&L-share settlement. Existing /api/v1/admin/* endpoints are
untouched in behavior — they only gain a scoping wrapper that applies to
ADMIN-role callers (handled in those routers, not here).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from typing import Annotated

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.dependencies import SuperAdmin, require_perm
from app.models.user import User, UserRole
from app.schemas.admin.management import (
    AssignUserRequest,
    BulkAssignRequest,
    CreateSubAdminRequest,
    MarkPaidRequest,
    RecomputeSettlementRequest,
    ResetPasswordRequest,
    SetMaintenanceRequest,
    SettlementDTO,
    SubAdminDTO,
    UpdatePermissionsRequest,
    UpdatePnlShareRequest,
    UpdateSubAdminRequest,
)
from app.schemas.common import APIResponse
from app.services import admin_management_service as mgmt
from app.services import admin_settlement_service as stl
from app.services import user_service
from app.utils.time_utils import IST

router = APIRouter(prefix="/management", tags=["admin-management"])


# ── Serialization helpers ────────────────────────────────────────────
async def _ser_sub_admin(sa: User) -> SubAdminDTO:
    return SubAdminDTO(
        id=str(sa.id),
        user_code=sa.user_code,
        full_name=sa.full_name,
        email=sa.email,
        mobile=sa.mobile,
        status=sa.status.value,
        permissions=sa.admin_permissions,
        pnl_share_pct=str(sa.pnl_share_pct) if sa.pnl_share_pct is not None else "0",
        user_count=await mgmt.count_assigned_users(sa.id),
        broker_count=await mgmt.count_assigned_brokers(sa.id),
        maintenance_mode=bool(getattr(sa, "maintenance_mode", False)),
        payment_gateway_enabled=bool(getattr(sa, "payment_gateway_enabled", False)),
        created_at=sa.created_at,
    )


def _ser_settlement(row, sa: User | None) -> SettlementDTO:
    return SettlementDTO(
        id=str(row.id),
        sub_admin_id=str(row.sub_admin_id),
        sub_admin_name=sa.full_name if sa else None,
        sub_admin_code=sa.user_code if sa else None,
        period_start=row.period_start,
        period_end=row.period_end,
        user_count=row.user_count,
        gross_user_loss_inr=str(row.gross_user_loss_inr),
        gross_user_profit_inr=str(row.gross_user_profit_inr),
        total_brokerage_inr=str(row.total_brokerage_inr),
        net_house_pnl_inr=str(row.net_house_pnl_inr),
        pnl_share_pct_snapshot=str(row.pnl_share_pct_snapshot),
        sub_admin_share_inr=str(row.sub_admin_share_inr),
        status=row.status.value,
        finalized_at=row.finalized_at,
        paid_at=row.paid_at,
        notes=row.notes,
        frozen=row.is_frozen(),
    )


# ── Sub-admin CRUD ───────────────────────────────────────────────────
@router.get("/sub-admins", response_model=APIResponse[dict])
async def list_sub_admins(
    admin: SuperAdmin,
    q: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    rows, total = await mgmt.list_sub_admins(
        status=status, q=q, page=page, page_size=page_size
    )
    items = [await _ser_sub_admin(sa) for sa in rows]
    return APIResponse(
        data={
            "items": items,
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
            },
        }
    )


@router.post("/sub-admins", response_model=APIResponse[SubAdminDTO])
async def create_sub_admin(payload: CreateSubAdminRequest, admin: SuperAdmin):
    sa = await mgmt.create_sub_admin(
        email=payload.email,
        mobile=payload.mobile,
        password=payload.password,
        full_name=payload.full_name,
        permissions=payload.permissions,
        pnl_share_pct=payload.pnl_share_pct,
        created_by=admin.id,
    )
    return APIResponse(data=await _ser_sub_admin(sa))


@router.get("/sub-admins/{sub_admin_id}", response_model=APIResponse[SubAdminDTO])
async def get_sub_admin(sub_admin_id: str, admin: SuperAdmin):
    sa = await mgmt._get_sub_admin_or_404(sub_admin_id)
    return APIResponse(data=await _ser_sub_admin(sa))


@router.put("/sub-admins/{sub_admin_id}", response_model=APIResponse[SubAdminDTO])
async def update_sub_admin(
    sub_admin_id: str, payload: UpdateSubAdminRequest, admin: SuperAdmin
):
    sa = await mgmt.update_sub_admin(
        sub_admin_id, full_name=payload.full_name, actor_id=admin.id
    )
    return APIResponse(data=await _ser_sub_admin(sa))


@router.put(
    "/sub-admins/{sub_admin_id}/permissions", response_model=APIResponse[SubAdminDTO]
)
async def update_permissions(
    sub_admin_id: str, payload: UpdatePermissionsRequest, admin: SuperAdmin
):
    sa = await mgmt.update_permissions(sub_admin_id, payload.permissions, admin.id)
    return APIResponse(data=await _ser_sub_admin(sa))


@router.put(
    "/sub-admins/{sub_admin_id}/pnl-share", response_model=APIResponse[SubAdminDTO]
)
async def update_pnl_share(
    sub_admin_id: str, payload: UpdatePnlShareRequest, admin: SuperAdmin
):
    sa = await mgmt.set_pnl_share(sub_admin_id, payload.pct, admin.id)
    return APIResponse(data=await _ser_sub_admin(sa))


@router.post(
    "/sub-admins/{sub_admin_id}/block", response_model=APIResponse[SubAdminDTO]
)
async def block_sub_admin(sub_admin_id: str, admin: SuperAdmin):
    sa = await mgmt.block_sub_admin(sub_admin_id, admin.id)
    return APIResponse(data=await _ser_sub_admin(sa))


@router.post(
    "/sub-admins/{sub_admin_id}/unblock", response_model=APIResponse[SubAdminDTO]
)
async def unblock_sub_admin(sub_admin_id: str, admin: SuperAdmin):
    sa = await mgmt.unblock_sub_admin(sub_admin_id, admin.id)
    return APIResponse(data=await _ser_sub_admin(sa))


@router.post(
    "/sub-admins/{sub_admin_id}/maintenance",
    response_model=APIResponse[SubAdminDTO],
)
async def set_sub_admin_maintenance(
    sub_admin_id: str, body: SetMaintenanceRequest, admin: SuperAdmin
):
    """Turn maintenance mode ON/OFF for this admin's whole pool. ON blocks every
    non-admin user's login and kicks live sessions; OFF restores normal access."""
    sa = await mgmt.set_maintenance(sub_admin_id, body.enabled, admin.id)
    return APIResponse(data=await _ser_sub_admin(sa))


@router.put(
    "/sub-admins/{sub_admin_id}/payment-gateway",
    response_model=APIResponse[SubAdminDTO],
)
async def set_sub_admin_payment_gateway(
    sub_admin_id: str, body: SetMaintenanceRequest, admin: SuperAdmin
):
    """Turn the Divinepay UPI gateway ON/OFF for this admin's pool. ON → their
    users get the auto-crediting online pay-in; OFF → the manual deposit flow."""
    sa = await mgmt.set_payment_gateway(sub_admin_id, body.enabled, admin.id)
    return APIResponse(data=await _ser_sub_admin(sa))


@router.delete(
    "/sub-admins/{sub_admin_id}",
    response_model=APIResponse[dict],
)
async def delete_sub_admin(
    sub_admin_id: PydanticObjectId,
    actor: SuperAdmin,
):
    """Permanently delete a sub-admin (super-admin only). Reassigns the
    sub-admin's users back to the platform pool, ENDs any active P&L
    sharing agreements, then removes the user row."""
    await mgmt.delete_sub_admin(sub_admin_id, actor_id=actor.id)
    return APIResponse(data={"deleted": str(sub_admin_id)})


@router.post(
    "/sub-admins/{sub_admin_id}/reset-password",
    response_model=APIResponse[dict],
)
async def reset_sub_admin_password(
    sub_admin_id: PydanticObjectId,
    body: ResetPasswordRequest,
    actor: SuperAdmin,
):
    """Reset a sub-admin's password (super-admin only)."""
    await mgmt.reset_password(sub_admin_id, body.new_password, actor_id=actor.id)
    return APIResponse(data={"reset": str(sub_admin_id)})


@router.get(
    "/sub-admins/{sub_admin_id}/users", response_model=APIResponse[dict]
)
async def list_users_of_sub_admin(
    sub_admin_id: str,
    admin: SuperAdmin,
    page: int = 1,
    page_size: int = 50,
):
    rows, total = await mgmt.list_assigned_users(
        sub_admin_id, page=page, page_size=page_size
    )
    items = [
        {
            "id": str(u.id),
            "user_code": u.user_code,
            "email": u.email,
            "mobile": u.mobile,
            "full_name": u.full_name,
            "role": u.role.value,
            "status": u.status.value,
            "created_at": u.created_at,
        }
        for u in rows
    ]
    return APIResponse(
        data={
            "items": items,
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
            },
        }
    )


# ── User assignment ──────────────────────────────────────────────────
@router.post("/users/{user_id}/assign", response_model=APIResponse[dict])
async def assign_user(
    user_id: str, payload: AssignUserRequest, admin: SuperAdmin
):
    target = await mgmt.reassign_user(user_id, payload.sub_admin_id, admin.id)
    return APIResponse(
        data={
            "id": str(target.id),
            "assigned_admin_id": (
                str(target.assigned_admin_id) if target.assigned_admin_id else None
            ),
        }
    )


@router.post("/users/bulk-assign", response_model=APIResponse[dict])
async def bulk_assign_users(payload: BulkAssignRequest, admin: SuperAdmin):
    result = await mgmt.bulk_reassign(
        payload.user_ids, payload.sub_admin_id, admin.id
    )
    return APIResponse(data=result)


# ── Cross-admin transfer (delegated to permitted admins) ─────────────
# Lets a NON-super admin move THEIR OWN users to another admin, gated by the
# `transfer_users` permission. Super-admin already has this via /users/{id}/
# assign; these endpoints are the admin-facing, scope-checked equivalent that
# power the "Transfer User" sidebar section.
TransferAdmin = Annotated[User, Depends(require_perm("transfer_users"))]


class TransferToAdminRequest(BaseModel):
    target_admin_id: str


class TransferBulkRequest(BaseModel):
    user_ids: list[str]
    target_admin_id: str


async def _assert_owns(admin: User, target: User) -> bool:
    if admin.role == UserRole.SUPER_ADMIN:
        return True
    return target.assigned_admin_id == admin.id or admin.id in (
        getattr(target, "broker_ancestry", None) or []
    )


@router.get("/transfer/admins", response_model=APIResponse[dict])
async def transfer_admin_targets(admin: TransferAdmin):
    """Every admin on the platform (except the caller) as a transfer target."""
    rows = await User.find(User.role == UserRole.ADMIN).to_list()
    items = [
        {
            "id": str(a.id),
            "user_code": a.user_code,
            "full_name": a.full_name,
            "email": a.email,
        }
        for a in rows
        if a.id != admin.id
    ]
    return APIResponse(data={"items": items})


@router.post("/transfer/to-admin/{user_id}", response_model=APIResponse[dict])
async def transfer_user_to_admin(
    user_id: str, body: TransferToAdminRequest, admin: TransferAdmin
):
    """Move one of the caller's users to another admin. Ownership, positions,
    wallet, and trade history all travel with the user; the user then logs in
    under the new admin's link only."""
    target = await user_service.get_user_or_404(user_id)
    if not await _assert_owns(admin, target):
        raise HTTPException(status_code=403, detail="This user isn't in your pool")
    if str(body.target_admin_id) == str(admin.id):
        raise HTTPException(status_code=400, detail="Pick a different admin")
    moved = await mgmt.reassign_user(user_id, body.target_admin_id, admin.id)
    return APIResponse(
        data={
            "id": str(moved.id),
            "assigned_admin_id": str(moved.assigned_admin_id) if moved.assigned_admin_id else None,
        }
    )


@router.post("/transfer/to-admin-bulk", response_model=APIResponse[dict])
async def transfer_users_to_admin_bulk(body: TransferBulkRequest, admin: TransferAdmin):
    """Move several of the caller's users to another admin in one go. Each user
    is scope-checked; out-of-pool / bad ids are skipped, not fatal."""
    if str(body.target_admin_id) == str(admin.id):
        raise HTTPException(status_code=400, detail="Pick a different admin")
    moved = 0
    skipped = 0
    for uid in body.user_ids:
        try:
            target = await user_service.get_user_or_404(uid)
            if not await _assert_owns(admin, target):
                skipped += 1
                continue
            await mgmt.reassign_user(uid, body.target_admin_id, admin.id)
            moved += 1
        except Exception:
            skipped += 1
    return APIResponse(data={"transferred": moved, "skipped": skipped})


# ── Settlements ──────────────────────────────────────────────────────
@router.get("/settlements", response_model=APIResponse[dict])
async def list_settlements(admin: SuperAdmin, week_start: date | None = None):
    if week_start is None:
        anchor = datetime.now(IST)
    else:
        anchor = datetime.combine(week_start, datetime.min.time(), tzinfo=IST)
    rows = await stl.list_settlements_for_week(anchor, actor_id=admin.id)
    period_start, period_end = stl.ist_week_bounds(anchor)
    items = [_ser_settlement(r, sa) for (r, sa) in rows]
    total_users = sum(r.user_count for (r, _) in rows)
    total_net = sum(
        (r.net_house_pnl_inr.to_decimal() for (r, _) in rows), start=Decimal("0")
    )
    total_share = sum(
        (r.sub_admin_share_inr.to_decimal() for (r, _) in rows), start=Decimal("0")
    )
    return APIResponse(
        data={
            "period_start": period_start,
            "period_end": period_end,
            "items": items,
            "totals": {
                "user_count": total_users,
                "net_house_pnl_inr": str(total_net),
                "sub_admin_share_inr": str(total_share),
            },
        }
    )


@router.get(
    "/settlements/sub-admin/{sub_admin_id}", response_model=APIResponse[dict]
)
async def history_for_sub_admin(
    sub_admin_id: str,
    admin: SuperAdmin,
    from_date: date | None = None,
    to_date: date | None = None,
):
    sa = await mgmt._get_sub_admin_or_404(sub_admin_id)
    rows = await stl.history_for_sub_admin(
        sub_admin_id, from_date=from_date, to_date=to_date
    )
    return APIResponse(
        data={
            "sub_admin": {
                "id": str(sa.id),
                "user_code": sa.user_code,
                "full_name": sa.full_name,
            },
            "items": [_ser_settlement(r, sa) for r in rows],
        }
    )


@router.post("/settlements/recompute", response_model=APIResponse[dict])
async def recompute_settlements(
    payload: RecomputeSettlementRequest, admin: SuperAdmin
):
    anchor = datetime.combine(payload.week_start, datetime.min.time(), tzinfo=IST)
    if payload.sub_admin_id:
        row, frozen = await stl.compute_settlement(
            payload.sub_admin_id, anchor, actor_id=admin.id
        )
        sa = await mgmt._get_sub_admin_or_404(payload.sub_admin_id)
        return APIResponse(
            data={
                "items": [_ser_settlement(row, sa)],
                "frozen_skipped": 1 if frozen else 0,
            }
        )

    results = await stl.compute_all_for_week(anchor, actor_id=admin.id)
    # Re-fetch sub-admin docs for naming
    sub_admins = await User.find({"role": "ADMIN"}).to_list()
    by_id = {sa.id: sa for sa in sub_admins}
    return APIResponse(
        data={
            "items": [
                _ser_settlement(row, by_id.get(row.sub_admin_id)) for (row, _) in results
            ],
            "frozen_skipped": sum(1 for (_, frozen) in results if frozen),
        }
    )


@router.post(
    "/settlements/{settlement_id}/finalize", response_model=APIResponse[SettlementDTO]
)
async def finalize_settlement(settlement_id: str, admin: SuperAdmin):
    row = await stl.finalize(settlement_id, admin.id)
    sa = await User.get(PydanticObjectId(row.sub_admin_id))
    return APIResponse(data=_ser_settlement(row, sa))


@router.post(
    "/settlements/{settlement_id}/mark-paid", response_model=APIResponse[SettlementDTO]
)
async def mark_paid_settlement(
    settlement_id: str, payload: MarkPaidRequest, admin: SuperAdmin
):
    row = await stl.mark_paid(settlement_id, admin.id, notes=payload.notes)
    sa = await User.get(PydanticObjectId(row.sub_admin_id))
    return APIResponse(data=_ser_settlement(row, sa))


# ── Sub-admin detail report ──────────────────────────────────────────
@router.get("/sub-admins/{sub_admin_id}/report", response_model=APIResponse[dict])
async def sub_admin_report(sub_admin_id: str, admin: SuperAdmin):
    """Aggregated overview of one sub-admin's pool.

    Returns: user count + active count, total deposits / withdrawals / brokerage
    across their pool, today / this-week / all-time realised PNL, today &
    week trade count, current open positions count + unrealised PNL, and
    the most recent 10 trades. Drives the super-admin's detail page.
    """
    from datetime import timedelta as _td
    from decimal import Decimal as _D

    from app.models.position import Position, PositionStatus
    from app.models.trade import Trade
    from app.models.transaction import (
        TransactionType,
        WalletTransaction,
    )
    from app.models.user import UserStatus
    from app.models.wallet import Wallet
    from app.services import market_data_service
    from app.utils.decimal_utils import to_decimal
    from app.utils.time_utils import now_utc

    sa = await mgmt._get_sub_admin_or_404(sub_admin_id)

    from app.core.dependencies import _NON_CLIENT_ROLES, _admin_pool_clause
    from app.models.user import UserRole as _UR

    # Resolve the trading-client pool comprehensively (directly-assigned +
    # whole broker subtree), broker/sub-broker LOGIN rows excluded. All
    # statuses are kept here so wallet / PNL / trade rollups still cover
    # closed users' history; the counts below split active vs non-closed.
    coll = User.get_motor_collection()
    clause = await _admin_pool_clause(sa.id)
    cursor = coll.find(
        {**clause, "role": {"$nin": _NON_CLIENT_ROLES}},
        {"_id": 1, "status": 1},
    )
    pool: list[PydanticObjectId] = []
    active_count = 0
    non_closed_count = 0
    async for doc in cursor:
        pool.append(doc["_id"])
        st = doc.get("status")
        if st == UserStatus.ACTIVE.value:
            active_count += 1
        if st != UserStatus.CLOSED.value:
            non_closed_count += 1
    # Broker / sub-broker login accounts under this admin, shown separately
    # from the client count (CLOSED excluded).
    broker_count = await coll.count_documents(
        {**clause, "role": _UR.BROKER.value, "status": {"$ne": UserStatus.CLOSED.value}}
    )

    if not pool:
        return APIResponse(
            data={
                "sub_admin": await _ser_sub_admin(sa),
                "user_count": 0,
                "active_user_count": 0,
                "broker_count": broker_count,
                "wallet": {
                    "available_balance": "0",
                    "used_margin": "0",
                    "credit_limit": "0",
                    "total_deposits": "0",
                    "total_withdrawals": "0",
                    "total_brokerage": "0",
                },
                "pnl": {
                    "today_realised": "0",
                    "week_realised": "0",
                    "all_time_realised": "0",
                    "open_unrealised": "0",
                },
                "trades": {
                    "today": 0,
                    "this_week": 0,
                    "all_time": 0,
                },
                "open_positions": 0,
                "recent_trades": [],
                "deposits_week": "0",
                "withdrawals_week": "0",
            }
        )

    user_q = {"user_id": {"$in": pool}}

    # IST week boundary (Mon 00:00 IST → now)
    now_utc_dt = now_utc()
    ist_now = now_utc_dt.astimezone(IST)
    today_ist_start = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ist_start = today_ist_start - _td(days=ist_now.weekday())
    today_start = today_ist_start.astimezone(now_utc_dt.tzinfo)
    week_start = week_ist_start.astimezone(now_utc_dt.tzinfo)

    # Wallet rollup across the pool
    wallets = await Wallet.find(user_q).to_list()
    wallet_sum = {
        "available_balance": _D("0"),
        "used_margin": _D("0"),
        "credit_limit": _D("0"),
        "total_deposits": _D("0"),
        "total_withdrawals": _D("0"),
        "total_brokerage": _D("0"),
    }
    for w in wallets:
        wallet_sum["available_balance"] += to_decimal(w.available_balance)
        wallet_sum["used_margin"] += to_decimal(w.used_margin)
        wallet_sum["credit_limit"] += to_decimal(w.credit_limit)
        wallet_sum["total_deposits"] += to_decimal(w.total_deposits)
        wallet_sum["total_withdrawals"] += to_decimal(w.total_withdrawals)
        wallet_sum["total_brokerage"] += to_decimal(w.total_brokerage)

    # Realised PNL by window (positions closed in window). Reuses the
    # USD-quoted FX-conversion pattern from /admin/users/{id}/live-trade-stats.
    fallback_usd_inr = to_decimal(market_data_service.get_usd_inr_rate())

    def _is_usd(p) -> bool:
        return market_data_service.is_usd_quoted_segment(p.segment_type) or (
            p.instrument
            and market_data_service.is_usd_quoted_segment(p.instrument.segment)
        )

    def _realised_inr(p) -> _D:
        raw = to_decimal(p.realized_pnl)
        if not _is_usd(p):
            return raw
        rate = (
            to_decimal(p.open_usd_inr_rate)
            if p.open_usd_inr_rate is not None
            else fallback_usd_inr
        )
        return raw * rate

    async def _realised_in(window_start):
        rows = await Position.find(
            {
                **user_q,
                "status": PositionStatus.CLOSED.value,
                "closed_at": {"$gte": window_start},
            }
        ).to_list()
        return sum((_realised_inr(p) for p in rows), _D("0"))

    today_realised = await _realised_in(today_start)
    week_realised = await _realised_in(week_start)
    all_closed = await Position.find(
        {**user_q, "status": PositionStatus.CLOSED.value}
    ).to_list()
    all_time_realised = sum((_realised_inr(p) for p in all_closed), _D("0"))

    # Open unrealised — sum of stored unrealized_pnl on currently OPEN positions
    open_positions = await Position.find(
        {**user_q, "status": PositionStatus.OPEN.value}
    ).to_list()
    open_unrealised = _D("0")
    for p in open_positions:
        raw = to_decimal(p.unrealized_pnl)
        if _is_usd(p):
            raw *= fallback_usd_inr
        open_unrealised += raw

    # Trades
    today_trades_count = await Trade.find(
        {**user_q, "executed_at": {"$gte": today_start}}
    ).count()
    week_trades_count = await Trade.find(
        {**user_q, "executed_at": {"$gte": week_start}}
    ).count()
    all_time_trades_count = await Trade.find(user_q).count()
    recent_trades_rows = (
        await Trade.find(user_q).sort("-executed_at").limit(10).to_list()
    )

    # Per-row user_code map for the recent trades table
    trade_user_ids = list({t.user_id for t in recent_trades_rows})
    users_for_trades = (
        await User.find({"_id": {"$in": trade_user_ids}}).to_list()
        if trade_user_ids
        else []
    )
    code_map = {str(u.id): u.user_code for u in users_for_trades}

    # Weekly deposit / withdrawal flow (this IST week)
    dep_txns = await WalletTransaction.find(
        {
            **user_q,
            "transaction_type": TransactionType.DEPOSIT.value,
            "created_at": {"$gte": week_start},
        }
    ).to_list()
    wd_txns = await WalletTransaction.find(
        {
            **user_q,
            "transaction_type": TransactionType.WITHDRAWAL.value,
            "created_at": {"$gte": week_start},
        }
    ).to_list()
    dep_week = sum((abs(to_decimal(t.amount)) for t in dep_txns), _D("0"))
    wd_week = sum((abs(to_decimal(t.amount)) for t in wd_txns), _D("0"))

    return APIResponse(
        data={
            "sub_admin": await _ser_sub_admin(sa),
            "user_count": non_closed_count,
            "active_user_count": active_count,
            "broker_count": broker_count,
            "wallet": {k: str(v) for k, v in wallet_sum.items()},
            "pnl": {
                "today_realised": str(today_realised),
                "week_realised": str(week_realised),
                "all_time_realised": str(all_time_realised),
                "open_unrealised": str(open_unrealised),
            },
            "trades": {
                "today": today_trades_count,
                "this_week": week_trades_count,
                "all_time": all_time_trades_count,
            },
            "open_positions": len(open_positions),
            "deposits_week": str(dep_week),
            "withdrawals_week": str(wd_week),
            "recent_trades": [
                {
                    "id": str(t.id),
                    "user_id": str(t.user_id),
                    "user_code": code_map.get(str(t.user_id)),
                    "symbol": t.instrument.symbol,
                    "exchange": str(t.instrument.exchange),
                    "action": t.action.value,
                    "quantity": t.quantity,
                    "price": str(t.price),
                    "value": str(t.value),
                    "brokerage": str(t.brokerage),
                    "executed_at": t.executed_at,
                }
                for t in recent_trades_rows
            ],
        }
    )


# ── Login As sub-admin (impersonate) ─────────────────────────────────
@router.post(
    "/sub-admins/{sub_admin_id}/impersonate", response_model=APIResponse[dict]
)
async def impersonate_sub_admin(sub_admin_id: str, admin: SuperAdmin):
    """Mint admin-side tokens for the target sub-admin.

    Super-admin only. Returns an `admin` token pair shaped like the
    `/admin/auth/login` response so the frontend can drop them straight
    into its admin auth store and load the sub-admin's dashboard.
    Audited as `IMPERSONATE` with `kind: SUB_ADMIN` in metadata.
    """
    from app.core.config import settings as cfg
    from app.core.redis_client import cache_set
    from app.core.security import (
        create_access_token,
        create_refresh_token,
        refresh_jti_key,
        session_key,
    )
    from app.models.audit_log import AuditAction
    from app.services.audit_service import log_event

    target = await mgmt._get_sub_admin_or_404(sub_admin_id)

    access = create_access_token(
        user_id=str(target.id),
        role=target.role.value,
        extra={"impersonator": str(admin.id), "ver": int(target.token_version or 0)},
    )
    refresh, jti = create_refresh_token(
        user_id=str(target.id), role=target.role.value
    )
    await cache_set(
        refresh_jti_key(str(target.id), jti),
        {
            "user_id": str(target.id),
            "audience": "admin",
            "impersonator": str(admin.id),
        },
        ttl_sec=cfg.JWT_REFRESH_TTL_DAYS * 86400,
    )
    await cache_set(
        session_key(str(target.id), jti),
        {"audience": "admin", "impersonator": str(admin.id)},
        ttl_sec=cfg.JWT_REFRESH_TTL_DAYS * 86400,
    )

    await log_event(
        action=AuditAction.IMPERSONATE,
        entity_type="User",
        entity_id=target.id,
        actor_id=admin.id,
        target_user_id=target.id,
        metadata={"as_role": target.role.value, "kind": "SUB_ADMIN"},
    )

    # Picks the FIRST canonical origin if CORS_ADMIN_ORIGIN holds a
    # comma-separated list (same approach as user-side impersonate).
    admin_origin = (cfg.CORS_ADMIN_ORIGIN or "").split(",")[0].strip()

    return APIResponse(
        data={
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": cfg.JWT_ACCESS_TTL_MIN * 60,
            "admin": {
                "id": str(target.id),
                "user_code": target.user_code,
                "email": target.email,
                "full_name": target.full_name,
                "role": target.role.value,
                "admin_permissions": (
                    target.admin_permissions.model_dump()
                    if target.admin_permissions is not None
                    else None
                ),
                "pnl_share_pct": (
                    str(target.pnl_share_pct)
                    if target.pnl_share_pct is not None
                    else None
                ),
            },
            "admin_app_url": admin_origin,
        }
    )
