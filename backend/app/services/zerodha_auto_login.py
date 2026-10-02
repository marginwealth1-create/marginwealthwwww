"""Automated daily Kite Connect access-token refresh.

Drives the Kite OAuth + TOTP login screen with a headless Playwright
browser and lets Kite redirect it to our own
`/api/v1/admin/zerodha/callback`. That endpoint does the ONLY exchange of
the single-use `request_token` (`zerodha_service.generate_session`), exactly
like a manual login — so auto-login and manual login share one code path.

Ported from the design Stock4X runs in production. The earlier version here
aborted the redirect with `page.route()` and exchanged the token itself, but
Playwright never routes redirect hops, so the browser still reached /callback
and the two exchanges raced: one side failed with "Token is invalid or has
expired".

Where it runs
-------------
Only on the feed-leader process (the one owning the Kite WS pool): the daily
scheduler lives there, and the admin "Test login now" button just queues a
Redis flag that the scheduler claims (`queue_test` / `take_queued_test`).
/callback may land on any HTTP worker; `generate_session` hands the WS
reconnect to the leader, scoped to the account that logged in (A or B).

Success is confirmed by THIS account's `lastConnected` being stamped after
the run started — not by the token value, which Kite repeats on a same-day
re-login.

Cross-worker safety
-------------------
A Redis SETNX lock per account guards `refresh_now()`. See `_REFRESH_LOCK_KEY`.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any
from urllib.parse import unquote

from beanie import PydanticObjectId

from app.models.audit_log import AuditAction
from app.models.zerodha_auto_login import ZerodhaAutoLogin
from app.services import audit_service
from app.utils.crypto import CryptoError, decrypt, encrypt, mask_secret
from app.utils.time_utils import now_utc

logger = logging.getLogger(__name__)

# Generous for slow EC2 networks, short enough that the scheduler's 3
# retries with 5-min gaps still finish well before the 09:15 IST open.
_NAV_TIMEOUT_MS = 25_000

# The browser requesting this path is how we know Kite issued a
# request_token. Substring match on the request event — `page.route` never
# sees redirect hops, and a `**…/callback*` glob doesn't match `https://`.
_CALLBACK_PATH = "/api/v1/admin/zerodha/callback"

# Kite's 2FA step reuses input#userid (type=number); the rest cover UI drift.
_TOTP_SELECTORS = (
    "input#userid",
    "input[type='number']",
    "input#totp",
    "input#pin",
    "input[maxlength='6']",
)

# Cross-worker single-flight guard for refresh_now(). 5 minutes is the
# operator-recovery upper bound — long enough to survive a slow Kite login,
# short enough that a crashed worker doesn't block the next attempt forever.
_REFRESH_LOCK_KEY = "zerodha_auto_login:refresh_lock"
_REFRESH_LOCK_TTL_SEC = 300

# "Test login now" queue: the API sets it, the feed leader's scheduler claims
# it. The TTL drops a request that no live leader picks up.
_RUN_NOW_KEY = "zerodha_auto_login:run_now"
_RUN_NOW_TTL_SEC = 180


class AutoLoginError(RuntimeError):
    """Stage-tagged failure. `.stage` identifies which Playwright step blew up."""

    def __init__(self, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


class ZerodhaAutoLoginService:
    """Singleton service — account_index selects which Zerodha account (0=A, 1=B)."""

    # ── Per-account row helpers ────────────────────────────────────
    async def _get_or_create(self, account_index: int = 0) -> ZerodhaAutoLogin:
        existing = await ZerodhaAutoLogin.find_one(
            ZerodhaAutoLogin.account_index == account_index
        )
        if existing:
            return existing
        if account_index == 0:
            # Legacy document (before dual-account) has no account_index field.
            legacy = await ZerodhaAutoLogin.find_one()
            if legacy is not None:
                legacy.account_index = 0
                await legacy.save()
                return legacy
        doc = ZerodhaAutoLogin(account_index=account_index)
        await doc.insert()
        return doc

    # ── Credentials management ─────────────────────────────────────
    async def save_credentials(
        self,
        *,
        account_index: int = 0,
        username: str,
        password: str,
        totp_secret: str,
        actor_id: PydanticObjectId | str | None,
        ip_address: str | None = None,
    ) -> None:
        username = (username or "").strip()
        password = password or ""
        totp_secret = (totp_secret or "").strip().replace(" ", "").upper()

        if not username or not password or not totp_secret:
            raise ValueError("username, password, totp_secret all required")

        # Fail loudly at save-time on a bad TOTP secret so we don't
        # discover it at 07:00 the next morning when the scheduler fires.
        try:
            import pyotp
            pyotp.TOTP(totp_secret).now()
        except Exception as exc:
            raise ValueError(f"totp_secret is not valid base32: {exc}") from exc

        ct_user, iv_user = encrypt(username)
        ct_pwd, iv_pwd = encrypt(password)
        ct_totp, iv_totp = encrypt(totp_secret)

        doc = await self._get_or_create(account_index)
        had_creds = bool(doc.encrypted_username)
        doc.encrypted_username = ct_user
        doc.encrypted_username_iv = iv_user
        doc.encrypted_password = ct_pwd
        doc.encrypted_password_iv = iv_pwd
        doc.encrypted_totp_secret = ct_totp
        doc.encrypted_totp_secret_iv = iv_totp
        # Reset failure counter — fresh creds get a clean slate.
        doc.consecutive_failures = 0
        doc.last_error_detail = None
        await doc.save()

        await audit_service.log_event(
            action=AuditAction.SETTING_CHANGE,
            entity_type="ZerodhaAutoLogin",
            entity_id=str(doc.id),
            actor_id=actor_id,
            metadata={
                "operation": "credentials_updated",
                "previously_configured": had_creds,
                "username_masked": mask_secret(username),
            },
            ip_address=ip_address,
        )

    async def force_reset_lock(self, account_index: int = 0) -> None:
        """Clear a stuck Redis lock + reset in_progress DB state to failed.
        Called by the admin reset-lock endpoint when a Playwright run crashed
        mid-execution and left the lock held."""
        from app.core.redis_client import get_redis

        await get_redis().delete(f"{_REFRESH_LOCK_KEY}:{account_index}")
        doc = await ZerodhaAutoLogin.find_one(
            ZerodhaAutoLogin.account_index == account_index
        )
        if doc and doc.last_stage == "in_progress":
            doc.last_stage = "failed"
            doc.last_status = "failed"
            doc.last_error_detail = "Manually reset by admin (lock was stuck)"
            await doc.save()

    # ── "Test login now" hand-off to the feed leader ───────────────
    async def queue_test(
        self, account_index: int = 0, *, actor_id: PydanticObjectId | str | None
    ) -> None:
        """Ask the feed leader to run one login for this account. Playwright
        must not run in an API worker: the WS pool isn't there."""
        from app.core.redis_client import get_redis

        await get_redis().set(
            f"{_RUN_NOW_KEY}:{account_index}",
            str(actor_id or ""),
            ex=_RUN_NOW_TTL_SEC,
        )

    async def take_queued_test(self, account_index: int = 0) -> str | None:
        """Claim a queued test run. Returns the requesting admin id ("" if
        unknown), or None when nothing is queued. DEL is the claim, so only
        one process wins even if several poll the same flag."""
        from app.core.redis_client import get_redis

        key = f"{_RUN_NOW_KEY}:{account_index}"
        try:
            redis = get_redis()
            actor = await redis.get(key)
            if actor is None or not await redis.delete(key):
                return None
        except Exception:
            # Polled every scheduler tick — a Redis blip must not stop the
            # daily schedule from being evaluated.
            logger.warning("zerodha_auto_login_run_now_poll_failed", exc_info=True)
            return None
        return actor.decode() if isinstance(actor, bytes) else str(actor)

    async def get_status(self, account_index: int = 0) -> dict[str, Any]:
        """Masked snapshot for the admin UI. Never returns raw creds."""
        doc = await ZerodhaAutoLogin.find_one(
            ZerodhaAutoLogin.account_index == account_index
        )
        if doc is None:
            return {
                "is_configured": False,
                "is_enabled": False,
                "schedule_time_ist": "07:00",
                "last_attempt_at": None,
                "last_success_at": None,
                "last_status": "",
                "last_error_detail": None,
                "last_stage": None,
                "consecutive_failures": 0,
                "last_duration_ms": None,
                "username_masked": "",
            }

        username_masked = ""
        if doc.encrypted_username:
            try:
                username_masked = mask_secret(
                    decrypt(doc.encrypted_username, doc.encrypted_username_iv),
                )
            except CryptoError:
                username_masked = "(unreadable — key rotated?)"

        return {
            "is_configured": bool(
                doc.encrypted_username
                and doc.encrypted_password
                and doc.encrypted_totp_secret
            ),
            "is_enabled": doc.is_enabled,
            "schedule_time_ist": doc.schedule_time_ist,
            "last_attempt_at": doc.last_attempt_at,
            "last_success_at": doc.last_success_at,
            "last_status": doc.last_status,
            "last_error_detail": doc.last_error_detail,
            "last_stage": doc.last_stage,
            "consecutive_failures": doc.consecutive_failures,
            "last_duration_ms": doc.last_duration_ms,
            "username_masked": username_masked,
        }

    async def set_enabled(
        self,
        enabled: bool,
        *,
        account_index: int = 0,
        actor_id: PydanticObjectId | str | None,
        ip_address: str | None = None,
    ) -> None:
        doc = await self._get_or_create(account_index)
        if enabled and not (
            doc.encrypted_username
            and doc.encrypted_password
            and doc.encrypted_totp_secret
        ):
            raise ValueError("Cannot enable until credentials are saved.")
        was = doc.is_enabled
        doc.is_enabled = bool(enabled)
        await doc.save()
        if was != doc.is_enabled:
            await audit_service.log_event(
                action=AuditAction.SETTING_CHANGE,
                entity_type="ZerodhaAutoLogin",
                entity_id=str(doc.id),
                actor_id=actor_id,
                metadata={
                    "operation": "scheduler_toggled",
                    "enabled": doc.is_enabled,
                },
                ip_address=ip_address,
            )

    async def set_schedule(
        self,
        schedule_time_ist: str,
        *,
        account_index: int = 0,
        actor_id: PydanticObjectId | str | None,
        ip_address: str | None = None,
    ) -> None:
        s = (schedule_time_ist or "").strip()
        try:
            hh, mm = s.split(":")
            h, m = int(hh), int(mm)
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError("out of range")
        except Exception as exc:
            raise ValueError(
                f"schedule_time_ist must be HH:MM IST 24-hour (got {s!r}): {exc}"
            )
        normalised = f"{h:02d}:{m:02d}"
        doc = await self._get_or_create(account_index)
        prev = doc.schedule_time_ist
        doc.schedule_time_ist = normalised
        await doc.save()
        if prev != normalised:
            await audit_service.log_event(
                action=AuditAction.SETTING_CHANGE,
                entity_type="ZerodhaAutoLogin",
                entity_id=str(doc.id),
                actor_id=actor_id,
                metadata={
                    "operation": "schedule_updated",
                    "from": prev,
                    "to": normalised,
                },
                ip_address=ip_address,
            )

    # ── The actual login flow ──────────────────────────────────────
    async def refresh_now(
        self,
        *,
        account_index: int = 0,
        actor_id: PydanticObjectId | str | None = None,
        ip_address: str | None = None,
        triggered_by: str = "manual",
    ) -> dict[str, Any]:
        from app.core.redis_client import get_redis

        lock_key = f"{_REFRESH_LOCK_KEY}:{account_index}"

        # Single-flight guard — multi-worker safe.
        lock_acquired = False
        try:
            redis = get_redis()
            lock_acquired = bool(
                await redis.set(
                    lock_key, "1", ex=_REFRESH_LOCK_TTL_SEC, nx=True
                )
            )
            if not lock_acquired:
                return {
                    "success": False,
                    "error": "Another auto-login is already in progress.",
                    "stage": "lock",
                }
        except Exception:
            logger.warning(
                "zerodha_auto_login_lock_unavailable_continuing",
                exc_info=True,
            )

        doc = await self._get_or_create(account_index)

        # Stamp `last_attempt_at` as the FIRST thing we do — before any
        # precheck / decrypt — so the scheduler can rely on it as a
        # "fired today" marker that survives crashes and is set even on
        # early-bail-out paths (missing credentials, decrypt failure,
        # bad config). Without this stamp, an early-bail-out would leave
        # `last_attempt_at` stuck at yesterday's value and the scheduler
        # would re-fire on every 60-s tick all day long.
        doc.last_attempt_at = now_utc()
        doc.last_stage = "in_progress"
        doc.last_attempt_source = "scheduler" if "scheduler" in triggered_by else "manual"
        await doc.save()

        if not (
            doc.encrypted_username
            and doc.encrypted_password
            and doc.encrypted_totp_secret
        ):
            await self._record_failure(
                doc,
                stage="precheck",
                error="Credentials not configured.",
                triggered_by=triggered_by,
                actor_id=actor_id,
            )
            await self._release_lock(lock_acquired, account_index)
            return {
                "success": False,
                "error": "Credentials not configured.",
                "stage": "precheck",
            }

        try:
            username = decrypt(doc.encrypted_username, doc.encrypted_username_iv)
            password = decrypt(doc.encrypted_password, doc.encrypted_password_iv)
            totp_secret = decrypt(
                doc.encrypted_totp_secret, doc.encrypted_totp_secret_iv
            )
        except CryptoError as exc:
            await self._record_failure(
                doc,
                stage="decrypt",
                error=str(exc),
                triggered_by=triggered_by,
                actor_id=actor_id,
            )
            await self._release_lock(lock_acquired, account_index)
            return {"success": False, "error": str(exc), "stage": "decrypt"}

        # Keep self-heal quiet for the run: its token probe could read the
        # expired token mid-login and clear the fresh one /callback just saved.
        # The sockets are left alone — tearing the pool down here used to kill
        # the OTHER account's feed too.
        from app.services.zerodha_service import zerodha

        prior_heal_state = getattr(zerodha, "_self_heal_paused", False)
        zerodha._self_heal_paused = True
        try:
            start = time.monotonic()

            try:
                access_token = await self._run_login_flow(
                    username=username,
                    password=password,
                    totp_secret=totp_secret,
                    account_index=account_index,
                )
            except AutoLoginError as exc:
                duration_ms = int((time.monotonic() - start) * 1000)
                await self._record_failure(
                    doc,
                    stage=exc.stage,
                    error=str(exc),
                    duration_ms=duration_ms,
                    triggered_by=triggered_by,
                    actor_id=actor_id,
                )
                return {
                    "success": False,
                    "error": str(exc),
                    "stage": exc.stage,
                    "duration_ms": duration_ms,
                }
            except Exception as exc:
                duration_ms = int((time.monotonic() - start) * 1000)
                logger.exception("zerodha_auto_login_unexpected_error")
                await self._record_failure(
                    doc,
                    stage="unknown",
                    error=f"{type(exc).__name__}: {exc}",
                    duration_ms=duration_ms,
                    triggered_by=triggered_by,
                    actor_id=actor_id,
                )
                return {
                    "success": False,
                    "error": f"Unexpected error: {exc}",
                    "stage": "unknown",
                    "duration_ms": duration_ms,
                }

            duration_ms = int((time.monotonic() - start) * 1000)
            doc.last_success_at = now_utc()
            doc.last_status = "success"
            doc.last_error_detail = None
            doc.last_stage = "complete"
            doc.consecutive_failures = 0
            doc.last_duration_ms = duration_ms
            await doc.save()
            logger.info(
                "zerodha_auto_login_success",
                extra={"account": account_index, "triggered_by": triggered_by},
            )

            await audit_service.log_event(
                action=AuditAction.SETTING_CHANGE,
                entity_type="ZerodhaAutoLogin",
                entity_id=str(doc.id),
                actor_id=actor_id,
                metadata={
                    "operation": "auto_login_success",
                    "triggered_by": triggered_by,
                    "duration_ms": duration_ms,
                    "access_token_present": bool(access_token),
                },
                ip_address=ip_address,
            )

            return {
                "success": True,
                "access_token_obtained": bool(access_token),
                "duration_ms": duration_ms,
                "stage": "complete",
            }
        finally:
            # Always re-arm self-heal so the loop can recover even on
            # partial failures. We never restore the prior "paused"
            # state if it was True — admin's explicit disconnect intent
            # is overridden by an explicit auto-login attempt (the
            # admin who pressed Disconnect would not be running the
            # auto-login simultaneously).
            zerodha._self_heal_paused = False
            if prior_heal_state:
                logger.info(
                    "zerodha_auto_login_self_heal_rearmed",
                    extra={"prior_state": prior_heal_state},
                )
            await self._release_lock(lock_acquired, account_index)

    async def _release_lock(self, acquired: bool, account_index: int = 0) -> None:
        if not acquired:
            return
        try:
            from app.core.redis_client import get_redis

            await get_redis().delete(f"{_REFRESH_LOCK_KEY}:{account_index}")
        except Exception:
            pass

    async def _record_failure(
        self,
        doc: ZerodhaAutoLogin,
        *,
        stage: str,
        error: str,
        triggered_by: str,
        actor_id: PydanticObjectId | str | None,
        duration_ms: int | None = None,
    ) -> None:
        doc.last_status = "failed"
        doc.last_error_detail = f"[{stage}] {error}"[:500]
        doc.last_stage = stage
        doc.consecutive_failures += 1
        if duration_ms is not None:
            doc.last_duration_ms = duration_ms
        await doc.save()
        logger.warning(
            "zerodha_auto_login_failed",
            extra={
                "stage": stage,
                "consecutive_failures": doc.consecutive_failures,
                "triggered_by": triggered_by,
            },
        )
        await audit_service.log_event(
            action=AuditAction.SETTING_CHANGE,
            entity_type="ZerodhaAutoLogin",
            entity_id=str(doc.id),
            actor_id=actor_id,
            metadata={
                "operation": "auto_login_failed",
                "triggered_by": triggered_by,
                "stage": stage,
                "consecutive_failures": doc.consecutive_failures,
                "error": error[:500],
            },
        )

    # ── The Playwright flow ────────────────────────────────────────
    async def _run_login_flow(
        self,
        *,
        username: str,
        password: str,
        totp_secret: str,
        account_index: int = 0,
    ) -> str:
        """Headless drive of the Kite OAuth screen; returns the access token
        /callback saved for this account.

        Stages (``AutoLoginError.stage``, shown in the admin UI):
            precheck  — Kite API key missing for this account
            import    — playwright/pyotp/chromium not installed
            navigate  — Kite login URL did not load
            userid    — username/password form not interactive
            totp_page — no 2FA input found
            redirect  — Kite never sent the browser to /callback (wrong
                        password/TOTP, or the Kite app's redirect URL isn't
                        this backend's /callback)
            session   — /callback rejected the token, or saved no fresh
                        session for this account
        """
        from app.services.zerodha_service import _ensure_aware_utc, zerodha

        try:
            login_url = await zerodha.get_login_url(account_index)
        except RuntimeError as exc:
            raise AutoLoginError(
                f"{exc} — set it in the Zerodha settings page first.",
                stage="precheck",
            ) from exc

        try:
            import pyotp
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise AutoLoginError(
                "playwright or pyotp not installed — run "
                "`pip install playwright pyotp` and "
                "`playwright install chromium` on the backend host.",
                stage="import",
            ) from exc

        totp = pyotp.TOTP(totp_secret)

        # Stealth is best-effort (playwright-stealth 2.x API). Kite's OAuth
        # screen works with plain headless Chromium, so a version mismatch
        # must never abort the login.
        try:
            from playwright_stealth import Stealth

            pw_ctx = Stealth().use_async(async_playwright())
        except Exception:
            pw_ctx = async_playwright()

        run_start = now_utc()
        seen: dict[str, str] = {}

        def _on_request(request: Any) -> None:
            # Fires for redirect hops too, unlike page.route. After the
            # exchange /callback bounces the browser to
            # <admin>/zerodha?success=true or ?error=<why>.
            url = request.url
            if _CALLBACK_PATH in url:
                seen["callback"] = "1"
            elif "callback" in seen and "/zerodha?" in url:
                m = re.search(r"[?&]error=([^&]+)", url)
                if m:
                    seen["error"] = unquote(m.group(1))[:200]
                elif "success=true" in url:
                    seen["done"] = "1"

        kite_text = ""
        async with pw_ctx as p:
            try:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
            except Exception as exc:
                raise AutoLoginError(
                    f"chromium failed to launch — has "
                    f"`playwright install chromium` been run on this host? "
                    f"({type(exc).__name__}: {exc})",
                    stage="import",
                ) from exc

            try:
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                )
                page = await context.new_page()
                page.on("request", _on_request)

                try:
                    await page.goto(
                        login_url,
                        wait_until="domcontentloaded",
                        timeout=_NAV_TIMEOUT_MS,
                    )
                except Exception as exc:
                    raise AutoLoginError(
                        f"Kite login URL did not load: {exc}", stage="navigate"
                    ) from exc

                try:
                    await page.fill("input#userid", username, timeout=_NAV_TIMEOUT_MS)
                    await page.fill("input#password", password, timeout=_NAV_TIMEOUT_MS)
                    await page.click("button[type='submit']", timeout=_NAV_TIMEOUT_MS)
                except Exception as exc:
                    raise AutoLoginError(
                        f"username/password page not interactive: {exc}",
                        stage="userid",
                    ) from exc

                # Let the 2FA form swap in, then compute the code at the last
                # moment so it can't roll over mid-login.
                await page.wait_for_timeout(1800)
                code = totp.now()
                for sel in _TOTP_SELECTORS:
                    try:
                        el = page.locator(sel).first
                        await el.wait_for(state="visible", timeout=4000)
                        await el.fill(code, timeout=3000)
                        break
                    except Exception:
                        continue
                else:
                    raise AutoLoginError(
                        "TOTP input not found on the 2FA page (Kite UI changed?)",
                        stage="totp_page",
                    )
                try:
                    # Some Kite variants auto-submit on the 6th digit.
                    await page.click("button[type='submit']", timeout=3000)
                except Exception:
                    pass

                # Wait for the browser to request /callback, then for its
                # success/error bounce. Closing mid-redirect would stop the
                # exchange from ever running.
                for _ in range(40):  # ~20 s
                    if "callback" in seen:
                        break
                    await page.wait_for_timeout(500)
                if "callback" in seen:
                    for _ in range(20):  # ~10 s
                        if "done" in seen or "error" in seen:
                            break
                        await page.wait_for_timeout(500)
                else:
                    # Still on Kite — a wrong password / TOTP shows its text here.
                    try:
                        kite_text = " ".join((await page.inner_text("body")).split())[:200]
                    except Exception:
                        pass
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        if "callback" not in seen:
            raise AutoLoginError(
                f"Kite never redirected to /callback. Kite page: "
                f"{kite_text or '(empty)'} — check the password / TOTP secret, "
                "and that the Kite app's redirect URL is this backend's "
                f"{_CALLBACK_PATH}.",
                stage="redirect",
            )
        if "error" in seen:
            raise AutoLoginError(
                f"/callback rejected the login: {seen['error']}", stage="session"
            )

        # /callback stamps lastConnected right after the exchange, then warms
        # the instrument cache before redirecting — so the DB is the source of
        # truth, scoped to THIS account.
        for _ in range(20):  # ~20 s
            s = await zerodha._get_settings(account_index)
            last = _ensure_aware_utc(s.lastConnected)
            if s.accessToken and last is not None and last >= run_start:
                return s.accessToken
            await asyncio.sleep(1)
        raise AutoLoginError(
            "Kite redirected to /callback but no fresh session was saved for "
            "this account — check the backend logs for zerodha_callback_failed.",
            stage="session",
        )

    # ── Scheduler helpers ──────────────────────────────────────────
    async def is_enabled(self, account_index: int = 0) -> bool:
        doc = await ZerodhaAutoLogin.find_one(
            ZerodhaAutoLogin.account_index == account_index
        )
        return bool(doc and doc.is_enabled)

    async def schedule_time(self, account_index: int = 0) -> str:
        doc = await ZerodhaAutoLogin.find_one(
            ZerodhaAutoLogin.account_index == account_index
        )
        return doc.schedule_time_ist if doc else "07:00"


zerodha_auto_login = ZerodhaAutoLoginService()
