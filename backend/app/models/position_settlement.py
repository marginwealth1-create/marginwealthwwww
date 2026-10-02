"""Weekly mark-to-market settlement records.

Every Saturday the weekly-settlement engine walks each OPEN ``Position``,
realises its running P&L into the wallet ledger, closes the old position
and re-opens an identical fresh one at the settlement price. Two documents
capture that run for full traceability + idempotency:

  • ``SettlementBatch``      — one row per weekly run (keyed by ``week_key``).
  • ``PositionSettlement``   — one row per settled position inside a batch.

These are an append-only audit trail. They are NOT the same as
``transaction.SettlementRequest`` (that is the manual wallet-debt approval
flow); the names are deliberately distinct to avoid any collision.
"""

from __future__ import annotations

from datetime import datetime

from beanie import Indexed, PydanticObjectId
from bson import Decimal128
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin
from app.models._types import Money


def _zero() -> Decimal128:
    return Decimal128("0")


class SettlementBatchStatus(StrEnum):
    RUNNING = "RUNNING"   # batch created, positions being processed
    DONE = "DONE"         # all positions processed (some may be SKIPPED/FAILED)
    FAILED = "FAILED"     # batch aborted before completion


class PositionSettlementStatus(StrEnum):
    PENDING = "PENDING"              # record written, wallet/position not yet swapped
    DONE = "DONE"                    # P&L booked + position re-opened
    SKIPPED_NO_PRICE = "SKIPPED_NO_PRICE"  # no valid LTP — left untouched, retry next run
    FAILED = "FAILED"               # error mid-settle — left for resume / manual review


class SettlementBatch(TimestampMixin):
    """One weekly settlement run.

    A run can be GLOBAL (the automatic Saturday job — ``scope_admin_id`` is
    None, settles every open position) or SCOPED to a single admin's user
    pool (a manual "Run now" by a SUPER_ADMIN / ADMIN — settles only that
    actor's users). To allow both in the same week, the unique gate is
    ``run_key`` (= ``week_key`` for the global run, ``"{week_key}:{admin}"``
    for a scoped run), NOT ``week_key`` alone. Double-settling a single
    position across overlapping runs is prevented at the PositionSettlement
    level via the unique ``(week_key, old_position_id)`` index.
    """

    run_key: Indexed(str, unique=True)  # type: ignore[valid-type]  # week_key or "week_key:admin_id"
    week_key: str  # e.g. "2026-W24" (indexed, NOT unique)
    scope_admin_id: PydanticObjectId | None = None  # None = global (all users)

    status: SettlementBatchStatus = SettlementBatchStatus.RUNNING

    started_at: datetime
    finished_at: datetime | None = None

    fx_rate_snapshot: Money = Field(default_factory=_zero)  # USD/INR frozen for the batch

    total: int = 0      # open positions seen
    settled: int = 0    # status=DONE
    skipped: int = 0    # status=SKIPPED_NO_PRICE
    failed: int = 0     # status=FAILED

    error: str | None = None

    class Settings:
        name = "settlement_batches"
        indexes = [
            IndexModel([("run_key", ASCENDING)], unique=True),
            IndexModel([("week_key", ASCENDING)]),
            IndexModel([("status", ASCENDING)]),
            IndexModel([("started_at", DESCENDING)]),
        ]


class PositionSettlement(TimestampMixin):
    """One settled position inside a weekly batch.

    Written FIRST (status=PENDING) before any wallet/position mutation so
    it doubles as the idempotency anchor: the unique
    ``(week_key, old_position_id)`` index makes ANY run (global Saturday
    job or a scoped manual run) settle a given position at most once per
    ISO week, so overlapping admin/global runs can never double-book P&L.
    """

    batch_id: PydanticObjectId
    week_key: str
    scope_admin_id: PydanticObjectId | None = None  # which run settled it (None = global)

    user_id: PydanticObjectId
    old_position_id: PydanticObjectId
    new_position_id: PydanticObjectId | None = None

    symbol: str
    instrument_token: str
    side: str          # "BUY" / "SELL" (opened_side of the position)
    quantity: float    # absolute lots-equivalent qty carried into the new position

    previous_avg_price: Money = Field(default_factory=_zero)
    settlement_price: Money = Field(default_factory=_zero)
    realized_pnl: Money = Field(default_factory=_zero)  # signed INR booked to wallet
    fx_rate: Money = Field(default_factory=_zero)        # USD/INR used (1 for INR segments)

    status: PositionSettlementStatus = PositionSettlementStatus.PENDING
    error: str | None = None
    settled_at: datetime | None = None

    is_demo: bool = False

    class Settings:
        name = "position_settlements"
        indexes = [
            IndexModel(
                [("week_key", ASCENDING), ("old_position_id", ASCENDING)],
                unique=True,
            ),
            IndexModel([("batch_id", ASCENDING)]),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("status", ASCENDING)]),
        ]
