"""One-off: settle three legacy undo refunds the Billing statement check found.

  2N4GW5  +51.88   31 Oct: an admin reopen undid the AUTO close of
                   NATURALGAS26NOVFUT and refunded nothing — the close's P&L
                   was 0, and the reopen of the day refunded nothing then — so
                   the close's charge stayed on the wallet.
  4WDYC6  +240.05  The same on five closes (NYKAA, RPOWER, SBILIFE).
  U87KPE  -102.78  13 Nov: two admin deletes of BTCUSD both refunded the first
                   position's opening charge (before P4).

Each fill is checked first: no longer live, charged exactly that. One
REVERSAL row per client (reference CORRECTION / LEGACY_UNDO_<code>), so a
re-run is a no-op. Dry-run unless --apply.

    cd /opt/marginwealth/backend && source .venv/bin/activate
    python -m scripts.correct_legacy_undo_refunds          # dry run
    python -m scripts.correct_legacy_undo_refunds --apply
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from bson import ObjectId

from app.core.database import close_database, init_database
from app.models.trade import LIVE_FILL, Trade
from app.models.transaction import TransactionType, WalletTransaction
from app.models.user import User
from app.services import wallet_service
from app.utils.decimal_utils import to_decimal

CASES = [
    (
        "2N4GW5",
        "51.88",
        ["6ae58741083cf923faa5ec19"],
        "closing charge of NATURALGAS26NOVFUT refunded — its close was undone on 31 Oct",
    ),
    (
        "4WDYC6",
        "240.05",
        [
            "6ae58741083cf923faa5ec31",
            "6ae58742083cf923faa5ec34",
            "6ae58742083cf923faa5ec37",
            "6ae58742083cf923faa5ec3a",
            "6ae58742083cf923faa5ec3d",
        ],
        "closing charges of NYKAA26NOV360CE, RPOWER, NYKAA26NOV370CE, SBILIFE26NOV1780CE and "
        "SBILIFE26NOV1760CE refunded — their closes were undone on 31 Oct",
    ),
    (
        "U87KPE",
        "-102.78",
        ["6af6c93192196cf99315e876"],
        "opening charge of BTCUSD refunded twice by the deletes of 13 Nov, taken back once",
    ),
]


async def main(apply: bool) -> None:
    await init_database()
    try:
        for code, amount, fills, why in CASES:
            user = await User.find_one(User.user_code == code)
            ref = f"LEGACY_UNDO_{code}"
            if await WalletTransaction.find_one(
                {"user_id": user.id, "reference_type": "CORRECTION", "reference_id": ref}
            ):
                print(f"{code}: already corrected")
                continue
            ids = [ObjectId(f) for f in fills]
            trades = await Trade.find({"_id": {"$in": ids}, "user_id": user.id}).to_list()
            charged = sum((to_decimal(t.total_charges) for t in trades), Decimal(0))
            live = await Trade.find({"_id": {"$in": ids}, **LIVE_FILL}).count()
            if len(trades) != len(ids) or live or charged != abs(Decimal(amount)):
                print(f"{code}: SKIPPED — fills {len(trades)}/{len(ids)}, live {live}, charged {charged}")
                continue
            print(f"{code}: {'+' if Decimal(amount) > 0 else ''}{amount} — {why}")
            if apply:
                await wallet_service.adjust(
                    user.id,
                    Decimal(amount),
                    transaction_type=TransactionType.REVERSAL,
                    narration=f"Correction: {why}",
                    reference_type="CORRECTION",
                    reference_id=ref,
                )
        if not apply:
            print("dry run — nothing written; --apply to write")
    finally:
        await close_database()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    asyncio.run(main(ap.parse_args().apply))
