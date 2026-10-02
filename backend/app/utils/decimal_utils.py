"""Decimal128 helpers — never use float for money.

MongoDB stores money as bson.Decimal128. Inside Python we work with
decimal.Decimal (exact arithmetic) and convert at the storage boundary.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from typing import Any

from bson import Decimal128

# 28 digits of precision is the Python default — plenty for INR amounts.
getcontext().prec = 28

ZERO: Decimal = Decimal("0")
ONE: Decimal = Decimal("1")
HUNDRED: Decimal = Decimal("100")
PAISE: Decimal = Decimal("0.01")  # 2dp


def to_decimal(value: Any) -> Decimal:
    """Coerce anything money-shaped into a Decimal. Floats are stringified first
    to avoid binary representation drift."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, (int, str)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if value is None:
        return ZERO
    raise TypeError(f"Cannot convert {type(value).__name__} to Decimal")


def to_decimal128(value: Any) -> Decimal128:
    return Decimal128(quantize_money(to_decimal(value)))


def quantize_money(value: Decimal) -> Decimal:
    """Round to 2 decimal places using banker's rounding."""
    return value.quantize(PAISE, rounding=ROUND_HALF_EVEN)


def quantize_price(value: Decimal, *, tick_size: Decimal | None = None) -> Decimal:
    """Round price to tick size if given, else 2dp."""
    if tick_size is None or tick_size <= ZERO:
        return value.quantize(PAISE, rounding=ROUND_HALF_EVEN)
    steps = (value / tick_size).quantize(ONE, rounding=ROUND_HALF_EVEN)
    return (steps * tick_size).quantize(tick_size, rounding=ROUND_HALF_EVEN)


def percent_of(value: Decimal | Any, percent: Decimal | Any) -> Decimal:
    return quantize_money(to_decimal(value) * to_decimal(percent) / HUNDRED)


def add(*values: Any) -> Decimal:
    return quantize_money(sum((to_decimal(v) for v in values), ZERO))


def sub(a: Any, b: Any) -> Decimal:
    return quantize_money(to_decimal(a) - to_decimal(b))


def is_positive(value: Any) -> bool:
    return to_decimal(value) > ZERO


def format_inr(value: Any, *, with_symbol: bool = True) -> str:
    """Format Decimal as Indian numbering (₹ 1,23,456.78)."""
    d = quantize_money(to_decimal(value))
    sign = "-" if d < ZERO else ""
    abs_str = f"{abs(d):.2f}"
    int_part, frac_part = abs_str.split(".")
    if len(int_part) <= 3:
        formatted_int = int_part
    else:
        last_three = int_part[-3:]
        rest = int_part[:-3]
        chunks = []
        while len(rest) > 2:
            chunks.append(rest[-2:])
            rest = rest[:-2]
        if rest:
            chunks.append(rest)
        formatted_int = ",".join(reversed(chunks)) + "," + last_three
    out = f"{sign}{formatted_int}.{frac_part}"
    return f"₹ {out}" if with_symbol else out
