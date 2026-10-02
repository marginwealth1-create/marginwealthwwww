"""Bonus template CRUD + matching. The single source of truth for "which
template applies to this deposit" (used by both auto-grant and the user-side
preview). Scope: a template with admin_id=None is a super-admin global visible
to everyone; admin_id=<id> is that admin's own. Part of Bonus Management.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from beanie import PydanticObjectId

from app.models.bonus_template import (
    BonusCalcMode,
    BonusTemplate,
    BonusType,
    TemplateStatus,
)
from app.models.user import User, UserRole
from app.schemas.bonus import BonusTemplateCreate, BonusTemplatePatch
from app.utils.decimal_utils import quantize_money, to_decimal, to_decimal128
from app.utils.time_utils import now_utc

_MONEY_FIELDS = {"bonus_value", "min_deposit", "max_bonus"}


def _is_super(actor: User) -> bool:
    return actor.role == UserRole.SUPER_ADMIN


def _scope_filter(actor: User) -> dict[str, Any]:
    """Mongo filter limiting templates the actor may see/manage."""
    if _is_super(actor):
        return {}
    return {"$or": [{"admin_id": actor.id}, {"admin_id": None}]}


def _can_manage(actor: User, tpl: BonusTemplate) -> bool:
    return _is_super(actor) or tpl.admin_id == actor.id


async def list_templates(actor: User) -> list[BonusTemplate]:
    return await BonusTemplate.find(_scope_filter(actor)).sort("-created_at").to_list()


async def get_template(template_id: str, actor: User) -> BonusTemplate | None:
    try:
        tpl = await BonusTemplate.get(PydanticObjectId(template_id))
    except Exception:
        return None
    if tpl is None or not _can_manage(actor, tpl):
        return None
    return tpl


async def create_template(payload: BonusTemplateCreate, actor: User) -> BonusTemplate:
    tpl = BonusTemplate(
        name=payload.name.strip(),
        type=BonusType(payload.type),
        bonus_type=BonusCalcMode(payload.bonus_type),
        bonus_value=to_decimal128(max(0.0, payload.bonus_value)),
        min_deposit=to_decimal128(max(0.0, payload.min_deposit)),
        max_bonus=to_decimal128(payload.max_bonus) if payload.max_bonus is not None else None,
        wager_requirement_multiple=max(0, int(payload.wager_requirement_multiple)),
        duration_days=max(0, int(payload.duration_days)),
        usage_limit=payload.usage_limit,
        end_date=payload.end_date,
        status=TemplateStatus(payload.status),
        description=payload.description or "",
        admin_id=None if _is_super(actor) else actor.id,
        created_by=actor.id,
    )
    await tpl.insert()
    return tpl


async def update_template(
    template_id: str, patch: BonusTemplatePatch, actor: User
) -> BonusTemplate | None:
    tpl = await get_template(template_id, actor)
    if tpl is None:
        return None
    data = patch.model_dump(exclude_unset=True)
    for k, v in data.items():
        if k in _MONEY_FIELDS:
            setattr(tpl, k, to_decimal128(v) if v is not None else None)
        elif k == "type" and v is not None:
            tpl.type = BonusType(v)
        elif k == "bonus_type" and v is not None:
            tpl.bonus_type = BonusCalcMode(v)
        elif k == "status" and v is not None:
            tpl.status = TemplateStatus(v)
        else:
            setattr(tpl, k, v)
    await tpl.save()
    return tpl


async def delete_template(template_id: str, actor: User) -> bool:
    tpl = await get_template(template_id, actor)
    if tpl is None:
        return False
    await tpl.delete()
    return True


def compute_bonus_amount(template: BonusTemplate, deposit_amount: Any) -> Decimal:
    """Pure calculator: PERCENTAGE → deposit×value/100, FIXED → flat value;
    capped at max_bonus when set (> 0). Returns a quantized Decimal."""
    dep = to_decimal(deposit_amount)
    val = to_decimal(template.bonus_value)
    if template.bonus_type == BonusCalcMode.PERCENTAGE:
        amt = dep * val / Decimal("100")
    else:
        amt = val
    if template.max_bonus is not None:
        cap = to_decimal(template.max_bonus)
        if cap > 0 and amt > cap:
            amt = cap
    return quantize_money(amt)


async def find_matching_template(
    deposit_amount: Any, is_first_deposit: bool, admin_id: PydanticObjectId | None
) -> BonusTemplate | None:
    """Pick the template a deposit qualifies for. Priority: FIRST_DEPOSIT (only
    on the user's first deposit) then REGULAR_DEPOSIT; within a type the newest
    ACTIVE, in-scope, min-met, non-expired, non-exhausted template wins."""
    amt = to_decimal(deposit_amount)
    now = now_utc()
    types = (
        [BonusType.FIRST_DEPOSIT, BonusType.REGULAR_DEPOSIT]
        if is_first_deposit
        else [BonusType.REGULAR_DEPOSIT]
    )
    for t in types:
        rows = (
            await BonusTemplate.find(
                BonusTemplate.type == t,
                BonusTemplate.status == TemplateStatus.ACTIVE,
            )
            .sort("-created_at")
            .to_list()
        )
        for tpl in rows:
            if tpl.admin_id not in (None, admin_id):
                continue
            if to_decimal(tpl.min_deposit) > amt:
                continue
            if tpl.end_date is not None and tpl.end_date < now:
                continue
            if tpl.usage_limit is not None and tpl.used_count >= tpl.usage_limit:
                continue
            return tpl
    return None
