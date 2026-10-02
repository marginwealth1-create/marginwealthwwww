"""Generate a VAPID key pair for Web Push.

Run ONCE per deployment from `backend/`:

    source .venv/bin/activate
    python -m scripts.generate_vapid_keys

Copy the printed lines into the backend .env (or systemd
EnvironmentFile). DO NOT commit the private key.

Why VAPID matters: the browser refuses anonymous push subscriptions —
the application server needs an identity. The keys we generate here are
that identity; the push service (FCM / Mozilla autopush / Apple) uses
them to verify every message originated from us before delivering it.

Rotating the keys invalidates EVERY existing subscription — users have
to re-grant permission and resubscribe. Plan accordingly.
"""

from __future__ import annotations

import base64
import sys

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except ImportError:
    print("cryptography is required — pip install cryptography", file=sys.stderr)
    raise SystemExit(1)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> None:
    priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    pub = priv.public_key()

    # RFC 8292 expects the raw 65-byte uncompressed public point (X9.62).
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    # Private key as raw 32-byte scalar (DER-decoded), to match what
    # pywebpush / py-vapid expect when passed via env.
    priv_bytes = priv.private_numbers().private_value.to_bytes(32, "big")

    print("# Paste these into your backend .env (DO NOT commit the private key)")
    print()
    print(f"VAPID_PUBLIC_KEY={_b64url(pub_bytes)}")
    print(f"VAPID_PRIVATE_KEY={_b64url(priv_bytes)}")
    print("# Optional — your operations contact:")
    print("VAPID_SUBJECT=mailto:admin@marginwealth.com")
    print()
    print("# Then restart the backend so the new keys load.")


if __name__ == "__main__":
    main()
