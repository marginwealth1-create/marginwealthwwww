"""Symmetric encryption for third-party secrets stored at rest.

Used for the per-admin oxapay merchant API key so a DB dump never exposes a
usable payment-gateway credential. The Fernet key is derived deterministically
from JWT_SECRET (already required to be a strong random value), so no extra
key management is needed and the same running config always decrypts.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    raw = settings.JWT_SECRET.get_secret_value().encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    """Encrypt a plaintext secret → base64 token safe to store in Mongo."""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str | None) -> str | None:
    """Decrypt a token produced by :func:`encrypt_secret`. Returns None for a
    missing/blank value or a token that can't be decrypted (rotated secret /
    corruption) — callers treat None as 'no usable key'."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def _selfcheck() -> None:
    """Round-trip assertion — run standalone: python -m app.utils.crypto_secrets"""
    s = "oxapay_test_key_ABC123"
    enc = encrypt_secret(s)
    assert enc != s and decrypt_secret(enc) == s, "encrypt/decrypt round-trip failed"
    assert decrypt_secret(None) is None and decrypt_secret("garbage") is None
    print("crypto_secrets round-trip: ok")


if __name__ == "__main__":
    _selfcheck()
