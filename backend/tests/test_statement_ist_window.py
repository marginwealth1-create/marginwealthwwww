"""§4.2 Statement and Trade Book windows are whole IST days, printed in IST.

The admin bill read its plain dates as UTC midnights, so its window started at
05:30 IST and ended at 05:30 IST of its last day: a single-day pick collapsed
to an instant and returned nothing. The client's statement was sent
browser-local ISO instants and printed UTC wall clocks, so a 00:30 IST close
read 19:00 the day before and the period's From date was a day early.

One parser (`tradebook_builder.window`) reads both sides' dates as whole IST
days; an ISO instant from an older app build counts as the IST day it falls
on. Every time a document prints is IST.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

from app.api.v1.admin import reports as admin_reports
from app.api.v1.user import reports as user_reports
from app.core.config import settings
from app.core.database import _document_models
from app.core.exceptions import ValidationFailedError
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import report_pdf_service, tradebook_builder
from app.utils.time_utils import IST, UTC
from tests._real_fill import market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_statement_ist_window"
DAY = "2026-11-02"


def _ist(h: int, m: int, day: int = 2) -> datetime:
    return datetime(2026, 11, day, h, m, tzinfo=IST).astimezone(UTC)


def test_dates_are_whole_ist_days():
    lo, hi = tradebook_builder.window("2026-11-02", "2026-11-08")
    assert lo == datetime(2026, 11, 1, 18, 30, tzinfo=UTC)  # 00:00:00 IST on the 2nd
    assert hi == datetime(2026, 11, 8, 18, 29, 59, 999000, tzinfo=UTC)  # 23:59:59.999 IST on the 8th
    lo, hi = tradebook_builder.window(DAY, DAY)
    assert hi - lo == timedelta(days=1, milliseconds=-1)  # was an instant
    assert tradebook_builder.window(None, None) == (None, None)


def test_an_instant_from_an_older_app_counts_as_its_ist_day():
    assert tradebook_builder.window("2026-11-01T18:30:00.000Z", "2026-11-08T18:29:59.999Z") == (
        tradebook_builder.window("2026-11-02", "2026-11-08")
    )


def test_a_date_that_is_not_one_is_refused():
    with pytest.raises(ValidationFailedError):
        tradebook_builder.window("02/11/2026", None)


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    await set_commission_per_lot(1)

    async def mk(code: str, role: UserRole) -> User:
        u = User(
            user_code=code,
            email=f"{code.lower()}@example.com",
            mobile=f"93{abs(hash(code)) % 10**8:08d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128("1000000")).insert()
        return u

    c = await mk("SIW001", UserRole.CLIENT)
    # Monday 2 Nov IST: a round trip just after midnight and a fill just
    # before the next one — all outside [05:30 IST 2 Nov, 05:30 IST 2 Nov].
    await market(c, "BUY", 10, "100", at=_ist(0, 20))
    await market(c, "SELL", 10, "110", at=_ist(0, 30))
    await market(c, "BUY", 3, "105", at=_ist(23, 50))
    await market(c, "BUY", 1, "101", at=_ist(0, 10, day=3))  # Tuesday: outside

    got: dict[str, object] = {}
    for name in ("build_tradebook_pdf", "build_full_tradebook_pdf"):
        orig = getattr(report_pdf_service, name)

        def spy(user, data, _name=name, _orig=orig):
            got[_name] = data
            return _orig(user, data)

        monkeypatch.setattr(report_pdf_service, name, spy)
    try:
        yield SimpleNamespace(c=c, sa=await mk("SIWSA", UserRole.SUPER_ADMIN), got=got)
    finally:
        await client.drop_database(_DB)
        client.close()


def _closes(payload) -> list[tuple[str, str]]:
    return [(r["time"], r["open_time"]) for r in payload["closed_transactions"] if r["type"] == "Close"]


async def test_the_admin_bill_covers_the_whole_ist_day(w):
    await admin_reports.tradebook_pdf(user_id=str(w.c.id), admin=w.sa, _=None, from_date=DAY, to_date=DAY)
    p = w.got["build_full_tradebook_pdf"]
    assert _closes(p) == [("2026-11-02 00:30:00", "2026-11-02 00:20:00")]  # IST wall clock
    assert (p["from_label"], p["to_label"]) == (DAY, DAY)


async def test_the_clients_statement_covers_the_same_day_from_plain_dates(w):
    await user_reports.tradebook_full_pdf(user=w.c, from_date=DAY, to_date=DAY)
    p = w.got["build_full_tradebook_pdf"]
    assert _closes(p) == [("2026-11-02 00:30:00", "2026-11-02 00:20:00")]
    assert (p["from_label"], p["to_label"]) == (DAY, DAY)  # was 2026-11-01 from the UTC instant


async def test_both_trade_books_cover_the_whole_ist_day_and_print_ist(w):
    await admin_reports.trade_book_pdf(user_id=str(w.c.id), admin=w.sa, _=None, from_date=DAY, to_date=DAY)
    admin_rows = w.got["build_tradebook_pdf"]
    await user_reports.tradebook_pdf(user=w.c, from_date=DAY, to_date=DAY, limit=None)
    assert w.got["build_tradebook_pdf"] == admin_rows
    assert [(r["action"], r["quantity"], Decimal(r["price"])) for r in admin_rows] == [
        ("BUY", 3, Decimal("105")),
        ("SELL", 10, Decimal("110")),
        ("BUY", 10, Decimal("100")),
    ]
    assert report_pdf_service._fmt_date(admin_rows[0]["executed_at"]) == "02 Nov 2026, 23:50"
