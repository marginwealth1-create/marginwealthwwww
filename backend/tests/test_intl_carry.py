"""The international markets and crypto carry (operator 2026-11-20): forex and
the stock / index / commodity CFDs close their day at 03:30 IST, so the EOD
rollover runs for them then, Tue-Sat; crypto rolls at 03:30 every day. The
admin's Overnight (holding) Margin applies, and until a tier sets one the
carry check keeps the Intraday margin (the base row's seeded 100 would turn
a 500x book into a 100x check overnight) — the matrix says "same as
Intraday"."""

from __future__ import annotations

from datetime import date, time

import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.user.positions import _segment_carryable
from app.core.config import settings
from app.core.database import _document_models
from app.models.netting import NettingSegment, SuperAdminSegmentOverride
from app.models.user import User, UserRole, UserStatus
from app.services import netting_service
from app.services.position_service import rollover_fire_at
from app.utils.time_utils import INTL_ROLLOVER, NSE_BSE_CLOSE

_DB = f"{settings.MONGODB_DB_NAME}_test_intl_carry"
TUE, SAT, SUN, MON = date(2026, 11, 17), date(2026, 11, 21), date(2026, 11, 22), date(2026, 11, 23)


def test_intl_rolls_at_0330_tuesday_to_saturday_crypto_every_day():
    assert [rollover_fire_at("INTL", INTL_ROLLOVER, d) for d in (TUE, SAT, SUN, MON)] == [
        time(3, 30),
        time(3, 30),
        None,
        None,
    ]
    assert {rollover_fire_at("CRYPTO", INTL_ROLLOVER, d) for d in (TUE, SAT, SUN, MON)} == {time(3, 30)}
    # The Indian groups keep their weekday close, and a Muhurat session's.
    assert rollover_fire_at("INDIAN_EQUITY_FNO", NSE_BSE_CLOSE, SAT) is None
    assert rollover_fire_at("INDIAN_EQUITY_FNO", NSE_BSE_CLOSE, TUE) == NSE_BSE_CLOSE
    assert rollover_fire_at("INDIAN_EQUITY_FNO", NSE_BSE_CLOSE, SUN, time(19, 15)) == time(19, 16)
    assert rollover_fire_at("INTL", INTL_ROLLOVER, SUN, time(19, 15)) is None


def test_every_rolled_position_shows_its_carry_toggle():
    for seg in ("FOREX", "STOCKS", "INDICES", "COMMODITIES", "CRYPTO_SPOT", "NFO_OPTION", "MCX_FUTURE"):
        assert _segment_carryable(seg), seg
    assert not _segment_carryable("CDS_FUTURE")


@pytest_asyncio.fixture
async def sa(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models())
    monkeypatch.setattr(netting_service, "_SUPER_ADMIN_ID_CACHE", None)
    try:
        await netting_service.seed_default_segments()
        u = User(
            user_code="FXC1",
            email="fxc1@example.com",
            mobile="9500099201",
            full_name="FXC1",
            password_hash="x",
            role=UserRole.SUPER_ADMIN,
            status=UserStatus.ACTIVE,
        )
        await u.insert()
        yield u
    finally:
        await client.drop_database(_DB)
        client.close()


async def _overnight(user: User, segment: str, symbol: str) -> dict:
    out = await netting_service.get_effective_settings(
        user.id, segment, action="BUY", product_type="NRML", symbol=symbol
    )
    return out["settings"]


async def test_the_holding_margin_is_the_admins_or_else_intraday(sa):
    fx = SuperAdminSegmentOverride(
        super_admin_id=sa.id, segment_name="FOREX", marginCalcMode="times", intradayMargin=500
    )
    gold = SuperAdminSegmentOverride(
        super_admin_id=sa.id, segment_name="COMMODITIES", marginCalcMode="fixed", intradayMargin=25000
    )
    await fx.insert()
    await gold.insert()
    # Unset: the intraday 500x / Rs 25,000 a lot, not the seeded 100.
    assert (await _overnight(sa, "FOREX", "GBPUSD"))["overnight_leverage"] == 500
    assert (await _overnight(sa, "COMMODITIES", "XAUUSD"))["overnight_fixed_margin_per_lot"] == 25000
    fx.overnightMargin = 200
    gold.overnightMargin = 40000
    await fx.save()  # no Redis in tests: the resolver's 5-min cache is skipped
    await gold.save()
    assert (await _overnight(sa, "FOREX", "GBPUSD"))["overnight_leverage"] == 200
    assert (await _overnight(sa, "COMMODITIES", "XAUUSD"))["overnight_fixed_margin_per_lot"] == 40000


async def test_the_matrix_shows_an_unset_holding_margin_as_intraday(sa):
    rows = await NettingSegment.find({"name": {"$in": ["CRYPTO", "MCX_FUT"]}}).to_list()
    inherited = await netting_service.caller_inherited_segment_fields(sa, rows)
    assert inherited["CRYPTO"]["overnightMargin"] == {"value": None, "tier": "INTRADAY"}
    assert inherited["MCX_FUT"]["overnightMargin"]["tier"] == "SEED"
