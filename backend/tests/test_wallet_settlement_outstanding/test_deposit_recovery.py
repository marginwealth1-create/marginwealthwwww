from decimal import Decimal

import pytest
from bson import Decimal128

from app.models.transaction import TransactionType, WalletTransaction
from app.models.wallet import Wallet
from app.services import wallet_service


@pytest.mark.asyncio
async def test_deposit_no_outstanding_credits_normally(db, user, wallet):
    """Deposit with zero outstanding: full amount credits available_balance."""
    await wallet_service.adjust(
        user_id=user.id,
        amount=Decimal("2000"),
        transaction_type=TransactionType.DEPOSIT,
        narration="user deposit",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("3000")  # 1000 + 2000
    assert Decimal(str(w.settlement_outstanding)) == Decimal("0")


@pytest.mark.asyncio
async def test_deposit_smaller_than_outstanding_full_recovery(db, user, wallet):
    """Outstanding 1000, deposit 600 → outstanding becomes 400, balance unchanged."""
    wallet.available_balance = Decimal128("0")
    wallet.settlement_outstanding = Decimal128("1000")
    await wallet.save()

    await wallet_service.adjust(
        user_id=user.id,
        amount=Decimal("600"),
        transaction_type=TransactionType.DEPOSIT,
        narration="partial recovery deposit",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("0")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("400")


@pytest.mark.asyncio
async def test_deposit_exactly_equals_outstanding(db, user, wallet):
    """Outstanding 500, deposit 500 → outstanding cleared, balance unchanged."""
    wallet.available_balance = Decimal128("0")
    wallet.settlement_outstanding = Decimal128("500")
    await wallet.save()

    await wallet_service.adjust(
        user_id=user.id,
        amount=Decimal("500"),
        transaction_type=TransactionType.DEPOSIT,
        narration="exact recovery",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("0")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("0")


@pytest.mark.asyncio
async def test_deposit_larger_than_outstanding_splits(db, user, wallet):
    """Outstanding 300, deposit 1000 → outstanding cleared, balance += 700."""
    wallet.available_balance = Decimal128("0")
    wallet.settlement_outstanding = Decimal128("300")
    await wallet.save()

    await wallet_service.adjust(
        user_id=user.id,
        amount=Decimal("1000"),
        transaction_type=TransactionType.DEPOSIT,
        narration="recovery + credit",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("700")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("0")


@pytest.mark.asyncio
async def test_deposit_writes_recovery_transaction(db, user, wallet):
    """When recovery happens, a SETTLEMENT_OUTSTANDING_RECOVERY transaction is logged."""
    wallet.available_balance = Decimal128("0")
    wallet.settlement_outstanding = Decimal128("400")
    await wallet.save()

    await wallet_service.adjust(
        user_id=user.id,
        amount=Decimal("1000"),
        transaction_type=TransactionType.DEPOSIT,
        narration="user deposit",
    )
    txns = await WalletTransaction.find(WalletTransaction.user_id == user.id).to_list()
    types = [t.transaction_type for t in txns]
    assert TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY in types
    assert TransactionType.DEPOSIT in types


@pytest.mark.asyncio
async def test_excluded_types_do_not_trigger_recovery(db, user, wallet):
    """REVERSAL and the SETTLEMENT_OUTSTANDING_* accounting types are
    explicitly excluded from recovery (would create accounting loops).
    All OTHER credit types — DEPOSIT, BONUS, ADJUSTMENT, PROMO, etc. —
    DO trigger recovery."""
    wallet.available_balance = Decimal128("100")
    wallet.settlement_outstanding = Decimal128("400")
    await wallet.save()

    await wallet_service.adjust(
        user_id=user.id,
        amount=Decimal("500"),
        transaction_type=TransactionType.REVERSAL,
        narration="reversal of prior debit",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("600")  # 100 + 500
    assert Decimal(str(w.settlement_outstanding)) == Decimal("400")  # untouched


# ── Recovery now fires for ANY credit type (Option C broadening) ────


@pytest.mark.asyncio
async def test_bonus_credit_recovers_outstanding(db, user, wallet):
    """BONUS credits clear outstanding before crediting balance — same rule
    as DEPOSIT. Previously BONUS bypassed recovery entirely."""
    wallet.available_balance = Decimal128("0")
    wallet.settlement_outstanding = Decimal128("400")
    await wallet.save()

    await wallet_service.adjust(
        user_id=user.id,
        amount=Decimal("1000"),
        transaction_type=TransactionType.BONUS,
        narration="referral bonus",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("600")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("0")


@pytest.mark.asyncio
async def test_adjustment_credit_recovers_outstanding(db, user, wallet):
    """Admin manual ADJUSTMENT credit clears outstanding too."""
    wallet.available_balance = Decimal128("0")
    wallet.settlement_outstanding = Decimal128("500")
    await wallet.save()

    await wallet_service.adjust(
        user_id=user.id,
        amount=Decimal("200"),
        transaction_type=TransactionType.ADJUSTMENT,
        narration="manual credit",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("0")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("300")


@pytest.mark.asyncio
async def test_reversal_credit_does_not_recover_outstanding(db, user, wallet):
    """REVERSAL is excluded — it would create accounting loops with the
    transaction it's reversing. Reversal credits bypass recovery."""
    wallet.available_balance = Decimal128("0")
    wallet.settlement_outstanding = Decimal128("500")
    await wallet.save()

    await wallet_service.adjust(
        user_id=user.id,
        amount=Decimal("300"),
        transaction_type=TransactionType.REVERSAL,
        narration="reversing a prior debit",
    )
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("300")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("500")


# ── Margin release also recovers outstanding ─────────────────────────


@pytest.mark.asyncio
async def test_release_margin_recovers_outstanding_first(db, user, wallet):
    """When margin is released after a force-debit booked dues, the freed
    margin clears the outstanding before any remainder credits balance."""
    wallet.available_balance = Decimal128("0")
    wallet.used_margin = Decimal128("3000")
    wallet.settlement_outstanding = Decimal128("1000")
    await wallet.save()

    await wallet_service.release_margin(user.id, Decimal("3000"))

    w = await Wallet.find_one(Wallet.user_id == user.id)
    # Outstanding cleared, balance gets the remaining 2000
    assert Decimal(str(w.used_margin)) == Decimal("0")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("0")
    assert Decimal(str(w.available_balance)) == Decimal("2000")


@pytest.mark.asyncio
async def test_release_margin_partial_recovery_when_margin_under_outstanding(db, user, wallet):
    """If released margin is less than outstanding, all of it goes to recovery
    and the balance is unchanged."""
    wallet.available_balance = Decimal128("0")
    wallet.used_margin = Decimal128("500")
    wallet.settlement_outstanding = Decimal128("2000")
    await wallet.save()

    await wallet_service.release_margin(user.id, Decimal("500"))

    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.used_margin)) == Decimal("0")
    assert Decimal(str(w.available_balance)) == Decimal("0")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("1500")


@pytest.mark.asyncio
async def test_release_margin_no_outstanding_credits_full_amount(db, user, wallet):
    """Backward-compat: when outstanding is 0, release_margin behaves exactly
    as before — credits the full released amount to available_balance."""
    wallet.available_balance = Decimal128("1000")
    wallet.used_margin = Decimal128("500")
    wallet.settlement_outstanding = Decimal128("0")
    await wallet.save()

    await wallet_service.release_margin(user.id, Decimal("500"))

    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.used_margin)) == Decimal("0")
    assert Decimal(str(w.available_balance)) == Decimal("1500")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("0")
