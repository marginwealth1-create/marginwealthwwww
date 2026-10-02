"""Weekly P&L-share settlement records for brokers.

Same shape as AdminSettlement (admin → sub-admin) but keyed by `broker_id`
so the broker-tier reconciliation surface stays separate. Each row covers
only a broker's *direct* clients (`broker_ancestry[-1] == broker.id`) —
sub-brokers under that broker get their own settlement rows. This keeps
the per-broker P&L share unambiguous and non-overlapping.
"""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed, PydanticObjectId
from bson import Decimal128
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin


class BrokerSettlementStatus(StrEnum):
    PENDING = "PENDING"
    FINALIZED = "FINALIZED"
    PAID = "PAID"


class BrokerSettlement(TimestampMixin):
    broker_id: Indexed(PydanticObjectId)  # type: ignore[valid-type]
    period_start: datetime
    period_end: datetime

    user_count: int = 0
    gross_user_loss_inr: Decimal128 = Decimal128("0")
    gross_user_profit_inr: Decimal128 = Decimal128("0")
    total_brokerage_inr: Decimal128 = Decimal128("0")
    net_house_pnl_inr: Decimal128 = Decimal128("0")

    pnl_share_pct_snapshot: Decimal128 = Decimal128("0")
    broker_share_inr: Decimal128 = Decimal128("0")

    status: BrokerSettlementStatus = BrokerSettlementStatus.PENDING
    finalized_at: datetime | None = None
    finalized_by: PydanticObjectId | None = None
    paid_at: datetime | None = None
    paid_by: PydanticObjectId | None = None
    notes: str | None = None

    class Settings:
        name = "broker_settlements"
        use_state_management = True
        indexes = [
            IndexModel(
                [("broker_id", ASCENDING), ("period_start", DESCENDING)],
                unique=True,
            ),
            IndexModel([("period_start", DESCENDING)]),
            IndexModel([("status", ASCENDING)]),
        ]

    def is_frozen(self) -> bool:
        return self.status in {
            BrokerSettlementStatus.FINALIZED,
            BrokerSettlementStatus.PAID,
        }
