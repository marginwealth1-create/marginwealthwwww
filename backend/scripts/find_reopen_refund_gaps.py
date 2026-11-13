"""List admin reopens whose wallet refund differs from the closes they undid.

Before §6.6 a reopen refunded the position's whole realized_pnl at ONE fx
rate plus the charge of ONE closing fill found within 15 s of closed_at, while
it hid every close of the cycle. So a cycle closed in parts kept the earlier
closes' charges, and a USD cycle closed across a rate change was refunded at
the close rate, not at each close's own.

For each reopen that moved cash ("Reopen <symbol> — ..." REVERSAL or, when
the debit floored to a due, its SETTLEMENT_OUTSTANDING_BOOKED row), the
undone closes are that symbol's closing fills flagged superseded_by_reopen
since the position's previous reopen (or its open) up to this one. What the
reopen should have handed back is −Σ pnl_inr (each close's P&L, at its own
rate, net of its own charge). What it did hand back is the REVERSAL (+ any
part floored to a SETTLEMENT_OUTSTANDING_BOOKED due) + any settlement it
unbooked (RECOVERY), all with that position's reference within 5 s.

Run from `backend/`:

    source .venv/bin/activate
    python -m scripts.find_reopen_refund_gaps
    python -m scripts.find_reopen_refund_gaps --user <user ObjectId>
    python -m doctest scripts/find_reopen_refund_gaps.py   # self-check

READ-ONLY. It runs find() on wallet_transactions, positions and trades through
plain pymongo and writes nothing. Candidates, not verdicts: the match is by
symbol and time (not product), a reopen that moved no cash has no REVERSAL to
find, and a flip's first leg undone with the cycle is not flagged. Crediting a
difference is the operator's call (ground rule 1). Exits 1 when anything is
flagged.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from bson import ObjectId
from pymongo import MongoClient

from app.core.config import settings

CENT = Decimal("0.01")
_WINDOW = timedelta(seconds=5)


def _d(v: object) -> Decimal:
    return Decimal(str(v or 0))


def symbol_of(narration: str) -> str | None:
    """The symbol a reopen REVERSAL names.

    >>> symbol_of("Reopen NIFTY24NOVFUT — reverse close P&L + brokerage (closed by USER)")
    'NIFTY24NOVFUT'
    >>> symbol_of("Delete NIFTY — reverse") is None
    True
    """
    if not narration.startswith("Reopen "):
        return None
    return narration[len("Reopen ") :].split(" — ", maxsplit=1)[0].strip() or None


def shortfall(should: Decimal, did: Decimal) -> Decimal:
    """Rupees the client is still owed by the reopen (negative: it handed
    the client that much too much).

    >>> shortfall(Decimal("30"), Decimal("24"))
    Decimal('6.00')
    >>> shortfall(Decimal("-13650"), Decimal("-13400"))
    Decimal('-250.00')
    """
    return (should - did).quantize(CENT)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", help="target user ObjectId")
    args = ap.parse_args()

    db = MongoClient(settings.MONGODB_URL, tz_aware=True)[settings.MONGODB_DB_NAME]
    moved_types = ["REVERSAL", "SETTLEMENT_OUTSTANDING_BOOKED", "SETTLEMENT_OUTSTANDING_RECOVERY"]
    q: dict = {
        "transaction_type": {"$in": moved_types[:2]},
        "reference_type": "Position",
        "narration": {"$regex": "^Reopen "},
    }
    if args.user:
        q["user_id"] = ObjectId(args.user)

    reopens: dict[tuple, list[dict]] = defaultdict(list)
    for r in db.wallet_transactions.find(q).sort("created_at", 1):
        reopens[(r["user_id"], r["reference_id"])].append(r)

    flagged = 0
    for (uid, pid), rows in reopens.items():
        pos = db.positions.find_one({"_id": ObjectId(pid)}) if ObjectId.is_valid(pid) else None
        since = pos["opened_at"] - _WINDOW if pos and pos.get("opened_at") else None
        for r in rows:
            at = r["created_at"]
            if since is not None and at - since <= _WINDOW:
                continue  # another row of the reopen just counted
            sym = symbol_of(r.get("narration") or "")
            span: dict = {"$lte": at}
            if since is not None:
                span["$gt"] = since
            undone = list(
                db.trades.find(
                    {
                        "user_id": uid,
                        "instrument.symbol": sym,
                        "superseded_by_reopen": True,
                        "pnl_inr": {"$ne": None},
                        "executed_at": span,
                    }
                )
            )
            since = at
            moved = db.wallet_transactions.find(
                {
                    "user_id": uid,
                    "reference_type": "Position",
                    "reference_id": pid,
                    "transaction_type": {"$in": moved_types},
                    "created_at": {"$gte": at - _WINDOW, "$lte": at + _WINDOW},
                    "narration": {"$regex": "^Reopen "},
                }
            )
            did = sum((_d(m["amount"]) for m in moved), Decimal(0))
            should = -sum((_d(t["pnl_inr"]) for t in undone), Decimal(0))
            owed = shortfall(should, did)
            if owed:
                flagged += 1
                print(
                    f"user {uid} position {pid} {sym} reopened {at:%Y-%m-%d %H:%M:%S} UTC "
                    f"({'DELETED' if pos is None else pos.get('status')}): "
                    f"{len(undone)} close(s) undone, should refund {should}, refunded {did}, owed {owed}"
                )
    print(f"{flagged} reopen(s) flagged")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
