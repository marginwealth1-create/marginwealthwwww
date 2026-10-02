"""Weekly P&L-share settlement records for sub-admins.

A row per (sub_admin_id, week-start). Computed on demand the first time a
super-admin views a given week and cached here so finalize / mark-paid have
a stable target. The B-book formula is:

    net_house_pnl = gross_user_loss - gross_user_profit + total_brokerage
    sub_admin_share = (pnl_share_pct / 100) * net_house_pnl
"""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed, PydanticObjectId
from bson import Decimal128
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin


class AdminSettlementStatus(StrEnum):
    PENDING = "PENDING"       # computed, not yet locked
    FINALIZED = "FINALIZED"   # super-admin reviewed and locked
    PAID = "PAID"             # marked paid offline


class AdminSettlement(TimestampMixin):
    sub_admin_id: Indexed(PydanticObjectId)  # type: ignore[valid-type]
    period_start: datetime  # Mon 00:00:00 IST (stored as UTC)
    period_end: datetime    # Sun 23:59:59.999 IST (stored as UTC)

    user_count: int = 0
    gross_user_loss_inr: Decimal128 = Decimal128("0")     # positive
    gross_user_profit_inr: Decimal128 = Decimal128("0")   # positive
    total_brokerage_inr: Decimal128 = Decimal128("0")
    net_house_pnl_inr: Decimal128 = Decimal128("0")       # loss - profit + brokerage

    pnl_share_pct_snapshot: Decimal128 = Decimal128("0")  # frozen at compute time
    sub_admin_share_inr: Decimal128 = Decimal128("0")     # pct * net_house

    status: AdminSettlementStatus = AdminSettlementStatus.PENDING
    finalized_at: datetime | None = None
    finalized_by: PydanticObjectId | None = None
    paid_at: datetime | None = None
    paid_by: PydanticObjectId | None = None
    notes: str | None = None

    class Settings:
        name = "admin_settlements"
        use_state_management = True
        indexes = [
            IndexModel(
                [("sub_admin_id", ASCENDING), ("period_start", DESCENDING)],
                unique=True,
            ),
            IndexModel([("period_start", DESCENDING)]),
            IndexModel([("status", ASCENDING)]),
        ]

    def is_frozen(self) -> bool:
        return self.status in {
            AdminSettlementStatus.FINALIZED,
            AdminSettlementStatus.PAID,
        }
