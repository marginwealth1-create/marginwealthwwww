"""The Billing statement adds up: Opening + Funds + Net PnL is its Balance.

A ledger row's balance_before / balance_after is the FREE cash: blocking
margin for an open position writes no row, and a loss past the wallet floors
it at 0 and books the rest as owed (settlement outstanding). Read off those,
a statement whose window opened or closed with a position open, or held a
stop-out, did not add up — on prod 43 client-weeks, T73EMF by ₹4.5 lakh of
margin its open positions held.

Balance is what the account is worth on the ledger at the window's edge:
cash, margin in open positions included, less what the client owes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import trading
from app.core.config import settings
from app.core.database import _document_models
from app.models.position import Position
from app.models.transaction import SettlementRequest, TransactionType
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import tradebook_builder, wallet_service
from app.utils.time_utils import now_utc, to_ist
from tests._real_fill import later, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_statement_balance"
WEEK = 8 * 24 * 60  # minutes: the next trade lands in the next window


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(trading, "publish", _no_publish)
    await set_commission_per_lot(1)  # ₹1 a unit, both legs

    async def mk(code: str, role: UserRole = UserRole.CLIENT, **kw) -> User:
        u = User(
            user_code=code,
            email=f"{code.lower()}@example.com",
            mobile=f"93{abs(hash(code)) % 10**8:08d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            **kw,
        )
        await u.insert()
        await Wallet(user_id=u.id).insert()
        return u

    try:
        yield SimpleNamespace(db=client[_DB], mk=mk, sa=await mk("SBSA", UserRole.SUPER_ADMIN))
    finally:
        await client.drop_database(_DB)
        client.close()


async def _deposit(c: User, amount: str) -> None:
    await wallet_service.adjust(
        c.id, Decimal(amount), transaction_type=TransactionType.DEPOSIT, narration="UPI"
    )


async def _statement(c: User, from_days: int, to_days: int) -> dict:
    """The statement over IST days [today - from_days, today - to_days]."""
    today = to_ist(now_utc()).date()
    lo, hi = tradebook_builder.window(
        (today - timedelta(days=from_days)).isoformat(), (today - timedelta(days=to_days)).isoformat()
    )
    return await tradebook_builder.statement_payload(c, lo, hi)


def _check(payload: dict) -> tuple[Decimal, Decimal]:
    """(Opening + Net Funds + Net PnL, Balance) — one figure when it adds up.
    Returns (opening, balance)."""
    funds, fin = payload["funds"], payload["financial"]
    opening = Decimal(str(funds["opening_balance"]))
    net_funds = sum((Decimal(str(r["amount"])) for r in funds["rows"]), Decimal(0))
    balance = Decimal(str(fin["balance"]))
    assert opening + net_funds + Decimal(str(fin["net_pnl"])) == balance, (payload["from_label"], funds, fin)
    return opening, balance


async def _worth(c: User) -> Decimal:
    wallet = await Wallet.find_one(Wallet.user_id == c.id)
    return (
        Decimal(str(wallet.available_balance))
        + Decimal(str(wallet.used_margin))
        - Decimal(str(wallet.settlement_outstanding))
    )


async def test_a_position_open_at_either_edge_still_adds_up(w):
    """The margin a carried position holds stays in the Balance: the first
    week closed with 5,000 blocked (its Balance read 5,000 short) and the
    next opened with it (its Opening read 5,000 short)."""
    c = await w.mk("SB001")
    await _deposit(c, "20000")
    await market(c, "BUY", 10, "1000", margin="5000")  # brokerage 10, carried
    await later(w.db, minutes=WEEK)
    await market(c, "SELL", 4, "1100")  # +400 − 4; 6 still carried

    _, first_close = _check(await _statement(c, 13, 7))
    second_open, second_close = _check(await _statement(c, 6, 0))
    assert first_close == second_open == Decimal("19990")
    assert second_close == await _worth(c) == Decimal("20386")


@pytest.mark.parametrize("mode", ["auto", "approved", "clamped"])
async def test_a_stop_out_past_the_wallet_still_adds_up(w, mode):
    """A stop-out losing 3,020 on a 1,000 wallet: the client owes 2,020 —
    auto settlement books it at the close; manual settlement lets the wallet
    go negative until an admin approves the due (or the boot migration
    clamps it). The statement's Balance is −2,020 every way, and the next
    deposit pays the due off first."""
    c = await w.mk("SB002", auto_settlement=mode == "auto")
    await _deposit(c, "1000")
    await market(c, "BUY", 10, "1000", margin="900")  # brokerage 10
    await market(c, "SELL", 10, "700", squareoff=True)  # −3,000, brokerage 10
    if mode == "approved":
        req = SettlementRequest(user_id=c.id, requested_amount=Decimal128("2020"))
        await req.insert()
        await wallet_service.approve_settlement_request(req.id, w.sa.id, w.sa.user_code)
    elif mode == "clamped":
        await wallet_service.clamp_negative_balances_to_settlement()
    wallet = await Wallet.find_one(Wallet.user_id == c.id)
    assert Decimal(str(wallet.settlement_outstanding)) == Decimal("2020")
    await later(w.db, minutes=WEEK)
    await _deposit(c, "5000")

    _, first_close = _check(await _statement(c, 13, 7))
    second_open, second_close = _check(await _statement(c, 6, 0))
    assert first_close == second_open == Decimal("-2020")
    assert second_close == await _worth(c) == Decimal("2980")


async def test_a_deleted_stop_out_leaves_the_deposit(w):
    """An admin deletes the stop-out: the reversal refunds what the wallet
    paid and unbooks the due (a RECOVERY row that moves no cash). Nothing
    happened, so the statement is the deposit alone."""
    c = await w.mk("SB003")
    await _deposit(c, "1000")
    await market(c, "BUY", 10, "1000", margin="900")
    await market(c, "SELL", 10, "700", squareoff=True)
    await later(w.db, minutes=10)
    [p] = await Position.find(Position.user_id == c.id).to_list()
    await trading.delete_position(str(p.id), w.sa, None)

    opening, balance = _check(await _statement(c, 6, 0))
    assert (opening, balance) == (Decimal(0), Decimal("1000"))
    assert await _worth(c) == Decimal("1000")
