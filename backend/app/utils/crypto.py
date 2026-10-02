"""AES-256-GCM symmetric encryption for credential storage at rest.

Used by the Zerodha auto-login service to encrypt the Kite username,
password, and TOTP secret in the `zerodha_auto_login` collection. Key
comes from the `ZERODHA_CREDS_KEY` env var (32 raw bytes, base64).

GCM gives us authenticated encryption — tampering with the ciphertext
fails decryption rather than silently returning garbage, so a corrupted
DB row surfaces as a CryptoError instead of attempting to log in with
random bytes as the password.
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

# NIST SP 800-38D §5.2.1.1 recommends 96-bit IVs for GCM. New IV per
# encryption (random) is what keeps two encryptions of the same plaintext
# from producing identical ciphertexts.
_IV_LEN = 12


class CryptoError(RuntimeError):
    """Raised on missing/invalid key, malformed ciphertext, or tamper."""


@lru_cache(maxsize=1)
def _key() -> bytes:
    raw = settings.ZERODHA_CREDS_KEY.get_secret_value()
    if not raw:
        raise CryptoError(
            "ZERODHA_CREDS_KEY is not configured. Generate one with "
            "`python -c \"import os, base64; "
            'print(base64.b64encode(os.urandom(32)).decode())"` and set it '
            "in the backend .env file."
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise CryptoError(f"ZERODHA_CREDS_KEY is not valid base64: {exc}") from exc
    if len(key) != 32:
        raise CryptoError(
            f"ZERODHA_CREDS_KEY must decode to exactly 32 bytes (got {len(key)})."
        )
    return key


def encrypt(plaintext: str) -> tuple[str, str]:
    """Encrypt and return (ciphertext_b64, iv_b64). Fresh IV per call."""
    if plaintext is None:
        raise CryptoError("encrypt() received None — pass an empty string instead")
    aes = AESGCM(_key())
    iv = os.urandom(_IV_LEN)
    ct = aes.encrypt(iv, plaintext.encode("utf-8"), associated_data=None)
    return (
        base64.b64encode(ct).decode("ascii"),
        base64.b64encode(iv).decode("ascii"),
    )


def decrypt(ciphertext_b64: str, iv_b64: str) -> str:
    """Reverse encrypt(). Raises CryptoError on any failure."""
    if not ciphertext_b64 or not iv_b64:
        raise CryptoError("decrypt() received empty ciphertext or IV")
    try:
        ct = base64.b64decode(ciphertext_b64, validate=True)
        iv = base64.b64decode(iv_b64, validate=True)
    except Exception as exc:
        raise CryptoError(f"ciphertext/IV not valid base64: {exc}") from exc
    if len(iv) != _IV_LEN:
        raise CryptoError(f"IV must be {_IV_LEN} bytes (got {len(iv)})")
    aes = AESGCM(_key())
    try:
        pt = aes.decrypt(iv, ct, associated_data=None)
    except Exception as exc:
        raise CryptoError(
            "decryption failed (wrong key or tampered ciphertext)"
        ) from exc
    return pt.decode("utf-8")


def mask_secret(value: str, *, keep_head: int = 2, keep_tail: int = 1) -> str:
    """Render a sensitive string with most chars replaced by *."""
    if not value:
        return ""
    if len(value) <= keep_head + keep_tail:
        return "*" * len(value)
    return (
        value[:keep_head]
        + "*" * (len(value) - keep_head - keep_tail)
        + value[-keep_tail:]
    )
