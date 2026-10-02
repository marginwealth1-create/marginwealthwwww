"""Align mixed-product OPEN positions on the SAME instrument into ONE book.

WHY: netting keys on (user_id, instrument.token, product_type), so a stock a
user carried MIS->NRML at yesterday's EOD PLUS a fresh MIS order placed today
shows as TWO rows (one MIS, one NRML) on the Positions page. `apply_fill`
correctly keeps different product books separate, and the (user, token, product)
merge script / EOD consolidation never fold ACROSS products. Operator decision:
one product book per stock — a fresh order must net into the carried position.

Going forward `order_service.place_order` adopts an existing open position's
product_type so this never happens again. This one-off cleans up the rows that
already exist.

WHAT THIS DOES (per user): for every token that has MORE THAN ONE distinct
product_type open, it converts each non-NRML lot to NRML using the SAME
margin-aware logic the EOD `convert_intraday_to_carry` rollover uses
(re-resolve overnight margin, block/release the wallet delta), then calls
`consolidate_open_positions` to weighted-average the now-same-product lots into
a single row and re-sync wallet used_margin.

SAFETY:
  • Dry-run by DEFAULT — prints the exact plan and changes nothing. Only
    `--apply` writes. Review the dry-run first.
  • A lot whose overnight (NRML) margin the wallet CANNOT cover is SKIPPED and
    flagged — NOT force-closed (a manual mid-day cleanup must never silently
    liquidate a user's position; run the EOD path or top up funds instead).
  • Only tokens with >1 distinct product type are touched. Single-product
    stocks and users with no mixed books are a no-op.

USAGE (from backend/, venv active):
    python -m scripts.align_mixed_product_positions --user CL59347510          # dry-run
    python -m scripts.align_mixed_product_positions --user CL59347510 --apply  # apply
    python -m scripts.align_mixed_product_positions --all                      # dry-run, everyone
    python -m scripts.align_mixed_product_positions --all --apply              # apply, everyone
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import defaultdict
from decimal import Decimal

from bson import Decimal128

from app.core.database import close_database, init_database
from app.models._base import ProductType
from app.models.position import Position, PositionStatus
from app.models.user import User
from app.utils.decimal_utils import quantize_money, to_decimal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("align_mixed_product_positions")

ZERO = Decimal("0")


async def _resolve_nrml_margin(pos: Position) -> Decimal | None:
    """Overnight (NRML) margin requirement for `pos`, mirroring the exact
    formula in position_service.convert_intraday_to_carry. Returns None on a
    resolver failure (caller then skips the lot)."""
    from app.services import netting_service
    from app.services.market_data_service import (
        get_usd_inr_rate,
        is_usd_quoted_segment,
    )

    _osym = (pos.instrument.symbol or "").upper()
    _otype = (
        ("CE" if _osym.endswith("CE") else "PE" if _osym.endswith("PE") else None)
        if len(_osym) >= 3 and _osym[-3].isdigit()
        else None
    )
    try:
        resolved = await netting_service.get_effective_settings(
            pos.user_id,
            pos.instrument.segment,
            action="BUY" if pos.quantity >= 0 else "SELL",
            option_type=_otype,
            product_type="NRML",
            symbol=pos.instrument.symbol,
        )
    except Exception:  # noqa: BLE001
        return None
    s = resolved.get("settings") or {}

    cur_avg = to_decimal(pos.avg_price)
    cur_qty_abs = to_decimal(abs(pos.quantity))
    notional = cur_avg * cur_qty_abs

    ovn_fixed_per_lot = to_decimal(s.get("overnight_fixed_margin_per_lot") or 0)
    if (s.get("margin_calc_mode") == "fixed") and ovn_fixed_per_lot > 0:
        lot_size = max(1, int(pos.instrument.lot_size or 1))
        lots = cur_qty_abs / to_decimal(lot_size)
        new_margin = ovn_fixed_per_lot * lots
    else:
        ovn_margin_pct = to_decimal(s.get("overnight_margin_percentage") or 100.0) / to_decimal(100)
        ovn_leverage = to_decimal(s.get("overnight_leverage") or 1.0) or to_decimal(1)
        new_margin = notional * ovn_margin_pct / ovn_leverage

    if (
        is_usd_quoted_segment(pos.segment_type)
        or is_usd_quoted_segment(pos.instrument.segment)
    ):
        if not ((s.get("margin_calc_mode") == "fixed") and ovn_fixed_per_lot > 0):
            new_margin = new_margin * to_decimal(get_usd_inr_rate())

    return quantize_money(new_margin)


async def _align_user(uid, apply: bool) -> dict[str, int]:
    from app.models._base import OrderAction
    from app.services import wallet_service

    opens = await Position.find(
        Position.user_id == uid,
        Position.status == PositionStatus.OPEN,
    ).to_list()

    by_token: dict[str, list[Position]] = defaultdict(list)
    for p in opens:
        by_token[p.instrument.token].append(p)

    merged_tokens = skipped = 0
    touched = False

    for token, lots in by_token.items():
        distinct = {p.product_type for p in lots}
        if len(distinct) <= 1:
            continue  # single product book — nothing to align

        sym = lots[0].instrument.symbol
        logger.info(
            "  MIXED %s token=%s products=%s (%d rows)",
            sym, token, sorted(pt.value for pt in distinct), len(lots),
        )

        # Same-side only. A BUY+SELL mix on the same token would realise P&L
        # on merge — a data cleanup must never do that silently; skip + flag.
        buys = [p for p in lots if float(p.quantity or 0) > 0]
        sells = [p for p in lots if float(p.quantity or 0) < 0]
        if buys and sells:
            logger.warning(
                "    SKIP (BUY+SELL mix — would realise P&L) %s token=%s", sym, token,
            )
            skipped += 1
            continue

        total_signed = sum(float(p.quantity or 0) for p in lots)
        total_abs = abs(total_signed)
        if total_abs <= 0:
            logger.warning("    SKIP (net qty 0) %s token=%s", sym, token)
            skipped += 1
            continue

        # Weighted-average cost across every lot (same-side).
        wsum = ZERO
        for p in lots:
            wsum += to_decimal(p.avg_price) * to_decimal(abs(float(p.quantity or 0)))
        wavg = quantize_money(wsum / to_decimal(total_abs))

        # KEEPER = an existing NRML lot if present (its id/opened_at stay),
        # else the earliest lot (which we retype to NRML). We update ONE doc
        # and delete the rest, so the unique index is never violated (no
        # transient second NRML row — that was the E11000 the flip hit).
        nrml_lots = [p for p in lots if p.product_type == ProductType.NRML]
        keeper = (
            min(nrml_lots, key=lambda p: p.opened_at or p.id.generation_time)
            if nrml_lots
            else min(lots, key=lambda p: p.opened_at or p.id.generation_time)
        )
        drop = [p for p in lots if p.id != keeper.id]

        # Resolve NRML margin for the MERGED size (compute on a temp copy of
        # the keeper carrying the merged qty + wavg).
        keeper.quantity = total_signed
        keeper.avg_price = Decimal128(str(wavg))
        keeper.product_type = ProductType.NRML
        new_margin = await _resolve_nrml_margin(keeper)
        if new_margin is None:
            logger.warning("    SKIP (margin resolve failed) %s token=%s", sym, token)
            skipped += 1
            continue

        logger.info(
            "    MERGE %s token=%s -> NRML qty=%s @ %s margin=%s (keep %s, drop %d)",
            sym, token, total_signed, str(wavg), str(new_margin),
            str(keeper.id), len(drop),
        )
        if not apply:
            continue

        keeper.margin_used = Decimal128(str(new_margin))
        keeper.opened_side = OrderAction.BUY if total_signed > 0 else OrderAction.SELL
        keeper.opening_quantity = total_abs
        keeper.realized_pnl = Decimal128("0")
        # Drop the losers FIRST so the keeper.save() can never collide with a
        # sibling NRML row on the (user, token, NRML) unique index.
        for p in drop:
            await p.delete()
        await keeper.save()
        merged_tokens += 1
        touched = True

    # Re-sync wallet used_margin from the (now-merged) open positions. This
    # applies the NRML margin increase to the wallet in ONE consistent step —
    # no per-lot block_margin that could leak if a later write fails.
    if apply and touched:
        try:
            await wallet_service.recompute_used_margin(uid)
        except Exception:  # noqa: BLE001
            logger.exception("  recompute_used_margin failed user=%s", str(uid))

    return {"converted": merged_tokens, "skipped": skipped, "tokens": merged_tokens}


async def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--user", help="single user code, e.g. CL59347510")
    g.add_argument("--all", action="store_true", help="scan every user")
    ap.add_argument("--apply", action="store_true", help="WRITE (default is dry-run)")
    args = ap.parse_args()

    await init_database()
    try:
        if args.user:
            user = await User.find_one(User.user_code == args.user)
            if user is None:
                logger.error("User %s not found", args.user)
                return
            uids = [user.id]
        else:
            uids = [u.id for u in await User.find_all().to_list()]

        logger.info("Aligning mixed-product positions — mode=%s users=%d",
                    "APPLY" if args.apply else "DRY-RUN", len(uids))

        totals = {"converted": 0, "skipped": 0, "tokens": 0}
        for uid in uids:
            res = await _align_user(uid, apply=args.apply)
            for k in totals:
                totals[k] += res[k]

        logger.info(
            "Done. converted=%d skipped=%d consolidated_tokens=%d %s",
            totals["converted"], totals["skipped"], totals["tokens"],
            "" if args.apply else "(run with --apply to execute — review the plan first!)",
        )
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
