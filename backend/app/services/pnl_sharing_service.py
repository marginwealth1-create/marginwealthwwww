"""P&L Sharing — compute, CRUD, and settlement service.

Pure compute functions are at the top (no DB writes — easy to test).
CRUD and settle helpers will be appended in later tasks.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId
from bson import Decimal128
from pymongo.errors import DuplicateKeyError

from app.core.exceptions import (
    ConflictError,
    InsufficientFundsError,
    ValidationFailedError,
)
from app.models.audit_log import AuditAction
from app.models.pnl_sharing import (
    AgreementStatus,
    AgreementType,
    PnlSharingAgreement,
    PnlSharingSettlement,
    SettlementCadence,
    SettlementMode,
    SharingSettlementStatus,
)
from app.models.position import Position, PositionStatus
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User, UserRole
from app.schemas.pnl_sharing import ReportRow, ReportSummary
from app.services import market_data_service, wallet_service
from app.services.admin_settlement_service import _realised_inr
from app.services.audit_service import log_event
from app.utils.decimal_utils import quantize_money, to_decimal
from app.utils.time_utils import now_utc

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


def _d128(value: Decimal) -> Decimal128:
    """Decimal → Decimal128 with str conversion (BSON safest path)."""
    return Decimal128(str(value))


def _as_ist(dt: datetime) -> datetime:
    """Treat naive datetimes as IST; convert tz-aware to IST."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def compute_period_bounds(
    cadence: SettlementCadence, ref_dt: datetime
) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for the period containing ref_dt.

    Bounds are IST-anchored. End is inclusive of last millisecond
    (23:59:59.999000 in IST).
    """
    ref_ist = _as_ist(ref_dt)

    if cadence == SettlementCadence.DAILY:
        start_ist = ref_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        end_ist = start_ist.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif cadence == SettlementCadence.WEEKLY:
        # Same Mon-Sun IST week as admin_settlement_service.ist_week_bounds —
        # kept inline here so DAILY/WEEKLY/MONTHLY share one switch.
        days_since_monday = ref_ist.weekday()  # Mon=0
        monday_ist = (ref_ist - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_ist = monday_ist
        end_ist = (monday_ist + timedelta(days=6)).replace(
            hour=23, minute=59, second=59, microsecond=999999
        )
    elif cadence == SettlementCadence.MONTHLY:
        start_ist = ref_ist.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_day = calendar.monthrange(ref_ist.year, ref_ist.month)[1]
        end_ist = start_ist.replace(
            day=last_day, hour=23, minute=59, second=59, microsecond=999999
        )
    else:
        raise ValueError(f"Unknown cadence: {cadence}")

    return start_ist.astimezone(UTC), end_ist.astimezone(UTC)


# ── Snapshot aggregation ─────────────────────────────────────────────
@dataclass(frozen=True)
class SharingSnapshot:
    """One period's aggregated view of a broker's clients' P&L and brokerage.

    Sign convention (locked):
      - ``net_client_pnl_inr`` is CLIENT-view: positive means clients profited,
        negative means clients lost (= broker won that side).
      - ``net_client_bkg_inr`` is the absolute total brokerage collected; ≥ 0.
      - ``total_of_both_inr`` is BROKER-view = -(net_client_pnl) + net_client_bkg.
        Positive means the broker gained (clients lost + brokerage); negative
        means the broker lost.
      - ``actual_pnl_inr`` is the broker's net economic position for the period.
        In Phase A there is no prior-settlement deduction, so it equals
        ``total_of_both_inr``.
      - ``sharing_pnl_inr`` is the admin's share of the broker's PnL component:
        ``share_pct% × (-(net_client_pnl))``.
      - ``sharing_bkg_inr`` is the admin's share of the brokerage component:
        ``share_pct% × net_client_bkg``.
      - ``sharing_total_inr`` is ``sharing_pnl_inr + sharing_bkg_inr``.
    """

    net_client_pnl_inr: Decimal
    net_client_bkg_inr: Decimal
    total_of_both_inr: Decimal
    actual_pnl_inr: Decimal
    sharing_pnl_inr: Decimal
    sharing_bkg_inr: Decimal
    sharing_total_inr: Decimal


async def _broker_client_ids(
    broker_id: PydanticObjectId,
) -> list[PydanticObjectId]:
    """User ids in this broker's entire subtree (direct clients + sub-brokers + their clients).

    Matches via ``broker_ancestry`` multikey index — captures everyone whose
    ancestor chain includes ``broker_id``: direct clients (parent broker is
    in their ancestry), nested sub-brokers, and clients of those sub-brokers.

    Brokers themselves don't have closed Positions of their own, so including
    them in the lookup is harmless — the Position query filters by
    ``status=CLOSED`` and brokers don't trade.
    """
    coll = User.get_motor_collection()
    cursor = coll.find({"broker_ancestry": broker_id}, {"_id": 1})
    return [doc["_id"] async for doc in cursor]


async def compute_sharing_snapshot(
    agreement: PnlSharingAgreement,
    period_start: datetime,
    period_end: datetime,
) -> SharingSnapshot:
    """Aggregate the broker's clients' realized P&L + brokerage in window
    and apply the agreement's share %.

    Pure-ish: reads from Mongo (Position / WalletTransaction / User) but
    writes nothing. The result is a frozen ``SharingSnapshot`` dataclass.

    Mirrors ``admin_settlement_service.compute_settlement`` at broker-level
    via ``assigned_broker_id`` instead of admin-level via ``assigned_admin_id``.
    """
    user_ids = await _broker_client_ids(agreement.broker_id)

    net_client_pnl = Decimal("0")
    net_client_bkg = Decimal("0")

    if user_ids:
        fallback_usd_inr = to_decimal(market_data_service.get_usd_inr_rate())

        positions = await Position.find(
            {
                "user_id": {"$in": user_ids},
                "status": PositionStatus.CLOSED.value,
                "closed_at": {"$gte": period_start, "$lte": period_end},
            }
        ).to_list()
        for p in positions:
            net_client_pnl += _realised_inr(p, fallback_usd_inr)

        bkg_txns = await WalletTransaction.find(
            {
                "user_id": {"$in": user_ids},
                "transaction_type": TransactionType.BROKERAGE.value,
                "created_at": {"$gte": period_start, "$lte": period_end},
            }
        ).to_list()
        for t in bkg_txns:
            net_client_bkg += abs(to_decimal(t.amount))

    share_frac = to_decimal(agreement.share_pct) / Decimal("100")
    broker_view_pnl = -net_client_pnl
    total_of_both = broker_view_pnl + net_client_bkg

    sharing_pnl = quantize_money(broker_view_pnl * share_frac)
    sharing_bkg = quantize_money(net_client_bkg * share_frac)

    # If BROKERAGE_ONLY, force sharing_pnl to 0 and recompute total.
    if agreement.agreement_type == AgreementType.BROKERAGE_ONLY:
        sharing_pnl = Decimal("0")
        # broker_view_pnl and net_client_pnl_inr are still surfaced in the
        # snapshot (display purposes — admin can see what the user did),
        # but the agreement's economic share is zero for the PNL component.
        # total_of_both stays as-is (it's a broker-view aggregate).

    return SharingSnapshot(
        net_client_pnl_inr=quantize_money(net_client_pnl),
        net_client_bkg_inr=quantize_money(net_client_bkg),
        total_of_both_inr=quantize_money(total_of_both),
        actual_pnl_inr=quantize_money(total_of_both),
        sharing_pnl_inr=sharing_pnl,
        sharing_bkg_inr=sharing_bkg,
        sharing_total_inr=sharing_pnl + sharing_bkg,
    )


# ── Agreement CRUD ───────────────────────────────────────────────────
class AgreementValidationError(ValidationFailedError):
    """Validation failure on agreement create/update."""


class AgreementConflict(ConflictError):
    """An ACTIVE/PAUSED agreement for (admin, broker) already exists."""


async def create_agreement(
    *,
    actor: User,
    admin_id: PydanticObjectId,
    broker_id: PydanticObjectId,
    share_pct: Decimal,
    settlement_mode: SettlementMode,
    settlement_cadence: SettlementCadence | None,
    agreement_type: AgreementType = AgreementType.PNL_AND_BROKERAGE,
) -> PnlSharingAgreement:
    if not (Decimal("0") <= share_pct <= Decimal("100")):
        raise AgreementValidationError("share_pct must be in [0, 100]")
    if settlement_mode == SettlementMode.AUTO and settlement_cadence is None:
        raise AgreementValidationError("AUTO mode requires cadence")
    if settlement_mode == SettlementMode.MANUAL and settlement_cadence is not None:
        raise AgreementValidationError("MANUAL mode must not set cadence")

    admin = await User.get(admin_id)
    if admin is None or admin.role != UserRole.ADMIN:
        raise AgreementValidationError("admin_id is not a valid admin user")

    broker = await User.get(broker_id)
    if (
        broker is None
        or broker.role != UserRole.BROKER
        or broker.assigned_admin_id != admin_id
    ):
        raise AgreementValidationError("broker_id is not a broker under admin_id")

    existing = await PnlSharingAgreement.find_one(
        PnlSharingAgreement.admin_id == admin_id,
        PnlSharingAgreement.broker_id == broker_id,
        PnlSharingAgreement.agreement_type == agreement_type,
        PnlSharingAgreement.status != AgreementStatus.ENDED,
    )
    if existing is not None:
        raise AgreementConflict(f"Active agreement already exists: {existing.id}")

    a = PnlSharingAgreement(
        admin_id=admin_id,
        broker_id=broker_id,
        share_pct=Decimal128(str(share_pct)),
        settlement_mode=settlement_mode,
        settlement_cadence=settlement_cadence,
        agreement_type=agreement_type,
        status=AgreementStatus.ACTIVE,
        effective_from=now_utc(),
        created_by=actor.id,
        last_modified_by=actor.id,
    )
    try:
        await a.insert()
    except DuplicateKeyError as e:
        # Two parallel callers slipped past the find_one pre-check; the unique
        # partial index in PnlSharingAgreement collapses the race into a clean
        # conflict instead of a 500.
        raise AgreementConflict(
            f"Active agreement already exists for admin={admin_id} broker={broker_id}"
        ) from e

    await log_event(
        action=AuditAction.PNL_SHARING_AGREEMENT_CREATE,
        entity_type="PnlSharingAgreement",
        entity_id=a.id,
        actor_id=actor.id,
        target_user_id=broker_id,
        new_values={
            "admin_id": str(admin_id),
            "broker_id": str(broker_id),
            "share_pct": str(share_pct),
            "settlement_mode": settlement_mode.value,
            "settlement_cadence": settlement_cadence.value
            if settlement_cadence
            else None,
            "agreement_type": agreement_type.value,
        },
    )
    return a


async def update_agreement(
    *,
    actor: User,
    agreement_id: PydanticObjectId,
    share_pct: Decimal | None = None,
    settlement_mode: SettlementMode | None = None,
    settlement_cadence: SettlementCadence | None = None,
) -> PnlSharingAgreement:
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise AgreementValidationError("agreement not found")
    if a.status == AgreementStatus.ENDED:
        raise AgreementValidationError("cannot edit ENDED agreement")

    old_values = {
        "share_pct": str(a.share_pct),
        "settlement_mode": a.settlement_mode.value,
        "settlement_cadence": a.settlement_cadence.value
        if a.settlement_cadence
        else None,
    }

    if share_pct is not None:
        if not (Decimal("0") <= share_pct <= Decimal("100")):
            raise AgreementValidationError("share_pct must be in [0, 100]")
        a.share_pct = Decimal128(str(share_pct))

    if settlement_mode is not None:
        a.settlement_mode = settlement_mode
        if settlement_mode == SettlementMode.MANUAL:
            a.settlement_cadence = None
    if settlement_cadence is not None:
        a.settlement_cadence = settlement_cadence

    if a.settlement_mode == SettlementMode.AUTO and a.settlement_cadence is None:
        raise AgreementValidationError("AUTO mode requires cadence")

    a.last_modified_by = actor.id
    await a.save()
    await log_event(
        action=AuditAction.PNL_SHARING_AGREEMENT_UPDATE,
        entity_type="PnlSharingAgreement",
        entity_id=a.id,
        actor_id=actor.id,
        target_user_id=a.broker_id,
        old_values=old_values,
        new_values={
            "share_pct": str(a.share_pct),
            "settlement_mode": a.settlement_mode.value,
            "settlement_cadence": a.settlement_cadence.value
            if a.settlement_cadence
            else None,
        },
    )
    return a


async def pause_agreement(
    *, actor: User, agreement_id: PydanticObjectId
) -> PnlSharingAgreement:
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise AgreementValidationError("agreement not found")
    if a.status != AgreementStatus.ACTIVE:
        raise AgreementValidationError(f"cannot pause from status {a.status}")
    a.status = AgreementStatus.PAUSED
    a.last_modified_by = actor.id
    await a.save()
    await log_event(
        action=AuditAction.PNL_SHARING_AGREEMENT_PAUSE,
        entity_type="PnlSharingAgreement",
        entity_id=a.id,
        actor_id=actor.id,
        target_user_id=a.broker_id,
        old_values={"status": AgreementStatus.ACTIVE.value},
        new_values={"status": AgreementStatus.PAUSED.value},
    )
    return a


async def resume_agreement(
    *, actor: User, agreement_id: PydanticObjectId
) -> PnlSharingAgreement:
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise AgreementValidationError("agreement not found")
    if a.status != AgreementStatus.PAUSED:
        raise AgreementValidationError(f"cannot resume from status {a.status}")
    a.status = AgreementStatus.ACTIVE
    a.last_modified_by = actor.id
    await a.save()
    await log_event(
        action=AuditAction.PNL_SHARING_AGREEMENT_RESUME,
        entity_type="PnlSharingAgreement",
        entity_id=a.id,
        actor_id=actor.id,
        target_user_id=a.broker_id,
        old_values={"status": AgreementStatus.PAUSED.value},
        new_values={"status": AgreementStatus.ACTIVE.value},
    )
    return a


async def end_agreement(
    *, actor: User, agreement_id: PydanticObjectId
) -> PnlSharingAgreement:
    a = await PnlSharingAgreement.get(agreement_id)
    if a is None:
        raise AgreementValidationError("agreement not found")
    if a.status == AgreementStatus.ENDED:
        raise AgreementValidationError("already ended")
    old_status = a.status
    a.status = AgreementStatus.ENDED
    a.effective_until = now_utc()
    a.last_modified_by = actor.id
    await a.save()
    await log_event(
        action=AuditAction.PNL_SHARING_AGREEMENT_END,
        entity_type="PnlSharingAgreement",
        entity_id=a.id,
        actor_id=actor.id,
        target_user_id=a.broker_id,
        old_values={"status": old_status.value},
        new_values={"status": AgreementStatus.ENDED.value},
    )
    return a


async def list_agreements_for_actor(
    *,
    actor: User,
    status: AgreementStatus | None = None,
    admin_id: PydanticObjectId | None = None,
    broker_id: PydanticObjectId | None = None,
    agreement_type: AgreementType | None = None,
    skip: int = 0,
    limit: int = 50,
) -> list[PnlSharingAgreement]:
    q = PnlSharingAgreement.find()
    if actor.role == UserRole.ADMIN:
        q = q.find(PnlSharingAgreement.admin_id == actor.id)
    elif actor.role == UserRole.BROKER:
        q = q.find(PnlSharingAgreement.broker_id == actor.id)
    if status is not None:
        q = q.find(PnlSharingAgreement.status == status)
    if admin_id is not None:
        q = q.find(PnlSharingAgreement.admin_id == admin_id)
    if broker_id is not None:
        q = q.find(PnlSharingAgreement.broker_id == broker_id)
    if agreement_type is not None:
        q = q.find(PnlSharingAgreement.agreement_type == agreement_type)
    return await q.skip(skip).limit(limit).to_list()


# ── Settlement ───────────────────────────────────────────────────────
async def settle_period(
    *,
    agreement_id: PydanticObjectId,
    period_start: datetime,
    period_end: datetime,
    cadence: SettlementCadence,
    triggered_by: Literal["AUTO", "MANUAL"],
    actor: User | None = None,
) -> PnlSharingSettlement:
    """Compute snapshot for period and transfer wallet amount admin↔broker.

    Idempotent: the unique (agreement_id, period_start) index prevents a
    double-fire — if a SETTLED row already exists for the period, return it
    unchanged. If a PENDING/FAILED row exists, this call will retry it by
    refreshing the snapshot and re-attempting wallet movement.

    Direction convention:
      - sharing_total > 0: broker pays admin (broker debit, admin credit)
      - sharing_total < 0: admin pays broker (admin debit, broker credit)
      - sharing_total == 0: skip wallet calls entirely
    """
    agreement = await PnlSharingAgreement.get(agreement_id)
    if agreement is None:
        raise AgreementValidationError("agreement not found")

    existing = await PnlSharingSettlement.find_one(
        PnlSharingSettlement.agreement_id == agreement_id,
        PnlSharingSettlement.period_start == period_start,
    )
    if existing is not None and existing.status == SharingSettlementStatus.SETTLED:
        return existing  # already done — idempotent

    snap = await compute_sharing_snapshot(agreement, period_start, period_end)

    if existing is None:
        row = PnlSharingSettlement(
            agreement_id=agreement_id,
            admin_id=agreement.admin_id,
            broker_id=agreement.broker_id,
            period_start=period_start,
            period_end=period_end,
            cadence=cadence,
            net_client_pnl_inr=_d128(snap.net_client_pnl_inr),
            net_client_bkg_inr=_d128(snap.net_client_bkg_inr),
            total_of_both_inr=_d128(snap.total_of_both_inr),
            actual_pnl_inr=_d128(snap.actual_pnl_inr),
            share_pct_snapshot=agreement.share_pct,
            sharing_pnl_inr=_d128(snap.sharing_pnl_inr),
            sharing_bkg_inr=_d128(snap.sharing_bkg_inr),
            sharing_total_inr=_d128(snap.sharing_total_inr),
            status=SharingSettlementStatus.PENDING,
        )
        try:
            await row.insert()
        except DuplicateKeyError:
            # Another concurrent call inserted the row first. Re-read and proceed
            # via the retry path (snapshot refresh + wallet attempt).
            row = await PnlSharingSettlement.find_one(
                PnlSharingSettlement.agreement_id == agreement_id,
                PnlSharingSettlement.period_start == period_start,
            )
            if row is None:
                raise  # paranoid: should not happen given unique index
            if row.status == SharingSettlementStatus.SETTLED:
                return row
            row.retry_count += 1
            # Refresh snapshot fields on the now-existing row
            row.net_client_pnl_inr = _d128(snap.net_client_pnl_inr)
            row.net_client_bkg_inr = _d128(snap.net_client_bkg_inr)
            row.total_of_both_inr = _d128(snap.total_of_both_inr)
            row.actual_pnl_inr = _d128(snap.actual_pnl_inr)
            row.sharing_pnl_inr = _d128(snap.sharing_pnl_inr)
            row.sharing_bkg_inr = _d128(snap.sharing_bkg_inr)
            row.sharing_total_inr = _d128(snap.sharing_total_inr)
    else:
        row = existing
        row.retry_count += 1
        # Refresh snapshot fields in case data has changed since FAILED state
        row.net_client_pnl_inr = _d128(snap.net_client_pnl_inr)
        row.net_client_bkg_inr = _d128(snap.net_client_bkg_inr)
        row.total_of_both_inr = _d128(snap.total_of_both_inr)
        row.actual_pnl_inr = _d128(snap.actual_pnl_inr)
        row.sharing_pnl_inr = _d128(snap.sharing_pnl_inr)
        row.sharing_bkg_inr = _d128(snap.sharing_bkg_inr)
        row.sharing_total_inr = _d128(snap.sharing_total_inr)

    amount = snap.sharing_total_inr
    tx_admin: WalletTransaction | None = None
    tx_broker: WalletTransaction | None = None

    try:
        if amount > 0:
            # Broker pays admin → debit broker, credit admin
            tx_broker = await wallet_service.adjust(
                user_id=agreement.broker_id,
                amount=-amount,  # negative = debit
                transaction_type=TransactionType.PNL_SHARING_PAYOUT,
                narration=f"P&L sharing payout for period {period_start.date()}",
                reference_type="PnlSharingSettlement",
                reference_id=str(row.id),
                actor_id=actor.id if actor else None,
            )
            tx_admin = await wallet_service.adjust(
                user_id=agreement.admin_id,
                amount=amount,  # positive = credit
                transaction_type=TransactionType.PNL_SHARING_RECEIPT,
                narration=f"P&L sharing receipt for period {period_start.date()}",
                reference_type="PnlSharingSettlement",
                reference_id=str(row.id),
                actor_id=actor.id if actor else None,
            )
        elif amount < 0:
            # Admin pays broker → debit admin, credit broker
            abs_amt = -amount
            tx_admin = await wallet_service.adjust(
                user_id=agreement.admin_id,
                amount=-abs_amt,  # negative = debit
                transaction_type=TransactionType.PNL_SHARING_PAYOUT,
                narration=f"P&L sharing payout for period {period_start.date()}",
                reference_type="PnlSharingSettlement",
                reference_id=str(row.id),
                actor_id=actor.id if actor else None,
            )
            tx_broker = await wallet_service.adjust(
                user_id=agreement.broker_id,
                amount=abs_amt,
                transaction_type=TransactionType.PNL_SHARING_RECEIPT,
                narration=f"P&L sharing receipt for period {period_start.date()}",
                reference_type="PnlSharingSettlement",
                reference_id=str(row.id),
                actor_id=actor.id if actor else None,
            )
        # amount == 0: skip wallet calls

        row.transaction_ref_admin = tx_admin.id if tx_admin else None
        row.transaction_ref_broker = tx_broker.id if tx_broker else None
        row.status = SharingSettlementStatus.SETTLED
        row.settled_at = now_utc()
        row.settled_by = actor.id if actor else None
        row.failure_reason = None
    except InsufficientFundsError as e:
        row.status = SharingSettlementStatus.FAILED
        row.failure_reason = str(e)[:500]
        # NOTE: if tx_broker succeeded but tx_admin then failed, the broker debit
        # is already booked. Phase A accepts this (rare in MANUAL mode; manual
        # retry will resume from PENDING and either succeed or stay FAILED).
        # Phase B will add a true transaction wrapping mechanism.
    except Exception as e:  # noqa: BLE001 — defensive
        row.status = SharingSettlementStatus.FAILED
        row.failure_reason = f"unexpected: {type(e).__name__}: {str(e)[:400]}"

    await row.save()

    audit_action = (
        AuditAction.PNL_SHARING_SETTLEMENT_SETTLED
        if row.status == SharingSettlementStatus.SETTLED
        else AuditAction.PNL_SHARING_SETTLEMENT_FAILED
    )
    await log_event(
        action=audit_action,
        entity_type="PnlSharingSettlement",
        entity_id=row.id,
        actor_id=actor.id if actor else None,
        target_user_id=row.broker_id,
        new_values={
            "agreement_id": str(row.agreement_id),
            "period_start": row.period_start.isoformat(),
            "period_end": row.period_end.isoformat(),
            "cadence": row.cadence.value,
            "sharing_total_inr": str(row.sharing_total_inr),
            "status": row.status.value,
            "retry_count": row.retry_count,
            "triggered_by": triggered_by,
            "failure_reason": row.failure_reason,
        },
    )
    return row


# ── Reports ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Report:
    rows: list[ReportRow]
    summary: ReportSummary


async def build_report(
    *,
    agreement: PnlSharingAgreement,
    cadence: SettlementCadence,
    from_dt: datetime,
    to_dt: datetime,
) -> Report:
    """Walk periods from from_dt to to_dt at given cadence; snapshot each.

    For each period, also look up the corresponding PnlSharingSettlement row
    (if any) to determine the settlement_status ("SETTLED" / "PENDING" /
    "FAILED" / "UNSETTLED").
    """
    rows: list[ReportRow] = []
    total_pnl = Decimal("0")
    total_bkg = Decimal("0")
    settled = pending = failed = unsettled = 0

    cursor = _as_ist(from_dt)
    end_ist = _as_ist(to_dt)
    while cursor <= end_ist:
        p_start, p_end = compute_period_bounds(cadence, cursor)
        snap = await compute_sharing_snapshot(agreement, p_start, p_end)

        existing = await PnlSharingSettlement.find_one(
            PnlSharingSettlement.agreement_id == agreement.id,
            PnlSharingSettlement.period_start == p_start,
        )
        if existing is None:
            status_label = "UNSETTLED"
            unsettled += 1
        elif existing.status == SharingSettlementStatus.SETTLED:
            status_label = "SETTLED"
            settled += 1
        elif existing.status == SharingSettlementStatus.FAILED:
            status_label = "FAILED"
            failed += 1
        else:
            status_label = "PENDING"
            pending += 1

        rows.append(
            ReportRow(
                period_start=p_start,
                period_end=p_end,
                net_client_pnl_inr=str(snap.net_client_pnl_inr),
                net_client_bkg_inr=str(snap.net_client_bkg_inr),
                total_of_both_inr=str(snap.total_of_both_inr),
                actual_pnl_inr=str(snap.actual_pnl_inr),
                sharing_pnl_inr=str(snap.sharing_pnl_inr),
                sharing_bkg_inr=str(snap.sharing_bkg_inr),
                settlement_status=status_label,
            )
        )
        total_pnl += snap.sharing_pnl_inr
        total_bkg += snap.sharing_bkg_inr

        # Advance cursor past this period (1ms after p_end to ensure progress)
        cursor = _as_ist(p_end) + timedelta(milliseconds=1)

    summary = ReportSummary(
        total_sharing_pnl_inr=str(total_pnl),
        total_sharing_bkg_inr=str(total_bkg),
        periods_settled=settled,
        periods_pending=pending,
        periods_failed=failed,
        periods_unsettled=unsettled,
    )
    return Report(rows=rows, summary=summary)


# ── Scheduler ────────────────────────────────────────────────────────
async def find_due_settlements(
    *,
    now: datetime | None = None,
) -> list[tuple[PnlSharingAgreement, datetime, datetime]]:
    """Find (agreement, period_start, period_end) eligible for auto-settle.

    Returns the most recently CLOSED period per the agreement's cadence for each
    ACTIVE + AUTO agreement, subject to:
    - effective_from must be on/before period_start (no partial-period backfill)
    - period_end must be before `now` (period actually closed)
    - no SETTLED row already exists for that period (FAILED/PENDING re-fire OK)
    """
    now = now or now_utc()
    agreements = await PnlSharingAgreement.find(
        PnlSharingAgreement.status == AgreementStatus.ACTIVE,
        PnlSharingAgreement.settlement_mode == SettlementMode.AUTO,
    ).to_list()

    due: list[tuple[PnlSharingAgreement, datetime, datetime]] = []
    for agreement in agreements:
        cadence = agreement.settlement_cadence
        if cadence is None:
            continue  # invariant violation; AUTO mode should always have cadence

        # Step back into the previous period
        if cadence == SettlementCadence.DAILY:
            ref = now - timedelta(days=1)
        elif cadence == SettlementCadence.WEEKLY:
            ref = now - timedelta(days=7)
        elif cadence == SettlementCadence.MONTHLY:
            ref = now - timedelta(days=32)  # always lands in the previous month
        else:
            continue

        period_start, period_end = compute_period_bounds(cadence, ref)

        # Skip if agreement was created after this period started
        if agreement.effective_from > period_start:
            continue

        # Skip if the period hasn't actually closed yet
        if period_end >= now:
            continue

        # Skip if a SETTLED row already exists (idempotency optimisation)
        existing_settled = await PnlSharingSettlement.find_one(
            PnlSharingSettlement.agreement_id == agreement.id,
            PnlSharingSettlement.period_start == period_start,
            PnlSharingSettlement.status == SharingSettlementStatus.SETTLED,
        )
        if existing_settled is not None:
            continue

        due.append((agreement, period_start, period_end))
    return due


# ── Scheduler loop ───────────────────────────────────────────────────
logger = logging.getLogger(__name__)

_pnl_sharing_scheduler_stop = False


async def pnl_sharing_scheduler_loop(interval_sec: float = 300.0) -> None:
    """Background scheduler — auto-settle AUTO-mode agreements at period close.

    Polls every `interval_sec` seconds (default 5 min). Each tick:
      1. Calls find_due_settlements() to identify (agreement, period) pairs
         that should be settled.
      2. For each, calls settle_period(triggered_by="AUTO"). Idempotency is
         guaranteed by the unique (agreement_id, period_start) index.
      3. FAILED rows are retried on each subsequent tick until SETTLED, or
         until the agreement is PAUSED/ENDED.

    Started in main.py lifespan; stopped via stop_pnl_sharing_scheduler().
    """
    global _pnl_sharing_scheduler_stop
    _pnl_sharing_scheduler_stop = False
    logger.info(
        "pnl_sharing_scheduler_started",
        extra={"interval_sec": interval_sec},
    )
    while not _pnl_sharing_scheduler_stop:
        try:
            due = await find_due_settlements()
            for agreement, period_start, period_end in due:
                try:
                    await settle_period(
                        agreement_id=agreement.id,
                        period_start=period_start,
                        period_end=period_end,
                        cadence=agreement.settlement_cadence,
                        triggered_by="AUTO",
                        actor=None,
                    )
                except Exception:
                    # Per-agreement failure shouldn't stop the loop.
                    logger.exception(
                        "pnl_sharing_auto_settle_failed",
                        extra={"agreement_id": str(agreement.id)},
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("pnl_sharing_scheduler_tick_error")

        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            break
    logger.info("pnl_sharing_scheduler_stopped")


def stop_pnl_sharing_scheduler() -> None:
    """Signal the scheduler loop to exit on its next tick. Called from main.py shutdown."""
    global _pnl_sharing_scheduler_stop
    _pnl_sharing_scheduler_stop = True


async def heal_pnl_sharing_agreement_type() -> int:
    """Idempotent backfill: set agreement_type=PNL_AND_BROKERAGE on any rows
    missing it, then drop the legacy `uniq_active_admin_broker` index if
    present (the new `uniq_active_admin_broker_type` index replaces it).

    Returns count of rows backfilled.
    """
    coll = PnlSharingAgreement.get_motor_collection()

    # Backfill missing field
    res = await coll.update_many(
        {"agreement_type": {"$exists": False}},
        {"$set": {"agreement_type": "PNL_AND_BROKERAGE"}},
    )

    # Drop the legacy index if present (new model defines a different name)
    try:
        await coll.drop_index("uniq_active_admin_broker")
    except Exception:
        pass  # already dropped or never existed

    return res.modified_count


async def publish_pnl_sharing_update(broker_id: PydanticObjectId) -> None:
    """Best-effort WS notify when a position close MAY have changed the
    snapshot for any agreement under this broker.

    Called from the matching-engine close path. Frontend admin/broker views
    use the `pnl_sharing_update` event to invalidate report query keys and
    refetch fresh data. Payload carries the broker_id so future versions can
    filter (Phase C invalidates all P&L sharing queries on any update).

    Never blocks the caller. Errors are logged + swallowed by the underlying
    `publish_admin_event`.
    """
    from app.services.admin_events import publish_admin_event
    await publish_admin_event(
        "pnl_sharing_update",
        {"broker_id": str(broker_id)},
    )
