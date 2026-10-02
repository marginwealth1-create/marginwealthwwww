"""Accounts Dashboard — per-admin/per-broker/per-sub-broker financial summary.

Scope parameter controls grouping:
  all_users    → grand total only (one card, fastest)
  admins       → per-admin breakdown (super-admin only)
  brokers      → per-broker breakdown
  sub_brokers  → per-sub-broker breakdown

All calculations verified per spec — see docstring at module top.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, Query
from starlette.responses import StreamingResponse

from app.core.dependencies import CurrentAdmin, require_perm, scoped_user_filter, scoped_user_ids
from app.models.position import Position, PositionStatus
from app.models.trade import Trade
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.schemas.common import APIResponse
from app.services import accounts_dashboard_service as ads
from app.utils.decimal_utils import to_decimal

router = APIRouter(prefix="/accounts", tags=["admin-accounts"])

IST = ZoneInfo("Asia/Kolkata")

_WEEK_PRESETS = {
    "current_week": lambda now: (
        (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0),
        now,
    ),
    "last_week": lambda now: (
        (now - timedelta(days=now.weekday() + 7)).replace(hour=0, minute=0, second=0, microsecond=0),
        (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1),
    ),
    "this_month": lambda now: (
        now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        now,
    ),
    "last_month": lambda now: (
        (now.replace(day=1) - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1),
    ),
}

TRADING_ROLES = [UserRole.CLIENT.value, UserRole.DEALER.value, UserRole.MASTER.value]


def _to_utc(dt_ist: datetime) -> datetime:
    if dt_ist.tzinfo is None:
        dt_ist = dt_ist.replace(tzinfo=IST)
    return dt_ist.astimezone(timezone.utc)


def _d(val: Any) -> float:
    if val is None:
        return 0.0
    try:
        return float(str(val))
    except Exception:
        return 0.0


async def _aggregate_for_users(
    user_ids: list[PydanticObjectId],
    *,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
) -> dict[str, Any]:
    if not user_ids:
        return _empty()

    is_lifetime = start_utc is None and end_utc is None

    wallets = await Wallet.find({"user_id": {"$in": user_ids}}).to_list()

    total_balance = sum(_d(w.available_balance) + _d(w.used_margin) for w in wallets)
    total_used_margin = sum(_d(w.used_margin) for w in wallets)

    open_positions = await Position.find(
        {"user_id": {"$in": user_ids}, "status": PositionStatus.OPEN.value}
    ).to_list()
    total_unrealized = sum(_d(p.unrealized_pnl) for p in open_positions)
    open_count = len(open_positions)
    total_equity = total_balance + total_unrealized
    total_settlement = sum(_d(w.settlement_outstanding) for w in wallets)

    if is_lifetime:
        deposits = sum(_d(w.total_deposits) for w in wallets)
        withdrawals = sum(_d(w.total_withdrawals) for w in wallets)
        realized_pnl = sum(_d(w.realized_pnl) for w in wallets)

        # Brokerage: wallet.total_brokerage is often 0 because brokerage
        # is tracked per-trade (Trade.brokerage) not as a separate wallet
        # transaction. Always sum from trades for accuracy.
        all_trades = await Trade.find({
            "user_id": {"$in": user_ids},
        }).to_list()
        brokerage = sum(_d(t.brokerage) for t in all_trades)
        volume = sum(_d(t.value) for t in all_trades)

        all_closing_trades = [t for t in all_trades if t.pnl_inr is not None]
        total_trades = len(all_closing_trades)
        profit_trades = sum(1 for t in all_closing_trades if _d(t.pnl_inr) > 0)
        loss_trades = sum(1 for t in all_closing_trades if _d(t.pnl_inr) < 0)
    else:
        date_filter = {}
        if start_utc:
            date_filter["$gte"] = start_utc
        if end_utc:
            date_filter["$lte"] = end_utc

        dep_txns = await WalletTransaction.find({
            "user_id": {"$in": user_ids},
            "transaction_type": TransactionType.DEPOSIT.value,
            "created_at": date_filter,
        }).to_list()
        deposits = sum(_d(t.amount) for t in dep_txns)

        wd_txns = await WalletTransaction.find({
            "user_id": {"$in": user_ids},
            "transaction_type": TransactionType.WITHDRAWAL.value,
            "created_at": date_filter,
        }).to_list()
        withdrawals = sum(abs(_d(t.amount)) for t in wd_txns)

        closed_positions = await Position.find({
            "user_id": {"$in": user_ids},
            "status": PositionStatus.CLOSED.value,
            "closed_at": date_filter,
        }).to_list()
        realized_pnl = sum(_d(p.realized_pnl) for p in closed_positions)

        closing_trades = await Trade.find({
            "user_id": {"$in": user_ids},
            "pnl_inr": {"$ne": None},
            "executed_at": date_filter,
        }).to_list()
        total_trades = len(closing_trades)
        profit_trades = sum(1 for t in closing_trades if _d(t.pnl_inr) > 0)
        loss_trades = sum(1 for t in closing_trades if _d(t.pnl_inr) < 0)

        all_trades = await Trade.find({
            "user_id": {"$in": user_ids},
            "executed_at": date_filter,
        }).to_list()
        brokerage = sum(_d(t.brokerage) for t in all_trades)
        volume = sum(_d(t.value) for t in all_trades)

    net_deposit = deposits - withdrawals
    win_rate = round((profit_trades / total_trades) * 100, 1) if total_trades > 0 else 0.0
    net_pnl = realized_pnl + total_unrealized

    return {
        "deposits": round(deposits, 2),
        "withdrawals": round(withdrawals, 2),
        "net_deposit": round(net_deposit, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(total_unrealized, 2),
        "net_pnl": round(net_pnl, 2),
        "brokerage": round(brokerage, 2),
        "total_trades": total_trades,
        "profit_trades": profit_trades,
        "loss_trades": loss_trades,
        "win_rate": win_rate,
        "volume": round(volume, 2),
        "balance": round(total_balance, 2),
        "equity": round(total_equity, 2),
        "open_positions": open_count,
        "settlement_outstanding": round(total_settlement, 2),
        "user_count": len(user_ids),
    }


def _empty() -> dict[str, Any]:
    return {
        "deposits": 0, "withdrawals": 0, "net_deposit": 0,
        "realized_pnl": 0, "unrealized_pnl": 0, "net_pnl": 0,
        "brokerage": 0, "total_trades": 0, "profit_trades": 0,
        "loss_trades": 0, "win_rate": 0, "volume": 0,
        "balance": 0, "equity": 0, "open_positions": 0,
        "settlement_outstanding": 0, "user_count": 0,
    }


async def _make_entity(
    entity_user: User,
    pool_ids: list[PydanticObjectId],
    role_label: str,
    start_utc: datetime | None,
    end_utc: datetime | None,
    **extra: Any,
) -> dict[str, Any]:
    agg = await _aggregate_for_users(pool_ids, start_utc=start_utc, end_utc=end_utc)
    return {
        "id": str(entity_user.id),
        "name": entity_user.full_name or entity_user.user_code or role_label,
        "user_code": entity_user.user_code,
        "role": role_label,
        **extra,
        **agg,
    }


@router.get("/summary")
async def accounts_summary(
    admin: CurrentAdmin,
    scope: str = Query(default="all_users", description="all_users|admins|brokers|sub_brokers"),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    preset: str | None = Query(default=None),
    _: None = Depends(require_perm("users", "read")),
) -> APIResponse:
    now_ist = datetime.now(IST)

    start_utc: datetime | None = None
    end_utc: datetime | None = None
    if preset and preset in _WEEK_PRESETS:
        s, e = _WEEK_PRESETS[preset](now_ist)
        start_utc = _to_utc(s)
        end_utc = _to_utc(e)
    elif from_date or to_date:
        try:
            if from_date:
                start_utc = _to_utc(datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=IST))
            if to_date:
                end_utc = _to_utc(datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=IST))
        except ValueError:
            pass

    entities: list[dict[str, Any]] = []
    # Comprehensive scope (incl. broker subtree for ADMIN) so the
    # brokers / sub-brokers tabs list every broker under the admin even
    # when a transferred broker's descendants weren't re-stamped.
    admin_scope = await scoped_user_filter(admin)

    # Collect ALL user ids in scope for grand total
    all_scope_query: dict[str, Any] = {
        "role": {"$in": TRADING_ROLES},
        "status": {"$ne": UserStatus.CLOSED.value},
        "is_demo": {"$ne": True},
    }
    if admin.role == UserRole.SUPER_ADMIN:
        all_users = await User.find(all_scope_query).to_list()
    else:
        # Use the thorough subtree resolver (NOT the flat assigned_admin_id
        # filter): for an ADMIN it pulls every client under their brokers /
        # sub-brokers even when assigned_admin_id wasn't propagated down the
        # chain; for a BROKER it's the whole broker_ancestry subtree. This is
        # what makes "All Users" actually show everyone under the entity —
        # broker AND sub-broker clients — per the operator's flow.
        scoped_ids = await scoped_user_ids(admin)
        all_users = (
            await User.find({**all_scope_query, "_id": {"$in": scoped_ids}}).to_list()
            if scoped_ids
            else []
        )

    all_ids = [u.id for u in all_users]
    grand_total = await _aggregate_for_users(all_ids, start_utc=start_utc, end_utc=end_utc)

    if scope == "all_users":
        # Per-user breakdown for every trading user in scope
        async def _do_user(u: User) -> dict[str, Any]:
            agg = await _aggregate_for_users([u.id], start_utc=start_utc, end_utc=end_utc)
            owner_label = ""
            if u.assigned_broker_id:
                broker = await User.get(u.assigned_broker_id)
                owner_label = (broker.full_name or broker.user_code or "Broker") if broker else ""
            elif u.assigned_admin_id:
                adm = await User.get(u.assigned_admin_id)
                owner_label = (adm.full_name or adm.user_code or "Admin") if adm else ""
            return {
                "id": str(u.id),
                "name": u.full_name or u.user_code or "User",
                "user_code": u.user_code,
                "role": u.role.value if hasattr(u.role, "value") else str(u.role),
                "owner": owner_label,
                **agg,
            }

        results = await asyncio.gather(*[_do_user(u) for u in all_users], return_exceptions=True)
        entities.extend(r for r in results if isinstance(r, dict))

    elif scope == "admins" and admin.role == UserRole.SUPER_ADMIN:
        # Super-admin's direct users
        direct = [u for u in all_users if u.assigned_admin_id is None]
        if direct:
            agg = await _aggregate_for_users([u.id for u in direct], start_utc=start_utc, end_utc=end_utc)
            entities.append({"id": str(admin.id), "name": "Direct Users", "role": "DIRECT", **agg})

        # Per admin
        admins = await User.find({
            "role": UserRole.ADMIN.value,
            "status": {"$ne": UserStatus.CLOSED.value},
        }).to_list()

        async def _do_admin(adm: User) -> dict[str, Any]:
            pool = [u.id for u in all_users if u.assigned_admin_id == adm.id]
            broker_count = sum(1 for u in await User.find({"assigned_admin_id": adm.id, "role": UserRole.BROKER.value}).to_list())
            return await _make_entity(adm, pool, "ADMIN", start_utc, end_utc, broker_count=broker_count)

        results = await asyncio.gather(*[_do_admin(a) for a in admins], return_exceptions=True)
        entities.extend(r for r in results if isinstance(r, dict))

    elif scope == "brokers":
        # Per-broker (works for both super-admin and admin)
        broker_query: dict[str, Any] = {
            "role": UserRole.BROKER.value,
            "status": {"$ne": UserStatus.CLOSED.value},
        }
        if admin.role != UserRole.SUPER_ADMIN:
            broker_query.update(admin_scope)
        brokers = await User.find(broker_query).to_list()

        # Direct users (no broker)
        direct = [u for u in all_users if u.assigned_broker_id is None]
        if direct:
            agg = await _aggregate_for_users([u.id for u in direct], start_utc=start_utc, end_utc=end_utc)
            entities.append({"id": "direct", "name": "Direct Users (No Broker)", "role": "DIRECT", **agg})

        async def _do_broker(b: User) -> dict[str, Any]:
            pool = [u.id for u in all_users if u.assigned_broker_id == b.id]
            return await _make_entity(b, pool, "BROKER", start_utc, end_utc)

        results = await asyncio.gather(*[_do_broker(b) for b in brokers], return_exceptions=True)
        entities.extend(r for r in results if isinstance(r, dict))

    elif scope == "sub_brokers":
        # Sub-brokers = BROKER users who themselves have a parent broker
        sub_query: dict[str, Any] = {
            "role": UserRole.BROKER.value,
            "status": {"$ne": UserStatus.CLOSED.value},
            "assigned_broker_id": {"$ne": None},
        }
        if admin.role != UserRole.SUPER_ADMIN:
            sub_query.update(admin_scope)
        sub_brokers = await User.find(sub_query).to_list()

        async def _do_sub(sb: User) -> dict[str, Any]:
            pool = [u.id for u in all_users if u.assigned_broker_id == sb.id]
            return await _make_entity(sb, pool, "SUB_BROKER", start_utc, end_utc)

        results = await asyncio.gather(*[_do_sub(sb) for sb in sub_brokers], return_exceptions=True)
        entities.extend(r for r in results if isinstance(r, dict))

    return APIResponse(data={
        "entities": entities,
        "grand_total": grand_total,
        "scope": scope,
        "filter": {
            "from_date": from_date,
            "to_date": to_date,
            "preset": preset,
            "is_lifetime": start_utc is None,
        },
    })


# ── Shared date-parsing helper ───────────────────────────────────────
def _parse_dates(
    from_date: str | None, to_date: str | None, preset: str | None,
) -> tuple[datetime | None, datetime | None]:
    now_ist = datetime.now(IST)
    start_utc: datetime | None = None
    end_utc: datetime | None = None
    if preset and preset in _WEEK_PRESETS:
        s, e = _WEEK_PRESETS[preset](now_ist)
        start_utc = _to_utc(s)
        end_utc = _to_utc(e)
    elif from_date or to_date:
        try:
            if from_date:
                start_utc = _to_utc(datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=IST))
            if to_date:
                end_utc = _to_utc(datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=IST))
        except ValueError:
            pass
    return start_utc, end_utc


def _period_label(from_date: str | None, to_date: str | None, preset: str | None) -> str:
    if preset:
        return preset.replace("_", " ").title()
    if from_date and to_date:
        return f"{from_date} to {to_date}"
    if from_date:
        return f"From {from_date}"
    if to_date:
        return f"Until {to_date}"
    return "All time"


# ── Week options ─────────────────────────────────────────────────────
@router.get("/weeks")
async def accounts_weeks(
    admin: CurrentAdmin,
    num_weeks: int = Query(default=16, ge=4, le=52),
    _: None = Depends(require_perm("users", "read")),
) -> APIResponse:
    return APIResponse(data=ads.generate_week_options(num_weeks))


# ── Broker totals (PNL sharing snapshot) ─────────────────────────────
@router.get("/broker-totals/{entity_id}")
async def broker_totals(
    entity_id: str,
    admin: CurrentAdmin,
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    preset: str | None = Query(default=None),
    _: None = Depends(require_perm("users", "read")),
) -> APIResponse:
    try:
        eid = PydanticObjectId(entity_id)
    except Exception:
        return APIResponse(data=ads._empty_broker_totals(), message="Invalid entity ID")

    entity = await User.get(eid)
    if not entity:
        return APIResponse(data=ads._empty_broker_totals(), message="Entity not found")

    start_utc, end_utc = _parse_dates(from_date, to_date, preset)
    result = await ads.compute_broker_totals(eid, start_utc, end_utc)
    return APIResponse(data=result)


# ── Entity users (paginated per-user PNL) ────────────────────────────
@router.get("/entity-users/{entity_id}")
async def entity_users(
    entity_id: str,
    admin: CurrentAdmin,
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    preset: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=15, ge=1, le=100),
    search: str | None = Query(default=None),
    _: None = Depends(require_perm("users", "read")),
) -> APIResponse:
    _empty_resp = {"items": [], "meta": {"page": 1, "page_size": page_size, "total": 0, "total_pages": 0}}
    try:
        eid = PydanticObjectId(entity_id)
    except Exception:
        return APIResponse(data=_empty_resp, message="Invalid entity ID")

    entity = await User.get(eid)
    if not entity:
        return APIResponse(data=_empty_resp)

    entity_role = entity.role.value if hasattr(entity.role, "value") else str(entity.role)
    start_utc, end_utc = _parse_dates(from_date, to_date, preset)
    result = await ads.get_entity_users(eid, entity_role, start_utc, end_utc, page, page_size, search)
    return APIResponse(data=result)


# ── Export: all users of entity as Excel ─────────────────────────────
@router.get("/entity-users/{entity_id}/export/excel")
async def export_entity_users_excel(
    entity_id: str,
    admin: CurrentAdmin,
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    preset: str | None = Query(default=None),
    _: None = Depends(require_perm("users", "read")),
) -> StreamingResponse:
    eid = PydanticObjectId(entity_id)
    entity = await User.get(eid)
    entity_name = (entity.full_name or entity.user_code or entity_id) if entity else entity_id
    entity_role = (entity.role.value if hasattr(entity.role, "value") else str(entity.role)) if entity else "BROKER"

    start_utc, end_utc = _parse_dates(from_date, to_date, preset)
    users_data = await ads.get_all_entity_users(eid, entity_role, start_utc, end_utc)
    label = _period_label(from_date, to_date, preset)
    data = ads.render_entity_users_excel(entity_name, users_data, label)
    filename = f"pnl_{entity_name}_{from_date or 'all'}_{to_date or 'time'}.xlsx"

    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Export: all users of entity as PDF ───────────────────────────────
@router.get("/entity-users/{entity_id}/export/pdf")
async def export_entity_users_pdf(
    entity_id: str,
    admin: CurrentAdmin,
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    preset: str | None = Query(default=None),
    _: None = Depends(require_perm("users", "read")),
) -> StreamingResponse:
    eid = PydanticObjectId(entity_id)
    entity = await User.get(eid)
    entity_name = (entity.full_name or entity.user_code or entity_id) if entity else entity_id
    entity_role = (entity.role.value if hasattr(entity.role, "value") else str(entity.role)) if entity else "BROKER"

    start_utc, end_utc = _parse_dates(from_date, to_date, preset)
    users_data = await ads.get_all_entity_users(eid, entity_role, start_utc, end_utc)
    label = _period_label(from_date, to_date, preset)
    data = ads.render_entity_users_pdf(entity_name, users_data, label)
    filename = f"pnl_{entity_name}_{from_date or 'all'}_{to_date or 'time'}.pdf"

    return StreamingResponse(
        BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Export: broker totals as Excel ───────────────────────────────────
@router.get("/broker-totals/{entity_id}/export/excel")
async def export_broker_totals_excel(
    entity_id: str,
    admin: CurrentAdmin,
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    preset: str | None = Query(default=None),
    _: None = Depends(require_perm("users", "read")),
) -> StreamingResponse:
    eid = PydanticObjectId(entity_id)
    entity = await User.get(eid)
    entity_name = (entity.full_name or entity.user_code or entity_id) if entity else entity_id

    start_utc, end_utc = _parse_dates(from_date, to_date, preset)
    totals = await ads.compute_broker_totals(eid, start_utc, end_utc)
    label = _period_label(from_date, to_date, preset)
    data = ads.render_broker_totals_excel(totals, entity_name, label)
    filename = f"broker_summary_{entity_name}_{from_date or 'all'}_{to_date or 'time'}.xlsx"

    return StreamingResponse(
        BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Export: broker totals as PDF ─────────────────────────────────────
@router.get("/broker-totals/{entity_id}/export/pdf")
async def export_broker_totals_pdf(
    entity_id: str,
    admin: CurrentAdmin,
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    preset: str | None = Query(default=None),
    _: None = Depends(require_perm("users", "read")),
) -> StreamingResponse:
    eid = PydanticObjectId(entity_id)
    entity = await User.get(eid)
    entity_name = (entity.full_name or entity.user_code or entity_id) if entity else entity_id

    start_utc, end_utc = _parse_dates(from_date, to_date, preset)
    totals = await ads.compute_broker_totals(eid, start_utc, end_utc)
    label = _period_label(from_date, to_date, preset)
    data = ads.render_broker_totals_pdf(totals, entity_name, label)
    filename = f"broker_summary_{entity_name}_{from_date or 'all'}_{to_date or 'time'}.pdf"

    return StreamingResponse(
        BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
