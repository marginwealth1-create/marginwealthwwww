"""Zerodha Kite auto-login credentials + scheduler state.

Singleton document — the service layer ensures at most one row exists.
Stores encrypted (AES-256-GCM) Kite username, password, and TOTP secret
so the daily scheduler can drive a headless Playwright browser through
the Kite OAuth screen and refresh the access token at 07:00 IST.

Each credential field has its own IV so we can rotate one secret without
re-encrypting the others.
"""

from __future__ import annotations

from datetime import datetime

from app.models._base import TimestampMixin


class ZerodhaAutoLogin(TimestampMixin):
    # 0 = primary Zerodha account, 1 = secondary
    account_index: int = 0

    # ── Encrypted credential payload ─────────────────────────────────
    # ciphertext + iv pairs, both base64-encoded.
    encrypted_username: str = ""
    encrypted_username_iv: str = ""
    encrypted_password: str = ""
    encrypted_password_iv: str = ""
    encrypted_totp_secret: str = ""
    encrypted_totp_secret_iv: str = ""

    # ── Scheduler controls ───────────────────────────────────────────
    is_enabled: bool = False
    schedule_time_ist: str = "07:00"  # HH:MM in 24-hour IST

    # ── Last-attempt diagnostics (drives the admin UI status panel) ─
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_status: str = ""  # "success" | "failed" | "" (never run)
    last_attempt_source: str = ""  # "scheduler" | "manual" | ""
    last_error_detail: str | None = None
    consecutive_failures: int = 0
    last_duration_ms: int | None = None
    last_stage: str | None = None  # which Playwright stage last attempted

    class Settings:
        name = "zerodha_auto_login"
