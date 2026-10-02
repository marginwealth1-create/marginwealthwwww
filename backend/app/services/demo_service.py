"""Shared demo account lifecycle.

The login page's "Try Demo" logs every visitor into ONE shared demo account
(see `auth_service.create_demo_session` / `GLOBAL_DEMO_EMAIL`) instead of
minting a throwaway per click. That single account accumulates everyone's
trades, so it must be flattened and re-funded on a schedule — otherwise its
open positions never close and the books drift. `reset_global_demo` does that
full wipe + ₹50L restore; `main.py` calls it every 24h via `demo_reset_loop`.
"""

from __future__ import annotations

import logging

from bson import Decimal128

from app.models.order import Order
from app.models.position import Position
from app.models.trade import Trade
from app.models.transaction import TransactionStatus, TransactionType, WalletTransaction
from app.models.user import User
from app.services import wallet_service

logger = logging.getLogger(__name__)

_DEMO_FUND = Decimal128("5000000")  # ₹50,00,000 virtual demo balance
_ZERO = Decimal128("0")


async def reset_global_demo() -> dict:
    """Flatten the shared demo account and restore its ₹50L virtual balance.

    Idempotent — safe to call repeatedly. Returns a small summary dict (used
    by the scheduler log and the admin manual-trigger, if any). No-op when the
    demo account hasn't been provisioned yet (nobody has clicked Try Demo).
    """
    from app.services.auth_service import GLOBAL_DEMO_EMAIL

    user = await User.find_one(User.email == GLOBAL_DEMO_EMAIL)
    if user is None:
        return {"reset": False, "reason": "global demo not provisioned yet"}

    uid = user.id

    # Full wipe — it's a demo, a clean slate every cycle keeps the account
    # light (the whole point: open demo trades were never closing and piling
    # up). Order matters little since these are independent collections.
    pos_res = await Position.find(Position.user_id == uid).delete()
    ord_res = await Order.find(Order.user_id == uid).delete()
    trd_res = await Trade.find(Trade.user_id == uid).delete()
    await WalletTransaction.find(WalletTransaction.user_id == uid).delete()

    # Restore the virtual balance: flat ₹50L, no blocked margin, no shortfall.
    wallet = await wallet_service.get_or_create(uid)
    wallet.available_balance = _DEMO_FUND
    wallet.used_margin = _ZERO
    wallet.settlement_outstanding = _ZERO
    wallet.version = (wallet.version or 0) + 1
    await wallet.save()

    # One clean ledger row so the wallet history shows the daily credit.
    await WalletTransaction(
        user_id=uid,
        transaction_type=TransactionType.BONUS,
        amount=_DEMO_FUND,
        balance_before=_ZERO,
        balance_after=_DEMO_FUND,
        narration="Demo daily reset — ₹50,00,000 virtual balance restored",
        status=TransactionStatus.COMPLETED,
    ).insert()

    summary = {
        "reset": True,
        "positions_cleared": getattr(pos_res, "deleted_count", None),
        "orders_cleared": getattr(ord_res, "deleted_count", None),
        "trades_cleared": getattr(trd_res, "deleted_count", None),
    }
    logger.info("demo_global_reset_done", extra=summary)
    return summary


async def demo_reset_loop(*, interval_sec: float = 3600.0) -> None:
    """Reset the shared demo every 24h.

    Polls hourly (the supervisor/leader wrapper in main.py owns the lifecycle)
    and fires `reset_global_demo` only once a full day has elapsed since the
    last reset. The "last reset" timestamp lives in Redis, so the 24h cadence
    survives process restarts/redeploys instead of restarting from boot. On
    the very first run (no timestamp yet) it resets immediately, then settles
    into the daily rhythm.
    """
    import asyncio
    import time

    from app.core.redis_client import cache_get, cache_set

    _KEY = "demo:last_reset_ts"
    _DAY = 24 * 3600

    while True:
        try:
            rec = await cache_get(_KEY)
            last = float(rec.get("ts")) if rec and rec.get("ts") else 0.0
            if time.time() - last >= _DAY:
                await reset_global_demo()
                # Re-read now() AFTER the reset so a long wipe doesn't shorten
                # the next cycle. TTL is 2 days so a stalled cluster re-fires.
                await cache_set(_KEY, {"ts": time.time()}, ttl_sec=_DAY * 2)
        except Exception:
            logger.exception("demo_reset_loop_iteration_failed")
        await asyncio.sleep(interval_sec)
