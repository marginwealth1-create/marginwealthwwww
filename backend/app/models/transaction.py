"""Wallet transactions, deposit & withdrawal requests, and W/D rules.

`WalletTransaction` is the **immutable ledger** — every credit/debit appends
a new doc; never edit existing ones. balance_before / balance_after make
reconciliation trivial.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from beanie import Indexed, PydanticObjectId
from bson import Decimal128
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import StrEnum, TimestampMixin
from app.models._types import Money


def _zero() -> Decimal128:
    return Decimal128("0")


# ── 16. wallet_transactions ──────────────────────────────────────────
class TransactionType(StrEnum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    TRADE = "TRADE"
    BROKERAGE = "BROKERAGE"
    CHARGES = "CHARGES"
    PNL = "PNL"
    ADJUSTMENT = "ADJUSTMENT"
    BONUS = "BONUS"
    BONUS_CONVERTED = "BONUS_CONVERTED"  # bonus credit → withdrawable cash (wager met)
    PENALTY = "PENALTY"
    PROMO = "PROMO"
    INTER_USER = "INTER_USER"
    REVERSAL = "REVERSAL"
    PNL_SHARING_PAYOUT = "PNL_SHARING_PAYOUT"
    PNL_SHARING_RECEIPT = "PNL_SHARING_RECEIPT"
    SETTLEMENT_OUTSTANDING_BOOKED = "SETTLEMENT_OUTSTANDING_BOOKED"
    SETTLEMENT_OUTSTANDING_RECOVERY = "SETTLEMENT_OUTSTANDING_RECOVERY"


class TransactionStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REVERSED = "REVERSED"


class WalletTransaction(TimestampMixin):
    user_id: PydanticObjectId
    transaction_type: TransactionType
    amount: Money  # signed: + credit, - debit
    balance_before: Money = Field(default_factory=_zero)
    balance_after: Money = Field(default_factory=_zero)

    reference_type: str | None = None  # "ORDER" / "DEPOSIT" / "WITHDRAWAL" / "MANUAL"
    reference_id: str | None = None
    narration: str
    status: TransactionStatus = TransactionStatus.COMPLETED

    created_by: PydanticObjectId | None = None  # admin id for manual entries
    reversal_of: PydanticObjectId | None = None  # link back when reversed

    # Bonus Management (gated by settings.BONUSES_ENABLED). Set on
    # BONUS_CONVERTED rows to link the cash credit back to the originating
    # bonus grant. Null on every non-bonus transaction.
    bonus_id: PydanticObjectId | None = None
    bonus_amount: Money | None = None

    class Settings:
        name = "wallet_transactions"
        indexes = [
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("transaction_type", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("reference_type", ASCENDING), ("reference_id", ASCENDING)]),
            IndexModel([("status", ASCENDING)]),
            IndexModel([("created_at", DESCENDING)]),
        ]


# ── 17. deposit_requests ─────────────────────────────────────────────
class DepositStatus(StrEnum):
    INITIATED = "INITIATED"  # gateway invoice created, user hasn't paid yet — hidden from admin
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"  # gateway expired/failed or user abandoned — shown to user, hidden from admin


class PaymentMode(StrEnum):
    BANK_TRANSFER = "BANK_TRANSFER"
    UPI = "UPI"
    CRYPTO = "CRYPTO"
    NEFT = "NEFT"
    RTGS = "RTGS"
    IMPS = "IMPS"


class DepositRequest(TimestampMixin):
    user_id: PydanticObjectId
    amount: Money
    payment_mode: PaymentMode = PaymentMode.UPI
    utr_number: str | None = None
    screenshot_url: str | None = None
    # ── Crypto deposit fields (payment_mode == CRYPTO) ────────────────
    crypto_asset: str | None = None       # USDT / BTC …
    crypto_network: str | None = None     # TRC20 / ERC20 …
    crypto_address: str | None = None     # the address shown to the user
    crypto_tx_hash: str | None = None     # user-submitted on-chain tx id
    gateway: str | None = None            # "oxapay" for gateway-routed deposits
    gateway_ref: str | None = None        # oxapay invoice/track id
    gateway_status: str | None = None     # raw provider status (paid / expired …)
    bank_account_id: PydanticObjectId | None = None  # company bank used
    # Bonus Management — stamped by the approval path when an auto-grant fires.
    bonus_id: PydanticObjectId | None = None
    bonus_amount: Money | None = None

    user_remark: str | None = None
    admin_remark: str | None = None

    status: DepositStatus = DepositStatus.PENDING
    processed_by: PydanticObjectId | None = None
    processed_at: datetime | None = None

    idempotency_key: Indexed(str, unique=True, sparse=True) | None = None  # type: ignore[valid-type]

    class Settings:
        name = "deposit_requests"
        indexes = [
            IndexModel([("status", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("utr_number", ASCENDING)], sparse=True),
            IndexModel([("idempotency_key", ASCENDING)], unique=True, sparse=True),
        ]


# ── 18. withdrawal_requests ──────────────────────────────────────────
class WithdrawalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class BankSnapshot(BaseModel):
    """Where the user wants their withdrawal sent.

    Two channels are supported: bank transfer (name/account/ifsc/holder)
    OR UPI (upi_id, with optional qr_url for admin-side scan). Fields are
    optional individually; the request handler enforces "at least one
    channel populated" so existing bank-only rows stay valid.
    """

    name: str | None = None
    account_number: str | None = None
    ifsc: str | None = None
    holder: str | None = None
    branch: str | None = None
    account_type: str | None = None  # SAVINGS / CURRENT
    upi_id: str | None = None        # VPA, e.g. user@bank
    qr_url: str | None = None        # uploaded QR image (optional)


class WithdrawalRequest(TimestampMixin):
    user_id: PydanticObjectId
    amount: Money
    bank: BankSnapshot
    remarks: str | None = None
    utr_number: str | None = None  # filled by admin after disbursal
    charges: Money = Field(default_factory=_zero)
    net_amount: Money = Field(default_factory=_zero)

    status: WithdrawalStatus = WithdrawalStatus.PENDING
    processed_by: PydanticObjectId | None = None
    processed_at: datetime | None = None
    rejection_reason: str | None = None

    idempotency_key: Indexed(str, unique=True, sparse=True) | None = None  # type: ignore[valid-type]

    class Settings:
        name = "withdrawal_requests"
        indexes = [
            IndexModel([("status", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("idempotency_key", ASCENDING)], unique=True, sparse=True),
        ]


# ── 21. settlement_requests ──────────────────────────────────────────
# A "settlement request" is queued by `wallet_service.adjust()` when a
# user whose `User.auto_settlement == False` incurs a debit that pushes
# `available_balance` below 0. Instead of the default auto-flow that
# clips the balance to 0 and books the overflow into
# `settlement_outstanding`, the wallet is left NEGATIVE and the admin
# is asked to approve from the Payments → Settlement Requests tab.
#
# Per-user invariant: at most ONE pending row at a time. Successive
# debits that grow the shortfall update the same row's `requested_amount`
# (= |available_balance|) so the admin always sees the latest figure.
# Enforced with a unique partial index on (user_id, status=PENDING).
class SettlementStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class SettlementRequest(TimestampMixin):
    user_id: PydanticObjectId

    # |available_balance| at the moment of the latest debit that grew
    # the shortfall. Updated in place while the row stays PENDING.
    # Frozen at the value the admin saw the instant they hit Approve.
    requested_amount: Money = Field(default_factory=_zero)

    # Snapshot of the wallet at request time — useful for audit when
    # the operator wants to know how the user landed in this state.
    available_at_request: Money = Field(default_factory=_zero)
    settlement_outstanding_at_request: Money = Field(default_factory=_zero)

    # What triggered the most recent shortfall growth. Mirrors the
    # WalletTransaction.reference_* fields so the admin row can link
    # back to the closing order / trade that pushed the user into red.
    reference_type: str | None = None  # "ORDER" / "PNL" / "CHARGES"
    reference_id: str | None = None
    narration: str = ""

    status: SettlementStatus = SettlementStatus.PENDING
    approved_by: PydanticObjectId | None = None
    approved_at: datetime | None = None
    rejected_reason: str | None = None

    class Settings:
        name = "settlement_requests"
        indexes = [
            IndexModel([("status", ASCENDING), ("created_at", DESCENDING)]),
            IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)]),
            # Structural one-pending-per-user guarantee: the partial
            # filter limits the unique constraint to PENDING rows
            # only, so an APPROVED + a new PENDING on the same user
            # are both legal — but two PENDING ones are not.
            IndexModel(
                [("user_id", ASCENDING)],
                unique=True,
                partialFilterExpression={"status": "PENDING"},
                name="settlement_one_pending_per_user",
            ),
        ]


# ── 22. wd_rules ─────────────────────────────────────────────────────
class WdRuleType(StrEnum):
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"


class AllowedTimeWindow(BaseModel):
    start: str = "09:00"  # HH:MM IST
    end: str = "21:00"


class WdRule(TimestampMixin):
    """Platform-global deposit / withdrawal rule. One row per `rule_type`.

    Per-tier overrides (`SuperAdminWdRule` / `SubAdminWdRule` / `BrokerWdRule`)
    layer on top of this — same cascade pattern as the netting/segment
    settings. The resolver in `services/wd_rules_service.py` merges all
    relevant tiers so a user's effective rule reflects their owner's
    pool's overrides.
    """

    rule_type: Indexed(str, unique=True)  # type: ignore[valid-type] # one row each
    min_amount: Money = Field(default_factory=_zero)
    max_amount: Money = Field(default_factory=lambda: Decimal128("10000000"))
    daily_limit: Money = Field(default_factory=lambda: Decimal128("1000000"))

    # Weekday gate — 0=Monday … 6=Sunday. Empty list also accepted = "no
    # day restriction" (treat as all 7 days allowed). Most brokers restrict
    # WITHDRAWAL to working days only (0..4); DEPOSIT typically stays 0..6.
    allowed_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    allowed_times: list[AllowedTimeWindow] = Field(default_factory=lambda: [AllowedTimeWindow()])
    charges_flat: Money = Field(default_factory=_zero)
    charges_percent: float = 0.0
    auto_approve_under: Money = Field(default_factory=_zero)
    mandatory_remark: bool = False
    # WITHDRAWAL-only gate: when True, a user holding ANY open position
    # cannot raise a withdrawal request. Default False (no restriction).
    # Ignored on DEPOSIT rows. Enforced in the user withdrawal path.
    block_withdrawal_with_open_positions: bool = False
    # Applies to BOTH rule types: when True, a user with an existing PENDING
    # request of this type cannot raise another one until it's processed.
    # Default False (no restriction). Enforced in the user deposit/withdrawal
    # create paths.
    block_duplicate_pending: bool = False
    # Applies to BOTH rule types: max number of requests a single user may
    # submit per IST day. 0 = no cap (default; preserves prior behaviour).
    # Counts ALL requests created today (any status). Enforced in the user
    # deposit/withdrawal create paths.
    max_requests_per_day: int = 0
    # WITHDRAWAL-only: when True, the user MUST supply full bank details
    # (account holder / number / IFSC) on a withdrawal request — UPI stays
    # optional. Default False (bank optional; UPI or bank either works).
    # Enforced in the user withdrawal create path + the withdrawal UI.
    require_bank_details: bool = False
    # WITHDRAWAL-only: when False, the admin has DISABLED UPI as a payout
    # channel — users must withdraw to a bank account and the UPI option is
    # hidden in the UI + rejected server-side. Default True (UPI allowed).
    allow_upi_payout: bool = True

    class Settings:
        name = "wd_rules"
        indexes = [IndexModel([("rule_type", ASCENDING)], unique=True)]


# ── Per-tier override layers ─────────────────────────────────────────
#
# Each tier override is a SPARSE document: every editable field is
# Optional. A None means "inherit from the tier below". This mirrors the
# `NettingSegment` ↔ `Sub/Super/BrokerSegmentOverride` shape so the same
# admin mental model carries over to deposit / withdrawal rules.
#
# Resolution order (most specific first):
#     BrokerWdRule (broker pool)
#  →  SubAdminWdRule (admin pool)
#  →  SuperAdminWdRule (super-admin pool)
#  →  WdRule (platform global)
#
# A user's `assigned_admin_id` + `broker_ancestry` decide which tier
# pools are visible to the resolver — that's the same logic the netting
# resolver already runs, so we re-use the user-doc fields here.


class _WdRuleOverrideBase(TimestampMixin):
    """Common shape — every override row has these fields nullable so
    admins can set only the fields they want to override."""

    rule_type: str  # "DEPOSIT" or "WITHDRAWAL"
    min_amount: Money | None = None
    max_amount: Money | None = None
    daily_limit: Money | None = None
    allowed_days: list[int] | None = None
    allowed_times: list[AllowedTimeWindow] | None = None
    charges_flat: Money | None = None
    charges_percent: float | None = None
    auto_approve_under: Money | None = None
    mandatory_remark: bool | None = None
    block_withdrawal_with_open_positions: bool | None = None
    block_duplicate_pending: bool | None = None
    max_requests_per_day: int | None = None
    require_bank_details: bool | None = None
    allow_upi_payout: bool | None = None


class SuperAdminWdRule(_WdRuleOverrideBase):
    super_admin_id: PydanticObjectId

    class Settings:
        name = "super_admin_wd_rules"
        indexes = [
            IndexModel(
                [("super_admin_id", ASCENDING), ("rule_type", ASCENDING)],
                unique=True,
                name="super_admin_wd_rule_unique",
            ),
        ]


class SubAdminWdRule(_WdRuleOverrideBase):
    sub_admin_id: PydanticObjectId

    class Settings:
        name = "sub_admin_wd_rules"
        indexes = [
            IndexModel(
                [("sub_admin_id", ASCENDING), ("rule_type", ASCENDING)],
                unique=True,
                name="sub_admin_wd_rule_unique",
            ),
        ]


class BrokerWdRule(_WdRuleOverrideBase):
    broker_id: PydanticObjectId

    class Settings:
        name = "broker_wd_rules"
        indexes = [
            IndexModel(
                [("broker_id", ASCENDING), ("rule_type", ASCENDING)],
                unique=True,
                name="broker_wd_rule_unique",
            ),
        ]
