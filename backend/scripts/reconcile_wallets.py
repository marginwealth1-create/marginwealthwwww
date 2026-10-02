"""Reconcile wallet `available_balance` against the transaction ledger.

WHY: `wallet_service.adjust()` used to be a read-modify-write. When several
closes landed at once (Square off all / stop-out fan-out) the parallel writes
clobbered each other (LOST UPDATES), so `available_balance` drifted from the
truth. An admin Reopen that reversed the full recorded P&L then over-credited
the user (CL20371190: ₹2L → ₹7L). The code is now atomic (version-guarded),
but wallets corrupted BEFORE that fix need a one-time repair.

THE INVARIANT (holds regardless of the race, because each ledger row records
its own delta `balance_after - balance_before` correctly even when the stored
absolute balance was clobbered):

    correct_available = Σ(balance_after - balance_before over ALL txns)
                        - Σ(open-position margin_used)

Settlement rows (BOOKED / RECOVERY) have balance_after == balance_before, so
they contribute 0 and need no special-casing. Margin only moves between
available and used, so it's subtracted via the live open-position margin sum.

USAGE (from backend/, venv active):

    # Dry-run report for ONE user (no writes):
    python -m scripts.reconcile_wallets --user CL20371190

    # Dry-run report for ALL users with a > ₹1 discrepancy:
    python -m scripts.reconcile_wallets --all

    # Actually APPLY the fix (writes an ADJUSTMENT ledger row + snaps the
    # wallet to the reconstructed value). Review the dry-run first!
    python -m scripts.reconcile_wallets --user CL20371190 --apply
    python -m scripts.reconcile_wallets --all --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from decimal import Decimal

from bson import Decimal128

from app.core.database import close_database, init_database
from app.models.position import Position, PositionStatus
from app.models.transaction import (
    TransactionStatus,
    TransactionType,
    WalletTransaction,
)
from app.models.user import User
from app.models.wallet import Wallet
from app.utils.decimal_utils import quantize_money, to_decimal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reconcile_wallets")

ZERO = Decimal("0")
# Only flag/fix discrepancies bigger than this (paise-level rounding noise is
# never "fixed").
THRESHOLD = Decimal("1")


async def _ledger_available(user_id) -> Decimal:
    """Σ(balance_after - balance_before) over every COMPLETED txn for a user.
    This is the net change to available_balance the ledger says happened.

    IMPORTANT: our own correction rows (reference_type == "RECONCILE") are
    EXCLUDED from the sum — otherwise re-running the script would count its
    previous correction as new ledger movement and re-apply it forever
    (the double-subtract that hit CL20371190). Excluding them makes the
    reconstruction the same no matter how many times we've corrected, so
    the script is fully idempotent."""
    total = ZERO
    async for t in WalletTransaction.find(
        WalletTransaction.user_id == user_id,
        WalletTransaction.status == TransactionStatus.COMPLETED,
        WalletTransaction.reference_type != "RECONCILE",
    ):
        after = to_decimal(t.balance_after or 0)
        before = to_decimal(t.balance_before or 0)
        total += after - before
    return total


async def _open_margin(user_id) -> Decimal:
    """Σ(margin_used) over the user's OPEN positions — the live used margin."""
    total = ZERO
    async for p in Position.find(
        Position.user_id == user_id,
        Position.status == PositionStatus.OPEN,
    ):
        total += to_decimal(p.margin_used or 0)
    return total


async def reconcile_one(user: User, apply: bool) -> dict | None:
    w = await Wallet.find_one(Wallet.user_id == user.id)
    if w is None:
        return None

    ledger_avail = quantize_money(await _ledger_available(user.id))
    open_margin = quantize_money(await _open_margin(user.id))
    correct_available = quantize_money(ledger_avail - open_margin)

    stored_available = quantize_money(to_decimal(w.available_balance))
    diff = quantize_money(stored_available - correct_available)

    if abs(diff) <= THRESHOLD:
        return None  # in sync — nothing to report

    row = {
        "code": user.user_code,
        "name": user.full_name,
        "user_id": str(user.id),
        "stored_available": str(stored_available),
        "correct_available": str(correct_available),
        "diff": str(diff),
        "open_margin": str(open_margin),
    }

    logger.info(
        "MISMATCH %-12s stored=%-14s correct=%-14s diff=%-14s (margin %s)",
        user.user_code,
        stored_available,
        correct_available,
        diff,
        open_margin,
    )

    if apply:
        before = stored_available
        w.available_balance = Decimal128(str(correct_available))
        w.version = (w.version or 0) + 1
        await w.save()
        # Audit row so the trail explains the correction.
        await WalletTransaction(
            user_id=user.id,
            transaction_type=TransactionType.ADJUSTMENT,
            amount=Decimal128(str(quantize_money(correct_available - before))),
            balance_before=Decimal128(str(before)),
            balance_after=Decimal128(str(correct_available)),
            reference_type="RECONCILE",
            reference_id=str(user.id),
            narration=(
                "Wallet reconciled to ledger after the close-race / reopen "
                f"over-credit bug (was ₹{before}, ledger-correct ₹{correct_available})"
            ),
            status=TransactionStatus.COMPLETED,
        ).insert()
        row["fixed"] = True
        logger.info("  → FIXED %s: %s → %s", user.user_code, before, correct_available)

    return row


async def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--user", help="single user code, e.g. CL20371190")
    g.add_argument("--all", action="store_true", help="scan every user")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="WRITE the correction (default is dry-run report only)",
    )
    args = ap.parse_args()

    await init_database()
    try:
        if args.user:
            user = await User.find_one(User.user_code == args.user)
            if user is None:
                logger.error("User %s not found", args.user)
                return
            users = [user]
        else:
            users = await User.find_all().to_list()

        logger.info(
            "Reconciling %d user(s) — mode=%s",
            len(users),
            "APPLY" if args.apply else "DRY-RUN",
        )
        flagged = 0
        for u in users:
            res = await reconcile_one(u, apply=args.apply)
            if res is not None:
                flagged += 1
        logger.info(
            "Done. %d wallet(s) %s.",
            flagged,
            "FIXED" if args.apply else "flagged (run with --apply to fix)",
        )
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
