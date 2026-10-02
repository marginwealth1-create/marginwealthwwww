"""Broker management surface — under /api/v1/admin/management/brokers.

Visible to:
  - SUPER_ADMIN: always
  - ADMIN: if admin_permissions.brokers == True
  - BROKER: if broker_permissions.sub_brokers >= VIEW (sub-broker mgmt
    is the broker's view of this same router; backend uses scoping +
    cap-validation to keep it safe)

Existing /admin/* surface is byte-identical for everyone — this is a
separate router mounted alongside /management/sub-admins.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.dependencies import (
    CurrentAdmin,
    assert_broker_in_scope,
    max_grantable_perms,
    require_perm,
)
from app.models.user import User, UserRole
from app.schemas.admin.brokers import (
    AssignUserToBrokerRequest,
    BrokerDTO,
    BrokerSettlementDTO,
    BulkAssignToBrokerRequest,
    CreateBrokerRequest,
    MarkPaidBrokerRequest,
    MaxGrantableDTO,
    RecomputeBrokerSettlementRequest,
    UpdateBrokerPermissionsRequest,
    UpdateBrokerPnlShareRequest,
    UpdateBrokerRequest,
)
from app.schemas.admin.management import ResetPasswordRequest
from app.schemas.common import APIResponse
from app.services import broker_management_service as svc
from app.services import broker_settlement_service as stl
from app.utils.time_utils import IST

router = APIRouter(prefix="/management", tags=["admin-brokers"])


# Permission dep used for the broker mgmt surface. Super-admin + admin
# (with `brokers` flag) pass via the standard admin pathway; broker
# (acting on sub-brokers) needs `sub_brokers` at the requested level.
def _brokers_read():
    return require_perm("brokers", "read")


def _brokers_write():
    return require_perm("brokers", "write")


# ── Serialization helpers ────────────────────────────────────────────
async def _ser_broker(b: User) -> BrokerDTO:
    return BrokerDTO(
        id=str(b.id),
        user_code=b.user_code,
        full_name=b.full_name,
        email=b.email,
        mobile=b.mobile,
        status=b.status.value,
        permissions=b.broker_permissions,
        pnl_share_pct=(
            str(b.broker_pnl_share_pct)
            if b.broker_pnl_share_pct is not None
            else "0"
        ),
        brokerage_share_pct=(
            str(b.broker_brokerage_share_pct)
            if b.broker_brokerage_share_pct is not None
            # Back-compat: pre-split brokers shared brokerage at the PnL %.
            else (str(b.broker_pnl_share_pct) if b.broker_pnl_share_pct is not None else "0")
        ),
        user_count=await svc.count_assigned_users(b.id),
        subtree_user_count=await svc.count_subtree_users(b.id),
        broker_ancestry=[str(x) for x in (b.broker_ancestry or [])],
        assigned_admin_id=str(b.assigned_admin_id) if b.assigned_admin_id else None,
        assigned_broker_id=str(b.assigned_broker_id) if b.assigned_broker_id else None,
        payment_gateway_enabled=bool(getattr(b, "payment_gateway_enabled", False)),
        created_at=b.created_at,
    )


def _ser_settlement(row, br: User | None) -> BrokerSettlementDTO:
    return BrokerSettlementDTO(
        id=str(row.id),
        broker_id=str(row.broker_id),
        broker_name=br.full_name if br else None,
        broker_code=br.user_code if br else None,
        period_start=row.period_start,
        period_end=row.period_end,
        user_count=row.user_count,
        gross_user_loss_inr=str(row.gross_user_loss_inr),
        gross_user_profit_inr=str(row.gross_user_profit_inr),
        total_brokerage_inr=str(row.total_brokerage_inr),
        net_house_pnl_inr=str(row.net_house_pnl_inr),
        pnl_share_pct_snapshot=str(row.pnl_share_pct_snapshot),
        broker_share_inr=str(row.broker_share_inr),
        status=row.status.value,
        finalized_at=row.finalized_at,
        paid_at=row.paid_at,
        notes=row.notes,
        frozen=row.is_frozen(),
    )


# ── Cap (drives the create/edit form greying) ───────────────────────
@router.get("/brokers/max-grantable", response_model=APIResponse[MaxGrantableDTO])
async def get_max_grantable(actor: CurrentAdmin):
    """Returns the highest permission level the caller can grant for each
    broker-permission key. The frontend uses this to grey out OFF/VIEW/EDIT
    radio options above the cap on the create/edit dialog."""
    cap = max_grantable_perms(actor)
    return APIResponse(
        data=MaxGrantableDTO(cap={k: v.value for k, v in cap.items()})
    )


# ── Broker CRUD ──────────────────────────────────────────────────────
@router.get("/brokers", response_model=APIResponse[dict])
async def list_brokers(
    actor: CurrentAdmin,
    q: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
    admin_id: PydanticObjectId | None = None,
    include_sub: bool = False,
):
    # Permission gate inline. Super-admin always passes; admin needs
    # admin_permissions.brokers; broker needs broker_permissions.sub_brokers >= VIEW.
    if actor.role == UserRole.ADMIN:
        if not (actor.admin_permissions and actor.admin_permissions.brokers):
            raise HTTPException(status_code=403, detail="Brokers permission not granted")
    elif actor.role == UserRole.BROKER:
        from app.models._base import PermissionLevel

        bp = actor.broker_permissions
        if bp is None or not PermissionLevel.at_least(
            bp.sub_brokers if isinstance(bp.sub_brokers, PermissionLevel) else PermissionLevel(bp.sub_brokers),
            PermissionLevel.VIEW,
        ):
            raise HTTPException(status_code=403, detail="Sub-brokers permission not granted")

    # `admin_id` filter is super-admin only; ignore for other roles so they
    # can't peek across pools.
    effective_admin_id = admin_id if actor.role == UserRole.SUPER_ADMIN else None
    rows, total = await svc.list_brokers_for(
        actor,
        status=status,
        q=q,
        page=page,
        page_size=page_size,
        admin_id=effective_admin_id,
        include_sub=include_sub,
    )
    items = [await _ser_broker(b) for b in rows]
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


@router.post("/brokers", response_model=APIResponse[BrokerDTO])
async def create_broker(payload: CreateBrokerRequest, actor: CurrentAdmin):
    # Admin needs `brokers`, broker needs `sub_brokers` at EDIT.
    if actor.role == UserRole.ADMIN:
        if not (actor.admin_permissions and actor.admin_permissions.brokers):
            raise HTTPException(status_code=403, detail="Brokers permission not granted")
    elif actor.role == UserRole.BROKER:
        from app.models._base import PermissionLevel

        bp = actor.broker_permissions
        cur = (
            bp.sub_brokers
            if (bp and isinstance(bp.sub_brokers, PermissionLevel))
            else PermissionLevel(bp.sub_brokers) if bp else PermissionLevel.OFF
        )
        if not PermissionLevel.at_least(cur, PermissionLevel.EDIT):
            raise HTTPException(status_code=403, detail="Sub-brokers EDIT required")

    b = await svc.create_broker(
        creator=actor,
        email=payload.email,
        mobile=payload.mobile,
        password=payload.password,
        full_name=payload.full_name,
        permissions=payload.permissions,
        pnl_share_pct=payload.pnl_share_pct,
        brokerage_share_pct=payload.brokerage_share_pct,
        assigned_admin_id=payload.assigned_admin_id,
    )
    return APIResponse(data=await _ser_broker(b))


@router.get("/brokers/{broker_id}", response_model=APIResponse[BrokerDTO])
async def get_broker(broker_id: str, actor: CurrentAdmin):
    b = await assert_broker_in_scope(actor, broker_id)
    return APIResponse(data=await _ser_broker(b))


@router.put("/brokers/{broker_id}", response_model=APIResponse[BrokerDTO])
async def update_broker(
    broker_id: str, payload: UpdateBrokerRequest, actor: CurrentAdmin
):
    b = await svc.update_broker(actor, broker_id, full_name=payload.full_name)
    return APIResponse(data=await _ser_broker(b))


@router.put(
    "/brokers/{broker_id}/permissions", response_model=APIResponse[dict]
)
async def update_permissions(
    broker_id: str,
    payload: UpdateBrokerPermissionsRequest,
    actor: CurrentAdmin,
):
    b, cascaded = await svc.update_broker_permissions(
        actor, broker_id, payload.permissions
    )
    return APIResponse(
        data={
            "broker": (await _ser_broker(b)).model_dump(),
            "cascaded_changes": cascaded,
        }
    )


@router.put(
    "/brokers/{broker_id}/pnl-share", response_model=APIResponse[BrokerDTO]
)
async def update_pnl_share(
    broker_id: str, payload: UpdateBrokerPnlShareRequest, actor: CurrentAdmin
):
    b = await svc.set_broker_pnl_share(
        actor, broker_id, payload.pct, brokerage_pct=payload.brokerage_pct
    )
    return APIResponse(data=await _ser_broker(b))


@router.post("/brokers/{broker_id}/block", response_model=APIResponse[BrokerDTO])
async def block_broker(broker_id: str, actor: CurrentAdmin):
    b = await svc.block_broker(actor, broker_id)
    return APIResponse(data=await _ser_broker(b))


@router.post(
    "/brokers/{broker_id}/unblock", response_model=APIResponse[BrokerDTO]
)
async def unblock_broker(broker_id: str, actor: CurrentAdmin):
    b = await svc.unblock_broker(actor, broker_id)
    return APIResponse(data=await _ser_broker(b))


@router.delete("/brokers/{broker_id}", response_model=APIResponse[dict])
async def delete_broker(broker_id: str, actor: CurrentAdmin):
    """Delete a broker / sub-broker. Their users + sub-brokers move up to the
    parent (no orphans). Scope-checked — admin deletes their brokers, broker
    deletes their sub-brokers."""
    b = await svc.delete_broker(actor, broker_id)
    return APIResponse(data={"deleted": str(b.id)})


class _SetBrokerGatewayBody(BaseModel):
    enabled: bool


@router.put(
    "/brokers/{broker_id}/payment-gateway", response_model=APIResponse[BrokerDTO]
)
async def set_broker_payment_gateway(
    broker_id: str, body: _SetBrokerGatewayBody, actor: CurrentAdmin
):
    """Admin turns the Divinepay UPI gateway ON/OFF for this broker's users.
    Only takes effect when the owning admin's own gateway is enabled (enforced
    in gateway_on_for)."""
    b = await svc.set_broker_payment_gateway(actor, broker_id, body.enabled)
    return APIResponse(data=await _ser_broker(b))


@router.post(
    "/brokers/{broker_id}/reset-password",
    response_model=APIResponse[dict],
)
async def reset_broker_password(
    broker_id: str, body: ResetPasswordRequest, actor: CurrentAdmin
):
    """Reset a broker / sub-broker password to a value the actor
    chooses. Scope (super-admin → any broker, admin → their brokers,
    broker → their sub-brokers) is enforced inside the service via
    `assert_broker_in_scope`. Mirrors the sub-admin reset endpoint at
    `/management/sub-admins/{id}/reset-password` so the admin UI's
    three-dot menu can expose the same flow for every tier.
    """
    b = await svc.reset_broker_password(actor, broker_id, body.new_password)
    return APIResponse(data={"reset": str(b.id)})


@router.get("/brokers/{broker_id}/users", response_model=APIResponse[dict])
async def list_broker_subtree_users(
    broker_id: str,
    actor: CurrentAdmin,
    page: int = 1,
    page_size: int = 50,
):
    # Scope check first — actor must own this broker
    await assert_broker_in_scope(actor, broker_id)
    rows, total = await svc.list_subtree_clients(
        broker_id, page=page, page_size=page_size
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
            "assigned_broker_id": (
                str(u.assigned_broker_id) if u.assigned_broker_id else None
            ),
            "broker_ancestry": [str(x) for x in (u.broker_ancestry or [])],
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


# ── User → broker assignment ─────────────────────────────────────────
@router.post(
    "/users/{user_id}/assign-to-broker", response_model=APIResponse[dict]
)
async def assign_user_to_broker(
    user_id: str, payload: AssignUserToBrokerRequest, actor: CurrentAdmin
):
    target = await svc.reassign_user_to_broker(actor, user_id, payload.broker_id)
    return APIResponse(
        data={
            "id": str(target.id),
            "assigned_admin_id": (
                str(target.assigned_admin_id) if target.assigned_admin_id else None
            ),
            "assigned_broker_id": (
                str(target.assigned_broker_id) if target.assigned_broker_id else None
            ),
            "broker_ancestry": [str(x) for x in (target.broker_ancestry or [])],
        }
    )


@router.post(
    "/users/bulk-assign-to-broker", response_model=APIResponse[dict]
)
async def bulk_assign_to_broker(
    payload: BulkAssignToBrokerRequest, actor: CurrentAdmin
):
    result = await svc.bulk_reassign_to_broker(
        actor, payload.user_ids, payload.broker_id
    )
    return APIResponse(data=result)


# ── Broker report ────────────────────────────────────────────────────
@router.get("/brokers/{broker_id}/report", response_model=APIResponse[dict])
async def broker_report(broker_id: str, actor: CurrentAdmin):
    """Detail-page aggregator (user counts, wallet rollup, weekly PNL,
    open positions, recent trades). Mirrors the sub-admin report at
    /admin/management/sub-admins/{id}/report but scoped to broker's
    direct clients + a 'subtree' headline number."""
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
    from app.utils.decimal_utils import to_decimal
    from app.utils.time_utils import now_utc

    b = await assert_broker_in_scope(actor, broker_id)

    coll = User.get_motor_collection()
    cursor = coll.find({"assigned_broker_id": b.id}, {"_id": 1, "status": 1, "role": 1})
    pool: list[PydanticObjectId] = []
    active_count = 0
    async for doc in cursor:
        if doc.get("role") in {
            UserRole.SUPER_ADMIN.value,
            UserRole.ADMIN.value,
            UserRole.BROKER.value,
        }:
            continue
        pool.append(doc["_id"])
        if doc.get("status") == UserStatus.ACTIVE.value:
            active_count += 1

    subtree_count = await svc.count_subtree_users(b.id)

    if not pool:
        return APIResponse(
            data={
                "broker": (await _ser_broker(b)).model_dump(),
                "user_count": 0,
                "active_user_count": 0,
                "subtree_user_count": subtree_count,
                "wallet": {k: "0" for k in (
                    "available_balance", "used_margin", "credit_limit",
                    "total_deposits", "total_withdrawals", "total_brokerage",
                )},
                "pnl": {k: "0" for k in (
                    "today_realised", "week_realised", "all_time_realised", "open_unrealised",
                )},
                "trades": {"today": 0, "this_week": 0, "all_time": 0},
                "open_positions": 0,
                "recent_trades": [],
                "deposits_week": "0",
                "withdrawals_week": "0",
            }
        )

    user_q = {"user_id": {"$in": pool}}

    now_utc_dt = now_utc()
    ist_now = now_utc_dt.astimezone(IST)
    today_ist_start = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ist_start = today_ist_start - _td(days=ist_now.weekday())
    today_start = today_ist_start.astimezone(now_utc_dt.tzinfo)
    week_start = week_ist_start.astimezone(now_utc_dt.tzinfo)

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

    from app.services import market_data_service
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
        rate = to_decimal(p.open_usd_inr_rate) if p.open_usd_inr_rate is not None else fallback_usd_inr
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

    open_positions = await Position.find(
        {**user_q, "status": PositionStatus.OPEN.value}
    ).to_list()
    open_unrealised = _D("0")
    for p in open_positions:
        raw = to_decimal(p.unrealized_pnl)
        if _is_usd(p):
            raw *= fallback_usd_inr
        open_unrealised += raw

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

    trade_user_ids = list({t.user_id for t in recent_trades_rows})
    users_for_trades = (
        await User.find({"_id": {"$in": trade_user_ids}}).to_list()
        if trade_user_ids
        else []
    )
    code_map = {str(u.id): u.user_code for u in users_for_trades}

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
            "broker": (await _ser_broker(b)).model_dump(),
            "user_count": len(pool),
            "active_user_count": active_count,
            "subtree_user_count": subtree_count,
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


# ── Login-as broker ─────────────────────────────────────────────────
@router.post(
    "/brokers/{broker_id}/impersonate", response_model=APIResponse[dict]
)
async def impersonate_broker(broker_id: str, actor: CurrentAdmin):
    """Mint admin-side tokens for the target broker. Actor must own them
    (super-admin: top-pool only; admin: their pool; broker: subtree)."""
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

    target = await assert_broker_in_scope(actor, broker_id)

    access = create_access_token(
        user_id=str(target.id),
        role=target.role.value,
        extra={"impersonator": str(actor.id), "ver": int(target.token_version or 0)},
    )
    refresh, jti = create_refresh_token(
        user_id=str(target.id), role=target.role.value
    )
    await cache_set(
        refresh_jti_key(str(target.id), jti),
        {
            "user_id": str(target.id),
            "audience": "admin",
            "impersonator": str(actor.id),
        },
        ttl_sec=cfg.JWT_REFRESH_TTL_DAYS * 86400,
    )
    await cache_set(
        session_key(str(target.id), jti),
        {"audience": "admin", "impersonator": str(actor.id)},
        ttl_sec=cfg.JWT_REFRESH_TTL_DAYS * 86400,
    )
    await log_event(
        action=AuditAction.IMPERSONATE,
        entity_type="User",
        entity_id=target.id,
        actor_id=actor.id,
        target_user_id=target.id,
        metadata={"as_role": target.role.value, "kind": "BROKER"},
    )
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
                "admin_permissions": None,
                "broker_permissions": (
                    target.broker_permissions.model_dump()
                    if target.broker_permissions is not None
                    else None
                ),
                "pnl_share_pct": (
                    str(target.broker_pnl_share_pct)
                    if target.broker_pnl_share_pct is not None
                    else None
                ),
            },
            "admin_app_url": admin_origin,
        }
    )


# ── Broker settlements ───────────────────────────────────────────────
@router.get("/broker-settlements", response_model=APIResponse[dict])
async def list_broker_settlements(
    actor: CurrentAdmin, week_start: date | None = None
):
    # Settlements are an admin reconciliation surface — broker doesn't see this.
    if actor.role == UserRole.BROKER:
        raise HTTPException(status_code=403, detail="Settlements not available to broker")
    if actor.role == UserRole.ADMIN and not (
        actor.admin_permissions and actor.admin_permissions.brokers
    ):
        raise HTTPException(status_code=403, detail="Brokers permission not granted")

    if week_start is None:
        anchor = datetime.now(IST)
    else:
        anchor = datetime.combine(week_start, datetime.min.time(), tzinfo=IST)

    scoped_admin_id = actor.id if actor.role == UserRole.ADMIN else None
    rows = await stl.list_settlements_for_week(
        anchor, actor_id=actor.id, scoped_admin_id=scoped_admin_id
    )
    period_start, period_end = stl.ist_week_bounds(anchor)
    items = [_ser_settlement(r, br) for (r, br) in rows]
    total_users = sum(r.user_count for (r, _) in rows)
    total_net = sum(
        (r.net_house_pnl_inr.to_decimal() for (r, _) in rows), start=Decimal("0")
    )
    total_share = sum(
        (r.broker_share_inr.to_decimal() for (r, _) in rows), start=Decimal("0")
    )
    return APIResponse(
        data={
            "period_start": period_start,
            "period_end": period_end,
            "items": items,
            "totals": {
                "user_count": total_users,
                "net_house_pnl_inr": str(total_net),
                "broker_share_inr": str(total_share),
            },
        }
    )


@router.get(
    "/broker-settlements/broker/{broker_id}",
    response_model=APIResponse[dict],
)
async def history_for_broker(
    broker_id: str,
    actor: CurrentAdmin,
    from_date: date | None = None,
    to_date: date | None = None,
):
    if actor.role == UserRole.BROKER:
        raise HTTPException(status_code=403, detail="Settlements not available to broker")
    br = await assert_broker_in_scope(actor, broker_id)
    rows = await stl.history_for_broker(
        broker_id, from_date=from_date, to_date=to_date
    )
    return APIResponse(
        data={
            "broker": {
                "id": str(br.id),
                "user_code": br.user_code,
                "full_name": br.full_name,
            },
            "items": [_ser_settlement(r, br) for r in rows],
        }
    )


@router.post(
    "/broker-settlements/recompute", response_model=APIResponse[dict]
)
async def recompute_broker_settlements(
    payload: RecomputeBrokerSettlementRequest, actor: CurrentAdmin
):
    if actor.role == UserRole.BROKER:
        raise HTTPException(status_code=403, detail="Settlements not available to broker")
    if actor.role == UserRole.ADMIN and not (
        actor.admin_permissions and actor.admin_permissions.brokers
    ):
        raise HTTPException(status_code=403, detail="Brokers permission not granted")
    anchor = datetime.combine(payload.week_start, datetime.min.time(), tzinfo=IST)
    scoped_admin_id = actor.id if actor.role == UserRole.ADMIN else None
    if payload.broker_id:
        # Validate scope on the specific broker
        await assert_broker_in_scope(actor, payload.broker_id)
        row, frozen = await stl.compute_settlement(
            payload.broker_id, anchor, actor_id=actor.id
        )
        br = await svc.get_broker_or_404(payload.broker_id)
        return APIResponse(
            data={
                "items": [_ser_settlement(row, br)],
                "frozen_skipped": 1 if frozen else 0,
            }
        )
    results = await stl.compute_all_for_week(
        anchor, actor_id=actor.id, scoped_admin_id=scoped_admin_id
    )
    brokers = await User.find({"role": UserRole.BROKER.value}).to_list()
    by_id = {br.id: br for br in brokers}
    return APIResponse(
        data={
            "items": [
                _ser_settlement(row, by_id.get(row.broker_id))
                for (row, _) in results
            ],
            "frozen_skipped": sum(1 for (_, frozen) in results if frozen),
        }
    )


@router.post(
    "/broker-settlements/{settlement_id}/finalize",
    response_model=APIResponse[BrokerSettlementDTO],
)
async def finalize_broker_settlement(settlement_id: str, actor: CurrentAdmin):
    if actor.role == UserRole.BROKER:
        raise HTTPException(status_code=403, detail="Settlements not available to broker")
    row = await stl.finalize(settlement_id, actor.id)
    br = await User.get(PydanticObjectId(row.broker_id))
    return APIResponse(data=_ser_settlement(row, br))


@router.post(
    "/broker-settlements/{settlement_id}/mark-paid",
    response_model=APIResponse[BrokerSettlementDTO],
)
async def mark_paid_broker_settlement(
    settlement_id: str, payload: MarkPaidBrokerRequest, actor: CurrentAdmin
):
    if actor.role == UserRole.BROKER:
        raise HTTPException(status_code=403, detail="Settlements not available to broker")
    row = await stl.mark_paid(settlement_id, actor.id, notes=payload.notes)
    br = await User.get(PydanticObjectId(row.broker_id))
    return APIResponse(data=_ser_settlement(row, br))
