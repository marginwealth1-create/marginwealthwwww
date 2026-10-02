"""Admin user management — list, detail, create, update, block, wallet adjust, delete."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import (
    CurrentAdmin,
    assert_role_aware_perm,
    assert_user_in_scope,
    require_perm,
    scoped_user_filter,
)
from app.core.security import hash_password
from app.models.audit_log import AuditAction
from app.models.transaction import TransactionType
from app.models.user import User, UserRole, UserStatus
from app.schemas.admin.common import (
    BlockUserRequest,
    CreateUserRequest,
    WalletAdjustRequest,
)
from app.schemas.common import APIResponse
from app.services import user_service, wallet_service
from app.services.audit_service import log_event
from app.utils.decimal_utils import to_decimal128

router = APIRouter(prefix="/users", tags=["admin-users"])

logger = logging.getLogger(__name__)


# Deliberately loose: the column also stores sentinels for closed accounts
# ("<orig>+deleted-<id>") and our own no-email placeholder, so this only
# rejects what is obviously not an address.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _ser(u: User) -> dict:
    return {
        "id": str(u.id),
        "user_code": u.user_code,
        "email": u.email,
        "mobile": u.mobile,
        "full_name": u.full_name,
        "role": u.role.value,
        "status": u.status.value,
        "is_demo": u.is_demo,
        "parent_id": str(u.parent_id) if u.parent_id else None,
        "two_fa_enabled": u.two_fa_enabled,
        "last_login_at": u.last_login_at,
        "created_at": u.created_at,
        # Owner badge fodder — the Users table renders "Self" or
        # "Broker: <name>" / "Admin: <name>" based on these.
        "assigned_admin_id": str(u.assigned_admin_id) if u.assigned_admin_id else None,
        "assigned_broker_id": str(u.assigned_broker_id) if u.assigned_broker_id else None,
        # Transfer telemetry — non-null means this user landed in the
        # current owner's pool via a `Transfer User` reassignment (vs.
        # being originally created by them). Drives the "Transferred"
        # chip on every user-list row.
        "last_transferred_at": u.last_transferred_at,
        "last_transferred_by": (
            str(u.last_transferred_by) if u.last_transferred_by else None
        ),
        # Per-user auto-settlement toggle. Default True; when False the
        # wallet allows negative balance and queues a SettlementRequest
        # for admin approval. Drives the user-detail toggle button +
        # the Payments → Settlement Requests tab.
        "auto_settlement": bool(getattr(u, "auto_settlement", True)),
        # Per-user admin trade alert — drives the side toast + sound in
        # the admin panel when this user opens / closes a trade.
        "trade_alert": bool(getattr(u, "trade_alert", False)),
        "trade_alert_sound": getattr(u, "trade_alert_sound", "chime") or "chime",
    }


async def _enrich_admin_broker_names(rows: list[dict]) -> None:
    """Resolve the parent admin / broker name for each row so the table
    can show 'Broker: Acme Trades' (or 'Sub-broker: …') instead of an id.

    When the assigned broker is itself a sub-broker, also resolve and
    emit `parent_broker_id` + `parent_broker_name` so the UI can render
    the full chain 'Sub-broker: X → Broker: Y'.
    """
    admin_ids = list({r["assigned_admin_id"] for r in rows if r.get("assigned_admin_id")})
    broker_ids = list({r["assigned_broker_id"] for r in rows if r.get("assigned_broker_id")})
    admin_oids = [PydanticObjectId(i) for i in admin_ids]
    broker_oids = [PydanticObjectId(i) for i in broker_ids]
    admins = await User.find({"_id": {"$in": admin_oids}}).to_list() if admin_oids else []
    brokers = await User.find({"_id": {"$in": broker_oids}}).to_list() if broker_oids else []
    admin_name = {str(a.id): a.full_name for a in admins}
    broker_name = {str(b.id): b.full_name for b in brokers}
    # A broker is a sub-broker iff it itself sits under another broker.
    broker_is_sub = {str(b.id): bool(b.assigned_broker_id) for b in brokers}
    # Sub-broker → parent broker id mapping; the UI shows the parent
    # name alongside the sub-broker chip so it's obvious who the
    # downline reports to.
    sub_to_parent_id: dict[str, str] = {
        str(b.id): str(b.assigned_broker_id)
        for b in brokers
        if b.assigned_broker_id
    }
    parent_oids = [PydanticObjectId(pid) for pid in set(sub_to_parent_id.values())]
    parent_brokers = (
        await User.find({"_id": {"$in": parent_oids}}).to_list()
        if parent_oids
        else []
    )
    parent_broker_name = {str(b.id): b.full_name for b in parent_brokers}
    for r in rows:
        r["assigned_admin_name"] = admin_name.get(r.get("assigned_admin_id") or "")
        r["assigned_broker_name"] = broker_name.get(r.get("assigned_broker_id") or "")
        r["assigned_broker_is_sub"] = broker_is_sub.get(
            r.get("assigned_broker_id") or "", False
        )
        sub_broker_id = r.get("assigned_broker_id") or ""
        parent_id = sub_to_parent_id.get(sub_broker_id)
        r["parent_broker_id"] = parent_id
        r["parent_broker_name"] = (
            parent_broker_name.get(parent_id) if parent_id else None
        )


@router.get("", response_model=APIResponse[dict])
async def list_users(
    admin: CurrentAdmin,
    q: str | None = None,
    role: str | None = None,
    status: str | None = None,
    parent_id: str | None = None,
    mode: str = Query(default="live"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    _: None = Depends(require_perm("users", "read")),
):
    query: dict[str, Any] = {}
    # /admin/users is for trading users only. Sub-admins are listed at
    # /admin/management/sub-admins (super-admin only). Reject role filters
    # that would leak admin rows through this endpoint.
    if role:
        if role in {
            UserRole.SUPER_ADMIN.value,
            UserRole.ADMIN.value,
            UserRole.BROKER.value,
        }:
            raise HTTPException(
                status_code=400,
                detail="Admin/broker roles are listed at /admin/management/*",
            )
        query["role"] = role
    else:
        # Hide all admin-tier rows from the regular Users page — they live
        # at /admin/management/sub-admins and /admin/management/brokers.
        query["role"] = {
            "$nin": [
                UserRole.SUPER_ADMIN.value,
                UserRole.ADMIN.value,
                UserRole.BROKER.value,
            ]
        }
    if status:
        # Explicit filter — honour it exactly. Lets the admin select
        # "Closed" from the status dropdown to audit archived users.
        query["status"] = status
    else:
        # Default view hides CLOSED (archived / deleted) users so the
        # list doesn't keep growing forever with soft-deleted rows.
        # Operator-flagged 21-May: clicking Delete archived the user
        # successfully (status = CLOSED) but the row stayed visible in
        # the list, making it look like the action didn't fire. Now a
        # default no-status query excludes CLOSED rows; an admin who
        # wants to audit archived users picks "Closed" from the filter
        # dropdown explicitly.
        query["status"] = {"$ne": UserStatus.CLOSED.value}
    if parent_id:
        query["parent_id"] = PydanticObjectId(parent_id)
    # Comprehensive owned-pool scope (for ADMIN this unions the directly-
    # assigned clients with the whole broker subtree) so users sitting
    # under a transferred broker — whose assigned_admin_id was never
    # propagated — still appear here, matching the dashboard count.
    # The scope may itself be an $or, and the search box is also an $or,
    # so AND them together; merging two $or keys into one dict silently
    # drops the first.
    if mode == "demo":
        # Demo-tab: show ONLY demo users so the admin can manage them.
        query["is_demo"] = True
    else:
        # Live-tab (default): exclude demo accounts from all admin views.
        query["is_demo"] = {"$ne": True}
        query["email"] = {"$not": re.compile(r"@demo\.local$", re.IGNORECASE)}

    scope = await scoped_user_filter(admin)
    and_clauses: list[dict] = []
    if q:
        regex = re.compile(re.escape(q.strip()), re.IGNORECASE)
        and_clauses.append(
            {
                "$or": [
                    {"email": regex},
                    {"mobile": regex},
                    {"user_code": regex},
                    {"full_name": regex},
                ]
            }
        )
    if "$or" in scope:
        and_clauses.append(scope)
    else:
        query.update(scope)
    if and_clauses:
        query["$and"] = and_clauses

    total = await User.find(query).count()
    rows = (
        await User.find(query)
        .sort("-created_at")
        .skip((page - 1) * page_size)
        .limit(page_size)
        .to_list()
    )
    # Python-level safety net (live mode only): strip any demo users that slipped through
    if mode != "demo":
        rows = [r for r in rows if not getattr(r, "is_demo", False) and "@demo.local" not in (r.email or "")]
    items = [_ser(u) for u in rows]
    await _enrich_admin_broker_names(items)

    # Batch-load wallets for the page so the admin list can surface the
    # `available_balance` + `settlement_outstanding` columns. One round-trip
    # via $in keeps this O(1) instead of N+1 over wallet_service.summary().
    from app.models.wallet import Wallet
    user_ids = [u.id for u in rows]
    wallets = await Wallet.find({"user_id": {"$in": user_ids}}).to_list()
    wallet_map = {str(w.user_id): w for w in wallets}

    # Weekly closed P&L (#6) — net realised P&L booked in the current ISO week
    # (Monday 00:00 IST → now). Surfaced on the all-users page in place of the
    # old settlement column. One grouped aggregate over just this page's users.
    from datetime import timedelta as _td

    from app.models.transaction import TransactionType, WalletTransaction
    from app.utils.time_utils import now_ist, to_utc

    _now_i = now_ist()
    _wk_start = to_utc(
        (_now_i - _td(days=_now_i.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    )
    wk_pnl_map: dict[str, float] = {}
    # NET realized P&L for the week = booked P&L − brokerage ± any admin
    # reversals. Summing ONLY PNL rows made the column (a) gross (brokerage
    # never subtracted) and (b) sticky after a delete — deleting a closed
    # trade posts a REVERSAL row (not a PNL row), so the deleted trade's P&L
    # lingered here forever. Include CHARGES (brokerage), REVERSAL
    # (delete/reopen/edit undo), and the stop-out settlement legs so a delete
    # of any close nets its whole footprint back to zero. Operator: "trade
    # delete karne par pnl and brokerage dono weekly se hat jaye."
    _WK_TYPES = [
        TransactionType.PNL.value,
        TransactionType.CHARGES.value,
        TransactionType.REVERSAL.value,
        TransactionType.SETTLEMENT_OUTSTANDING_BOOKED.value,
        TransactionType.SETTLEMENT_OUTSTANDING_RECOVERY.value,
    ]
    try:
        _cur = WalletTransaction.get_motor_collection().aggregate(
            [
                {
                    "$match": {
                        "user_id": {"$in": user_ids},
                        "transaction_type": {"$in": _WK_TYPES},
                        "created_at": {"$gte": _wk_start},
                    }
                },
                {"$group": {"_id": "$user_id", "pnl": {"$sum": {"$toDouble": "$amount"}}}},
            ]
        )
        async for _row in _cur:
            wk_pnl_map[str(_row["_id"])] = float(_row.get("pnl") or 0.0)
    except Exception:  # noqa: BLE001
        wk_pnl_map = {}

    for item in items:
        w = wallet_map.get(item["id"])
        _wk = round(wk_pnl_map.get(item["id"], 0.0), 2)
        item["wallet"] = (
            {
                "available_balance": str(w.available_balance),
                "used_margin": str(w.used_margin),
                "credit_limit": str(w.credit_limit),
                "settlement_outstanding": str(w.settlement_outstanding),
                "weekly_closed_pnl": str(_wk),
            }
            if w is not None
            else {
                "available_balance": "0",
                "used_margin": "0",
                "credit_limit": "0",
                "settlement_outstanding": "0",
                "weekly_closed_pnl": str(_wk),
            }
        )

    return APIResponse(
        data={
            "items": items,
            "meta": {"page": page, "page_size": page_size, "total": total, "total_pages": (total + page_size - 1) // page_size},
        }
    )


@router.get("/live-stats", response_model=APIResponse[dict])
async def users_live_stats(
    admin: CurrentAdmin,
    user_ids: str | None = Query(
        default=None,
        description=(
            "Comma-separated list of user ids to compute stats for. When "
            "omitted the endpoint computes stats for every user in the "
            "admin's scope (capped at 200 for safety)."
        ),
    ),
    _: None = Depends(require_perm("users", "read")),
) -> APIResponse:
    """Live per-user balance + open P&L aggregate for the admin Users
    table. Polled every ~1.5s by the frontend so the OPEN P&L column
    can update with the same cadence the customer-side terminal uses.

    Returns
    -------
    items: list[{
        user_id, available_balance, open_pnl, equity,
        used_margin, credit_limit,
    }]

    Computation
    -----------
    • `available_balance` = wallet cash (from `Wallet.available_balance`)
    • `open_pnl`          = Σ unrealised P&L across the user's currently
                            OPEN positions, recomputed against the latest
                            cached LTP (no DB writes — this is a hot path).
                            USD-quoted segments are baked into INR using
                            the same `usd_inr_rate` snapshot the customer
                            terminal uses, so admin numbers match what
                            users see in real time.
    • `equity`            = `available_balance + open_pnl`. The "left
                            balance" / net account value once floating
                            P&L is folded in.

    Performance notes
    -----------------
    LTP fan-out is parallel across unique tokens (the same pattern used
    by `live_trade_stats` for the per-user detail page) so the per-row
    cost stays sub-linear in the number of open positions across the
    whole page.
    """
    from decimal import Decimal

    from app.models.position import Position, PositionStatus
    from app.models.wallet import Wallet
    from app.services import market_data_service, position_service
    from app.utils.decimal_utils import to_decimal

    # ── 1. Resolve target users (parse ids or query the admin's scope) ──
    target_oids: list[PydanticObjectId] = []
    if user_ids:
        for raw in user_ids.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                target_oids.append(PydanticObjectId(raw))
            except Exception:
                # Skip malformed ids — the frontend filter probably has
                # a stale value during page transitions. Don't 400 the
                # whole call because of it.
                continue
        if not target_oids:
            return APIResponse(data={"items": []})
        scope_query: dict[str, Any] = {"_id": {"$in": target_oids}}
        scope_query.update(await scoped_user_filter(admin))
        users = await User.find(scope_query).to_list()
    else:
        scope_query = {
            "role": {
                "$nin": [
                    UserRole.SUPER_ADMIN.value,
                    UserRole.ADMIN.value,
                    UserRole.BROKER.value,
                ]
            },
            "status": {"$ne": UserStatus.CLOSED.value},
        }
        scope_query.update(await scoped_user_filter(admin))
        users = await User.find(scope_query).limit(200).to_list()

    if not users:
        return APIResponse(data={"items": []})

    user_ids_oid = [u.id for u in users]

    # ── 2. Batch-load wallets (one round-trip) ─────────────────────
    wallets = await Wallet.find({"user_id": {"$in": user_ids_oid}}).to_list()
    wallet_map = {str(w.user_id): w for w in wallets}

    # ── 3. Pull every OPEN position for the target users in one query ─
    open_positions = await Position.find(
        {
            "user_id": {"$in": user_ids_oid},
            "status": PositionStatus.OPEN.value,
        }
    ).to_list()

    # Bucket positions by user for the P&L sum below.
    pos_by_user: dict[str, list] = {}
    for p in open_positions:
        pos_by_user.setdefault(str(p.user_id), []).append(p)

    # ── 4. Parallel LTP fan-out across UNIQUE tokens ─────────────────
    unique_tokens = list({p.instrument.token for p in open_positions})
    ltp_results = await asyncio.gather(
        *[market_data_service.get_display_ltp(tok) for tok in unique_tokens],
        return_exceptions=True,
    )
    ltp_map: dict[str, Any] = {}
    for tok, res in zip(unique_tokens, ltp_results):
        ltp_map[tok] = res if not isinstance(res, BaseException) else None

    # ── 5. Compute per-user aggregates ────────────────────────────────
    items: list[dict[str, Any]] = []
    for u in users:
        uid = str(u.id)
        w = wallet_map.get(uid)
        available = to_decimal(w.available_balance) if w else Decimal("0")
        used_margin = to_decimal(w.used_margin) if w else Decimal("0")
        credit_limit = to_decimal(w.credit_limit) if w else Decimal("0")

        open_pnl = Decimal("0")
        for p in pos_by_user.get(uid, []):
            ltp = ltp_map.get(p.instrument.token)
            if ltp is None:
                # No cached price this tick — fall back to the
                # last-persisted unrealised so the column doesn't blink
                # to 0 when a feed misses one round.
                try:
                    open_pnl += to_decimal(p.unrealized_pnl)
                except Exception:
                    pass
                continue
            try:
                # In-memory refresh — never writes to the DB.
                await position_service.refresh_unrealized_pnl(p, ltp)
                open_pnl += to_decimal(p.unrealized_pnl)
            except Exception:
                # Don't let one bad position kill the whole page.
                try:
                    open_pnl += to_decimal(p.unrealized_pnl)
                except Exception:
                    pass

        equity = available + open_pnl

        items.append(
            {
                "user_id": uid,
                "available_balance": str(available),
                "open_pnl": str(open_pnl),
                "equity": str(equity),
                "used_margin": str(used_margin),
                "credit_limit": str(credit_limit),
            }
        )

    return APIResponse(data={"items": items})


@router.get("/{user_id}", response_model=APIResponse[dict])
async def get_user(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "read")),
):
    u = await assert_user_in_scope(admin, user_id)
    detail = _ser(u)
    wallet = await wallet_service.summary(u.id)
    # Realized P&L that reconciles with deposits (balance = deposits + realised).
    # The wallet's running `realized_pnl` field only tracks PNL ledger rows and
    # MISSES the REVERSAL booked when an admin deletes a closed trade — so it
    # drifted (e.g. showed −192.50 while the true lifetime loss was −416.75).
    # Recompute from the closed positions — the SAME figure the Live Trading
    # Stats dialog shows — so the card, the stats dialog and (deposits − balance)
    # all agree. FX positions are converted to INR at their close/open rate.
    try:
        from app.models.position import Position, PositionStatus
        from app.services import market_data_service as _mds

        _usd_inr = _mds.get_usd_inr_rate()

        def _real_inr(p) -> float:
            raw = float(str(p.realized_pnl or 0))
            is_usd = _mds.is_usd_quoted_segment(p.segment_type) or (
                p.instrument and _mds.is_usd_quoted_segment(p.instrument.segment)
            )
            if not is_usd:
                return raw
            rate = (
                float(str(p.close_usd_inr_rate))
                if p.close_usd_inr_rate is not None
                else float(str(p.open_usd_inr_rate))
                if p.open_usd_inr_rate is not None
                else _usd_inr
            )
            return raw * rate

        _closed = await Position.find(
            {"user_id": u.id, "status": PositionStatus.CLOSED.value}
        ).to_list()
        wallet["realized_pnl"] = str(round(sum(_real_inr(p) for p in _closed), 2))
    except Exception:
        pass  # on any error keep the wallet's own realized_pnl field

    detail.update(
        {
            "kyc": u.kyc.model_dump() if u.kyc else None,
            "permissions": u.permissions.model_dump() if u.permissions else None,
            "trading_hours": u.trading_hours.model_dump() if u.trading_hours else None,
            "risk": u.risk.model_dump() if u.risk else None,
            "communication": u.communication.model_dump() if u.communication else None,
            "wallet": wallet,
        }
    )
    return APIResponse(data=detail)


@router.post("", response_model=APIResponse[dict])
async def create_user(
    payload: CreateUserRequest,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "write")),
):
    # This endpoint only creates trading users (CLIENT). Sub-admins are
    # minted via /api/v1/admin/management/sub-admins (super-admin only).
    # Any other role from the legacy payload is rejected here.
    target_role = UserRole(payload.role)
    if target_role != UserRole.CLIENT:
        raise HTTPException(
            status_code=400,
            detail="Only CLIENT users can be created here. Use /admin/management/sub-admins for sub-admins.",
        )

    # Resolve the ownership chain for the new client based on caller role.
    #   SUPER_ADMIN   → platform pool (assigned_admin_id null, no broker)
    #   ADMIN         → admin's pool (assigned_admin_id = admin.id)
    #   BROKER        → broker's subtree (broker_ancestry includes broker.id);
    #                   assigned_admin_id stays at broker's parent admin so
    #                   the admin still sees this user in their lists.
    assigned_admin_id: PydanticObjectId | None = None
    assigned_broker_id: PydanticObjectId | None = None
    broker_ancestry_for_new: list[PydanticObjectId] = []

    # Optional "Place user under <broker>" selector from the create form.
    # When set, the new user is placed inside that broker's subtree
    # instead of directly under the caller.
    #
    # Scope check is custom here because `assert_user_in_scope` rejects
    # admin/broker targets outright ("Cannot operate on an admin/broker
    # user via this endpoint") — that guard exists so non-admin
    # endpoints can't mutate elevated rows.  Placement is different:
    # we're not mutating the broker, just stamping a new client under
    # them.  Enforce scope manually:
    #   - SUPER_ADMIN can target any broker
    #   - ADMIN can target brokers/sub-brokers in their pool
    #   - BROKER can target their own subtree
    target_broker: User | None = None
    if payload.assign_to_broker_id:
        try:
            tb_oid = PydanticObjectId(payload.assign_to_broker_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid assign_to_broker_id")
        target_broker = await User.get(tb_oid)
        if target_broker is None or target_broker.role != UserRole.BROKER:
            raise HTTPException(
                status_code=400,
                detail="assign_to_broker_id must reference a broker / sub-broker user.",
            )
        if admin.role == UserRole.ADMIN:
            if target_broker.assigned_admin_id != admin.id:
                raise HTTPException(
                    status_code=403,
                    detail="That broker is not in your pool.",
                )
        elif admin.role == UserRole.BROKER:
            ancestry = [str(a) for a in (target_broker.broker_ancestry or [])]
            if target_broker.id == admin.id or str(admin.id) not in ancestry:
                raise HTTPException(
                    status_code=403,
                    detail="That broker is not in your downline.",
                )

    if admin.role == UserRole.SUPER_ADMIN:
        if target_broker is not None:
            # Super-admin placing user under a specific broker/sub-broker.
            assigned_admin_id = target_broker.assigned_admin_id
            assigned_broker_id = target_broker.id
            broker_ancestry_for_new = (
                list(target_broker.broker_ancestry or []) + [target_broker.id]
            )
    elif admin.role == UserRole.BROKER:
        if payload.parent_id:
            await assert_user_in_scope(admin, payload.parent_id)
        assigned_admin_id = admin.assigned_admin_id  # inherit broker's top admin
        if target_broker is not None:
            # Broker placing user under one of their sub-brokers.
            assigned_broker_id = target_broker.id
            broker_ancestry_for_new = (
                list(target_broker.broker_ancestry or []) + [target_broker.id]
            )
        else:
            assigned_broker_id = admin.id
            broker_ancestry_for_new = list(admin.broker_ancestry or []) + [admin.id]
    else:
        # ADMIN
        if payload.parent_id:
            await assert_user_in_scope(admin, payload.parent_id)
        assigned_admin_id = admin.id
        if target_broker is not None:
            # Admin placing user under one of their brokers/sub-brokers.
            assigned_broker_id = target_broker.id
            broker_ancestry_for_new = (
                list(target_broker.broker_ancestry or []) + [target_broker.id]
            )

    user = await user_service.create_user(
        email=payload.email,
        mobile=payload.mobile,
        password=payload.password,
        full_name=payload.full_name,
        role=target_role,
        status=UserStatus.ACTIVE,
        parent_id=PydanticObjectId(payload.parent_id) if payload.parent_id else None,
        is_demo=payload.is_demo,
        created_by=admin.id,
        assigned_admin_id=assigned_admin_id,
        assigned_broker_id=assigned_broker_id,
        broker_ancestry=broker_ancestry_for_new,
    )
    initial_bal = payload.initial_balance or (100_000 if payload.is_demo else 0)
    if initial_bal:
        await wallet_service.adjust(
            user.id,
            initial_bal,
            transaction_type=TransactionType.ADJUSTMENT if not payload.is_demo else TransactionType.BONUS,
            narration=(
                "Demo account virtual credit" if payload.is_demo
                else f"Initial balance credit by {admin.user_code}"
            ),
            actor_id=admin.id,
        )
    if payload.credit_limit:
        from app.services import wallet_service as ws
        wallet = await ws.get(user.id)
        wallet.credit_limit = to_decimal128(payload.credit_limit)
        await wallet.save()

    await log_event(
        action=AuditAction.CREATE,
        entity_type="User",
        entity_id=user.id,
        actor_id=admin.id,
        target_user_id=user.id,
    )
    return APIResponse(data=_ser(user))


@router.put("/{user_id}", response_model=APIResponse[dict])
async def update_user(
    user_id: str,
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "write")),
):
    u = await assert_user_in_scope(admin, user_id)
    for k in ("full_name", "photo_url", "is_demo"):
        if k in payload and payload[k] is not None:
            setattr(u, k, payload[k])

    # Contact details. An admin-created account often starts with neither,
    # and the real email / phone turn up later — so both are editable here,
    # blank included: clearing one puts back the placeholder rather than an
    # empty string, because both columns are uniquely indexed and non-null.
    if "email" in payload or "mobile" in payload:
        from app.utils.validators import is_valid_mobile_in, normalize_mobile_in

        new_email = u.email
        new_mobile = u.mobile
        if "email" in payload:
            raw = str(payload.get("email") or "").strip().lower()
            if not raw:
                new_email = user_service.placeholder_email(u.user_code)
            elif not _EMAIL_RE.match(raw):
                raise HTTPException(status_code=400, detail="Invalid email address")
            else:
                new_email = raw
        if "mobile" in payload:
            raw = str(payload.get("mobile") or "").strip()
            if not raw:
                new_mobile = user_service.placeholder_mobile(u.user_code)
            else:
                normalised = normalize_mobile_in(raw)
                if not is_valid_mobile_in(normalised):
                    raise HTTPException(
                        status_code=400, detail="Invalid Indian mobile number"
                    )
                new_mobile = normalised
        # Only the values that actually changed are worth a conflict check,
        # and the user's own row must never count as the conflict.
        conflict = await user_service.email_or_mobile_taken(
            new_email if new_email != u.email else None,
            new_mobile if new_mobile != u.mobile else None,
            exclude_user_id=u.id,
        )
        if conflict:
            raise HTTPException(
                status_code=409, detail=f"Another user already has this {conflict}"
            )
        u.email, u.mobile = new_email, new_mobile
    if "permissions" in payload and payload["permissions"]:
        for k, v in payload["permissions"].items():
            if hasattr(u.permissions, k):
                setattr(u.permissions, k, v)
    if "risk" in payload and payload["risk"]:
        for k, v in payload["risk"].items():
            if hasattr(u.risk, k):
                setattr(u.risk, k, v)
    await u.save()
    await log_event(
        action=AuditAction.UPDATE, entity_type="User", entity_id=u.id, actor_id=admin.id, target_user_id=u.id
    )
    return APIResponse(data=_ser(u))


@router.post("/{user_id}/block", response_model=APIResponse[dict])
async def block(
    user_id: str,
    payload: BlockUserRequest,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "write")),
):
    u = await assert_user_in_scope(admin, user_id)
    u.status = UserStatus.BLOCKED
    await u.save()
    # Kick the user out of every active session immediately — bump the
    # session epoch + purge refresh tokens. Without this the blocked user
    # kept their live web/app session (their access token stayed valid for
    # up to 15 min and they never got redirected to login). Operator-flagged.
    from app.services import auth_service as _auth

    await _auth.revoke_user_sessions(u)
    await log_event(
        action=AuditAction.BLOCK,
        entity_type="User",
        entity_id=u.id,
        actor_id=admin.id,
        target_user_id=u.id,
        metadata={"reason": payload.reason},
    )
    return APIResponse(data=_ser(u))


@router.post("/{user_id}/unblock", response_model=APIResponse[dict])
async def unblock(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "write")),
):
    u = await assert_user_in_scope(admin, user_id)
    u.status = UserStatus.ACTIVE
    u.failed_login_count = 0
    u.locked_until = None
    await u.save()
    await log_event(
        action=AuditAction.UNBLOCK, entity_type="User", entity_id=u.id, actor_id=admin.id, target_user_id=u.id
    )
    return APIResponse(data=_ser(u))


@router.post("/{user_id}/auto-settlement", response_model=APIResponse[dict])
async def set_auto_settlement(
    user_id: str,
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "write")),
):
    """Toggle a user's `auto_settlement` flag (default True). When True
    the wallet auto-floors at 0 + books shortfall to
    `settlement_outstanding` as today. When False the wallet is allowed
    to go negative and `wallet_service` queues a pending
    SettlementRequest for admin approval from Payments → Settlement
    Requests.

    Payload: `{"enabled": bool}`. Audit-logged.
    """
    enabled = bool(payload.get("enabled"))
    u = await assert_user_in_scope(admin, user_id)
    old_value = bool(getattr(u, "auto_settlement", True))
    u.auto_settlement = enabled
    await u.save()
    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="User",
        entity_id=u.id,
        actor_id=admin.id,
        target_user_id=u.id,
        old_values={"auto_settlement": old_value},
        new_values={"auto_settlement": enabled},
        metadata={"action": "AUTO_SETTLEMENT_TOGGLE"},
    )
    return APIResponse(data=_ser(u))


@router.post("/{user_id}/trade-alert", response_model=APIResponse[dict])
async def set_trade_alert(
    user_id: str,
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "write")),
):
    """Watch (or stop watching) this user's trades.

    With it on, every fill on the user's positions pops a toast in the
    admin panel — scoped to the admins who own the user — and plays the
    chosen sound. Payload: `{"enabled": bool, "sound": str}`; `sound` is
    optional and keeps the stored one when omitted. Audit-logged, because
    "why didn't I get alerted" is a question that gets asked later.
    """
    u = await assert_user_in_scope(admin, user_id)
    old_value = {
        "trade_alert": bool(getattr(u, "trade_alert", False)),
        "trade_alert_sound": getattr(u, "trade_alert_sound", "chime"),
    }
    if "enabled" in payload:
        u.trade_alert = bool(payload.get("enabled"))
    sound = str(payload.get("sound") or "").strip()
    if sound:
        u.trade_alert_sound = sound[:40]
    await u.save()
    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="User",
        entity_id=u.id,
        actor_id=admin.id,
        target_user_id=u.id,
        old_values=old_value,
        new_values={
            "trade_alert": u.trade_alert,
            "trade_alert_sound": u.trade_alert_sound,
        },
        metadata={"action": "TRADE_ALERT_TOGGLE"},
    )
    return APIResponse(data=_ser(u))


@router.post("/{user_id}/reset-password", response_model=APIResponse[dict])
async def admin_reset_password(
    user_id: str,
    payload: dict,
    admin: CurrentAdmin,
):
    # Admins keep the `users` gate; a BROKER needs the dedicated `user_password`
    # EDIT — so granting a broker Users-EDIT (create users) does NOT also let
    # them reset their users' passwords.
    assert_role_aware_perm(admin, admin_perm="users", broker_perm="user_password", mode="write")
    new_pw = payload.get("new_password") or ""
    if len(new_pw) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    u = await assert_user_in_scope(admin, user_id)
    u.password_hash = hash_password(new_pw)
    u.must_change_password = True
    u.failed_login_count = 0
    await u.save()
    # Force-logout everywhere so the OLD password's live sessions die the
    # moment the password changes — otherwise the access token stayed valid
    # for 15 min and refresh kept minting new ones, so the user never had to
    # re-login with the new password. Operator-flagged.
    from app.services import auth_service as _auth

    await _auth.revoke_user_sessions(u)
    await log_event(
        action=AuditAction.PASSWORD_RESET,
        entity_type="User",
        entity_id=u.id,
        actor_id=admin.id,
        target_user_id=u.id,
    )
    return APIResponse(data={"ok": True})


@router.post("/{user_id}/wallet-adjust", response_model=APIResponse[dict])
async def wallet_adjust(
    user_id: str,
    payload: WalletAdjustRequest,
    admin: CurrentAdmin,
):
    from app.models.wallet import Wallet
    from app.utils.decimal_utils import to_decimal as _to_decimal

    amt = _to_decimal(payload.amount)
    # Fund add/deduct gate. Admins keep the `users` grant; a BROKER needs the
    # money section that matches the direction — Deposits EDIT to ADD funds,
    # Withdrawals EDIT to DEDUCT — so a broker with only Users-EDIT (create
    # users) can NOT add/deduct funds from the 3-dot menu.
    assert_role_aware_perm(
        admin,
        admin_perm="users",
        broker_perm=("deposits" if amt >= 0 else "withdrawals"),
        mode="write",
    )
    await assert_user_in_scope(admin, user_id)

    # ── Balance pre-check for debits ─────────────────────────────────
    # Admin manual deduct must NEVER silently book to settlement_outstanding
    # when the user's available_balance is insufficient.  Same rule as
    # withdrawals: if you don't have the money, the action is rejected.
    if amt < 0:
        # `user_id` arrives as a string from the path param; the Wallet
        # row keys on PydanticObjectId, so a string `==` filter never
        # matches and the lookup silently returns None ("User wallet
        # not found" toast even when the wallet exists).  Cast first.
        try:
            uid = PydanticObjectId(user_id)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid user id")
        wallet = await Wallet.find_one(Wallet.user_id == uid)
        if wallet is None:
            raise HTTPException(status_code=400, detail="User wallet not found")
        available = _to_decimal(wallet.available_balance)
        if available + amt < 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Insufficient balance: user has ₹{available:,.2f} available "
                    f"but you're trying to debit ₹{abs(amt):,.2f}."
                ),
            )

    txn = await wallet_service.adjust(
        user_id,
        payload.amount,
        transaction_type=TransactionType(payload.transaction_type),
        narration=payload.narration,
        actor_id=admin.id,
    )
    await log_event(
        action=AuditAction.WALLET_ADJUST,
        entity_type="Wallet",
        entity_id=str(txn.id),
        actor_id=admin.id,
        target_user_id=user_id,
        metadata={"amount": str(payload.amount), "type": payload.transaction_type},
    )
    return APIResponse(data={"transaction_id": str(txn.id), "amount": str(txn.amount)})


@router.delete("/{user_id}", response_model=APIResponse[dict])
async def delete_user(
    user_id: str,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "write")),
):
    u = await assert_user_in_scope(admin, user_id)
    if u.role == UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=400, detail="Super admin cannot be deleted")
    # Sub-admins must not delete other admins via this generic endpoint.
    if admin.role != UserRole.SUPER_ADMIN and u.role == UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Cannot delete an admin user")

    # Full financial purge on EVERY delete (demo AND real). Without this a
    # deleted real user was only soft-closed, so their Position / Trade rows
    # lingered and kept showing up in the admin's live aggregates — open
    # positions, Open PNL, This/Last Week's Net P&L on the Positions page,
    # position-card P&L — even though the user was "deleted". Deleting the
    # financial rows drops them out of every one of those queries at once
    # (all scope by Position/Trade user_id). The user row itself is still
    # tombstoned below (not for demo) so re-registration + the delete audit
    # trail survive.
    import asyncio as _asyncio
    from app.models.order import Order
    from app.models.position import Position
    from app.models.trade import Trade
    from app.models.transaction import DepositRequest, WalletTransaction, WithdrawalRequest
    from app.models.wallet import Wallet

    uid = u.id
    await _asyncio.gather(
        Order.find({"user_id": uid}).delete(),
        Position.find({"user_id": uid}).delete(),
        Trade.find({"user_id": uid}).delete(),
        Wallet.find({"user_id": uid}).delete(),
        WalletTransaction.find({"user_id": uid}).delete(),
        DepositRequest.find({"user_id": uid}).delete(),
        WithdrawalRequest.find({"user_id": uid}).delete(),
    )

    # Demo users: also drop the row entirely (shared throwaway accounts).
    if u.is_demo:
        await u.delete()
        await log_event(
            action=AuditAction.DELETE, entity_type="User", entity_id=uid, actor_id=admin.id, target_user_id=uid
        )
        return APIResponse(data={"ok": True, "status": "deleted"})

    u.status = UserStatus.CLOSED
    # Free up the email + mobile so the user (or someone using their
    # contact) can re-register without hitting the unique index.  The
    # original values are tucked into a tombstone field for the audit
    # trail.  Without this, deleting and re-registering with the same
    # email always errored with "email already exists" (the unique
    # index doesn't know about UserStatus.CLOSED).
    if u.email and "+deleted-" not in u.email:
        u.deleted_email_original = u.email
        # Plus-address in the LOCAL part ("name+deleted-<id>@domain") so the
        # tombstoned email stays a VALID, well-formed address. The old form
        # appended the suffix after the domain ("name@domain+deleted-<id>"),
        # which is not a valid email — harmless now that the model stores
        # email as str, but this keeps freed-up rows clean. The "+deleted-"
        # marker is still present, so the dedup check above still detects it.
        if "@" in u.email:
            local, _, domain = u.email.partition("@")
            u.email = f"{local}+deleted-{str(u.id)}@{domain}"
        else:
            u.email = f"{u.email}+deleted-{str(u.id)}"
    if u.mobile and not u.mobile.startswith("DEL"):
        u.deleted_mobile_original = u.mobile
        u.mobile = f"DEL{str(u.id)[-12:]}"
    await u.save()
    await log_event(
        action=AuditAction.DELETE, entity_type="User", entity_id=u.id, actor_id=admin.id, target_user_id=u.id
    )
    return APIResponse(data={"ok": True, "status": u.status.value})


# ── Credit limit (Give / Take Credit) ───────────────────────────────
@router.patch("/{user_id}/credit-limit", response_model=APIResponse[dict])
async def update_credit_limit(
    user_id: str,
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "write")),
):
    """Adjust the user's credit_limit by `delta` (positive = give credit,
    negative = take credit). The new total cannot go below 0."""
    from bson import Decimal128
    from decimal import Decimal

    from app.utils.decimal_utils import to_decimal

    delta_raw = payload.get("delta")
    if delta_raw is None:
        raise HTTPException(status_code=400, detail="delta is required")
    try:
        delta = to_decimal(delta_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid delta value")
    narration = (payload.get("narration") or "").strip() or "credit limit adjust"

    await assert_user_in_scope(admin, user_id)
    wallet = await wallet_service.get_or_create(user_id)
    new_limit = to_decimal(wallet.credit_limit) + delta
    if new_limit < Decimal("0"):
        raise HTTPException(
            status_code=400,
            detail=f"Resulting credit limit would be negative (current ₹{wallet.credit_limit}, delta ₹{delta})",
        )
    wallet.credit_limit = Decimal128(str(new_limit))
    wallet.version += 1
    await wallet.save()

    await log_event(
        action=AuditAction.WALLET_ADJUST,
        entity_type="Wallet",
        entity_id=str(wallet.id),
        actor_id=admin.id,
        target_user_id=user_id,
        metadata={"delta": str(delta), "new_credit_limit": str(new_limit), "narration": narration, "kind": "CREDIT_LIMIT"},
    )
    return APIResponse(
        data={
            "credit_limit": str(new_limit),
            "delta": str(delta),
            "narration": narration,
        }
    )


# ── Kill Switch ─────────────────────────────────────────────────────
@router.post("/{user_id}/kill-switch", response_model=APIResponse[dict])
async def kill_switch(
    user_id: str,
    payload: dict,
    admin: CurrentAdmin,
    _: None = Depends(require_perm("users", "write")),
):
    """Emergency stop for a user account:
        1. Cancel all pending / open orders
        2. Square off all open positions at market
        3. Block the account (status = BLOCKED)

    Idempotent — running it twice on a stopped user is a no-op.
    """
    from beanie import PydanticObjectId

    from app.models._base import OrderAction, OrderType, ProductType
    from app.models.order import Order, OrderStatus
    from app.models.position import Position, PositionStatus
    from app.services import order_service

    u = await assert_user_in_scope(admin, user_id)
    reason = (payload.get("reason") or "kill switch").strip()

    # 1) Cancel pending orders — use a raw Mongo $in expression because
    # Beanie's `Order.status.in_(...)` chain isn't supported on enum fields.
    cancelled_count = 0
    pending = await Order.find(
        {
            "user_id": PydanticObjectId(user_id),
            "status": {
                "$in": [
                    OrderStatus.OPEN.value,
                    OrderStatus.PENDING.value,
                    OrderStatus.PARTIAL.value,
                ]
            },
        }
    ).to_list()
    for o in pending:
        try:
            await order_service.admin_force_cancel(str(o.id), reason="KILL_SWITCH")
            cancelled_count += 1
        except Exception:
            continue

    # 2) Square off all open positions at market
    open_positions = await Position.find(
        Position.user_id == PydanticObjectId(user_id),
        Position.status == PositionStatus.OPEN,
    ).to_list()
    squared_off = 0
    for p in open_positions:
        if p.quantity == 0:
            continue
        action = OrderAction.SELL if p.quantity > 0 else OrderAction.BUY
        full_qty = abs(p.quantity)
        full_lots = max(0.01, full_qty / max(1, p.instrument.lot_size or 1))
        try:
            # `force_quantity` + `is_squareoff` mirrors admin_squareoff —
            # flattens the exact open size and bypasses validator caps so
            # the kill switch works 24×7 (weekend / off-hours / margin).
            await order_service.place_order(
                user=u,
                payload={
                    "token": p.instrument.token,
                    "action": action.value,
                    "order_type": OrderType.MARKET.value,
                    "product_type": p.product_type.value,
                    "lots": full_lots,
                    "force_quantity": full_qty,
                    "placed_from": "ADMIN_KILL_SWITCH",
                    "is_squareoff": True,
                },
            )
            squared_off += 1
        except Exception:
            continue

    # 3) Block the user
    u.status = UserStatus.BLOCKED
    await u.save()

    await log_event(
        action=AuditAction.SQUAREOFF_FORCE,
        entity_type="User",
        entity_id=u.id,
        actor_id=admin.id,
        target_user_id=u.id,
        metadata={
            "kind": "KILL_SWITCH",
            "reason": reason,
            "orders_cancelled": cancelled_count,
            "positions_squared_off": squared_off,
        },
    )
    return APIResponse(
        data={
            "ok": True,
            "orders_cancelled": cancelled_count,
            "positions_squared_off": squared_off,
            "user_status": u.status.value,
        }
    )


# ── Login As (impersonate) ──────────────────────────────────────────
@router.post("/{user_id}/impersonate", response_model=APIResponse[dict])
async def impersonate(user_id: str, admin: CurrentAdmin):
    """Mint a user-side JWT pair for the target user. Admin-only.

    The returned tokens hit the user app's /api/v1/user routes — admin pastes
    them into the user app's localStorage (or the admin UI does it for them
    with `window.open(...)`) and operates the user app as that user.
    """
    from app.core.config import settings as cfg
    from app.core.redis_client import cache_set
    from app.core.security import (
        create_access_token,
        create_refresh_token,
        refresh_jti_key,
        session_key,
    )
    from app.models.user import UserRole

    target = await assert_user_in_scope(admin, user_id)
    if target.role == UserRole.SUPER_ADMIN and admin.role != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Cannot impersonate a super admin")
    # Sub-admin must not impersonate another admin.
    if admin.role != UserRole.SUPER_ADMIN and target.role in {
        UserRole.SUPER_ADMIN,
        UserRole.ADMIN,
    }:
        raise HTTPException(status_code=403, detail="Cannot impersonate an admin")

    target_id = str(target.id)  # always present after get_user_or_404
    access = create_access_token(
        user_id=target_id,
        role=target.role.value,
        # Carry the target's session epoch so the impersonation token isn't
        # rejected by the `ver` gate (and dies if the target is blocked).
        extra={"impersonator": str(admin.id), "ver": int(target.token_version or 0)},
    )
    refresh, jti = create_refresh_token(user_id=target_id, role=target.role.value)
    await cache_set(
        refresh_jti_key(str(target.id), jti),
        {
            "user_id": str(target.id),
            "audience": "user",
            "impersonator": str(admin.id),
        },
        ttl_sec=cfg.JWT_REFRESH_TTL_DAYS * 86400,
    )
    await cache_set(
        session_key(str(target.id), jti),
        {"audience": "user", "impersonator": str(admin.id)},
        ttl_sec=cfg.JWT_REFRESH_TTL_DAYS * 86400,
    )

    await log_event(
        action=AuditAction.IMPERSONATE,
        entity_type="User",
        entity_id=target.id,
        actor_id=admin.id,
        target_user_id=target.id,
        metadata={"as_role": target.role.value},
    )

    # Where the admin UI opens the impersonated session.
    #
    # Default = the shared platform user app (CORS_USER_ORIGIN). But if the
    # target user belongs to an admin with a READY custom domain (e.g.
    # tradeox.in), open DIRECTLY on that branded host instead. Opening on
    # the platform first makes the branding context redirect to the custom
    # domain via the ``#wl=`` hash handoff, which carries the TOKENS but not
    # the auth-store user — so the terminal guard sees "no user" and bounces
    # back to /login (impersonation never sticks for branded admins). Landing
    # straight on the branded host lets the login page's impersonation effect
    # run there (me() → setUser) and persist the session. Falls back cleanly
    # to the platform origin for non-branded / platform users.
    #
    # CORS_USER_ORIGIN may hold comma-separated origins (e.g.
    # "https://marginwealth.live,https://www.marginwealth.live") — take the FIRST canonical
    # origin only, otherwise the comma lands in the URL and the browser tries
    # to resolve the whole string as a hostname (DNS_PROBE_FINISHED_NXDOMAIN).
    user_app_url = cfg.CORS_USER_ORIGIN.split(",")[0].strip()
    try:
        from app.services import branding_service

        brand_domain = await branding_service.brand_domain_for_user(target)
        if brand_domain:
            user_app_url = f"https://{brand_domain}"
    except Exception:  # pragma: no cover - defensive; never block login-as
        logger.exception("impersonate_brand_domain_resolve_failed")

    return APIResponse(
        data={
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": cfg.JWT_ACCESS_TTL_MIN * 60,
            "user": {
                "id": str(target.id),
                "user_code": target.user_code,
                "email": target.email,
                "mobile": target.mobile,
                "full_name": target.full_name,
                "role": target.role.value,
                "status": target.status.value,
                "is_demo": target.is_demo,
                "two_fa_enabled": target.two_fa_enabled,
                "must_change_password": target.must_change_password,
            },
            "user_app_url": user_app_url,
        }
    )


# ── Live Trade Stats ────────────────────────────────────────────────
@router.get("/{user_id}/live-trade-stats", response_model=APIResponse[dict])
async def live_trade_stats(user_id: str, admin: CurrentAdmin):
    """Per-user live trading snapshot for the admin row dropdown.

    Aggregates:
      • floating_pnl     — open unrealised P&L (INR), close-side prices
                           applied for USD-quoted segments
      • margin_used      — wallet.used_margin (currently locked)
      • equity           — available + used + floating P&L
      • cf_total_eod     — sum of overnight margin needed for every
                           currently-open MIS/NRML position at EOD rates
      • cf_extra_needed  — max(0, cf_total_eod − wallet free balance)
      • weekly_net_pnl   — realised P&L this IST week
      • weekly_trades    — closed-position count this IST week
                           (also split into wins / losses)
      • closed_pnl_all   — realised P&L lifetime
      • all_time_trades  — closed-position count lifetime
      • open_positions   — list of currently-open positions (symbol,
                           qty, avg, ltp, floating P&L per row)
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from decimal import Decimal

    from app.models.position import Position, PositionStatus
    from app.services import (
        market_data_service,
        netting_service,
        position_service,
    )
    from app.utils.decimal_utils import to_decimal

    target = await assert_user_in_scope(admin, user_id)
    wallet = await wallet_service.get_or_create(target.id)

    available = float(str(wallet.available_balance))
    used_margin = float(str(wallet.used_margin))
    credit_limit = float(str(wallet.credit_limit))

    IST = _tz(_td(hours=5, minutes=30))
    now_ist_dt = _dt.now(IST)
    today_start = now_ist_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    days_back = (now_ist_dt.weekday() + 1) % 7  # Sun = 0
    week_start_ist = today_start - _td(days=days_back)
    week_start = week_start_ist.astimezone(_tz.utc)

    usd_inr = market_data_service.get_usd_inr_rate()

    def _is_usd(p) -> bool:
        return market_data_service.is_usd_quoted_segment(p.segment_type) or (
            p.instrument
            and market_data_service.is_usd_quoted_segment(p.instrument.segment)
        )

    def _realised_inr(p) -> float:
        raw = float(str(p.realized_pnl))
        if not _is_usd(p):
            return raw
        # User-side trade history bakes FX at CLOSE time (matching_engine
        # stamps `trade.pnl_inr` using the close-time USDINR), so the same
        # rate must be used here for admin numbers to match what the user
        # sees. Fall back to open rate for positions that were only
        # partially closed (no `close_usd_inr_rate` set yet), then to the
        # current spot for the rare row that has neither rate snapshot.
        rate = (
            float(str(p.close_usd_inr_rate))
            if p.close_usd_inr_rate is not None
            else float(str(p.open_usd_inr_rate))
            if p.open_usd_inr_rate is not None
            else usd_inr
        )
        return raw * rate

    # Closed positions: this IST week + all-time
    weekly_closed = await Position.find(
        {
            "user_id": target.id,
            "status": PositionStatus.CLOSED.value,
            "closed_at": {"$gte": week_start},
        }
    ).to_list()
    all_closed = await Position.find(
        {
            "user_id": target.id,
            "status": PositionStatus.CLOSED.value,
        }
    ).to_list()

    weekly_realised = sum(_realised_inr(p) for p in weekly_closed)
    weekly_wins = sum(1 for p in weekly_closed if _realised_inr(p) > 0)
    weekly_losses = sum(1 for p in weekly_closed if _realised_inr(p) < 0)
    all_realised = sum(_realised_inr(p) for p in all_closed)
    all_wins = sum(1 for p in all_closed if _realised_inr(p) > 0)
    all_losses = sum(1 for p in all_closed if _realised_inr(p) < 0)

    # Open positions: floating P&L + carry-forward requirement
    open_positions = await Position.find(
        {"user_id": target.id, "status": PositionStatus.OPEN.value}
    ).to_list()

    open_rows: list[dict[str, Any]] = []
    floating_pnl = 0.0
    cf_total_eod = 0.0

    # Parallel LTP fan-out (see /admin/positions for rationale). The
    # user-detail page hits this on every navigation from the sidebar
    # so the serial loop was adding ~100 ms × N positions to the
    # response time — multi-second blank state on a busy account.
    unique_tokens = list({p.instrument.token for p in open_positions})
    ltp_results = await asyncio.gather(
        *[market_data_service.get_display_ltp(tok) for tok in unique_tokens],
        return_exceptions=True,
    )
    ltp_map: dict[str, Any] = {}
    for tok, res in zip(unique_tokens, ltp_results):
        ltp_map[tok] = res if not isinstance(res, BaseException) else None

    for p in open_positions:
        # Refresh live LTP + recompute unrealised so this snapshot
        # reflects the same number the user side sees right now.
        try:
            cached = ltp_map.get(p.instrument.token)
            if cached is None:
                raise RuntimeError("ltp feed miss")
            ltp = cached
            await position_service.refresh_unrealized_pnl(p, ltp)
        except Exception:
            ltp = to_decimal(p.ltp or 0)

        avg = float(str(p.avg_price))
        ltp_native = float(str(p.ltp or 0))
        qty = float(p.quantity)
        raw = (ltp_native - avg) * qty
        if _is_usd(p):
            raw *= usd_inr
        floating_pnl += raw

        # Compute carry-forward (NRML) margin needed for this open
        # position via the same resolver order_validator uses.
        try:
            # Derive CE/PE from the symbol so the resolver applies the admin's
            # per-side option overrides (Opt Sell/Buy Fixed ₹/lot). With
            # option_type=None it fell back to the generic segment Times/% and
            # CF Total (EOD) showed the ~20% number (₹795) instead of the
            # configured Fixed carry (₹15000/lot). Same fix as the user
            # positions endpoint + EOD carry rollover.
            _csym = (p.instrument.symbol or "").upper()
            _cotype = (
                ("CE" if _csym.endswith("CE") else "PE" if _csym.endswith("PE") else None)
                if len(_csym) >= 3 and _csym[-3].isdigit()
                else None
            )
            resolved = await netting_service.get_effective_settings(
                target.id,
                p.instrument.segment,
                action="BUY" if qty >= 0 else "SELL",
                option_type=_cotype,
                product_type="NRML",
                symbol=p.instrument.symbol,
            )
            s = resolved.get("settings") or {}
            mode = (s.get("margin_calc_mode") or "").lower()
            stored_lot = max(1, int(p.instrument.lot_size or 1))
            abs_qty = abs(qty)
            notional = avg * abs_qty
            # CF (carry-forward) margin MUST read the overnight_* triple,
            # NOT the product-aware `leverage` / `margin_percentage` /
            # `fixed_margin_per_lot`. In Times mode the resolver keeps
            # the product-aware fields on the INTRADAY value (the
            # "symmetric-Times patch" in netting_service), so reading
            # them here returned the intraday number — operator-flagged
            # 22-May: NIFTY26MAYFUT MIS 65-qty showed CF Total ₹3,096
            # (= intraday at 500×) when it should have been ₹25,802 (=
            # notional ÷ overnight=60). Same bug family the position
            # serializer + intraday-to-carry rollover already fixed.
            ovn_fixed = float(s.get("overnight_fixed_margin_per_lot") or 0)
            if mode == "fixed" and ovn_fixed > 0:
                lots = abs_qty / stored_lot
                nrml_margin = ovn_fixed * lots
            else:
                ovn_pct = float(s.get("overnight_margin_percentage") or 100.0) / 100.0
                ovn_lev = float(s.get("overnight_leverage") or 1.0) or 1.0
                nrml_margin = (notional * ovn_pct) / ovn_lev
                if _is_usd(p):
                    nrml_margin *= usd_inr
            cf_total_eod += nrml_margin
        except Exception:
            pass

        open_rows.append(
            {
                "symbol": p.instrument.symbol,
                "exchange": str(p.instrument.exchange),
                "segment": p.instrument.segment,
                "instrument_token": p.instrument.token,
                "product_type": p.product_type.value,
                "quantity": qty,
                "lots": qty / stored_lot if stored_lot > 0 else qty,
                "avg_price": avg,
                "ltp": ltp_native,
                "unrealized_pnl_inr": round(raw, 2),
                "is_usd": bool(_is_usd(p)),
            }
        )

    free_balance = available + credit_limit
    cf_extra_needed = max(0.0, cf_total_eod - free_balance)
    equity = available + used_margin + floating_pnl

    return APIResponse(
        data={
            "user_id": str(target.id),
            "user_code": target.user_code,
            "full_name": target.full_name,
            "floating_pnl": round(floating_pnl, 2),
            "margin_used": round(used_margin, 2),
            "available_balance": round(available, 2),
            "credit_limit": round(credit_limit, 2),
            "equity": round(equity, 2),
            "cf_total_eod": round(cf_total_eod, 2),
            "cf_extra_needed": round(cf_extra_needed, 2),
            "weekly_net_pnl": round(weekly_realised, 2),
            "weekly_trades": len(weekly_closed),
            "weekly_wins": weekly_wins,
            "weekly_losses": weekly_losses,
            "closed_pnl_all_time": round(all_realised, 2),
            "all_time_trades": len(all_closed),
            "all_time_wins": all_wins,
            "all_time_losses": all_losses,
            "open_positions": open_rows,
            "usd_inr_rate": round(usd_inr, 4),
        }
    )
