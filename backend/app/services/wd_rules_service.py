"""Deposit / Withdrawal rule resolver — tier-aware cascade.

The codebase's segment-settings resolver (`netting_service.get_effective_settings`)
already proves the tier-cascade pattern works for admin-configurable per-pool
rules. This module reuses that exact shape for the deposit/withdrawal rule
tower:

    User's BrokerWdRule (broker pool)
  → User's SubAdminWdRule (admin pool)
  → SuperAdminWdRule (super-admin pool)
  → WdRule (platform global default)

Each override row is SPARSE — every field is Optional. None means "inherit
from the tier below". The merger walks the chain top-down (broker first)
and fills any field still missing from the next layer. That way:

  * Each admin/broker is free to set whatever they want for their own
    users (operator's spec: "admin apne user ke liye, broker apne user ke
    liye").
  * They DON'T have to fill every field — the global WdRule provides the
    safe fallback.
  * Adding a new tier (e.g. UserWdRule) later is just one more `_pick`
    call at the top of the chain.

The user-side wallet endpoints call `get_effective_rule(user_id, rule_type)`
before persisting a deposit / withdrawal request and reject anything that
violates the resolved rule. The admin UI calls `get_effective_rule_for_admin`
to show the caller's own tier values (with global defaults filled in for
clarity, but flagged so admin sees which were inherited).
"""

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId
from bson import Decimal128

from app.models.transaction import (
    BrokerWdRule,
    SubAdminWdRule,
    SuperAdminWdRule,
    WdRule,
    WdRuleType,
)
from app.models.user import User

logger = logging.getLogger(__name__)


# Field list — driven by the override base. Keeping this here (instead of
# introspecting the model) so the resolver short-circuits on a stable
# explicit list, even if someone adds an unrelated field to the override
# class later.
_RULE_FIELDS = (
    "min_amount",
    "max_amount",
    "daily_limit",
    "allowed_days",
    "allowed_times",
    "charges_flat",
    "charges_percent",
    "auto_approve_under",
    "mandatory_remark",
    "block_withdrawal_with_open_positions",
    "block_duplicate_pending",
    "max_requests_per_day",
    "require_bank_details",
    "allow_upi_payout",
)


def _is_none(v: Any) -> bool:
    """True when an override field should be treated as "not set" — covers
    Python None AND the empty-list sentinel some admins choose to send."""
    if v is None:
        return True
    if isinstance(v, list) and not v:
        return True
    return False


def _dump(row: Any) -> dict[str, Any]:
    """Pull just the rule fields off a model instance into a flat dict.
    Skips the bookkeeping fields (id/created_at/owner ids etc)."""
    out: dict[str, Any] = {}
    for f in _RULE_FIELDS:
        out[f] = getattr(row, f, None)
    return out


def _merge_layer(target: dict[str, Any], layer: dict[str, Any]) -> None:
    """Fill `target` with values from `layer` for any field still missing.
    First non-None wins, so call this in descending priority order."""
    for f in _RULE_FIELDS:
        if _is_none(target.get(f)) and not _is_none(layer.get(f)):
            target[f] = layer[f]


async def _ensure_global_default(rule_type: str) -> WdRule:
    """Make sure a platform-global WdRule row exists for `rule_type`. The
    seed boots a row each on first startup, but in case it doesn't (or
    someone deleted it post-deploy), create it with the model defaults so
    the resolver always has SOMETHING to fall back on."""
    row = await WdRule.find_one(WdRule.rule_type == rule_type)
    if row is None:
        row = WdRule(rule_type=rule_type)
        try:
            await row.insert()
        except Exception:  # pragma: no cover — race on first boot
            row = await WdRule.find_one(WdRule.rule_type == rule_type)
            if row is None:
                raise
    return row


async def _resolve_super_admin_id() -> PydanticObjectId | None:
    """Single super-admin in the system (per the codebase's invariant —
    see netting_service._resolve_super_admin_id for the same pattern).
    Returns None on the rare empty-DB / pre-seed case."""
    sa = await User.find_one(User.role == "SUPER_ADMIN")
    return sa.id if sa is not None else None


async def get_effective_rule(
    user_id: str | PydanticObjectId,
    rule_type: str,
) -> dict[str, Any]:
    """Compute the effective deposit/withdrawal rule for a single user.

    Cascade order (first non-None wins per field):
        BrokerWdRule  →  SubAdminWdRule  →  SuperAdminWdRule  →  WdRule

    Returns a flat dict with every rule field populated — call sites
    don't need to know which layer each value came from. For UI / audit
    where you DO want that, use `get_effective_rule_with_sources`.
    """
    uid = PydanticObjectId(str(user_id))
    user_doc = await User.get(uid)

    effective: dict[str, Any] = {f: None for f in _RULE_FIELDS}

    # 1) Broker pool — most specific tier that applies to ANY end user.
    if user_doc is not None:
        broker_anc = user_doc.broker_ancestry or []
        if broker_anc:
            broker_id = broker_anc[-1]
            br = await BrokerWdRule.find_one(
                BrokerWdRule.broker_id == broker_id,
                BrokerWdRule.rule_type == rule_type,
            )
            if br is not None:
                _merge_layer(effective, _dump(br))

    # 2) Sub-admin pool — the user's assigned admin.
    if user_doc is not None and user_doc.assigned_admin_id is not None:
        sa = await SubAdminWdRule.find_one(
            SubAdminWdRule.sub_admin_id == user_doc.assigned_admin_id,
            SubAdminWdRule.rule_type == rule_type,
        )
        if sa is not None:
            _merge_layer(effective, _dump(sa))

    # 3) Super-admin pool — fallback for the org's super-admin.
    super_admin_id = await _resolve_super_admin_id()
    if super_admin_id is not None:
        sup = await SuperAdminWdRule.find_one(
            SuperAdminWdRule.super_admin_id == super_admin_id,
            SuperAdminWdRule.rule_type == rule_type,
        )
        if sup is not None:
            _merge_layer(effective, _dump(sup))

    # 4) Platform global — guaranteed populated; sets every remaining None.
    glob = await _ensure_global_default(rule_type)
    _merge_layer(effective, _dump(glob))

    return effective


async def get_effective_rule_with_sources(
    user_id: str | PydanticObjectId,
    rule_type: str,
) -> dict[str, Any]:
    """Same as `get_effective_rule` but ALSO returns which tier each
    field came from. Useful for the admin UI to render "this value came
    from your super-admin's pool" hints — not exposed to users.

    Result shape:
        {
            "rule_type": "WITHDRAWAL",
            "values":  {"min_amount": "100", "max_amount": "1000000", ...},
            "sources": {"min_amount": "broker", "max_amount": "global", ...},
        }
    """
    uid = PydanticObjectId(str(user_id))
    user_doc = await User.get(uid)

    values: dict[str, Any] = {f: None for f in _RULE_FIELDS}
    sources: dict[str, str] = {f: "" for f in _RULE_FIELDS}

    async def _apply(tier_name: str, row: Any) -> None:
        if row is None:
            return
        layer = _dump(row)
        for f in _RULE_FIELDS:
            if _is_none(values[f]) and not _is_none(layer.get(f)):
                values[f] = layer[f]
                sources[f] = tier_name

    if user_doc is not None:
        broker_anc = user_doc.broker_ancestry or []
        if broker_anc:
            br = await BrokerWdRule.find_one(
                BrokerWdRule.broker_id == broker_anc[-1],
                BrokerWdRule.rule_type == rule_type,
            )
            await _apply("broker", br)
        if user_doc.assigned_admin_id is not None:
            sa = await SubAdminWdRule.find_one(
                SubAdminWdRule.sub_admin_id == user_doc.assigned_admin_id,
                SubAdminWdRule.rule_type == rule_type,
            )
            await _apply("admin", sa)

    super_admin_id = await _resolve_super_admin_id()
    if super_admin_id is not None:
        sup = await SuperAdminWdRule.find_one(
            SuperAdminWdRule.super_admin_id == super_admin_id,
            SuperAdminWdRule.rule_type == rule_type,
        )
        await _apply("super_admin", sup)

    glob = await _ensure_global_default(rule_type)
    await _apply("global", glob)

    return {"rule_type": rule_type, "values": values, "sources": sources}


# ── Owner-tier helpers — used by the admin UI to read / write the
# caller's OWN tier override, regardless of which tier they sit at. The
# admin endpoint stays a single PUT route that figures out the tier
# automatically based on the caller's role + ownership ids.


def _to_money(v: Any) -> Decimal128 | None:
    if v is None or v == "":
        return None
    try:
        return Decimal128(str(v))
    except Exception:
        return None


def _coerce_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise a partial-update payload into the model's expected types.
    Returns ONLY the keys the caller provided — preserves the "None means
    inherit" semantics for fields the caller deliberately cleared."""
    out: dict[str, Any] = {}
    for f in _RULE_FIELDS:
        if f not in payload:
            continue
        v = payload[f]
        if f in ("min_amount", "max_amount", "daily_limit", "charges_flat", "auto_approve_under"):
            out[f] = _to_money(v)
        elif f == "charges_percent":
            try:
                out[f] = float(v) if v is not None else None
            except (TypeError, ValueError):
                out[f] = None
        elif f in (
            "mandatory_remark",
            "block_withdrawal_with_open_positions",
            "block_duplicate_pending",
            "require_bank_details",
            "allow_upi_payout",
        ):
            out[f] = bool(v) if v is not None else None
        elif f == "max_requests_per_day":
            # Non-negative int; None = inherit, 0 = no cap. Anything
            # un-parseable is treated as "inherit" (defensive).
            if v is None:
                out[f] = None
            else:
                try:
                    out[f] = max(0, int(v))
                except (TypeError, ValueError):
                    out[f] = None
        elif f == "allowed_days":
            if v is None:
                out[f] = None
            else:
                # Accept ints OR strings; clamp to 0..6; dedupe + sort so
                # downstream comparisons stay stable.
                try:
                    days = sorted({int(d) for d in v if 0 <= int(d) <= 6})
                    out[f] = days
                except (TypeError, ValueError):
                    out[f] = None
        elif f == "allowed_times":
            if v is None:
                out[f] = None
            else:
                # Expect list[{"start": "HH:MM", "end": "HH:MM"}]
                from app.models.transaction import AllowedTimeWindow

                cleaned: list[AllowedTimeWindow] = []
                for w in v or []:
                    s = (w or {}).get("start")
                    e = (w or {}).get("end")
                    if s and e:
                        cleaned.append(AllowedTimeWindow(start=str(s), end=str(e)))
                out[f] = cleaned or None
    return out


async def upsert_for_tier(
    *,
    rule_type: str,
    tier: str,  # "global" | "super_admin" | "admin" | "broker"
    owner_id: PydanticObjectId | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Single entry point for "save this admin's rule patch". Picks the
    right collection based on tier and upserts the partial-fields-only
    document, leaving unset fields as None (= inherit).

    `owner_id` is required for super_admin / admin / broker tiers; it's
    ignored for "global". Caller validates that the JWT actor has
    permission to write to the requested tier (super-admin to all
    tiers; admin only to their own admin row; broker only to their own
    broker row).
    """
    coerced = _coerce_payload(payload)

    if tier == "global":
        row = await WdRule.find_one(WdRule.rule_type == rule_type)
        if row is None:
            row = WdRule(rule_type=rule_type)
        # Global cannot have any field be None — fall back to the model's
        # default for any None the caller sent (vs override-tier rows
        # where None means "inherit"). The defaults match the model spec.
        for f, v in coerced.items():
            if v is None:
                continue
            setattr(row, f, v)
        await row.save()
        return _dump(row)

    if owner_id is None:
        raise ValueError("owner_id is required for non-global tiers")

    if tier == "super_admin":
        existing = await SuperAdminWdRule.find_one(
            SuperAdminWdRule.super_admin_id == owner_id,
            SuperAdminWdRule.rule_type == rule_type,
        )
        if existing is None:
            existing = SuperAdminWdRule(super_admin_id=owner_id, rule_type=rule_type)
    elif tier == "admin":
        existing = await SubAdminWdRule.find_one(
            SubAdminWdRule.sub_admin_id == owner_id,
            SubAdminWdRule.rule_type == rule_type,
        )
        if existing is None:
            existing = SubAdminWdRule(sub_admin_id=owner_id, rule_type=rule_type)
    elif tier == "broker":
        existing = await BrokerWdRule.find_one(
            BrokerWdRule.broker_id == owner_id,
            BrokerWdRule.rule_type == rule_type,
        )
        if existing is None:
            existing = BrokerWdRule(broker_id=owner_id, rule_type=rule_type)
    else:
        raise ValueError(f"Unknown tier: {tier}")

    # Apply the partial update — any field NOT in the payload keeps its
    # current value, and a None in the payload explicitly CLEARS the
    # override (so the field starts inheriting from the tier below).
    for f, v in coerced.items():
        setattr(existing, f, v)
    await existing.save()
    return _dump(existing)


__all__ = [
    "get_effective_rule",
    "get_effective_rule_with_sources",
    "upsert_for_tier",
    "validate_request",
    "WdRuleType",
]


# ── Validation — called from user-side create endpoints ─────────────


_WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _decimal_or_zero(v: Any) -> float:
    """Decimal128 / Decimal / str / number → float. Money fields in our
    schema are Decimal128, but the validator only needs float-level
    precision for the cap comparison (amounts are paise-rounded already
    upstream)."""
    if v is None:
        return 0.0
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return 0.0


def _hhmm_to_minutes(s: str) -> int:
    """Parse 'HH:MM' → minutes-since-midnight. Returns -1 on a malformed
    value so a partial admin config can't accidentally lock everyone out;
    the outer check treats -1 as "this window is unusable, skip it"."""
    try:
        h, m = s.split(":", 1)
        h_i, m_i = int(h), int(m)
        if 0 <= h_i <= 23 and 0 <= m_i <= 59:
            return h_i * 60 + m_i
    except (ValueError, TypeError, AttributeError):
        pass
    return -1


def _format_time_windows(windows: Any) -> str:
    if not windows:
        return "anytime"
    parts: list[str] = []
    for w in windows:
        s = getattr(w, "start", None) or (w.get("start") if isinstance(w, dict) else None)
        e = getattr(w, "end", None) or (w.get("end") if isinstance(w, dict) else None)
        if s and e:
            parts.append(f"{s}–{e}")
    return ", ".join(parts) if parts else "anytime"


def _format_days(days: list[int] | None) -> str:
    if not days:
        return "Any day"
    if sorted(days) == [0, 1, 2, 3, 4, 5, 6]:
        return "All days"
    return ", ".join(_WEEKDAY_NAMES[d] for d in sorted(days) if 0 <= d <= 6)


async def validate_request(
    *,
    user_id: str | PydanticObjectId,
    rule_type: str,
    amount: float,
    user_remark: str | None = None,
) -> dict[str, Any]:
    """Raise `OrderRejectedError` (re-used here as a generic
    HTTP-400-with-code container) if the request violates the user's
    effective rule. Returns the resolved rule on success so the caller
    can short-circuit a redundant resolve.

    Checks:
      • amount >= min_amount  (configured per tier)
      • amount <= max_amount
      • amount + today's already-submitted amount <= daily_limit
      • current IST weekday in allowed_days
      • current IST time within at least one allowed_times window
      • user_remark non-empty when mandatory_remark = true

    All errors carry a stable code (MIN_AMOUNT / MAX_AMOUNT / DAILY_LIMIT
    / DAY_NOT_ALLOWED / TIME_NOT_ALLOWED / REMARK_REQUIRED) so the
    frontend can translate them or branch logic on them.
    """
    from datetime import timedelta

    from app.core.exceptions import OrderRejectedError
    from app.utils.time_utils import now_ist

    rule = await get_effective_rule(user_id, rule_type)

    # 1) Min / max — cheapest checks, do first.
    min_amt = _decimal_or_zero(rule.get("min_amount"))
    max_amt = _decimal_or_zero(rule.get("max_amount"))
    if min_amt > 0 and amount < min_amt:
        raise OrderRejectedError(
            f"Minimum {rule_type.lower()} is ₹{min_amt:.2f}",
            code="MIN_AMOUNT",
        )
    if max_amt > 0 and amount > max_amt:
        raise OrderRejectedError(
            f"Maximum {rule_type.lower()} per request is ₹{max_amt:.2f}",
            code="MAX_AMOUNT",
        )

    # 2) Daily-limit — sum today's already-submitted-or-approved requests.
    daily_limit = _decimal_or_zero(rule.get("daily_limit"))
    if daily_limit > 0:
        now = now_ist()
        start_of_day_ist = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # Same model collection drives both deposits + withdrawals via
        # `rule_type`. Treat REJECTED / CANCELLED states as "not consumed"
        # — only count rows that are still in-flight or already settled
        # against today's cap.
        if rule_type == WdRuleType.DEPOSIT.value:
            from app.models.transaction import DepositRequest, DepositStatus

            rows = await DepositRequest.find(
                DepositRequest.user_id == PydanticObjectId(str(user_id)),
                DepositRequest.created_at >= start_of_day_ist,
                DepositRequest.status != DepositStatus.REJECTED,
            ).to_list()
        else:
            from app.models.transaction import WithdrawalRequest, WithdrawalStatus

            rows = await WithdrawalRequest.find(
                WithdrawalRequest.user_id == PydanticObjectId(str(user_id)),
                WithdrawalRequest.created_at >= start_of_day_ist,
                WithdrawalRequest.status != WithdrawalStatus.REJECTED,
            ).to_list()
        consumed = sum(_decimal_or_zero(r.amount) for r in rows)
        if (consumed + amount) > daily_limit:
            remaining = max(0.0, daily_limit - consumed)
            raise OrderRejectedError(
                (
                    f"Daily {rule_type.lower()} limit ₹{daily_limit:.2f} "
                    f"would be exceeded — ₹{remaining:.2f} remaining today"
                ),
                code="DAILY_LIMIT",
            )

    # 3) Day-of-week — admin's allowed_days uses 0=Mon..6=Sun (Python's
    # weekday() ordering, matches Indian banking week conventions).
    allowed_days = rule.get("allowed_days") or []
    if allowed_days:
        now = now_ist()
        wd = now.weekday()
        if wd not in allowed_days:
            raise OrderRejectedError(
                (
                    f"{rule_type.title()}s not allowed on {_WEEKDAY_NAMES[wd]} — "
                    f"allowed: {_format_days(allowed_days)}"
                ),
                code="DAY_NOT_ALLOWED",
            )

    # 4) Time-of-day — at least one window must contain "now".
    allowed_times = rule.get("allowed_times") or []
    if allowed_times:
        now = now_ist()
        now_min = now.hour * 60 + now.minute
        ok = False
        for w in allowed_times:
            s = getattr(w, "start", None) or (w.get("start") if isinstance(w, dict) else None)
            e = getattr(w, "end", None) or (w.get("end") if isinstance(w, dict) else None)
            if not s or not e:
                continue
            s_min = _hhmm_to_minutes(s)
            e_min = _hhmm_to_minutes(e)
            if s_min < 0 or e_min < 0:
                continue
            # Same-day window. Cross-midnight (end < start) is treated as
            # two halves: [start, 24:00) ∪ [00:00, end). Most admin
            # configs are 09:00–21:00 style so the simple case wins.
            if s_min <= e_min:
                if s_min <= now_min < e_min:
                    ok = True
                    break
            else:
                if now_min >= s_min or now_min < e_min:
                    ok = True
                    break
        if not ok:
            raise OrderRejectedError(
                (
                    f"{rule_type.title()}s allowed between {_format_time_windows(allowed_times)} IST"
                ),
                code="TIME_NOT_ALLOWED",
            )

    # 5) Mandatory remark — DISABLED for both deposits and withdrawals
    # (operator request): the remark field is optional on both flows, so a
    # request must go through without one. Deposits are verified on the
    # UTR + screenshot; withdrawals on the admin's manual review. The
    # mandatory_remark rule field is kept in the model but no longer blocks.
    _ = user_remark  # intentionally not enforced

    return rule
