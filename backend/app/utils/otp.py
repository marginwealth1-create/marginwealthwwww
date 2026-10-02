"""Numeric OTP generation and Redis-backed verification.

OTPs are stored as `otp:{purpose}:{identifier} → {code}` with TTL.
Each verify increments an attempt counter; > MAX_ATTEMPTS invalidates the OTP.
"""

from __future__ import annotations

import secrets
from typing import Literal

from app.core.redis_client import get_redis

OtpPurpose = Literal["register", "login", "reset_password", "withdrawal"]
OTP_TTL_SEC = 300  # 5 minutes
OTP_LENGTH = 6
MAX_ATTEMPTS = 5


def generate_otp(length: int = OTP_LENGTH) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def _key(purpose: OtpPurpose, identifier: str) -> str:
    return f"otp:{purpose}:{identifier}"


def _attempts_key(purpose: OtpPurpose, identifier: str) -> str:
    return f"otp:attempts:{purpose}:{identifier}"


async def issue_otp(purpose: OtpPurpose, identifier: str, *, ttl_sec: int = OTP_TTL_SEC) -> str:
    code = generate_otp()
    r = get_redis()
    pipe = r.pipeline()
    pipe.setex(_key(purpose, identifier), ttl_sec, code)
    pipe.delete(_attempts_key(purpose, identifier))
    await pipe.execute()
    return code


async def verify_otp(purpose: OtpPurpose, identifier: str, code: str) -> bool:
    r = get_redis()
    stored = await r.get(_key(purpose, identifier))
    if stored is None:
        return False

    attempts = int(await r.incr(_attempts_key(purpose, identifier)))
    await r.expire(_attempts_key(purpose, identifier), OTP_TTL_SEC)

    if attempts > MAX_ATTEMPTS:
        await r.delete(_key(purpose, identifier))
        return False

    if not secrets.compare_digest(stored, code):
        return False

    # Single-use — invalidate on success
    await r.delete(_key(purpose, identifier), _attempts_key(purpose, identifier))
    return True


async def invalidate_otp(purpose: OtpPurpose, identifier: str) -> None:
    r = get_redis()
    await r.delete(_key(purpose, identifier), _attempts_key(purpose, identifier))
