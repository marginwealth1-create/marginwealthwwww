"""Bonus templates — reusable rules that define how a deposit bonus is
calculated and granted. Part of the Bonus Management feature (the whole
feature is gated by settings.BONUSES_ENABLED; inert when False).
"""

from __future__ import annotations

from datetime import datetime

from beanie import PydanticObjectId
from bson import Decimal128
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin
from app.models._types import Money


def _zero() -> Decimal128:
    return Decimal128("0")


class BonusType(StrEnum):
    FIRST_DEPOSIT = "FIRST_DEPOSIT"
    REGULAR_DEPOSIT = "REGULAR_DEPOSIT"
    RELOAD = "RELOAD"
    SPECIAL = "SPECIAL"


class BonusCalcMode(StrEnum):
    PERCENTAGE = "PERCENTAGE"
    FIXED = "FIXED"


class TemplateStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class BonusTemplate(TimestampMixin):
    name: str
    type: BonusType
    bonus_type: BonusCalcMode
    bonus_value: Money  # PERCENTAGE → %; FIXED → flat ₹
    min_deposit: Money = Field(default_factory=_zero)
    max_bonus: Money | None = None
    wager_requirement_multiple: int = 0  # 0 = no wager requirement
    duration_days: int = 30
    usage_limit: int | None = None  # None = unlimited
    used_count: int = 0
    end_date: datetime | None = None  # campaign hard end (optional)
    status: TemplateStatus = TemplateStatus.ACTIVE
    description: str = ""
    admin_id: PydanticObjectId | None = None  # None = super-admin global
    created_by: PydanticObjectId | None = None

    class Settings:
        name = "bonus_templates"
        indexes = [
            IndexModel([("status", ASCENDING), ("type", ASCENDING)]),
            IndexModel([("admin_id", ASCENDING), ("status", ASCENDING)]),
        ]
