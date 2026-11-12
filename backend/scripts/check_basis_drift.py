"""Per client, per script: does the dashboard's P&L and the billing P&L agree
with the cash that actually changed hands?

Three numbers, all GROSS (before brokerage) and in the instrument's NATIVE
currency, so USD scripts compare without an FX rate:

  oracle  sum(sell value) - sum(buy value) over the live fills. Only
          meaningful when the script is FLAT (bought units == sold units and
          no OPEN position), and then it is the arbiter.
  dash    sum(Position.realized_pnl) on that script — what the wallet was
          charged and what the Accounts dashboard sums.
  bill    sum(gross_pnl) of the FIFO closed blotter — what the billing PDF
          (tradebook_builder.fifo_closed_rows) prints.

On a flat script it names which of dash/bill is wrong. On an open script it
can only report the dash-vs-bill gap. Weekly settlement closes and re-opens
at one price with no fills, so it nets out of the oracle; an expiry
settlement leaves the fills unbalanced and the script shows as not flat.

Run from `backend/`:

    source .venv/bin/activate
    python -m scripts.check_basis_drift                 # 40 most active clients
    python -m scripts.check_basis_drift --limit 0       # every client
    python -m scripts.check_basis_drift --user CL62329114
    python -m scripts.check_basis_drift --json /tmp/drift_before.json

Read-only. Makes no DB writes — safe to run on production. Exits 1 when a
flat script disagrees with its oracle, so it can run on cron.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from app.core.database import close_database, init_database
from app.models.position import Position
from app.models.trade import Trade
from app.models.user import User, UserRole
from app.services import position_service
from app.utils.decimal_utils import to_decimal

TOL = Decimal("1")  # rupee/dollar tolerance for rounding across many fills


def _verdict(flat: bool, oracle: Decimal | None, dash: Decimal, bill: Decimal) -> str:
    if not flat or oracle is None:
        return "ok" if abs(dash - bill) <= TOL else "open-gap"
    dash_ok = abs(dash - oracle) <= TOL
    bill_ok = abs(bill - oracle) <= TOL
    if dash_ok and bill_ok:
        return "ok"
    if dash_ok:
        return "BILL-WRONG"
    if bill_ok:
        return "DASH-WRONG"
    return "BOTH-WRONG"


async def check_user(user: User) -> list[dict]:
    fills = await Trade.find(
        Trade.user_id == user.id, Trade.superseded_by_reopen != True  # noqa: E712
    ).to_list()
    if not fills:
        return []

    by_tok: dict[str, dict] = defaultdict(
        lambda: {"symbol": "", "buy_q": 0.0, "sell_q": 0.0,
                 "buy_v": Decimal(0), "sell_v": Decimal(0),
                 "dash": Decimal(0), "bill": Decimal(0), "open": False}
    )
    for t in fills:
        s = by_tok[str(t.instrument.token)]
        s["symbol"] = t.instrument.symbol
        v = to_decimal(t.price) * to_decimal(t.quantity)
        if t.action.value == "BUY":
            s["buy_q"] += float(t.quantity)
            s["buy_v"] += v
        else:
            s["sell_q"] += float(t.quantity)
            s["sell_v"] += v

    for p in await Position.find(Position.user_id == user.id).to_list():
        s = by_tok[str(p.instrument.token)]
        s["symbol"] = s["symbol"] or p.instrument.symbol
        s["dash"] += to_decimal(p.realized_pnl or 0)
        if p.status.value == "OPEN" and float(p.quantity or 0) != 0:
            s["open"] = True

    events, _ = await position_service.list_closed_trade_events_fifo(
        user.id, skip=0, limit=1_000_000
    )
    for ev in events:
        s = by_tok[str(ev["instrument"].token)]
        s["bill"] += to_decimal(ev["gross_pnl"])

    rows = []
    for tok, s in by_tok.items():
        flat = abs(s["buy_q"] - s["sell_q"]) < 1e-6 and not s["open"]
        oracle = (s["sell_v"] - s["buy_v"]) if flat else None
        rows.append({
            "user": user.user_code, "symbol": s["symbol"], "token": tok, "flat": flat,
            "oracle": None if oracle is None else float(round(oracle, 2)),
            "dash": float(round(s["dash"], 2)), "bill": float(round(s["bill"], 2)),
            "verdict": _verdict(flat, oracle, s["dash"], s["bill"]),
        })
    return rows


async def main(args: argparse.Namespace) -> int:
    await init_database()
    try:
        q = {"role": UserRole.CLIENT.value, "is_demo": {"$ne": True}}
        if args.user:
            q = {"user_code": args.user}
        users = await User.find(q).to_list()
        if not args.user and args.limit:
            counts = {
                d["_id"]: d["n"]
                for d in await Trade.aggregate(
                    [{"$group": {"_id": "$user_id", "n": {"$sum": 1}}}]
                ).to_list()
            }
            users = sorted(users, key=lambda u: -counts.get(u.id, 0))[: args.limit]

        rows: list[dict] = []
        for u in users:
            rows.extend(await check_user(u))
    finally:
        await close_database()

    bad = [r for r in rows if r["verdict"] != "ok"]
    print(f"{'client':<14}{'script':<26}{'flat':<6}{'oracle':>14}{'dash':>14}{'bill':>14}  verdict")
    for r in sorted(bad, key=lambda r: (r["user"], r["symbol"])):
        o = "" if r["oracle"] is None else f"{r['oracle']:,.2f}"
        print(f"{r['user']:<14}{r['symbol'][:25]:<26}{'yes' if r['flat'] else 'no':<6}"
              f"{o:>14}{r['dash']:>14,.2f}{r['bill']:>14,.2f}  {r['verdict']}")

    tally: dict[str, int] = defaultdict(int)
    gap: dict[str, float] = defaultdict(float)
    for r in rows:
        tally[r["verdict"]] += 1
        if r["verdict"] == "DASH-WRONG":
            gap["dash"] += abs(r["dash"] - r["oracle"])
        elif r["verdict"] == "BILL-WRONG":
            gap["bill"] += abs(r["bill"] - r["oracle"])
        elif r["verdict"] == "BOTH-WRONG":
            gap["dash"] += abs(r["dash"] - r["oracle"])
            gap["bill"] += abs(r["bill"] - r["oracle"])
        elif r["verdict"] == "open-gap":
            gap["open"] += abs(r["dash"] - r["bill"])
    clients = len({r["user"] for r in rows})
    clients_bad = len({r["user"] for r in bad if r["verdict"] != "open-gap"})
    print(f"\n{clients} clients, {len(rows)} scripts ({sum(r['flat'] for r in rows)} flat): "
          + ", ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    print(f"clients with a flat-book mismatch: {clients_bad}")
    print(f"gap vs oracle: dash {gap['dash']:,.2f}, bill {gap['bill']:,.2f}; "
          f"dash-vs-bill on open scripts {gap['open']:,.2f}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=1))  # noqa: ASYNC240 — one-shot script, DB is closed
    return 1 if clients_bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--user", help="one client's user_code")
    ap.add_argument("--limit", type=int, default=40, help="most active N clients (0 = all)")
    ap.add_argument("--json", help="also write every row to this file")
    sys.exit(asyncio.run(main(ap.parse_args())))
