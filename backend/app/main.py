"""FastAPI app entry — middleware, routers, lifespan.

Phase 1 mounts only auth + profile routers. Subsequent phases add more
routers under /api/v1/user and /api/v1/admin.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from prometheus_fastapi_instrumentator import Instrumentator

from app import __version__
from app.api.v1 import branding as branding_public
from app.api.v1 import webhooks as webhooks_public
from app.api.v1.admin import router as admin_router
from app.api.v1.user import router as user_router
from app.api.ws import router as ws_router
from app.core.config import settings
from app.core.database import close_database, healthcheck as db_health, init_database
from app.core.exceptions import register_exception_handlers
from app.core.logging_config import configure_logging
from app.core.redis_client import (
    close_redis,
    healthcheck as redis_health,
    init_redis,
)
from app.schemas.common import APIResponse, HealthResponse

logger = logging.getLogger(__name__)


async def _claim_boot_once() -> bool:
    """Claim the per-deploy right to run the one-time boot migrations.

    In a multi-worker deployment every worker reaches the boot-migration
    block at once; without a guard they each run the (idempotent but heavy)
    seeds / backfills / index-heals concurrently — N× the load and a needless
    index-heal stampede. A short-lived Redis NX claim keyed by app version
    lets exactly ONE worker per deploy run them; the rest skip straight to
    serving (the results land in shared Mongo, so they still see them).
    Falls back to True (run them) when Redis is unavailable so single-worker
    and dev boots are unaffected.
    """
    try:
        from app.core.redis_client import get_redis

        claimed = await get_redis().set(
            f"boot:migrations:{__version__}", "1", nx=True, ex=600
        )
        return bool(claimed)
    except Exception:
        return True


async def run_boot_migrations() -> None:
    """One-time idempotent seeds / backfills / index heals.

    Extracted verbatim from the lifespan so it can be gated behind
    ``_claim_boot_once()`` for multi-worker boots. Body unchanged.
    """
    if settings.RUN_SEED_ON_STARTUP:
        from app.seed.instruments import seed_instruments
        from app.seed.seed_data import run_seed

        try:
            await run_seed()
            await seed_instruments()
        except Exception:
            logger.exception("seed_failed_continuing_anyway")

    # Always run the index-lot backfill — even when seeding is off the DB
    # may still hold rows from earlier runs with the wrong lot_size (NIFTY 50,
    # auto-created rows stuck at 1, etc). Idempotent: no-op once everything
    # already matches the canonical values.
    try:
        from app.seed.instruments import backfill_index_lot_sizes

        await backfill_index_lot_sizes()
    except Exception:
        logger.exception("backfill_index_lots_failed_continuing")

    # Populate the lowercased prefix-search fields on any Instrument rows that
    # predate them (or came in via seed `insert_many` / raw writes that bypass
    # the before-save hook). Idempotent — scoped to rows missing symbol_lc, so
    # it's a no-op on every boot after the first. Without this, the fast
    # prefix search returns nothing for un-backfilled rows.
    try:
        from app.services.instrument_service import backfill_search_keys

        await backfill_search_keys()
    except Exception:
        logger.exception("backfill_instrument_search_keys_failed_continuing")

    # Heal legacy `marginCalcMode = "percent"` rows on every boot. Old seed
    # default locked freshly-seeded NSE_FUT / NSE_OPT / MCX_OPT etc. into
    # percent mode with intradayMargin = 100, so the user-side panel showed
    # "100.00% · ₹{notional}/lot" until the admin explicitly clicked the
    # Mode dropdown. This heal resets seed-default rows to NULL so the
    # resolver's defensive inference picks the right mode automatically.
    # Idempotent — no-op once those rows are cleaned up or customised.
    try:
        from app.services.netting_service import heal_legacy_percent_seeds

        healed = await heal_legacy_percent_seeds()
        if healed:
            logger.info("startup_healed_legacy_percent_seeds count=%d", healed)
    except Exception:
        logger.exception("heal_legacy_percent_seeds_failed_continuing")

    # Backfill agreement_type on legacy P&L sharing agreements and drop the
    # old (admin_id, broker_id) unique index — replaced by the per-type
    # index so the same pair can hold both PNL_AND_BROKERAGE and
    # BROKERAGE_ONLY agreements simultaneously. Idempotent — no-op after
    # the first successful boot post-deploy.
    try:
        from app.services.pnl_sharing_service import (
            heal_pnl_sharing_agreement_type,
        )

        healed_pnl = await heal_pnl_sharing_agreement_type()
        if healed_pnl:
            logger.info(
                "startup_healed_pnl_sharing_agreement_type count=%d", healed_pnl
            )
    except Exception:
        logger.exception("heal_pnl_sharing_agreement_type_failed_continuing")

    # ── Heal fragmented open positions + enforce one-open-per-(user,token,product) ──
    # Root cause of same-stock fragmentation across order types: apply_fill's
    # non-atomic find-then-insert let a MARKET fill race a pending LIMIT/SL-M
    # fire (in the 1.5 s poller) into TWO open rows, after which later fills
    # merged into only one and the other's average went stale. Consolidate any
    # existing duplicates (money-neutral weighted-avg netting), then create a
    # PARTIAL UNIQUE index so the DB can never hold two OPEN positions for the
    # same (user, instrument, product) again — apply_fill now retries-on-dup
    # and merges instead. Idempotent: no-op once clean + index exists.
    try:
        from app.models.position import Position as _Position
        from app.services.position_service import (
            consolidate_open_positions as _consolidate_open_positions,
        )

        _pcoll = _Position.get_motor_collection()
        _dup_uids: set = set()
        async for _row in _pcoll.aggregate(
            [
                {"$match": {"status": "OPEN"}},
                {
                    "$group": {
                        "_id": {
                            "u": "$user_id",
                            "t": "$instrument.token",
                            "p": "$product_type",
                        },
                        "n": {"$sum": 1},
                        "uid": {"$first": "$user_id"},
                    }
                },
                {"$match": {"n": {"$gt": 1}}},
            ]
        ):
            _dup_uids.add(_row["uid"])
        _collapsed = 0
        for _uid in _dup_uids:
            try:
                _collapsed += await _consolidate_open_positions(_uid)
            except Exception:
                logger.exception("frag_position_consolidate_failed")
        if _dup_uids:
            logger.info(
                "startup_healed_fragmented_positions users=%d groups=%d",
                len(_dup_uids),
                _collapsed,
            )
        # Create the partial unique index (idempotent). A DuplicateKeyError
        # here means some dups survived consolidation — logged, never fatal.
        try:
            await _pcoll.create_index(
                [("user_id", 1), ("instrument.token", 1), ("product_type", 1)],
                name="uniq_open_position_user_token_product",
                unique=True,
                partialFilterExpression={"status": "OPEN"},
            )
            logger.info("open_position_unique_index_ready")
        except Exception:
            logger.exception(
                "open_position_unique_index_create_failed_dups_may_remain"
            )
    except Exception:
        logger.exception("heal_fragmented_positions_failed_continuing")

    # ── Historical wallet migrations: DISABLED 21-May per operator ───
    # Operator decision (after seeing MEHUL/CL62477932 state):
    #     "esse pehle vale logic galat tha settlement aaj se start
    #      karo, ab se next trade se pehle ka rehne do"
    # i.e. leave any existing wallet state — available_balance,
    # used_margin, settlement_outstanding, realized_pnl — exactly as
    # it is right now. The new floor-at-0 / route-to-settlement rule
    # in `wallet_service.adjust()` applies ONLY to new debits from
    # this point forward. No retroactive clamping, no retroactive
    # PnL backfill that could re-rewrite tracker fields.
    #
    # The helpers themselves stay in `wallet_service` so an operator
    # can run them manually later if they ever want to bulk-repair —
    # but the boot hooks are gone so a redeploy never silently
    # mutates user balances. Functions to call manually if needed:
    #   • wallet_service.clamp_negative_balances_to_settlement()
    #   • wallet_service.recompute_realized_pnl_for_all()

    # White-label branding: drop the obsolete `custom_domain_unique_sparse`
    # index left over from the very first Phase-1 deploy. The original
    # design used `sparse=True`, but MongoDB sparse indexes only skip
    # MISSING fields — they STILL index `null` values, and Beanie always
    # serializes the optional `custom_domain: None` default into the
    # document, so the unique constraint collapsed to "at most one user
    # row with custom_domain=null" → every second user insert hit
    # E11000 → 500 on /admin/management/sub-admins, /auth/register, etc.
    # The replacement `custom_domain_unique_partial` index uses
    # `partialFilterExpression={custom_domain: {$type: "string"}}` which
    # correctly indexes only rows that have a real string value.
    # Beanie creates the new index but never drops the old one — this
    # heal handles the swap. Idempotent: NamespaceNotFound (collection
    # missing) and IndexNotFound (already dropped) are both no-ops.
    try:
        from app.core.database import get_db

        _coll = get_db()["users"]
        try:
            await _coll.drop_index("custom_domain_unique_sparse")
            logger.info("startup_dropped_obsolete_sparse_index name=custom_domain_unique_sparse")
        except Exception as _exc:
            # `IndexNotFound` (code 27) and `NamespaceNotFound` (code 26)
            # both mean "nothing to clean up" — expected on every boot
            # after the first one. Anything else is logged but never
            # halts startup (worst case: the next sub-admin create
            # fails with E11000 and the operator runs the manual
            # `db.users.dropIndex` from DEPLOY_BRANDING.md).
            msg = str(_exc).lower()
            if "indexnotfound" not in msg and "ns not found" not in msg and "index not found" not in msg:
                logger.warning("startup_drop_obsolete_sparse_index_failed err=%s", _exc)
    except Exception:
        logger.exception("startup_branding_index_cleanup_failed_continuing")

    # Settings snapshot backfill: walks every existing ADMIN and BROKER
    # and ensures their tier-specific override table has one row per
    # segment, seeded from the creator's effective settings
    # (admin ← super-admin, broker ← admin/super, sub-broker ← parent
    # broker). Brings legacy tiers in line with the new copy-on-create
    # policy without forcing the operator to recreate each account.
    # Idempotent — per-segment upserts skip rows that already exist.
    #
    # `repair_null_seed_rows` runs FIRST to delete rows written by the
    # buggy 21-May boot (NettingSegment.segment_name → name) so the
    # subsequent backfill regenerates them with the seed values.
    try:
        from app.services.settings_snapshot import (
            backfill_missing_snapshots,
            repair_null_seed_rows,
        )

        repair = await repair_null_seed_rows()
        if repair.get("admin_deleted") or repair.get("broker_deleted"):
            logger.info(
                "startup_repaired_null_seed_rows admin=%d broker=%d",
                repair.get("admin_deleted", 0),
                repair.get("broker_deleted", 0),
            )

        bf_result = await backfill_missing_snapshots()
        if bf_result.get("admins_filled") or bf_result.get("brokers_filled"):
            logger.info(
                "startup_backfilled_settings_snapshots admins=%d brokers=%d",
                bf_result.get("admins_filled", 0),
                bf_result.get("brokers_filled", 0),
            )
    except Exception:
        logger.exception("settings_snapshot_backfill_failed_continuing")


# ── Risk-shard admission gate (multi-worker balance) ─────────────────────────
# Each shard's loop runs on EVERY worker and races for its own
# `leader:risk:shard:k` Redis lock, so ONE worker could win several shard locks
# and run multiple sweeps on a single event loop — exactly the 3-shards-on-one-
# worker case that starved the loop (3s+ `ltp_ms`, 14s `risk_enforcer_tick_overrun`).
# This per-PROCESS gate caps a worker at one risk shard and HARD-EXCLUDES the
# feed leader (already saturated by the 0.1s tick fanout — never run risk there),
# so the N shards fan out across N distinct HTTP backend workers.
#
# SAFETY (correctness > balance): if a shard stays denied for a few poll cycles
# — fewer live workers than shards, e.g. after a crash — the gate relaxes so the
# shard is NEVER left un-enforced (which would mean no SL/TP/stop-out for those
# users). The per-shard Redis lock still guarantees exactly one runner, so a
# relaxed attempt only ever wins a genuinely unclaimed shard. All logic here is
# synchronous (no await) → race-free across one worker's sibling shard loops.
_local_risk_shards_held: set[int] = set()
_risk_shard_admit_denials: dict[int, int] = {}
_MAX_RISK_SHARDS_PER_WORKER = 1
_RISK_SHARD_RELAX_CYCLES = 3  # ~15s at the 5s leader poll → then ignore the cap


def _risk_shard_admit(shard_id: int) -> bool:
    from app.services import market_data_service

    if shard_id in _local_risk_shards_held:
        return True
    is_leader = market_data_service.is_feed_leader()
    # HARD EXCLUSION: the feed-leader process NEVER runs a risk shard.
    # It is the WS-pool + 0.1s tick-fanout hot path on a single event loop
    # (routinely CPU-pegged). A co-located risk sweep there both stalls tick
    # publishing AND overruns 3-7s under load (observed 1800×/day when a
    # recycling single HTTP worker kept dropping its shard lock and this
    # ever-alive leader grabbed all four). Risk sharding lives EXCLUSIVELY on
    # the HTTP backend workers, which read prices from the leader's `mdlive`
    # Redis snapshot; their own relax path below guarantees every shard stays
    # covered even while individual workers recycle. (RISK_SHARDS<=1 single-
    # process dev is unaffected — that path co-locates risk in the feed
    # factory and never reaches this gate.)
    if is_leader:
        return False
    capped = len(_local_risk_shards_held) >= _MAX_RISK_SHARDS_PER_WORKER
    if not capped:
        _local_risk_shards_held.add(shard_id)
        _risk_shard_admit_denials.pop(shard_id, None)
        return True
    # Non-leader worker already at its shard cap. Count denials and relax after
    # a grace period so a shard with no other taker (fewer live workers than
    # shards, e.g. after a crash/recycle) is NEVER left un-enforced — the
    # per-shard Redis lock still guarantees exactly one runner cluster-wide.
    n = _risk_shard_admit_denials.get(shard_id, 0) + 1
    _risk_shard_admit_denials[shard_id] = n
    if n >= _RISK_SHARD_RELAX_CYCLES:
        _local_risk_shards_held.add(shard_id)
        _risk_shard_admit_denials.pop(shard_id, None)
        return True
    return False


def _risk_shard_release(shard_id: int) -> None:
    _local_risk_shards_held.discard(shard_id)
    _risk_shard_admit_denials.pop(shard_id, None)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()

    if settings.SENTRY_DSN:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            traces_sample_rate=0.05 if settings.is_production else 1.0,
            environment=settings.APP_ENV,
            release=__version__,
        )
        logger.info("sentry_initialized")

    await init_database()
    try:
        await init_redis()
    except Exception:
        logger.warning("redis_unavailable_starting_without_cache")

    # Start the process-wide WebSocket hubs. Each hub holds a single
    # shared Redis pub/sub connection and fans messages out to all
    # attached sockets in-process — replaces the previous design where
    # every connected WS opened its own pub/sub. Idempotent and tolerant
    # of Redis being unavailable (handlers will retry start() on connect).
    try:
        from app.core.ws_hub import start_all_hubs

        await start_all_hubs()
    except Exception:
        logger.warning("ws_hubs_startup_failed_continuing")

    if await _claim_boot_once():
        await run_boot_migrations()
    else:
        logger.info("boot_migrations_skipped_non_leader_worker")

    # Start mock market data tick loop
    import asyncio as _asyncio
    from functools import partial as _partial

    from app.core.leader_lock import leader_elected as _leader_elected
    from app.core.loop_supervisor import supervise as _supervise
    from app.services import market_data_service

    def _leader_only(loop_name: str, fn, /, **kwargs):
        """Wrap a loop factory so it only runs on the cluster leader.

        The leader lock is keyed by ``leader:{loop_name}`` in Redis and
        held with a 30 s TTL renewed every ~10 s. If the leader dies,
        a standby worker picks up within `poll_sec` (5 s default).
        """
        return _partial(
            _leader_elected,
            loop_name,
            _partial(fn, **kwargs),
            lock_key=f"leader:{loop_name}",
        )

    # 250 ms tick fanout — matches what the web frontend's `useMarketStream`
    # comment refers to ("WS pump now runs at 250 ms"). The previous 1 s
    # default made mobile prices feel laggy compared to web because the
    # tick_loop is what bridges the fast Zerodha/Infoway WS overlays into
    # the per-token Redis channels that `/ws/marketdata` clients subscribe
    # to. At 1 Hz, even when the upstream feed delivered ticks at 100 ms,
    # the user saw a refresh only every second. 4×-faster pump = sub-second
    # bid/ask movement on the APK and web, matching what the user expects.
    #
    # Every background loop below is wrapped in `supervise()` so an
    # uncaught exception escaping the loop's own try/except cannot
    # silently kill it for the rest of the process lifetime — the
    # supervisor logs the crash, backs off, and restarts the loop.
    # Loop internals, intervals and shutdown semantics are unchanged.
    # ── Feed leader (multi-worker) ──────────────────────────────────
    # Every upstream feed connection MUST live on a SINGLE worker — else
    # we'd open N Zerodha WS pools + N Infoway connections (blowing past
    # Kite's per-account slot limit) and fan out N duplicate tick streams.
    # So the market-tick fanout, the Infoway feed, the Zerodha WS pool, the
    # Zerodha WS self-heal and the cross-worker `feed:subscribe` listener
    # are ALL driven by ONE leader-gated factory sharing the SAME
    # `leader:feed` lock, so they co-locate on whichever worker holds it.
    # Non-leader workers serve quotes/fills from the leader's `mdlive`
    # Redis snapshot instead. On a single worker this process simply IS the
    # leader → behaviour identical to the previous always-on tick loop.
    async def _feed_leader_main() -> None:
        market_data_service.set_feed_leader(True)
        # Feed process split. "all" (default) runs everything on one core;
        # "main" runs Zerodha + its helpers; "global" runs crypto/forex/metals
        # only. Shared loops (tick_loop, poller, subscribe listener, open-pos,
        # aggregator) run in BOTH and self-scope by which upstream populates
        # `_state`; the market_data_service token filter keeps each side off
        # the other's feed.
        _grp = (settings.FEED_GROUP or "all").strip().lower()
        market_data_service.set_feed_group(_grp)
        _run_global = _grp in ("all", "global")  # Infoway / Binance / MetaAPI
        _run_main = _grp in ("all", "main")       # Zerodha WS pool + helpers
        logger.info(
            "feed_leader_starting group=%s run_main=%s run_global=%s",
            _grp, _run_main, _run_global,
        )
        subtasks: list[_asyncio.Task] = []
        try:
            # Infoway (forex / crypto / metals / energy) — leader-only.
            if _run_global and settings.INFOWAY_AUTO_CONNECT and settings.INFOWAY_API_KEY.get_secret_value():
                try:
                    from app.services.infoway_service import (
                        default_symbols,
                        infoway,
                        mirror_subscribed_to_instruments,
                    )

                    await infoway.start()
                    await infoway.subscribe(default_symbols())
                    # Mirror Infoway codes into the Instrument collection so
                    # /instruments/search finds forex/crypto/metals on every
                    # worker (shared Mongo). Idempotent.
                    mirrored = await mirror_subscribed_to_instruments()
                    logger.info(
                        "infoway_auto_started",
                        extra={"symbols": len(default_symbols()), "mirrored": mirrored},
                    )
                except Exception:
                    logger.exception("infoway_auto_start_failed")

            # Binance crypto feed (free public WS, no API key) — leader-only.
            # When BINANCE_CRYPTO_FEED=true it supplies crypto prices (BTCUSDT…)
            # in place of Infoway; forex / metals / energy are untouched. No-op
            # when the flag is off.
            try:
                from app.services.binance_service import binance as _binance

                if _run_global and _binance.is_enabled():
                    await _binance.start()
                    logger.info("binance_feed_auto_started")
            except Exception:
                logger.exception("binance_feed_auto_start_failed")

            # MetaAPI (forex / metals / indices / commodities) feed — leader-only.
            # When METAAPI_FEED=true it supplies those segments in place of
            # Infoway (Infoway stays the automatic fallback). No-op when off.
            try:
                from app.services.metaapi_service import metaapi as _metaapi

                if _run_global:
                    # start() self-skips when the admin panel / .env has it off.
                    await _metaapi.start()
                    # Connect + disconnect come from the admin API on another
                    # worker; the pool lives here, so listen for those commands.
                    subtasks.append(
                        _asyncio.create_task(
                            _supervise("metaapi_cmd_listener", _metaapi.cmd_listener),
                            name="metaapi_cmd_listener",
                        )
                    )
                    logger.info("metaapi_feed_auto_started")
            except Exception:
                logger.exception("metaapi_feed_auto_start_failed")

            # Zerodha live WS pool — leader-only. The instrument CATALOG warm
            # already ran on EVERY worker in `_zerodha_boot` (search needs it
            # cluster-wide); here we only open the live WS connections.
            try:
                from app.services.zerodha_service import zerodha as _zerodha

                try:
                    _zerodha._main_loop = _asyncio.get_running_loop()
                except RuntimeError:
                    pass
                z_status = await _zerodha.get_status()
                if _run_main and z_status.get("isConnected"):
                    try:
                        await _zerodha.connect_ws()
                        logger.info("zerodha_ws_pool_started_on_boot")
                    except Exception:
                        logger.exception("zerodha_ws_pool_start_failed")
                    # Account B: spawn its WS slot too if it has a token.
                    try:
                        s_b = await _zerodha._get_settings(1)
                        if s_b.apiKey and s_b.accessToken and s_b.isConnected:
                            await _zerodha._spawn_ws_connection(s_b.apiKey, s_b.accessToken)
                            logger.info("zerodha_account_b_ws_spawned_on_boot")
                    except Exception:
                        logger.exception("zerodha_account_b_boot_failed")
            except Exception:
                logger.exception("zerodha_ws_boot_failed")

            # Leader-only background helpers — supervised (self-restart on
            # crash) and cancelled together when leadership is lost / shutdown.
            from app.services.zerodha_service import zerodha as _zerodha_heal

            subtasks.append(
                _asyncio.create_task(
                    _supervise(
                        "feed_subscribe_listener",
                        market_data_service.feed_subscribe_listener,
                    ),
                    name="feed_subscribe_listener",
                )
            )
            # Open-position subscription reconcile — co-located on the feed
            # leader because it mutates the in-process `_subscribed` set that
            # tick_loop reads to mirror quotes into `mdlive`. Guarantees every
            # held token stays on the live feed so the risk enforcer always has
            # a price for its SL/TP/stop-out (fixes the risk_ltp_fetch_failed
            # flood where unwatched held legs got no mdlive and were skipped).
            subtasks.append(
                _asyncio.create_task(
                    _supervise(
                        "open_position_subscriptions",
                        _partial(
                            market_data_service.open_position_subscription_loop,
                            interval_sec=120.0,
                        ),
                    ),
                    name="open_position_subscriptions",
                )
            )
            # Zerodha-only helpers — self-heal, dual-account HA failover
            # controller, and the admin failover-command listener. All three
            # read/steer the leader's IN-PROCESS Zerodha WS pool, so they run
            # ONLY on the process that owns it (main / all). On the global feed
            # process they'd have no pool AND the failover loop would overwrite
            # the real status in Redis — so gate them off entirely.
            if _run_main:
                subtasks += [
                    _asyncio.create_task(
                        _supervise(
                            "zerodha_ws_self_heal",
                            _partial(_zerodha_heal.ws_self_heal_loop, interval_sec=30.0),
                        ),
                        name="zerodha_ws_self_heal",
                    ),
                    _asyncio.create_task(
                        _supervise(
                            "zerodha_feed_failover",
                            _partial(_zerodha_heal.feed_failover_loop, interval_sec=3.0),
                        ),
                        name="zerodha_feed_failover",
                    ),
                    _asyncio.create_task(
                        _supervise(
                            "zerodha_failover_cmd",
                            _zerodha_heal.feed_failover_cmd_listener,
                        ),
                        name="zerodha_failover_cmd",
                    ),
                ]
            # Per-minute bid/ask aggregator flush — co-located under the
            # leader:feed gate because tick_loop (above) folds live quotes into
            # this worker's IN-PROCESS bucket memory; the flush loop persists
            # each completed minute to `tick_snapshots` for the admin Rate
            # History modal. Leader-only ⇒ exactly one writer, no dupes.
            from app.services import tick_aggregator as _tick_agg

            subtasks.append(
                _asyncio.create_task(
                    _supervise(
                        "tick_aggregator_flush",
                        _partial(
                            _tick_agg.tick_aggregator_flush_loop,
                            interval_sec=60.0,
                        ),
                    ),
                    name="tick_aggregator_flush",
                )
            )

            # Hot trading loops that read the LEADER's IN-PROCESS price state
            # (`_state` / Zerodha `ticks_by_token`) via the zero-network
            # get_ltp_instant / get_quote_instant fast paths MUST co-locate
            # here. Those reads are only warm on the worker running tick_loop;
            # gating them under a SEPARATE leader lock would let them land on
            # a cold worker → silent no-op (no stop-outs, no margin calls, no
            # LIMIT/SL-M fires). So they ride the SAME leader:feed gate.
            from app.services.matching_engine import pending_order_poller
            from app.services.risk_enforcer import risk_enforcer_loop

            subtasks.append(
                _asyncio.create_task(
                    _supervise(
                        "pending_order_poller",
                        _partial(pending_order_poller, interval_sec=1.5),
                    ),
                    name="pending_order_poller",
                )
            )
            # RISK_SHARDS <= 1 (DEFAULT): risk_enforcer co-locates on the feed
            # leader, reading prices in-process — byte-for-byte today's setup.
            # RISK_SHARDS > 1: the risk loop is NOT started here; instead N
            # independently-gated shard loops are registered below (each on its
            # own `leader:risk:shard:k`), reading prices from `mdlive`.
            # On a split, co-located risk stays with the main (Zerodha) process.
            if _run_main and settings.RISK_SHARDS <= 1:
                subtasks.append(
                    _asyncio.create_task(
                        _supervise(
                            "risk_enforcer",
                            _partial(risk_enforcer_loop, interval_sec=settings.RISK_TICK_SEC),
                        ),
                        name="risk_enforcer",
                    )
                )
            # Daily Zerodha auto-login (3-layer WS reconnect after the 08:00 IST
            # token rotation) + subscription LRU-trim (evict stale option strikes
            # so tick parsing + Kite's per-WS cap never overrun). BOTH act on
            # THIS worker's in-process Zerodha ticker, so they run ONLY on the
            # process that owns the pool (main / all) — never the global feed.
            # trim always preserves open positions / watchlists / pinned tokens.
            if _run_main:
                from app.services.zerodha_auto_login_scheduler import (
                    zerodha_auto_login_loop,
                )
                subtasks += [
                    _asyncio.create_task(
                        _supervise("zerodha_auto_login_scheduler", zerodha_auto_login_loop),
                        name="zerodha_auto_login_scheduler",
                    ),
                    _asyncio.create_task(
                        _supervise(
                            "zerodha_subscription_trim",
                            _partial(
                                _zerodha_heal.subscription_trim_loop,
                                interval_sec=300.0,
                                keep_count=1200,
                            ),
                        ),
                        name="zerodha_subscription_trim",
                    ),
                ]

            # Drive the tick fanout in the foreground until cancelled
            # (leadership lost / shutdown) or its own _running flag clears.
            # Interval is env-tunable (MARKET_TICK_SEC, default 0.07s ≈ 14 Hz)
            # so price movement is snappy and `mdlive` stays fresh for risk.
            await market_data_service.tick_loop(interval_sec=settings.MARKET_TICK_SEC)
        finally:
            market_data_service.set_feed_leader(False)
            for _t in subtasks:
                _t.cancel()
            for _t in subtasks:
                try:
                    await _t
                except (_asyncio.CancelledError, Exception):
                    pass

    # Feed/loop process isolation: only processes with RUN_FEED_LOOP=true
    # compete for the `leader:feed` lock and drive the feed bundle. Set it
    # False on the gunicorn HTTP service and run ONE dedicated process with
    # it True — the feed then runs on that process's own event loop and can
    # never be starved by HTTP request load (root-cause fix for the frozen-
    # price incident where the feed-leader worker sat pegged at ~100% CPU).
    # Default True ⇒ single-process / dev behaviour is unchanged.
    if settings.RUN_FEED_LOOP:
        # Feed-split: the global feed process elects on its OWN lock
        # (`leader:feed:global`) so it can run CONCURRENTLY with the main
        # (Zerodha) process which keeps `leader:feed`. FEED_GROUP=all/main
        # both use `leader:feed` (mutually exclusive — only one wins), so a
        # stray "all" process can never double-run the global feeds.
        _feed_lock = (
            "feed:global"
            if (settings.FEED_GROUP or "all").strip().lower() == "global"
            else "feed"
        )
        feed_leader_task: _asyncio.Task = _asyncio.create_task(
            _supervise("feed", _leader_only(_feed_lock, _feed_leader_main)),
            name="feed_leader",
        )
        # Keep reference on the app so it isn't GC'd and can be cancelled cleanly on shutdown
        setattr(app, "_feed_leader_task", feed_leader_task)
    else:
        logger.info("feed_loop_disabled_on_this_process (RUN_FEED_LOOP=false)")

    # ── Risk-loop sharding (RISK_SHARDS > 1 only) ───────────────────────
    # DEFAULT RISK_SHARDS=1 → this block is skipped entirely and the risk loop
    # rode the `leader:feed` gate above (today's behaviour). When sharding is
    # ON we instead start N risk loops, each gated by its OWN
    # `leader:risk:shard:k` lock so they spread across N worker processes and
    # each reads prices from the leader's `mdlive` snapshot. Clamp to >=1 and
    # never exceed the (single-node) sanity ceiling of 64 shards.
    _n_shards = max(1, min(int(getattr(settings, "RISK_SHARDS", 1) or 1), 64))
    if _n_shards > 1:
        from app.services.risk_enforcer import risk_enforcer_loop as _risk_loop

        _risk_shard_tasks: list[_asyncio.Task] = []
        for _k in range(_n_shards):
            # Build the leader-gated factory directly (instead of `_leader_only`)
            # so we can pass the per-process admission gate that spreads shards
            # across workers — see `_risk_shard_admit` / `_risk_shard_release`.
            _risk_shard_tasks.append(
                _asyncio.create_task(
                    _supervise(
                        f"risk:shard:{_k}",
                        _partial(
                            _leader_elected,
                            f"risk:shard:{_k}",
                            _partial(
                                _risk_loop,
                                interval_sec=settings.RISK_TICK_SEC,
                                shard_id=_k,
                                num_shards=_n_shards,
                            ),
                            lock_key=f"leader:risk:shard:{_k}",
                            local_admit=_partial(_risk_shard_admit, _k),
                            local_release=_partial(_risk_shard_release, _k),
                        ),
                    ),
                    name=f"risk_shard_{_k}",
                )
            )
        # Keep references so they aren't GC'd; cancelled on shutdown like the rest.
        setattr(app, "_risk_shard_tasks", _risk_shard_tasks)
        logger.info("risk_sharding_enabled", extra={"num_shards": _n_shards})

    # The loops below are wrapped in BOTH `_supervise` (auto-restart on
    # crash) AND `_leader_only` (single-leader across the cluster). They are
    # safe to run on ANY worker because they read prices via the async
    # `get_quote` (which falls back to the leader's `mdlive` Redis snapshot)
    # or only touch Mongo. The risk enforcer + pending-order poller, by
    # contrast, read the leader's IN-PROCESS price state and so run inside
    # the `leader:feed` factory above (NOT here) to stay co-located with it.

    # Expiry cleanup: hourly sweep that removes day-after-expiry instruments
    # from every user's watchlist, unsubscribes them from the Zerodha ticker
    # and marks them inactive in the Instrument collection. The first sweep
    # runs immediately so anything that expired overnight is cleaned at boot.
    from app.services.expiry_cleanup import expiry_cleanup_loop
    expiry_task: _asyncio.Task = _asyncio.create_task(
        _supervise(
            "expiry_cleanup",
            _leader_only("expiry_cleanup", expiry_cleanup_loop, interval_sec=3600.0),
        )
    )

    # Intraday→carryforward auto-rollover: at each segment's exchange-close
    # minute, flip all open MIS positions to NRML. Recomputes the overnight
    # margin via the segment-settings resolver and auto-squareoff's any
    # position whose user can't cover the new requirement. Forex (24/5)
    # and crypto (24/7) are exempt — no daily close means no rollover.
    from app.services.position_service import intraday_to_carry_loop
    rollover_task: _asyncio.Task = _asyncio.create_task(
        _supervise(
            "intraday_to_carry",
            _leader_only("intraday_to_carry", intraday_to_carry_loop, interval_sec=60.0),
        )
    )
    setattr(app, "_intraday_to_carry_task", rollover_task)
    setattr(app, "_expiry_cleanup_task", expiry_task)

    # Bonus expiry sweep (Bonus Management) — hourly; converts wager-met
    # bonuses / claws back the rest once past expiry. Only started when the
    # feature flag is on, so it's fully absent otherwise.
    if settings.BONUSES_ENABLED:
        from app.services.bonus_expiry import bonus_expiry_loop
        bonus_task: _asyncio.Task = _asyncio.create_task(
            _supervise(
                "bonus_expiry",
                _leader_only("bonus_expiry", bonus_expiry_loop, interval_sec=3600.0),
            )
        )
        setattr(app, "_bonus_expiry_task", bonus_task)

    # EOD pending-order auto-cancel: just after 00:00 IST (and once on boot),
    # expire every still-parked NSE/BSE/NFO/BFO/MCX LIMIT/SL-M order so a
    # DAY order can't carry into the next session and fire on the next day's
    # prices. Releases the blocked margin and marks the order EXPIRED. Forex
    # (24×5) / crypto (24×7) are exempt. Mongo-only → own leader lock.
    from app.services.eod_order_cleanup import eod_order_cleanup_loop
    eod_cancel_task: _asyncio.Task = _asyncio.create_task(
        _supervise(
            "eod_order_cleanup",
            _leader_only("eod_order_cleanup", eod_order_cleanup_loop, interval_sec=60.0),
        )
    )
    setattr(app, "_eod_order_cleanup_task", eod_cancel_task)

    # Tracker self-heal: every 15 min, walk every UserPositionTracker row
    # and recompute it from the live Position docs. Catches any drift
    # introduced by an unexpected restart / fill retry / partial flow,
    # so users never get stuck with stale holding_lots blocking their
    # next order (root-cause fix for the BTCUSD holding_lots=47
    # incident on 2026-05-19).
    from app.services.position_service import tracker_reconcile_loop
    tracker_heal_task: _asyncio.Task = _asyncio.create_task(
        _supervise(
            "tracker_reconcile",
            _leader_only("tracker_reconcile", tracker_reconcile_loop, interval_sec=900.0),
        )
    )
    setattr(app, "_tracker_reconcile_task", tracker_heal_task)

    # P&L sharing auto-settle scheduler: every 5 min, scan ACTIVE+AUTO agreements
    # and settle the most recently closed period. Idempotent via unique
    # (agreement_id, period_start) index — duplicate fires are no-ops.
    from app.services.pnl_sharing_service import pnl_sharing_scheduler_loop
    pnl_sharing_task: _asyncio.Task = _asyncio.create_task(
        _supervise(
            "pnl_sharing_scheduler",
            _leader_only("pnl_sharing_scheduler", pnl_sharing_scheduler_loop, interval_sec=300.0),
        )
    )
    setattr(app, "_pnl_sharing_scheduler_task", pnl_sharing_task)

    # Shared demo reset: the login page's "Try Demo" logs everyone into ONE
    # shared demo account (auth_service.GLOBAL_DEMO_EMAIL) instead of minting a
    # throwaway per click. That account accrues everyone's trades, so flatten
    # it and restore the ₹50L virtual balance every 24h. Polls hourly; the 24h
    # cadence is tracked in Redis so it survives restarts.
    from app.services.demo_service import demo_reset_loop
    demo_reset_task: _asyncio.Task = _asyncio.create_task(
        _supervise(
            "demo_reset",
            _leader_only("demo_reset", demo_reset_loop, interval_sec=3600.0),
        )
    )
    setattr(app, "_demo_reset_task", demo_reset_task)

    # Divinepay deposit reconcile: every 20 s, ask the gateway about recent
    # PENDING gateway deposits and credit the paid ones — so a user who paid
    # then closed the tab before verifying their UTR is still credited. Leader-
    # only + supervised; no-op when the gateway isn't configured.
    from app.services.divinepay_service import deposit_reconcile_loop
    divinepay_task: _asyncio.Task = _asyncio.create_task(
        _supervise(
            "divinepay_reconcile",
            _leader_only("divinepay_reconcile", deposit_reconcile_loop, interval_sec=20.0),
        )
    )
    setattr(app, "_divinepay_reconcile_task", divinepay_task)

    # Weekly settlement engine: wakes every 60 s and, at the Saturday-23:00
    # IST window (Saturday 11 PM — see is_saturday_settlement_window), mark-to-
    # market settles every OPEN position — books the
    # running P&L into the wallet ledger, closes the old position and
    # re-opens an identical fresh one at the settlement price. Leader-only +
    # supervised like the loops above; idempotent via a unique per-week batch
    # so a duplicate fire is a no-op. Gated behind the
    # `weekly_settlement.enabled` PlatformSetting (default ON) so an admin can
    # disable it without a redeploy. Off the trading hot path entirely.
    from app.services.weekly_settlement_service import weekly_settlement_loop
    weekly_settlement_task: _asyncio.Task = _asyncio.create_task(
        _supervise(
            "weekly_settlement",
            _leader_only("weekly_settlement", weekly_settlement_loop, interval_sec=60.0),
        )
    )
    setattr(app, "_weekly_settlement_task", weekly_settlement_task)

    # Zerodha instrument CATALOG warm — runs on EVERY worker (search +
    # metadata need the in-process cache cluster-wide). The live WS POOL
    # connect + WS self-heal are leader-only (see `_feed_leader_main`).
    # Infoway auto-start likewise moved into the feed leader factory.
    async def _zerodha_boot():
        try:
            from app.services.zerodha_service import zerodha as _zerodha

            # Capture the event loop early so the WebSocket receive loop
            # and synchronous subscribe/unsubscribe sends have a loop to
            # schedule onto.
            try:
                _zerodha._main_loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            z_status = await _zerodha.get_status()
            # Warm the instrument CATALOG whenever Kite is CONFIGURED — the
            # catalog is a REST fetch (uses the access token), INDEPENDENT of
            # the WS. The previous `isConnected` (WS) gate starved the catalog
            # on every NON-leader worker (the WS is leader-only), so instrument
            # SEARCH returned EMPTY on 3 of 4 workers. Gate on `isConfigured`
            # so every worker warms its own search cache; `fetch_instruments`
            # already fails gracefully if the token is missing/expired.
            if not z_status.get("isConfigured"):
                return
            for ex in ("NSE", "NFO", "MCX"):
                try:
                    instruments = await _zerodha.fetch_instruments(ex)
                    logger.info("zerodha_cache_warmed", extra={"exchange": ex, "count": len(instruments)})
                except Exception:
                    logger.warning(f"zerodha_cache_warm_{ex}_failed")
        except Exception:
            logger.exception("zerodha_startup_init_failed")

    asyncio.create_task(_zerodha_boot())

    # Zerodha auto-login daily scheduler now runs INSIDE the leader:feed
    # factory above — its WS reconnect + verify must act on the worker that
    # owns the ticker pool (see `_feed_leader_main`), so it can't run as an
    # independently-locked loop on a possibly-different worker.

    # Demo account cleanup: hourly sweep deletes demo accounts older than 7 days
    # along with all their data (orders, positions, trades, wallet, transactions).
    async def _demo_cleanup_loop():
        from datetime import timedelta

        from app.models.holding import Holding
        from app.models.order import Order
        from app.models.position import Position
        from app.models.trade import Trade
        from app.models.transaction import DepositRequest, WalletTransaction, WithdrawalRequest
        from app.models.user import User
        from app.models.wallet import Wallet
        from app.services.auth_service import GLOBAL_DEMO_EMAIL

        while True:
            await _asyncio.sleep(3600)
            try:
                from app.utils.time_utils import now_utc

                cutoff = now_utc() - timedelta(days=7)
                # Purge legacy throwaway per-click demos (pre shared-demo era),
                # but NEVER the single shared demo account — it's permanent and
                # self-resets every 24h via demo_reset_loop. Without this guard
                # the shared demo would vanish 7 days after creation.
                demo_users = await User.find(
                    User.is_demo == True,  # noqa: E712
                    User.created_at < cutoff,
                    User.email != GLOBAL_DEMO_EMAIL,
                ).to_list()
                for u in demo_users:
                    uid = u.id
                    await _asyncio.gather(
                        Order.find({"user_id": uid}).delete(),
                        Position.find({"user_id": uid}).delete(),
                        Trade.find({"user_id": uid}).delete(),
                        Wallet.find({"user_id": uid}).delete(),
                        WalletTransaction.find({"user_id": uid}).delete(),
                        DepositRequest.find({"user_id": uid}).delete(),
                        WithdrawalRequest.find({"user_id": uid}).delete(),
                    )
                    await u.delete()
                    logger.info("demo_account_deleted user_id=%s", str(uid))
                if demo_users:
                    logger.info("demo_cleanup_done deleted=%d", len(demo_users))
            except Exception:
                logger.exception("demo_cleanup_failed")

    # Leader-gated: this loop does bulk DELETEs (demo accounts + all their
    # orders/positions/trades/wallet). Deletes are idempotent so running it
    # on every worker is harmless-but-wasteful; the leader gate keeps it to
    # ONE worker so 4 workers don't fire 4 concurrent delete sweeps hourly.
    # It uses async Mongo only (no in-process price state) so it's safe on
    # any worker — hence its own lock, not the leader:feed gate.
    demo_cleanup_task: _asyncio.Task = _asyncio.create_task(
        _supervise("demo_cleanup", _leader_only("demo_cleanup", _demo_cleanup_loop))
    )
    setattr(app, "_demo_cleanup_task", demo_cleanup_task)

    logger.info(
        "app_started",
        extra={
            "version": __version__,
            "env": settings.APP_ENV,
            "debug": settings.APP_DEBUG,
        },
    )

    yield

    # Shutdown
    from app.services import market_data_service as _mds

    _mds.stop_tick_loop()
    # Signal the loops co-located inside the feed factory (risk enforcer,
    # pending-order poller, auto-login) to exit via their _running/_stop
    # flags BEFORE we cancel the feed task, so they unwind at a loop
    # boundary instead of being cancelled mid-sweep. Cancelling the feed
    # task below is the hard backstop. Each guarded — a missing stop fn
    # must never block shutdown.
    for _mod, _fn in (
        ("app.services.risk_enforcer", "stop_risk_enforcer"),
        ("app.services.matching_engine", "stop_pending_order_poller"),
        ("app.services.zerodha_auto_login_scheduler", "stop_zerodha_auto_login_scheduler"),
        ("app.services.tick_aggregator", "stop_tick_aggregator"),
    ):
        try:
            import importlib

            getattr(importlib.import_module(_mod), _fn)()
        except Exception:
            pass
    # The market-tick fanout + all upstream feed connections now live inside
    # the leader-gated feed factory; cancelling it triggers a clean release
    # of the `leader:feed` lock and shuts the feeds down.
    task = getattr(app, "_feed_leader_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    # Stop risk enforcer cleanly
    try:
        from app.services.risk_enforcer import stop_risk_enforcer
        stop_risk_enforcer()
        rtask = getattr(app, "_risk_enforcer_task", None)
        if rtask is not None:
            rtask.cancel()
            try:
                await rtask
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop expiry-cleanup loop cleanly
    try:
        from app.services.expiry_cleanup import stop_expiry_cleanup
        stop_expiry_cleanup()
        etask = getattr(app, "_expiry_cleanup_task", None)
        if etask is not None:
            etask.cancel()
            try:
                await etask
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop bonus-expiry loop cleanly (only ever started when flag on)
    try:
        from app.services.bonus_expiry import stop_bonus_expiry
        stop_bonus_expiry()
        btask = getattr(app, "_bonus_expiry_task", None)
        if btask is not None:
            btask.cancel()
            try:
                await btask
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop intraday→carry rollover loop cleanly
    try:
        from app.services.position_service import stop_intraday_to_carry_loop
        stop_intraday_to_carry_loop()
        itask = getattr(app, "_intraday_to_carry_task", None)
        if itask is not None:
            itask.cancel()
            try:
                await itask
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop EOD pending-order auto-cancel loop cleanly
    try:
        from app.services.eod_order_cleanup import stop_eod_order_cleanup
        stop_eod_order_cleanup()
        etask2 = getattr(app, "_eod_order_cleanup_task", None)
        if etask2 is not None:
            etask2.cancel()
            try:
                await etask2
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop tracker self-heal loop cleanly
    try:
        from app.services.position_service import stop_tracker_reconcile_loop
        stop_tracker_reconcile_loop()
        ttask = getattr(app, "_tracker_reconcile_task", None)
        if ttask is not None:
            ttask.cancel()
            try:
                await ttask
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop P&L sharing scheduler cleanly
    try:
        from app.services.pnl_sharing_service import stop_pnl_sharing_scheduler
        stop_pnl_sharing_scheduler()
        ptask = getattr(app, "_pnl_sharing_scheduler_task", None)
        if ptask is not None:
            ptask.cancel()
            try:
                await ptask
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop Divinepay reconcile loop cleanly
    try:
        dtask = getattr(app, "_divinepay_reconcile_task", None)
        if dtask is not None:
            dtask.cancel()
            try:
                await dtask
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop weekly settlement loop cleanly
    try:
        from app.services.weekly_settlement_service import stop_weekly_settlement_loop
        stop_weekly_settlement_loop()
        wtask = getattr(app, "_weekly_settlement_task", None)
        if wtask is not None:
            wtask.cancel()
            try:
                await wtask
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop pending-order poller cleanly
    try:
        from app.services.matching_engine import stop_pending_order_poller
        stop_pending_order_poller()
        ptask = getattr(app, "_pending_order_task", None)
        if ptask is not None:
            ptask.cancel()
            try:
                await ptask
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop Zerodha auto-login scheduler cleanly — guarded because the
    # task is no longer started above (Playwright auto-login disabled
    # 2026-06-01). Only cancels if a future code path re-enables it.
    try:
        ztask = getattr(app, "_zerodha_auto_login_task", None)
        if ztask is not None:
            from app.services.zerodha_auto_login_scheduler import (
                stop_zerodha_auto_login_scheduler,
            )

            stop_zerodha_auto_login_scheduler()
            ztask.cancel()
            try:
                await ztask
            except (asyncio.CancelledError, Exception):
                pass
    except Exception:
        pass

    # Stop Infoway WebSocket cleanly
    try:
        from app.services.infoway_service import infoway

        await infoway.stop()
    except Exception:
        pass

    # Stop Binance crypto feed cleanly
    try:
        from app.services.binance_service import binance

        await binance.stop()
    except Exception:
        pass

    # Stop MetaAPI feed cleanly
    try:
        from app.services.metaapi_service import metaapi

        await metaapi.stop()
    except Exception:
        pass

    # Stop the WS hubs before closing Redis so the shared pub/sub
    # connections get a chance to unsubscribe cleanly.
    try:
        from app.core.ws_hub import stop_all_hubs

        await stop_all_hubs()
    except Exception:  # pragma: no cover
        pass

    await close_redis()
    await close_database()
    logger.info("app_stopped")


app = FastAPI(
    title=settings.APP_NAME,
    version=__version__,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    openapi_url="/openapi.json" if not settings.is_production else None,
    lifespan=lifespan,
)

# ── Middleware ────────────────────────────────────────────────────────
# IMPORTANT: Starlette `add_middleware` PREPENDS to the stack — the
# LAST one registered runs FIRST on the incoming request. So everything
# below is in "innermost-first" order: CORSMiddleware/GZip/TrustedHost
# are registered first (they end up as inner layers), then the dynamic
# branding CORS middleware is registered LAST so it becomes the
# OUTERMOST layer and intercepts the OPTIONS preflight before the
# static CORSMiddleware (which only knows our own origins) can 400 it.
# Before this swap, tenant custom domains (e.g. stockcafe.live) hit
# CORSMiddleware first, got rejected without ACAO, and never reached
# the branding lookup — so every branded login page failed with a
# CORS preflight error and fell back to the platform default.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
    max_age=3600,
)
app.add_middleware(GZipMiddleware, minimum_size=1024)

if settings.is_production:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])  # tighten via env in prod


# Branding CORS middleware: lets requests from active admin
# custom_domain origins through (the regular CORSMiddleware above
# can't see DB rows, so it would 403 a request from broker_a.com
# even when broker_a.com is a legitimate, READY-status tenant).
# Cached in-process for 60 s — refreshed lazily on the first request
# after the TTL expires. Idempotent and tolerant of DB outages
# (falls back to "no extra origins" when the lookup fails).
# MUST be registered AFTER CORSMiddleware so it ends up outermost.
@app.middleware("http")
async def branding_cors_middleware(request: Request, call_next):
    origin = request.headers.get("origin")
    if not origin:
        return await call_next(request)
    # Only act when the origin is NOT already in the static allow-list.
    if origin in settings.cors_allowed_origins:
        return await call_next(request)
    if not settings.BRANDING_ENABLED:
        return await call_next(request)

    try:
        from app.services.branding_service import all_active_custom_domains
    except Exception:  # pragma: no cover
        return await call_next(request)

    # Tiny in-process cache so we don't hit Mongo on every request.
    now = asyncio.get_event_loop().time()
    cache = getattr(app.state, "_branding_cors_cache", None)
    if cache is None or (now - cache["at"]) > 60.0:
        try:
            domains = await all_active_custom_domains()
        except Exception:  # pragma: no cover
            domains = []
        # Each admin's domain is allowed via both apex and www, http+https.
        allowed_set: set[str] = set()
        for d in domains:
            allowed_set.add(f"https://{d}")
            allowed_set.add(f"https://www.{d}")
            allowed_set.add(f"http://{d}")
            allowed_set.add(f"http://www.{d}")
        cache = {"at": now, "set": allowed_set}
        app.state._branding_cors_cache = cache

    if origin not in cache["set"]:
        return await call_next(request)

    # Preflight: respond directly so we control headers and method.
    # We answer here (instead of forwarding to CORSMiddleware) because
    # CORSMiddleware only knows the static allow-list and would reject
    # this origin — we already validated it against the live DB above.
    if request.method == "OPTIONS":
        from starlette.responses import Response as _R

        resp = _R(status_code=204)
    else:
        resp = await call_next(request)
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = (
        request.headers.get("access-control-request-headers")
        or "Authorization, Content-Type, X-Request-Id, X-Admin-Api-Key"
    )
    resp.headers["Access-Control-Expose-Headers"] = "X-Request-Id"
    resp.headers["Access-Control-Max-Age"] = "3600"
    resp.headers["Vary"] = "Origin"
    return resp


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    import uuid

    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), camera=(), microphone=()"
    )
    if settings.is_production:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


# ── Exception handlers ────────────────────────────────────────────────
register_exception_handlers(app)

# ── Metrics ──────────────────────────────────────────────────────────
Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


# ── Static uploads (deposit screenshots etc., admin logos) ───────────
_uploads_dir = Path("uploads")
_uploads_dir.mkdir(parents=True, exist_ok=True)
(_uploads_dir / "logos").mkdir(parents=True, exist_ok=True)


# CORS for static files: custom-domain PWA installs fetch logo from
# api.marginwealth.live → stockcafe.live origin. Without ACAO header
# Chrome blocks the image and PWA gets the default platform icon.
@app.middleware("http")
async def uploads_cors_middleware(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/uploads/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response


app.mount("/uploads", StaticFiles(directory=str(_uploads_dir)), name="uploads")

# ── TradingView Advanced Charts (licensed library, server-hosted) ────
# Serves the mobile chart page + custom datafeed + the licensed
# charting_library/ folder (the latter is .gitignored and must be copied
# onto the server manually per TradingView's redistribution terms). The
# APK WebView loads /charting/index.html and the datafeed fetches candles
# from /api/v1/user/instruments/{token}/history. Mounted only when the
# directory is present so a deploy without the library doesn't 500 at boot.
_charting_dir = Path("charting")
if _charting_dir.exists():
    app.mount(
        "/charting",
        StaticFiles(directory=str(_charting_dir), html=True),
        name="charting",
    )

# ── Routers ──────────────────────────────────────────────────────────
app.include_router(user_router, prefix="/api/v1")
app.include_router(admin_router, prefix="/api/v1")
# Public (no-auth) branding lookups live alongside /user and /admin
# at the v1 root so the path is /api/v1/branding/by-code/...
app.include_router(branding_public.router, prefix="/api/v1")
app.include_router(webhooks_public.router, prefix="/api/v1")
app.include_router(ws_router)


# ── Health & meta ────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    return APIResponse(
        data={
            "service": settings.APP_NAME,
            "version": __version__,
            "env": settings.APP_ENV,
            "docs": "/docs",
        },
    )


@app.get("/healthz", tags=["meta"])
async def healthz():
    """Liveness probe — ZERO I/O, returns instantly.

    Unlike `/health` (which pings Mongo + Redis on every call), this does
    no dependency checks, so a load balancer / uptime monitor / k8s probe
    can poll it at high frequency WITHOUT consuming a Mongo connection per
    hit. Under load (e.g. `ab -c 200`) `/health` queued behind 200
    concurrent Mongo+Redis pings → ~850 ms; this path stays sub-ms and
    never steals a DB connection from a real trade request. Point all
    frequent/automated health checks here; use `/health` only when you
    actually want the deep dependency status.
    """
    return {"status": "ok", "version": __version__}


# Memoize the deep dependency check so a burst of `/health` calls (LB,
# uptime monitor, accidental `ab -c 200`) collapses to ONE Mongo+Redis ping
# per window instead of one-per-request. 2 s is short enough that a real
# outage still surfaces within ~2 s, but long enough that a flood can never
# starve the Mongo connection pool of slots a live trade needs.
_HEALTH_CACHE_TTL_SEC = 2.0
_health_cache: dict[str, Any] = {"at": 0.0, "db": False, "redis": False}
_health_lock = asyncio.Lock()


@app.get("/health", response_model=APIResponse[HealthResponse], tags=["meta"])
async def health():
    import time as _time

    now = _time.monotonic()
    if now - _health_cache["at"] >= _HEALTH_CACHE_TTL_SEC:
        async with _health_lock:
            # Re-check inside the lock so only the first waiter pings; the
            # rest reuse the fresh result it just wrote.
            if _time.monotonic() - _health_cache["at"] >= _HEALTH_CACHE_TTL_SEC:
                _health_cache["db"] = await db_health()
                _health_cache["redis"] = await redis_health()
                _health_cache["at"] = _time.monotonic()
    db_ok = bool(_health_cache["db"])
    redis_ok = bool(_health_cache["redis"])
    overall = "ok" if (db_ok and redis_ok) else "degraded"
    return APIResponse(
        data=HealthResponse(status=overall, version=__version__, db=db_ok, redis=redis_ok),
    )


@app.get("/health/db", tags=["meta"])
async def health_db():
    return APIResponse(data={"db": await db_health()})


@app.get("/health/deep", tags=["meta"])
async def health_deep():
    """Liveness signal that surfaces the resilience plumbing's state.

    Goes beyond the basic ``/health`` (which only pings DB+Redis) to
    include the WS hub status and the Redis publish-queue depth so an
    operator (or k8s readiness probe) can detect a worker whose
    plumbing has degraded even though the underlying datastores are
    still responding.
    """
    db_ok = await db_health()
    redis_ok = await redis_health()

    hub_status: dict = {}
    try:
        from app.core.ws_hub import (
            admin_event_hub,
            market_tick_hub,
            user_channel_hub,
        )

        for hub in (market_tick_hub, user_channel_hub, admin_event_hub):
            hub_status[hub.name] = {
                "running": hub._started,  # noqa: SLF001 — read-only diagnostic
                "subscriber_count": hub.subscriber_count(),
            }
    except Exception:  # pragma: no cover
        pass

    publish_queue: dict = {}
    try:
        from app.core import redis_client as _rc

        publish_queue = {
            "queue_size": _rc._publish_queue.qsize() if _rc._publish_queue is not None else None,  # noqa: SLF001
            "max": _rc._PUBLISH_QUEUE_MAX,  # noqa: SLF001
            "drainer_running": _rc._drain_task is not None and not _rc._drain_task.done(),  # noqa: SLF001
        }
    except Exception:  # pragma: no cover
        pass

    overall = "ok" if (db_ok and redis_ok) else "degraded"
    return APIResponse(
        data={
            "status": overall,
            "db": db_ok,
            "redis": redis_ok,
            "ws_hubs": hub_status,
            "publish_queue": publish_queue,
        }
    )


@app.get("/health/redis", tags=["meta"])
async def health_redis():
    return APIResponse(data={"redis": await redis_health()})
