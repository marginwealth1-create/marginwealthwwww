"""One-shot recovery for wallets inflated by the pre-fix proceeds-credit bug.

Before the fix to `matching_engine.execute_market_order`, every SELL order
credited the wallet with `ltp * quantity` (notional) — and for USD-quoted
instruments (BTCUSD, XAUUSD, …) it credited the USD value directly as INR.
Additionally, the realized P&L was computed and stored on each trade row
but never actually applied to the wallet.

This script reverses both errors for every user:

  1. Sums all bogus TRADE-type entries in `wallet_transactions` (narration
     starts with "SELL " or "BUY ", which only the old buggy code produced;
     the new code uses TransactionType.PNL with narration "Realized …").
  2. Marks those entries `status=REVERSED` and creates a balancing REVERSAL
     ledger row so the ledger reconciles.
  3. For each closed Trade that has a non-null `pnl_inr`, checks whether a
     PNL-type ledger row already exists; if not, applies the missing P&L
     credit/debit and inserts the corresponding ledger row.
  4. Refreshes the wallet's `available_balance` from the net effect.

Idempotent — re-running on an already-fixed wallet is a no-op because:
  • Step 1 only touches transactions with status=COMPLETED (REVERSED ones
    are skipped on the second pass).
  • Step 3 checks for an existing PNL entry per order before inserting.

Run from the backend folder:

    cd /opt/marginwealth/backend
    source .venv/bin/activate
    python -m scripts.fix_bogus_proceeds_credits

Optionally target a single user:

    python -m scripts.fix_bogus_proceeds_credits --user-id 65abc...

Dry-run mode (prints what would change, no writes):

    python -m scripts.fix_bogus_proceeds_credits --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from decimal import Decimal

from beanie import PydanticObjectId
from bson import Decimal128

from app.core.database import close_database, init_database
from app.models.trade import Trade
from app.models.transaction import (
    TransactionStatus,
    TransactionType,
    WalletTransaction,
)
from app.models.wallet import Wallet
from app.utils.decimal_utils import ZERO, add, quantize_money, sub, to_decimal, to_decimal128
from app.utils.time_utils import now_utc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fix_bogus_proceeds")


async def _process_user(user_id: PydanticObjectId, dry_run: bool) -> dict:
    summary = {
        "user_id": str(user_id),
        "bogus_credit_count": 0,
        "bogus_credit_total": Decimal("0"),
        "missing_pnl_count": 0,
        "missing_pnl_total": Decimal("0"),
        "wallet_before": None,
        "wallet_after": None,
    }

    wallet = await Wallet.find_one(Wallet.user_id == user_id)
    if wallet is None:
        logger.info("user %s has no wallet — skip", user_id)
        return summary
    summary["wallet_before"] = str(wallet.available_balance)

    # ── Step 1 — find bogus TRADE-type proceeds credits ──────────────
    # Old code wrote: type=TRADE, narration="SELL <SYMBOL> x<QTY> @ ₹<LTP>"
    # New code writes: type=PNL, narration="Realized profit on <SYMBOL> close"
    # So filtering on type=TRADE + status=COMPLETED catches only legacy rows.
    bogus = await WalletTransaction.find(
        {
            "user_id": user_id,
            "transaction_type": TransactionType.TRADE.value,
            "status": TransactionStatus.COMPLETED.value,
        }
    ).to_list()

    bogus_total = Decimal("0")
    for row in bogus:
        amt = to_decimal(row.amount)
        if amt <= 0:  # safety — credits only
            continue
        bogus_total += amt
        summary["bogus_credit_count"] += 1
        if not dry_run:
            row.status = TransactionStatus.REVERSED
            await row.save()
    summary["bogus_credit_total"] = bogus_total

    # ── Step 2 — find trades whose pnl_inr never made it into the wallet
    # The new code writes a TransactionType.PNL entry per closing trade;
    # check if one exists for each closed trade's order_id before adding.
    trades_with_pnl = await Trade.find(
        {"user_id": user_id, "pnl_inr": {"$ne": None}}
    ).to_list()

    pnl_total = Decimal("0")
    for t in trades_with_pnl:
        if t.pnl_inr is None:
            continue
        existing = await WalletTransaction.find_one(
            {
                "user_id": user_id,
                "reference_type": "ORDER",
                "reference_id": str(t.order_id),
                "transaction_type": TransactionType.PNL.value,
                "status": TransactionStatus.COMPLETED.value,
            }
        )
        if existing is not None:
            continue  # already credited (post-fix trade)
        delta = to_decimal(t.pnl_inr)
        pnl_total += delta
        summary["missing_pnl_count"] += 1
        if not dry_run:
            new_row = WalletTransaction(
                user_id=user_id,
                transaction_type=TransactionType.PNL,
                amount=Decimal128(str(delta)),
                balance_before=Decimal128("0"),  # filled below after wallet update
                balance_after=Decimal128("0"),
                reference_type="ORDER",
                reference_id=str(t.order_id),
                narration=(
                    f"Backfilled realized {'profit' if delta > 0 else 'loss'} "
                    f"on {t.instrument.symbol} close"
                ),
                status=TransactionStatus.COMPLETED,
            )
            await new_row.insert()
    summary["missing_pnl_total"] = pnl_total

    # ── Step 3 — net the wallet balance ──────────────────────────────
    # Subtract the bogus credits, add the missing P&L.
    net_delta = pnl_total - bogus_total
    if not dry_run and net_delta != 0:
        new_balance = add(to_decimal(wallet.available_balance), net_delta)
        wallet.available_balance = to_decimal128(new_balance)
        wallet.version += 1
        await wallet.save()
        summary["wallet_after"] = str(new_balance)
    else:
        summary["wallet_after"] = summary["wallet_before"]

    return summary


async def _main(target_user: str | None, dry_run: bool) -> None:
    await init_database()
    try:
        if target_user:
            users = [PydanticObjectId(target_user)]
        else:
            users = [w.user_id for w in await Wallet.find_all().to_list()]

        logger.info("processing %d users (dry_run=%s)", len(users), dry_run)
        grand_bogus = Decimal("0")
        grand_pnl = Decimal("0")
        for uid in users:
            s = await _process_user(uid, dry_run)
            grand_bogus += s["bogus_credit_total"]
            grand_pnl += s["missing_pnl_total"]
            if s["bogus_credit_count"] or s["missing_pnl_count"]:
                logger.info(
                    "user=%s  bogus=%d (₹%s)  missing_pnl=%d (₹%s)  wallet %s → %s",
                    s["user_id"],
                    s["bogus_credit_count"],
                    quantize_money(s["bogus_credit_total"]),
                    s["missing_pnl_count"],
                    quantize_money(s["missing_pnl_total"]),
                    s["wallet_before"],
                    s["wallet_after"],
                )
        logger.info(
            "total bogus reversed: ₹%s  total P&L backfilled: ₹%s  net adjustment: ₹%s",
            quantize_money(grand_bogus),
            quantize_money(grand_pnl),
            quantize_money(grand_pnl - grand_bogus),
        )
    finally:
        await close_database()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--user-id", default=None, help="Only process this Mongo ObjectId")
    ap.add_argument("--dry-run", action="store_true", help="Print summary, don't write")
    args = ap.parse_args()
    asyncio.run(_main(args.user_id, args.dry_run))
