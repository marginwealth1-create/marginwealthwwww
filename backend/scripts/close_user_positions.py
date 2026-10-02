"""Bulk-close every OPEN position for ONE user (by user_code), via the SAME
squareoff path the risk enforcer uses (an opposite MARKET order through
order_service). Because it goes through the real engine, the user's
used_margin is released, the realised PnL is booked to the wallet, and a
closing trade is written so the position shows up in their history.

A raw `db.positions.updateMany(...,{status:"CLOSED"})` would NOT do any of
that — it would orphan margin/PnL/history. ALWAYS use this script.

Dry-run by default (lists what WOULD close). Pass --apply to actually close.

    cd /root/marginwealth/backend && source .venv/bin/activate
    python -m scripts.close_user_positions CL33333046           # preview
    python -m scripts.close_user_positions CL33333046 --apply    # close
"""

from __future__ import annotations

import asyncio
import sys

from app.core.database import close_database, init_database
from app.core.redis_client import close_redis, init_redis
from app.models.position import Position, PositionStatus
from app.models.user import User
from app.services.risk_enforcer import _squareoff_position


async def main(user_code: str, apply: bool) -> None:
    await init_database()
    # Redis is REQUIRED: the squareoff path reads the fill price from
    # mdlast:{token} in Redis. Without it every close fails with "feed is
    # stale" AND the close helper swallows that error — so the run would
    # report success while closing nothing.
    redis_ok = True
    try:
        await init_redis()
    except Exception as e:  # noqa: BLE001
        redis_ok = False
        print(f"⚠️  Redis init failed: {str(e)[:120]}")
    if apply and not redis_ok:
        print("❌ Aborting --apply: Redis unavailable, every close would fail.")
        await close_database()
        return

    user = await User.find_one(User.user_code == user_code)
    if user is None:
        print(f"❌ No user found with user_code = {user_code}")
        try:
            await close_redis()
        except Exception:  # noqa: BLE001
            pass
        await close_database()
        return

    mode = "APPLY (closing for real)" if apply else "DRY RUN (no changes)"
    print(f"✅ User {user_code} ({user.full_name}) — id={user.id} — mode: {mode}\n")

    open_positions = await Position.find(
        Position.user_id == user.id,
        Position.status == PositionStatus.OPEN,
    ).to_list()
    print(f"OPEN positions: {len(open_positions)}\n")
    if not open_positions:
        print("Nothing to do.")
        try:
            await close_redis()
        except Exception:  # noqa: BLE001
            pass
        await close_database()
        return

    ok = fail = 0
    for p in open_positions:
        if not apply:
            print(f"  WOULD  {p.instrument.symbol:18} qty={p.quantity}")
            continue
        try:
            await _squareoff_position(user, p, reason="ADMIN_MANUAL_CLOSE")
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  FAIL   {p.instrument.symbol:18} — {str(e)[:120]}")
            continue
        # _squareoff_position SWALLOWS feed/engine errors, so verify by
        # re-reading the position — only count it if it really flipped.
        fresh = await Position.get(p.id)
        if fresh is not None and fresh.status == PositionStatus.OPEN and fresh.quantity != 0:
            fail += 1
            print(f"  FAIL   {p.instrument.symbol:18} — still OPEN (stale feed / no price)")
        else:
            ok += 1
            print(f"  CLOSED {p.instrument.symbol:18} qty={p.quantity}")

    if apply:
        print(f"\n✅ Done. closed={ok} failed={fail}. used_margin/PnL/history reconcile through the engine.")
    else:
        print(f"\nℹ️  DRY RUN only — re-run with --apply to close these {len(open_positions)} positions.")
    try:
        await close_redis()
    except Exception:  # noqa: BLE001
        pass
    await close_database()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--apply"]
    if not args:
        print("Usage: python -m scripts.close_user_positions <USER_CODE> [--apply]")
        raise SystemExit(1)
    asyncio.run(main(user_code=args[0], apply="--apply" in sys.argv))
