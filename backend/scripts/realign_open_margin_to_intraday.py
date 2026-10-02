"""Realign open positions' `margin_used` to the INTRADAY lock.

Companion to the "USED stays intraday through carry" model change
(commit c60569e). Positions that were carried through an EOD rollover
*before* that change were re-locked to the OVERNIGHT (CF) margin, so their
`margin_used` sits at ~70×-tier instead of the intraday ~700×-tier the new
model wants USED to show. This one-shot snaps every open MIS/NRML position
back to the intraday margin the order validator would lock at entry, then
reconciles each affected wallet (the released margin flows back to
available_balance via `recompute_used_margin`).

Formula matches order_validator.validate — the resolver's product-aware
`leverage` / `margin_percentage` (intraday tier in Times mode) or fixed
per-lot, with the USD→INR conversion for USD-quoted segments.

Run from the backend folder (dry-run first, then apply):

    cd /opt/marginwealth/backend && source .venv/bin/activate
    python -m scripts.realign_open_margin_to_intraday --user CL47940000
    python -m scripts.realign_open_margin_to_intraday --user CL47940000 --apply
    python -m scripts.realign_open_margin_to_intraday --apply          # ALL users

Idempotent — re-running just snaps numbers to the same canonical value.
Safe: it only ever RELEASES over-locked margin back to available (USED
drops), never debits real money; stop-out uses available+used+credit which
is invariant to the split.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from bson import Decimal128

from app.core.database import close_database, init_database
from app.models._base import ProductType
from app.models.position import Position, PositionStatus
from app.models.user import User
from app.services import netting_service, wallet_service
from app.services.market_data_service import (
    get_usd_inr_rate,
    is_usd_quoted_segment,
)
from app.utils.decimal_utils import quantize_money, to_decimal

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("realign_open_margin")

_CARRY_PRODUCTS = {ProductType.MIS.value, ProductType.NRML.value}


def _option_type(symbol: str | None) -> str | None:
    s = (symbol or "").upper()
    if len(s) >= 3 and s[-3:-2].isdigit():
        return "CE" if s.endswith("CE") else "PE" if s.endswith("PE") else None
    return None


async def _intraday_margin(user_id, pos) -> "Decimal128 | None":
    """The intraday margin the validator would lock at entry for this
    position (same resolver, same math). None if the lookup fails."""
    try:
        resolved = await netting_service.get_effective_settings(
            user_id,
            pos.instrument.segment,
            action="BUY" if pos.quantity >= 0 else "SELL",
            option_type=_option_type(pos.instrument.symbol),
            product_type=pos.product_type.value,
            symbol=pos.instrument.symbol,
        )
    except Exception:  # noqa: BLE001
        return None
    s = resolved.get("settings") or {}
    notional = to_decimal(pos.avg_price) * to_decimal(abs(pos.quantity))
    fixed = to_decimal(s.get("fixed_margin_per_lot") or 0)
    if (s.get("margin_calc_mode") == "fixed") and fixed > 0:
        lot_size = max(1, int(pos.instrument.lot_size or 1))
        lots = to_decimal(abs(pos.quantity)) / to_decimal(lot_size)
        margin = fixed * lots
    else:
        lev = to_decimal(s.get("leverage") or 1) or to_decimal(1)
        pct = to_decimal(s.get("margin_percentage") or 100) / to_decimal(100)
        margin = notional * pct / lev
        if is_usd_quoted_segment(pos.segment_type) or is_usd_quoted_segment(
            pos.instrument.segment
        ):
            margin = margin * to_decimal(get_usd_inr_rate())
    return quantize_money(margin)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="user_code to limit to (default: all users)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    await init_database()
    print("✅ MongoDB connected  (mode: %s)\n" % ("APPLY" if args.apply else "DRY-RUN"))

    q = {
        "status": PositionStatus.OPEN.value,
        "product_type": {"$in": list(_CARRY_PRODUCTS)},
    }
    if args.user:
        u = await User.find_one(User.user_code == args.user)
        if not u:
            print("❌ no user with code %s" % args.user)
            await close_database()
            return
        q["user_id"] = u.id

    rows = await Position.find(q).to_list()
    print("%d open MIS/NRML positions in scope\n" % len(rows))
    print("%-20s %-5s %11s %11s %11s" % ("SYMBOL", "PT", "USED_old", "intraday", "delta"))

    affected: set = set()
    changed = 0
    for p in rows:
        intr = await _intraday_margin(p.user_id, p)
        if intr is None:
            print("  %-18s  SKIP (resolve failed)" % (p.instrument.symbol or "")[:18])
            continue
        old = to_decimal(p.margin_used or 0)
        new = to_decimal(intr)
        print(
            "%-20s %-5s %11.2f %11.2f %11.2f"
            % ((p.instrument.symbol or "")[:20], p.product_type.value, float(old), float(new), float(new - old))
        )
        if new != old:
            if args.apply:
                p.margin_used = Decimal128(str(new))
                await p.save()
            affected.add(p.user_id)
            changed += 1

    print("\n%d positions %s; %d wallets to reconcile"
          % (changed, "updated" if args.apply else "would change", len(affected)))

    if args.apply:
        for uid in affected:
            res = await wallet_service.recompute_used_margin(uid)
            print("  wallet %s: used %s -> %s (delta %s)"
                  % (uid, res.get("before_used"), res.get("after_used"), res.get("delta")))
        print("\n✅ done")
    else:
        print("\n(dry-run — re-run with --apply to write)")

    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
