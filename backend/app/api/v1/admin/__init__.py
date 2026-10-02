"""Admin panel routers (mounted under /api/v1/admin)."""

from fastapi import APIRouter

from app.api.v1.admin import (
    accounts,
    auth,
    bonuses,
    branding,
    brokerage,
    chart_levels,
    crypto_config,
    brokers,
    dashboard,
    employees,
    expiry_overrides,
    infoway,
    metaapi,
    instruments,
    kyc,
    ledger,
    management,
    marketwatch,
    money_transactions,
    netting,
    notifications,
    payin_out,
    platform_reports,
    pnl_sharing,
    push,
    reports,
    risk,
    settings,
    support,
    trading,
    users,
    zerodha,
    zerodha_auto_login,
)

router = APIRouter(prefix="/admin", tags=["admin"])
router.include_router(accounts.router)
router.include_router(auth.router)
router.include_router(dashboard.router)
router.include_router(users.router)
router.include_router(risk.router)
router.include_router(netting.router)
router.include_router(trading.router)
router.include_router(payin_out.router)
router.include_router(brokerage.router)
router.include_router(chart_levels.router)
router.include_router(crypto_config.router)
router.include_router(bonuses.router)
router.include_router(instruments.router)
router.include_router(marketwatch.router)
router.include_router(ledger.router)
router.include_router(money_transactions.router)
router.include_router(reports.router)
router.include_router(platform_reports.router)
router.include_router(settings.router)
router.include_router(expiry_overrides.router)
router.include_router(metaapi.router)
router.include_router(zerodha.router)
router.include_router(zerodha_auto_login.router)
router.include_router(infoway.router)
router.include_router(kyc.router)
router.include_router(management.router)
router.include_router(employees.router)
router.include_router(brokers.router)
router.include_router(pnl_sharing.router)
router.include_router(notifications.router)
router.include_router(support.router)
router.include_router(branding.router)
router.include_router(push.router)
