"""Weekly P&L-share settlement for sub-admins.

B-book formula::

    net_house_pnl = gross_user_loss - gross_user_profit + total_brokerage
    sub_admin_share = (pnl_share_pct / 100) * net_house_pnl

Where:
- gross_user_loss  = sum of negative `Position.realized_pnl` (taken positive)
  for positions closed during the IST week, across the sub-admin's users.
- gross_user_profit = sum of positive `Position.realized_pnl`.
- total_brokerage = sum of |amount| in `WalletTransaction` rows of type
  ``BROKERAGE`` for the same users in the same window.

USD-quoted segments (crypto / forex / metals / energy) are converted to INR
using ``Position.open_usd_inr_rate`` snapshot when present (the same logic
used by ``/admin/users/{id}/live-trade-stats``), else the current spot rate.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from beanie import PydanticObjectId

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.models.admin_settlement import AdminSettlement, AdminSettlementStatus
from app.models.audit_log import AuditAction
from app.models.position import Position, PositionStatus
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole
from app.services import market_data_service
from app.services.audit_service import log_event
from app.utils.decimal_utils import to_decimal, to_decimal128
from app.utils.time_utils import IST, UTC

logger = logging.getLogger(__name__)


# ── IST week helpers ─────────────────────────────────────────────────
def ist_week_bounds(any_dt: date | datetime) -> tuple[datetime, datetime]:
    """Returns (period_start_utc, period_end_utc) for the IST week containing
    `any_dt`. Week starts Monday 00:00:00 IST and ends Sunday 23:59:59.999 IST."""
    if isinstance(any_dt, datetime):
        d = any_dt.astimezone(IST).date() if any_dt.tzinfo else any_dt.date()
    else:
        d = any_dt
    # weekday(): Mon=0 ... Sun=6
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    start_ist = datetime.combine(monday, time.min, tzinfo=IST)
    end_ist = datetime.combine(sunday, time.max, tzinfo=IST)
    return start_ist.astimezone(UTC), end_ist.astimezone(UTC)


# ── Aggregation ──────────────────────────────────────────────────────
def _is_usd_position(p: Position) -> bool:
    """USD-quoted instrument? Crypto / forex / metals / energy segments."""
    return market_data_service.is_usd_quoted_segment(p.segment_type) or bool(
        p.instrument
        and market_data_service.is_usd_quoted_segment(p.instrument.segment)
    )


def _realised_inr(p: Position, fallback_usd_inr: Decimal) -> Decimal:
    raw = to_decimal(p.realized_pnl)
    if not _is_usd_position(p):
        return raw
    rate = to_decimal(p.open_usd_inr_rate) if p.open_usd_inr_rate is not None else fallback_usd_inr
    return raw * rate


async def _sub_admin_user_ids(sub_admin_id: PydanticObjectId) -> list[PydanticObjectId]:
    coll = User.get_motor_collection()
    cursor = coll.find({"assigned_admin_id": sub_admin_id}, {"_id": 1})
    return [doc["_id"] async for doc in cursor]


async def compute_settlement(
    sub_admin_id: str | PydanticObjectId,
    period_start_utc: datetime,
    *,
    actor_id: PydanticObjectId | None = None,
) -> tuple[AdminSettlement, bool]:
    """Compute and upsert the weekly settlement row for one sub-admin.

    Returns ``(settlement, frozen)``. When ``frozen`` is True the existing row
    is FINALIZED or PAID and was not overwritten — the returned doc is the
    untouched persisted one. Otherwise the row has been refreshed in place.
    """
    try:
        sa_oid = PydanticObjectId(sub_admin_id)
    except Exception as e:
        raise ValidationFailedError("Invalid sub-admin id") from e
    sa = await User.get(sa_oid)
    if sa is None or sa.role != UserRole.ADMIN:
        raise NotFoundError("Sub-admin not found")

    # Snap to a Monday IST start regardless of what the caller passed.
    period_start_utc, period_end_utc = ist_week_bounds(period_start_utc)

    existing = await AdminSettlement.find_one(
        {"sub_admin_id": sa_oid, "period_start": period_start_utc}
    )
    if existing is not None and existing.is_frozen():
        return existing, True

    user_ids = await _sub_admin_user_ids(sa_oid)
    fallback_usd_inr = to_decimal(market_data_service.get_usd_inr_rate())

    gross_loss = Decimal("0")
    gross_profit = Decimal("0")
    brokerage = Decimal("0")

    if user_ids:
        # Positions closed within the week
        closed = await Position.find(
            {
                "user_id": {"$in": user_ids},
                "status": PositionStatus.CLOSED.value,
                "closed_at": {"$gte": period_start_utc, "$lte": period_end_utc},
            }
        ).to_list()
        for p in closed:
            pnl = _realised_inr(p, fallback_usd_inr)
            if pnl < 0:
                gross_loss += -pnl
            elif pnl > 0:
                gross_profit += pnl

        # Brokerage charged within the week
        broker_txns = await WalletTransaction.find(
            {
                "user_id": {"$in": user_ids},
                "transaction_type": TransactionType.BROKERAGE.value,
                "created_at": {"$gte": period_start_utc, "$lte": period_end_utc},
            }
        ).to_list()
        for t in broker_txns:
            brokerage += abs(to_decimal(t.amount))

    net_house = gross_loss - gross_profit + brokerage
    pct = to_decimal(sa.pnl_share_pct) if sa.pnl_share_pct is not None else Decimal("0")
    share = (pct / Decimal("100")) * net_house

    if existing is None:
        existing = AdminSettlement(
            sub_admin_id=sa_oid,
            period_start=period_start_utc,
            period_end=period_end_utc,
        )
    existing.period_end = period_end_utc
    existing.user_count = len(user_ids)
    existing.gross_user_loss_inr = to_decimal128(gross_loss)
    existing.gross_user_profit_inr = to_decimal128(gross_profit)
    existing.total_brokerage_inr = to_decimal128(brokerage)
    existing.net_house_pnl_inr = to_decimal128(net_house)
    existing.pnl_share_pct_snapshot = to_decimal128(pct)
    existing.sub_admin_share_inr = to_decimal128(share)
    existing.status = AdminSettlementStatus.PENDING
    await existing.save()

    await log_event(
        action=AuditAction.SETTLEMENT_COMPUTE,
        entity_type="AdminSettlement",
        entity_id=existing.id,
        actor_id=actor_id,
        target_user_id=sa.id,
        new_values={
            "user_count": existing.user_count,
            "gross_user_loss_inr": str(gross_loss),
            "gross_user_profit_inr": str(gross_profit),
            "total_brokerage_inr": str(brokerage),
            "net_house_pnl_inr": str(net_house),
            "pnl_share_pct": str(pct),
            "sub_admin_share_inr": str(share),
        },
    )
    return existing, False


async def compute_all_for_week(
    period_start_utc: datetime,
    *,
    actor_id: PydanticObjectId | None = None,
) -> list[tuple[AdminSettlement, bool]]:
    sub_admins = await User.find({"role": UserRole.ADMIN.value}).to_list()
    results: list[tuple[AdminSettlement, bool]] = []
    for sa in sub_admins:
        try:
            results.append(
                await compute_settlement(sa.id, period_start_utc, actor_id=actor_id)
            )
        except Exception:
            logger.exception(
                "settlement_compute_failed", extra={"sub_admin_id": str(sa.id)}
            )
            continue
    return results


async def list_settlements_for_week(
    period_start_utc: datetime,
    *,
    actor_id: PydanticObjectId | None = None,
) -> list[tuple[AdminSettlement, User]]:
    """Returns one row per sub-admin for the week.

    For sub-admins that have no row yet, computes one on the fly (caching it).
    """
    period_start_utc, _ = ist_week_bounds(period_start_utc)
    sub_admins = await User.find({"role": UserRole.ADMIN.value}).to_list()
    by_sa = {sa.id: sa for sa in sub_admins}

    existing_rows = await AdminSettlement.find(
        {"sub_admin_id": {"$in": list(by_sa.keys())}, "period_start": period_start_utc}
    ).to_list()
    existing_by_sa = {r.sub_admin_id: r for r in existing_rows}

    out: list[tuple[AdminSettlement, User]] = []
    for sa in sub_admins:
        row = existing_by_sa.get(sa.id)
        if row is None:
            row, _ = await compute_settlement(sa.id, period_start_utc, actor_id=actor_id)
        out.append((row, sa))
    return out


async def history_for_sub_admin(
    sub_admin_id: str | PydanticObjectId,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[AdminSettlement]:
    try:
        oid = PydanticObjectId(sub_admin_id)
    except Exception as e:
        raise ValidationFailedError("Invalid sub-admin id") from e
    q: dict[str, Any] = {"sub_admin_id": oid}
    if from_date or to_date:
        rng: dict[str, Any] = {}
        if from_date:
            rng["$gte"], _ = ist_week_bounds(from_date)
        if to_date:
            _, rng["$lte"] = ist_week_bounds(to_date)
        q["period_start"] = rng
    return await AdminSettlement.find(q).sort("-period_start").to_list()


async def finalize(
    settlement_id: str | PydanticObjectId, actor_id: PydanticObjectId
) -> AdminSettlement:
    try:
        oid = PydanticObjectId(settlement_id)
    except Exception as e:
        raise ValidationFailedError("Invalid settlement id") from e
    row = await AdminSettlement.get(oid)
    if row is None:
        raise NotFoundError("Settlement not found")
    if row.status == AdminSettlementStatus.PAID:
        raise ConflictError("Cannot finalize a settlement that is already paid")
    row.status = AdminSettlementStatus.FINALIZED
    row.finalized_at = datetime.now(UTC)
    row.finalized_by = actor_id
    await row.save()
    await log_event(
        action=AuditAction.SETTLEMENT_FINALIZE,
        entity_type="AdminSettlement",
        entity_id=row.id,
        actor_id=actor_id,
        target_user_id=row.sub_admin_id,
    )
    return row


async def mark_paid(
    settlement_id: str | PydanticObjectId,
    actor_id: PydanticObjectId,
    *,
    notes: str | None = None,
) -> AdminSettlement:
    try:
        oid = PydanticObjectId(settlement_id)
    except Exception as e:
        raise ValidationFailedError("Invalid settlement id") from e
    row = await AdminSettlement.get(oid)
    if row is None:
        raise NotFoundError("Settlement not found")
    if row.status != AdminSettlementStatus.FINALIZED:
        raise ConflictError("Settlement must be finalized before paying")
    row.status = AdminSettlementStatus.PAID
    row.paid_at = datetime.now(UTC)
    row.paid_by = actor_id
    if notes:
        row.notes = notes
    await row.save()
    await log_event(
        action=AuditAction.SETTLEMENT_PAY,
        entity_type="AdminSettlement",
        entity_id=row.id,
        actor_id=actor_id,
        target_user_id=row.sub_admin_id,
        metadata={"notes": notes} if notes else None,
    )
    return row
