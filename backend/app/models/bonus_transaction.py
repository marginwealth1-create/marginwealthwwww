"""Immutable, append-only ledger of every change to a bonus's credit. The
signed sum of a bonus's rows == its current_credit; the sum across a user's
ACTIVE bonuses == Wallet.credit. Part of Bonus Management (gated by
settings.BONUSES_ENABLED).
"""

from __future__ import annotations

from beanie import PydanticObjectId
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin
from app.models._types import Money


class BonusAction(StrEnum):
    GRANTED = "GRANTED"  # +original_amount when the bonus is granted
    LOSS_ABSORBED = "LOSS_ABSORBED"  # −loss when credit absorbs a trade loss
    COMPLETED_CONVERTED = "COMPLETED_CONVERTED"  # −credit, paired +WalletTransaction BONUS_CONVERTED
    CANCELLED_CLAWED = "CANCELLED_CLAWED"  # −remaining on admin cancel
    EXPIRED_CLAWED = "EXPIRED_CLAWED"  # −remaining on expiry (wager unmet)
    STOPOUT_FORFEITED = "STOPOUT_FORFEITED"  # −remaining when a stop-out wipes the account


class BonusTransaction(TimestampMixin):
    user_id: PydanticObjectId
    bonus_id: PydanticObjectId
    action: BonusAction
    credit_delta: Money  # signed change to the bonus's credit
    related_position_id: PydanticObjectId | None = None
    related_trade_id: PydanticObjectId | None = None
    related_wallet_tx_id: PydanticObjectId | None = None
    metadata: dict = Field(default_factory=dict)

    class Settings:
        name = "bonus_transactions"
        indexes = [
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("bonus_id", ASCENDING), ("created_at", DESCENDING)]),
        ]
