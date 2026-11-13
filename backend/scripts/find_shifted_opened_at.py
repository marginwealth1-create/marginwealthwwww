"""List admin position edits whose opened_at was moved back by the §6.3
datetime-local bug.

The admin Positions edit dialog prefilled <input type="datetime-local"> with
the UTC wall clock (seconds dropped) and parsed it back as the admin's local
time. On an IST machine every OPEN-row save, even an SL-only one, sent

    opened_at = trunc_minute(old opened_at) - 5:30

and each later save stacked another -5:30. Every window keyed on opened_at
(charges, delete refund, reopen supersede, fill resync) then widened back over
earlier positions on the same token.

A POSITION_EDIT audit row is flagged when new_values.opened_at equals
old_values.opened_at truncated to the minute minus k * 5:30 for some k >= 1.
Rows are grouped per position. "before" is the opened_at ahead of that
position's first flagged save, and "now" is the Position's current opened_at.

Run from `backend/`:

    source .venv/bin/activate
    python -m scripts.find_shifted_opened_at
    python -m scripts.find_shifted_opened_at --user <user ObjectId>
    python -m doctest scripts/find_shifted_opened_at.py   # self-check

READ-ONLY. It runs find() on audit_logs and positions through plain pymongo
(no init_database, so no index builds) and writes nothing. It repairs
nothing: whether and how to restore opened_at is the operator's call. Exits 1
when anything is flagged.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta, timezone

from bson import ObjectId
from pymongo import MongoClient

from app.core.config import settings

SHIFT = timedelta(hours=5, minutes=30)
IST = timezone(SHIFT)


def _dt(v: object) -> datetime | None:
    """Audit values are isoformat strings; naive means UTC."""
    if not v:
        return None
    if isinstance(v, datetime):
        d = v
    else:
        try:
            d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def shift_steps(old: datetime | None, new: datetime | None) -> int:
    """k >= 1 when new == trunc_minute(old) - k * 5:30, else 0.

    >>> o = _dt("2026-11-12T04:30:15.123000+00:00")
    >>> shift_steps(o, _dt("2026-11-11T23:00:00+00:00"))
    1
    >>> shift_steps(o, _dt("2026-11-11T17:30:00+00:00"))
    2
    >>> shift_steps(o, _dt("2026-11-12T04:30:00+00:00"))  # truncation only
    0
    >>> shift_steps(o, _dt("2026-11-11T23:00:15.123000+00:00"))  # seconds kept: a typed edit
    0
    >>> shift_steps(o, _dt("2026-11-12T10:00:00+00:00"))  # moved later
    0
    """
    if old is None or new is None:
        return 0
    k, rem = divmod(old.replace(second=0, microsecond=0) - new, SHIFT)
    return k if k >= 1 and not rem else 0


def _ist(d: datetime | None) -> str:
    return d.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST") if d else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", help="target user ObjectId")
    args = ap.parse_args()

    db = MongoClient(settings.MONGODB_URL, tz_aware=True)[settings.MONGODB_DB_NAME]
    q: dict = {"action": "POSITION_EDIT", "entity_type": "Position"}
    if args.user:
        q["target_user_id"] = ObjectId(args.user)

    hits: dict[str, list[tuple[dict, datetime, datetime, int]]] = defaultdict(list)
    for r in db.audit_logs.find(q).sort("created_at", 1):
        old = _dt((r.get("old_values") or {}).get("opened_at"))
        new = _dt((r.get("new_values") or {}).get("opened_at"))
        k = shift_steps(old, new)
        if k and old and new:
            hits[r.get("entity_id") or "?"].append((r, old, new, k))

    for pid, rows in hits.items():
        first = rows[0][0]
        pos = (
            db.positions.find_one({"_id": ObjectId(pid)}, {"opened_at": 1, "status": 1})
            if ObjectId.is_valid(pid)
            else None
        )
        steps = sum(k for *_, k in rows)
        print(
            f"position {pid} user {first.get('target_user_id')} "
            f"{(first.get('metadata') or {}).get('symbol', '?')} "
            f"status {pos.get('status') if pos else 'DELETED'}: "
            f"{len(rows)} flagged save(s), shifted -{steps * SHIFT}"
        )
        print(f"  before {_ist(rows[0][1])}   now {_ist(_dt(pos.get('opened_at')) if pos else None)}")
        for r, old, new, k in rows:
            print(f"  {_ist(r.get('created_at'))} by {r.get('user_id')}: {_ist(old)} -> {_ist(new)} (k={k})")

    print(f"{len(hits)} position(s), {sum(len(v) for v in hits.values())} edit(s) flagged")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
