#!/usr/bin/env python3
"""Repair instruments wrongly tagged segment="FOREX".

Background
----------
The Infoway mirror (`mirror_subscribed_to_instruments`) joins by token and
overwrites `exchange/segment/instrument_type` from `_classify_infoway_code`,
whose fallback dumps any unrecognised code into FOREX/CDS. Internal seed
tokens (CRYPTO_*, NSE_EQ_*, NSE_IDX_*, BSE_IDX_*, MCX_FUT_*) and junk codes
("UNDEFINED") got subscribed at some point, so their rows were corrupted to
segment=FOREX — polluting the user app's Forex tab with crypto, Indian
stocks, indices and an MCX contract. The mirror also created bare-symbol
duplicates (EURUSD vs the seed FX_EURUSD).

What this does (SAFE — token never changes, so positions/orders stay linked):
  1. RECLASSIFY by token prefix back to the seed-correct fields.
  2. DEDUPE bare FX rows against their FX_ sibling — delete the duplicate
     ONLY when it has 0 positions AND 0 orders (keep the active one;
     ties -> keep the FX_-prefixed canonical row).
  3. DELETE the "UNDEFINED" junk row (only if it has 0 positions/orders).

A row that carries ANY positions or orders is NEVER deleted — at worst it
is reclassified. Deletion is reserved for rows with zero trading history.

Usage (on the server, backend dir, venv active):
    cd /root/marginwealth/backend && source .venv/bin/activate
    PYTHONPATH=/root/marginwealth/backend python scripts/fix_instrument_segments.py          # DRY-RUN
    PYTHONPATH=/root/marginwealth/backend python scripts/fix_instrument_segments.py --apply   # execute

Run the DRY-RUN first and review the plan. Take a Mongo backup before --apply.
Recommended to run AFTER market close (segment drives margin/market-hours).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import MongoClient

from app.core.config import settings

# token prefix -> seed-correct classification (mirrors app/seed/instruments.py)
PREFIX_MAP: list[tuple[str, dict]] = [
    ("CRYPTO_", {"exchange": "CRYPTO", "segment": "CRYPTO_SPOT", "instrument_type": "SPOT"}),
    ("NSE_EQ_", {"exchange": "NSE", "segment": "NSE_EQUITY", "instrument_type": "EQ"}),
    ("NSE_IDX_", {"exchange": "NSE", "segment": "NSE_EQUITY", "instrument_type": "INDEX", "is_tradable": False}),
    ("BSE_IDX_", {"exchange": "BSE", "segment": "BSE_EQUITY", "instrument_type": "INDEX", "is_tradable": False}),
    ("MCX_FUT_", {"exchange": "MCX", "segment": "MCX_FUTURE", "instrument_type": "FUT"}),
    # FX_ rows are already correct (FOREX/CDS/SPOT) — left untouched.
]


def _activity(db, token: str) -> tuple[int, int]:
    """(positions, orders) referencing this instrument token."""
    p = db["positions"].count_documents({"instrument.token": token})
    o = db["orders"].count_documents({"instrument.token": token})
    return p, o


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    mode = "APPLY" if apply else "DRY-RUN"

    db = MongoClient(settings.MONGODB_URL)[settings.MONGODB_DB_NAME]
    ins = db["instruments"]

    rows = list(ins.find({"segment": "FOREX"}))
    print(f"=== fix_instrument_segments [{mode}] — {len(rows)} rows tagged FOREX ===\n")

    reclassified = 0
    deleted = 0
    kept_forex: list[dict] = []  # rows that legitimately remain FOREX

    # ── Phase 1: reclassify by prefix ───────────────────────────────────
    for r in rows:
        token = r["token"]
        matched = None
        for prefix, fields in PREFIX_MAP:
            if token.startswith(prefix):
                matched = fields
                break
        if matched is None:
            # FX_ or bare symbol or UNDEFINED — handled in phase 2.
            kept_forex.append(r)
            continue
        changes = {k: v for k, v in matched.items() if r.get(k) != v}
        if not changes:
            continue
        print(f"[reclassify] {token:<22} {r.get('symbol'):<12} "
              f"FOREX/{r.get('exchange')} -> {matched['segment']}/{matched['exchange']} "
              f"{changes}")
        if apply:
            ins.update_one({"_id": r["_id"]}, {"$set": changes})
        reclassified += 1

    # ── Phase 2: dedupe FX_ vs bare symbol + drop UNDEFINED ─────────────
    # Group the remaining FOREX rows by symbol.
    by_symbol: dict[str, list[dict]] = {}
    for r in kept_forex:
        by_symbol.setdefault(r.get("symbol", ""), []).append(r)

    print()
    for symbol, group in by_symbol.items():
        if symbol == "UNDEFINED":
            for r in group:
                p, o = _activity(db, r["token"])
                if p == 0 and o == 0:
                    print(f"[delete    ] {r['token']:<22} UNDEFINED junk (0 pos / 0 orders)")
                    if apply:
                        ins.delete_one({"_id": r["_id"]})
                    deleted += 1
                else:
                    print(f"[KEEP-WARN ] {r['token']:<22} UNDEFINED has trades (p={p} o={o}) — left as-is")
            continue

        if len(group) <= 1:
            continue  # unique pair — nothing to dedupe

        # Pick the keeper: most activity, tie -> FX_ prefixed, tie -> first.
        scored = []
        for r in group:
            p, o = _activity(db, r["token"])
            scored.append((p + o, r["token"].startswith("FX_"), r))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        keeper = scored[0][2]
        print(f"[dedupe    ] {symbol}: keep {keeper['token']}")
        for activity, _is_fx, r in scored[1:]:
            p, o = _activity(db, r["token"])
            if p == 0 and o == 0:
                print(f"             delete dup {r['token']:<22} (0 pos / 0 orders)")
                if apply:
                    ins.delete_one({"_id": r["_id"]})
                deleted += 1
            else:
                print(f"             KEEP-WARN dup {r['token']:<22} has trades (p={p} o={o}) — left as-is")

    print(f"\n=== {mode} summary: reclassify={reclassified}  delete={deleted} ===")
    if not apply:
        print("Dry run only. Re-run with --apply (after a Mongo backup) to execute.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
