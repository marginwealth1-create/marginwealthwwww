"""Hourly sweep of expired bonuses. ACTIVE bonuses past `expires_at` either
convert (wager already met) or get clawed back (wager unmet). Started from
main.py lifespan only when settings.BONUSES_ENABLED. Not Celery.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.config import settings
from app.models.user_bonus import UserBonus, UserBonusStatus
from app.services import bonus_service
from app.utils.decimal_utils import to_decimal
from app.utils.time_utils import now_utc

logger = logging.getLogger("bonus_expiry")

_stop = False


def stop_bonus_expiry() -> None:
    global _stop
    _stop = True


async def bonus_expiry_loop(interval_sec: float = 3600.0) -> None:
    global _stop
    _stop = False
    logger.info("bonus_expiry_loop started interval=%.0fs", interval_sec)
    while not _stop:
        try:
            if settings.BONUSES_ENABLED:
                now = now_utc()
                due = await UserBonus.find(
                    {
                        "status": UserBonusStatus.ACTIVE.value,
                        "expires_at": {"$ne": None, "$lte": now},
                    }
                ).to_list()
                for b in due:
                    try:
                        target = to_decimal(b.wager_target_volume)
                        met = target > 0 and to_decimal(b.wager_progress_volume) >= target
                        if met:
                            await bonus_service.complete_and_convert(b)
                        else:
                            await bonus_service.expire(b)
                    except Exception:  # pragma: no cover — never let one bonus kill the sweep
                        logger.exception("bonus_expiry_row_failed bonus=%s", b.id)
        except Exception:  # pragma: no cover
            logger.exception("bonus_expiry_tick_failed")
        await asyncio.sleep(interval_sec)
