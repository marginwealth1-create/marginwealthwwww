"""Brokerage plans — per-segment platform brokerage rate.

A plan owns N PlanDetail entries (embedded). The brokerage calculator looks
up the active plan, finds the matching segment row, and applies it to every
fill.

This platform does **not** pass any statutory charge through to the user
(no STT, exchange, SEBI, stamp duty, DP, or GST). The only line item on a
trade is the platform's own brokerage — see `services.brokerage_calculator`.
"""

from __future__ import annotations

from beanie import Indexed
from bson import Decimal128
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel

from app.models._base import CommissionType, TimestampMixin


def _zero() -> Decimal128:
    return Decimal128("0")


class BrokerageType(str):
    PER_LOT = "PER_LOT"
    PER_CRORE = "PER_CRORE"
    PERCENTAGE = "PERCENTAGE"
    FLAT = "FLAT"


class PlanDetail(BaseModel):
    segment_type: str  # SegmentType.value

    # Platform brokerage — the *only* charge users pay on this platform.
    # Statutory components (STT / exchange / SEBI / stamp / DP / GST) are
    # intentionally absent: admin policy, never passed through to the user.
    brokerage_type: CommissionType = CommissionType.PER_LOT
    value: float = 20.0
    min_brokerage: float = 0.0
    max_brokerage: float = 0.0  # 0 = uncapped


class BrokeragePlan(TimestampMixin):
    plan_name: Indexed(str, unique=True)  # type: ignore[valid-type]
    description: str = ""
    is_active: bool = True
    is_default: bool = False
    details: list[PlanDetail] = Field(default_factory=list)

    class Settings:
        name = "brokerage_plans"
        indexes = [
            IndexModel([("plan_name", ASCENDING)], unique=True),
            IndexModel([("is_active", ASCENDING), ("is_default", ASCENDING)]),
        ]
