"""Backfill `opened_side` on positions that pre-date the field.

Before the fix, the Position document did NOT carry the opening direction
explicitly — the UI derived the side from `quantity > 0 ? BUY : SELL`. That
worked while the position was open but defaulted every fully-closed row
(quantity == 0) to SELL, so a closed BUY trade rendered as "SELL" on the
Closed tab.

The fix added an `opened_side` field that's stamped at insert / reopen /
flip and stays stable on close. This script back-populates that field for
every Position that doesn't have it yet:

  • For OPEN positions: derive from current `quantity` sign — it's accurate
    while the row is still open.
  • For CLOSED positions (quantity == 0): look up the earliest Trade row
    for the same (user, instrument.token, product_type) executed within
    [opened_at, closed_at] and use its action.

Idempotent — only writes when `opened_side` is currently None.

Run from the backend folder:

    cd /opt/marginwealth/backend
    source .venv/bin/activate
    python -m scripts.backfill_position_opened_side

Dry-run preview (no writes):

    python -m scripts.backfill_position_opened_side --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import timedelta

from app.core.database import close_database, init_database
from app.models._base import OrderAction
from app.models.position import Position, PositionStatus
from app.models.trade import Trade

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_opened_side")


async def _infer_from_trades(pos: Position) -> OrderAction | None:
    """Find the earliest trade that contributed to this position and return
    its action. Looks within the position's opened_at→closed_at window plus
    a small slop, scoped by user + instrument + product_type."""
    if pos.opened_at is None:
        return None
    window_start = pos.opened_at - timedelta(seconds=5)
    window_end = (pos.closed_at or pos.opened_at) + timedelta(seconds=5)
    earliest = (
        await Trade.find(
            {
                "user_id": pos.user_id,
                "instrument.token": pos.instrument.token,
                "product_type": pos.product_type.value,
                "executed_at": {"$gte": window_start, "$lte": window_end},
            }
        )
        .sort("+executed_at")
        .limit(1)
        .to_list()
    )
    if not earliest:
        return None
    return earliest[0].action


async def _main(dry_run: bool) -> None:
    await init_database()
    try:
        rows = await Position.find({"opened_side": None}).to_list()
        logger.info("found %d positions without opened_side", len(rows))

        backfilled_open = 0
        backfilled_closed_from_qty = 0
        backfilled_closed_from_trade = 0
        unresolved = 0

        for pos in rows:
            inferred: OrderAction | None = None

            if pos.quantity > 0:
                inferred = OrderAction.BUY
                if pos.status == PositionStatus.OPEN:
                    backfilled_open += 1
                else:
                    backfilled_closed_from_qty += 1
            elif pos.quantity < 0:
                inferred = OrderAction.SELL
                if pos.status == PositionStatus.OPEN:
                    backfilled_open += 1
                else:
                    backfilled_closed_from_qty += 1
            else:
                # quantity == 0 → fully closed; can't derive from sign.
                inferred = await _infer_from_trades(pos)
                if inferred is not None:
                    backfilled_closed_from_trade += 1
                else:
                    unresolved += 1
                    logger.warning(
                        "could not infer opened_side for position %s (user=%s token=%s)",
                        pos.id,
                        pos.user_id,
                        pos.instrument.token,
                    )

            if inferred is not None and not dry_run:
                pos.opened_side = inferred
                await pos.save()

        logger.info(
            "summary: open=%d  closed_from_qty=%d  closed_from_trade=%d  unresolved=%d  dry_run=%s",
            backfilled_open,
            backfilled_closed_from_qty,
            backfilled_closed_from_trade,
            unresolved,
            dry_run,
        )
    finally:
        await close_database()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(_main(args.dry_run))
