"""One-shot: give every EXISTING sub-admin access to all admin sections.

Why: sections were added to `AdminPermissions` over time. Sub-admins created
BEFORE a section existed have that field missing → it defaults to False, so the
section silently disappears from their admin panel AND from the permission grid
when they create an Employee. The operator wants all already-created admins to
have the full section set the super-admin has; NEW admins keep being configured
explicitly by the super-admin at creation time (this script does NOT touch that
flow — it only backfills the admins that already exist).

Run from the backend folder:

    source .venv/bin/activate
    python -m scripts.grant_all_sections_to_existing_admins

Idempotent — re-running just re-asserts all-True on every existing sub-admin.
Only role == ADMIN rows are touched (super-admin ignores admin_permissions;
brokers use broker_permissions; employees are configured per-employee).
"""

from __future__ import annotations

import asyncio
import logging

from app.core.database import close_database, init_database
from app.models.user import AdminPermissions, User, UserRole

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("grant_all_sections")


def _all_true() -> AdminPermissions:
    return AdminPermissions(**{f: True for f in AdminPermissions.model_fields})


async def main() -> None:
    await init_database()
    try:
        full = _all_true()
        admins = await User.find(User.role == UserRole.ADMIN).to_list()
        logger.info("found %d existing sub-admins", len(admins))
        changed = 0
        for a in admins:
            before = a.admin_permissions.model_dump() if a.admin_permissions else None
            after = full.model_dump()
            if before == after:
                continue
            a.admin_permissions = _all_true()
            await a.save()
            changed += 1
            logger.info("granted all sections -> %s (%s)", a.user_code, a.email)
        logger.info("done: %d/%d sub-admins updated", changed, len(admins))
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
