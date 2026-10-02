"""User panel routers (mounted under /api/v1/user)."""

from fastapi import APIRouter

from app.api.v1.user import (
    alerts,
    auth,
    bonuses,
    dashboard,
    instruments,
    kyc,
    ledger,
    marketwatch,
    news,
    notifications,
    push,
    option_chain,
    orders,
    positions,
    profile,
    reports,
    segment_settings,
    support,
    wallet,
)

router = APIRouter(prefix="/user", tags=["user"])
router.include_router(auth.router)
router.include_router(profile.router)
router.include_router(dashboard.router)
router.include_router(wallet.router)
router.include_router(bonuses.router)
router.include_router(marketwatch.router)
router.include_router(instruments.router)
router.include_router(orders.router)
router.include_router(positions.router)
router.include_router(positions.holdings_router)
router.include_router(ledger.router)
router.include_router(reports.router)
router.include_router(alerts.router)
router.include_router(notifications.router)
router.include_router(option_chain.router)
router.include_router(segment_settings.router)
router.include_router(kyc.router)
router.include_router(news.router)
router.include_router(support.router)
router.include_router(push.router)
