"""TimestampMixin must move `updated_at` on every Beanie write (handoff §3.1).

Before the fix the docstring promised `before_event` hooks that did not exist
and the `save_changes` override was never called, so a settings edit through
`.save()` left `updated_at == created_at` forever.
"""

from __future__ import annotations

import asyncio

import pytest_asyncio
from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.models.netting import NettingFieldsRequired, NettingSegment

_DB = f"{settings.MONGODB_DB_NAME}_test_timestamp_mixin"


@pytest_asyncio.fixture
async def seg():
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=[NettingSegment])
    try:
        s = NettingSegment(
            name="MCX_FUT", displayName="MCX_FUT", **NettingFieldsRequired().model_dump()
        )
        await s.insert()
        yield s
    finally:
        await client.drop_database(_DB)
        client.close()


async def _stored(s: NettingSegment) -> NettingSegment:
    got = await NettingSegment.get(s.id)
    assert got is not None
    return got


async def test_save_moves_updated_at_and_keeps_created_at(seg):
    before = await _stored(seg)
    await asyncio.sleep(0.01)  # Mongo dates are ms-precision
    seg.commission = 0.0
    await seg.save()
    after = await _stored(seg)
    assert after.commission == 0.0
    assert after.updated_at > before.updated_at
    assert after.created_at == before.created_at


async def test_replace_moves_updated_at(seg):
    t0 = (await _stored(seg)).updated_at
    await asyncio.sleep(0.01)
    seg.commission = 7.0
    await seg.replace()
    assert (await _stored(seg)).updated_at > t0
