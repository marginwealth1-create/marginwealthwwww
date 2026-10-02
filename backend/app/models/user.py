"""User & UserSegment documents.

A single User collection holds clients, dealers, masters, admins, super-admin
— role-based filtering keeps query plans simple. Hierarchical relationships
(master → dealer → client) are modelled via `parent_id`.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from beanie import Indexed, Link, PydanticObjectId
from bson import Decimal128
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models._base import PermissionLevel, StrEnum, TimestampMixin
from app.utils.time_utils import now_utc


class UserRole(StrEnum):
    SUPER_ADMIN = "SUPER_ADMIN"
    ADMIN = "ADMIN"
    MASTER = "MASTER"
    DEALER = "DEALER"
    CLIENT = "CLIENT"
    # New tier: a broker sits under an admin and manages their own client
    # pool. Brokers can also create sub-brokers (nested, via broker_ancestry).
    BROKER = "BROKER"
    # Staff sub-user of an admin. Logs in through the separate /employee-login
    # portal, reuses `admin_permissions` for its granted sections, and operates
    # on its PARENT admin's pool (assigned_admin_id → the creating admin).
    EMPLOYEE = "EMPLOYEE"


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    PENDING = "PENDING"
    CLOSED = "CLOSED"


class AccountType(StrEnum):
    LIVE = "LIVE"
    DEMO = "DEMO"


# ── Embedded sub-documents ──────────────────────────────────────────
class KycInfo(BaseModel):
    pan: str | None = None
    aadhaar: str | None = None  # store hashed/last-4 in production
    dob: date | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    country: str = "India"
    is_verified: bool = False
    verified_at: datetime | None = None


class UserPermissions(BaseModel):
    can_place_orders: bool = True
    can_modify_orders: bool = True
    can_cancel_orders: bool = True
    can_withdraw: bool = True
    can_deposit: bool = True
    can_view_charts: bool = True
    api_access: bool = False
    algo_trading: bool = False


class TradingHours(BaseModel):
    login_start: str = "00:00"  # HH:MM, IST
    login_end: str = "23:59"
    ip_whitelist: list[str] = Field(default_factory=list)


class RiskProfile(BaseModel):
    max_daily_loss: float = 0.0  # 0 = no limit
    max_position_value: float = 0.0
    max_open_positions: int = 0
    auto_squareoff_enabled: bool = True
    m2m_squareoff_percent: float = 80.0  # squareoff at -80% of margin


class CommunicationPrefs(BaseModel):
    email_alerts: bool = True
    sms_alerts: bool = True
    whatsapp_alerts: bool = False
    push_alerts: bool = True


# Section toggles for sub-admins (role == ADMIN). One boolean per admin nav
# section; SUPER_ADMIN ignores this object entirely. Adding a new section
# means: append a field here, gate it in admin endpoints with
# require_admin_permission(<name>), and surface a toggle in the
# `frontend-admin/management` page.
class AdminPermissions(BaseModel):
    users: bool = False
    kyc: bool = False
    deposits: bool = False
    withdrawals: bool = False
    segment_settings: bool = False
    risk: bool = False
    netting: bool = False
    trading_view: bool = False
    ledger: bool = False
    reports: bool = False
    brokerage: bool = False
    # Gates access to /management/brokers — admin needs this ON to create
    # brokers under their pool. Super-admin always has it.
    brokers: bool = False
    # Gates the Bank Accounts tab on the Payments page (list/create/edit/
    # delete of CompanyBankAccount rows in the admin's own pool). Default
    # True so existing admins keep their bank-management capability —
    # super-admin can turn it OFF per sub-admin to lock down.
    banks: bool = True
    # Sections that used to be un-gated (visible to every admin-tier user).
    # Added so they can be granted to EMPLOYEES per-section. Existing admins are
    # backfilled to True (scripts.grant_all_sections_to_existing_admins) so they
    # keep the access they already had; employees get them only when granted.
    accounts: bool = False        # Accounts + Accounts Dashboard
    pnl_sharing: bool = False     # P&L Sharing agreements
    audit: bool = False           # Audit logs + Admin Actions history
    support: bool = False         # Support
    # Granular sub-sections of the grouped perms above, so an admin can grant an
    # employee (or sub-admin) exactly one nav page. Backend gates stay on the
    # umbrella perm (trading_view / ledger) but ACCEPT any child too — see
    # dependencies._UMBRELLA_CHILDREN — so granting a child grants API access.
    orders: bool = False          # Trading → Orders          (child of trading_view)
    positions: bool = False       # Trading → Positions        (child of trading_view)
    marketwatch: bool = False     # Trading → Market Watch     (child of trading_view)
    money_transactions: bool = False  # Money → Money Transactions (child of ledger)
    broker_deposits: bool = False     # Money → Broker Deposits    (child of ledger)
    download_app: bool = False    # System → Download App
    bonuses: bool = False         # Bonus Management (gated by BONUSES_ENABLED)
    # Lets a NON-super admin transfer their own users to ANOTHER admin on the
    # platform (the super-admin's cross-admin reassign capability, delegated).
    # Default False — only the super-admin has it until granted. Gates the
    # "Transfer User" sidebar section + the /management/transfer/* endpoints.
    transfer_users: bool = False


# Tri-state permissions granted by an admin to a broker (or by a broker to
# a sub-broker). Each key mirrors a section in the admin nav; the level
# decides what the broker sees and can do on that page:
#   OFF  → section hidden from sidebar; backend rejects all calls with 403
#   VIEW → page loads, list/details readable; mutation buttons disabled,
#          backend rejects writes with 403
#   EDIT → full access (read + write)
# The `sub_brokers` key here is the broker-level equivalent of admin's
# `brokers` flag — gates the broker's ability to mint sub-brokers.
class BrokerPermissions(BaseModel):
    users: PermissionLevel = PermissionLevel.OFF
    kyc: PermissionLevel = PermissionLevel.OFF
    deposits: PermissionLevel = PermissionLevel.OFF
    withdrawals: PermissionLevel = PermissionLevel.OFF
    segment_settings: PermissionLevel = PermissionLevel.OFF
    risk: PermissionLevel = PermissionLevel.OFF
    netting: PermissionLevel = PermissionLevel.OFF
    trading_view: PermissionLevel = PermissionLevel.OFF
    ledger: PermissionLevel = PermissionLevel.OFF
    reports: PermissionLevel = PermissionLevel.OFF
    brokerage: PermissionLevel = PermissionLevel.OFF
    sub_brokers: PermissionLevel = PermissionLevel.OFF
    # Bank Accounts tab — VIEW lets broker see existing banks in their pool,
    # EDIT lets them add / update / delete banks for their own users.
    banks: PermissionLevel = PermissionLevel.OFF
    bonuses: PermissionLevel = PermissionLevel.OFF  # Bonus Management
    # Change-password gate. A broker can reset THEIR users' passwords only with
    # EDIT here — deliberately separate from `users` so "let the broker create
    # users" (users=EDIT) doesn't also hand them password resets. Default OFF.
    user_password: PermissionLevel = PermissionLevel.OFF
    # Support-number gate. EDIT lets the broker set their OWN support WhatsApp
    # number (shown to their clients). When OFF, the broker's number is IGNORED
    # by the user-side resolver — their clients fall back to the parent admin's
    # number automatically. Default OFF: a brand-new broker must be granted this
    # before their number takes effect. (Existing brokers are backfilled to EDIT
    # by scripts/backfill_broker_support_perm.py so they keep working.)
    support: PermissionLevel = PermissionLevel.OFF


# ── User document ───────────────────────────────────────────────────
class User(TimestampMixin):
    user_code: Indexed(str, unique=True)  # type: ignore[valid-type]
    # Stored as plain `str` (NOT EmailStr) on purpose: the soft-delete flow
    # rewrites a closed user's email to "<orig>+deleted-<id>" to free the
    # unique index (see /admin/users DELETE). That suffix is not a valid
    # RFC email, so an EmailStr field would raise ValidationError when
    # beanie re-parses the row — crashing any `.to_list()` whose scope
    # includes a closed user (admin Users list, sub-admin drill-in, money
    # / accounts aggregations). Email FORMAT is still validated at the API
    # input layer (register / create-user / create-sub-admin / create-broker
    # request schemas all use EmailStr), so new accounts stay well-formed.
    email: Indexed(str, unique=True)  # type: ignore[valid-type]
    mobile: Indexed(str, unique=True)  # type: ignore[valid-type]
    password_hash: str
    full_name: str
    photo_url: str | None = None

    # Tombstones stamped by /admin/users/{id} DELETE when the row is
    # soft-closed.  email/mobile are rewritten to "<orig>+deleted-<id>"
    # and "DEL<oid-tail>" respectively so the unique index frees up for
    # a future registration with the same contact; the originals are
    # kept here for the audit trail / KYC lookup.
    deleted_email_original: str | None = None
    deleted_mobile_original: str | None = None

    # ── Terms & Conditions (admin-tier writes; cascades to clients) ──
    # Each admin-tier user (SUPER_ADMIN / ADMIN / BROKER) can set their
    # own T&C text + toggle. When `terms_enabled=True`, every CLIENT
    # in their downline sees the T&C modal once after register and
    # again whenever the text changes (acceptance is tracked via
    # `terms_accepted_at` on the client row).
    terms_text: str | None = None
    terms_enabled: bool = False
    # CLIENT-side: timestamp of the last accept click. Reset to None by
    # admin if they update terms_text and want re-acceptance.
    terms_accepted_at: datetime | None = None

    role: UserRole = UserRole.CLIENT
    status: UserStatus = UserStatus.PENDING
    account_type: AccountType = AccountType.LIVE
    is_demo: bool = False

    # Session epoch. Stamped into every access token as the `ver` claim;
    # the per-request auth dependency rejects any token whose `ver` doesn't
    # match. Bumping this (on block / admin password reset) instantly
    # invalidates EVERY outstanding access token for the user — they can't
    # ride out the 15-min access-token window or refresh back in, so the
    # account is force-logged-out on its very next request.
    token_version: int = 0

    parent_id: PydanticObjectId | None = None  # hierarchy

    kyc: KycInfo = Field(default_factory=KycInfo)
    permissions: UserPermissions = Field(default_factory=UserPermissions)
    trading_hours: TradingHours = Field(default_factory=TradingHours)
    risk: RiskProfile = Field(default_factory=RiskProfile)
    communication: CommunicationPrefs = Field(default_factory=CommunicationPrefs)

    # Brokerage plan (FK to brokerage_plans, optional → uses default)
    brokerage_plan_id: PydanticObjectId | None = None

    # 2FA
    two_fa_enabled: bool = False
    two_fa_secret: str | None = None
    two_fa_backup_codes: list[str] = Field(default_factory=list)

    # Login telemetry
    last_login_at: datetime | None = None
    last_login_ip: str | None = None
    # First approved deposit — stamped once by the deposit-approval path when
    # BONUSES_ENABLED, so the bonus engine can tell FIRST_DEPOSIT from RELOAD.
    first_deposit_at: datetime | None = None
    failed_login_count: int = 0
    locked_until: datetime | None = None
    password_changed_at: datetime | None = None
    must_change_password: bool = False

    created_by: PydanticObjectId | None = None

    # Pool transfer telemetry — stamped every time a super-admin /
    # admin / broker moves this user into a new pool via the admin
    # `Transfer User` action. Lets the destination dashboard render a
    # "Transferred" badge so the new owner can spot users that landed
    # in their pool through reassignment vs. ones they personally
    # created. NULL on freshly-created users (the originating admin
    # is `created_by`).
    last_transferred_at: datetime | None = None
    last_transferred_by: PydanticObjectId | None = None

    # Sub-admin ownership (CLIENT/DEALER/MASTER → which ADMIN owns them).
    # NULL ⇒ owned by super-admin (the platform itself).
    assigned_admin_id: PydanticObjectId | None = None

    # Sub-admin profile — only populated for role == ADMIN.
    admin_permissions: AdminPermissions | None = None
    pnl_share_pct: Decimal128 | None = None  # 0..100

    # Immediate broker owner. For BROKER role: their parent broker (NULL for
    # a top-level broker created by an admin/super-admin). For CLIENT role:
    # the broker that minted them (NULL when client belongs to admin pool).
    assigned_broker_id: PydanticObjectId | None = None

    # Materialised broker ancestry, root-first, NOT including self. Lets us
    # scope an entire subtree in O(1) via a single multikey index lookup:
    #     User.find({"broker_ancestry": broker.id})
    # matches every descendant (sub-brokers + their clients) since the array
    # contains the broker.id at any depth. Top broker under an admin: [].
    # Sub-broker: [top_broker.id]. Sub-sub-broker: [top_broker.id, parent.id].
    broker_ancestry: list[PydanticObjectId] = Field(default_factory=list)

    # Broker profile — only meaningful when role == BROKER.
    broker_permissions: BrokerPermissions | None = None
    broker_pnl_share_pct: Decimal128 | None = None  # 0..100
    # Separate brokerage-sharing %, independent of the PnL share. None on
    # brokers created before the split — those inherit broker_pnl_share_pct
    # everywhere, so their settlement math stays byte-identical to before.
    broker_brokerage_share_pct: Decimal128 | None = None  # 0..100

    # Per-user "auto settle" toggle (default ON). When True (default),
    # `wallet_service.adjust()` floors any debit that would push
    # available_balance below 0 and books the overflow into
    # settlement_outstanding automatically — that's the existing
    # 21-May floor-at-0 behaviour every legacy user runs.
    #
    # When False (admin opt-out via the user-detail toggle), the
    # wallet is allowed to go NEGATIVE. The same path queues a
    # pending `SettlementRequest` instead so the admin can manually
    # approve from the Payments → Settlement Requests tab. While a
    # PENDING request exists the order validator refuses new-opening
    # orders (closing trades still pass through via the existing
    # `is_reducing` exemption).
    auto_settlement: bool = True

    # Per-user admin trade alert (default OFF). When True, every fill on
    # this user's positions also carries an `alert` block on the
    # `admin:events` publish, so the owning admins get a side toast + a
    # chosen sound the moment the user opens or closes a trade. Watching a
    # handful of users is the point — turning it on for everybody would
    # make the panel unusable. `trade_alert_sound` is one of the ids in
    # frontend-admin/lib/notify-sound.ts.
    trade_alert: bool = False
    trade_alert_sound: str = "chime"

    # Tokens already seeded into this user's marketwatch by
    # `default_watchlist.ensure_defaults`. Remembering them is what lets a
    # user delete a default instrument and have it STAY deleted, while
    # next month's contract — a token nobody has seen — still arrives.
    default_watchlist_seeded: list[str] = []

    # Per-admin support WhatsApp number, shown to that admin's downstream
    # users on the "Add funds → Support" button and any other Contact-
    # support affordance in the apk/user web. Cascade resolution: when a
    # user requests their support number, we walk up the parent_id chain
    # (CLIENT → DEALER/MASTER/BROKER → ADMIN → SUPER_ADMIN) and return
    # the first non-empty value. Falls back to the global
    # `platform.support_whatsapp` PlatformSetting if nothing is set
    # anywhere in the chain. Only meaningful for admin-tier roles
    # (SUPER_ADMIN / ADMIN / BROKER); CLIENT rows leave this NULL.
    # Stored as a free-form string so country code + spacing + the
    # leading `+` survive round-trips — the apk's `buildWhatsappUrl`
    # strips non-digits before composing the wa.me link.
    support_whatsapp: str | None = None

    # Per-admin "user carry-forward toggle" feature switch. Default OFF ⇒ the
    # platform's normal auto-carry runs at EOD (affordable MIS → NRML) and users
    # see no per-position toggle. When an admin turns this ON for their pool,
    # each of their users' open positions gets a per-position Carry-Forward
    # toggle (default OFF): only positions the user flips ON carry overnight;
    # the rest are squared off at EOD. Cascades to the pool like support_whatsapp.
    carry_forward_toggle_enabled: bool = False

    # Per-admin home-page ticker — a scrolling marquee of announcement lines
    # shown ONLY on this admin's users' home screen. Cascades down the same
    # chain as `support_whatsapp` (closest ancestor with a non-empty list
    # wins). Empty list ⇒ no ticker. Admin adds/removes lines from the
    # Support page; runs continuously until cleared.
    ticker_messages: list[str] = Field(default_factory=list)

    # ── White-label branding (Phase 1: schema-only, gated by
    # `settings.BRANDING_ENABLED`). All optional, default `None`, so
    # existing 10k user rows behave exactly as today on read. Only
    # meaningful when role == ADMIN, except `signup_origin` which is
    # stamped on every newly-registered user post-rollout. None for any
    # legacy user is treated as "PLATFORM" by the resolution logic, so
    # zero backfill is needed.
    #
    # Why these fields can ship invisibly:
    #   * Pydantic/Beanie auto-fills missing keys with `None` on read.
    #   * The unique index on `custom_domain` below is *sparse* — rows
    #     with `None` are simply not indexed, so the existing 10k rows
    #     contribute zero index entries and zero write overhead.
    #   * No code path consumes these fields until BRANDING_ENABLED
    #     flips on (Phase 2+) and the public `/branding/*` endpoints
    #     ship.
    brand_name: str | None = None
    logo_url: str | None = None  # "/uploads/logos/logo-<admin_id>-<ts>.png"
    # Per-admin Telegram invite link (e.g. https://t.me/mychannel). Surfaced
    # via branding on THIS admin's login page (referral / custom-domain
    # resolved) as a Telegram button. null / empty → nothing shown.
    telegram_link: str | None = None

    # Per-admin (and super-admin) public-registration switch. When False, the
    # website /register flow for THIS owner's pool (their ?ref= link, custom
    # domain, or — for the super-admin — the platform pool) is turned OFF and
    # shows a "registration temporarily disabled" message. Default True so every
    # existing pool keeps accepting signups. Only the owner's own toggle matters;
    # other admins are unaffected.
    registration_enabled: bool = True

    # Per-admin Terms & Conditions text shown to THIS admin's users on their
    # profile. The admin types/edits it from Platform Settings; empty ⇒ the
    # profile falls back to the super-admin's platform default, else hides the
    # T&C link. Plain text (newline-separated); the app renders it read-only.
    terms_and_conditions: str | None = None

    # Divinepay UPI gateway switch (set on an ADMIN row by the super-admin).
    # When True, this admin's users get the auto-crediting online pay-in flow;
    # False → the manual bank-QR + screenshot + admin-approval deposit flow.
    # Default False — the whole platform ships with the gateway OFF; the
    # super-admin turns it on per admin (and per their own pool via a
    # PlatformSetting). See divinepay_service.gateway_on_for.
    payment_gateway_enabled: bool = False

    # Per-admin maintenance switch (set on an ADMIN row by the super-admin).
    # When True, EVERY non-admin user in this admin's pool is blocked from
    # logging in AND any already-logged-in session is kicked on its next
    # request (enforced in the auth dependency). Default False so pools run
    # normally. Only affects this admin's own pool — other admins unaffected.
    maintenance_mode: bool = False

    # Custom domain (sparse-unique — see Settings.indexes). Stored
    # lowercased, no scheme: "mybroker.com".
    custom_domain: str | None = None

    # Lifecycle state machine for `custom_domain` provisioning.
    #   PENDING_DNS  → admin saved domain, hasn't verified yet
    #   DNS_VERIFIED → backend confirmed A records point to platform IP
    #   PROVISIONING → certbot Celery task running
    #   READY        → cert installed, nginx reloaded — domain live
    #   FAILED       → cert issuance failed (last_error populated)
    custom_domain_status: str | None = None
    custom_domain_last_error: str | None = None
    custom_domain_verified_at: datetime | None = None

    # How this user originally signed up — drives the post-login
    # cross-origin redirect gate. `None` ≡ "PLATFORM" (the default for
    # every existing legacy user, hence no backfill).
    #   PLATFORM         : signed up at marginwealth.com/register (or pre-rollout)
    #   BRANDED_REFERRAL : signed up via /r/<admin_user_code>/signup or ?ref=
    #   CUSTOM_DOMAIN    : signed up directly on admin's custom_domain host
    signup_origin: str | None = None

    class Settings:
        name = "users"
        use_state_management = True
        indexes = [
            IndexModel([("email", ASCENDING)], unique=True),
            IndexModel([("mobile", ASCENDING)], unique=True),
            IndexModel([("user_code", ASCENDING)], unique=True),
            IndexModel([("parent_id", ASCENDING)]),
            IndexModel([("role", ASCENDING), ("status", ASCENDING)]),
            IndexModel([("created_at", DESCENDING)]),
            IndexModel([("kyc.pan", ASCENDING)]),
            IndexModel([("assigned_admin_id", ASCENDING), ("role", ASCENDING)]),
            IndexModel([("assigned_broker_id", ASCENDING), ("role", ASCENDING)]),
            # Multikey index — Mongo creates one entry per element of the
            # array, so {"broker_ancestry": <id>} matches in O(log n).
            IndexModel([("broker_ancestry", ASCENDING)]),
            # White-label custom domain — partial + unique. `sparse=True`
            # was wrong: MongoDB sparse only skips MISSING fields, not
            # explicit `null` values, and Beanie/Pydantic always serializes
            # the field (default None) so every user row had `custom_domain: null`,
            # collapsing the unique constraint to "at most one row with null".
            # `partialFilterExpression` correctly indexes only rows that
            # actually have a string custom_domain set.
            IndexModel(
                [("custom_domain", ASCENDING)],
                unique=True,
                partialFilterExpression={"custom_domain": {"$type": "string"}},
                name="custom_domain_unique_partial",
            ),
        ]

    def is_admin(self) -> bool:
        # BROKER role is considered admin-tier for purposes of the admin
        # login endpoint + admin-side JWT audience. Permission gating then
        # narrows behavior down via require_admin_permission /
        # require_broker_permission.
        return self.role in {
            UserRole.SUPER_ADMIN,
            UserRole.ADMIN,
            UserRole.BROKER,
            UserRole.EMPLOYEE,
        }

    def is_internal(self) -> bool:
        return self.role in {
            UserRole.SUPER_ADMIN,
            UserRole.ADMIN,
            UserRole.BROKER,
            UserRole.MASTER,
            UserRole.DEALER,
        }

    def record_successful_login(self, ip: str) -> None:
        self.last_login_at = now_utc()
        self.last_login_ip = ip
        self.failed_login_count = 0
        self.locked_until = None


# ── User segment toggle (which segments this user may even *see*) ────
class UserSegment(TimestampMixin):
    user_id: PydanticObjectId
    segment: str  # SegmentType.value
    enabled: bool = True

    class Settings:
        name = "user_segments"
        indexes = [
            IndexModel([("user_id", ASCENDING), ("segment", ASCENDING)], unique=True),
        ]
