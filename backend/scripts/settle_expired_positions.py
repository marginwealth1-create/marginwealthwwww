"""One-off reconcile: settle OPEN positions stuck in EXPIRED contracts.

Background: ``expiry_cleanup`` used to unsubscribe an expired contract's
token from the live feed and mark its Instrument inactive WITHOUT closing
open positions in it. Those positions became *zombies* — the risk-enforcer
can never price a dead/unsubscribed token, so it silently skipped
SL/TP/stop-out for them (the ``risk_ltp_fetch_failed`` flood) and they sat
OPEN forever holding the user's margin. The forward fix now settles on the
expiry sweep, but pre-existing zombies (e.g. the 26JUN contracts that
expired 25-Jun) need this one-off pass because their Instrument is already
``is_active=False`` and the sweep filters on ``is_active=True``.

It settles each via ``position_service.settle_expired_position`` — books
realized P&L at the position's frozen last-known ``ltp`` (live feed is gone
for an expired token), releases the locked margin, and flips the row CLOSED
with ``close_reason="EXPIRY_SETTLED"``. Identical money path to the weekly
settlement service, so the wallet / history stay consistent.

Dry-run by default (lists what WOULD settle + the price + P&L). Pass
--apply to actually settle.

    cd /root/marginwealth/backend && source .venv/bin/activate
    python -m scripts.settle_expired_positions            # preview
    python -m scripts.settle_expired_positions --apply    # settle for real

Optional: restrict to one user_code for a careful first pass:
    python -m scripts.settle_expired_positions CL01587793 --apply
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.core.database import close_database, init_database
from app.core.redis_client import close_redis, init_redis
from app.models.instrument import Instrument
from app.models.position import Position, PositionStatus
from app.models.user import User
from app.services import position_service
from app.utils.decimal_utils import to_decimal

IST = timezone(timedelta(hours=5, minutes=30))


def _ist_today_date():
    return datetime.now(IST).date()


async def main(user_code: str | None, apply: bool) -> None:
    await init_database()
    # Redis is best-effort here: settle_expired_position prefers the live
    # LTP (Redis-backed) but falls back to the position's frozen `ltp`,
    # which is exactly what we want for a long-dead contract. So unlike
    # close_user_positions we DON'T abort when Redis is down.
    try:
        await init_redis()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  Redis init failed (will use frozen position ltp): {str(e)[:120]}")

    today = _ist_today_date()

    # 1) Every expired contract token — regardless of is_active, because the
    #    pre-existing zombies have already been deactivated by the old sweep.
    expired = await Instrument.find({"expiry": {"$ne": None, "$lt": today}}).to_list()
    expired_tokens = {str(i.token) for i in expired}
    print(f"Expired contracts in catalog (expiry < {today}): {len(expired_tokens)}\n")
    if not expired_tokens:
        print("Nothing expired. Done.")
        await _shutdown()
        return

    # 2) OPEN positions sitting in those expired tokens.
    query: dict = {
        "status": PositionStatus.OPEN.value,
        "instrument.token": {"$in": list(expired_tokens)},
    }

    user = None
    if user_code:
        user = await User.find_one(User.user_code == user_code)
        if user is None:
            print(f"❌ No user found with user_code = {user_code}")
            await _shutdown()
            return
        query["user_id"] = user.id

    zombies = await Position.find(query).to_list()
    scope = f"user {user_code}" if user_code else "ALL users"
    mode = "APPLY (settling for real)" if apply else "DRY RUN (no changes)"
    print(f"Scope: {scope} — mode: {mode}")
    print(f"Zombie OPEN positions in expired contracts: {len(zombies)}\n")
    if not zombies:
        print("Nothing to settle. Done.")
        await _shutdown()
        return

    ok = skipped = failed = 0
    total_pnl = Decimal(0)
    for p in zombies:
        avg = to_decimal(p.avg_price)
        ltp = to_decimal(p.ltp)
        qty = float(p.quantity or 0)
        sign = 1 if qty > 0 else -1
        est_pnl = (ltp - avg) * to_decimal(abs(qty)) * Decimal(sign) if ltp > 0 else Decimal(0)

        if not apply:
            price_note = f"@{ltp}" if ltp > 0 else "@NO-PRICE(will skip)"
            print(
                f"  WOULD  {p.instrument.symbol:22} qty={qty:<10} avg={avg} "
                f"settle{price_note} est_pnl≈{est_pnl} margin={to_decimal(p.margin_used)}"
            )
            if ltp > 0:
                total_pnl += est_pnl
            continue

        try:
            result = await position_service.settle_expired_position(p)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL   {p.instrument.symbol:22} — {str(e)[:120]}")
            continue

        if result == "settled":
            ok += 1
            fresh = await Position.get(p.id)
            booked = to_decimal(fresh.realized_pnl) if fresh else est_pnl
            total_pnl += est_pnl
            print(f"  SETTLED {p.instrument.symbol:22} qty={qty:<10} realized_pnl={booked}")
        elif result == "skipped":
            skipped += 1
            print(f"  SKIP   {p.instrument.symbol:22} — no usable price (ltp=0); left OPEN")
        else:
            failed += 1
            print(f"  FAIL   {p.instrument.symbol:22} — settle returned '{result}'")

    print()
    if apply:
        print(
            f"✅ Done. settled={ok} skipped={skipped} failed={failed}. "
            f"Net est. P&L booked ≈ {total_pnl}. Margin released per-position."
        )
        if skipped:
            print(
                "ℹ️  Skipped rows had no last-known price (ltp=0). Settle them "
                "manually with an admin-supplied price, or via the engine once a "
                "price source exists."
            )
    else:
        print(
            f"ℹ️  DRY RUN only — {len(zombies)} positions would be processed, "
            f"net est. P&L ≈ {total_pnl}. Re-run with --apply to settle."
        )
    await _shutdown()


async def _shutdown() -> None:
    try:
        await close_redis()
    except Exception:  # noqa: BLE001
        pass
    await close_database()


if __name__ == "__main__":
    pos_args = [a for a in sys.argv[1:] if not a.startswith("--")]
    code = pos_args[0] if pos_args else None
    asyncio.run(main(user_code=code, apply="--apply" in sys.argv))
