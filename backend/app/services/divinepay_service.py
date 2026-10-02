"""Divinepay UPI pay-in gateway (single platform merchant account).

B-book money path: a deposit creates a PENDING ``DepositRequest`` and the
GATEWAY'S verdict — never the client — credits the wallet. All calls are
server-side; the API key is a live secret and never reaches the browser.

REST contract (base = DIVINEPAY_BASE_URL, header x-api-key):
  create_payin  POST /api/payin/payin/create  {amount}            → {order_id, paymentUrl}
  submit_utr    POST /api/payin/submit-utr     {order_id, utr}     → {status}
  check_status  POST /api/payin/status         {order_id}          → {status}

A deposit is credited only when the gateway returns status == "success", via
``credit_deposit`` (atomic PENDING→APPROVED claim guards against double-credit)
— driven by the user's UTR-submit, the status poll, or ``reconcile_deposits``.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import settings
from app.models.transaction import (
    DepositRequest,
    DepositStatus,
    PaymentMode,
    TransactionType,
)
from app.services import wallet_service
from app.utils.decimal_utils import to_decimal
from app.utils.time_utils import now_utc

logger = logging.getLogger("divinepay_service")

MIN_AMOUNT = Decimal("100")
MAX_AMOUNT = Decimal("25000")
GATEWAY = "divinepay"


class DivinepayError(Exception):
    """Any Divinepay config / API / response error."""


SUPER_ADMIN_POOL_KEY = "payment_gateway.super_admin_pool_enabled"


def is_configured() -> bool:
    return bool(settings.DIVINEPAY_API_KEY.get_secret_value())


async def gateway_on_for(user) -> bool:
    """Is the Divinepay gateway ON for THIS user?

    Ships OFF everywhere. Enabled only when the key is configured AND the user's
    pool is switched on by the super-admin:
      • user.assigned_admin_id is None (super-admin's own pool) → the
        ``payment_gateway.super_admin_pool_enabled`` PlatformSetting; an absent
        row means OFF (the whole platform defaults to manual).
      • otherwise → the owning admin's ``User.payment_gateway_enabled`` (default
        False), so the super-admin turns it on per admin.
    """
    if not is_configured():
        return False
    admin_id = getattr(user, "assigned_admin_id", None)
    if admin_id is None:
        from app.models.platform_setting import PlatformSetting

        row = await PlatformSetting.find_one(
            PlatformSetting.setting_key == SUPER_ADMIN_POOL_KEY
        )
        if row is None:
            return False
        raw = row.setting_value
        return raw is True or str(raw).strip().lower() in {"true", "1", "yes", "on"}
    from app.models.user import User

    admin = await User.get(admin_id)
    # Admin gate (prerequisite): the super-admin must have enabled this admin.
    # If the admin isn't allowed, NONE of their users get the gateway — a broker
    # toggle can't override this.
    if not (admin and getattr(admin, "payment_gateway_enabled", False)):
        return False
    # Broker layer: if the user sits under a broker, the ADMIN decides per broker
    # whether that broker's users get the gateway (broker.payment_gateway_enabled,
    # default OFF). Users directly under the admin (no broker) follow the admin.
    broker_id = getattr(user, "assigned_broker_id", None)
    if broker_id is None:
        anc = getattr(user, "broker_ancestry", None) or []
        broker_id = anc[-1] if anc else None
    if broker_id is not None:
        broker = await User.get(broker_id)
        return bool(broker and getattr(broker, "payment_gateway_enabled", False))
    return True


def is_success(d: dict[str, Any] | None) -> bool:
    return str((d or {}).get("status", "")).strip().lower() == "success"


def _headers() -> dict[str, str]:
    return {
        "x-api-key": settings.DIVINEPAY_API_KEY.get_secret_value(),
        "Content-Type": "application/json",
    }


async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    if not is_configured():
        raise DivinepayError("Divinepay is not configured")
    url = f"{settings.DIVINEPAY_BASE_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json=body, headers=_headers())
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code >= 400:
        logger.error("divinepay_http_%s %s: %s", resp.status_code, path, data or resp.text)
        msg = (data or {}).get("message") or f"HTTP {resp.status_code}"
        raise DivinepayError(f"Divinepay: {msg}")
    data = data or {}
    # Divinepay wraps every result in a {success, message, data:{...}} envelope.
    # Flatten the inner `data` up so callers can read order_id / paymentUrl /
    # status uniformly regardless of nesting.
    inner = data.get("data")
    if isinstance(inner, dict):
        return {**data, **inner}
    return data


# ── Gateway calls ────────────────────────────────────────────────────
async def create_payin(amount: Decimal) -> dict[str, Any]:
    """Create a UPI pay-in order. Returns ``{order_id, payment_url}``."""
    amt = to_decimal(amount)
    if amt < MIN_AMOUNT or amt > MAX_AMOUNT:
        raise DivinepayError(
            f"Amount must be between ₹{MIN_AMOUNT} and ₹{MAX_AMOUNT}"
        )
    data = await _post("/api/payin/payin/create", {"amount": float(amt)})
    order_id = data.get("order_id") or data.get("orderId")
    payment_url = data.get("paymentUrl") or data.get("payment_url")
    if not order_id or not payment_url:
        logger.error("divinepay_create_bad_response: %s", data)
        raise DivinepayError("Divinepay: no payment link returned")
    return {"order_id": str(order_id), "payment_url": str(payment_url)}


async def submit_utr(order_id: str, utr: str) -> dict[str, Any]:
    return await _post("/api/payin/submit-utr", {"order_id": order_id, "utr": utr})


async def check_status(order_id: str) -> dict[str, Any]:
    return await _post("/api/payin/status", {"order_id": order_id})


# ── Credit (gateway-authorised, double-credit-safe) ──────────────────
async def credit_deposit(req: DepositRequest, utr: str | None = None) -> bool:
    """Verify the order with the gateway and, if paid, ATOMICALLY claim the
    PENDING deposit and credit the wallet. Returns False when the gateway
    hasn't confirmed payment OR the deposit was already credited (lost the
    compare-and-set race). The GATEWAY authorises the credit, never the client.
    """
    order_id = (req.gateway_ref or "").strip()
    if not order_id and req.idempotency_key and ":" in req.idempotency_key:
        order_id = req.idempotency_key.split(":", 1)[1]
    if not order_id:
        return False

    # 1) Gateway verdict FIRST — never credit off the client's word.
    try:
        status = await check_status(order_id)
    except DivinepayError:
        return False
    if not is_success(status):
        return False

    # 2) Atomic PENDING→APPROVED claim. Only the winner credits; a concurrent
    #    submit-utr / reconcile / status poll that lost the race matches zero
    #    docs and returns False, so the wallet is credited exactly once.
    processed_at = now_utc()
    claimed = await DepositRequest.get_motor_collection().find_one_and_update(
        {"_id": req.id, "status": DepositStatus.PENDING.value},
        {
            "$set": {
                "status": DepositStatus.APPROVED.value,
                "utr_number": (utr or req.utr_number or None),
                "gateway_status": "success",
                "processed_at": processed_at,
                "updated_at": processed_at,
            }
        },
    )
    if claimed is None:
        return False

    amount = to_decimal(req.amount)
    try:
        await wallet_service.adjust(
            req.user_id,
            amount,
            transaction_type=TransactionType.DEPOSIT,
            narration=f"Deposit via Divinepay (UPI ref {utr or req.utr_number or order_id})",
            reference_type="DEPOSIT",
            reference_id=str(req.id),
        )
    except Exception:
        # Credit failed AFTER the claim — revert to PENDING so the deposit
        # drops back into the reconcile queue instead of showing APPROVED with
        # no money moved. Re-raise the real error.
        await DepositRequest.get_motor_collection().update_one(
            {"_id": req.id},
            {"$set": {"status": DepositStatus.PENDING.value, "updated_at": now_utc()}},
        )
        raise

    # First-deposit stamp + best-effort bonus grant (mirrors the manual-approve
    # path; never blocks the credit that already succeeded).
    try:
        if settings.BONUSES_ENABLED:
            from app.models.user import User as _User
            from app.services import bonus_service as _bonus

            u = await _User.get(req.user_id)
            if u is not None:
                was_first = getattr(u, "first_deposit_at", None) is None
                if was_first:
                    u.first_deposit_at = processed_at
                    await u.save()
                await _bonus.maybe_auto_grant_on_deposit(
                    u, amount, deposit_id=req.id, is_first=was_first
                )
    except Exception:
        logger.exception("divinepay_bonus_grant_failed deposit=%s", req.id)

    logger.info("divinepay_credited deposit=%s order=%s amount=%s", req.id, order_id, amount)
    return True


# Gateway statuses that mean the pay-in is dead. A PENDING deposit that hits
# any of these — or that the user simply abandoned (see STALE_MINUTES) — is
# flipped to FAILED so the admin's Gateway Payments tab never shows an eternal
# "PENDING". A real UPI collect completes in a minute or two.
_FAILURE_STATUSES = {
    "failed", "fail", "expired", "expire", "cancelled", "canceled",
    "declined", "decline", "rejected", "reject", "timeout", "error",
}
STALE_MINUTES = 20  # unpaid after this long → treat as abandoned → FAILED


async def reconcile_deposits(user_id=None, max_age_hours: int = 24) -> int:
    """Sweep PENDING Divinepay deposits: credit the paid ones and mark the dead
    ones FAILED so the status is always terminal (SUCCESS / FAILED), never a
    stale PENDING. `max_age_hours` bounds how long we still try to CREDIT a
    late-confirmed payment; anything older is only ever failed, never credited."""
    if not is_configured():
        return 0
    from datetime import timedelta

    now = now_utc()
    credit_window = now - timedelta(hours=max_age_hours)
    stale_before = now - timedelta(minutes=STALE_MINUTES)
    # Bound the scan to 30 days so ancient rows don't get re-swept forever.
    scan_from = now - timedelta(days=30)
    q: dict[str, Any] = {
        "gateway": GATEWAY,
        "status": DepositStatus.PENDING.value,
        "created_at": {"$gte": scan_from},
    }
    if user_id is not None:
        q["user_id"] = user_id
    credited = 0
    failed = 0
    rows = await DepositRequest.find(q).to_list()
    for r in rows:
        order_id = (r.gateway_ref or "").strip()
        if not order_id and r.idempotency_key and ":" in r.idempotency_key:
            order_id = r.idempotency_key.split(":", 1)[1]
        if not order_id:
            continue

        status = ""
        # Only ping the gateway while the deposit is still creditable (< max_age)
        # — for older rows we just fail them without a call.
        if r.created_at is None or r.created_at >= credit_window:
            try:
                st = await check_status(order_id)
                status = str((st or {}).get("status", "")).strip().lower()
            except DivinepayError:
                continue  # gateway hiccup — retry on the next sweep

        if status == "success":
            try:
                if await credit_deposit(r):
                    credited += 1
            except Exception:
                logger.exception("divinepay_reconcile_credit_failed deposit=%s", r.id)
            continue

        # Dead: the gateway said so, OR the user abandoned it (unpaid past the
        # stale window). Atomic guard so we never fail a row that just credited.
        is_dead = status in _FAILURE_STATUSES or (
            r.created_at is not None and r.created_at < stale_before
        )
        if is_dead:
            res = await DepositRequest.get_motor_collection().update_one(
                {"_id": r.id, "status": DepositStatus.PENDING.value},
                {
                    "$set": {
                        "status": DepositStatus.FAILED.value,
                        "gateway_status": status or "abandoned",
                        "updated_at": now,
                    }
                },
            )
            if getattr(res, "modified_count", 0):
                failed += 1

    if credited or failed:
        logger.info("divinepay_reconcile credited=%s failed=%s", credited, failed)
    return credited


async def deposit_reconcile_loop(*, interval_sec: float = 20.0) -> None:
    """Leader-only background loop — auto-credits paid Divinepay deposits."""
    import asyncio

    while True:
        try:
            if is_configured():
                await reconcile_deposits()
        except Exception:
            logger.exception("divinepay_reconcile_loop_iteration_failed")
        await asyncio.sleep(interval_sec)
