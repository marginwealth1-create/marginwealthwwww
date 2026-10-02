"""Diagnose the risk_ltp_fetch_failed flood: WHICH open positions have no
live price, and WHY.

For every OPEN position it reports exactly what the sharded risk-enforcer
sees each tick — the leader's ``mdlive:{token}`` snapshot — plus the
display-only ``mdlast:{token}`` last-known value and the Instrument catalog
metadata (segment / expiry / is_active / token type). Positions whose
``mdlive`` LTP is missing/0 are the ones the risk loop skips SL/TP/stop-out
for (the flood). Bucketing them by segment + expiry + token-type tells us
the real cause (subscription gap vs expired vs MCX/forex vs token-format
mismatch) instead of guessing.

Read-only. Makes NO changes.

    cd /root/marginwealth/backend && source .venv/bin/activate
    python -m scripts.diag_stale_feed
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.core.database import close_database, init_database
from app.core.redis_client import close_redis, get_redis, init_redis
from app.models.instrument import Instrument
from app.models.position import Position, PositionStatus
from app.services import market_data_service
from app.utils.decimal_utils import to_decimal

IST = timezone(timedelta(hours=5, minutes=30))


def _is_numeric_token(t: str) -> bool:
    return bool(t) and str(t).lstrip("-").isdigit()


async def main() -> None:
    await init_database()
    try:
        await init_redis()
    except Exception as e:  # noqa: BLE001
        print(f"❌ Redis init failed — can't read mdlive/mdlast: {str(e)[:120]}")
        await close_database()
        return

    today = datetime.now(IST).date()
    positions = await Position.find(Position.status == PositionStatus.OPEN).to_list()
    print(f"OPEN positions: {len(positions)}\n")
    if not positions:
        await _shutdown()
        return

    tokens = [p.instrument.token for p in positions]

    # What the risk loop actually reads each tick.
    live_ltp, _live_quote = await market_data_service.get_ltp_quote_batch_mdlive(tokens)

    # Display-only last-known (mdlast) — present even when live feed is dead.
    redis = get_redis()
    mdlast_raw = await redis.mget([f"mdlast:{t}" for t in tokens])
    mdlast: dict[str, float] = {}
    for t, val in zip(tokens, mdlast_raw):
        if not val:
            continue
        try:
            mdlast[t] = float(json.loads(val).get("ltp") or 0)
        except Exception:  # noqa: BLE001
            pass

    # Instrument catalog metadata, one batched lookup.
    str_tokens = [str(t) for t in tokens]
    instr_docs = await Instrument.find(
        {"token": {"$in": list({*str_tokens, *tokens})}}
    ).to_list()
    instr_by_token = {str(i.token): i for i in instr_docs}

    no_price = []
    for p in positions:
        tok = p.instrument.token
        if live_ltp.get(tok) and live_ltp[tok] > 0:
            continue  # risk loop CAN price it — fine
        no_price.append(p)

    print(f"Positions with NO live mdlive price (risk loop skips these): {len(no_price)}\n")

    seg_counter: Counter = Counter()
    reason_counter: Counter = Counter()
    print(
        f"{'SYMBOL':24}{'SEG':10}{'TOKEN':14}{'NUM?':6}"
        f"{'EXPIRY':12}{'ACTIVE':8}{'POS.LTP':>10}{'MDLAST':>10}  REASON"
    )
    print("-" * 120)
    for p in no_price:
        tok = p.instrument.token
        inst = instr_by_token.get(str(tok))
        seg = p.instrument.segment or (inst.segment if inst else "?")
        is_num = "yes" if _is_numeric_token(str(tok)) else "NO"
        expiry = str(inst.expiry) if (inst and inst.expiry) else "-"
        active = ("yes" if inst.is_active else "NO") if inst else "MISSING"
        pos_ltp = float(to_decimal(p.ltp))
        last = mdlast.get(tok, 0.0)

        # Classify the most likely cause.
        if inst is None:
            reason = "instrument MISSING from catalog (token mismatch?)"
        elif inst.expiry and inst.expiry < today:
            reason = "EXPIRED contract"
        elif not _is_numeric_token(str(tok)):
            reason = "synthetic (Infoway forex/crypto) — separate feed"
        elif seg and str(seg).upper() in ("MCX", "CDS", "BFO"):
            reason = f"{seg} segment — check feed subscribes it"
        elif last > 0:
            reason = "was subscribed (mdlast present) but mdlive STALE — sub gap/not ticking"
        else:
            reason = "never priced (mdlast empty) — NOT subscribed to live feed"

        seg_counter[str(seg)] += 1
        reason_counter[reason] += 1
        print(
            f"{p.instrument.symbol:24}{str(seg):10}{str(tok):14}{is_num:6}"
            f"{expiry:12}{active:8}{pos_ltp:>10.2f}{last:>10.2f}  {reason}"
        )

    print("\n── By segment ──")
    for seg, n in seg_counter.most_common():
        print(f"  {seg:12} {n}")
    print("\n── By likely cause ──")
    for reason, n in reason_counter.most_common():
        print(f"  {n:4}  {reason}")

    await _shutdown()


async def _shutdown() -> None:
    try:
        await close_redis()
    except Exception:  # noqa: BLE001
        pass
    await close_database()


if __name__ == "__main__":
    asyncio.run(main())
