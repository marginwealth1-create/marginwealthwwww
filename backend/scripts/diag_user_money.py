"""Dump every money-side WalletTransaction for one user with a per-type breakdown.

Use when "ADD FUND lakhs me kyu dikh raha" or any Money Transactions /
Accounts Dashboard tile looks unexpectedly large. Prints:

  • Per-type subtotal (DEPOSIT / WITHDRAWAL / ADJUSTMENT+ / ADJUSTMENT−
    / SETTLEMENT_OUTSTANDING_BOOKED / RECOVERY) within an optional date
    window.
  • A row-by-row dump of every matching transaction so the operator can
    see narration + actor + date for each entry — surfaces the source
    of any inflated total (admin-creation initial balance, batch credit,
    historical admin Add Fund, etc.).

Run from `backend/`:

    source .venv/bin/activate
    python -m scripts.diag_user_money CL62329114
    # or with a date window:
    python -m scripts.diag_user_money CL62329114 --from 2026-06-01 --to 2026-06-07

Read-only. Makes no DB writes — safe to run on production.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.database import close_database, init_database
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User
from app.models.wallet import Wallet
from app.utils.decimal_utils import to_decimal


IST = timezone(timedelta(hours=5, minutes=30))


def _parse_date(s: str | None, end_of_day: bool) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=IST)
    except ValueError:
        print(f"❌ Invalid date '{s}' — expected YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59)
    return d.astimezone(timezone.utc)


def _fmt(d: Decimal) -> str:
    sign = "+" if d > 0 else ""
    return f"{sign}{d:,.2f}"


async def main(user_code: str, from_s: str | None, to_s: str | None) -> int:
    await init_database()
    print("✅ MongoDB connected\n")

    user = await User.find_one(User.user_code == user_code)
    if user is None:
        print(f"❌ User '{user_code}' not found.")
        return 1

    start_utc = _parse_date(from_s, end_of_day=False)
    end_utc = _parse_date(to_s, end_of_day=True)
    window = ""
    if start_utc or end_utc:
        window = f"  [window: {from_s or 'beginning'} → {to_s or 'now'}]"

    print("=" * 78)
    print(f"User              : {user.user_code} · {user.full_name or '—'}")
    print(f"Wallet ID         : (lookup)")
    wallet = await Wallet.find_one(Wallet.user_id == user.id)
    if wallet:
        print(f"  available_balance       : ₹{to_decimal(wallet.available_balance):,.2f}")
        print(f"  used_margin             : ₹{to_decimal(wallet.used_margin):,.2f}")
        print(f"  credit_limit            : ₹{to_decimal(wallet.credit_limit):,.2f}")
        print(f"  settlement_outstanding  : ₹{to_decimal(wallet.settlement_outstanding):,.2f}")
    else:
        print("  (no wallet row)")
    print("=" * 78)
    print(f"Money-side transactions{window}")
    print("=" * 78)

    money_types = [
        TransactionType.DEPOSIT,
        TransactionType.WITHDRAWAL,
        TransactionType.ADJUSTMENT,
        TransactionType.SETTLEMENT_OUTSTANDING_BOOKED,
        TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY,
    ]
    money_type_values = [t.value for t in money_types]

    q: dict = {
        "user_id": user.id,
        "transaction_type": {"$in": money_type_values},
    }
    if start_utc or end_utc:
        cf: dict = {}
        if start_utc:
            cf["$gte"] = start_utc
        if end_utc:
            cf["$lte"] = end_utc
        q["created_at"] = cf

    txns = await WalletTransaction.find(q).sort("+created_at").to_list()

    # Sub-totals
    sub = {
        "DEPOSIT": Decimal("0"),
        "WITHDRAWAL": Decimal("0"),
        "ADJUSTMENT+ (Add Fund)": Decimal("0"),
        "ADJUSTMENT+ (Initial balance)": Decimal("0"),
        "ADJUSTMENT+ (other narration)": Decimal("0"),
        "ADJUSTMENT- (Deduct Fund)": Decimal("0"),
        "SETTLEMENT_OUTSTANDING_BOOKED": Decimal("0"),
        "SETTLEMENT_OUTSTANDING_RECOVERY": Decimal("0"),
    }

    for t in txns:
        amt = to_decimal(t.amount)
        tt = t.transaction_type
        nar = t.narration or ""
        if tt == TransactionType.DEPOSIT:
            sub["DEPOSIT"] += abs(amt)
        elif tt == TransactionType.WITHDRAWAL:
            sub["WITHDRAWAL"] += abs(amt)
        elif tt == TransactionType.ADJUSTMENT:
            if amt > 0:
                if nar.startswith("Initial balance credit"):
                    sub["ADJUSTMENT+ (Initial balance)"] += amt
                elif "add fund" in nar.lower() or "credit" in nar.lower() or "deposit" in nar.lower():
                    sub["ADJUSTMENT+ (Add Fund)"] += amt
                else:
                    sub["ADJUSTMENT+ (other narration)"] += amt
            elif amt < 0:
                sub["ADJUSTMENT- (Deduct Fund)"] += abs(amt)
        elif tt == TransactionType.SETTLEMENT_OUTSTANDING_BOOKED:
            sub["SETTLEMENT_OUTSTANDING_BOOKED"] += abs(amt)
        elif tt == TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY:
            sub["SETTLEMENT_OUTSTANDING_RECOVERY"] += abs(amt)

    print("\nPer-type subtotals:")
    print("-" * 78)
    for k, v in sub.items():
        if v != 0:
            print(f"  {k:<40s} : ₹{v:>16,.2f}")
    print("-" * 78)

    print(f"\nRow-by-row dump ({len(txns)} txns):")
    print("-" * 78)
    print(f"{'Date (IST)':<19} {'Type':<35} {'Amount':>14}  Narration")
    print("-" * 78)
    for t in txns:
        amt = to_decimal(t.amount)
        when_ist = t.created_at.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S") if t.created_at else "—"
        tt_label = t.transaction_type.value
        if t.transaction_type == TransactionType.ADJUSTMENT:
            tt_label += " (+)" if amt > 0 else " (−)"
        nar = (t.narration or "").strip().replace("\n", " ")[:80]
        print(f"{when_ist:<19} {tt_label:<35} {_fmt(amt):>14}  {nar}")

    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Dump money-side wallet transactions for one user")
    ap.add_argument("user_code", help="e.g. CL62329114")
    ap.add_argument("--from", dest="from_date", help="YYYY-MM-DD (IST, inclusive)")
    ap.add_argument("--to", dest="to_date", help="YYYY-MM-DD (IST, inclusive)")
    args = ap.parse_args()

    try:
        rc = asyncio.run(main(args.user_code, args.from_date, args.to_date))
    finally:
        try:
            asyncio.run(close_database())
        except Exception:
            pass
    sys.exit(rc)
