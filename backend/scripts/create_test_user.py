"""One-shot script to create a test trader account.

Run from the backend folder:

    cd /opt/marginwealth/backend
    source .venv/bin/activate
    python -m scripts.create_test_user

Idempotent — if a user with the same email or mobile already exists, the
script reports it and exits without raising. Safe to re-run.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.core.database import close_database, init_database
from app.core.exceptions import ConflictError
from app.models._base import ALL_SEGMENTS
from app.models.user import UserRole, UserStatus
from app.services import user_service, wallet_service
from app.utils.decimal_utils import to_decimal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("create_test_user")


# ── Test user spec ───────────────────────────────────────────────────────
TEST_EMAIL = "test@marginwealth.live"
TEST_MOBILE = "9000000001"
TEST_PASSWORD = "Test@MarginWealth2026!"
TEST_FULL_NAME = "Test Trader"
# Starting wallet credit so the user can immediately place trades.
TEST_OPENING_BALANCE = 100_000  # ₹1 lakh demo funds


async def main() -> None:
    print(f"Connecting to MongoDB → {settings.MONGODB_URL[:50]}...")
    await init_database()
    print("✅ MongoDB connected")

    # ── Create the user ─────────────────────────────────────────────────
    try:
        user = await user_service.create_user(
            email=TEST_EMAIL,
            mobile=TEST_MOBILE,
            password=TEST_PASSWORD,
            full_name=TEST_FULL_NAME,
            role=UserRole.CLIENT,
            status=UserStatus.ACTIVE,
            is_demo=True,
        )
        print(f"✅ Created user: {user.user_code} <{user.email}> (id={user.id})")
    except ConflictError as e:
        print(f"⚠️  User already exists: {e}")
        # Re-fetch so we can still top up the wallet
        from app.models.user import User
        user = await User.find_one(User.email == TEST_EMAIL.lower())
        if user is None:
            print("❌ Could not locate existing user — aborting.")
            await close_database()
            return

    # ── Top up wallet to the configured opening balance ─────────────────
    wallet = await wallet_service.get_or_create(user.id)  # type: ignore[arg-type]
    current = float(str(wallet.available_balance))
    if current < TEST_OPENING_BALANCE:
        topup = TEST_OPENING_BALANCE - current
        wallet.available_balance = to_decimal(TEST_OPENING_BALANCE)  # type: ignore[assignment]
        await wallet.save()
        print(f"✅ Topped up wallet by ₹{topup:,.2f} → ₹{TEST_OPENING_BALANCE:,.2f}")
    else:
        print(f"ℹ️  Wallet already at ₹{current:,.2f} — skipping top-up")

    # ── Summary ─────────────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("Test user ready")
    print("═" * 60)
    print(f"  Email:    {TEST_EMAIL}")
    print(f"  Mobile:   {TEST_MOBILE}")
    print(f"  Password: {TEST_PASSWORD}")
    print(f"  Wallet:   ₹{TEST_OPENING_BALANCE:,.2f}")
    print(f"  Segments: {len(ALL_SEGMENTS)} (all enabled)")
    print("═" * 60)

    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
