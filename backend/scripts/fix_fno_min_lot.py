"""Floor stored min-lot to 1 for F&O (option/future) segment rows.

The NSE F&O segment split left many segment rows / overrides with
``minLots = 0.01`` (or 0.1) for whole-lot F&O segments — NSE_STK_OPT,
NSE_IDX_OPT, NSE_STK_FUT, NSE_IDX_FUT, BSE_FUT, BSE_OPT, MCX_FUT, MCX_OPT.
The resolver already floors the EFFECTIVE min-lot to 1 for these segments
(see netting_service._to_legacy_dict), so enforcement is already correct; this
one-shot brings the STORED values in line so the admin panel shows 1, not 0.01.

FOREX / CRYPTO / equity are left untouched (they legitimately allow fractional
or use qty, not lots).

Run from the backend folder (dry-run first, then apply):

    cd /opt/marginwealth/backend && source .venv/bin/activate
    python -m scripts.fix_fno_min_lot
    python -m scripts.fix_fno_min_lot --apply

Idempotent — re-running just leaves already-1 rows as-is.
"""

from __future__ import annotations

import argparse
import asyncio

from app.core.database import close_database, init_database

# Whole-lot F&O segments (optionApplies or futureApplies == True).
FNO_SEGMENTS = [
    "NSE_STK_OPT",
    "NSE_IDX_OPT",
    "NSE_STK_FUT",
    "NSE_IDX_FUT",
    "BSE_FUT",
    "BSE_OPT",
    "MCX_FUT",
    "MCX_OPT",
]

# (collection, segment-key field)
TARGETS = [
    ("netting_segments", "name"),
    ("super_admin_segment_overrides", "segment_name"),
    ("sub_admin_segment_overrides", "segment_name"),
    ("user_segment_overrides", "segment_name"),
]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    await init_database()
    from app.core.database import get_db

    db = get_db()
    print("mode: %s\n" % ("APPLY" if args.apply else "DRY-RUN"))

    total = 0
    for coll, key in TARGETS:
        flt = {key: {"$in": FNO_SEGMENTS}, "minLots": {"$lt": 1, "$ne": None}}
        n = await db[coll].count_documents(flt)
        total += n
        print("%-32s rows with minLots<1: %d" % (coll, n))
        if args.apply and n:
            res = await db[coll].update_many(flt, {"$set": {"minLots": 1.0}})
            print("   -> set minLots=1.0 on %d rows" % res.modified_count)

    print("\n%d F&O rows %s" % (total, "updated" if args.apply else "would change"))
    if args.apply:
        # Effective-settings cache no longer matches the stored rows.
        try:
            from app.core.redis_client import get_redis

            r = await get_redis()
            keys = [k async for k in r.scan_iter("netting_eff:*")]
            if keys:
                await r.delete(*keys)
            print("cleared %d netting_eff cache keys" % len(keys))
        except Exception as e:  # noqa: BLE001
            print("cache clear skipped:", e)
        print("\n✅ done")
    else:
        print("\n(dry-run — re-run with --apply to write)")

    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
