"""Bulk-close every OPEN demo-account position via the SAME squareoff path
the risk enforcer uses (an opposite MARKET order through order_service), so
the demo wallet / used-margin / closing trades all reconcile correctly.

A raw `db.positions.updateMany({is_demo:true}, {status:"CLOSED"})` would NOT
do that — it would orphan the demo wallet's used_margin and the trade
blotter, leaving demo users with locked margin and wrong balances. ALWAYS
use this script (or the admin UI), never a raw status update.

Dry-run by default (lists what WOULD close, changes nothing). Pass --apply
to actually close. Run from the backend folder:

    cd /root/marginwealth/backend
    source .venv/bin/activate
    python -m scripts.close_demo_positions            # dry run (preview)
    python -m scripts.close_demo_positions --apply    # really close

Re-runnable — failures (usually a closed market / stale feed for that
segment) can be retried once that market is open.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.core.database import close_database, init_database
from app.core.redis_client import close_redis, init_redis
from app.models.position import Position, PositionStatus
from app.models.user import User
from app.services.risk_enforcer import _squareoff_position

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("close_demo_positions")


async def main(apply: bool) -> None:
    await init_database()
    # Redis is REQUIRED for an actual close: the squareoff path in the
    # matching engine reads the fill price from `mdlast:{token}` in Redis.
    # Without it every close fails with "feed is stale (price unavailable)"
    # AND the close helper swallows that error — so the run would silently
    # report success while closing nothing. Refuse to --apply without Redis.
    redis_ok = True
    try:
        await init_redis()
    except Exception as e:  # noqa: BLE001
        redis_ok = False
        print(f"⚠️  Redis init failed: {str(e)[:120]}")
    if apply and not redis_ok:
        print("❌ Aborting --apply: Redis is unavailable, every close would fail. Fix Redis first.")
        await close_database()
        return

    mode = "APPLY (closing for real)" if apply else "DRY RUN (no changes)"
    print(f"✅ MongoDB connected — mode: {mode}\n")

    demo_open = await Position.find(
        Position.status == PositionStatus.OPEN,
        Position.is_demo == True,  # noqa: E712
    ).to_list()
    print(f"Demo OPEN positions: {len(demo_open)}\n")
    if not demo_open:
        print("Nothing to do.")
        await close_database()
        return

    # Cache users so the same demo user isn't reloaded per-position.
    user_cache: dict[str, User | None] = {}
    ok = fail = skip = 0
    for p in demo_open:
        uid = str(p.user_id)
        if uid not in user_cache:
            user_cache[uid] = await User.get(p.user_id)
        user = user_cache[uid]
        code = (user.user_code if user else None) or uid

        if user is None:
            skip += 1
            print(f"  SKIP   {code:14} {p.instrument.symbol:16} — user not found")
            continue

        if not apply:
            print(f"  WOULD  {code:14} {p.instrument.symbol:16} qty={p.quantity}")
            continue

        try:
            await _squareoff_position(user, p, reason="ADMIN_DEMO_CLEANUP")
        except Exception as e:  # isolate per-position so one failure doesn't stop the run
            fail += 1
            print(f"  FAIL   {code:14} {p.instrument.symbol:16} — {str(e)[:120]}")
            continue
        # _squareoff_position SWALLOWS feed/engine errors (logs but doesn't
        # raise), so a no-exception return does NOT mean it closed. Verify by
        # re-reading the position: only count it closed if it really flipped.
        fresh = await Position.get(p.id)
        if fresh is not None and fresh.status == PositionStatus.OPEN and fresh.quantity != 0:
            fail += 1
            print(f"  FAIL   {code:14} {p.instrument.symbol:16} — still OPEN (stale feed / no price)")
        else:
            ok += 1
            print(f"  CLOSED {code:14} {p.instrument.symbol:16} qty={p.quantity}")

    if apply:
        print(
            f"\n✅ Done. closed={ok} failed={fail} skipped={skip}. "
            "Failures are usually a closed market / stale feed — re-run when that segment is open."
        )
    else:
        print(
            f"\nℹ️  DRY RUN only — nothing changed. "
            f"Re-run with --apply to actually close these {len(demo_open)} demo positions."
        )
    try:
        await close_redis()
    except Exception:  # noqa: BLE001
        pass
    await close_database()


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
