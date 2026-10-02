"""Integration test: when stop-out close realizes loss > balance, the
overflow books to settlement_outstanding instead of crashing the squareoff."""

from decimal import Decimal

import pytest

from app.models.transaction import TransactionType
from app.models.wallet import Wallet
from app.services import wallet_service


@pytest.mark.asyncio
async def test_squareoff_with_insufficient_balance_books_outstanding(
    db, user, wallet
):
    """Direct unit test of the force_debit fallback when adjust would raise.
    Verifies that force_debit's split-debit produces the right wallet state."""
    # Wallet starts at 1000 (from fixture). force_debit a 1500 loss.
    # Expected: balance → 0, outstanding → 500.
    await wallet_service.force_debit(
        user_id=user.id,
        amount=Decimal("1500"),
        transaction_type=TransactionType.PNL,
        narration="stop-out close - loss > balance",
        reference_type="ORDER",
        reference_id="dummy_order_id",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("0")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("500")


@pytest.mark.asyncio
async def test_squareoff_with_sufficient_balance_no_outstanding(
    db, user, wallet
):
    """Sanity: when balance covers the loss, force_debit behaves like adjust -
    no outstanding accrued. Confirms force_debit is safe to call as a fallback
    even when the regular adjust path would have succeeded."""
    await wallet_service.force_debit(
        user_id=user.id,
        amount=Decimal("700"),  # < 1000 balance
        transaction_type=TransactionType.PNL,
        narration="stop-out close - within balance",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("300")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("0")
