"""§5 The admin's ledger and the client's read one type set.

Both list the cash rows of models.transaction.CASH_TYPES (the admin adds the
SETTLEMENT_TYPES the operator hides from clients) and both bucket a row the
same way: the EOD carry fee, a system ADJUSTMENT, is a trading charge on both
sides, never "Withdrawal by admin". The client's wallet feed shows an admin
delete's REVERSAL as the client's ledger words it, a trade adjustment, with
no admin wording or code.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from app.api.v1.admin import ledger as admin_ledger
from app.api.v1.user import ledger as user_ledger
from app.api.v1.user import wallet as user_wallet
from app.models.transaction import CASH_TYPES, SETTLEMENT_TYPES
from tests.test_ledger_summary import ROUND, scenario, w  # noqa: F401  (w: the fixture)

_REPO = Path(__file__).resolve().parents[2]


def _ts_types(path: str, const: str) -> set[str]:
    """The quoted type names in a frontend `const NAME = …[ … ]` literal."""
    block = (_REPO / path).read_text().split(f"const {const}", 1)[1].split("]", 1)[0]
    return set(re.findall(r'"([A-Z_]+)"', block))


def _join(types) -> str:
    return ",".join(sorted(t.value for t in types))


def test_both_screens_ask_for_the_model_type_sets():
    client = _ts_types("frontend-user/app/(dashboard)/ledger/page.tsx", "VISIBLE_TYPES")
    admin = _ts_types("frontend-admin/components/admin/LedgerSheet.tsx", "LEDGER_TYPES")
    assert client == {t.value for t in CASH_TYPES}
    assert admin == {t.value for t in CASH_TYPES | SETTLEMENT_TYPES}


async def test_the_admin_and_client_ledgers_hold_the_same_cash(w):  # noqa: F811
    await scenario(w)
    rows = (
        await user_ledger.ledger(w.u, from_date=None, to_date=None, limit=1000, types=_join(CASH_TYPES))
    ).data["rows"]
    page = (
        await admin_ledger.list_all(
            w.sa,
            user_id=str(w.u.id),
            transaction_type=_join(CASH_TYPES | SETTLEMENT_TYPES),
            page=1,
            page_size=1000,
        )
    ).data
    assert {r["id"] for r in rows} == {i["id"] for i in page["items"] if i["bucket"] != "settlement"}

    [fee] = [r for r in rows if r["particulars"].startswith("Carry-forward charge")]
    assert fee["label"] == "Adjustment"  # was "Withdrawal by admin"
    dep = sum(Decimal(str(r["credit"])) for r in rows if r["label"] in ("Deposit", "Deposit by admin"))
    wd = sum(Decimal(str(r["debit"])) for r in rows if r["label"] in ("Withdrawal", "Withdrawal by admin"))
    assert (dep, wd) == (Decimal(page["summary"]["deposits"]), Decimal(page["summary"]["withdrawals"]))


async def test_the_wallet_feed_shows_a_reversal_as_the_ledger_words_it(w):  # noqa: F811
    await scenario(w)
    feed = (await user_wallet.transactions(w.u, limit=200, skip=0)).data
    [rev] = [t for t in feed if t["transaction_type"] == "REVERSAL"]
    assert rev["bucket"] == "trading"
    assert rev["narration"] == f"Trade adjustment — {ROUND.symbol}"  # no "Delete", no admin code
    [fee] = [t for t in feed if t["transaction_type"] == "ADJUSTMENT" and t["bucket"] == "trading"]
    assert fee["narration"].startswith("Carry-forward charge")
