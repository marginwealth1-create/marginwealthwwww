"""Brokerage calculator — the *only* charge users pay on this platform.

Sourced from one of two places (in order of priority):
  1. ``netting_override`` — the admin's per-segment `commission_type` +
     `commission_value` resolved through Segment Settings. Always wins
     when supplied.
  2. The default ``BrokeragePlan`` row for the segment.

No statutory pass-through is computed (no STT / exchange / SEBI / stamp /
DP / GST). Admin policy: users see brokerage only.

Returned figures are Decimal — caller converts to Decimal128 at the
storage boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models._base import CommissionType, OrderAction, ProductType
from app.models.brokerage_plan import BrokeragePlan, PlanDetail
from app.utils.decimal_utils import (
    ZERO,
    percent_of,
    quantize_money,
    to_decimal,
)


@dataclass
class ChargesBreakdown:
    brokerage: Decimal
    total: Decimal  # = brokerage; kept as a separate field so callers that
    # already write `total_charges` don't need to change.

    def to_dict(self) -> dict[str, str]:
        return {
            "brokerage": str(self.brokerage),
            "total": str(self.total),
        }


async def get_active_plan() -> BrokeragePlan | None:
    return await BrokeragePlan.find_one(
        BrokeragePlan.is_default == True,  # noqa: E712
        BrokeragePlan.is_active == True,  # noqa: E712
    )


def _detail_for(plan: BrokeragePlan, segment_type: str) -> PlanDetail | None:
    return next((d for d in plan.details if d.segment_type == segment_type), None)


def _brokerage(detail: PlanDetail, *, qty: float, price: Decimal, lot_size: int) -> Decimal:
    qty_dec = to_decimal(qty)
    turnover = qty_dec * price
    if detail.brokerage_type == CommissionType.FLAT:
        b = to_decimal(detail.value)
    elif detail.brokerage_type == CommissionType.PERCENTAGE:
        b = percent_of(turnover, detail.value)
    elif detail.brokerage_type == CommissionType.PER_CRORE:
        b = quantize_money(turnover * to_decimal(detail.value) / Decimal("10000000"))
    else:  # PER_LOT
        lots = max(Decimal("0.01"), qty_dec / Decimal(max(1, lot_size)))
        b = to_decimal(detail.value) * lots
    if detail.min_brokerage and b < to_decimal(detail.min_brokerage):
        b = to_decimal(detail.min_brokerage)
    if detail.max_brokerage and to_decimal(detail.max_brokerage) > ZERO and b > to_decimal(detail.max_brokerage):
        b = to_decimal(detail.max_brokerage)
    return quantize_money(b)


def _brokerage_from_netting(
    netting: dict,
    *,
    qty: float,
    price: Decimal,
    lot_size: int,
) -> Decimal:
    """Compute brokerage using the netting `commission_type` + `commission_value`
    as the source of truth (admin's segment-settings override)."""
    qty_dec = to_decimal(qty)
    turnover = qty_dec * price
    ctype = (netting.get("commission_type") or "PER_LOT").upper()
    value = to_decimal(netting.get("commission_value") or 0)

    if ctype == "FLAT":
        b = value
    elif ctype == "PERCENTAGE":
        b = percent_of(turnover, float(value))
    elif ctype == "PER_CRORE":
        b = quantize_money(turnover * value / Decimal("10000000"))
    else:  # PER_LOT
        lots = max(Decimal("0.01"), qty_dec / Decimal(max(1, lot_size)))
        b = value * lots
    min_b = to_decimal(netting.get("min_brokerage") or 0)
    if min_b and b < min_b:
        b = min_b
    return quantize_money(b)


async def calculate(
    *,
    segment_type: str,
    action: OrderAction,
    product_type: ProductType,
    qty: float,
    price: Decimal,
    lot_size: int = 1,
    plan: BrokeragePlan | None = None,
    netting_override: dict | None = None,
    is_closing: bool = False,
    charge_on: str | None = None,
) -> ChargesBreakdown:
    """Compute the trade's brokerage. No statutory components.

    `netting_override` (admin's Segment Settings `commission_type` +
    `commission_value`) wins when supplied, otherwise we fall back to the
    default BrokeragePlan row for the segment. Returns zero brokerage when
    neither is configured (caller should treat that as an admin-config gap,
    not a free trade).

    `is_closing` + `charge_on` gate which legs are charged:
        charge_on = "open"  → only opening trades incur brokerage; closing
                              trades return 0.
        charge_on = "close" → only closing trades incur; opening returns 0.
        charge_on = "both" (default) → every fill is charged.

    `action` and `product_type` are accepted for caller-side compatibility
    (older signature drove STT / stamp / DP toggles off them) but no
    longer influence the result.
    """
    _ = (action, product_type)  # explicit-ignore for tooling

    # charge_on gating runs before any plan lookup. Skip the work entirely
    # for legs the admin doesn't charge — important when a user opens a
    # close-only segment and we'd otherwise still pay for the plan fetch.
    co = (charge_on or "both").lower()
    if co == "open" and is_closing:
        return ChargesBreakdown(ZERO, ZERO)
    if co == "close" and not is_closing:
        return ChargesBreakdown(ZERO, ZERO)

    plan = plan or await get_active_plan()
    if plan is None and netting_override is None:
        return ChargesBreakdown(ZERO, ZERO)

    detail = _detail_for(plan, segment_type) if plan else None
    if detail is None and plan is not None:
        detail = plan.details[0] if plan.details else None

    price = to_decimal(price)

    if netting_override is not None:
        brokerage = _brokerage_from_netting(
            netting_override, qty=qty, price=price, lot_size=lot_size
        )
    elif detail is not None:
        brokerage = _brokerage(detail, qty=qty, price=price, lot_size=lot_size)
    else:
        brokerage = ZERO

    return ChargesBreakdown(brokerage, brokerage)
