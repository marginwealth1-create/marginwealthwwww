"""Admin-broker P&L sharing agreements and per-period settlements.

Admin partners with broker on a percentage of broker's net client P&L
(bi-directional: gains share + losses share). The agreement defines the
percentage, settlement mode (AUTO/MANUAL), and cadence. Each settlement
row is a frozen snapshot of one period's computation.
"""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed, PydanticObjectId
from bson import Decimal128
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin


class AgreementStatus(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ENDED = "ENDED"


class SettlementMode(StrEnum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class SettlementCadence(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"


class SharingSettlementStatus(StrEnum):
    PENDING = "PENDING"
    SETTLED = "SETTLED"
    FAILED = "FAILED"


class AgreementType(StrEnum):
    PNL_AND_BROKERAGE = "PNL_AND_BROKERAGE"  # shares both PNL and brokerage (default)
    BROKERAGE_ONLY = "BROKERAGE_ONLY"        # shares brokerage only; sharing_pnl forced to 0


class PnlSharingAgreement(TimestampMixin):
    admin_id: Indexed(PydanticObjectId)   # type: ignore[valid-type]
    broker_id: Indexed(PydanticObjectId)  # type: ignore[valid-type]
    share_pct: Decimal128                 # 0..100 inclusive
    settlement_mode: SettlementMode
    settlement_cadence: SettlementCadence | None = None  # null iff mode=MANUAL
    agreement_type: AgreementType = AgreementType.PNL_AND_BROKERAGE
    status: AgreementStatus = AgreementStatus.ACTIVE
    effective_from: datetime              # IST midnight, stored UTC
    effective_until: datetime | None = None
    created_by: PydanticObjectId
    last_modified_by: PydanticObjectId

    class Settings:
        name = "pnl_sharing_agreements"
        use_state_management = True
        indexes = [
            IndexModel(
                [("admin_id", ASCENDING), ("broker_id", ASCENDING), ("agreement_type", ASCENDING)],
                unique=True,
                partialFilterExpression={"status": {"$in": ["ACTIVE", "PAUSED"]}},
                name="uniq_active_admin_broker_type",
            ),
            IndexModel([("broker_id", ASCENDING)]),
            IndexModel(
                [("status", ASCENDING), ("settlement_mode", ASCENDING),
                 ("settlement_cadence", ASCENDING)],
                name="scheduler_lookup",
            ),
        ]


class PnlSharingSettlement(TimestampMixin):
    agreement_id: Indexed(PydanticObjectId)  # type: ignore[valid-type]
    admin_id: Indexed(PydanticObjectId)      # type: ignore[valid-type]
    broker_id: Indexed(PydanticObjectId)     # type: ignore[valid-type]
    period_start: datetime
    period_end: datetime
    cadence: SettlementCadence

    # Snapshot
    net_client_pnl_inr: Decimal128 = Decimal128("0")
    net_client_bkg_inr: Decimal128 = Decimal128("0")
    total_of_both_inr: Decimal128 = Decimal128("0")
    actual_pnl_inr: Decimal128 = Decimal128("0")

    # Sharing (locked at settlement)
    share_pct_snapshot: Decimal128 = Decimal128("0")
    sharing_pnl_inr: Decimal128 = Decimal128("0")
    sharing_bkg_inr: Decimal128 = Decimal128("0")
    sharing_total_inr: Decimal128 = Decimal128("0")

    status: SharingSettlementStatus = SharingSettlementStatus.PENDING
    settled_at: datetime | None = None
    settled_by: PydanticObjectId | None = None
    transaction_ref_admin: PydanticObjectId | None = None
    transaction_ref_broker: PydanticObjectId | None = None
    failure_reason: str | None = None
    retry_count: int = 0

    class Settings:
        name = "pnl_sharing_settlements"
        use_state_management = True
        indexes = [
            IndexModel(
                [("agreement_id", ASCENDING), ("period_start", ASCENDING)],
                unique=True,
                name="uniq_agreement_period",
            ),
            IndexModel([("admin_id", ASCENDING), ("period_start", DESCENDING)]),
            IndexModel([("broker_id", ASCENDING), ("period_start", DESCENDING)]),
            IndexModel([("status", ASCENDING), ("period_end", ASCENDING)]),
        ]
