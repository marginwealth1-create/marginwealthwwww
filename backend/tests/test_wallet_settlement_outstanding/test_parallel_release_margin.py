"""Regression: parallel margin releases during a multi-position stop-out must
not lose updates — the CL30479363 phantom-settlement root cause.

Before the fix, `release_margin` did a non-atomic read-modify-write
`w.save()` (full-document replace), so two concurrent releases (fired by the
stop-out's `asyncio.gather`) read the SAME wallet version and the second
`save()` clobbered the first. One position's freed margin stayed stranded in
`used_margin` while `available_balance` was left short — which then booked a
phantom `settlement_outstanding`, later flooding back as an inflated
`available_balance`. The version-guarded atomic release makes every concurrent
release land exactly once.
"""

import asyncio
from decimal import Decimal

import pytest
from bson import Decimal128

from app.models.wallet import Wallet
from app.services import wallet_service


@pytest.mark.asyncio
async def test_parallel_release_margin_no_lost_update(db, user, wallet):
    # Two positions' margin blocked: used_margin = 800 (e.g. 500 + 300),
    # available = 200. A stop-out flattens both → two release_margin calls
    # run concurrently.
    await Wallet.get_motor_collection().update_one(
        {"_id": wallet.id},
        {
            "$set": {
                "available_balance": Decimal128("200"),
                "used_margin": Decimal128("800"),
            }
        },
    )

    await asyncio.gather(
        wallet_service.release_margin(user.id, Decimal("500")),
        wallet_service.release_margin(user.id, Decimal("300")),
    )

    w = await Wallet.find_one(Wallet.user_id == user.id)
    # Both releases must land: used_margin 800 -> 0, available 200 -> 1000.
    # (Old non-atomic code lost one release: used stuck at 300/500.)
    assert Decimal(str(w.used_margin)) == Decimal("0")
    assert Decimal(str(w.available_balance)) == Decimal("1000")


@pytest.mark.asyncio
async def test_net_phantom_settlement_clears_stranded_split(db, user, wallet):
    """The exact CL30479363 end-state: high available + a phantom settlement
    the freed margin can cover. net_phantom_settlement must net it,
    net-neutral on equity (available - settlement stays 297.57)."""
    await Wallet.get_motor_collection().update_one(
        {"_id": wallet.id},
        {
            "$set": {
                "available_balance": Decimal128("745.68"),
                "used_margin": Decimal128("0"),
                "settlement_outstanding": Decimal128("448.11"),
            }
        },
    )

    # settlement_before the stop-out batch was 0 -> all 448.11 is phantom.
    txn = await wallet_service.net_phantom_settlement(user.id, Decimal("0"))

    assert txn is not None
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("297.57")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("0")


@pytest.mark.asyncio
async def test_net_phantom_settlement_leaves_genuine_shortfall(db, user, wallet):
    """Genuine capital-exhausted shortfall (available == 0, settlement > 0)
    must be left untouched — no available to net against."""
    await Wallet.get_motor_collection().update_one(
        {"_id": wallet.id},
        {
            "$set": {
                "available_balance": Decimal128("0"),
                "used_margin": Decimal128("0"),
                "settlement_outstanding": Decimal128("500"),
            }
        },
    )

    txn = await wallet_service.net_phantom_settlement(user.id, Decimal("0"))

    assert txn is None
    w = await Wallet.find_one(Wallet.user_id == user.id)
    assert Decimal(str(w.available_balance)) == Decimal("0")
    assert Decimal(str(w.settlement_outstanding)) == Decimal("500")
