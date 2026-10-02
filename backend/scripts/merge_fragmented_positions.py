"""Merge fragmented OPEN positions back into ONE per (user, token, product).

WHY: `apply_fill` is supposed to keep exactly ONE open Position per
(user_id, instrument.token, product_type) — every same-side fill merges into
it (weighted avg). But the weekly-settlement reopen path
(`weekly_settlement_service._settle_one_position`) creates a BRAND-NEW Position
doc per settled position instead of netting same-instrument ones, and any fresh
buy that fails to match those reopened docs adds yet another. Result: the
Positions tab shows the SAME stock as many separate rows (operator report:
CL59347510 had 7 VOLTAS NRML rows, all token 951809, avg 1302.40 — should be
one 440-qty line). ~17 such (user × token × product) groups exist platform-wide.

WHAT THIS DOES: for each OPEN group with >1 doc, folds them into a single
position:

    net_qty       = Σ signed quantity
    avg_price     = Σ(|qty_i| · avg_i) / Σ|qty_i|      (weighted-avg cost)
    margin_used   = Σ margin_used                       (matches wallet.used_margin)
    opening_qty   = Σ opening_quantity                  (peak size for the card)
    realized_pnl  = Σ realized_pnl                      (carry any booked slice)
    opened_at     = earliest                            (oldest lot's open time)
    SL / TP       = from the largest-qty lot            (don't invent a bracket)

The earliest doc is KEPT and updated; the rest are hard-deleted (their full
JSON is logged first, so a merge is reversible from the journal).

SAFETY:
  • MIXED-SIDE groups (some BUY + some SELL on the same token/product) are
    SKIPPED and flagged — merging opposite sides would realise P&L, which a
    data migration must never do silently. Handle those manually.
  • Dry-run by DEFAULT — prints the exact plan and changes nothing. Only
    `--apply` writes. Review the dry-run first.
  • After applying, each touched user's wallet `used_margin` is recomputed
    from the (now-consolidated) OPEN positions so the locked-margin total
    stays exactly correct.

USAGE (from backend/, venv active):
    python -m scripts.merge_fragmented_positions --user CL59347510          # dry-run, one user
    python -m scripts.merge_fragmented_positions --all                      # dry-run, everyone
    python -m scripts.merge_fragmented_positions --user CL59347510 --apply  # apply, one user
    python -m scripts.merge_fragmented_positions --all --apply              # apply, everyone
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import defaultdict
from decimal import Decimal

from bson import Decimal128

from app.core.database import close_database, init_database
from app.models.position import Position, PositionStatus
from app.models.user import User
from app.utils.decimal_utils import quantize_money, to_decimal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("merge_fragmented_positions")

ZERO = Decimal("0")


def _key(p: Position) -> tuple:
    return (str(p.user_id), p.instrument.token, p.product_type.value)


async def _load_open_groups(user_id=None) -> dict[tuple, list[Position]]:
    """Group OPEN positions by (user, token, product); keep only groups >1."""
    q = {"status": PositionStatus.OPEN.value}
    if user_id is not None:
        q["user_id"] = user_id
    groups: dict[tuple, list[Position]] = defaultdict(list)
    async for p in Position.find(q):
        groups[_key(p)].append(p)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _wavg(lots: list[Position], total_abs_qty: float) -> Decimal:
    """Weighted-average cost across lots (money in Decimal)."""
    if total_abs_qty <= 0:
        return ZERO
    s = ZERO
    for p in lots:
        s += to_decimal(p.avg_price) * to_decimal(abs(float(p.quantity or 0)))
    return s / to_decimal(total_abs_qty)


def _plan_merge(positions: list[Position]) -> dict | None:
    """Compute the netted result for a (user, token, product) group.

    Handles BOTH cases in one formula:
      • Same-side (all BUY or all SELL) → pure consolidation, realised = 0.
      • Mixed BUY+SELL → the overlapping quantity is CLOSED against itself;
        realised P&L = (sell_wavg − buy_wavg) × offset (same formula the
        matching engine books on a close), and the leftover keeps the larger
        side's cost basis. This is the P&L that SHOULD have been booked when
        the opposite trade landed but wasn't, because the position was
        fragmented instead of netted.
    """
    buys = [p for p in positions if float(p.quantity or 0) > 0]
    sells = [p for p in positions if float(p.quantity or 0) < 0]
    buy_qty = sum(float(p.quantity or 0) for p in buys)          # >= 0
    sell_qty = sum(-float(p.quantity or 0) for p in sells)       # magnitude >= 0
    if buy_qty <= 0 and sell_qty <= 0:
        return None

    buy_avg = quantize_money(_wavg(buys, buy_qty))
    sell_avg = quantize_money(_wavg(sells, sell_qty))
    offset = min(buy_qty, sell_qty)
    realized = (
        quantize_money((sell_avg - buy_avg) * to_decimal(offset)) if offset > 0 else ZERO
    )

    net_signed = buy_qty - sell_qty
    if net_signed > 0:
        net_side, net_avg = "BUY", buy_avg
    elif net_signed < 0:
        net_side, net_avg = "SELL", sell_avg
    else:
        net_side, net_avg = None, ZERO  # fully offset → flat

    # Keep the earliest-opened doc; bracket comes from the largest lot on the
    # SURVIVING (net) side so we don't attach a short's SL to a net long.
    keep = min(positions, key=lambda p: p.opened_at or p.id.generation_time)
    net_side_lots = buys if net_signed > 0 else (sells if net_signed < 0 else [])
    largest = max(net_side_lots, key=lambda p: abs(float(p.quantity or 0))) if net_side_lots else None

    # Net margin = the surviving side's locked margin, scaled to the leftover
    # qty (the offset portion's margin is released). Same-side groups have no
    # offset (net_side_qty == |net|), so the ratio is 1 → full sum preserved.
    # The KEPT doc might be a zero-margin SELL lot, so we MUST set this
    # explicitly — otherwise wallet used_margin would under-count after merge.
    net_side_qty = buy_qty if net_signed > 0 else (sell_qty if net_signed < 0 else 0.0)
    net_side_margin = sum((to_decimal(p.margin_used or 0) for p in net_side_lots), ZERO)
    net_margin = (
        quantize_money(net_side_margin * to_decimal(abs(net_signed)) / to_decimal(net_side_qty))
        if net_side_qty > 0
        else ZERO
    )

    return {
        "keep": keep,
        "drop": [p for p in positions if p.id != keep.id],
        "mixed": bool(buys and sells),
        "buy_qty": buy_qty,
        "buy_avg": buy_avg,
        "sell_qty": sell_qty,
        "sell_avg": sell_avg,
        "offset": offset,
        "realized": realized,
        "net_signed": net_signed,
        "net_side": net_side,
        "net_avg": net_avg,
        "margin": net_margin,
        "opening_qty": abs(net_signed),
        "stop_loss": largest.stop_loss if largest else None,
        "target": largest.target if largest else None,
    }


async def merge_one_group(key: tuple, positions: list[Position], apply: bool) -> str:
    uid, token, product = key
    sym = positions[0].instrument.symbol
    plan = _plan_merge(positions)
    if plan is None:
        logger.warning("  SKIP (no qty) %s %s/%s", sym, token, product)
        return "skipped"

    kind = "NET(mixed)" if plan["mixed"] else "MERGE(same-side)"
    result = "FLAT (net 0)" if plan["net_side"] is None else f'{plan["net_side"]} {abs(plan["net_signed"])} @ {plan["net_avg"]}'
    logger.info(
        "  %s %s %s/%s : %d docs → %s",
        kind, sym, token, product, len(positions), result,
    )
    logger.info(
        "      buy=%s@%s  sell=%s@%s  offset=%s  REALIZED_PnL=%s (→wallet)  net_margin=%s",
        plan["buy_qty"], plan["buy_avg"], plan["sell_qty"], plan["sell_avg"],
        plan["offset"], plan["realized"], plan["margin"],
    )
    for p in positions:
        logger.info(
            "      lot %s qty=%s avg=%s margin=%s opened=%s%s",
            str(p.id), float(p.quantity or 0), str(p.avg_price),
            str(p.margin_used), p.opened_at,
            "  <== KEEP" if p.id == plan["keep"].id else "",
        )

    if not apply:
        return "planned"

    # Log full JSON of every doc first so the whole net is reversible from logs.
    for p in positions:
        logger.info("      DUMP %s", json.dumps(p.model_dump(mode="json"), default=str))

    # ── Book the offset's realised P&L to the wallet (same as a real close) ──
    realized = plan["realized"]
    if realized != ZERO:
        from app.core.exceptions import InsufficientFundsError
        from app.models.transaction import TransactionType
        from app.services import wallet_service

        narration = (
            f"Position defragment {sym}: netted offset {plan['offset']} "
            f"(buy {plan['buy_avg']} vs sell {plan['sell_avg']})"
        )
        try:
            await wallet_service.adjust(
                uid, realized,
                transaction_type=TransactionType.PNL,
                narration=narration,
                reference_type="POSITION_DEFRAG",
                reference_id=str(plan["keep"].id),
            )
        except InsufficientFundsError:
            # Loss exceeds available — mirror the stop-out path: debit what we
            # can, overflow to settlement_outstanding. (Only for losses.)
            if realized < ZERO:
                await wallet_service.force_debit(
                    uid, -realized,
                    transaction_type=TransactionType.PNL,
                    narration=f"{narration} (shortfall → outstanding)",
                    reference_type="POSITION_DEFRAG",
                    reference_id=str(plan["keep"].id),
                )
            else:
                raise
        logger.info("      → booked realized P&L %s to wallet", realized)

    keep = plan["keep"]
    if plan["net_side"] is None:
        # Fully offset → no surviving position. Delete every doc.
        for p in positions:
            await p.delete()
        logger.info("      → FLAT: deleted all %d dup(s)", len(positions))
    else:
        from app.models._base import OrderAction

        keep.quantity = plan["net_signed"]
        keep.opened_side = OrderAction(plan["net_side"])
        keep.avg_price = Decimal128(str(plan["net_avg"]))
        keep.margin_used = Decimal128(str(plan["margin"]))  # net-side margin, scaled
        keep.realized_pnl = Decimal128("0")   # offset P&L went to the wallet above
        keep.opening_quantity = plan["opening_qty"]
        keep.stop_loss = plan["stop_loss"]
        keep.target = plan["target"]
        await keep.save()
        for p in plan["drop"]:
            await p.delete()
        logger.info(
            "      → kept %s as %s %s, deleted %d dup(s)",
            str(keep.id), plan["net_side"], abs(plan["net_signed"]), len(plan["drop"]),
        )
    return "merged"


async def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--user", help="single user code, e.g. CL59347510")
    g.add_argument("--all", action="store_true", help="scan every user")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="WRITE the merge (default is dry-run report only)",
    )
    ap.add_argument(
        "--recompute-margin",
        action="store_true",
        help="Skip merging; just re-sync wallet used_margin from the current "
             "open positions' margin_used (recovery after a fix to a position's "
             "margin). Adjusts available_balance by the delta.",
    )
    args = ap.parse_args()

    await init_database()
    try:
        uid = None
        if args.user:
            user = await User.find_one(User.user_code == args.user)
            if user is None:
                logger.error("User %s not found", args.user)
                return
            uid = user.id

        # ── Recompute-only recovery mode ─────────────────────────────────
        if args.recompute_margin:
            from app.services import wallet_service as ws
            targets = [uid] if uid is not None else [u.id for u in await User.find_all().to_list()]
            for u in targets:
                try:
                    res = await ws.recompute_used_margin(u)
                    if isinstance(res, dict) and res.get("changed"):
                        logger.info("  recomputed used_margin user=%s %s", str(u), res)
                except Exception:
                    logger.exception("  recompute_used_margin failed for %s", str(u))
            logger.info("Done (recompute-margin only).")
            return

        groups = await _load_open_groups(uid)
        logger.info(
            "Found %d fragmented (user × token × product) group(s) — mode=%s",
            len(groups), "APPLY" if args.apply else "DRY-RUN",
        )

        counts: dict[str, int] = defaultdict(int)
        touched_users: set = set()
        for key, positions in groups.items():
            outcome = await merge_one_group(key, positions, apply=args.apply)
            counts[outcome] += 1
            if outcome == "merged":
                touched_users.add(positions[0].user_id)

        # Re-sync locked margin for every user we actually changed.
        if args.apply and touched_users:
            from app.services import wallet_service as ws
            for u in touched_users:
                try:
                    await ws.recompute_used_margin(u)
                    logger.info("  recomputed used_margin for user %s", str(u))
                except Exception:
                    logger.exception("  recompute_used_margin failed for %s", str(u))

        logger.info(
            "Done. merged=%d planned=%d skipped=%d %s",
            counts["merged"], counts["planned"], counts["skipped"],
            "" if args.apply else "(run with --apply to execute — review REALIZED_PnL first!)",
        )
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
