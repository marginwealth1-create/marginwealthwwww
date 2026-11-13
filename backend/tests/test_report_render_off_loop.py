"""§4.4 Reports render in a worker thread, not on the event loop.

reportlab and openpyxl are synchronous and CPU-bound. A lifetime statement
of ~3000 rows takes ~1.7 s to build; rendered inline in an `async def`
endpoint, it freezes the event loop for that long, and the ticker,
risk_enforcer_loop and pending_order_poller sharing the process miss their
ticks — a stop-out or SL-M fires late, at whatever LTP is current then.
Every endpoint hands its builder to `asyncio.to_thread`; the scope check
stays before it, on the loop.
"""

from __future__ import annotations

import ast
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from beanie import init_beanie
from bson import Decimal128
from motor.motor_asyncio import AsyncIOMotorClient

import app
from app.api.v1.admin import accounts
from app.api.v1.admin import reports as admin_reports
from app.api.v1.user import reports as user_reports
from app.core.config import settings
from app.core.database import _document_models
from app.models.user import User, UserRole, UserStatus
from app.models.wallet import Wallet
from app.services import accounts_dashboard_service as ads
from app.services import report_pdf_service, report_xlsx_service

_DB = f"{settings.MONGODB_DB_NAME}_test_report_render_off_loop"
_NO_DATES = {"from_date": None, "to_date": None, "preset": None, "_": None}
_BUILDERS = {
    report_pdf_service: (
        "build_pnl_pdf", "build_tradebook_pdf", "build_brokerage_pdf",
        "build_tax_pdf", "build_margin_pdf", "build_full_tradebook_pdf",
    ),
    report_xlsx_service: ("render_billing_xlsx", "render_trade_book_xlsx"),
    ads: (
        "render_entity_users_excel", "render_entity_users_pdf",
        "render_broker_totals_excel", "render_broker_totals_pdf",
    ),
}


@pytest_asyncio.fixture
async def w():
    client = AsyncIOMotorClient(settings.MONGODB_URL, tz_aware=True)
    await client.drop_database(_DB)
    await init_beanie(database=client[_DB], document_models=_document_models(), skip_indexes=True)

    async def mk(code: str, role: UserRole) -> User:
        u = User(
            user_code=code, email=f"{code.lower()}@example.com", mobile=f"96{len(code):08d}",
            full_name=code, password_hash="x", role=role, status=UserStatus.ACTIVE,
        )
        await u.insert()
        await Wallet(user_id=u.id, available_balance=Decimal128("1000")).insert()
        return u

    try:
        c, sa = await mk("ROL001", UserRole.CLIENT), await mk("ROLSA", UserRole.SUPER_ADMIN)
        yield SimpleNamespace(c=c, sa=sa)
    finally:
        await client.drop_database(_DB)
        client.close()


@pytest.fixture
def renders(monkeypatch):
    """(builder, thread it ran on) for every document built; it still renders."""
    seen: list[tuple[str, int]] = []
    for mod, names in _BUILDERS.items():
        for name in names:
            orig = getattr(mod, name)

            def spy(*a, _name=name, _orig=orig, **k):
                seen.append((_name, threading.get_ident()))
                return _orig(*a, **k)

            monkeypatch.setattr(mod, name, spy)
    return seen


async def test_every_report_renders_in_a_worker_thread(w, renders):
    uid, eid = str(w.c.id), str(w.sa.id)
    await admin_reports.tradebook_pdf(user_id=uid, admin=w.sa, _=None)
    await admin_reports.trade_book_pdf(user_id=uid, admin=w.sa, _=None)
    await admin_reports.tradebook_xlsx(user_id=uid, admin=w.sa, _=None)
    await admin_reports.trade_book_xlsx(user_id=uid, admin=w.sa, _=None)
    await user_reports.pnl_report_pdf(user=w.c)
    await user_reports.tradebook_pdf(user=w.c, limit=None)
    await user_reports.brokerage_pdf(user=w.c)
    await user_reports.tax_pdf(user=w.c)
    await user_reports.margin_pdf(user=w.c)
    await user_reports.tradebook_full_pdf(user=w.c)
    for fn in (accounts.export_entity_users_excel, accounts.export_entity_users_pdf):
        await fn(eid, w.sa, user_id=None, **_NO_DATES)
    for fn in (accounts.export_broker_totals_excel, accounts.export_broker_totals_pdf):
        await fn(eid, w.sa, **_NO_DATES)

    loop = threading.get_ident()
    assert {n for n, _ in renders} == {n for names in _BUILDERS.values() for n in names}
    assert [n for n, t in renders if t == loop] == []


def test_no_endpoint_calls_a_document_builder_inline():
    """Any router, pnl-sharing included: a builder is handed to to_thread,
    never called on the loop."""
    builder = re.compile(r"^(build_\w+_pdf|render_\w+)$")
    inline = []
    for path in sorted(Path(app.__file__).parent.joinpath("api").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                if builder.match(name):
                    inline.append(f"{path.name}:{node.lineno} {name}")
    assert inline == []
