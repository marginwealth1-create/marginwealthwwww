"""End-to-end money audit for a single user — find where the wallet
disagrees with the ledger / trades / positions.

Run from the backend folder:

    cd ~/marginwealth/backend
    source .venv/bin/activate
    python -m scripts.audit_user CL15362105

Read-only. The headline check is:

    (available_balance + used_margin)  ==  sum of every NON-settlement
                                           ledger row's signed amount

because margin block/release shuffle money WITHIN (available <-> used)
without a ledger row, so the two sides must net out. Any gap means money
moved without a ledger entry (proceeds bug, double credit/debit, manual
DB edit) — i.e. the wallet is wrong.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from decimal import Decimal

from app.core.database import close_database, init_database
from app.models.position import Position, PositionStatus
from app.models.trade import Trade
from app.models.transaction import (
    DepositRequest,
    DepositStatus,
    TransactionType,
    WalletTransaction,
    WithdrawalRequest,
    WithdrawalStatus,
)
from app.models.user import User
from app.services import wallet_service
from app.utils.decimal_utils import to_decimal

Z = Decimal("0")
_SETTLEMENT_TYPES = {
    TransactionType.SETTLEMENT_OUTSTANDING_BOOKED,
    TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY,
}


def _m(v) -> str:
    try:
        return f"{float(to_decimal(v)):,.2f}"
    except Exception:
        return str(v)


async def main() -> None:
    code = sys.argv[1] if len(sys.argv) > 1 else "CL15362105"
    await init_database()
    print(f"\n{'='*64}\nMONEY AUDIT — user_code = {code}\n{'='*64}")

    user = await User.find_one({"user_code": code})
    if user is None:
        print(f"❌ No user with user_code = {code}")
        await close_database()
        return
    uid = user.id
    print(f"USER : {user.full_name}  ({code})  id={uid}")
    print(f"  role={getattr(user,'role',None)} status={getattr(user,'status',None)} "
          f"auto_settlement={getattr(user,'auto_settlement',None)}")

    # ── Wallet snapshot ──────────────────────────────────────────────
    w = await wallet_service.get_or_create(uid)
    avail = to_decimal(w.available_balance)
    used = to_decimal(w.used_margin)
    credit = to_decimal(w.credit_limit)
    settle = to_decimal(w.settlement_outstanding)
    rpnl = to_decimal(w.realized_pnl)
    print(f"\n── WALLET (raw) {'─'*48}")
    print(f"  available_balance      = {_m(avail)}")
    print(f"  used_margin            = {_m(used)}")
    print(f"  credit_limit           = {_m(credit)}")
    print(f"  realized_pnl (tracker) = {_m(rpnl)}")
    print(f"  settlement_outstanding = {_m(settle)}")
    print(f"  total_deposits         = {_m(w.total_deposits)}")
    print(f"  total_withdrawals      = {_m(w.total_withdrawals)}")
    print(f"  total_brokerage        = {_m(w.total_brokerage)}")
    print(f"  total_charges          = {_m(w.total_charges)}")

    # ── Ledger, grouped by type ──────────────────────────────────────
    txns = (
        await WalletTransaction.find(WalletTransaction.user_id == uid)
        .sort("+created_at")
        .to_list()
    )
    by_type_sum: dict = defaultdict(lambda: Z)
    by_type_cnt: dict = defaultdict(int)
    for t in txns:
        tt = t.transaction_type
        by_type_sum[tt] += to_decimal(t.amount)
        by_type_cnt[tt] += 1
    print(f"\n── LEDGER BY TYPE ({len(txns)} rows) {'─'*36}")
    for tt in sorted(by_type_sum, key=lambda x: str(x)):
        print(f"  {str(tt):<32} cnt={by_type_cnt[tt]:<5} sum={_m(by_type_sum[tt])}")

    ledger_net = sum(
        (to_decimal(t.amount) for t in txns if t.transaction_type not in _SETTLEMENT_TYPES),
        Z,
    )
    settlement_booked = sum(
        (to_decimal(t.amount) for t in txns if t.transaction_type in _SETTLEMENT_TYPES),
        Z,
    )

    # ── HEADLINE RECONCILIATION ──────────────────────────────────────
    actual = avail + used
    delta = actual - ledger_net
    print(f"\n── RECONCILIATION {'─'*46}")
    print(f"  (available + used_margin)      = {_m(actual)}")
    print(f"  sum(non-settlement ledger amt) = {_m(ledger_net)}")
    print(f"  settlement-row amount sum      = {_m(settlement_booked)}")
    print(f"  ┌ DELTA (actual - ledger_net)  = {_m(delta)}")
    if abs(delta) < Decimal("0.01"):
        print("  └ ✅ wallet matches ledger — no money moved off-ledger.")
    else:
        sign = "MORE" if delta > 0 else "LESS"
        print(f"  └ ⚠️  MISMATCH: wallet has ₹{_m(abs(delta))} {sign} than the")
        print("       ledger explains. Money moved WITHOUT a ledger row →")
        print("       proceeds bug / double credit-debit / manual edit / margin")
        print("       release mismatch. THIS is the bug to chase.")

    # ── realized_pnl cross-check ─────────────────────────────────────
    pnl_ledger = by_type_sum.get(TransactionType.PNL, Z)
    pos_all = await Position.find(Position.user_id == uid).to_list()
    pos_realized = sum((to_decimal(p.realized_pnl) for p in pos_all), Z)
    trades = await Trade.find(Trade.user_id == uid).to_list()
    trade_pnl_inr = sum(
        (to_decimal(t.pnl_inr) for t in trades if getattr(t, "pnl_inr", None) is not None),
        Z,
    )
    trade_bkg = sum((to_decimal(t.brokerage) for t in trades), Z)
    print(f"\n── REALIZED P&L CROSS-CHECK {'─'*37}")
    print(f"  wallet.realized_pnl tracker        = {_m(rpnl)}")
    print(f"  sum(PNL ledger rows)               = {_m(pnl_ledger)}")
    print(f"  sum(PNL ledger) + settlement       = {_m(pnl_ledger + settlement_booked)}  (true realized incl. floored losses)")
    print(f"  sum(position.realized_pnl)         = {_m(pos_realized)}")
    print(f"  sum(Trade.pnl_inr, closing legs)   = {_m(trade_pnl_inr)}  (brokerage-folded, INR)")
    print(f"  sum(Trade.brokerage)               = {_m(trade_bkg)}")
    if abs(rpnl - pnl_ledger) >= Decimal("0.01"):
        print(f"  ⚠️  tracker vs PNL-ledger gap = {_m(rpnl - pnl_ledger)} "
              "(force_debit losses don't update the tracker — expected if stop-outs happened)")

    # ── Positions ────────────────────────────────────────────────────
    open_pos = [p for p in pos_all if p.status == PositionStatus.OPEN]
    closed_pos = [p for p in pos_all if p.status == PositionStatus.CLOSED]
    print(f"\n── POSITIONS  (open={len(open_pos)}  closed={len(closed_pos)}) {'─'*24}")
    for p in open_pos:
        print(f"  OPEN  {p.instrument.symbol:<16} qty={p.quantity} avg={_m(p.avg_price)} "
              f"uPnL={_m(p.unrealized_pnl)} margin={_m(p.margin_used)}")
    for p in sorted(closed_pos, key=lambda x: getattr(x, "closed_at", None) or 0)[-15:]:
        print(f"  CLOSED {p.instrument.symbol:<16} rPnL={_m(p.realized_pnl)} "
              f"reason={p.close_reason} closed_at={getattr(p,'closed_at',None)}")

    # ── Deposit / Withdrawal requests ────────────────────────────────
    deps = await DepositRequest.find(DepositRequest.user_id == uid).to_list()
    wds = await WithdrawalRequest.find(WithdrawalRequest.user_id == uid).to_list()
    dep_appr = sum((to_decimal(d.amount) for d in deps if d.status == DepositStatus.APPROVED), Z)
    wd_done = sum(
        (to_decimal(x.amount) for x in wds
         if x.status in (WithdrawalStatus.COMPLETED, WithdrawalStatus.APPROVED, WithdrawalStatus.PROCESSING)),
        Z,
    )
    print(f"\n── DEPOSIT / WITHDRAWAL REQUESTS {'─'*32}")
    print(f"  approved deposits (sum)   = {_m(dep_appr)}   ({len(deps)} requests total)")
    print(f"  approved/done withdrawals = {_m(wd_done)}   ({len(wds)} requests total)")
    print(f"  vs ledger DEPOSIT sum     = {_m(by_type_sum.get(TransactionType.DEPOSIT, Z))}")
    print(f"  vs ledger WITHDRAWAL sum  = {_m(by_type_sum.get(TransactionType.WITHDRAWAL, Z))}")
    if abs(dep_appr - by_type_sum.get(TransactionType.DEPOSIT, Z)) >= Decimal("0.01"):
        print("  ⚠️  approved deposits != ledger DEPOSIT credits — a deposit may "
              "not have been credited (or credited twice).")

    # ── Ledger continuity (find off-ledger jumps) ────────────────────
    print(f"\n── LEDGER CONTINUITY (gaps = margin shuffle OR bug) {'─'*13}")
    prev_after = None
    gap_total = Z
    breaks = []
    for t in txns:
        b = to_decimal(t.balance_before)
        a = to_decimal(t.balance_after)
        if prev_after is not None:
            gap = b - prev_after
            if abs(gap) >= Decimal("0.01"):
                gap_total += gap
                breaks.append((t, gap))
        prev_after = a
    print(f"  total off-ledger movement (Σ gaps) = {_m(gap_total)}  ({len(breaks)} jumps)")
    print("  (margin block lowers balance with no row; release raises it — "
          "so gaps are NORMAL. Look for ONE big unexplained jump.)")
    for t, gap in sorted(breaks, key=lambda x: abs(x[1]), reverse=True)[:8]:
        print(f"    gap={_m(gap):>14}  before={_m(t.balance_before)} "
              f"[{str(t.transaction_type)}] {(t.narration or '')[:46]}")

    # ── Last 20 ledger rows ──────────────────────────────────────────
    print(f"\n── LAST 20 LEDGER ROWS {'─'*41}")
    for t in txns[-20:]:
        print(f"  {str(getattr(t,'created_at',''))[:19]}  {str(t.transaction_type):<28} "
              f"amt={_m(t.amount):>13}  bal {_m(t.balance_before):>12} → {_m(t.balance_after):>12}")

    await close_database()
    print(f"\n{'='*64}\nDone (read-only).\n")


if __name__ == "__main__":
    asyncio.run(main())
