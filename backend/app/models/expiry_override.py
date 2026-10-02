"""Per-actor Expiry-Settings override.

Mirrors the segment-settings override pattern: one collection that stores
per-actor (USER / ADMIN / BROKER) overrides on top of the global
PlatformSetting "option_chain.underlyings" + "option_chain.max_expiries".

A None (vs empty []) field means "don't shadow this field — inherit from
the parent tier", so an admin can override only `max_expiries_fallback`
without re-stating the whole `underlyings` array.
"""

from __future__ import annotations

from typing import Any

from beanie import PydanticObjectId
from pymongo import ASCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin


class ExpiryOverrideActor(StrEnum):
    USER = "USER"
    BROKER = "BROKER"
    ADMIN = "ADMIN"


class ExpiryOverride(TimestampMixin):
    actor_kind: ExpiryOverrideActor
    actor_id: PydanticObjectId

    underlyings: list[dict[str, Any]] | None = None
    max_expiries_fallback: int | None = None
    # Per-exchange expiry fallback {"NSE": int, "BSE": int, "MCX": int}.
    # None = inherit from the parent tier. Wins over max_expiries_fallback
    # for the matching exchange; per-underlying "Show expiry month" still
    # wins over this.
    max_expiries_by_exchange: dict[str, int] | None = None

    # Per-option-segment "strikes around ATM" count, keyed by the admin
    # option-segment row name: {"NSE_IDX_OPT": int, "NSE_STK_OPT": int,
    # "MCX_OPT": int, "BSE_OPT": int}. The chain renders 2N+1 strikes centred
    # on the live ATM. None = inherit from the parent tier; a missing segment
    # key inside the dict also inherits. Falls back to the flat platform-wide
    # option_chain.strikes_around_atm when no tier sets it.
    strikes_around_atm_by_segment: dict[str, int] | None = None

    class Settings:
        name = "expiry_overrides"
        indexes = [
            IndexModel(
                [("actor_kind", ASCENDING), ("actor_id", ASCENDING)],
                unique=True,
            ),
        ]
