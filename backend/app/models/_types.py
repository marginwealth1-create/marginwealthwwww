"""Pydantic-compatible wrappers for BSON types.

`Money` is an annotated `Decimal128` that Pydantic v2 can validate and
serialize. Use it everywhere a money field appears — never bare `Decimal128`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from bson import Decimal128
from pydantic import GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema


class _Decimal128Annotation:
    """Tells Pydantic how to coerce strings/ints/floats/Decimal/Decimal128 → Decimal128."""

    @classmethod
    def __get_pydantic_core_schema__(
        cls, _source_type: Any, _handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        def validate(value: Any) -> Decimal128:
            if isinstance(value, Decimal128):
                return value
            if isinstance(value, Decimal):
                return Decimal128(value)
            if isinstance(value, (int, str)):
                return Decimal128(str(value))
            if isinstance(value, float):
                # Stringify first to avoid binary-float drift
                return Decimal128(str(value))
            if value is None:
                return Decimal128("0")
            raise TypeError(f"Cannot coerce {type(value).__name__} to Decimal128")

        def serialize(value: Decimal128) -> str:
            try:
                return str(value.to_decimal())
            except Exception:
                return str(value)

        return core_schema.no_info_plain_validator_function(
            validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                serialize, return_schema=core_schema.str_schema(), when_used="json"
            ),
        )


Money = Annotated[Decimal128, _Decimal128Annotation]
"""Use `Money` for any Decimal128-backed monetary field on a Beanie document
or Pydantic model. Pydantic will validate and JSON-serialize it correctly."""
