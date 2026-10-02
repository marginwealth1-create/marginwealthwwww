"""Daily background loop that fires the Zerodha auto-login.

Also runs queued "Test login now" requests (`take_queued_test`), so every
login executes on this feed-leader process — never in an API worker.

Runs every 30 seconds. Fires the login when ALL of these are true:

  • Auto-login feature is enabled (admin toggle on)
  • The current IST time is inside the fire window around the
    configured `schedule_time_ist` (default ±2 min early / 50 min late
    catch-up — see `_FIRE_BEFORE_SEC` / `_FIRE_AFTER_SEC`)
  • Today's IST date is not already represented in the DB-persisted
    `last_attempt_at` (so we don't double-fire after a restart)
  • This worker wins the Redis SETNX leader lock (multi-worker safe)

If the trigger window fires, runs up to 3 retries with 5-min gaps. If
all retries fail, dispatches a high-severity Notification to every
super-admin so they get the bell-icon alert.

Bugs this rewrite addresses (chronological)
-------------------------------------------
1. 60-s tick alignment race: the old "wait_sec <= 60" check coupled
   the fire decision to the loop's tick PHASE — if a tick happened at
   07:00:30 instead of 06:59:30, the day's chance was missed entirely.
   FIX: absolute-time window around `schedule_time` (not tick-relative).
2. In-memory `last_fired_iso_date` lost on every restart, with two
   failure modes: (a) double-fire if restart happens mid-catch-up
   window, (b) silent miss if restart happens after the catch-up
   window closes.
   FIX: use the DB-persisted `last_attempt_at` field instead — set on
   every `refresh_now()` call so it's a reliable "fired today" marker.
3. Leader lock TTL of 10 min was shorter than the worst-case retry
   sequence (3 × 5 min = 15 min). Lock could expire mid-process and
   a second worker could pick up → duplicate Playwright run.
   FIX: 30-min TTL.
4. 60-s tick was unnecessarily coarse — combined with a 60-s fire
   window in the old code, you had only one shot per minute. With the
   new 30-s tick + 120-s early window, the loop has 4+ chances per
   target.
5. No DEBUG logging at idle ticks — operators couldn't tell whether
   the scheduler was alive but waiting, or genuinely stuck. We now
   emit a DEBUG-level "tick" log line that includes the next-fire
   delta so `journalctl ... | grep zerodha_scheduler` shows liveness.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core.redis_client import get_redis
from app.models.notification import (
    Notification,
    NotificationLevel,
    NotificationType,
)
from app.models.user import User, UserRole
from app.models.zerodha_auto_login import ZerodhaAutoLogin
from app.services.zerodha_auto_login import zerodha_auto_login

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

# Cross-worker leader lock. 30 min covers the worst-case retry sequence
# (3 × 5 min gap + ~30 s each Playwright run). MUST be longer than
# `_MAX_RETRIES * _RETRY_GAP_SEC` plus a safety buffer, otherwise the
# lock can expire mid-retry and a second worker could grab it and start
# its own Playwright run on a token that's already mid-refresh.
_LEADER_KEY = "zerodha_auto_login:scheduler_leader"
_LEADER_TTL_SEC = 60 * 30

# Retry policy: 3 attempts × 5 min gap = ~15 min from first failure to
# last attempt — still inside the 07:00 → 08:00 IST Kite-expiry window
# so the day's token gets refreshed before it's invalidated.
_MAX_RETRIES = 3
_RETRY_GAP_SEC = 300

# Tick cadence — wake every 30 s. Finer than the historical 60 s so
# the fire window has at least 4 tick chances even at the tighter
# 2-min early gate.
_TICK_SEC = 30

# Absolute-time fire window around the configured schedule_time.
#
# `_FIRE_BEFORE_SEC` — how many seconds EARLY we'll accept (covers
#                      tick drift). 120 s gives 4+ tick chances at the
#                      30-s tick cadence even if ticks landed at
#                      unfavourable phases.
# `_FIRE_AFTER_SEC`  — how many seconds LATE we'll accept (catch-up
#                      window). 50 min covers "backend was restarted
#                      just after 07:00" / "loop stalled briefly"
#                      while still refreshing the token BEFORE the
#                      08:00 IST Kite-side rotation. After 50 min the
#                      catch-up closes — by then the token has likely
#                      already rotated.
_FIRE_BEFORE_SEC = 120
_FIRE_AFTER_SEC = 50 * 60

# Operator-visible "scheduler is alive" log line cadence. We log at
# DEBUG every tick, and at INFO every Nth tick so default-INFO setups
# still have proof-of-life in the journal.
_LIVENESS_LOG_EVERY_N_TICKS = 20  # ≈ every 10 minutes at 30-s tick

# Layer-2 verification settings. After refresh_now() returns success,
# we don't immediately trust the WebSocket is alive — the token can
# be valid but the ticker handshake may still be in flight, or may
# have hit a transient 403/network issue. So we POLL the ws-pool
# status for `_VERIFY_TIMEOUT_SEC` and treat it as failure if no
# ticker reaches the CONNECTED state in that window.
#
# On verification failure we run a Layer-2 recovery: an explicit
# `zerodha.connect_ws(force=True)` call. This is much lighter than
# another Playwright run — it just re-spawns the KiteTicker with the
# existing fresh token. If even that fails, we fall back to a full
# Layer-3 refresh_now() (new token + new WS spawn).
_VERIFY_TIMEOUT_SEC = 60
_VERIFY_POLL_INTERVAL_SEC = 2

_stop_flag = False


def stop_zerodha_auto_login_scheduler() -> None:
    global _stop_flag
    _stop_flag = True


async def _try_acquire_leader_lock() -> bool:
    try:
        redis = get_redis()
        return bool(
            await redis.set(_LEADER_KEY, "1", ex=_LEADER_TTL_SEC, nx=True)
        )
    except Exception:
        logger.warning("zerodha_scheduler_redis_down_running_anyway")
        # Single-worker dev — without Redis the lock can't be
        # coordinated, so just proceed (worst case: dev hits Kite twice,
        # which Kite tolerates fine since it just refreshes the token
        # twice and the second succeeds).
        return True


async def _release_leader_lock() -> None:
    """Best-effort release of the leader lock after a fire completes.
    If this fails the TTL will reclaim the key anyway."""
    try:
        await get_redis().delete(_LEADER_KEY)
    except Exception:
        pass


async def _verify_ws_connected(timeout_sec: int = _VERIFY_TIMEOUT_SEC) -> bool:
    """Poll the KiteTicker pool to confirm at least one ticker reaches
    CONNECTED state within ``timeout_sec`` seconds.

    Why this exists
    ---------------
    `refresh_now()` returns `success=True` once it successfully exchanges
    the request_token for an access_token via Kite REST. That guarantees
    the TOKEN is valid — but the KiteTicker WebSocket handshake happens
    separately (kicked off by `_post_login_ws_kickoff`) and can still
    fail with 403 / 1006 / network errors. Without this verification a
    "success" from refresh_now would falsely imply live ticks are
    flowing, but the operator would see a Disconnected status panel.

    Returns
    -------
    True if Account A's socket reaches `connected=True` before the timeout.
    False if the entire window elapses with the pool stuck in
    CONNECTING / DISCONNECTED / ERROR.
    """
    from app.services.zerodha_service import zerodha

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_sec
    while loop.time() < deadline:
        await asyncio.sleep(_VERIFY_POLL_INTERVAL_SEC)
        try:
            # Account A's OWN socket — a healthy Account B must not mask a
            # dead A now that logging in no longer tears the whole pool down.
            api_key = (await zerodha._get_settings(0)).apiKey
            with zerodha._ticker_lock:
                if any(
                    e.get("api_key") == api_key and e.get("connected")
                    for e in zerodha._tickers
                ):
                    return True
        except Exception:
            logger.warning(
                "zerodha_scheduler_verify_pool_info_failed",
                exc_info=True,
            )
    return False


async def _layer2_recovery_reconnect() -> bool:
    """Layer-2 recovery — explicit WS reconnect using the already-fresh
    token. Lighter than a full Playwright re-login (~5 ms vs ~30 s)
    because it skips the OAuth dance and just re-spawns the KiteTicker.

    Used when Layer-1 (refresh_now → token saved → ticker kickoff) ran
    successfully but the kickoff's ticker never reached CONNECTED state
    inside `_VERIFY_TIMEOUT_SEC`. Common causes: transient Kite WS
    server hiccup, IP momentarily throttled, prior zombie connection
    still occupying the single-WS-per-token slot.

    Returns True if a connected ticker exists after this attempt.
    """
    from app.services.zerodha_service import zerodha

    logger.info("zerodha_scheduler_layer2_reconnect_attempt")
    try:
        # `force=True` first tears down any existing tickers and waits
        # a few seconds for Kite to release the slot, then re-spawns.
        await zerodha.connect_ws(force=True)
    except Exception:
        logger.exception("zerodha_scheduler_layer2_reconnect_call_failed")
        return False
    # Give the new handshake a window to complete.
    return await _verify_ws_connected(timeout_sec=_VERIFY_TIMEOUT_SEC)


async def _alert_super_admins(error_summary: str) -> None:
    """Push a high-severity notification to every super-admin."""
    try:
        admins = await User.find(User.role == UserRole.SUPER_ADMIN).to_list()
        if not admins:
            return
        title = "Zerodha auto-login failed"
        body = (
            "All retries exhausted at the scheduled run. Please complete "
            "the manual login on /zerodha before the 09:15 IST market open. "
            f"Last error: {error_summary[:200]}"
        )
        for admin in admins:
            try:
                await Notification(
                    user_id=admin.id,
                    type=NotificationType.SYSTEM,
                    level=NotificationLevel.DANGER,
                    title=title,
                    message=body,
                    data={"source": "zerodha_auto_login"},
                ).insert()
            except Exception:
                logger.exception("zerodha_scheduler_notif_insert_failed")
    except Exception:
        logger.exception("zerodha_scheduler_alert_admins_failed")


def _parse_hhmm(schedule_time_ist: str) -> tuple[int, int]:
    try:
        hh, mm = schedule_time_ist.split(":")
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("out of range")
        return h, m
    except Exception:
        logger.warning(
            "zerodha_scheduler_invalid_schedule_format_falling_back",
            extra={"got": schedule_time_ist},
        )
        return 7, 0


async def _already_fired_today(today_ist_date: str, account_index: int = 0) -> bool:
    """Did the SCHEDULER already fire today for this account?

    Only counts scheduler-initiated attempts (last_attempt_source == "scheduler").
    Manual "Test login now" clicks must NOT block the daily scheduler.
    """
    try:
        doc = await ZerodhaAutoLogin.find_one(
            ZerodhaAutoLogin.account_index == account_index
        )
        if doc is None or doc.last_attempt_at is None:
            return False
        if getattr(doc, "last_attempt_source", "") != "scheduler":
            return False
        last_attempt_ist = doc.last_attempt_at.astimezone(IST).date()
        return last_attempt_ist.isoformat() == today_ist_date
    except Exception:
        logger.exception("zerodha_scheduler_last_attempt_lookup_failed")
        return False


async def _fire_account(account_index: int, delta_sec: float, schedule_time: str) -> None:
    """Run the 3-layer login + WS verification flow for one Zerodha account."""
    acct_label = f"account_{account_index}"

    if not await _try_acquire_leader_lock():
        logger.info("zerodha_scheduler_other_worker_won", extra={"account": acct_label})
        return

    try:
        if delta_sec > 60:
            logger.info(
                "zerodha_scheduler_catchup_fire",
                extra={"delta_sec": round(delta_sec, 1), "schedule": schedule_time, "account": acct_label},
            )
        else:
            logger.info(
                "zerodha_scheduler_on_time_fire",
                extra={"delta_sec": round(delta_sec, 1), "schedule": schedule_time, "account": acct_label},
            )

        # ── LAYER 1: Playwright login + token refresh ─────────
        last_error = ""
        layer1_success = False
        for attempt in range(1, _MAX_RETRIES + 1):
            logger.info(
                "zerodha_scheduler_layer1_attempt",
                extra={"attempt": attempt, "max": _MAX_RETRIES, "schedule": schedule_time, "account": acct_label},
            )
            result = await zerodha_auto_login.refresh_now(
                account_index=account_index,
                triggered_by=f"scheduler_layer1_attempt_{attempt}",
            )
            if result.get("success"):
                logger.info(
                    "zerodha_scheduler_layer1_success",
                    extra={"attempt": attempt, "duration_ms": result.get("duration_ms"), "account": acct_label},
                )
                last_error = ""
                layer1_success = True
                break
            last_error = f"{result.get('stage')}: {result.get('error')}"
            logger.warning(
                "zerodha_scheduler_layer1_attempt_failed",
                extra={"attempt": attempt, "error": last_error, "account": acct_label},
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_GAP_SEC)

        if not layer1_success:
            await _alert_super_admins(f"[Account {'B' if account_index == 1 else 'A'}] {last_error}")
            return

        # ── VERIFY: did the WS actually connect? ──────────────
        # Only verify WS for account 0 (primary). Account B's WS
        # starts independently and is managed by its own ticker pool.
        if account_index == 0:
            logger.info(
                "zerodha_scheduler_verify_start",
                extra={"timeout_sec": _VERIFY_TIMEOUT_SEC},
            )
            ws_connected = await _verify_ws_connected()
            if ws_connected:
                logger.info("zerodha_scheduler_verify_ok_ws_connected")
                return

            # ── LAYER 2: WS reconnect with current token ──────────
            logger.warning("zerodha_scheduler_layer1_token_ok_but_ws_dead_running_layer2")
            layer2_ok = await _layer2_recovery_reconnect()
            if layer2_ok:
                logger.info("zerodha_scheduler_layer2_recovery_success")
                return

            # ── LAYER 3 (fallback): one more full refresh ─────────
            logger.warning("zerodha_scheduler_layer2_failed_running_layer3_full_refresh")
            fallback_result = await zerodha_auto_login.refresh_now(
                account_index=account_index,
                triggered_by="scheduler_layer3_fallback",
            )
            if not fallback_result.get("success"):
                await _alert_super_admins(
                    f"Layer-3 refresh failed: "
                    f"{fallback_result.get('stage')}: {fallback_result.get('error')}"
                )
                return

            final_connected = await _verify_ws_connected()
            if final_connected:
                logger.info("zerodha_scheduler_layer3_recovery_success")
            else:
                logger.error("zerodha_scheduler_all_layers_failed")
                await _alert_super_admins(
                    "Token refreshed across all retry layers but the "
                    "KiteTicker WebSocket never reached CONNECTED — "
                    "manual investigation needed."
                )
    finally:
        await _release_leader_lock()


async def zerodha_auto_login_loop() -> None:
    """Long-running coroutine launched from the FastAPI lifespan."""
    logger.info(
        "zerodha_auto_login_scheduler_started",
        extra={
            "tick_sec": _TICK_SEC,
            "fire_before_sec": _FIRE_BEFORE_SEC,
            "fire_after_sec": _FIRE_AFTER_SEC,
            "leader_ttl_sec": _LEADER_TTL_SEC,
        },
    )
    tick_count = 0
    while not _stop_flag:
        try:
            await asyncio.sleep(_TICK_SEC)
            if _stop_flag:
                break
            tick_count += 1

            now_ist = datetime.now(IST)
            today_str = now_ist.date().isoformat()

            any_enabled = False
            # Check both accounts independently
            for account_index in [0, 1]:
                # "Test login now" from the admin API — runs here, on the
                # process that owns the WS pool, even if the daily schedule
                # is disabled (so the setup can be tested before enabling).
                actor = await zerodha_auto_login.take_queued_test(account_index)
                if actor is not None:
                    result = await zerodha_auto_login.refresh_now(
                        account_index=account_index,
                        actor_id=actor or None,
                        triggered_by="manual_test",
                    )
                    logger.info(
                        "zerodha_scheduler_test_login_result",
                        extra={
                            "account": account_index,
                            "success": result.get("success"),
                            "stage": result.get("stage"),
                            "error": (result.get("error") or "")[:200],
                        },
                    )

                if not await zerodha_auto_login.is_enabled(account_index):
                    continue
                any_enabled = True

                schedule_time = await zerodha_auto_login.schedule_time(account_index)
                target_h, target_m = _parse_hhmm(schedule_time)
                target_today = now_ist.replace(
                    hour=target_h, minute=target_m, second=0, microsecond=0
                )
                delta_sec = (now_ist - target_today).total_seconds()

                in_early_window = -_FIRE_BEFORE_SEC <= delta_sec < 0
                in_catchup_window = 0 <= delta_sec <= _FIRE_AFTER_SEC

                if not (in_early_window or in_catchup_window):
                    continue

                if await _already_fired_today(today_str, account_index):
                    continue

                # Fire this account's login
                await _fire_account(account_index, delta_sec, schedule_time)

            if not any_enabled and tick_count % _LIVENESS_LOG_EVERY_N_TICKS == 0:
                logger.info("zerodha_scheduler_tick_disabled")
            elif any_enabled and tick_count % _LIVENESS_LOG_EVERY_N_TICKS == 0:
                logger.info("zerodha_scheduler_tick", extra={"tick": tick_count})

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("zerodha_scheduler_iteration_crash")
            await asyncio.sleep(_TICK_SEC)

    logger.info("zerodha_auto_login_scheduler_stopped")
