"""One-shot: give the Indian segments the midnight cutoff they already had.

Before this setting existed, the EOD sweep cancelled parked orders on NSE /
BSE / NFO / BFO / MCX just after 00:00 IST, and never touched forex or
crypto. The setting's blank default means "never sweep", so without this
backfill the Indian segments would silently stop expiring orders — the exact
carry-forward bug the sweep was written to fix.

Writes "00:00" only where the field is still blank, so an admin's own choice
is never overwritten. Safe to re-run.

    cd backend && python -m scripts.backfill_pending_order_expiry
"""

from __future__ import annotations

import asyncio

INDIAN_PREFIXES = ("NSE", "BSE", "NFO", "BFO", "MCX")


async def main() -> None:
    from app.core.database import init_database
    from app.models.netting import NettingSegment

    await init_database()
    touched = 0
    for seg in await NettingSegment.find_all().to_list():
        name = str(seg.name).upper()
        if not name.startswith(INDIAN_PREFIXES):
            continue
        if (getattr(seg, "pendingOrderExpiryTime", "") or "").strip():
            continue
        seg.pendingOrderExpiryTime = "00:00"
        await seg.save()
        touched += 1
        print(f"  {name}: pendingOrderExpiryTime = 00:00")
    print(f"done — {touched} segment(s) updated")


if __name__ == "__main__":
    asyncio.run(main())
