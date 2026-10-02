#!/usr/bin/env python3
"""Export every user under a given broker (whole subtree) to CSV.

READ-ONLY — never writes to the database. Recovers contact details even for
SOFT-DELETED (status=CLOSED) users, whose real email/mobile were moved to the
`deleted_email_original` / `deleted_mobile_original` tombstone fields by the
admin delete flow (see api/v1/admin/users.py).

Usage (on the server, from the backend dir, with the venv active):
    cd /root/marginwealth/backend
    source .venv/bin/activate
    python scripts/export_broker_users.py BRK40576343

The broker code defaults to BRK40576343 if not passed. Output is written to
./broker_<CODE>_users.csv and a summary is printed.
"""
from __future__ import annotations

import csv
import os
import sys

# Make the backend package importable regardless of how this script is invoked
# (`python scripts/foo.py` only puts scripts/ on sys.path, not the backend dir).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import MongoClient

from app.core.config import settings

DEFAULT_BROKER_CODE = "BRK40576343"


def _real_mobile(u: dict) -> str:
    # Tombstoned (CLOSED) rows keep the original in deleted_mobile_original.
    return (u.get("deleted_mobile_original") or u.get("mobile") or "").strip()


def _real_email(u: dict) -> str:
    orig = u.get("deleted_email_original")
    if orig:
        return orig.strip()
    email = (u.get("email") or "").strip()
    # Defensive: strip the "+deleted-<id>" marker if an older row lacked the
    # tombstone field but had the suffix baked into the email itself.
    if "+deleted-" in email:
        local = email.split("+deleted-", 1)[0]
        domain = email.rsplit("@", 1)[-1] if "@" in email else ""
        email = f"{local}@{domain}" if domain else local
    return email


def main() -> int:
    broker_code = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BROKER_CODE

    client = MongoClient(settings.MONGODB_URL)
    db = client[settings.MONGODB_DB_NAME]
    users = db["users"]

    broker = users.find_one({"user_code": broker_code}, {"_id": 1, "full_name": 1})
    if not broker:
        print(f"ERROR: broker with user_code={broker_code!r} not found")
        return 1
    bid = broker["_id"]
    print(f"Broker found: {broker_code} ({broker.get('full_name')}) _id={bid}")

    # Whole subtree: direct clients (assigned_broker_id) + every descendant
    # at any depth (broker_ancestry multikey match). $or de-dups in Mongo.
    query = {"$or": [{"assigned_broker_id": bid}, {"broker_ancestry": bid}]}
    cursor = users.find(query).sort("created_at", 1)

    fields = [
        "user_code",
        "full_name",
        "role",
        "status",
        "mobile",
        "email",
        "pan",
        "created_at",
    ]
    out_path = f"broker_{broker_code}_users.csv"
    n_total = 0
    n_closed = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for u in cursor:
            n_total += 1
            status = u.get("status")
            if status == "CLOSED":
                n_closed += 1
            kyc = u.get("kyc") or {}
            writer.writerow(
                {
                    "user_code": u.get("user_code"),
                    "full_name": u.get("full_name"),
                    "role": u.get("role"),
                    "status": status,
                    "mobile": _real_mobile(u),
                    "email": _real_email(u),
                    "pan": kyc.get("pan") or "",
                    "created_at": u.get("created_at"),
                }
            )

    print(f"Exported {n_total} users ({n_closed} deleted/CLOSED) -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
