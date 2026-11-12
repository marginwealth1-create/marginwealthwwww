"""Weekly P&L-share settlement for brokers.

B-book formula (same as admin settlement)::

    net_house_pnl = gross_user_loss - gross_user_profit + total_brokerage
    broker_share = (pnl_share_pct / 100) * net_house_pnl

Pool definition is INTENTIONALLY direct-clients-only (not the whole subtree)
so that sub-broker pools don't double-count up the chain. A broker's
direct clients = users where ``broker_ancestry[-1] == broker.id``.
Brokerage and the pool filter are the sub-admin settlement's
(``_house_brokerage``, ``_settlement_pool``).
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from beanie import PydanticObjectId
from beanie.exceptions import DocumentNotFound

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.models.audit_log import AuditAction
from app.models.broker_settlement import BrokerSettlement, BrokerSettlementStatus
from app.models.position import Position, PositionStatus
from app.models.user import User, UserRole
from app.services import market_data_service
from app.services.admin_settlement_service import (
    _house_brokerage,
    _settlement_pool,
    ist_week_bounds,
)
from app.services.audit_service import log_event
from app.utils.decimal_utils import to_decimal, to_decimal128
from app.utils.time_utils import UTC

logger = logging.getLogger(__name__)


def _is_usd(p: Position) -> bool:
    return market_data_service.position_is_usd(p)


def _realised_inr(p: Position, fallback_usd_inr: Decimal | None = None) -> Decimal:
    """Rupees the position's realized P&L moved — see
    position_service.realized_pnl_inr (close → open → current)."""
    from app.services.position_service import realized_pnl_inr

    return realized_pnl_inr(p)


async def _direct_client_ids(broker_id: PydanticObjectId) -> list[PydanticObjectId]:
    """Direct clients of a broker: ``assigned_broker_id == broker_id``.
    Excludes sub-broker rows so settlement scope is non-overlapping with
    sub-broker settlements."""
    return await _settlement_pool({"assigned_broker_id": broker_id})


async def compute_settlement(
    broker_id: str | PydanticObjectId,
    period_start_utc: datetime,
    *,
    actor_id: PydanticObjectId | None = None,
) -> tuple[BrokerSettlement, bool]:
    """Compute and upsert the weekly settlement row for one broker.

    Returns ``(settlement, frozen)``. When `frozen` is True the existing
    row is FINALIZED or PAID and was not overwritten."""
    try:
        oid = PydanticObjectId(broker_id)
    except Exception as e:
        raise ValidationFailedError("Invalid broker id") from e
    b = await User.get(oid)
    if b is None or b.role != UserRole.BROKER:
        raise NotFoundError("Broker not found")

    period_start_utc, period_end_utc = ist_week_bounds(period_start_utc)

    existing = await BrokerSettlement.find_one(
        {"broker_id": oid, "period_start": period_start_utc}
    )
    if existing is not None and existing.is_frozen():
        return existing, True

    user_ids = await _direct_client_ids(oid)
    fallback_usd_inr = to_decimal(market_data_service.get_usd_inr_rate())

    gross_loss = Decimal("0")
    gross_profit = Decimal("0")
    brokerage = Decimal("0")

    if user_ids:
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

        brokerage = await _house_brokerage(user_ids, period_start_utc, period_end_utc)

    net_house = gross_loss - gross_profit + brokerage
    # Split share: PnL % on the trading-P&L leg (loss − profit), brokerage %
    # on the brokerage leg. Pre-split brokers (broker_brokerage_share_pct is
    # None) inherit the PnL % for brokerage too, so their share is identical
    # to the old single-% math:
    #     pnl% × (loss − profit) + pnl% × brokerage  ==  pnl% × net_house.
    pnl_pct = (
        to_decimal(b.broker_pnl_share_pct)
        if b.broker_pnl_share_pct is not None
        else Decimal("0")
    )
    bkg_pct = (
        to_decimal(b.broker_brokerage_share_pct)
        if getattr(b, "broker_brokerage_share_pct", None) is not None
        else pnl_pct
    )
    pnl_leg = gross_loss - gross_profit
    share = (pnl_pct / Decimal("100")) * pnl_leg + (bkg_pct / Decimal("100")) * brokerage
    pct = pnl_pct  # snapshot column keeps the PnL %

    if existing is None:
        existing = BrokerSettlement(
            broker_id=oid,
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
    existing.broker_share_inr = to_decimal128(share)
    existing.status = BrokerSettlementStatus.PENDING
    if existing.id is None:
        await existing.insert()
    else:
        existing.updated_at = datetime.now(UTC)
        try:
            # Rewrite only a row that is still PENDING: a finalize that landed
            # while this ran must never be overwritten.
            await BrokerSettlement.find_one(
                {"_id": existing.id, "status": BrokerSettlementStatus.PENDING.value}
            ).replace_one(existing)
        except DocumentNotFound:
            return (await BrokerSettlement.get(existing.id)) or existing, True

    await log_event(
        action=AuditAction.BROKER_SETTLEMENT_COMPUTE,
        entity_type="BrokerSettlement",
        entity_id=existing.id,
        actor_id=actor_id,
        target_user_id=b.id,
        new_values={
            "user_count": existing.user_count,
            "gross_user_loss_inr": str(gross_loss),
            "gross_user_profit_inr": str(gross_profit),
            "total_brokerage_inr": str(brokerage),
            "net_house_pnl_inr": str(net_house),
            "pnl_share_pct": str(pct),
            "broker_share_inr": str(share),
        },
    )
    return existing, False


async def compute_all_for_week(
    period_start_utc: datetime,
    *,
    actor_id: PydanticObjectId | None = None,
    scoped_admin_id: PydanticObjectId | None = None,
) -> list[tuple[BrokerSettlement, bool]]:
    """When `scoped_admin_id` is set, only brokers in that admin's pool are
    computed (admin viewing their own brokers). When None, all brokers."""
    q: dict[str, Any] = {"role": UserRole.BROKER.value}
    if scoped_admin_id is not None:
        q["assigned_admin_id"] = scoped_admin_id
    brokers = await User.find(q).to_list()
    results: list[tuple[BrokerSettlement, bool]] = []
    for br in brokers:
        try:
            results.append(
                await compute_settlement(br.id, period_start_utc, actor_id=actor_id)
            )
        except Exception:
            logger.exception(
                "broker_settlement_compute_failed",
                extra={"broker_id": str(br.id)},
            )
            continue
    return results


async def list_settlements_for_week(
    period_start_utc: datetime,
    *,
    actor_id: PydanticObjectId | None = None,
    scoped_admin_id: PydanticObjectId | None = None,
) -> list[tuple[BrokerSettlement, User]]:
    period_start_utc, _ = ist_week_bounds(period_start_utc)
    q: dict[str, Any] = {"role": UserRole.BROKER.value}
    if scoped_admin_id is not None:
        q["assigned_admin_id"] = scoped_admin_id
    brokers = await User.find(q).to_list()
    by_id = {br.id: br for br in brokers}
    existing_rows = await BrokerSettlement.find(
        {"broker_id": {"$in": list(by_id.keys())}, "period_start": period_start_utc}
    ).to_list()
    existing_by_id = {r.broker_id: r for r in existing_rows}
    out: list[tuple[BrokerSettlement, User]] = []
    for br in brokers:
        row = existing_by_id.get(br.id)
        if row is None:
            row, _ = await compute_settlement(
                br.id, period_start_utc, actor_id=actor_id
            )
        out.append((row, br))
    return out


async def history_for_broker(
    broker_id: str | PydanticObjectId,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[BrokerSettlement]:
    try:
        oid = PydanticObjectId(broker_id)
    except Exception as e:
        raise ValidationFailedError("Invalid broker id") from e
    q: dict[str, Any] = {"broker_id": oid}
    if from_date or to_date:
        rng: dict[str, Any] = {}
        if from_date:
            rng["$gte"], _ = ist_week_bounds(from_date)
        if to_date:
            _, rng["$lte"] = ist_week_bounds(to_date)
        q["period_start"] = rng
    return await BrokerSettlement.find(q).sort("-period_start").to_list()


async def finalize(
    settlement_id: str | PydanticObjectId, actor_id: PydanticObjectId
) -> BrokerSettlement:
    try:
        oid = PydanticObjectId(settlement_id)
    except Exception as e:
        raise ValidationFailedError("Invalid settlement id") from e
    row = await BrokerSettlement.get(oid)
    if row is None:
        raise NotFoundError("Settlement not found")
    if row.status == BrokerSettlementStatus.PAID:
        raise ConflictError("Cannot finalize a settlement that is already paid")
    row.status = BrokerSettlementStatus.FINALIZED
    row.finalized_at = datetime.now(UTC)
    row.finalized_by = actor_id
    await row.save()
    await log_event(
        action=AuditAction.BROKER_SETTLEMENT_FINALIZE,
        entity_type="BrokerSettlement",
        entity_id=row.id,
        actor_id=actor_id,
        target_user_id=row.broker_id,
    )
    return row


async def mark_paid(
    settlement_id: str | PydanticObjectId,
    actor_id: PydanticObjectId,
    *,
    notes: str | None = None,
) -> BrokerSettlement:
    try:
        oid = PydanticObjectId(settlement_id)
    except Exception as e:
        raise ValidationFailedError("Invalid settlement id") from e
    row = await BrokerSettlement.get(oid)
    if row is None:
        raise NotFoundError("Settlement not found")
    if row.status != BrokerSettlementStatus.FINALIZED:
        raise ConflictError("Settlement must be finalized before paying")
    row.status = BrokerSettlementStatus.PAID
    row.paid_at = datetime.now(UTC)
    row.paid_by = actor_id
    if notes:
        row.notes = notes
    await row.save()
    await log_event(
        action=AuditAction.BROKER_SETTLEMENT_PAY,
        entity_type="BrokerSettlement",
        entity_id=row.id,
        actor_id=actor_id,
        target_user_id=row.broker_id,
        metadata={"notes": notes} if notes else None,
    )
    return row
