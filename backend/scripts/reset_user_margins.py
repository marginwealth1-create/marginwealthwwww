"""One-shot recovery for margins corrupted by the pre-fix accumulation bug.

For every open Position we recompute `margin_used` from scratch:

    margin_used = |quantity| * avg_price * margin_pct * usd_inr_rate

Then for every Wallet we rebuild `used_margin` as the sum of its user's
open positions' `margin_used`. Run from the backend folder:

    cd /opt/marginwealth/backend
    source .venv/bin/activate
    python -m scripts.reset_user_margins

Idempotent — re-running just snaps numbers back to the canonical value.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from bson import Decimal128

from app.core.database import close_database, init_database
from app.models.position import Position, PositionStatus
from app.models.user import User
from app.models.wallet import Wallet
from app.services import netting_service
from app.services.market_data_service import (
    get_usd_inr_rate,
    is_usd_quoted_segment,
)
from app.utils.decimal_utils import quantize_money, to_decimal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reset_user_margins")


async def _effective_margin_pct(user: User, instrument_token: str, action: str, product: str) -> Decimal:
    """Pull the margin_percentage admin would actually enforce. Falls back
    to 100% (full margin) if the lookup fails."""
    try:
        eff = await netting_service.get_effective_settings(
            user_id=str(user.id),  # type: ignore[arg-type]
            instrument_token=instrument_token,
            action=action,
            product_type=product,
        )
        pct = float(eff.get("margin_percentage") or 100.0)
        return to_decimal(pct) / to_decimal(100)
    except Exception:
        return to_decimal(1)


async def main() -> None:
    await init_database()
    print("✅ MongoDB connected\n")

    usd_inr = to_decimal(get_usd_inr_rate())
    print(f"USD/INR rate: {usd_inr}\n")

    open_positions = await Position.find(Position.status == PositionStatus.OPEN).to_list()
    print(f"Open positions: {len(open_positions)}")

    # Index positions by user for the wallet-rebuild step below.
    by_user: dict[str, list[Position]] = {}
    for p in open_positions:
        user = await User.get(p.user_id)
        if user is None:
            continue
        mpct = await _effective_margin_pct(
            user, p.instrument.token, "BUY", p.product_type.value
        )
        qty_abs = to_decimal(abs(p.quantity))
        avg = to_decimal(p.avg_price)
        # Convert USD-quoted positions to INR
        fx = (
            usd_inr
            if (is_usd_quoted_segment(p.segment_type) or is_usd_quoted_segment(p.instrument.segment))
            else to_decimal(1)
        )
        new_margin = quantize_money(qty_abs * avg * mpct * fx)
        old_margin = to_decimal(p.margin_used)

        if new_margin != old_margin:
            print(
                f"  FIX {p.instrument.symbol:12} qty={p.quantity:>10} "
                f"old=₹{old_margin:>14,.2f}  new=₹{new_margin:>14,.2f}"
            )
            p.margin_used = Decimal128(str(new_margin))
            await p.save()
        else:
            print(
                f"  OK  {p.instrument.symbol:12} qty={p.quantity:>10} "
                f"margin=₹{old_margin:>14,.2f}"
            )

        by_user.setdefault(str(p.user_id), []).append(p)

    print()
    # Rebuild every wallet's used_margin from its open positions
    wallets = await Wallet.find_all().to_list()
    print(f"Wallets: {len(wallets)}")
    for w in wallets:
        positions = by_user.get(str(w.user_id), [])
        total = sum((to_decimal(p.margin_used) for p in positions), to_decimal(0))
        old_used = to_decimal(w.used_margin)
        if total != old_used:
            print(
                f"  FIX user={w.user_id}  old_used=₹{old_used:>14,.2f}  "
                f"new_used=₹{total:>14,.2f}  (sum of {len(positions)} positions)"
            )
            w.used_margin = Decimal128(str(quantize_money(total)))
            await w.save()
        else:
            print(f"  OK  user={w.user_id}  used=₹{old_used:>14,.2f}")

    print("\n✅ Reset complete. Restart backend if you ran this against a live system.")
    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
