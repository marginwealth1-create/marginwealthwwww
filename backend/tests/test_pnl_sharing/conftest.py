"""Shared fixtures for pnl_sharing service tests.

Spins up a per-function MongoDB test database (dropped on teardown) and
inserts admin / broker / client users plus a default agreement that
test functions can mutate freely.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.models.pnl_sharing import (
    AgreementStatus,
    PnlSharingAgreement,
    PnlSharingSettlement,
    SettlementMode,
)
from app.models.position import Position
from app.models.transaction import WalletTransaction
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet


@pytest_asyncio.fixture(scope="function")
async def db():
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    test_db_name = f"{settings.MONGODB_DB_NAME}_test_pnl_sharing"
    test_db = client[test_db_name]
    await init_beanie(
        database=test_db,
        document_models=[
            User,
            Position,
            Wallet,
            WalletTransaction,
            PnlSharingAgreement,
            PnlSharingSettlement,
        ],
    )
    try:
        yield test_db
    finally:
        await client.drop_database(test_db_name)
        client.close()


@pytest_asyncio.fixture
async def admin_user(db) -> User:
    u = User(
        user_code="TADM001",
        email="testadmin_pnl@example.com",
        mobile="9999900001",
        full_name="Test Admin",
        password_hash="x",
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    await u.insert()
    return u


@pytest_asyncio.fixture
async def broker_user(db, admin_user) -> User:
    u = User(
        user_code="TBRK001",
        email="testbroker_pnl@example.com",
        mobile="9999900002",
        full_name="Test Broker",
        password_hash="x",
        role=UserRole.BROKER,
        status=UserStatus.ACTIVE,
        assigned_admin_id=admin_user.id,
    )
    await u.insert()
    return u


@pytest_asyncio.fixture
async def client_user(db, admin_user, broker_user) -> User:
    u = User(
        user_code="TCLI001",
        email="testclient_pnl@example.com",
        mobile="9999900003",
        full_name="Test Client",
        password_hash="x",
        role=UserRole.CLIENT,
        status=UserStatus.ACTIVE,
        assigned_admin_id=admin_user.id,
        assigned_broker_id=broker_user.id,
        broker_ancestry=[broker_user.id],
    )
    await u.insert()
    return u


@pytest_asyncio.fixture
async def agreement(db, admin_user, broker_user) -> PnlSharingAgreement:
    a = PnlSharingAgreement(
        admin_id=admin_user.id,
        broker_id=broker_user.id,
        share_pct=Decimal128("30"),
        settlement_mode=SettlementMode.MANUAL,
        settlement_cadence=None,
        status=AgreementStatus.ACTIVE,
        effective_from=datetime(2026, 5, 1, tzinfo=UTC),
        created_by=admin_user.id,
        last_modified_by=admin_user.id,
    )
    await a.insert()
    return a
