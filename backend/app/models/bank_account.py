"""Company bank accounts (where users deposit) + per-user bank accounts (where withdrawals go)."""

from __future__ import annotations

from beanie import PydanticObjectId
from pydantic import Field
from pymongo import ASCENDING, IndexModel

from app.models._base import TimestampMixin


# ── 19. company_bank_accounts ─────────────────────────────────────────
class CompanyBankAccount(TimestampMixin):
    bank_name: str
    account_holder: str
    account_number: str
    ifsc_code: str
    branch: str | None = None
    account_type: str = "CURRENT"

    upi_id: str | None = None
    qr_code_url: str | None = None

    daily_limit_inr: float = 0.0  # 0 = no limit
    today_received: float = 0.0  # reset by EOD

    is_active: bool = True
    is_default: bool = False
    sort_order: int = 0

    # Which admin's pool this bank belongs to.
    # NULL ⇒ super-admin (platform-default pool — shown to every user whose
    # assigned_admin_id is also NULL). A non-null value ⇒ a sub-admin owns
    # this bank, and only users whose assigned_admin_id matches will see it
    # on their deposit form.
    owner_admin_id: PydanticObjectId | None = None

    # Broker-tier ownership. When non-null this row is shown ONLY to
    # clients whose assigned_broker_id matches. A user-side lookup picks
    # the most-specific owner present: broker > admin > platform default.
    owner_broker_id: PydanticObjectId | None = None

    class Settings:
        name = "company_bank_accounts"
        indexes = [
            IndexModel([("is_active", ASCENDING), ("is_default", ASCENDING)]),
            # account_number is scoped by owner (admin / broker pool) so the
            # SAME account number can legitimately exist in two different
            # admins' pools (each broker / admin manages their own list).
            # Earlier we had a GLOBAL unique on account_number which caused
            # admin B's POST to crash with DuplicateKeyError when admin A
            # had already used the same number — symptom user flagged: "ek
            # admin me add ho gaya, doosre me nahi". The duplicate error
            # surfaced as a 500 without CORS headers, so the browser
            # showed it as a CORS error in the console (red herring).
            # Compound key includes both owner fields so a single account
            # number can exist once per (admin_pool, broker_pool) tuple —
            # which is what the cascade resolver already expects.
            IndexModel(
                [
                    ("account_number", ASCENDING),
                    ("owner_admin_id", ASCENDING),
                    ("owner_broker_id", ASCENDING),
                ],
                unique=True,
            ),
            IndexModel([("owner_admin_id", ASCENDING), ("is_active", ASCENDING)]),
            IndexModel([("owner_broker_id", ASCENDING), ("is_active", ASCENDING)]),
        ]


# ── 20. user_bank_accounts (saved payout methods: BANK + UPI) ─────────
class UserBankAccount(TimestampMixin):
    """A user's saved withdrawal destination — a bank account OR a UPI id.

    `method_type` splits the two channels. `is_default` is the PRIMARY flag
    scoped WITHIN a channel (one primary bank + one primary UPI). At
    withdrawal the user just picks a saved method (default = the primaries)
    instead of re-typing bank/UPI every time.

    Bank fields default to "" so a UPI-only row validates. For UPI rows we
    store the VPA in BOTH `upi_id` and `account_number` — that keeps the
    existing unique (user_id, account_number) index meaningful (a user can't
    save the same VPA twice) without an index migration.
    """

    user_id: PydanticObjectId
    method_type: str = "BANK"  # BANK | UPI
    bank_name: str = ""
    account_holder: str = ""
    account_number: str = ""  # bank: a/c no; upi: the VPA (unique-index anchor)
    ifsc_code: str = ""
    branch: str | None = None
    account_type: str = "SAVINGS"
    upi_id: str | None = None  # set for UPI rows

    is_default: bool = False  # primary within its method_type
    is_verified: bool = False
    verification_method: str | None = None  # PENNY_DROP / MANUAL
    nickname: str | None = None

    class Settings:
        name = "user_bank_accounts"
        indexes = [
            IndexModel(
                [("user_id", ASCENDING), ("account_number", ASCENDING)], unique=True
            ),
            IndexModel([("user_id", ASCENDING), ("is_default", ASCENDING)]),
        ]
