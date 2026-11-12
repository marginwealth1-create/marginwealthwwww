"""Zero vs Not-set in the netting cascade (handoff §3.1, operator D5).

Margin fields: a stored 0 means "inherit" — the walk keeps going to the next
tier. Before, the composite merge kept a higher tier's 0 and threw away the
lower tiers, then the field chain read 0 as "unset" and fell to the
segment-wide margin or a hard-coded 100: a broker's stray 0 let clients
short options at ₹100/lot instead of the admin's ₹12,345.

Commission fields: 0 is a real "free" and stops the walk (unchanged).

The admin matrix shows each field as the caller's OWN value plus what it
inherits from the tiers strictly above the caller, and from which tier.

Every case runs the real resolver over real tier documents; the brokerage
case drives a MARKET order through the matching engine.

    pytest -q tests/test_netting_zero_semantics.py
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import PydanticObjectId, init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import netting as netting_api
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import OrderAction, OrderType, ProductType
from app.models.netting import (
    BrokerSegmentOverride,
    NettingSegment,
    SubAdminSegmentOverride,
    SuperAdminSegmentOverride,
)
from app.models.order import InstrumentRef, Order
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, matching_engine, netting_service

_DB = f"{settings.MONGODB_DB_NAME}_test_netting_zero_semantics"
_OPT = ("NFO_OPTION", "NIFTY26NOV24000CE")  # → NSE_IDX_OPT
_FUT = ("MCX_FUTURE", "GOLDZS")  # → MCX_FUT
_GOLD = InstrumentRef(token="TZS1", symbol="GOLDZS", exchange="MCX", segment="MCX_FUTURE", lot_size=100)
_LTP = Decimal("100")


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models())
    # Process-wide cache of the super-admin id; another test's DB may have set it.
    monkeypatch.setattr(netting_service, "_SUPER_ADMIN_ID_CACHE", None)

    async def _quote(_token):
        return {"ltp": str(_LTP)}

    monkeypatch.setattr(market_data_service, "get_quote", _quote)
    n = 0

    async def mk(role: UserRole, **kw) -> User:
        nonlocal n
        n += 1
        u = User(
            user_code=f"NZS{n:03d}",
            email=f"nzs{n}@example.com",
            mobile=f"95000{n:05d}",
            full_name=f"U{n}",
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            **kw,
        )
        await u.insert()
        return u

    try:
        await netting_service.seed_default_segments()
        sa = await mk(UserRole.SUPER_ADMIN)
        a1 = await mk(UserRole.ADMIN)
        b1 = await mk(UserRole.BROKER, assigned_admin_id=a1.id)
        c = await mk(
            UserRole.CLIENT,
            assigned_admin_id=a1.id,
            assigned_broker_id=b1.id,
            broker_ancestry=[b1.id],
        )
        await Wallet(user_id=c.id, available_balance=Decimal128("10000000")).insert()
        yield SimpleNamespace(sa=sa, a1=a1, b1=b1, c=c)
    finally:
        await client.drop_database(_DB)
        client.close()


async def _set(w, tier: str, seg: str, **fields) -> None:
    if tier == "broker":
        doc = BrokerSegmentOverride(broker_id=w.b1.id, segment_name=seg, **fields)
    elif tier == "admin":
        doc = SubAdminSegmentOverride(sub_admin_id=w.a1.id, segment_name=seg, **fields)
    else:
        doc = SuperAdminSegmentOverride(super_admin_id=w.sa.id, segment_name=seg, **fields)
    await doc.insert()


async def _resolve(user, instr, action, product, *, expiry=False) -> dict:
    option_type = instr[1][-2:] if instr is _OPT else None
    out = await netting_service.get_effective_settings(
        user.id,
        instr[0],
        action=action,
        option_type=option_type,
        product_type=product,
        is_expiry_day=expiry,
        symbol=instr[1],
    )
    return out["settings"]


# ── Margin 0 = inherit, all nine fields ──────────────────────────────
_NINE = [
    ("intradayMargin", _FUT, "BUY", "MIS", False),
    ("overnightMargin", _FUT, "BUY", "NRML", False),
    ("optionBuyIntraday", _OPT, "BUY", "MIS", False),
    ("optionBuyOvernight", _OPT, "BUY", "NRML", False),
    ("optionSellIntraday", _OPT, "SELL", "MIS", False),
    ("optionSellOvernight", _OPT, "SELL", "NRML", False),
    ("expiryDayIntradayMargin", _FUT, "BUY", "MIS", True),
    ("expiryDayOptionBuyMargin", _OPT, "BUY", "MIS", True),
    ("expiryDayOptionSellMargin", _OPT, "SELL", "MIS", True),
]


@pytest.mark.parametrize("case", _NINE, ids=[c[0] for c in _NINE])
async def test_margin_zero_at_broker_falls_through_to_admin(w, case):
    field, instr, action, product, expiry = case
    seg = netting_service.resolve_admin_row(*instr)
    await _set(w, "admin", seg, marginCalcMode="fixed", **{field: 12345.0})
    await _set(w, "broker", seg, **{field: 0.0})
    s = await _resolve(w.c, instr, action, product, expiry=expiry)
    assert s["margin_calc_mode"] == "fixed"
    assert s["fixed_margin_per_lot"] == 12345.0


async def test_margin_walks_past_two_zeros_to_super_admin(w):
    seg = netting_service.resolve_admin_row(*_OPT)
    await _set(w, "super", seg, marginCalcMode="fixed", optionSellIntraday=777.0)
    await _set(w, "admin", seg, optionSellIntraday=0.0)
    await _set(w, "broker", seg, optionSellIntraday=0.0)
    s = await _resolve(w.c, _OPT, "SELL", "MIS")
    assert s["fixed_margin_per_lot"] == 777.0


# ── Nobody set → exactly what it resolved to before ──────────────────
async def test_nobody_set_resolves_as_before(w):
    # Pinned from the pre-fix resolver on the seed rows.
    s = await _resolve(w.c, _OPT, "SELL", "MIS")
    assert s["margin_calc_mode"] == "fixed"
    assert s["fixed_margin_per_lot"] == s["overnight_fixed_margin_per_lot"] == 100.0
    assert s["commission_value"] == 20.0
    s = await _resolve(w.c, _FUT, "BUY", "NRML")
    assert (s["margin_calc_mode"], s["fixed_margin_per_lot"]) == ("fixed", 100.0)
    s = await _resolve(w.c, _OPT, "BUY", "MIS", expiry=True)
    assert s["fixed_margin_per_lot"] == 100.0

    # A tier that exists but holds only an unrelated field changes nothing.
    await _set(w, "broker", "NSE_IDX_OPT", commission=7.0)
    s = await _resolve(w.c, _OPT, "SELL", "MIS")
    assert s["fixed_margin_per_lot"] == 100.0

    # A lone 0 with nothing below it still lands on the segment-wide margin.
    await _set(
        w, "admin", "NSE_IDX_OPT", optionSellIntraday=0.0, intradayMargin=300.0, marginCalcMode="fixed"
    )
    s = await _resolve(w.c, _OPT, "SELL", "MIS")
    assert s["fixed_margin_per_lot"] == 300.0


# ── Commission 0 = free, still stops the walk ────────────────────────
async def test_option_commission_zero_still_free(w):
    seg = netting_service.resolve_admin_row(*_OPT)
    await _set(w, "admin", seg, optionSellCommission=15.0, commission=15.0)
    await _set(w, "broker", seg, optionSellCommission=0.0)
    assert (await _resolve(w.c, _OPT, "SELL", "MIS"))["commission_value"] == 0.0
    # The buy side was not zeroed: it keeps the seed's ₹20 Buy Brokerage.
    assert (await _resolve(w.c, _OPT, "BUY", "MIS"))["commission_value"] == 20.0


async def _fill(user: User, lots: int):
    order = Order(
        order_number=f"O{PydanticObjectId()}",
        user_id=user.id,
        placed_by=user.id,
        instrument=_GOLD,
        action=OrderAction.BUY,
        order_type=OrderType.MARKET,
        product_type=ProductType.MIS,
        lots=lots,
        quantity=lots * _GOLD.lot_size,
    )
    await order.insert()
    return await matching_engine.execute_market_order(order, cached_ltp=_LTP)


async def test_fill_with_broker_commission_zero_charges_nothing(w):
    await _set(w, "admin", "MCX_FUT", commission=15.0)
    await _set(w, "broker", "MCX_FUT", commission=0.0)
    trade = await _fill(w.c, 2)
    assert Decimal(str(trade.brokerage)) == 0
    wallet = await Wallet.find_one(Wallet.user_id == w.c.id)
    assert Decimal(str(wallet.total_charges)) == 0

    # Not set (null) at the broker walks on to the admin's ₹15/lot.
    await BrokerSegmentOverride.find(BrokerSegmentOverride.broker_id == w.b1.id).delete()
    trade = await _fill(w.c, 2)
    assert Decimal(str(trade.brokerage)) == 30


# ── Matrix: own value + inherited value and tier, caller-anchored ────
def _row(rows: list[dict], name: str) -> dict:
    return next(r for r in rows if r["name"] == name)


async def test_matrix_admin_sees_super_admin_tier(w):
    await _set(w, "super", "NSE_IDX_OPT", optionBuyCommission=10.0)
    row = _row((await netting_api.list_segments(w.a1, None)).data, "NSE_IDX_OPT")
    assert row["own"]["optionBuyCommission"] is None  # Not set
    assert row["inherited"]["optionBuyCommission"] == {"value": 10.0, "tier": "SUPER_ADMIN"}
    assert row["inherited"]["optionSellCommission"] == {"value": 20.0, "tier": "SEED"}
    assert row["optionBuyCommission"] == 10.0  # what this pool's clients pay


async def test_matrix_broker_sees_admin_then_super_admin(w):
    await _set(w, "super", "NSE_IDX_OPT", optionBuyCommission=10.0, optionSellIntraday=777.0)
    await _set(w, "admin", "NSE_IDX_OPT", optionBuyCommission=15.0, optionSellIntraday=0.0)
    await _set(w, "broker", "NSE_IDX_OPT", optionSellCommission=0.0)
    seg = await NettingSegment.find_one(NettingSegment.name == "NSE_IDX_OPT")
    row = (await netting_api.get_segment(str(seg.id), w.b1, None)).data
    assert row["own"]["optionBuyCommission"] is None
    assert row["inherited"]["optionBuyCommission"] == {"value": 15.0, "tier": "SUB_ADMIN"}
    # The admin's 0 margin inherits, so the broker sees the super-admin's.
    assert row["inherited"]["optionSellIntraday"] == {"value": 777.0, "tier": "SUPER_ADMIN"}
    # The broker's own 0 brokerage is a value (free), not "Not set".
    assert row["own"]["optionSellCommission"] == 0.0
    assert row["optionSellCommission"] == 0.0


async def test_matrix_super_admin_inherits_only_the_seed(w):
    await _set(w, "super", "NSE_IDX_OPT", optionBuyCommission=10.0)
    row = _row((await netting_api.list_segments(w.sa, None)).data, "NSE_IDX_OPT")
    assert row["own"]["optionBuyCommission"] == 10.0
    assert row["inherited"]["optionBuyCommission"] == {"value": 20.0, "tier": "SEED"}
