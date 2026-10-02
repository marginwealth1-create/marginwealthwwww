"""MongoDB connection lifecycle (Motor + Beanie).

`init_database()` is called from FastAPI's lifespan handler. It opens the
Motor client, registers every Beanie Document model, and ensures indexes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError

from app.core.config import settings

if TYPE_CHECKING:
    from beanie import Document

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def get_client() -> AsyncIOMotorClient:
    if _client is None:
        raise RuntimeError("MongoDB client not initialized — call init_database() first")
    return _client


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("MongoDB not initialized — call init_database() first")
    return _db


def _document_models() -> list[type["Document"]]:
    # Imported lazily so this module can be imported before models are written.
    from app.models.admin_settlement import AdminSettlement
    from app.models.broker_settlement import BrokerSettlement
    from app.models.pnl_sharing import PnlSharingAgreement, PnlSharingSettlement
    from app.models.alert import PriceAlert
    from app.models.audit_log import AuditLog
    from app.models.bank_account import CompanyBankAccount, UserBankAccount
    from app.models.bonus_template import BonusTemplate
    from app.models.bonus_transaction import BonusTransaction
    from app.models.brokerage_plan import BrokeragePlan
    from app.models.crypto_config import AdminCryptoConfig
    from app.models.chart_level import ChartLevel
    from app.models.user_bonus import UserBonus
    from app.models.holding import Holding
    from app.models.holiday import TradingHoliday
    from app.models.instrument import Instrument
    from app.models.tick_snapshot import TickSnapshot
    from app.models.kyc import KycSubmission
    from app.models.notification import AdminNotification, Notification
    from app.models.order import Order
    from app.models.platform_setting import PlatformSetting
    from app.models.expiry_override import ExpiryOverride
    from app.models.position import Position, UserPositionTracker
    from app.models.position_settlement import (
        PositionSettlement,
        SettlementBatch,
    )
    from app.models.netting import (
        BrokerRiskSettings,
        BrokerSegmentOverride,
        NettingScriptOverride,
        NettingSegment,
        RiskSettings,
        SubAdminRiskSettings,
        SubAdminSegmentOverride,
        SuperAdminRiskSettings,
        SuperAdminSegmentOverride,
        UserRiskSettings,
        UserSegmentOverride,
    )
    from app.models.trade import Trade
    from app.models.transaction import (
        BrokerWdRule,
        DepositRequest,
        SettlementRequest,
        SubAdminWdRule,
        SuperAdminWdRule,
        WalletTransaction,
        WdRule,
        WithdrawalRequest,
    )
    from app.models.user import User, UserSegment
    from app.models.wallet import Wallet
    from app.models.push_subscription import PushSubscription
    from app.models.watchlist import Watchlist, WatchlistItem
    from app.models.metaapi_settings import MetaApiSettings
    from app.models.zerodha_auto_login import ZerodhaAutoLogin
    from app.models.zerodha_feed_routing import ZerodhaFeedRouting
    from app.models.zerodha_settings import ZerodhaSettings

    return [
        # Users / segments
        User,
        UserSegment,
        # Risk + Netting
        RiskSettings,
        UserRiskSettings,
        SubAdminRiskSettings,
        SuperAdminRiskSettings,
        BrokerRiskSettings,
        NettingSegment,
        NettingScriptOverride,
        SubAdminSegmentOverride,
        SuperAdminSegmentOverride,
        BrokerSegmentOverride,
        UserSegmentOverride,
        # Market
        Instrument,
        TickSnapshot,
        # Wallet / money
        Wallet,
        WalletTransaction,
        DepositRequest,
        WithdrawalRequest,
        SettlementRequest,
        WdRule,
        SuperAdminWdRule,
        SubAdminWdRule,
        BrokerWdRule,
        CompanyBankAccount,
        UserBankAccount,
        AdminCryptoConfig,
        ChartLevel,
        BrokeragePlan,
        # Bonus Management (gated by BONUSES_ENABLED; models always registered)
        BonusTemplate,
        UserBonus,
        BonusTransaction,
        # Trading
        Order,
        Trade,
        Position,
        Holding,
        UserPositionTracker,
        SettlementBatch,
        PositionSettlement,
        Watchlist,
        PushSubscription,
        WatchlistItem,
        # Ops
        AuditLog,
        KycSubmission,
        Notification,
        AdminNotification,
        PriceAlert,
        PlatformSetting,
        ExpiryOverride,
        TradingHoliday,
        AdminSettlement,
        BrokerSettlement,
        PnlSharingAgreement,
        PnlSharingSettlement,
        # Integrations
        ZerodhaSettings,
        ZerodhaAutoLogin,
        MetaApiSettings,
        ZerodhaFeedRouting,
    ]


# Bump this whenever a NEW one-time destructive schema heal is added below,
# so the barrier re-runs once for the new migration instead of being
# permanently short-circuited by a prior run's `done` marker.
_SCHEMA_HEAL_LOCK_ID = "settlement_index_heal_v1"
# A leader that claimed the lock but never set `done` (crashed / SIGKILL
# mid-heal) is considered stale after this long and another worker reclaims.
_SCHEMA_HEAL_STALE_AFTER = timedelta(seconds=120)
# How long a follower waits for the leader to finish before giving up and
# proceeding anyway (so a wedged leader can't deadlock the whole fleet).
_SCHEMA_HEAL_WAIT_TIMEOUT_SEC = 60


async def _do_index_heal(db: AsyncIOMotorDatabase) -> None:
    """The actual destructive legacy-index migration. MUST run in exactly one
    worker (see `_run_schema_heal_once`) because its `drop()` / `drop_index()`
    calls abort any index build a sibling worker's `init_beanie` is running on
    the same collection (`IndexBuildAborted`, code 276 → worker crash).

    The first weekly-settlement deploy created `settlement_batches` with a
    UNIQUE index on `week_key`. The scoped-settlement schema makes `week_key`
    NON-unique (the unique gate is now `run_key`). Beanie cannot reconcile a
    same-named index whose `unique` flag changed (`IndexKeySpecsConflict`,
    code 86) and the whole app fails to start. We fix it BEFORE init_beanie:
      • empty collection (feature never ran) → drop it; Beanie recreates clean.
      • has data → drop only the conflicting legacy indexes so rows survive.
    Fully idempotent — once the indexes match the model this is a no-op."""
    existing_colls = await db.list_collection_names()
    _legacy_idx = {
        "settlement_batches": ("week_key_1",),
        "position_settlements": ("batch_id_1_old_position_id_1",),
    }
    for coll_name, legacy_names in _legacy_idx.items():
        if coll_name not in existing_colls:
            continue
        coll = db[coll_name]
        if await coll.estimated_document_count() == 0:
            await coll.drop()
            logger.info(
                "settlement_index_heal_dropped_empty_collection",
                extra={"collection": coll_name},
            )
            continue
        for idx_name in legacy_names:
            try:
                await coll.drop_index(idx_name)
                logger.info(
                    "settlement_index_heal_dropped_legacy_index",
                    extra={"collection": coll_name, "index": idx_name},
                )
            except Exception:  # pragma: no cover - index already absent
                pass


async def _run_schema_heal_once(db: AsyncIOMotorDatabase) -> None:
    """Cross-worker startup barrier for the destructive index heal.

    gunicorn runs N workers; each calls `init_database()` near-simultaneously
    on restart. Without coordination, one worker's heal `drop()` aborts another
    worker's concurrent `init_beanie` index build → `IndexBuildAborted`
    (code 276) and that worker crashes. Redis isn't up yet at this point
    (`init_redis()` runs AFTER `init_database()`), so we coordinate via a
    MongoDB lock document:
      • Winner (insert succeeds, or reclaims a stale/crashed lock) runs the
        heal, then marks `done` — and ONLY THEN do any workers call init_beanie.
      • Followers wait for `done` before returning, so no drop ever races an
        index build.
    Idempotent across restarts: once `done` is set it short-circuits (the heal
    is a permanent one-time migration), so future boots skip it entirely and
    never drop anything → the race can never recur."""
    locks = db["_startup_locks"]
    now = datetime.now(timezone.utc)

    claimed = False
    try:
        await locks.insert_one(
            {"_id": _SCHEMA_HEAL_LOCK_ID, "started_at": now, "done": False}
        )
        claimed = True
    except DuplicateKeyError:
        existing = await locks.find_one({"_id": _SCHEMA_HEAL_LOCK_ID})
        if existing and existing.get("done"):
            return  # already healed by a prior boot/worker — nothing to do
        # Leader crashed mid-heal → reclaim atomically if the claim is stale.
        res = await locks.update_one(
            {
                "_id": _SCHEMA_HEAL_LOCK_ID,
                "done": {"$ne": True},
                "started_at": {"$lt": now - _SCHEMA_HEAL_STALE_AFTER},
            },
            {"$set": {"started_at": now}},
        )
        claimed = res.modified_count == 1

    if claimed:
        try:
            await _do_index_heal(db)
        except Exception:  # pragma: no cover - never block startup on the heal
            logger.exception("settlement_index_heal_failed_continuing")
        finally:
            await locks.update_one(
                {"_id": _SCHEMA_HEAL_LOCK_ID},
                {"$set": {"done": True, "finished_at": datetime.now(timezone.utc)}},
            )
        return

    # Follower: wait for the leader to finish so init_beanie can't race a drop.
    deadline = time.monotonic() + _SCHEMA_HEAL_WAIT_TIMEOUT_SEC
    while time.monotonic() < deadline:
        doc = await locks.find_one({"_id": _SCHEMA_HEAL_LOCK_ID})
        if doc and doc.get("done"):
            return
        await asyncio.sleep(0.5)
    logger.warning("settlement_index_heal_barrier_timeout_proceeding")


async def init_database() -> None:
    """Open Motor client, register Beanie documents, ensure indexes."""
    global _client, _db

    kwargs: dict[str, object] = {
        "maxPoolSize": settings.MONGODB_MAX_POOL_SIZE,
        "minPoolSize": settings.MONGODB_MIN_POOL_SIZE,
        "serverSelectionTimeoutMS": 5000,
        "uuidRepresentation": "standard",
        # Return tz-aware (UTC) datetimes from MongoDB instead of naive ones,
        # so Pydantic serializes them with a `+00:00` offset and JS clients
        # parse them as UTC (not local time).
        "tz_aware": True,
    }
    if settings.MONGODB_REPLICA_SET:
        kwargs["replicaSet"] = settings.MONGODB_REPLICA_SET

    _client = AsyncIOMotorClient(settings.MONGODB_URL, **kwargs)
    _db = _client[settings.MONGODB_DB_NAME]

    try:
        await _client.admin.command("ping")
    except ServerSelectionTimeoutError as e:  # pragma: no cover
        logger.error("mongodb_unreachable", extra={"error": str(e)})
        raise

    # ── Pre-init migration: heal weekly-settlement index conflict ───
    # Runs the destructive legacy-index heal in exactly ONE worker and makes
    # the others wait for it, so no `drop()` races a sibling worker's
    # `init_beanie` index build (`IndexBuildAborted`, code 276). See
    # `_run_schema_heal_once` for the full rationale. Never blocks startup —
    # any failure inside is swallowed and logged.
    try:
        await _run_schema_heal_once(_db)
    except Exception:  # pragma: no cover - never block startup on the heal
        logger.exception("settlement_index_heal_barrier_failed_continuing")

    await init_beanie(database=_db, document_models=_document_models())
    logger.info("mongodb_connected", extra={"db": settings.MONGODB_DB_NAME})

    # ── Migration: drop legacy global-unique account_number index ───
    # Was a single-field unique on `company_bank_accounts.account_number`
    # which prevented two admins / brokers from registering the same
    # bank account in their own pools (a Beanie-managed model change
    # alone wouldn't drop the old index — Mongo keeps both the new
    # compound and the old single-field index, and the old one keeps
    # rejecting writes). One-shot drop here so the new compound key
    # (account_number, owner_admin_id, owner_broker_id) is the only
    # uniqueness constraint going forward. Idempotent: if the index
    # is already gone, the drop call is a no-op via the exception
    # swallow.
    try:
        coll = _db["company_bank_accounts"]
        existing = await coll.index_information()
        for name, spec in existing.items():
            keys = spec.get("key") or []
            if (
                len(keys) == 1
                and keys[0][0] == "account_number"
                and spec.get("unique")
            ):
                await coll.drop_index(name)
                logger.info(
                    "dropped_legacy_index",
                    extra={
                        "collection": "company_bank_accounts",
                        "index": name,
                    },
                )
    except Exception:
        logger.warning(
            "drop_legacy_account_number_index_failed",
            exc_info=True,
        )


async def close_database() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
        _client = None
        _db = None
        logger.info("mongodb_disconnected")


async def healthcheck() -> bool:
    try:
        await get_client().admin.command("ping")
        return True
    except Exception:  # pragma: no cover
        return False
