"""Authentication service — login, refresh, logout, 2FA flows.

Refresh tokens are stored as a Redis allow-list keyed by JTI; logout deletes
the JTI; rotation issues a new JTI and revokes the old one. This gives us
revocability without per-request DB hits.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Literal

from app.core.config import settings
from app.core.exceptions import (
    AccountBlockedError,
    AccountInactiveError,
    AppError,
    InvalidCredentialsError,
    TenantLoginNotAllowedError,
    TokenInvalidError,
    TwoFAInvalidError,
    TwoFARequiredError,
)
from app.core.redis_client import cache_get, cache_set, get_redis
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_totp_secret,
    hash_password,
    needs_rehash,
    refresh_jti_key,
    session_key,
    totp_provisioning_uri,
    verify_password,
    verify_totp,
)
from app.models.user import User, UserRole, UserStatus
from app.schemas.auth import AuthUserOut, TokenPair
from app.services import branding_service, user_service
from app.utils.time_utils import now_utc

logger = logging.getLogger(__name__)

ADMIN_ROLES: set[UserRole] = {
    UserRole.SUPER_ADMIN,
    UserRole.ADMIN,
    UserRole.BROKER,
    UserRole.EMPLOYEE,
}

LoginAudience = Literal["user", "admin"]

# Lockout feature is OFF — increment-only counter kept for audit / future
# re-enable. To re-introduce brute-force protection, set the constants
# below to positive values and uncomment the lock check in `authenticate`.
MAX_FAILED_ATTEMPTS = 0  # 0 = never lock
LOCKOUT_MINUTES = 0


# ── Failed-login tracking (lockout disabled) ─────────────────────────
async def _register_failed_attempt(user: User) -> None:
    user.failed_login_count = (user.failed_login_count or 0) + 1
    # Intentionally NOT setting locked_until — lockout disabled per project
    # decision (UX over brute-force-protection on the admin login).
    await user.save()


def _is_locked(_user: User) -> bool:
    # Lockout disabled — always returns False so authenticate() never
    # rejects with "Account temporarily locked". The field stays in the
    # model so admin-side unlock tooling and existing data still work.
    return False


# ── Multi-tenant login isolation (pure predicate) ────────────────────
def user_in_tenant(
    *,
    user_id,
    user_role: UserRole,
    user_assigned_admin_id,
    tenant_admin_id,
) -> bool:
    """May a user authenticate on the branded domain owned by
    ``tenant_admin_id``?  Pure function — no DB / IO, so it's fully
    unit-testable (see tests/test_login_tenant_isolation.py).

    Allowed when ANY of:
      * the user is a SUPER_ADMIN (platform owner — every domain),
      * the user IS the admin who owns this domain (own site login),
      * the user's ``assigned_admin_id`` is that admin (the admin's
        whole downstream pool — clients / dealers / masters / brokers).

    Everyone else (users of a DIFFERENT admin, or super-admin-pool
    users whose ``assigned_admin_id`` is None) is rejected on this
    branded domain — they must use their own login link / the main
    platform domain. All ids are compared as strings so ObjectId and
    str callers agree.
    """
    if user_role == UserRole.SUPER_ADMIN:
        return True
    tid = str(tenant_admin_id)
    if str(user_id) == tid:
        return True
    if user_assigned_admin_id is not None and str(user_assigned_admin_id) == tid:
        return True
    return False


# ── Login ────────────────────────────────────────────────────────────
async def authenticate(
    *,
    identifier: str,
    password: str,
    two_fa_code: str | None,
    audience: LoginAudience,
    ip: str,
    user_agent: str | None,
    login_host: str | None = None,
) -> TokenPair:
    user = await user_service.find_by_identifier(identifier)
    if user is None:
        raise InvalidCredentialsError()

    if user.status == UserStatus.BLOCKED:
        raise AccountBlockedError()
    if user.status != UserStatus.ACTIVE:
        raise AccountInactiveError()

    if _is_locked(user):
        raise AccountBlockedError(
            "Account temporarily locked due to too many failed attempts. Try again later."
        )

    # Audience guard — admin endpoint only allows admin roles
    if audience == "admin" and user.role not in ADMIN_ROLES:
        raise InvalidCredentialsError()

    # Password
    if not verify_password(password, user.password_hash):
        await _register_failed_attempt(user)
        raise InvalidCredentialsError()

    # Re-hash if the bcrypt cost has been raised
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    # 2FA — only enforced when the user has explicitly enabled it on their account.
    # (Spec previously required mandatory 2FA for admins; relaxed per project decision.)
    if user.two_fa_enabled:
        if not two_fa_code:
            raise TwoFARequiredError()
        if not user.two_fa_secret or not verify_totp(user.two_fa_secret, two_fa_code):
            await _register_failed_attempt(user)
            raise TwoFAInvalidError()

    # ── Multi-tenant login isolation ─────────────────────────────────
    # Gate the login by the branded domain it arrived on. Deliberately
    # placed AFTER the password + 2FA verify, so only the account's real
    # owner ever learns of a domain mismatch — no cross-tenant account
    # enumeration. A host that resolves to NO admin custom_domain is the
    # platform's own main domain (or the shared API host) → unrestricted,
    # exactly like today; only a real branded tenant domain gates.
    if settings.LOGIN_TENANT_ISOLATION and settings.BRANDING_ENABLED and login_host:
        tenant_admin = await branding_service.find_admin_by_domain(login_host)
        if tenant_admin is not None and not user_in_tenant(
            user_id=user.id,
            user_role=user.role,
            user_assigned_admin_id=user.assigned_admin_id,
            tenant_admin_id=tenant_admin.id,
        ):
            logger.warning(
                "login_tenant_mismatch user=%s role=%s assigned_admin=%s "
                "domain=%s tenant_admin=%s enforce=%s",
                user.id,
                user.role.value,
                user.assigned_admin_id,
                login_host,
                tenant_admin.id,
                settings.LOGIN_TENANT_ISOLATION_ENFORCE,
            )
            if settings.LOGIN_TENANT_ISOLATION_ENFORCE:
                raise TenantLoginNotAllowedError()

    # Mint tokens — stamp the user's current session epoch into the access
    # token so an admin block / password reset (which bumps token_version)
    # invalidates it instantly.
    access = create_access_token(
        user_id=user.id, role=user.role.value, extra={"ver": int(user.token_version or 0)}
    )
    refresh, jti = create_refresh_token(user_id=user.id, role=user.role.value)

    # Store JTI in Redis (allow-list) + session record + bump user's
    # last-login fields — all three are independent writes, so run them
    # concurrently instead of awaiting one-by-one. The refresh-token JTI
    # write is the only one that MUST land before we return (otherwise the
    # next request can't refresh); the session/audit row and user.save are
    # nice-to-have audit metadata.
    user.record_successful_login(ip)
    await asyncio.gather(
        cache_set(
            refresh_jti_key(str(user.id), jti),
            {"user_id": str(user.id), "audience": audience, "ip": ip, "ua": user_agent},
            ttl_sec=settings.JWT_REFRESH_TTL_DAYS * 86400,
        ),
        cache_set(
            session_key(str(user.id), jti),
            {"audience": audience, "ip": ip, "ua": user_agent, "issued_at": now_utc().isoformat()},
            ttl_sec=settings.JWT_REFRESH_TTL_DAYS * 86400,
        ),
        user.save(),
    )

    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.JWT_ACCESS_TTL_MIN * 60,
        user=_user_to_auth_out(user),
    )


def _user_to_auth_out(user: User) -> AuthUserOut:
    return AuthUserOut(
        id=str(user.id),
        user_code=user.user_code,
        email=user.email,
        mobile=user.mobile,
        full_name=user.full_name,
        role=user.role.value,
        status=user.status.value,
        is_demo=user.is_demo,
        two_fa_enabled=user.two_fa_enabled,
        must_change_password=user.must_change_password,
        admin_permissions=user.admin_permissions,
        pnl_share_pct=(
            str(user.pnl_share_pct) if user.pnl_share_pct is not None else None
        ),
        broker_permissions=user.broker_permissions,
        assigned_broker_id=(
            str(user.assigned_broker_id) if user.assigned_broker_id else None
        ),
    )


# ── Refresh ──────────────────────────────────────────────────────────
# Rotation grace window. When a refresh token is rotated we DON'T hard-
# delete the old jti immediately — we keep it for this many seconds with
# a pointer to the freshly-minted pair. If the SAME old token comes in
# again within the window (because the rotating response was lost in
# transit — extremely common on iOS Safari PWAs resuming from background /
# lock, where the request reaches the server but the reply is dropped as
# the network reattaches), we REPLAY the same new pair instead of
# rejecting it. Without this, the client keeps the old (now-deleted) token,
# the next refresh returns 401 "revoked", and the frontend logs the user
# out — the "iOS pe baar-baar logout" the operator reported despite the
# 30-day refresh TTL.
REFRESH_ROTATION_GRACE_SEC = 60


async def refresh_tokens(refresh_token: str) -> TokenPair:
    payload = decode_token(refresh_token, expected_type="refresh")
    user_id = payload.get("sub")
    jti = payload.get("jti")
    if not user_id or not jti:
        raise TokenInvalidError()

    r = get_redis()
    key = refresh_jti_key(user_id, jti)
    record = await cache_get(key)
    if record is None:
        raise TokenInvalidError("Refresh token has been revoked")

    # Replay path — this jti was already rotated within the grace window.
    # Return the SAME pair we minted on the first call so a lost-response
    # retry succeeds instead of logging the user out.
    if isinstance(record, dict) and record.get("rotated_to_refresh"):
        user = await user_service.get_user_or_404(record.get("user_id") or user_id)
        if user.status != UserStatus.ACTIVE:
            raise AccountInactiveError()
        return TokenPair(
            access_token=record["rotated_to_access"],
            refresh_token=record["rotated_to_refresh"],
            expires_in=settings.JWT_ACCESS_TTL_MIN * 60,
            user=_user_to_auth_out(user),
        )

    user = await user_service.get_user_or_404(user_id)
    if user.status != UserStatus.ACTIVE:
        raise AccountInactiveError()

    access = create_access_token(
        user_id=user.id, role=user.role.value, extra={"ver": int(user.token_version or 0)}
    )
    new_refresh, new_jti = create_refresh_token(user_id=user.id, role=user.role.value)
    await cache_set(
        refresh_jti_key(str(user.id), new_jti),
        {"user_id": str(user.id), "rotated_from": jti},
        ttl_sec=settings.JWT_REFRESH_TTL_DAYS * 86400,
    )

    # Grace-rotate the OLD jti instead of hard-deleting it: overwrite its
    # value with a pointer to the new pair and shrink its TTL to the grace
    # window. A duplicate/late refresh with this same old token within the
    # window hits the replay path above. After the window it expires and is
    # gone for good, so the security profile (one-shot rotation) is
    # preserved beyond the brief grace. The session_key is dropped now —
    # it carries no auth weight on the refresh path.
    await cache_set(
        key,
        {
            "user_id": str(user.id),
            "rotated_to_access": access,
            "rotated_to_refresh": new_refresh,
        },
        ttl_sec=REFRESH_ROTATION_GRACE_SEC,
    )
    try:
        await r.delete(session_key(user_id, jti))
    except Exception:  # noqa: BLE001 — best-effort; not auth-critical
        pass

    return TokenPair(
        access_token=access,
        refresh_token=new_refresh,
        expires_in=settings.JWT_ACCESS_TTL_MIN * 60,
        user=_user_to_auth_out(user),
    )


# ── Logout ───────────────────────────────────────────────────────────
async def logout(
    *,
    refresh_token: str | None,
    user_id: str | None = None,
    allow_global_purge: bool = True,
) -> None:
    """Revoke the JTI tied to this refresh token (if provided), else all of user's sessions.

    `allow_global_purge` MUST be False for the SHARED demo account. Every demo
    visitor logs into the SAME user id, so the `user_id` fallback below would
    scan-delete EVERY demo session — i.e. one demo user logging out without a
    valid refresh_token in the payload (already cleared / expired / garbage)
    would wipe every other concurrent demo visitor's refresh token, kicking
    them all to /login the moment their short access token expired. That is
    exactly the "kuch demo users ek saath logout ho jaate the" report. For a
    shared account we therefore only ever delete the ONE session tied to the
    presented refresh token and never the blanket purge.
    """
    r = get_redis()
    if refresh_token:
        try:
            payload = decode_token(refresh_token, expected_type="refresh")
            jti = payload.get("jti")
            sub = payload.get("sub")
            if jti and sub:
                await r.delete(refresh_jti_key(sub, jti), session_key(sub, jti))
                return
        except Exception:
            pass
    if user_id and allow_global_purge:
        # Best-effort: scan-delete all of this user's sessions. Detach into a
        # background task so the /logout response returns immediately instead
        # of waiting on Redis SCAN cursors (was adding ~50-150ms on users
        # with multiple active devices).
        async def _purge() -> None:
            try:
                async for key in r.scan_iter(match=f"refresh_jti:{user_id}:*", count=200):
                    await r.delete(key)
                async for key in r.scan_iter(match=f"session:{user_id}:*", count=200):
                    await r.delete(key)
            except Exception:  # noqa: BLE001 — best-effort cleanup
                logger.exception("session purge failed for user %s", user_id)

        asyncio.create_task(_purge())


# ── Force logout (block / password reset) ────────────────────────────
async def revoke_user_sessions(user: User) -> None:
    """Force-logout a user from EVERY device, immediately.

    Two-pronged so neither token type can survive:
      1. Bump `token_version` → every outstanding ACCESS token (carrying the
         old `ver` claim) is rejected by the auth dependency on its next
         request, without waiting for the 15-min access-token expiry.
      2. Purge all refresh JTIs from the Redis allow-list → the client can't
         silently refresh a new access token back in.

    Called when an admin blocks the account or resets its password so the
    user is kicked out the instant the action lands (not 15 min later).
    The caller is responsible for having already changed status / password.
    """
    user.token_version = int(user.token_version or 0) + 1
    await user.save()

    r = get_redis()
    uid = str(user.id)
    try:
        async for key in r.scan_iter(match=f"refresh_jti:{uid}:*", count=200):
            await r.delete(key)
        async for key in r.scan_iter(match=f"session:{uid}:*", count=200):
            await r.delete(key)
    except Exception:  # noqa: BLE001 — best-effort; the version bump alone
        # already invalidates access tokens, so a Redis hiccup here only
        # means a refresh token might live until its own TTL.
        logger.exception("revoke_user_sessions: redis purge failed for %s", uid)


# ── Demo login ───────────────────────────────────────────────────────
# Fixed identity for the ONE shared demo account. Every "Try Demo" click logs
# into THIS single account instead of minting a throwaway per click (operator
# decision 2026-06-23). The old per-click behaviour spawned thousands of demo
# users whose open positions never closed and bloated the DB/server. A daily
# reset loop (`demo_service.reset_global_demo`, scheduled in main.py) flattens
# its trades and restores the ₹50L virtual balance every 24h. NOTE: this is a
# SHARED account — concurrent demo visitors see each other's positions/balance.
GLOBAL_DEMO_EMAIL = "demo@test.app"
GLOBAL_DEMO_MOBILE = "9000000000"


async def create_demo_session(*, ip: str = "0.0.0.0", user_agent: str | None = None) -> TokenPair:
    """Log into the single shared demo account (find-or-create), return a JWT pair."""
    from app.models.transaction import TransactionType
    from app.models.user import AccountType, User
    from app.services import wallet_service

    user = await User.find_one(User.email == GLOBAL_DEMO_EMAIL)
    if user is None:
        # First-ever demo login — provision the shared account exactly once.
        try:
            user = await user_service.create_user(
                email=GLOBAL_DEMO_EMAIL,
                mobile=GLOBAL_DEMO_MOBILE,
                password=secrets.token_hex(16),
                full_name="Demo User",
                is_demo=True,
            )
            user.account_type = AccountType.DEMO
            await user.save()
            await wallet_service.adjust(
                user.id,
                5_000_000,
                transaction_type=TransactionType.BONUS,
                narration="Demo account virtual credit",
            )
        except Exception:
            # Race: two first-time clicks landed together and one already
            # inserted the row (unique email/mobile). Re-fetch the winner.
            user = await User.find_one(User.email == GLOBAL_DEMO_EMAIL)

    if user is None:
        raise AppError("Could not start demo session. Please try again.")

    # Stamp the user's current session epoch into the access token, exactly
    # like normal login / refresh / impersonation do. WITHOUT this the demo
    # token carried no `ver` claim → defaulted to 0 at the session-epoch gate
    # (dependencies.get_current_user). The instant the shared demo account's
    # `token_version` was ever bumped above 0 (admin block/unblock, password
    # reset, force_logout_user), EVERY fresh demo login 401'd on its very
    # first /users/me (0 != N) → "Could not load profile" + ₹0.00 wallet, and
    # couldn't self-heal because the client's refresh short-circuits a
    # not-yet-expiring token. Carrying the real `ver` keeps demo consistent.
    access = create_access_token(
        user_id=user.id,
        role=user.role.value,
        extra={"ver": int(user.token_version or 0)},
    )
    refresh, jti = create_refresh_token(user_id=user.id, role=user.role.value)
    await cache_set(
        refresh_jti_key(str(user.id), jti),
        {"user_id": str(user.id), "audience": "user", "ip": ip, "ua": user_agent},
        ttl_sec=settings.JWT_REFRESH_TTL_DAYS * 86400,
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.JWT_ACCESS_TTL_MIN * 60,
        user=_user_to_auth_out(user),
    )


# ── 2FA setup ─────────────────────────────────────────────────────────
async def begin_2fa_setup(user: User) -> tuple[str, str]:
    secret = generate_totp_secret()
    user.two_fa_secret = secret
    user.two_fa_enabled = False
    await user.save()
    uri = totp_provisioning_uri(secret, account_name=user.email, issuer="MarginWealth")
    return secret, uri


async def confirm_2fa(user: User, code: str) -> list[str]:
    if not user.two_fa_secret:
        raise TwoFAInvalidError("2FA setup has not been started")
    if not verify_totp(user.two_fa_secret, code):
        raise TwoFAInvalidError()
    user.two_fa_enabled = True
    user.two_fa_backup_codes = [secrets.token_hex(4).upper() for _ in range(8)]
    await user.save()
    return user.two_fa_backup_codes


async def disable_2fa(user: User, *, password: str, code: str) -> None:
    if not verify_password(password, user.password_hash):
        raise InvalidCredentialsError()
    if user.role in ADMIN_ROLES:
        raise InvalidCredentialsError("Admin accounts may not disable 2FA")
    if not user.two_fa_secret or not verify_totp(user.two_fa_secret, code):
        raise TwoFAInvalidError()
    user.two_fa_enabled = False
    user.two_fa_secret = None
    user.two_fa_backup_codes = []
    await user.save()


# ── Password change / reset ──────────────────────────────────────────
async def change_password(user: User, *, current: str, new: str) -> None:
    if not verify_password(current, user.password_hash):
        raise InvalidCredentialsError()
    user.password_hash = hash_password(new)
    user.password_changed_at = now_utc()
    user.must_change_password = False
    await user.save()


async def reset_password(user: User, *, new_password: str) -> None:
    user.password_hash = hash_password(new_password)
    user.password_changed_at = now_utc()
    user.must_change_password = False
    user.failed_login_count = 0
    user.locked_until = None
    await user.save()
    # Invalidate all existing sessions
    await logout(refresh_token=None, user_id=str(user.id))
