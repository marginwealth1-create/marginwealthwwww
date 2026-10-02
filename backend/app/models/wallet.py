"""Wallet — single document per user holding all balance figures.

Money is stored as Decimal128. Updates must occur inside MongoDB transactions
(see services/wallet_service.py).
"""

from __future__ import annotations

from beanie import Indexed, PydanticObjectId
from bson import Decimal128
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models._base import TimestampMixin
from app.models._types import Money


def _zero() -> Decimal128:
    return Decimal128("0")


class Wallet(TimestampMixin):
    user_id: Indexed(PydanticObjectId, unique=True)  # type: ignore[valid-type]

    available_balance: Money = Field(default_factory=_zero)
    used_margin: Money = Field(default_factory=_zero)
    realized_pnl: Money = Field(default_factory=_zero)
    unrealized_pnl: Money = Field(default_factory=_zero)
    credit_limit: Money = Field(default_factory=_zero)

    # Bonus credit pool (Bonus Management, gated by settings.BONUSES_ENABLED).
    # Phantom credit granted by a bonus. Counts toward the stop-out / free-
    # margin base like credit_limit, but ALSO absorbs realized losses — AFTER
    # real available_balance is exhausted (deposit-first), BEFORE
    # settlement_outstanding. `credit` holds the FREE (unlocked) bonus:
    # opening a trade locks bonus BEFORE cash (operator policy), moving the
    # locked amount into `bonus_locked` and back on close. Total bonus value
    # granted = credit + bonus_locked = Σ active UserBonus.current_credit.
    # Zero when bonuses are off.
    credit: Money = Field(default_factory=_zero)

    # Bonus currently tied up in open-position margin (Bonus Management).
    # Restored to `credit` when that margin is released on close.
    bonus_locked: Money = Field(default_factory=_zero)

    # Unrecovered settlement loss — when a stop-out force-close booked a
    # realized loss that exceeded available_balance + credit_limit, the
    # uncoverable shortfall sits here. Recovered automatically against the
    # next DEPOSIT (deducted before crediting available_balance). Read-only
    # for the user; modified only by wallet_service.force_debit and the
    # DEPOSIT recovery branch in wallet_service.adjust.
    settlement_outstanding: Money = Field(default_factory=_zero)

    total_deposits: Money = Field(default_factory=_zero)
    total_withdrawals: Money = Field(default_factory=_zero)
    total_brokerage: Money = Field(default_factory=_zero)
    total_charges: Money = Field(default_factory=_zero)

    # Optimistic-locking version. Increment on each financial mutation.
    version: int = 0

    class Settings:
        name = "wallets"
        indexes = [IndexModel([("user_id", ASCENDING)], unique=True)]
