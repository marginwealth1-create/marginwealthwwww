"""One-time cleanup: net/merge fragmented OPEN positions.

Fixes the duplicate-position rows left by the intraday->carryforward rollover
bug (MIS flipped to NRML in-place without netting into a pre-existing NRML
position on the same token). Reuses the SAME tested helper the live code now
calls automatically — `position_service.consolidate_open_positions` — which
folds each user's OPEN book to one row per (token, product): same-side lots
weighted-average into one; an opposing BUY/SELL nets and realises the offset
P&L exactly like a close.

Usage (on the server):

    cd /root/marginwealth/backend && source .venv/bin/activate

    # Preview WITHOUT changing anything (dry run) for one user:
    python -m scripts.consolidate_positions --user CL59347510 --dry-run

    # Apply for ONE user (by client code OR ObjectId):
    python -m scripts.consolidate_positions --user CL59347510

    # Apply for EVERY user that currently has >1 OPEN row on any (token,product):
    python -m scripts.consolidate_positions --all

Safe + idempotent: a user with no duplicates is a no-op. Money-neutral for
same-price lots; an opposing net books the real realised P&L to the wallet
(the profit/loss the user actually made going long then short).
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

from app.core.database import close_database, init_database
from app.models.position import Position, PositionStatus
from app.models.user import User
from app.services import position_service


async def _resolve_user_id(ref: str):
    """Accept a client code (CL...) or a raw ObjectId string."""
    from beanie import PydanticObjectId

    u = await User.find_one(User.user_code == ref)
    if u is not None:
        return u.id
    try:
        return PydanticObjectId(ref)
    except Exception:
        return None


async def _fragmented_groups(uid) -> list[tuple[str, str, int]]:
    """Return [(symbol, product, count)] for (token,product) groups with >1 OPEN row."""
    opens = await Position.find(
        Position.user_id == uid,
        Position.status == PositionStatus.OPEN,
    ).to_list()
    groups: dict[tuple, list[Position]] = defaultdict(list)
    for p in opens:
        groups[(p.instrument.token, p.product_type.value)].append(p)
    out = []
    for (_tok, prod), lots in groups.items():
        if len(lots) > 1:
            out.append((lots[0].instrument.symbol, prod, len(lots)))
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--user", help="client code (CL...) or ObjectId")
    g.add_argument("--all", action="store_true", help="every user with fragmented OPEN positions")
    ap.add_argument("--dry-run", action="store_true", help="report only; make no changes")
    args = ap.parse_args()

    await init_database()
    try:
        if args.all:
            uids = await Position.get_motor_collection().distinct(
                "user_id", {"status": PositionStatus.OPEN.value}
            )
            targets = list(uids)
        else:
            uid = await _resolve_user_id(args.user)
            if uid is None:
                print(f"❌ user not found: {args.user}")
                return
            targets = [uid]

        total_merged = 0
        for uid in targets:
            frags = await _fragmented_groups(uid)
            if not frags:
                if not args.all:
                    print(f"  {uid}: no fragmented positions — nothing to do.")
                continue
            desc = ", ".join(f"{s}({prod}) x{n}" for s, prod, n in frags)
            if args.dry_run:
                print(f"  [DRY] {uid}: would consolidate → {desc}")
                continue
            merged = await position_service.consolidate_open_positions(uid)
            total_merged += merged
            print(f"  ✅ {uid}: consolidated {merged} group(s) → was: {desc}")

        if args.dry_run:
            print("\nDry run — no changes made.")
        else:
            print(f"\nDone. Total (token,product) groups collapsed: {total_merged}")
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
