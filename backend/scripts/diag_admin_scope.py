"""Reconcile an admin's user counts — why does the sub-admin list say 141
but the admin's own dashboard / accounts say 122?

Run on the server from the backend folder:

    cd ~/marginwealth/backend
    source .venv/bin/activate
    python -m scripts.diag_admin_scope ADM84967256

Read-only. Prints, for the given admin:

  A) count_assigned_users  = flat  {assigned_admin_id: admin.id}  (the
     number the super-admin "Sub-admins" list shows)  — broken down by
     role so we can see how many are broker/sub-admin login accounts vs
     real trading clients.

  B) scoped_user_ids       = the dashboard / accounts number (clients
     only + broker subtree, non-client roles excluded).

  C) the two set differences, so we can see EXACTLY which rows are in one
     bucket but not the other — and whether any are transferred clients
     that genuinely fall through the cracks (a bug) vs broker accounts
     that are excluded on purpose.
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter

from app.core.database import close_database, init_database
from app.core.dependencies import scoped_user_ids
from app.models.user import User, UserRole


def _label(u: User) -> str:
    return (
        f"{u.user_code:<14} role={str(getattr(u, 'role', '')):<22} "
        f"assigned_admin_id={u.assigned_admin_id} "
        f"assigned_broker_id={getattr(u, 'assigned_broker_id', None)} "
        f"broker_ancestry={getattr(u, 'broker_ancestry', None)}  "
        f"{(u.full_name or '')[:24]}"
    )


async def main() -> None:
    code = sys.argv[1] if len(sys.argv) > 1 else "ADM84967256"
    await init_database()
    print(f"\n{'='*72}\nADMIN SCOPE DIAG — admin code = {code}\n{'='*72}")

    admin = await User.find_one({"user_code": code})
    if admin is None:
        print(f"❌ No user with user_code = {code}")
        await close_database()
        return
    print(f"ADMIN : {admin.full_name}  ({code})  id={admin.id}  role={admin.role}")

    # ── A) flat assigned_admin_id == admin.id, by role ────────────────
    flat = await User.find({"assigned_admin_id": admin.id}).to_list()
    by_role = Counter(str(getattr(u, "role", "")) for u in flat)
    print(f"\n── A) flat {{assigned_admin_id: admin.id}}  TOTAL = {len(flat)}  "
          "(this is the sub-admin-list 'USERS' number)")
    for r, n in sorted(by_role.items()):
        print(f"     {r:<28} {n}")

    # ── B) scoped_user_ids (dashboard / accounts) ─────────────────────
    scoped = await scoped_user_ids(admin)
    scoped_set = set(scoped)
    print(f"\n── B) scoped_user_ids()  TOTAL = {len(scoped)}  "
          "(dashboard 'Total users' + accounts)")

    # ── C) set differences ────────────────────────────────────────────
    flat_set = {u.id for u in flat}
    flat_by_id = {u.id: u for u in flat}

    in_flat_not_scoped = flat_set - scoped_set
    in_scoped_not_flat = scoped_set - flat_set

    print(f"\n── C) IN flat(A) but NOT in scoped(B)  = {len(in_flat_not_scoped)}")
    print("     (these inflate the 141 — expected if they are BROKER/admin "
          "login accounts; a BUG if they are CLIENT rows)")
    crole = Counter(str(getattr(flat_by_id[i], "role", "")) for i in in_flat_not_scoped)
    for r, n in sorted(crole.items()):
        print(f"     {r:<28} {n}")
    # show any CLIENT-tier rows that fell out — those would be the real bug
    client_dropped = [
        flat_by_id[i]
        for i in in_flat_not_scoped
        if str(getattr(flat_by_id[i], "role", "")) not in {
            UserRole.SUPER_ADMIN.value, UserRole.ADMIN.value, UserRole.BROKER.value,
        }
    ]
    if client_dropped:
        print(f"     ⚠️  {len(client_dropped)} CLIENT-tier rows dropped from scope "
              "(THIS would be a real bug):")
        for u in client_dropped[:30]:
            print("        " + _label(u))
    else:
        print("     ✅ no CLIENT-tier rows dropped — the gap is purely "
              "broker/admin login accounts (working as intended).")

    print(f"\n── IN scoped(B) but NOT in flat(A)  = {len(in_scoped_not_flat)}")
    print("     (transferred-broker subtree clients whose assigned_admin_id "
          "was never propagated — scoped catches them, flat misses them)")
    if in_scoped_not_flat:
        sample = list(in_scoped_not_flat)[:30]
        rows = await User.find({"_id": {"$in": sample}}).to_list()
        for u in rows:
            print("        " + _label(u))

    print(f"\n{'='*72}")
    print("SUMMARY")
    print(f"  sub-admin-list count (A) ......... {len(flat)}")
    print(f"  dashboard/accounts count (B) ..... {len(scoped)}")
    print(f"  broker/admin accts only in A ..... "
          f"{len(in_flat_not_scoped) - len(client_dropped)}")
    print(f"  CLIENT rows wrongly dropped ...... {len(client_dropped)}  "
          "(should be 0)")
    print(f"  subtree clients only in B ........ {len(in_scoped_not_flat)}")
    print(f"{'='*72}\nDone (read-only).\n")

    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
