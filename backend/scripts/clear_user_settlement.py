"""One-shot: wipe one user's settlement_outstanding and its txn history.

Run from `backend/`:

    source .venv/bin/activate
    python -m scripts.clear_user_settlement CL62329114
    # or pass --yes to skip the confirmation prompt
    python -m scripts.clear_user_settlement CL62329114 --yes

Effects (per target user):
  • Wallet.settlement_outstanding → 0
  • Every WalletTransaction row of type SETTLEMENT_OUTSTANDING_BOOKED
    and SETTLEMENT_OUTSTANDING_RECOVERY → deleted

Idempotent — re-running on a clean user is a no-op.

The transaction-row delete is the destructive bit: those rows are
the audit trail for how the settlement was booked / recovered, and
the dashboard's "windowed settlement" math reads them. After this
runs, the affected user's settlement column will show 0 in every
window for all time. Run only when the operator has decided the
debt is being written off (manual recovery, refund, etc.).

Exits with code 1 if the user_code isn't found, so a CI / cron
wrapper can detect typos.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from bson import Decimal128

from app.core.database import close_database, init_database
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User
from app.models.wallet import Wallet
from app.utils.decimal_utils import to_decimal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("clear_user_settlement")


async def main(user_code: str, assume_yes: bool) -> int:
    await init_database()
    print("✅ MongoDB connected\n")

    user = await User.find_one(User.user_code == user_code)
    if user is None:
        print(f"❌ User '{user_code}' not found.")
        return 1

    wallet = await Wallet.find_one(Wallet.user_id == user.id)
    if wallet is None:
        print(f"❌ Wallet for user '{user_code}' not found.")
        return 1

    current_settlement = to_decimal(wallet.settlement_outstanding)

    booked_rows = await WalletTransaction.find(
        WalletTransaction.user_id == user.id,
        WalletTransaction.transaction_type == TransactionType.SETTLEMENT_OUTSTANDING_BOOKED,
    ).to_list()
    recovery_rows = await WalletTransaction.find(
        WalletTransaction.user_id == user.id,
        WalletTransaction.transaction_type == TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY,
    ).to_list()

    print("=" * 60)
    print(f"User              : {user.user_code} · {user.full_name or '—'}")
    print(f"Wallet ID         : {wallet.id}")
    print(f"Current outstanding settlement : ₹{current_settlement:,.2f}")
    print(f"BOOKED txn rows to delete       : {len(booked_rows)}")
    print(f"RECOVERY txn rows to delete     : {len(recovery_rows)}")
    print("=" * 60)

    if (
        current_settlement == 0
        and not booked_rows
        and not recovery_rows
    ):
        print("✅ Already clean — nothing to do.")
        return 0

    if not assume_yes:
        ans = input("\nProceed with delete? [yes/NO] : ").strip().lower()
        if ans not in ("yes", "y"):
            print("❌ Cancelled — no changes made.")
            return 1

    # 1) Zero out the wallet snapshot. Use Decimal128 directly so the
    # column type matches the rest of the wallet record.
    wallet.settlement_outstanding = Decimal128("0")
    await wallet.save()
    print(f"✅ Wallet.settlement_outstanding → 0")

    # 2) Drop the txn history rows. Sequential deletes — these are
    # small lists (settlement events are rare per user), no need to
    # bulk-op.
    booked_deleted = 0
    for t in booked_rows:
        await t.delete()
        booked_deleted += 1
    recovery_deleted = 0
    for t in recovery_rows:
        await t.delete()
        recovery_deleted += 1

    print(f"✅ Deleted {booked_deleted} BOOKED rows")
    print(f"✅ Deleted {recovery_deleted} RECOVERY rows")
    print("\n✅ Done.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Clear a user's settlement_outstanding + its txn history")
    ap.add_argument("user_code", help="e.g. CL62329114")
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = ap.parse_args()

    try:
        rc = asyncio.run(main(args.user_code, assume_yes=args.yes))
    finally:
        # Best-effort close — script may exit before init_database() ran on bad args.
        try:
            asyncio.run(close_database())
        except Exception:
            pass
    sys.exit(rc)
