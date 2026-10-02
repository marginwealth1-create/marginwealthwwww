"""One-shot: backfill the new `support` broker-permission to EDIT for every
EXISTING broker / sub-broker so their behaviour is unchanged after the
support-permission feature ships.

Why: the `support` key was added to `BrokerPermissions` defaulting to OFF.
Before this feature, ANY broker's `support_whatsapp` was shown to their
clients unconditionally. Now the user-side resolver only honours a broker's
number when `broker_permissions.support != OFF`. Without this backfill, every
existing broker's number would suddenly stop showing (clients would fall back
to the parent admin) the moment the new code deploys — a regression.

Running this grants `support = EDIT` to all already-created brokers, so they
keep controlling their own number exactly as before. NEW brokers are created
with `support = OFF` by default (the admin explicitly grants it in the
broker-permission modal) — this script does NOT change that flow.

Run from the backend folder:

    source .venv/bin/activate
    python -m scripts.backfill_broker_support_perm

Idempotent — re-running only touches brokers whose `support` isn't already
EDIT. Only role == BROKER rows are affected.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.database import close_database, init_database
from app.models._base import PermissionLevel
from app.models.user import BrokerPermissions, User, UserRole

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_broker_support")


async def main() -> None:
    await init_database()
    try:
        brokers = await User.find(User.role == UserRole.BROKER).to_list()
        logger.info("found %d existing brokers / sub-brokers", len(brokers))
        changed = 0
        for b in brokers:
            bp = b.broker_permissions or BrokerPermissions()
            current = getattr(bp, "support", PermissionLevel.OFF)
            if current == PermissionLevel.EDIT:
                continue
            bp.support = PermissionLevel.EDIT
            b.broker_permissions = bp
            await b.save()
            changed += 1
            logger.info("granted support=EDIT -> %s (%s)", b.user_code, b.email)
        logger.info("done: %d/%d brokers updated", changed, len(brokers))
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
