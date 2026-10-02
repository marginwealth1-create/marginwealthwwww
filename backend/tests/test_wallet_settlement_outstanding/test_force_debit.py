from decimal import Decimal

import pytest
from bson import Decimal128

from app.models.transaction import TransactionType, WalletTransaction
from app.models.wallet import Wallet
from app.services import wallet_service


@pytest.mark.asyncio
async def test_force_debit_within_balance_no_outstanding(db, user, wallet):
    """Debit <= available_balance: behaves like normal adjust(), no outstanding accrued."""
    tx = await wallet_service.force_debit(
        user_id=user.id,
        amount=Decimal("500"),
        transaction_type=TransactionType.TRADE,
        narration="stop-out close",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("500")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("0")
    assert tx is not None


@pytest.mark.asyncio
async def test_force_debit_exceeds_balance_books_outstanding(db, user, wallet):
    """Debit > available_balance: balance floors at 0, excess goes to outstanding."""
    await wallet_service.force_debit(
        user_id=user.id,
        amount=Decimal("1500"),
        transaction_type=TransactionType.TRADE,
        narration="stop-out close exceeds balance",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("0")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("500")


@pytest.mark.asyncio
async def test_force_debit_with_zero_balance_full_outstanding(db, user, wallet):
    """Debit on an already-empty wallet: full amount goes to outstanding."""
    wallet.available_balance = Decimal128("0")
    await wallet.save()

    await wallet_service.force_debit(
        user_id=user.id,
        amount=Decimal("750"),
        transaction_type=TransactionType.TRADE,
        narration="full outstanding accrual",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("0")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("750")


@pytest.mark.asyncio
async def test_force_debit_adds_to_existing_outstanding(db, user, wallet):
    """Second force_debit when outstanding already exists: stacks up."""
    wallet.available_balance = Decimal128("0")
    wallet.settlement_outstanding = Decimal128("200")
    await wallet.save()

    await wallet_service.force_debit(
        user_id=user.id,
        amount=Decimal("300"),
        transaction_type=TransactionType.TRADE,
        narration="additional shortfall",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("0")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("500")


@pytest.mark.asyncio
async def test_force_debit_writes_two_transactions_when_partial(db, user, wallet):
    """When debit splits across balance + outstanding, expect TWO wallet
    transactions: one for the balance debit, one for the SETTLEMENT_OUTSTANDING_BOOKED."""
    await wallet_service.force_debit(
        user_id=user.id,
        amount=Decimal("1500"),
        transaction_type=TransactionType.TRADE,
        narration="split",
    )
    txns = await WalletTransaction.find(WalletTransaction.user_id == user.id).to_list()
    types = [t.transaction_type for t in txns]
    assert TransactionType.TRADE in types
    assert TransactionType.SETTLEMENT_OUTSTANDING_BOOKED in types
