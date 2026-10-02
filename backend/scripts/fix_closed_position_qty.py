"""One-off correction: fix the DISPLAYED quantity on a specific CLOSED
position without touching its realized P&L or the wallet ledger.

Background
----------
A CLOSED position always has ``quantity == 0`` (the closing leg flattens it).
The Closed / History tab renders the size the user actually held at close-time
from ``opening_quantity`` (see Position model). So to make a closed row show a
different quantity we edit ``opening_quantity`` ONLY — this is display metadata,
it does NOT feed the wallet, ledger, margin, or realized_pnl. Nothing financial
moves.

This was needed because an admin mis-keyed the size on user CL04229808's
NATURALGAS26SEPFUT AUTO-closed trade. The realized_pnl is already correct
(-16,500) in the DB; only the displayed size was wrong.

Usage (run from backend/, venv active)
--------------------------------------
    # READ-ONLY preview (default):
    python -m scripts.fix_closed_position_qty CL04229808 NATURALGAS26SEPFUT 6250

    # APPLY the change:
    python -m scripts.fix_closed_position_qty CL04229808 NATURALGAS26SEPFUT 6250 --apply

Only CLOSED positions matching the symbol are considered. If more than one
matches, the script prints them all and refuses to change anything unless
exactly one match exists (so you never edit the wrong row by accident).
"""

from __future__ import annotations

import asyncio
import sys

from app.core.database import close_database, init_database
from app.models.position import Position, PositionStatus
from app.models.user import User


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if len(args) < 3:
        print("usage: python -m scripts.fix_closed_position_qty "
              "<user_code> <symbol_contains> <new_qty> [--apply]")
        return

    user_code, symbol_needle, new_qty_raw = args[0], args[1].upper(), args[2]
    try:
        new_qty = float(new_qty_raw)
    except ValueError:
        print(f"[abort] new_qty must be a number, got: {new_qty_raw!r}")
        return

    await init_database()
    try:
        user = await User.find_one({"user_code": user_code})
        if user is None:
            print(f"[abort] no user with user_code = {user_code}")
            return
        print(f"USER: {user.full_name}  ({user_code})  id={user.id}")

        all_pos = await Position.find(Position.user_id == user.id).to_list()
        matches = [
            p
            for p in all_pos
            if p.status == PositionStatus.CLOSED
            and symbol_needle in (p.instrument.symbol or "").upper()
        ]

        if not matches:
            print(f"[abort] no CLOSED position matching '{symbol_needle}'")
            return

        print(f"\nFound {len(matches)} CLOSED position(s) matching "
              f"'{symbol_needle}':")
        for p in matches:
            print("  ---")
            print(f"  id            {p.id}")
            print(f"  symbol        {p.instrument.symbol}")
            print(f"  close_reason  {p.close_reason}")
            print(f"  closed_at     {p.closed_at}")
            print(f"  quantity      {p.quantity}   (0 for closed — expected)")
            print(f"  opening_qty   {p.opening_quantity}   <-- DISPLAYED size")
            print(f"  avg_price     {p.avg_price}")
            print(f"  realized_pnl  {p.realized_pnl}   (UNCHANGED by this script)")

        if len(matches) != 1:
            print("\n[abort] more than one CLOSED match — narrow the symbol "
                  "needle so exactly ONE row matches. Nothing changed.")
            return

        target = matches[0]
        old_qty = target.opening_quantity
        print(f"\nPLAN: set opening_quantity {old_qty} -> {new_qty} "
              f"on position {target.id}")
        print("      realized_pnl, wallet, ledger, margin: all UNTOUCHED.")

        if not apply:
            print("\n[dry-run] no changes written. Re-run with --apply to commit.")
            return

        target.opening_quantity = new_qty
        await target.save()
        # Re-read to confirm.
        fresh = await Position.get(target.id)
        print(f"\n[applied] opening_quantity is now {fresh.opening_quantity} "
              f"(realized_pnl still {fresh.realized_pnl})")
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
