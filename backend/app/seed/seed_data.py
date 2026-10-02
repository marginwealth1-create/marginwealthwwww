"""One-shot bootstrap seeding.

Idempotent — safe to run on every startup. Each section checks for existing
data before inserting.
"""

from __future__ import annotations

import logging
from datetime import date

from app.core.config import settings
from app.models._base import ALL_SEGMENTS
from app.models.bank_account import CompanyBankAccount
from app.models.brokerage_plan import BrokeragePlan, PlanDetail
from app.models.holiday import TradingHoliday
from app.models.platform_setting import PlatformSetting, SettingType
from app.models.transaction import AllowedTimeWindow, WdRule
from app.models.user import UserRole, UserStatus
from app.services import netting_service, user_service

logger = logging.getLogger(__name__)


async def seed_super_admin() -> None:
    from app.models.user import User

    existing = await User.find_one(User.email == settings.SEED_SUPER_ADMIN_EMAIL.lower())
    if existing is not None:
        return
    user = await user_service.create_user(
        email=settings.SEED_SUPER_ADMIN_EMAIL,
        mobile=settings.SEED_SUPER_ADMIN_MOBILE,
        password=settings.SEED_SUPER_ADMIN_PASSWORD.get_secret_value(),
        full_name="Super Admin",
        role=UserRole.SUPER_ADMIN,
        status=UserStatus.ACTIVE,
    )
    user.must_change_password = True
    await user.save()
    logger.info("seeded_super_admin", extra={"email": user.email})


async def seed_netting_and_risk() -> None:
    inserted = await netting_service.seed_default_segments()
    removed = await netting_service.cleanup_retired_segments()
    risk_created = await netting_service.seed_default_risk()
    logger.info(
        "seeded_netting_and_risk",
        extra={
            "segments_inserted": inserted,
            "retired_removed": removed,
            "risk_created": risk_created,
        },
    )


async def seed_default_brokerage_plan() -> None:
    existing = await BrokeragePlan.find_one(BrokeragePlan.plan_name == "Standard")
    if existing is not None:
        return
    details: list[PlanDetail] = []
    for seg in ALL_SEGMENTS:
        details.append(PlanDetail(segment_type=seg.value))  # uses field defaults
    await BrokeragePlan(
        plan_name="Standard",
        description="Standard plan applied to new users",
        is_default=True,
        is_active=True,
        details=details,
    ).insert()
    logger.info("seeded_default_brokerage_plan")


async def seed_company_bank() -> None:
    existing = await CompanyBankAccount.find_one()
    if existing is not None:
        return
    # Placeholder row so the deposit screen renders on a fresh install — the
    # operator replaces every field with real bank details in admin →
    # Payments. Brand comes from APP_NAME so a new deploy never stamps the
    # old `MarginWealth` name into the account holder / UPI handle.
    await CompanyBankAccount(
        bank_name="HDFC Bank",
        account_holder=f"{settings.APP_NAME} Pvt Ltd",
        account_number="00000000000000",
        ifsc_code="HDFC0000001",
        upi_id="",
        is_default=True,
        is_active=True,
    ).insert()
    logger.info("seeded_company_bank")


async def seed_wd_rules() -> None:
    for rule in ("DEPOSIT", "WITHDRAWAL"):
        existing = await WdRule.find_one(WdRule.rule_type == rule)
        if existing is None:
            await WdRule(rule_type=rule, allowed_times=[AllowedTimeWindow()]).insert()
    logger.info("seeded_wd_rules")


async def seed_platform_settings() -> None:
    defaults: list[tuple[str, object, SettingType, str, bool, str]] = [
        # Branding + support contact come from config (env-tunable), NOT a
        # hard-coded literal. These are seeded into PlatformSetting on first
        # boot and are editable afterwards in admin → Platform Settings, so a
        # literal here silently stamped the old `MarginWealth` branding /
        # `support@marginwealth.live` into every fresh deploy's DB even when the
        # .env already carried the correct brand. SUPPORT_EMAIL defaults to
        # empty — the support UI then hides the email option instead of
        # showing a dead address.
        ("platform.name", settings.APP_NAME, SettingType.STRING, "general", True, "Public platform name"),
        ("platform.support_email", settings.SUPPORT_EMAIL, SettingType.STRING, "general", True, "Support email"),
        ("platform.support_whatsapp", "", SettingType.STRING, "general", True, "Support WhatsApp number (with country code, e.g. +919999999999)"),
        ("platform.theme", "dark", SettingType.STRING, "general", True, "UI theme hint"),
        # Promo button — a blinking CTA in every user's dashboard header. Super
        # admin sets the URL + label and flips it on from Platform Settings.
        ("promo.button_enabled", False, SettingType.BOOL, "general", True, "Show the promo button on every user's dashboard header"),
        ("promo.button_url", "", SettingType.STRING, "general", True, "URL the promo button opens (blank = disabled)"),
        ("promo.button_label", "Offer", SettingType.STRING, "general", True, "Short label shown on the promo button"),
        # Divinepay UPI gateway for the super-admin's OWN pool (assigned_admin_id
        # is None). Default OFF — the whole platform ships manual; super-admin
        # flips this + per-admin toggles to enable the auto-crediting flow.
        ("payment_gateway.super_admin_pool_enabled", False, SettingType.BOOL, "payment", False, "Divinepay UPI gateway for the super-admin's own users (per-admin pools use each admin's own switch)"),
        ("platform.language", "en", SettingType.STRING, "general", True, "Default UI language"),
        ("trading.market_open", settings.MARKET_OPEN_TIME, SettingType.STRING, "trading", True, "Market open time"),
        ("trading.market_close", settings.MARKET_CLOSE_TIME, SettingType.STRING, "trading", True, "Market close time"),
        ("security.session_timeout_min", 60, SettingType.INTEGER, "security", False, "Session timeout"),
        ("security.password_min_len", 8, SettingType.INTEGER, "security", False, "Min password length"),
        ("security.failed_login_lock_min", 15, SettingType.INTEGER, "security", False, "Lockout window"),
        ("risk.max_platform_leverage", 5.0, SettingType.FLOAT, "risk", False, "Hard leverage ceiling"),
        (
            "weekly_settlement.enabled",
            True,
            SettingType.BOOL,
            "trading",
            False,
            "Weekly mark-to-market settlement (Saturday 00:00 IST): realises open-position P&L to the wallet and re-opens the same position at the settlement price. Turn OFF to disable.",
        ),
        # ── Option chain (drives the chart-tabs '+' picker) ─────────────
        (
            "option_chain.underlyings",
            [
                {"label": "Nifty", "symbol": "NIFTY", "color": "emerald"},
                {"label": "BankNifty", "symbol": "BANKNIFTY", "color": "violet"},
                {"label": "Sensex", "symbol": "SENSEX", "color": "rose"},
            ],
            SettingType.JSON,
            "option_chain",
            True,
            "Underlyings shown as chips in the option chain picker",
        ),
        (
            "option_chain.strikes_around_atm",
            15,
            SettingType.INTEGER,
            "option_chain",
            True,
            "Strikes shown above and below ATM (total = 2N+1)",
        ),
        (
            "option_chain.max_expiries",
            6,
            SettingType.INTEGER,
            "option_chain",
            True,
            "Number of expiries exposed to users",
        ),
    ]
    for k, v, t, cat, public, desc in defaults:
        existing = await PlatformSetting.find_one(PlatformSetting.setting_key == k)
        if existing is None:
            await PlatformSetting(
                setting_key=k,
                setting_value=v,
                setting_type=t,
                category=cat,
                is_public=public,
                description=desc,
            ).insert()
    logger.info("seeded_platform_settings")


async def seed_holidays() -> None:
    """Seed a small set of well-known NSE holidays for the current year. Admin
    can extend via /admin/holidays."""
    base = [
        (date(date.today().year, 1, 26), "Republic Day"),
        (date(date.today().year, 8, 15), "Independence Day"),
        (date(date.today().year, 10, 2), "Gandhi Jayanti"),
    ]
    for d, desc in base:
        existing = await TradingHoliday.find_one(
            TradingHoliday.exchange == "NSE", TradingHoliday.holiday_date == d
        )
        if existing is None:
            await TradingHoliday(holiday_date=d, description=desc).insert()
    logger.info("seeded_holidays")


async def seed_global_instruments() -> None:
    """Forex / metals / energy / stocks / indices catalogue rows. Without
    these the terminal's Stocks and Indices tabs are empty on a deployment
    that gets its prices from MetaAPI rather than Infoway."""
    from app.services.infoway_service import seed_default_instruments

    created = await seed_default_instruments()
    logger.info("seeded_global_instruments", extra={"inserted": created})


async def run_seed() -> None:
    """Top-level idempotent seeder."""
    await seed_netting_and_risk()
    await seed_default_brokerage_plan()
    await seed_super_admin()
    await seed_company_bank()
    await seed_wd_rules()
    await seed_platform_settings()
    await seed_holidays()
    await seed_global_instruments()
    logger.info("seed_complete")
