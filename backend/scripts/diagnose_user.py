"""Read-only diagnostic for a single user — why did stop-out not fire?

Dumps the exact wallet + open-position + risk-settings state the
risk_enforcer sees, and prints whether the stop-out evaluation would
even RUN (it early-returns when available+used_margin+credit_limit <= 0).

Run from the backend folder:

    cd ~/marginwealth/backend
    source .venv/bin/activate
    python -m scripts.diagnose_user CL45900793

Writes NOTHING — safe to run on production.
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal

from app.core.database import close_database, init_database
from app.models.position import Position, PositionStatus
from app.models.user import User
from app.services import market_data_service, netting_service, wallet_service
from app.utils.decimal_utils import to_decimal


def _money(v) -> str:
    try:
        return f"{float(to_decimal(v)):,.2f}"
    except Exception:
        return str(v)


async def main() -> None:
    code = sys.argv[1] if len(sys.argv) > 1 else "CL45900793"
    await init_database()
    print(f"\n✅ MongoDB connected — diagnosing user_code = {code}\n")

    user = await User.find_one({"user_code": code})
    if user is None:
        print(f"❌ No user found with user_code = {code}")
        await close_database()
        return

    uid = user.id
    print("─" * 60)
    print(f"USER     : {user.full_name}  ({code})")
    print(f"  id      : {uid}")
    print(f"  role    : {getattr(user, 'role', None)}")
    print(f"  status  : {getattr(user, 'status', None)}")
    print(f"  auto_settlement : {getattr(user, 'auto_settlement', None)}")
    print("─" * 60)

    # ── Wallet (raw fields) ──────────────────────────────────────────
    w = await wallet_service.get_or_create(uid)
    avail = to_decimal(w.available_balance)
    used = to_decimal(w.used_margin)
    credit = to_decimal(w.credit_limit)
    settle = to_decimal(w.settlement_outstanding)
    print("WALLET (raw):")
    print(f"  available_balance    : {_money(avail)}")
    print(f"  used_margin          : {_money(used)}")
    print(f"  credit_limit         : {_money(credit)}")
    print(f"  realized_pnl         : {_money(w.realized_pnl)}")
    print(f"  settlement_outstanding: {_money(settle)}")
    print("─" * 60)

    # ── The exact denominator risk_enforcer uses ─────────────────────
    denom = avail + used + credit  # == risk_enforcer._wallet_balance(w)
    print("RISK-ENFORCER STOP-OUT DENOMINATOR:")
    print(f"  balance = available + used_margin + credit_limit = {_money(denom)}")
    if denom <= 0:
        print("  ⚠️  balance <= 0  →  risk_enforcer RETURNS EARLY (line 527-528)")
        print("  ⚠️  STOP-OUT CHECK NEVER RUNS — this is the bug.")
    else:
        print("  ✓ balance > 0 → stop-out check would run.")
    print("─" * 60)

    # ── Open positions ───────────────────────────────────────────────
    open_positions = await Position.find(
        Position.user_id == uid, Position.status == PositionStatus.OPEN
    ).to_list()
    print(f"OPEN POSITIONS: {len(open_positions)}")
    total_unrealized = Decimal("0")
    total_pos_margin = Decimal("0")
    for p in open_positions:
        try:
            ltp = await market_data_service.get_ltp(p.instrument.token)
        except Exception:
            ltp = None
        upnl = to_decimal(p.unrealized_pnl)
        pmargin = to_decimal(p.margin_used)
        total_unrealized += upnl
        total_pos_margin += pmargin
        print(f"  • {p.instrument.symbol}  ({p.product_type})")
        print(f"      qty={p.quantity}  avg={_money(p.avg_price)}  ltp={_money(ltp) if ltp is not None else 'N/A'}")
        print(f"      unrealized_pnl = {_money(upnl)}   margin_used = {_money(pmargin)}")
        print(f"      stop_loss={p.stop_loss}  target={p.target}  segment={p.instrument.segment}")
    print(f"  → sum(unrealized_pnl) = {_money(total_unrealized)}")
    print(f"  → sum(position.margin_used) = {_money(total_pos_margin)}  (this is what reconcile sets used_margin to)")
    print("─" * 60)

    # ── Effective risk settings ──────────────────────────────────────
    try:
        risk = (await netting_service.get_effective_risk(str(uid)))["settings"]
        warn_pct = float(risk.get("stopOutWarningPercent") or 0)
        stop_pct = float(risk.get("stopOutPercent") or 0)
        print("EFFECTIVE RISK SETTINGS:")
        print(f"  stopOutWarningPercent : {warn_pct}")
        print(f"  stopOutPercent        : {stop_pct}")
        floating_loss = (-total_unrealized) if total_unrealized < 0 else Decimal("0")
        if denom > 0 and floating_loss > 0:
            loss_pct = float(floating_loss / denom * Decimal(100))
            print(f"  → loss_pct (if it ran) = {loss_pct:.2f}%  vs stop {stop_pct}%")
            print(f"     would_fire = {loss_pct >= stop_pct and stop_pct > 0}")
        else:
            print(f"  → loss_pct NOT COMPUTABLE: denom={_money(denom)}, floating_loss={_money(floating_loss)}")
            print("     (denom<=0 is exactly why stop-out was skipped)")
    except Exception as e:
        print(f"  (risk settings lookup failed: {e})")
    print("─" * 60)

    # ── Wallet summary (what the admin/app card shows) ───────────────
    try:
        s = await wallet_service.summary(uid)
        print("WALLET SUMMARY (admin/app card source):")
        print(f"  bal              : {s.get('bal')}")
        print(f"  equity           : {s.get('equity')}")
        print(f"  margin           : {s.get('margin')}")
        print(f"  margin_level_pct : {s.get('margin_level_pct')}")
        print(f"  open_unrealized_pnl : {s.get('open_unrealized_pnl')}")
    except Exception as e:
        print(f"  (summary failed: {e})")
    print("─" * 60)

    await close_database()
    print("\nDone (read-only — nothing was modified).\n")


if __name__ == "__main__":
    asyncio.run(main())
