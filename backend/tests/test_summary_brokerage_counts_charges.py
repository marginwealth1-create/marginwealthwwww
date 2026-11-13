"""Brokerage readers see what a real fill charged (handoff §3).

A fill books its brokerage as a CHARGES ledger row, which accumulates in
`Wallet.total_charges`; `Wallet.total_brokerage` only moves on an admin
manual BROKERAGE entry. Each test drives a real MARKET order through
`matching_engine.execute_market_order` with the brokerage the netting
resolver hands it, then asks the consumer what the client was charged.
No ledger row is seeded by hand.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest_asyncio
from beanie import PydanticObjectId, init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import accounts, brokers, management, reports, trading
from app.api.v1.user import reports as user_reports
from app.core.config import settings
from app.core.database import _document_models
from app.models._base import OrderAction, OrderType, ProductType
from app.models.netting import NettingFieldsRequired, NettingSegment
from app.models.order import InstrumentRef, Order
from app.models.position import Position, PositionStatus
from app.models.trade import Trade
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import (
    accounts_dashboard_service,
    market_data_service,
    matching_engine,
    position_service,
    report_pdf_service,
    wallet_service,
)
from app.utils.time_utils import now_utc

_DB = f"{settings.MONGODB_DB_NAME}_test_summary_brokerage_counts_charges"
_PER_LOT = 40  # MCX_FUT commission, per lot
_GOLD = InstrumentRef(token="TG1", symbol="GOLDTEST", exchange="MCX", segment="MCX_FUTURE", lot_size=100)
_SILV = InstrumentRef(token="TS1", symbol="SILVTEST", exchange="MCX", segment="MCX_FUTURE", lot_size=100)
_LTP = Decimal("100")


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models())

    async def _quote(_token):
        return {"ltp": str(_LTP)}

    monkeypatch.setattr(market_data_service, "get_quote", _quote)
    n = 0

    async def mk(role: UserRole, **kw) -> User:
        nonlocal n
        n += 1
        u = User(
            user_code=f"SBC{n:03d}",
            email=f"sbc{n}@example.com",
            mobile=f"96000{n:05d}",
            full_name=f"U{n}",
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            **kw,
        )
        await u.insert()
        return u

    try:
        await NettingSegment(
            name="MCX_FUT",
            displayName="MCX_FUT",
            **{**NettingFieldsRequired().model_dump(), "commission": float(_PER_LOT)},
        ).insert()
        sa = await mk(UserRole.SUPER_ADMIN)
        a1 = await mk(UserRole.ADMIN)
        b1 = await mk(UserRole.BROKER, assigned_admin_id=a1.id)
        c = await mk(
            UserRole.CLIENT,
            assigned_admin_id=a1.id,
            assigned_broker_id=b1.id,
            broker_ancestry=[b1.id],
        )
        await Wallet(user_id=c.id, available_balance=Decimal128("100000")).insert()
        yield SimpleNamespace(sa=sa, a1=a1, b1=b1, c=c)
    finally:
        await client.drop_database(_DB)
        client.close()


async def _fill(user: User, instr: InstrumentRef, lots: int, action=OrderAction.BUY) -> Trade:
    order = Order(
        order_number=f"O{PydanticObjectId()}",
        user_id=user.id,
        placed_by=user.id,
        instrument=instr,
        action=action,
        order_type=OrderType.MARKET,
        product_type=ProductType.MIS,
        lots=lots,
        quantity=lots * instr.lot_size,
    )
    await order.insert()
    return await matching_engine.execute_market_order(order, cached_ltp=_LTP)


async def test_wallet_brokerage_readers_include_fill_charges(w, monkeypatch):
    trade = await _fill(w.c, _GOLD, 2)
    charged = Decimal(str(trade.brokerage))
    assert charged == Decimal(2 * _PER_LOT)

    # User Margin Report PDF (summary keys themselves stay as they were).
    summary = await wallet_service.summary(w.c.id)
    assert Decimal(summary["total_brokerage"]) == 0
    assert Decimal(summary["total_charges"]) == charged
    grids: list = []
    real_grid = report_pdf_service._summary_grid
    monkeypatch.setattr(
        report_pdf_service,
        "_summary_grid",
        lambda items, styles: grids.append(dict(items)) or real_grid(items, styles),
    )
    margin = await user_reports._margin_payload(w.c)
    assert report_pdf_service.build_margin_pdf(w.c, margin)[:4] == b"%PDF"
    assert grids[0]["Total brokerage"] == report_pdf_service._fmt_money(charged)

    # Broker detail and sub-admin detail pool cards.
    rep = (await brokers.broker_report(str(w.b1.id), w.a1)).data
    assert Decimal(rep["wallet"]["total_brokerage"]) == charged
    rep = (await management.sub_admin_report(str(w.a1.id), w.sa)).data
    assert Decimal(rep["wallet"]["total_brokerage"]) == charged

    # Reports → Financial.
    fin = (await reports.financial_report(w.sa, None)).data
    assert Decimal(str(fin["total_brokerage"])) == charged


async def test_trade_brokerage_sums_skip_fills_a_delete_refunded(w):
    kept = await _fill(w.c, _GOLD, 2)
    await _fill(w.c, _SILV, 1)
    silv = await Position.find_one(
        {"user_id": w.c.id, "instrument.token": _SILV.token, "status": PositionStatus.OPEN.value}
    )
    await trading.delete_position(str(silv.id), w.a1, None)
    kept_bkg = float(str(kept.brokerage))
    assert await Trade.find({"superseded_by_reopen": True}).count() == 1

    trades = (await reports.trades_report(w.sa, None)).data
    assert trades["week"]["brokerage"] == kept_bkg
    assert trades["week"]["count"] == 1

    life = await accounts._aggregate_for_users([w.c.id])
    assert life["brokerage"] == kept_bkg
    now = now_utc()
    window = await accounts._aggregate_for_users(
        [w.c.id], start_utc=now - timedelta(days=1), end_utc=now + timedelta(minutes=1)
    )
    assert window["brokerage"] == kept_bkg


async def test_carry_forward_fee_is_not_an_admin_withdrawal(w):
    seg = await NettingSegment.find_one(NettingSegment.name == "MCX_FUT")
    seg.carryForwardChargePercent = 0.5
    await seg.save()
    await _fill(w.c, _GOLD, 1)

    out = await position_service.convert_intraday_to_carry({_GOLD.segment})
    assert out["converted"] == 1 and out["charged"] == 1
    pos = await Position.find_one({"user_id": w.c.id, "instrument.token": _GOLD.token})
    assert pos.product_type == ProductType.NRML

    totals = await accounts_dashboard_service.compute_broker_totals(w.a1.id, None, None)
    assert Decimal(str(totals["total_withdrawals"])) == 0


async def test_margin_report_brokerage_leaves_out_a_refunded_fill(w, monkeypatch):
    kept = await _fill(w.c, _GOLD, 2)
    await _fill(w.c, _SILV, 1)
    silv = await Position.find_one(
        {"user_id": w.c.id, "instrument.token": _SILV.token, "status": PositionStatus.OPEN.value}
    )
    await trading.delete_position(str(silv.id), w.a1, None)  # refunds SILV's ₹40
    kept_bkg = Decimal(str(kept.brokerage))

    grids: list = []
    real_grid = report_pdf_service._summary_grid
    monkeypatch.setattr(
        report_pdf_service,
        "_summary_grid",
        lambda items, styles: grids.append(dict(items)) or real_grid(items, styles),
    )
    await user_reports.margin_pdf(user=w.c)
    # was ₹120: total_charges keeps a charge the delete refunded
    assert grids[0]["Total brokerage"] == report_pdf_service._fmt_money(kept_bkg)
    margin = (await user_reports.margin_report(w.c)).data
    assert Decimal(margin["brokerage"]) == kept_bkg
    assert Decimal(margin["total_brokerage"]) == 0  # the wallet keys stay as they were
