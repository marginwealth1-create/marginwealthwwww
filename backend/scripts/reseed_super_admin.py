"""One-off: upsert the super-admin from .env values.

The boot-time `seed_super_admin()` short-circuits if any user with the seed
email already exists, so changing `SEED_SUPER_ADMIN_*` in `.env` has no
effect on an already-seeded DB. This script bridges that gap:

    python -m scripts.reseed_super_admin

It will (1) take SEED_SUPER_ADMIN_EMAIL / _PASSWORD / _MOBILE from settings,
(2) find any existing SUPER_ADMIN user (by current seed email OR by role),
(3) update its email/mobile/password to match .env, OR create one if none
exists. Status is forced to ACTIVE; 2FA and lockout state are cleared.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.core.database import close_database, init_database
from app.core.security import hash_password
from app.models.user import User, UserRole, UserStatus
from app.services import user_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reseed_super_admin")


async def main() -> None:
    await init_database()
    try:
        target_email = settings.SEED_SUPER_ADMIN_EMAIL.lower().strip()
        target_mobile = settings.SEED_SUPER_ADMIN_MOBILE.strip()
        target_pw = settings.SEED_SUPER_ADMIN_PASSWORD.get_secret_value()

        # Prefer match by current seed email; fall back to "any SUPER_ADMIN".
        existing = await User.find_one(User.email == target_email)
        if existing is None:
            existing = await User.find_one(User.role == UserRole.SUPER_ADMIN)

        if existing is None:
            user = await user_service.create_user(
                email=target_email,
                mobile=target_mobile,
                password=target_pw,
                full_name="Super Admin",
                role=UserRole.SUPER_ADMIN,
                status=UserStatus.ACTIVE,
            )
            user.must_change_password = False
            await user.save()
            logger.info("created super_admin email=%s mobile=%s", user.email, user.mobile)
            return

        # Update in place
        old_email = existing.email
        old_mobile = existing.mobile
        existing.email = target_email
        existing.mobile = target_mobile
        existing.password_hash = hash_password(target_pw)
        existing.status = UserStatus.ACTIVE
        existing.failed_login_count = 0
        existing.locked_until = None
        existing.must_change_password = False
        existing.role = UserRole.SUPER_ADMIN
        await existing.save()
        logger.info(
            "updated super_admin id=%s email %s -> %s mobile %s -> %s",
            existing.id,
            old_email,
            existing.email,
            old_mobile,
            existing.mobile,
        )
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
