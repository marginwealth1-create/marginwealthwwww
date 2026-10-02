from __future__ import annotations

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.models.transaction import WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet


@pytest_asyncio.fixture(scope="function")
async def db():
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    test_db = client[f"{settings.MONGODB_DB_NAME}_test_settlement_outstanding"]
    await init_beanie(
        database=test_db,
        document_models=[User, Wallet, WalletTransaction],
    )
    yield test_db
    await client.drop_database(test_db.name)
    client.close()


@pytest_asyncio.fixture
async def user(db) -> User:
    u = User(
        user_code="TUSER001",
        email="user_test_so@example.com",
        mobile="9999900200",
        full_name="Test User",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
    )
    await u.insert()
    return u


@pytest_asyncio.fixture
async def wallet(db, user) -> Wallet:
    w = Wallet(
        user_id=user.id,
        available_balance=Decimal128("1000"),
    )
    await w.insert()
    return w
