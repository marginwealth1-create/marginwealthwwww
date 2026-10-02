"""One-off, single-trade display correction for a mis-keyed admin close.

Scope: edits EXACTLY ONE Trade row (matched by trade_number) so the user's
Closed / History tab shows the size + realized P&L the admin intends. The
Closed tab reconstructs its rows from Trade fills via
`list_closed_trade_events_fifo`, which for this trade hits the empty-queue
algebraic path:

    displayed_gross (REALIZED P&L column) = trade.pnl_inr + trade.brokerage
    displayed_qty                          = trade.quantity

so to show qty=Q and gross=G we set:

    trade.quantity  = Q
    trade.pnl_inr   = G - trade.brokerage

WHAT THIS DOES NOT TOUCH
------------------------
* The FIFO code / system — untouched (this only edits data for ONE user).
* The wallet / ledger — WalletTransaction rows are historical and are NOT
  recomputed from Trade.pnl_inr. The wallet was already reconciled (audit
  delta 0.00), so editing this trade is display-only.
* Any other trade — this closing fill was processed against an EMPTY FIFO
  queue (post-reopen), so it emits a standalone row and pairs with no
  opening fill; changing its qty/pnl affects only its own Closed-tab row.

Usage (backend/, venv active):
    # preview:
    python -m scripts.fix_one_trade_display T2608289E873292 6250 -16500
    # apply:
    python -m scripts.fix_one_trade_display T2608289E873292 6250 -16500 --apply

Args: <trade_number> <new_qty> <target_gross_pnl> [--apply]
`target_gross_pnl` is the REALIZED P&L column value you want shown (gross,
i.e. inclusive of brokerage), e.g. -16500 for a 16,500 loss.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

from bson import Decimal128

from app.core.database import close_database, init_database
from app.models._base import OrderAction
from app.models.trade import Trade


def _d(x) -> Decimal:
    return Decimal(str(x))


async def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    if len(args) < 3:
        print("usage: python -m scripts.fix_one_trade_display "
              "<trade_number> <new_qty> <target_gross_pnl> [--apply]")
        return

    trade_number = args[0]
    new_qty = _d(args[1])
    target_gross = _d(args[2])

    await init_database()
    try:
        t = await Trade.find_one(Trade.trade_number == trade_number)
        if t is None:
            print(f"[abort] no trade with trade_number = {trade_number}")
            return

        old_price = _d(str(t.price))
        # Scale brokerage proportionally to the new qty so the BROKERAGE
        # column stays consistent with the smaller size.
        old_qty = _d(str(t.quantity))
        old_brk = _d(str(t.brokerage))
        new_brk = (old_brk * new_qty / old_qty).quantize(Decimal("0.01")) if old_qty != 0 else old_brk
        new_value = (old_price * new_qty).quantize(Decimal("0.01"))
        # displayed gross = pnl_inr + brokerage  ⇒  pnl_inr = gross - brokerage
        new_pnl = (target_gross - new_brk).quantize(Decimal("0.01"))
        is_buy = t.action == OrderAction.BUY
        # net_amount: BUY pays value+charges, SELL receives value-charges.
        new_net = (new_value + new_brk) if is_buy else (new_value - new_brk)
        new_net = new_net.quantize(Decimal("0.01"))

        print(f"TRADE {t.trade_number}  ({t.instrument.symbol}, {t.action.value})")
        print(f"  quantity     {t.quantity}  ->  {new_qty}")
        print(f"  price        {t.price}  (unchanged)")
        print(f"  value        {t.value}  ->  {new_value}")
        print(f"  brokerage    {t.brokerage}  ->  {new_brk}")
        print(f"  total_charges{t.total_charges}  ->  {new_brk}")
        print(f"  net_amount   {t.net_amount}  ->  {new_net}")
        print(f"  pnl_inr      {t.pnl_inr}  ->  {new_pnl}")
        print(f"\n  => Closed-tab will show qty={new_qty}, "
              f"REALIZED P&L={new_pnl + new_brk} (gross)")

        if not apply:
            print("\n[dry-run] nothing written. add --apply to commit.")
            return

        t.quantity = float(new_qty)
        t.value = Decimal128(str(new_value))
        t.brokerage = Decimal128(str(new_brk))
        t.total_charges = Decimal128(str(new_brk))
        t.net_amount = Decimal128(str(new_net))
        t.pnl_inr = Decimal128(str(new_pnl))
        await t.save()

        fresh = await Trade.find_one(Trade.trade_number == trade_number)
        print(f"\n[applied] quantity={fresh.quantity}  pnl_inr={fresh.pnl_inr}  "
              f"brokerage={fresh.brokerage}")
        print(f"[applied] Closed-tab gross now = "
              f"{_d(str(fresh.pnl_inr)) + _d(str(fresh.brokerage))}")
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
