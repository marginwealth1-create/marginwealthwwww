"""Password hashing, JWT tokens, TOTP 2FA."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import pyotp
from beanie import PydanticObjectId
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.exceptions import TokenExpiredError, TokenInvalidError

# bcrypt rounds=12 is the modern minimum; raise to 13+ if hardware allows.
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

TokenType = Literal["access", "refresh"]


# ── Passwords ────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return _pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


def needs_rehash(hashed: str) -> bool:
    return _pwd_ctx.needs_update(hashed)


# ── JWT ──────────────────────────────────────────────────────────────
def _encode(payload: dict[str, Any]) -> str:
    return jwt.encode(
        payload,
        settings.JWT_SECRET.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )


def create_access_token(
    *,
    user_id: str | PydanticObjectId,
    role: str,
    extra: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.JWT_ACCESS_TTL_MIN)).timestamp()),
        "jti": secrets.token_urlsafe(16),
    }
    if extra:
        payload.update(extra)
    return _encode(payload)


def create_refresh_token(*, user_id: str | PydanticObjectId, role: str) -> tuple[str, str]:
    """Returns (token, jti). The jti is stored server-side so refresh tokens can be revoked."""
    now = datetime.now(timezone.utc)
    jti = secrets.token_urlsafe(32)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.JWT_REFRESH_TTL_DAYS)).timestamp()),
        "jti": jti,
    }
    return _encode(payload), jti


def decode_token(token: str, *, expected_type: TokenType | None = None) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenExpiredError() from e
    except JWTError as e:
        raise TokenInvalidError() from e

    if expected_type is not None and payload.get("type") != expected_type:
        raise TokenInvalidError(f"Expected token type '{expected_type}'")
    return payload


# ── 2FA (TOTP) ───────────────────────────────────────────────────────
def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, *, account_name: str, issuer: str = "MarginWealth") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=issuer)


def verify_totp(secret: str, code: str, *, valid_window: int = 1) -> bool:
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=valid_window)
    except Exception:
        return False


# ── Refresh-token store (Redis-backed allowlist) ─────────────────────
def refresh_jti_key(user_id: str, jti: str) -> str:
    return f"refresh_jti:{user_id}:{jti}"


def session_key(user_id: str, jti: str) -> str:
    return f"session:{user_id}:{jti}"
