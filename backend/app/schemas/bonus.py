"""Request/response schemas for Bonus Management. Money is accepted as a
number on input and always serialized as a string on output (Decimal128
end-to-end). Feature gated by settings.BONUSES_ENABLED.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ── Templates ────────────────────────────────────────────────────────
class BonusTemplateCreate(BaseModel):
    name: str
    type: str  # BonusType: FIRST_DEPOSIT | REGULAR_DEPOSIT | RELOAD | SPECIAL
    bonus_type: str  # BonusCalcMode: PERCENTAGE | FIXED
    bonus_value: float = Field(ge=0)
    min_deposit: float = 0
    max_bonus: float | None = None
    wager_requirement_multiple: int = 0
    duration_days: int = 30
    usage_limit: int | None = None
    end_date: datetime | None = None
    status: str = "ACTIVE"
    description: str = ""


class BonusTemplatePatch(BaseModel):
    name: str | None = None
    type: str | None = None
    bonus_type: str | None = None
    bonus_value: float | None = None
    min_deposit: float | None = None
    max_bonus: float | None = None
    wager_requirement_multiple: int | None = None
    duration_days: int | None = None
    usage_limit: int | None = None
    end_date: datetime | None = None
    status: str | None = None
    description: str | None = None


# ── Grants ───────────────────────────────────────────────────────────
class BonusGrantRequest(BaseModel):
    user_id: str
    template_id: str | None = None  # template path (with deposit_amount)
    deposit_amount: float | None = None
    amount: float | None = None  # custom flat grant path
    notes: str = ""


class BonusCancelRequest(BaseModel):
    reason: str


class BonusDeductRequest(BaseModel):
    user_id: str
    amount: float
    reason: str = ""
