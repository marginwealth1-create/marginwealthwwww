"""auto_settlement OFF (default since 2026-11-09): a loss past the balance
takes it below ₹0; money added later pays the minus back and unblocks trading."""
from decimal import Decimal

import pytest

from app.models.transaction import SettlementRequest, SettlementStatus, TransactionType
from app.models.wallet import Wallet
from app.services import wallet_service


async def _bal(uid):
    w = await Wallet.find_one(Wallet.user_id == uid)
    return Decimal(str(w.available_balance)), Decimal(str(w.settlement_outstanding))


@pytest.mark.asyncio
async def test_loss_goes_minus_and_money_added_pays_it_back(db, user, wallet):
    user.auto_settlement = False
    await user.save()

    await wallet_service.force_debit(
        user_id=user.id, amount=Decimal("1500"),
        transaction_type=TransactionType.PNL, narration="stop-out loss",
    )
    assert await _bal(user.id) == (Decimal("-500"), Decimal("0"))  # minus, no settlement
    assert await wallet_service.has_pending_settlement_request(user.id) is True

    # admin "add fund" smaller than the minus must not be refused
    await wallet_service.adjust(user.id, Decimal("300"), transaction_type=TransactionType.ADJUSTMENT, narration="add fund")
    assert (await _bal(user.id))[0] == Decimal("-200")
    assert await wallet_service.has_pending_settlement_request(user.id) is True

    await wallet_service.adjust(user.id, Decimal("700"), transaction_type=TransactionType.DEPOSIT, narration="deposit")
    assert (await _bal(user.id))[0] == Decimal("500")  # 200 minus recovered from the deposit
    assert await wallet_service.has_pending_settlement_request(user.id) is False
    req = await SettlementRequest.find_one(SettlementRequest.user_id == user.id)
    assert req.status == SettlementStatus.REJECTED


@pytest.mark.asyncio
async def test_new_users_default_to_minus_mode():
    from app.models.user import User

    assert User.model_fields["auto_settlement"].default is False
