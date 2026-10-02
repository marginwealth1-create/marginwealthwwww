"""Admin platform settings + holidays + backup/EOD + audit logs."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from beanie import PydanticObjectId
from fastapi import APIRouter, HTTPException, Query

from app.core.dependencies import CurrentAdmin
from app.models._base import Exchange
from app.models.audit_log import AuditAction, AuditLog
from app.models.holiday import TradingHoliday
from app.models.platform_setting import PlatformSetting, SettingType
from app.models.user import UserRole
from app.schemas.admin.common import UpdatePlatformSettingRequest
from app.schemas.common import APIResponse
from app.services.audit_service import log_event

router = APIRouter(tags=["admin-settings"])


def _require_super_admin(admin) -> None:
    """The kill-switch is platform-wide, so only the super-admin may flip it."""
    if getattr(admin, "role", None) != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super-admin only")


def _require_admin_or_super(admin) -> None:
    """Running a settlement is allowed for SUPER_ADMIN and ADMIN only — each
    settles their own user pool. Brokers / sub-brokers are excluded."""
    if getattr(admin, "role", None) not in (UserRole.SUPER_ADMIN, UserRole.ADMIN):
        raise HTTPException(status_code=403, detail="Admin or super-admin only")


# ── Weekly settlement controls ───────────────────────────────────────
@router.post("/settings/weekly-settlement/run", response_model=APIResponse[dict])
async def trigger_weekly_settlement(admin: CurrentAdmin, force: bool = Query(True)):
    """Manually run (or resume) the weekly settlement batch for THIS actor's
    user pool. SUPER_ADMIN settles their own users; ADMIN settles only the
    users they own (incl. their broker subtree). Idempotent: re-running the
    same ISO week — or overlapping with the automatic Saturday global run —
    never double-books P&L (unique per-week, per-position record)."""
    _require_admin_or_super(admin)
    from app.core.dependencies import scoped_user_ids
    from app.services.weekly_settlement_service import run_weekly_settlement

    user_ids = await scoped_user_ids(admin)
    summary = await run_weekly_settlement(
        scope_admin_id=admin.id,
        scope_user_ids=user_ids,
        force=force,
    )
    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="WeeklySettlement",
        entity_id=summary.get("week_key", ""),
        actor_id=admin.id,
        new_values=summary,
    )
    return APIResponse(data=summary)


@router.put("/settings/weekly-settlement/enabled", response_model=APIResponse[dict])
async def set_weekly_settlement_enabled(payload: UpdatePlatformSettingRequest, admin: CurrentAdmin):
    """Turn the weekly settlement engine ON/OFF. Upserts the flag so it
    works even on a database that predates the seed default."""
    _require_super_admin(admin)
    from app.services.weekly_settlement_service import SETTLEMENT_ENABLED_KEY

    enabled = bool(payload.setting_value)
    row = await PlatformSetting.find_one(
        PlatformSetting.setting_key == SETTLEMENT_ENABLED_KEY
    )
    if row is None:
        row = PlatformSetting(
            setting_key=SETTLEMENT_ENABLED_KEY,
            setting_value=enabled,
            setting_type=SettingType.BOOL,
            category="trading",
            is_public=False,
            description="Weekly mark-to-market settlement (Saturday 00:00 IST).",
        )
        await row.insert()
    else:
        row.setting_value = enabled
        await row.save()
    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="PlatformSetting",
        entity_id=SETTLEMENT_ENABLED_KEY,
        actor_id=admin.id,
        new_values={"enabled": enabled},
    )
    return APIResponse(data={"enabled": enabled})


# ── Divinepay payment gateway (super-admin's own pool) ───────────────
@router.get("/settings/payment-gateway", response_model=APIResponse[dict])
async def get_payment_gateway(admin: CurrentAdmin):
    """Whether the Divinepay UPI gateway is ON for the super-admin's OWN pool
    (users with no assigned admin). Per-admin pools use each admin's own switch.
    Absent row ⇒ OFF (the platform ships manual)."""
    _require_super_admin(admin)
    from app.services.divinepay_service import SUPER_ADMIN_POOL_KEY, is_configured

    row = await PlatformSetting.find_one(
        PlatformSetting.setting_key == SUPER_ADMIN_POOL_KEY
    )
    raw = row.setting_value if row is not None else False
    enabled = raw is True or str(raw).strip().lower() in {"true", "1", "yes", "on"}
    return APIResponse(data={"enabled": enabled, "configured": is_configured()})


@router.put("/settings/payment-gateway", response_model=APIResponse[dict])
async def set_payment_gateway(payload: UpdatePlatformSettingRequest, admin: CurrentAdmin):
    """Turn the Divinepay gateway ON/OFF for the super-admin's own pool."""
    _require_super_admin(admin)
    from app.services.divinepay_service import SUPER_ADMIN_POOL_KEY

    enabled = bool(payload.setting_value)
    row = await PlatformSetting.find_one(
        PlatformSetting.setting_key == SUPER_ADMIN_POOL_KEY
    )
    if row is None:
        row = PlatformSetting(
            setting_key=SUPER_ADMIN_POOL_KEY,
            setting_value=enabled,
            setting_type=SettingType.BOOL,
            category="payment",
            is_public=False,
            description="Divinepay UPI gateway for the super-admin's own users.",
        )
        await row.insert()
    else:
        row.setting_value = enabled
        await row.save()
    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="PlatformSetting",
        entity_id=SUPER_ADMIN_POOL_KEY,
        actor_id=admin.id,
        new_values={"enabled": enabled},
    )
    return APIResponse(data={"enabled": enabled})


# ── Terms & Conditions (per-admin, shown to that admin's users) ──────
from pydantic import BaseModel  # noqa: E402


class _CarryTogglePayload(BaseModel):
    enabled: bool


@router.get("/settings/carry-forward-toggle", response_model=APIResponse[dict])
async def get_carry_toggle(admin: CurrentAdmin):
    """Whether this admin has enabled the per-position user Carry-Forward
    toggle for their pool. OFF ⇒ normal auto-carry at EOD."""
    return APIResponse(data={"enabled": bool(getattr(admin, "carry_forward_toggle_enabled", False))})


@router.put("/settings/carry-forward-toggle", response_model=APIResponse[dict])
async def set_carry_toggle(payload: _CarryTogglePayload, admin: CurrentAdmin):
    admin.carry_forward_toggle_enabled = bool(payload.enabled)
    await admin.save()
    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="User",
        entity_id=str(admin.id),
        actor_id=admin.id,
        new_values={"carry_forward_toggle_enabled": admin.carry_forward_toggle_enabled},
    )
    return APIResponse(
        data={"enabled": admin.carry_forward_toggle_enabled},
        message=(
            "Carry-Forward toggle enabled — your users now choose per position."
            if admin.carry_forward_toggle_enabled
            else "Carry-Forward toggle disabled — normal auto-carry runs."
        ),
    )


class _TermsPayload(BaseModel):
    terms: str


@router.get("/settings/terms", response_model=APIResponse[dict])
async def get_terms(admin: CurrentAdmin):
    """This admin's own Terms & Conditions text (edited here, shown to their
    users on the profile). Super-admin's copy is the platform default."""
    return APIResponse(data={"terms": getattr(admin, "terms_and_conditions", None) or ""})


@router.put("/settings/terms", response_model=APIResponse[dict])
async def set_terms(payload: _TermsPayload, admin: CurrentAdmin):
    admin.terms_and_conditions = (payload.terms or "").strip() or None
    await admin.save()
    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="User",
        entity_id=str(admin.id),
        actor_id=admin.id,
        new_values={"terms_chars": len(payload.terms or "")},
    )
    return APIResponse(
        data={"terms": admin.terms_and_conditions or ""},
        message="Terms & Conditions saved." if admin.terms_and_conditions else "Terms & Conditions cleared.",
    )


# ── Platform settings ────────────────────────────────────────────────
@router.get("/settings/platform", response_model=APIResponse[list])
async def list_platform_settings(admin: CurrentAdmin, category: str | None = None):
    q: dict[str, Any] = {}
    if category:
        q["category"] = category
    rows = await PlatformSetting.find(q).sort("category", "setting_key").to_list()
    return APIResponse(
        data=[
            {
                "key": r.setting_key,
                "value": r.setting_value,
                "type": r.setting_type.value,
                "description": r.description,
                "category": r.category,
                "is_public": r.is_public,
            }
            for r in rows
        ]
    )


@router.put("/settings/platform/{key:path}", response_model=APIResponse[dict])
async def update_platform_setting(key: str, payload: UpdatePlatformSettingRequest, admin: CurrentAdmin):
    s = await PlatformSetting.find_one(PlatformSetting.setting_key == key)
    if s is None:
        raise HTTPException(status_code=404, detail="Setting not found")
    s.setting_value = payload.setting_value
    await s.save()
    await log_event(
        action=AuditAction.SETTING_CHANGE,
        entity_type="PlatformSetting",
        entity_id=key,
        actor_id=admin.id,
        new_values={"value": payload.setting_value},
    )
    return APIResponse(data={"ok": True})


# ── Holidays ────────────────────────────────────────────────────────
@router.get("/holidays", response_model=APIResponse[list])
async def list_holidays(admin: CurrentAdmin, year: int | None = None):
    q: dict[str, Any] = {}
    if year:
        q["holiday_date"] = {"$gte": date(year, 1, 1), "$lte": date(year, 12, 31)}
    rows = await TradingHoliday.find(q).sort("holiday_date").to_list()
    return APIResponse(
        data=[
            {
                "id": str(h.id),
                "holiday_date": h.holiday_date.isoformat(),
                "exchange": str(h.exchange),
                "description": h.description,
                "is_full_day": h.is_full_day,
                "is_muhurat": h.is_muhurat,
            }
            for h in rows
        ]
    )


@router.post("/holidays", response_model=APIResponse[dict])
async def create_holiday(payload: dict, admin: CurrentAdmin):
    h = TradingHoliday(
        holiday_date=date.fromisoformat(payload["holiday_date"]),
        exchange=Exchange(payload.get("exchange", "NSE")),
        description=payload.get("description", "Holiday"),
        is_full_day=bool(payload.get("is_full_day", True)),
        is_muhurat=bool(payload.get("is_muhurat", False)),
    )
    await h.insert()
    return APIResponse(data={"id": str(h.id)})


@router.delete("/holidays/{holiday_id}", response_model=APIResponse[dict])
async def delete_holiday(holiday_id: str, admin: CurrentAdmin):
    h = await TradingHoliday.get(PydanticObjectId(holiday_id))
    if h is None:
        raise HTTPException(status_code=404, detail="Holiday not found")
    await h.delete()
    return APIResponse(data={"ok": True})


# ── Audit ───────────────────────────────────────────────────────────
async def _audit_scope_user_ids(admin) -> list[PydanticObjectId] | None:
    """Resolve which user_ids an admin can see audit events for.

    Returns `None` to mean "no filter" (super-admin sees everything).
    Otherwise returns the explicit list of in-scope user ids — events
    are matched if their `user_id` (actor) or `target_user_id`
    (subject) is in this list. Differs from `scoped_user_ids` in two
    important ways:

      • Includes broker-tier users in the admin's pool. The standard
        helper filters out SUPER_ADMIN / ADMIN / BROKER roles (it's
        designed for client-only scopes like deposits / withdrawals).
        For audit we want admins to see their downstream brokers'
        activity too.
      • Includes the admin themselves. An admin browsing their own
        audit page should see their own actions (logins, the very
        block / kyc / approve calls they fire).

    Admin / broker / sub-broker all go through the same logic — the
    underlying mongo filters (`assigned_admin_id` for admin,
    `broker_ancestry` for broker) walk the right subtree per role.
    """
    from app.core.dependencies import scoped_user_filter
    from app.models.user import User, UserRole

    if admin.role == UserRole.SUPER_ADMIN:
        return None
    ids: set[PydanticObjectId] = {admin.id}
    # Use the SAME comprehensive pool clause the /admin/users list + the audit
    # page's own user-search run through (`scoped_user_filter` → `_pool_clause`).
    # For ADMIN that unions the directly-assigned users AND the transferred-
    # broker subtree; the old flat `assigned_admin_id == admin.id` MISSED every
    # broker-subtree client. That mismatch was user-visible: the audit search
    # box (scoped by the list endpoint) offered a broker's client, but clicking
    # it filtered the feed by an id the audit scope excluded → empty feed
    # ("search me naam aata hai, click karo to record nahi dikhta").
    if admin.role in (UserRole.ADMIN, UserRole.BROKER):
        downstream = await User.find(await scoped_user_filter(admin)).to_list()
    else:
        downstream = []
    for u in downstream:
        if u.id is not None:
            ids.add(u.id)
    return list(ids)


@router.get("/audit/logs", response_model=APIResponse[dict])
async def list_audit(
    admin: CurrentAdmin,
    user_id: str | None = None,
    target_user_id: str | None = None,
    involving_user_id: str | None = None,
    action: str | None = None,
    actions: str | None = None,
    entity_type: str | None = None,
    entity_types: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, le=200),
):
    """Admin audit-log feed.

    Filter params (all optional, combine via AND):
      • user_id           — actor only
      • target_user_id    — subject only
      • involving_user_id — actor OR subject (used by per-user Activity)
      • action            — single action (back-compat with old UI)
      • actions           — CSV of actions; preset chips on the new
                            audit page (Edit Trade / Reopen / Deposit /
                            Withdrawal / etc.) pack 2-3 actions into
                            one chip and submit them all at once.
      • entity_type       — single entity_type
      • entity_types      — CSV of entity_types (used together with
                            `actions` by the preset chips so e.g.
                            "Deposit" chip narrows to
                            action in {APPROVE, REJECT} AND
                            entity_type = DepositRequest)
      • from_date / to_date — ISO timestamps; either bound is optional.
                              Inclusive lower, exclusive upper so a
                              day filter ("2026-05-21") naturally
                              spans 00:00..23:59:59.999.
    """
    from datetime import datetime as _dt

    q: dict[str, Any] = {}

    # ── Scope gate ──────────────────────────────────────────────────
    # Super-admin sees everything; admin sees events involving their
    # own pool (themselves + downstream brokers + clients); broker
    # sees their own subtree. Without this every admin who could hit
    # /admin/audit/logs would see the entire platform's audit trail.
    scope = await _audit_scope_user_ids(admin)
    scope_filter: dict[str, Any] | None = None
    if scope is not None:
        if not scope:
            return APIResponse(
                data={
                    "items": [],
                    "meta": {
                        "page": page,
                        "page_size": page_size,
                        "total": 0,
                        "total_pages": 0,
                    },
                }
            )
        scope_filter = {
            "$or": [
                {"user_id": {"$in": scope}},
                {"target_user_id": {"$in": scope}},
            ]
        }

    if user_id:
        q["user_id"] = PydanticObjectId(user_id)
    if target_user_id:
        q["target_user_id"] = PydanticObjectId(target_user_id)
    if involving_user_id:
        # Surface events where this user is EITHER the actor or the
        # subject — drives the user-detail "Activity" view, which used
        # to filter on target_user_id alone and miss every event the
        # user themselves initiated (logins, order placements, etc).
        oid = PydanticObjectId(involving_user_id)
        q["$or"] = [{"user_id": oid}, {"target_user_id": oid}]
    if action:
        q["action"] = action
    elif actions:
        # CSV of allowed actions — used by the preset filter chips
        # ("Edit Trade", "Close by Admin", "Reopen", "Deposit", etc.).
        # Trim + drop empties so a trailing comma doesn't poison the
        # $in list.
        action_list = [a.strip() for a in actions.split(",") if a.strip()]
        if action_list:
            q["action"] = {"$in": action_list}
    if entity_type:
        q["entity_type"] = entity_type
    elif entity_types:
        et_list = [e.strip() for e in entity_types.split(",") if e.strip()]
        if et_list:
            q["entity_type"] = {"$in": et_list}

    # Date range — `created_at` indexed already, so the bounded scan
    # uses the existing -created_at sort index efficiently. Both bounds
    # optional; accept ISO 8601 or `YYYY-MM-DD` (treated as IST 00:00
    # for the day filter shortcut on the preset chips).
    if from_date or to_date:
        date_q: dict[str, Any] = {}
        if from_date:
            try:
                date_q["$gte"] = _dt.fromisoformat(from_date.replace("Z", "+00:00"))
            except Exception:
                pass
        if to_date:
            try:
                date_q["$lte"] = _dt.fromisoformat(to_date.replace("Z", "+00:00"))
            except Exception:
                pass
        if date_q:
            q["created_at"] = date_q

    # Hide ALL super-admin activity from sub-admins / brokers. A sub-admin must
    # never see the super-admin impersonating them, creating their account, or
    # any other super-admin action in their own audit feed — only the super-
    # admin sees those. The scope filter above matches an event when the viewer
    # is its TARGET, so without this a super-admin action AIMED at the admin
    # (impersonate / sub-admin create) still leaked through.
    hide_super_filter: dict[str, Any] | None = None
    from app.models.user import User as _User, UserRole as _UserRole

    if admin.role != _UserRole.SUPER_ADMIN:
        sa_ids = [
            u.id
            for u in await _User.find(_User.role == _UserRole.SUPER_ADMIN).to_list()
            if u.id is not None
        ]
        if sa_ids:
            hide_super_filter = {"user_id": {"$nin": sa_ids}}

    # Combine scope + super-admin-hide + field filters via $and so each one
    # narrows the result (a single merged dict would let a later $or key
    # clobber an earlier one — e.g. scope $or vs involving_user_id $or).
    extra = [f for f in (scope_filter, hide_super_filter) if f is not None]
    if extra:
        if q:
            q = {"$and": [*extra, q]}
        elif len(extra) == 1:
            q = extra[0]
        else:
            q = {"$and": extra}

    total = await AuditLog.find(q).count()
    rows = (
        await AuditLog.find(q).sort("-created_at").skip((page - 1) * page_size).limit(page_size).to_list()
    )

    # Enrich actor / target user with readable name + user_code so the
    # admin's audit table doesn't show raw ObjectIds. Single batched
    # lookup — small N (page_size=50 at most) so this is one query.
    from app.models.user import User as _User

    referenced_ids: set[PydanticObjectId] = set()
    for r in rows:
        if r.user_id is not None:
            referenced_ids.add(r.user_id)
        if r.target_user_id is not None:
            referenced_ids.add(r.target_user_id)
    user_map: dict[str, dict[str, str | None]] = {}
    if referenced_ids:
        users = await _User.find(
            {"_id": {"$in": list(referenced_ids)}}
        ).to_list()
        user_map = {
            str(u.id): {
                "name": u.full_name,
                "code": u.user_code,
                "role": u.role.value if u.role else None,
            }
            for u in users
        }

    def _enrich(uid: PydanticObjectId | None) -> dict[str, Any] | None:
        if uid is None:
            return None
        info = user_map.get(str(uid))
        if info is None:
            return {"id": str(uid)}
        return {"id": str(uid), **info}

    # ── Backfill Position-entity rows from the live Position doc ──────
    # OLD audit rows (logged before symbol / close_price / size were
    # captured in metadata) showed "—" for those columns on the Admin
    # Actions → Edited Positions page. The Position document survives an
    # edit (only a delete removes it), so we batch-fetch the referenced
    # positions and fill the gaps. New rows already carry the fields, so
    # we only fill what's missing — never overwrite a real before→after.
    from app.models.position import Position as _Position

    pos_oids: list[PydanticObjectId] = []
    for r in rows:
        if r.entity_type == "Position" and r.entity_id:
            try:
                pos_oids.append(PydanticObjectId(r.entity_id))
            except Exception:
                pass
    pos_map: dict[str, Any] = {}
    if pos_oids:
        for p in await _Position.find({"_id": {"$in": pos_oids}}).to_list():
            pos_map[str(p.id)] = p

    def _pos_backfill(r) -> tuple[dict | None, dict | None, dict | None]:
        """Return (metadata, old_values, new_values) with Position fields
        filled in when the audit row didn't record them."""
        p = pos_map.get(str(r.entity_id)) if (r.entity_type == "Position" and r.entity_id) else None
        if p is None:
            return r.metadata, r.old_values, r.new_values
        md = dict(r.metadata) if r.metadata else {}
        ov = dict(r.old_values) if r.old_values else {}
        nv = dict(r.new_values) if r.new_values else {}
        if not md.get("symbol"):
            md["symbol"] = p.instrument.symbol
        if not md.get("trading_symbol"):
            md["trading_symbol"] = getattr(p.instrument, "trading_symbol", None) or p.instrument.symbol
        if not md.get("exchange"):
            md["exchange"] = str(p.instrument.exchange)
        if not md.get("segment"):
            md["segment"] = p.instrument.segment
        if not md.get("opened_side"):
            md["opened_side"] = str(getattr(p, "opened_side", None) or "")
        if md.get("opening_quantity") in (None, "", 0):
            md["opening_quantity"] = (
                p.opening_quantity if p.opening_quantity is not None else abs(p.quantity or 0)
            )
        # Close price: when the row never captured it, show the position's
        # current close as a static value (set BOTH sides equal so the UI
        # renders a single figure, not a misleading "— → ₹x" arrow).
        if not nv.get("close_price") and not ov.get("close_price"):
            cur_close = str(p.ltp) if p.ltp is not None else None
            if cur_close is not None:
                ov["close_price"] = cur_close
                nv["close_price"] = cur_close
        return md, ov, nv

    items: list[dict[str, Any]] = []
    for r in rows:
        md, ov, nv = _pos_backfill(r)
        items.append(
            {
                "id": str(r.id),
                "user_id": str(r.user_id) if r.user_id else None,
                "target_user_id": str(r.target_user_id) if r.target_user_id else None,
                "actor": _enrich(r.user_id),
                "target": _enrich(r.target_user_id),
                "action": r.action.value,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "old_values": ov,
                "new_values": nv,
                "metadata": md,
                "ip_address": r.ip_address,
                "user_agent": r.user_agent,
                "created_at": r.created_at,
            }
        )

    return APIResponse(
        data={
            "items": items,
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size,
            },
        }
    )


# ── Backup / EOD ────────────────────────────────────────────────────
@router.get("/backup/list", response_model=APIResponse[list])
async def list_backups(admin: CurrentAdmin):
    # Phase 7 ships actual S3-backed backups; for now we return audit-log entries marked as BACKUP
    rows = await AuditLog.find(AuditLog.action == AuditAction.BACKUP).sort("-created_at").limit(50).to_list()
    return APIResponse(
        data=[
            {
                "id": str(r.id),
                "created_at": r.created_at,
                "metadata": r.metadata,
                "actor_id": str(r.user_id) if r.user_id else None,
            }
            for r in rows
        ]
    )


@router.post("/backup/run", response_model=APIResponse[dict])
async def run_backup(admin: CurrentAdmin):
    """Stub — records a backup audit event. Phase 7 wires actual S3 dump."""
    await log_event(
        action=AuditAction.BACKUP,
        entity_type="System",
        entity_id="manual",
        actor_id=admin.id,
        metadata={"trigger": "manual", "ts": datetime.utcnow().isoformat()},
    )
    return APIResponse(data={"ok": True, "queued_at": datetime.utcnow().isoformat()})


@router.post("/backup/eod-reset", response_model=APIResponse[dict])
async def eod_reset(admin: CurrentAdmin):
    """Stub — Phase 7 wires real EOD: squareoff MIS, settle, update holdings, clear day counters.
    For now records the audit event."""
    await log_event(
        action=AuditAction.EOD_RESET,
        entity_type="System",
        entity_id="eod",
        actor_id=admin.id,
        metadata={"ts": datetime.utcnow().isoformat()},
    )
    return APIResponse(data={"ok": True})
