"""Read-only sanity check for the white-label branding Phase-1 schema.

Run after deploying the new User model to confirm Beanie/Mongo built
the expected sparse-unique index on `users.custom_domain` and that the
new optional fields are visible at the schema layer.

    python -m scripts.verify_branding_indexes

This script NEVER writes to the database. It is safe to run in
production any number of times.

Exits 0 on success, 1 on any inconsistency (so it can be wired into a
deploy smoke check).
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.core.config import settings
from app.core.database import close_database, init_database
from app.models.user import User

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("verify_branding_indexes")

EXPECTED_INDEX_NAME = "custom_domain_unique_partial"
EXPECTED_FIELDS = {
    "brand_name",
    "logo_url",
    "custom_domain",
    "custom_domain_status",
    "custom_domain_last_error",
    "custom_domain_verified_at",
    "signup_origin",
}


async def main() -> int:
    await init_database()
    failures: list[str] = []
    try:
        # 1. Schema-side check: model has the new optional fields with
        #    `None` default. We use Pydantic's model_fields introspection.
        model_fields = set(User.model_fields.keys())
        missing_fields = EXPECTED_FIELDS - model_fields
        if missing_fields:
            failures.append(
                f"User model missing branding fields: {sorted(missing_fields)}"
            )
        else:
            logger.info("[OK] User model exposes all 7 branding fields")

        # 2. DB-side check: pull the raw index list from Mongo and confirm
        #    the sparse-unique index on `custom_domain` exists exactly as
        #    declared in User.Settings.indexes.
        coll = User.get_motor_collection()
        index_info = await coll.index_information()

        target = index_info.get(EXPECTED_INDEX_NAME)
        if target is None:
            # Fall back: hunt for any index keyed on `custom_domain` so we
            # can report the mismatch precisely (helps if Beanie renamed it).
            candidates = {
                name: spec
                for name, spec in index_info.items()
                if any(k == "custom_domain" for k, _ in spec.get("key", []))
            }
            failures.append(
                f"Index `{EXPECTED_INDEX_NAME}` not found on `users`. "
                f"Custom-domain candidates seen: {candidates or 'none'}"
            )
        else:
            is_unique = bool(target.get("unique"))
            # New index uses partialFilterExpression (correctly excludes
            # null custom_domain values). Sparse=True was the original
            # buggy choice — replaced because Mongo sparse only skips
            # MISSING fields, not explicit nulls.
            partial = target.get("partialFilterExpression")
            if not is_unique or not partial:
                failures.append(
                    f"Index `{EXPECTED_INDEX_NAME}` exists but unique="
                    f"{is_unique}, partialFilterExpression={partial}; "
                    "expected unique=True with a partialFilterExpression"
                )
            else:
                logger.info(
                    "[OK] Index `%s` is unique+partial(%s) as expected",
                    EXPECTED_INDEX_NAME,
                    partial,
                )

        # 3. Sanity log: how many existing rows have a custom_domain set
        #    (must be 0 right after Phase 1 deploy).
        domain_count = await User.find({"custom_domain": {"$ne": None}}).count()
        logger.info(
            "[INFO] users with custom_domain set: %d (expected 0 right after Phase 1)",
            domain_count,
        )

        # 4. Feature-flag visibility
        logger.info(
            "[INFO] BRANDING_ENABLED=%s (Phase 1 ships with this False)",
            settings.BRANDING_ENABLED,
        )
    finally:
        await close_database()

    if failures:
        for msg in failures:
            logger.error("[FAIL] %s", msg)
        return 1
    logger.info("All branding schema checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
