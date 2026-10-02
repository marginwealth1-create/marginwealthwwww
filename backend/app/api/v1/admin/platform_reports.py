"""Super-admin platform reports — cross-pool order execution + login activity.

Aggregates the WHOLE platform (every admin pool) into:
  • executed-order counts       (today / this week / last week)
  • distinct client logins       (today / this week / last week)
  • per-admin breakdown: orders executed today + active clients
    (today / this week / last week)

SUPER_ADMIN only. Time buckets are IST calendar days (today = IST midnight →
now, week = Monday 00:00 IST → now, last week = the Monday before that).
Login activity comes from the LOGIN audit trail (distinct user_id per window);
order execution from Order.executed_at.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter

from app.core.dependencies import SuperAdmin
from app.models.audit_log import AuditAction, AuditLog
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole, UserStatus
from app.schemas.common import APIResponse
from app.utils.time_utils import now_ist, start_of_day_ist

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform-reports", tags=["admin-platform-reports"])

_SUPER = "__super__"


@router.get("", response_model=APIResponse[dict])
async def platform_reports(admin: SuperAdmin):
    today = now_ist().date()
    monday = today - timedelta(days=today.weekday())
    today_start = start_of_day_ist(today)
    week_start = start_of_day_ist(monday)
    last_week_start = start_of_day_ist(monday - timedelta(days=7))

    # ── Executed orders, platform-wide ──────────────────────────────
    ex = {"status": OrderStatus.EXECUTED.value}
    orders_today = await Order.find({**ex, "executed_at": {"$gte": today_start}}).count()
    orders_week = await Order.find({**ex, "executed_at": {"$gte": week_start}}).count()
    orders_last_week = await Order.find(
        {**ex, "executed_at": {"$gte": last_week_start, "$lt": week_start}}
    ).count()

    # Orders executed TODAY grouped by user — feeds the per-admin column.
    order_rows = await Order.aggregate(
        [
            {"$match": {**ex, "executed_at": {"$gte": today_start}}},
            {"$group": {"_id": "$user_id", "n": {"$sum": 1}}},
        ]
    ).to_list()
    orders_today_by_user = {r["_id"]: r["n"] for r in order_rows if r.get("_id")}

    # ── Distinct client logins per window (LOGIN audit trail) ────────
    coll = AuditLog.get_motor_collection()

    async def _login_ids(gte, lt=None) -> set:
        rng: dict = {"$gte": gte}
        if lt is not None:
            rng["$lt"] = lt
        ids = await coll.distinct(
            "user_id", {"action": AuditAction.LOGIN.value, "created_at": rng}
        )
        return {i for i in ids if i is not None}

    login_today = await _login_ids(today_start)
    login_week = await _login_ids(week_start)
    login_last_week = await _login_ids(last_week_start, week_start)

    # ── Map involved users → their admin (real clients only) ─────────
    involved = set(orders_today_by_user) | login_today | login_week | login_last_week
    client_admin: dict = {}
    if involved:
        users = await User.find({"_id": {"$in": list(involved)}}).to_list()
        for u in users:
            if u.role == UserRole.CLIENT and not getattr(u, "is_demo", False):
                client_admin[u.id] = u.assigned_admin_id  # None ⇒ super-admin's own pool

    buckets: dict = defaultdict(
        lambda: {"orders_today": 0, "active_today": 0, "active_week": 0, "active_last_week": 0}
    )
    for uid, admin_id in client_admin.items():
        b = buckets[str(admin_id) if admin_id else _SUPER]
        b["orders_today"] += orders_today_by_user.get(uid, 0)
        if uid in login_today:
            b["active_today"] += 1
        if uid in login_week:
            b["active_week"] += 1
        if uid in login_last_week:
            b["active_last_week"] += 1

    # ── All admins + the super-admin's direct pool row ──────────────
    admins = await User.find(
        {"role": UserRole.ADMIN.value, "status": {"$ne": UserStatus.CLOSED.value}}
    ).to_list()
    rows = []
    for a in admins:
        b = buckets.get(str(a.id), {})
        rows.append(
            {
                "admin_id": str(a.id),
                "admin_name": a.full_name,
                "admin_code": a.user_code,
                "orders_today": b.get("orders_today", 0),
                "active_today": b.get("active_today", 0),
                "active_week": b.get("active_week", 0),
                "active_last_week": b.get("active_last_week", 0),
            }
        )
    sp = buckets.get(_SUPER)
    if sp and any(sp.values()):
        rows.append(
            {
                "admin_id": None,
                "admin_name": "Platform (direct)",
                "admin_code": "—",
                **{k: sp.get(k, 0) for k in ("orders_today", "active_today", "active_week", "active_last_week")},
            }
        )
    rows.sort(key=lambda r: (r["active_today"], r["orders_today"]), reverse=True)

    # Platform login totals count real clients only (client_admin is client-scoped).
    clients = set(client_admin)
    return APIResponse(
        data={
            "orders": {
                "today": orders_today,
                "this_week": orders_week,
                "last_week": orders_last_week,
            },
            "logins": {
                "today": len(login_today & clients),
                "this_week": len(login_week & clients),
                "last_week": len(login_last_week & clients),
            },
            "admins": rows,
        }
    )
