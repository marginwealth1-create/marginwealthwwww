"""§4 The Trade Book and the Billing statement are two documents.

- Trade Book (`build_tradebook_pdf` ← `tradebook_builder.trade_rows`): one row
  per fill, newest first — what was executed. A fill an admin delete took back
  is not on it.
- Billing (`build_full_tradebook_pdf` ← `tradebook_builder.statement_payload`):
  the same trades put through FIFO pairing, dressed as a statement.

The per-fill rows sat as a private helper inside the USER reports router, so
the admin could reach only the statement. Both documents now come from shared
builders; the admin gets a Trade Book endpoint behind the same gate as the
Billing one, and the client's own statement and the admin's are the same
statement (one assembly). Every table ReportLab is handed has as many cells
per row as its header and columns that fill the frame: a row with one cell
more silently shifts every number after it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient
from reportlab.platypus import SimpleDocTemplate, Table

from app.api.v1.admin import reports as admin_reports
from app.api.v1.admin import trading
from app.api.v1.user import reports as user_reports
from app.core.config import settings
from app.core.database import _document_models
from app.core.exceptions import InsufficientPermissionsError
from app.models.position import Position
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import market_data_service, report_pdf_service, tradebook_builder
from app.utils.time_utils import UTC, now_utc, to_ist
from tests._real_fill import INSTRUMENT, later, market, set_commission_per_lot

_DB = f"{settings.MONGODB_DB_NAME}_test_tradebook_vs_billing"
OTHER = INSTRUMENT.model_copy(update={"token": "TBVB2", "symbol": "TBVO"})


@pytest_asyncio.fixture
async def w(monkeypatch):
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def _no_publish(*a, **k):
        return None

    async def _ltp(_token):
        return Decimal("120")

    monkeypatch.setattr("app.core.redis_client.publish", _no_publish)
    monkeypatch.setattr(trading, "publish", _no_publish)
    monkeypatch.setattr(market_data_service, "get_ltp", _ltp)
    monkeypatch.setattr(tradebook_builder, "now_utc", lambda: datetime(2026, 11, 13, 6, 0, tzinfo=UTC))
    await set_commission_per_lot(1)

    async def mk(code: str, role: UserRole, **kw) -> User:
        u = User(
            user_code=code,
            email=f"{code.lower()}@example.com",
            mobile=f"94{abs(hash(code)) % 10**8:08d}",
            full_name=code,
            password_hash="x",
            role=role,
            status=UserStatus.ACTIVE,
            **kw,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128("1000000")).insert()
        return u

    sa = await mk("TBVSA", UserRole.SUPER_ADMIN)
    try:
        yield SimpleNamespace(
            db=client[_DB],
            c=await mk("TBV001", UserRole.CLIENT),
            sa=sa,
            other_admin=await mk("TBVAD", UserRole.ADMIN, brand_name="Elsewhere"),
        )
    finally:
        await client.drop_database(_DB)
        client.close()


@pytest.fixture
def built(monkeypatch):
    """Every document ReportLab builds, with the flowables it was handed."""
    docs: list[tuple[SimpleDocTemplate, list]] = []
    orig = SimpleDocTemplate.build

    def build(self, flowables, *a, **k):
        docs.append((self, list(flowables)))
        return orig(self, flowables, *a, **k)

    monkeypatch.setattr(SimpleDocTemplate, "build", build)
    return docs


@pytest.fixture
def handed(monkeypatch):
    """What each PDF builder was handed (it still renders)."""
    got: dict[str, object] = {}
    for name in ("build_tradebook_pdf", "build_full_tradebook_pdf"):
        orig = getattr(report_pdf_service, name)

        def spy(user, data, _name=name, _orig=orig):
            got[_name] = data
            return _orig(user, data)

        monkeypatch.setattr(report_pdf_service, name, spy)
    return got


def _check_tables(built) -> None:
    assert built
    for doc, flowables in built:
        tables = [f for f in flowables if isinstance(f, Table)]
        assert tables
        for t in tables:
            rows = t._cellvalues
            assert all(len(r) == len(rows[0]) for r in rows), [len(r) for r in rows]
            assert sum(t._argW) == pytest.approx(doc.width)


async def _book(w) -> None:
    await market(w.c, "BUY", 10, "100")
    await market(w.c, "BUY", 10, "102")
    await later(w.db, seconds=5)
    await market(w.c, "SELL", 20, "110")
    await later(w.db, seconds=5)
    await market(w.c, "BUY", 5, "200", instrument=OTHER)  # still open


async def test_the_trade_book_is_per_fill_and_billing_is_fifo_paired(w):
    await _book(w)
    fills = await tradebook_builder.trade_rows(w.c.id, None, None)
    sides = [(r["action"], r["quantity"]) for r in fills]
    assert sides == [("BUY", 5), ("SELL", 20), ("BUY", 10), ("BUY", 10)]

    # One sell, two FIFO rows: one per lot it took, each billed at the
    # average the wallet booked.
    rows, _, _ = await tradebook_builder.fifo_closed_rows(w.c.id, None, None)
    paired = [(r["amount"], r["open_price"], r["close_price"]) for r in rows]
    assert paired == [("10.00", "101.00", "110.00")] * 2


async def test_admin_reaches_both_documents_and_they_differ(w, handed, built):
    await _book(w)
    await admin_reports.trade_book_pdf(user_id=str(w.c.id), admin=w.sa, _=None)
    await admin_reports.tradebook_pdf(user_id=str(w.c.id), admin=w.sa, _=None)

    assert handed["build_tradebook_pdf"] == await tradebook_builder.trade_rows(w.c.id, None, None)
    assert "closed_transactions" in handed["build_full_tradebook_pdf"]
    _check_tables(built)


async def test_a_deleted_position_leaves_the_trade_book(w):
    await _book(w)
    [other] = await Position.find(Position.user_id == w.c.id, {"instrument.token": OTHER.token}).to_list()
    await trading.delete_position(str(other.id), w.sa, None)
    fills = await tradebook_builder.trade_rows(w.c.id, None, None)
    assert [r["symbol"] for r in fills] == ["TBKG", "TBKG", "TBKG"]


async def test_the_trade_book_is_gated_like_billing(w):
    await _book(w)
    with pytest.raises(InsufficientPermissionsError):
        await admin_reports.trade_book_pdf(user_id=str(w.c.id), admin=w.other_admin, _=None)
    with pytest.raises(InsufficientPermissionsError):
        await admin_reports.tradebook_pdf(user_id=str(w.c.id), admin=w.other_admin, _=None)


async def test_the_client_and_the_admin_get_one_statement(w, handed, built):
    await _book(w)
    day = to_ist(now_utc()).date().isoformat()
    await user_reports.tradebook_full_pdf(user=w.c, from_date=day, to_date=day)
    mine = handed["build_full_tradebook_pdf"]
    await admin_reports.tradebook_pdf(user_id=str(w.c.id), admin=w.sa, _=None, from_date=day, to_date=day)
    theirs = handed["build_full_tradebook_pdf"]

    assert {**mine, "admin_brand_name": ""} == {**theirs, "admin_brand_name": ""}
    assert mine == await tradebook_builder.statement_payload(w.c, *tradebook_builder.window(day, day))
    assert [r["script"] for r in mine["closed_transactions"]] == ["TBKG", "TBKG"]
    assert [d["script"] for d in mine["opened_deals"]] == ["TBVO"]
    _check_tables(built)
