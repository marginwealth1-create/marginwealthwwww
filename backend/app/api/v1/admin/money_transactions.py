"""Admin "Money Transactions" report — user-wise + broker-wise money movement.

Two read-only aggregations over the immutable `wallet_transactions` ledger,
date-filtered (preset or explicit IST dates), scoped to the caller's pool:

  GET /admin/money-transactions/users    → per-user money movement
  GET /admin/money-transactions/brokers  → per-broker deposit roll-up (tree)

Money is Decimal128 in the ledger; we convert to float ONLY here at the API
boundary (these are read-only report numbers, never wallet mutations).
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, Query

from app.core.dependencies import CurrentAdmin, require_perm, scoped_user_ids
from app.models.transaction import WalletTransaction
from app.models.user import User, UserRole
from app.schemas.common import APIResponse

router = APIRouter(prefix="/money-transactions", tags=["admin-money-transactions"])

IST = ZoneInfo("Asia/Kolkata")

# Only money-movement rows count — trade / brokerage / charges / pnl are NOT
# money in/out of the wallet from the user's view and live on the ledger page.
_MONEY_TYPES = ["DEPOSIT", "WITHDRAWAL", "ADJUSTMENT", "SETTLEMENT_OUTSTANDING_BOOKED"]


def _parse_date(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _ist_start_utc(d) -> datetime:
    return datetime.combine(d, dtime.min, tzinfo=IST).astimezone(timezone.utc)


def _ist_end_utc(d) -> datetime:
    # inclusive to end-of-day in IST
    return datetime.combine(d, dtime.max, tzinfo=IST).astimezone(timezone.utc)


def _resolve_window(
    preset: str | None, from_date: str | None, to_date: str | None
) -> tuple[datetime | None, datetime | None, str]:
    """Preset wins when set, else explicit dates. Returns (from_utc, to_utc,
    label). None bounds = no limit ("All time")."""
    now = datetime.now(IST)
    today = now.date()
    p = (preset or "").strip().lower().replace("-", "_").replace(" ", "_")

    if p in ("all", "all_time"):
        return None, None, "All time"
    if p == "this_week":
        mon = today - timedelta(days=today.weekday())
        return _ist_start_utc(mon), _ist_end_utc(today), "This week"
    if p == "last_week":
        mon = today - timedelta(days=today.weekday() + 7)
        return _ist_start_utc(mon), _ist_end_utc(mon + timedelta(days=6)), "Last week"
    if p == "this_month":
        return _ist_start_utc(today.replace(day=1)), _ist_end_utc(today), "This month"
    if p == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return _ist_start_utc(last_prev.replace(day=1)), _ist_end_utc(last_prev), "Last month"

    fd = _parse_date(from_date)
    td = _parse_date(to_date)
    if fd or td:
        lbl = f"{fd.isoformat() if fd else '…'} → {td.isoformat() if td else '…'}"
        return (_ist_start_utc(fd) if fd else None), (_ist_end_utc(td) if td else None), lbl
    return None, None, "All time"


def _date_match(from_utc: datetime | None, to_utc: datetime | None) -> dict:
    if not from_utc and not to_utc:
        return {}
    rng: dict[str, Any] = {}
    if from_utc:
        rng["$gte"] = from_utc
    if to_utc:
        rng["$lte"] = to_utc
    return {"created_at": rng}


def _abs(x: float) -> float:
    return abs(round(float(x or 0.0), 2))


def _pos(x: float) -> float:
    return round(float(x or 0.0), 2)


def _empty_user_totals() -> dict[str, float]:
    return {k: 0.0 for k in ("deposit", "add_fund", "withdraw", "deduct", "settled", "total_in", "total_out", "net")}


# ── USERS view ────────────────────────────────────────────────────────────
@router.get("/users", response_model=APIResponse[dict])
async def money_by_user(
    admin: CurrentAdmin,
    preset: str | None = Query(default=None),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    _: None = Depends(require_perm("ledger", "read")),
):
    from_utc, to_utc, label = _resolve_window(preset, from_date, to_date)
    # include_closed=True — a deleted user's cash in/out is a financial record
    # that must stay visible here even after the user is soft-deleted.
    scoped = await scoped_user_ids(admin, include_closed=True)
    if not scoped:
        return APIResponse(data={"totals": _empty_user_totals(), "users": [], "filter": {"label": label}})

    match: dict[str, Any] = {
        "user_id": {"$in": scoped},
        "transaction_type": {"$in": _MONEY_TYPES},
        "status": "COMPLETED",
        **_date_match(from_utc, to_utc),
    }
    amt = {"$toDouble": "$amount"}
    # System-generated ADJUSTMENT corrections to exclude from add_fund /
    # deduct. ADJUSTMENT is overloaded: operator-driven Add/Deduct
    # Fund + a few automatic recovery entries (initial-balance credit
    # on user creation, "Bogus 0-price fill reversal" credits the
    # recovery script writes when undoing a Zerodha-flatline trade).
    # The corrections are NOT real cash flow, so excluding them from
    # the tile aggregations brings the per-week number back to operator
    # expectation. Operator hit this on CL62329114 — two ₹8,63,120
    # reversal rows inflated the week's Add Fund by ~₹17 lakh.
    _SYSTEM_NARRATION_RX = "Initial balance credit|Bogus 0-price fill reversal|Reversal trade="
    is_system_adj = {
        "$regexMatch": {
            "input": {"$ifNull": ["$narration", ""]},
            "regex": _SYSTEM_NARRATION_RX,
            "options": "i",
        }
    }
    is_operator_adj = {
        "$and": [
            {"$eq": ["$transaction_type", "ADJUSTMENT"]},
            {"$not": is_system_adj},
        ]
    }
    pipeline = [
        {"$match": match},
        {
            "$group": {
                "_id": "$user_id",
                "deposit": {"$sum": {"$cond": [{"$eq": ["$transaction_type", "DEPOSIT"]}, amt, 0]}},
                "withdraw": {"$sum": {"$cond": [{"$eq": ["$transaction_type", "WITHDRAWAL"]}, amt, 0]}},
                "add_fund": {"$sum": {"$cond": [{"$and": [is_operator_adj, {"$gt": [amt, 0]}]}, amt, 0]}},
                "deduct": {"$sum": {"$cond": [{"$and": [is_operator_adj, {"$lt": [amt, 0]}]}, amt, 0]}},
                "settled": {"$sum": {"$cond": [{"$eq": ["$transaction_type", "SETTLEMENT_OUTSTANDING_BOOKED"]}, amt, 0]}},
            }
        },
    ]

    raw: dict[str, dict[str, float]] = {}
    async for row in WalletTransaction.get_motor_collection().aggregate(pipeline):
        raw[str(row["_id"])] = row

    if not raw:
        return APIResponse(data={"totals": _empty_user_totals(), "users": [], "filter": {"label": label}})

    # Load only the user docs we actually need + resolve owner (broker/admin).
    uoids = [PydanticObjectId(uid) for uid in raw]
    udocs = {str(u.id): u for u in await User.find({"_id": {"$in": uoids}}).to_list()}
    broker_ids = {u.assigned_broker_id for u in udocs.values() if u.assigned_broker_id}
    admin_ids = {u.assigned_admin_id for u in udocs.values() if u.assigned_admin_id}
    owner_ids = list(broker_ids | admin_ids)
    owners = {str(o.id): o for o in await User.find({"_id": {"$in": owner_ids}}).to_list()} if owner_ids else {}

    def _owner(u: User) -> tuple[str, str]:
        if u.assigned_broker_id and str(u.assigned_broker_id) in owners:
            b = owners[str(u.assigned_broker_id)]
            kind = "Sub-broker" if (b.broker_ancestry or []) else "Broker"
            return kind, (b.full_name or b.user_code or "—")
        if u.assigned_admin_id and str(u.assigned_admin_id) in owners:
            a = owners[str(u.assigned_admin_id)]
            return "Admin", (a.full_name or a.user_code or "—")
        return "Direct", ""

    users: list[dict[str, Any]] = []
    tot = _empty_user_totals()
    for uid, g in raw.items():
        deposit = _pos(g.get("deposit", 0))
        add_fund = _pos(g.get("add_fund", 0))
        withdraw = _abs(g.get("withdraw", 0))
        deduct = _abs(g.get("deduct", 0))
        settled = _abs(g.get("settled", 0))
        total_in = round(deposit + add_fund, 2)
        total_out = round(withdraw + deduct, 2)
        # Drop phantom rows — nothing actually moved this window.
        if total_in == 0 and total_out == 0 and settled == 0:
            continue
        u = udocs.get(uid)
        owner_kind, owner_name = _owner(u) if u else ("Direct", "")
        users.append({
            "user_id": uid,
            "user_code": u.user_code if u else uid[-6:],
            "full_name": (u.full_name if u else None) or "—",
            "owner_kind": owner_kind,
            "owner_name": owner_name,
            "deposit": deposit,
            "add_fund": add_fund,
            "withdraw": withdraw,
            "deduct": deduct,
            "settled": settled,
            "total_in": total_in,
            "total_out": total_out,
            "net": round(total_in - total_out, 2),
        })
        tot["deposit"] = round(tot["deposit"] + deposit, 2)
        tot["add_fund"] = round(tot["add_fund"] + add_fund, 2)
        tot["withdraw"] = round(tot["withdraw"] + withdraw, 2)
        tot["deduct"] = round(tot["deduct"] + deduct, 2)
        tot["settled"] = round(tot["settled"] + settled, 2)
        tot["total_in"] = round(tot["total_in"] + total_in, 2)
        tot["total_out"] = round(tot["total_out"] + total_out, 2)
    tot["net"] = round(tot["total_in"] - tot["total_out"], 2)

    users.sort(key=lambda r: r["total_in"], reverse=True)
    return APIResponse(data={"totals": tot, "users": users, "filter": {"label": label}})


# ── BROKERS view ──────────────────────────────────────────────────────────
@router.get("/brokers", response_model=APIResponse[dict])
async def money_by_broker(
    admin: CurrentAdmin,
    preset: str | None = Query(default=None),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    _: None = Depends(require_perm("ledger", "read")),
):
    from_utc, to_utc, label = _resolve_window(preset, from_date, to_date)
    # include_closed=True — a deleted user's cash in/out is a financial record
    # that must stay visible here even after the user is soft-deleted.
    scoped = await scoped_user_ids(admin, include_closed=True)
    empty = {
        "totals": {"total_deposit": 0.0, "admin_add_fund": 0.0, "brokers": 0, "users_under_brokers": 0},
        "brokers": [],
        "filter": {"label": label},
    }
    if not scoped:
        return APIResponse(data=empty)

    # Minimal client → broker map for every scoped client (dict projection).
    clients = [
        c
        async for c in User.get_motor_collection().find(
            {"_id": {"$in": scoped}},
            {"assigned_broker_id": 1, "broker_ancestry": 1},
        )
    ]

    # Deposit (+ add-fund total) per client over the window.
    dmatch: dict[str, Any] = {
        "user_id": {"$in": scoped},
        "transaction_type": {"$in": ["DEPOSIT", "ADJUSTMENT"]},
        "status": "COMPLETED",
        **_date_match(from_utc, to_utc),
    }
    amt = {"$toDouble": "$amount"}
    # Same system-correction filter as the per-user aggregation above —
    # excludes initial-balance credits, bogus-fill reversal credits,
    # and any future "Reversal trade=" recovery entry. Keeps admin
    # Add Fund totals honest.
    _is_system_adj_byb = {
        "$regexMatch": {
            "input": {"$ifNull": ["$narration", ""]},
            "regex": "Initial balance credit|Bogus 0-price fill reversal|Reversal trade=",
            "options": "i",
        }
    }
    _is_operator_adj_byb = {
        "$and": [
            {"$eq": ["$transaction_type", "ADJUSTMENT"]},
            {"$not": _is_system_adj_byb},
        ]
    }
    dep_by_client: dict[str, float] = {}
    add_fund_total = 0.0
    total_deposit = 0.0
    async for row in WalletTransaction.get_motor_collection().aggregate([
        {"$match": dmatch},
        {"$group": {
            "_id": "$user_id",
            "deposit": {"$sum": {"$cond": [{"$eq": ["$transaction_type", "DEPOSIT"]}, amt, 0]}},
            "add_fund": {"$sum": {"$cond": [{"$and": [_is_operator_adj_byb, {"$gt": [amt, 0]}]}, amt, 0]}},
        }},
    ]):
        dep = _pos(row.get("deposit", 0))
        dep_by_client[str(row["_id"])] = dep
        total_deposit = round(total_deposit + dep, 2)
        add_fund_total = round(add_fund_total + _pos(row.get("add_fund", 0)), 2)

    # Brokers referenced by the in-scope clients (ancestry / direct).
    broker_id_set: set[PydanticObjectId] = set()
    users_under_brokers = 0
    for c in clients:
        anc = c.get("broker_ancestry") or []
        abid = c.get("assigned_broker_id")
        if anc or abid:
            users_under_brokers += 1
        for b in anc:
            broker_id_set.add(b)
        if abid:
            broker_id_set.add(abid)

    brokers_out: list[dict[str, Any]] = []
    if broker_id_set:
        bdocs = await User.find(
            {"_id": {"$in": list(broker_id_set)}, "role": UserRole.BROKER.value}
        ).to_list()
        for b in bdocs:
            subtree_dep = 0.0
            direct_dep = 0.0
            subtree_users = 0
            for c in clients:
                cid = str(c["_id"])
                if b.id in (c.get("broker_ancestry") or []):
                    subtree_users += 1
                    subtree_dep = round(subtree_dep + dep_by_client.get(cid, 0.0), 2)
                if c.get("assigned_broker_id") == b.id:
                    direct_dep = round(direct_dep + dep_by_client.get(cid, 0.0), 2)
            parent = b.broker_ancestry or []
            brokers_out.append({
                "broker_id": str(b.id),
                "user_code": b.user_code,
                "full_name": b.full_name or b.user_code,
                "is_sub": bool(parent),
                "parent_broker_id": str(parent[-1]) if parent else None,
                "deposit": subtree_dep,
                "deposit_direct": direct_dep,
                "users_count": subtree_users,
            })
        brokers_out.sort(key=lambda r: r["deposit"], reverse=True)

    return APIResponse(data={
        "totals": {
            "total_deposit": total_deposit,
            "admin_add_fund": add_fund_total,
            "brokers": len(brokers_out),
            "users_under_brokers": users_under_brokers,
        },
        "brokers": brokers_out,
        "filter": {"label": label},
    })
