"""List admin position edits made before §6.1/§6.2 reached the fills.

Before that fix an edit moved the Position doc (and the wallet) but not the
fills every report is rebuilt from:

  open-qty     OPEN quantity edit — the opening fill kept its old size.
  open-price   OPEN entry-price edit — the opening fill kept its old price.
  closed-price CLOSED avg/close edit — realized re-booked as
               (final close − avg) × opening_quantity, every close stamped
               with the final close price.
  override     CLOSED realized_pnl override — the fills were re-stamped the
               same way (it is wallet-only from the fix on).

Each row names the client and script; run
`python -m scripts.check_basis_drift --user <code>` for the arithmetic.

Run from `backend/`:

    source .venv/bin/activate
    python -m scripts.find_split_edits
    python -m scripts.find_split_edits --before 2026-11-14

Read-only. Makes no DB writes — safe to run on production. Nothing here is
repaired: a fix would move statements already sent (operator decision F1).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from app.core.database import close_database, init_database
from app.models.audit_log import AuditAction, AuditLog
from app.models.user import User


def _kind(old: dict, new: dict) -> str | None:
    def moved(k: str) -> bool:
        return str(old.get(k)) != str(new.get(k))

    if old.get("status") == "CLOSED":
        if moved("avg_price") or moved("close_price"):
            return "closed-price"
        return "override" if moved("realized_pnl") else None
    if moved("quantity"):
        return "open-qty"
    return "open-price" if moved("avg_price") else None


async def main(before: datetime | None) -> None:
    await init_database()
    try:
        q: dict = {"action": AuditAction.POSITION_EDIT.value}
        if before:
            q["created_at"] = {"$lt": before}
        codes: dict = {}
        async for log in AuditLog.find(q).sort("created_at"):
            kind = _kind(log.old_values or {}, log.new_values or {})
            if not kind:
                continue
            uid = log.target_user_id
            if uid not in codes:
                u = await User.get(uid) if uid else None
                codes[uid] = u.user_code if u else str(uid)
            meta = log.metadata or {}
            print(
                f"{log.created_at:%Y-%m-%d %H:%M}  {codes[uid]:<12} {meta.get('symbol') or '?':<20} "
                f"{kind:<13} position {log.entity_id}"
            )
    finally:
        await close_database()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--before", type=datetime.fromisoformat, help="only edits before this (the deploy)")
    asyncio.run(main(ap.parse_args().before))
