"""User wallet endpoints — balance, transactions, deposit/withdrawal requests."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from pathlib import Path

from beanie import PydanticObjectId
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.dependencies import CurrentUser
from app.models.bank_account import CompanyBankAccount, UserBankAccount
from app.models.transaction import (
    DepositRequest,
    DepositStatus,
    PaymentMode,
    WithdrawalRequest,
    WithdrawalStatus,
    BankSnapshot,
)
from app.schemas.common import APIResponse
from app.schemas.trading import DepositCreate, WalletSummary, WithdrawalCreate
from app.services import wallet_service
from app.utils.decimal_utils import to_decimal128

router = APIRouter(prefix="/wallet", tags=["user-wallet"])

# Screenshot uploads — saved to ./uploads/screenshots/<user_id>/<uuid>.<ext>
# and served back via the static mount at /uploads (configured in main.py).
UPLOAD_ROOT = Path("uploads") / "screenshots"
ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
# Server-side ceiling. The user-side compresses to ~300-500 KB before sending,
# so this is just a safety cap for clients that bypass compression.
MAX_BYTES = 10 * 1024 * 1024


@router.post("/upload-screenshot", response_model=APIResponse[dict])
async def upload_screenshot(user: CurrentUser, file: UploadFile = File(...)):
    """Accepts a single image file. Returns `{ url }` to embed in the deposit request."""
    ext = (Path(file.filename or "").suffix or "").lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type. Allowed: {sorted(ALLOWED_EXTS)}")

    contents = await file.read()
    if len(contents) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_BYTES // (1024*1024)} MB)")
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Empty file")

    user_dir = UPLOAD_ROOT / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid.uuid4().hex}{ext}"
    out_path = user_dir / fname
    # write_bytes blocks the event loop; offload so concurrent uploads from
    # other users aren't serialized behind a single big disk write.
    await asyncio.to_thread(out_path.write_bytes, contents)

    # Public URL (served by StaticFiles mount in main.py)
    url = f"/uploads/screenshots/{user.id}/{fname}"
    return APIResponse(data={"url": url, "size": len(contents)})


@router.get("/summary", response_model=APIResponse[WalletSummary])
async def summary(user: CurrentUser):
    # Best-effort: credit any Divinepay deposit this user paid but didn't verify
    # (paid then closed the tab), so a wallet refresh reflects it instantly. Only
    # touches the gateway when a PENDING gateway row actually exists; never
    # blocks the summary.
    try:
        from app.services import divinepay_service

        if divinepay_service.is_configured():
            await divinepay_service.reconcile_deposits(user.id)
    except Exception:
        pass
    return APIResponse(data=WalletSummary(**(await wallet_service.summary(user.id))))


@router.get("/transactions", response_model=APIResponse[list])
async def transactions(user: CurrentUser, limit: int = 100, skip: int = 0):
    txns = await wallet_service.list_transactions(user.id, limit=limit, skip=skip)
    return APIResponse(
        data=[
            {
                "id": str(t.id),
                "transaction_type": t.transaction_type.value,
                "amount": str(t.amount),
                "balance_before": str(t.balance_before),
                "balance_after": str(t.balance_after),
                "narration": t.narration,
                "status": t.status.value,
                "reference_type": t.reference_type,
                "reference_id": t.reference_id,
                "created_at": t.created_at,
            }
            for t in txns
        ]
    )


_COMPANY_BANKS_CACHE_PREFIX = "wallet:company-banks:"
_COMPANY_BANKS_CACHE_TTL = 3600  # 1 h — admin edits invalidate; otherwise rare


@router.get("/company-banks", response_model=APIResponse[list])
async def company_banks(user: CurrentUser):
    # Cascade owner resolution: deepest broker → walk up broker_ancestry
    # → admin → platform default. Earlier the cascade only checked the
    # IMMEDIATE broker before falling all the way back to admin, which
    # skipped any parent broker in a multi-level chain (sub-broker
    # without own banks went straight to admin, ignoring its parent
    # broker's banks). User-flagged: "admin ne jo details laga rakhi
    # hai broker / sub-broker ke user ko bhi wahi show kare, jab tak
    # broker / sub-broker change na kare". Walking the full ancestry
    # makes the cascade match that intent — each level shows the
    # closest ancestor's banks until someone in the chain authors
    # their own.
    from app.core.redis_client import cache_get, cache_set

    cascade: list[tuple[str, dict]] = []
    # Immediate broker first.
    if user.assigned_broker_id is not None:
        cascade.append(
            (f"broker:{user.assigned_broker_id}", {"owner_broker_id": user.assigned_broker_id})
        )
    # Walk broker_ancestry root-to-tip in REVERSE — closest-to-user
    # parent first, root last. Skip the immediate broker (already
    # added above). Skip empty ancestry safely.
    ancestry = list(user.broker_ancestry or [])
    if ancestry:
        # broker_ancestry stores root-first (root, ..., parent-of-immediate).
        # The immediate broker is `assigned_broker_id`, NOT in the array.
        for parent_broker_id in reversed(ancestry):
            if parent_broker_id == user.assigned_broker_id:
                continue
            cascade.append(
                (
                    f"broker:{parent_broker_id}",
                    {"owner_broker_id": parent_broker_id},
                )
            )
    if user.assigned_admin_id is not None:
        cascade.append(
            (
                f"admin:{user.assigned_admin_id}",
                {"owner_admin_id": user.assigned_admin_id, "owner_broker_id": None},
            )
        )
    # Platform-default (super-admin) pool. This is ONLY the fallback for users
    # who belong DIRECTLY to the super-admin — i.e. have no sub-admin. A user
    # under a sub-admin must NEVER fall through to the super-admin's bank
    # details: if that sub-admin (and any broker in the user's chain) hasn't
    # authored a payment method, the deposit form shows NOTHING instead of
    # leaking the super-admin's bank. Operator: "admin ne bank set nahi kiya
    # to uske user ko super-admin ka bank na dikhe — koi bhi bank na dikhe".
    # The broker → parent-broker → admin cascade above is unchanged, so a
    # broker's user still inherits its own admin's pool as before.
    if user.assigned_admin_id is None:
        cascade.append(("default", {"owner_admin_id": None, "owner_broker_id": None}))

    for pool_key, owner_filter in cascade:
        cache_key = f"{_COMPANY_BANKS_CACHE_PREFIX}{pool_key}"
        cached = await cache_get(cache_key)
        if cached is not None:
            if cached:
                return APIResponse(data=cached)
            continue

        rows = await CompanyBankAccount.find(
            {"is_active": True, **owner_filter}
        ).sort("-is_default").to_list()
        data = [
            {
                "id": str(r.id),
                "bank_name": r.bank_name,
                "account_holder": r.account_holder,
                "account_number": r.account_number,
                "ifsc_code": r.ifsc_code,
                "upi_id": r.upi_id,
                "qr_code_url": r.qr_code_url,
                "is_default": r.is_default,
            }
            for r in rows
        ]
        await cache_set(cache_key, data, ttl_sec=_COMPANY_BANKS_CACHE_TTL)
        if data:
            return APIResponse(data=data)

    return APIResponse(data=[])


@router.get("/crypto-config", response_model=APIResponse[dict | None])
async def wallet_crypto_config(user: CurrentUser):
    """Resolved crypto-deposit config for THIS user (their admin cascade).

    Returns ``null`` when crypto isn't enabled for them — the app then hides
    the Crypto option and shows only the plain INR flow. Never leaks any key:
    manual gives the address/network/asset; gateway only says it's available.
    """
    from app.services import crypto_config_service

    cfg = await crypto_config_service.resolve_for_user(user)
    return APIResponse(data=crypto_config_service.to_user_dict(cfg))


@router.get("/crypto-quote", response_model=APIResponse[dict | None])
async def wallet_crypto_quote(user: CurrentUser, amount: float, asset: str):
    """Live ₹→crypto conversion for the deposit screen — how much of `asset`
    to send for ₹`amount`. null when no live rate (frontend shows ₹ only)."""
    from app.services import crypto_price_service

    return APIResponse(data=await crypto_price_service.convert_inr_to_asset(amount, asset))


@router.post("/deposits/crypto/oxapay", response_model=APIResponse[dict])
async def create_oxapay_deposit(payload: DepositCreate, user: CurrentUser):
    """Start an oxapay-gateway crypto deposit: create a PENDING deposit, open an
    oxapay invoice with the owning admin's key (amount in INR — oxapay converts
    to crypto), and return the hosted payment URL. The webhook auto-credits on
    'paid'. Only the ₹ amount is used from the payload."""
    if getattr(user, "is_demo", False):
        raise HTTPException(status_code=403, detail="Demo accounts cannot deposit funds.")
    from app.core.config import settings as _settings
    from app.services import crypto_config_service, oxapay_service, wd_rules_service

    # Same deposit-rule gate as INR/manual (min/max/daily limits).
    await wd_rules_service.validate_request(
        user_id=user.id, rule_type="DEPOSIT", amount=float(payload.amount), user_remark=None
    )
    cfg = await crypto_config_service.resolve_for_user(user)
    key = crypto_config_service.decrypted_oxapay_key(cfg) if cfg else None
    if not cfg or not cfg.enabled or cfg.mode not in ("gateway", "both") or not key:
        raise HTTPException(status_code=400, detail="Crypto gateway isn't available for your account.")
    owner = await crypto_config_service.owner_user_for_config(cfg)
    owner_code = owner.user_code if owner else None
    if not owner_code:
        raise HTTPException(status_code=400, detail="Crypto gateway owner not found.")

    # INITIATED row first — its id is the oxapay order_id (webhook finds it
    # back). INITIATED is invisible to the admin and to the user's pending
    # count: if the user backs out without paying it stays INITIATED (later
    # flipped to FAILED). The webhook promotes it to APPROVED (auto-credit) on
    # 'paid', or FAILED on 'expired'/'failed'.
    req = DepositRequest(
        user_id=user.id,
        amount=to_decimal128(payload.amount),
        payment_mode=PaymentMode.CRYPTO,
        gateway="oxapay",
        status=DepositStatus.INITIATED,
        idempotency_key=uuid.uuid4().hex,
    )
    await req.insert()

    base = (_settings.BACKEND_PUBLIC_URL or "http://localhost:8000").rstrip("/")
    callback_url = f"{base}/api/v1/webhooks/oxapay/{owner_code}"
    dom = owner.custom_domain if (owner and owner.custom_domain) else "marginwealth.com"
    return_url = f"https://{dom}/wallet"
    try:
        inv = await oxapay_service.create_invoice(
            api_key=key,
            amount=float(payload.amount),
            currency="INR",
            callback_url=callback_url,
            return_url=return_url,
            order_id=str(req.id),
            description=f"Deposit for {user.user_code}",
        )
    except Exception as e:
        # Roll back the pending row so a failed invoice doesn't linger.
        await req.delete()
        raise HTTPException(status_code=400, detail=f"Could not start crypto payment: {e}")
    req.gateway_ref = inv["track_id"]
    await req.save()
    return APIResponse(data={"payment_url": inv["payment_url"], "deposit_id": str(req.id)})


# ── Divinepay UPI gateway (auto-crediting pay-in) ────────────────────
class _GatewayDepositBody(BaseModel):
    amount: Decimal


class _SubmitUtrBody(BaseModel):
    order_id: str
    utr: str


@router.get("/deposit-mode", response_model=APIResponse[dict])
async def deposit_mode(user: CurrentUser):
    """Tells the client which deposit flow to render: GATEWAY (auto-crediting
    Divinepay UPI) when the super-admin enabled it for this user's pool, else
    MANUAL (bank-QR + screenshot + admin approval)."""
    from app.services import divinepay_service

    on = await divinepay_service.gateway_on_for(user)
    return APIResponse(
        data={
            "mode": "GATEWAY" if on else "MANUAL",
            "min_amount": str(divinepay_service.MIN_AMOUNT),
            "max_amount": str(divinepay_service.MAX_AMOUNT),
        }
    )


@router.post("/deposits/gateway", response_model=APIResponse[dict])
async def create_gateway_deposit(payload: _GatewayDepositBody, user: CurrentUser):
    """Open a Divinepay UPI pay-in: create a PENDING DepositRequest + a gateway
    order, return the hosted payment URL. NOTHING is credited here — the
    gateway's verdict (submit-utr / status / reconcile) credits the wallet."""
    if getattr(user, "is_demo", False):
        raise HTTPException(status_code=403, detail="Demo accounts cannot deposit funds.")
    from app.services import divinepay_service

    if not await divinepay_service.gateway_on_for(user):
        raise HTTPException(status_code=400, detail="Online payment isn't available for your account.")
    # The gateway's own ₹100–₹25,000 band is the authoritative limit (enforced
    # in create_payin). The admin's MANUAL deposit rule (which can set a higher
    # min for bank transfers) deliberately does NOT gate the online flow — the
    # operator wants online pay-in to start from the ₹100 gateway floor.
    try:
        created = await divinepay_service.create_payin(payload.amount)
    except divinepay_service.DivinepayError as e:
        raise HTTPException(status_code=400, detail=str(e))

    order_id = created["order_id"]
    req = DepositRequest(
        user_id=user.id,
        amount=to_decimal128(payload.amount),
        payment_mode=PaymentMode.UPI,
        gateway=divinepay_service.GATEWAY,
        gateway_ref=order_id,
        status=DepositStatus.PENDING,
        idempotency_key=f"divinepay:{order_id}",
    )
    await req.insert()
    return APIResponse(
        data={
            "deposit_id": str(req.id),
            "order_id": order_id,
            "payment_url": created["payment_url"],
            "amount": str(payload.amount),
        }
    )


@router.post("/deposits/gateway/submit-utr", response_model=APIResponse[dict])
async def submit_gateway_utr(payload: _SubmitUtrBody, user: CurrentUser):
    """User submits their UPI UTR after paying. We hand it to the gateway and
    then let the gateway's success verdict credit the wallet — never the
    client's word."""
    from app.services import divinepay_service

    order_id = (payload.order_id or "").strip()
    utr = (payload.utr or "").strip()
    req = await DepositRequest.find_one(
        DepositRequest.idempotency_key == f"divinepay:{order_id}",
        DepositRequest.user_id == user.id,
    )
    if req is None:
        raise HTTPException(status_code=404, detail="Deposit not found")
    if req.status == DepositStatus.APPROVED:
        return APIResponse(data={"status": "success", "already_credited": True, "amount": str(req.amount)})

    # Record the UTR, tell the gateway, then let the gateway authorise the credit.
    if utr and req.utr_number != utr:
        req.utr_number = utr
        await req.save()
    try:
        await divinepay_service.submit_utr(order_id, utr)
    except divinepay_service.DivinepayError:
        pass  # fall through to a status-driven credit attempt

    credited = await divinepay_service.credit_deposit(req, utr)
    if credited:
        return APIResponse(data={"status": "success", "credited": True, "amount": str(req.amount)})
    # Not confirmed yet — surface the current gateway status; the 20s reconcile
    # loop (and the next UTR submit) will credit it once the gateway confirms.
    try:
        st = await divinepay_service.check_status(order_id)
    except divinepay_service.DivinepayError:
        st = {}
    return APIResponse(data={"status": str(st.get("status") or "pending"), "credited": False})


@router.post("/deposits", response_model=APIResponse[dict])
async def create_deposit(payload: DepositCreate, user: CurrentUser):
    if getattr(user, "is_demo", False):
        raise HTTPException(status_code=403, detail="Demo accounts cannot deposit funds. Open a real account to trade with real money.")
    # Enforce the effective deposit rule for this user — min/max/daily
    # limit/day/time window/mandatory-remark. Tier cascade is resolved
    # inside the service (broker → admin → super-admin → global). Raises
    # OrderRejectedError with a stable code on violation; AppError handler
    # converts that to 400 + machine-readable error envelope.
    from app.services import wd_rules_service

    await wd_rules_service.validate_request(
        user_id=user.id,
        rule_type="DEPOSIT",
        amount=float(payload.amount),
        user_remark=payload.user_remark,
    )

    # Duplicate-pending gate (admin-configurable, per-pool, default OFF). When
    # the user's effective DEPOSIT rule has `block_duplicate_pending` turned on
    # by their owning admin / super-admin, they cannot raise a new deposit
    # request while a PENDING one is still awaiting processing.
    _dep_rule = await wd_rules_service.get_effective_rule(user.id, "DEPOSIT")
    if _dep_rule.get("block_duplicate_pending"):
        _pending_dep = await DepositRequest.find(
            DepositRequest.user_id == user.id,
            DepositRequest.status == DepositStatus.PENDING,
        ).count()
        if _pending_dep > 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "You already have a pending deposit request. "
                    "Please wait for it to be processed."
                ),
            )

    # Per-day request cap (admin-configurable, per-pool, default 0 = no cap).
    # Counts EVERY deposit request this user created today (IST day, any
    # status). Mirrors the withdrawal cap.
    _dep_max_req = int(_dep_rule.get("max_requests_per_day") or 0)
    if _dep_max_req > 0:
        from app.utils.time_utils import start_of_day_ist, to_utc

        _dep_today = await DepositRequest.find(
            DepositRequest.user_id == user.id,
            DepositRequest.created_at >= to_utc(start_of_day_ist()),
        ).count()
        if _dep_today >= _dep_max_req:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Daily deposit request limit reached "
                    f"({_dep_max_req} per day). Try again tomorrow."
                ),
            )

    # Proof is mandatory so the admin can verify before approving. For a
    # CRYPTO deposit the on-chain tx hash IS the proof (screenshot optional);
    # for INR the payment screenshot is required as before.
    _is_crypto = str(payload.payment_mode or "").upper() == "CRYPTO"
    if _is_crypto:
        if not ((payload.crypto_tx_hash or "").strip() or (payload.screenshot_url or "").strip()):
            raise HTTPException(
                status_code=400,
                detail="Enter the transaction hash (or upload a screenshot) for your crypto payment.",
            )
    elif not (payload.screenshot_url or "").strip():
        raise HTTPException(status_code=400, detail="Payment screenshot is required.")

    # Idempotency: a client-supplied key dedups double / triple clicks and
    # retried-after-timeout requests — if a deposit with this key already
    # exists for the user, return it instead of inserting a duplicate. The
    # `deposit_requests` collection has a unique index on idempotency_key, so
    # we fall back to a fresh UUID when the client sends none.
    client_idem = (payload.idempotency_key or "").strip() or None
    if client_idem:
        dup = await DepositRequest.find_one(
            DepositRequest.user_id == user.id,
            DepositRequest.idempotency_key == client_idem,
        )
        if dup is not None:
            return APIResponse(data={"id": str(dup.id), "status": dup.status.value})
    idem = client_idem or uuid.uuid4().hex
    req = DepositRequest(
        user_id=user.id,
        amount=to_decimal128(payload.amount),
        payment_mode=PaymentMode(payload.payment_mode),
        utr_number=payload.utr_number,
        screenshot_url=payload.screenshot_url,
        user_remark=payload.user_remark,
        bank_account_id=PydanticObjectId(payload.bank_account_id) if payload.bank_account_id else None,
        status=DepositStatus.PENDING,
        idempotency_key=idem,
        crypto_asset=payload.crypto_asset if _is_crypto else None,
        crypto_network=payload.crypto_network if _is_crypto else None,
        crypto_address=payload.crypto_address if _is_crypto else None,
        crypto_tx_hash=payload.crypto_tx_hash if _is_crypto else None,
    )
    await req.insert()
    # Scope-aware push fan-out — survives PWA force-stop / locked phone
    # AND only pings the admins/brokers who actually own this user, NOT
    # the whole platform. Compute the recipient set first so we can
    # include it in the WS publish below; the frontend bridge mirrors
    # the same filter so the in-page toast is scoped identically.
    recipient_ids: list[str] = []
    try:
        import asyncio as _asyncio

        from app.services.push_service import (
            _compute_recipient_admin_ids as _compute_owners,
            send_to_user_owners as _push_owners,
        )

        owners = await _compute_owners(user.id)
        recipient_ids = [str(x) for x in owners]
        _asyncio.create_task(
            _push_owners(
                user.id,
                title="💰 New deposit request",
                body=f"₹{payload.amount} · {user.full_name or user.user_code} · {payload.payment_mode.upper()}",
                url="/payments?tab=deposits",
                tag=f"deposit-{req.id}",
            )
        )
    except Exception:  # pragma: no cover
        pass
    # Fan out to admin dashboards so the Deposits inbox shows the new
    # request immediately — no F5. Includes `recipient_admin_ids` so the
    # frontend AdminWsBridge can suppress the toast/native-notification
    # for admins who don't own this user (background invalidation still
    # runs — data stays fresh, just no noise for out-of-scope admins).
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "deposit_update",
            {
                "event": "submitted",
                "user_id": str(user.id),
                "deposit_id": str(req.id),
                "user_name": user.full_name,
                "user_code": user.user_code,
                "amount": str(payload.amount),
                "mode": payload.payment_mode,
                "recipient_admin_ids": recipient_ids,
            },
        )
    except Exception:  # pragma: no cover
        pass
    # Admin notification bell — fan out a per-recipient AdminNotification
    # row up the tier chain (super-admin + assigned admin + every broker
    # in the ancestry). Best-effort: a notification failure must NOT
    # roll back the deposit insert above.
    try:
        from app.models.notification import (
            AdminNotificationEventType,
            NotificationLevel,
        )
        from app.services import notification_service

        await notification_service.create_for_admins(
            source_user_id=user.id,
            event_type=AdminNotificationEventType.DEPOSIT_SUBMITTED,
            level=NotificationLevel.INFO,
            title=f"New deposit from {user.full_name}",
            message=(
                f"₹{payload.amount} via {payload.payment_mode.upper()}"
                + (f" · UTR {payload.utr_number}" if payload.utr_number else "")
            ),
            link="/payments?tab=deposits",
            reference_type="DepositRequest",
            reference_id=str(req.id),
            data={"amount": str(payload.amount), "payment_mode": payload.payment_mode},
        )
    except Exception:  # pragma: no cover
        pass
    return APIResponse(data={"id": str(req.id), "status": req.status.value})


@router.get("/deposits", response_model=APIResponse[list])
async def my_deposits(user: CurrentUser):
    rows = await DepositRequest.find(DepositRequest.user_id == user.id).sort("-created_at").limit(100).to_list()
    return APIResponse(
        data=[
            {
                "id": str(r.id),
                "amount": str(r.amount),
                "payment_mode": r.payment_mode.value,
                "utr_number": r.utr_number,
                "screenshot_url": r.screenshot_url,
                "status": r.status.value,
                "user_remark": r.user_remark,
                "admin_remark": r.admin_remark,
                "created_at": r.created_at,
                "processed_at": r.processed_at,
            }
            for r in rows
        ]
    )


@router.delete("/deposits/{deposit_id}", response_model=APIResponse[dict])
async def delete_deposit(deposit_id: str, user: CurrentUser):
    """Delete the user's OWN still-PENDING deposit request. Once an admin has
    approved/rejected it the request is immutable (money moved / audit trail),
    so only `PENDING` can be removed."""
    try:
        oid = PydanticObjectId(deposit_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Deposit request not found")
    req = await DepositRequest.find_one(
        DepositRequest.id == oid, DepositRequest.user_id == user.id
    )
    if req is None:
        raise HTTPException(status_code=404, detail="Deposit request not found")
    if req.status != DepositStatus.PENDING:
        raise HTTPException(
            status_code=400, detail="Only a pending request can be deleted"
        )
    await req.delete()
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "deposit_update",
            {"event": "deleted", "user_id": str(user.id), "deposit_id": deposit_id},
        )
    except Exception:  # noqa: BLE001
        pass
    return APIResponse(data={"deleted": True})


@router.post("/withdrawals", response_model=APIResponse[dict])
async def create_withdrawal(payload: WithdrawalCreate, user: CurrentUser):
    if getattr(user, "is_demo", False):
        raise HTTPException(status_code=403, detail="Demo accounts cannot withdraw funds. Open a real account to trade with real money.")
    # Enforce the effective withdrawal rule (same cascade as deposits).
    # Mandatory_remark + day/time window matter more here in practice —
    # most brokers gate withdrawals to working days + a daytime window.
    from app.services import wd_rules_service

    await wd_rules_service.validate_request(
        user_id=user.id,
        rule_type="WITHDRAWAL",
        amount=float(payload.amount),
        user_remark=payload.remarks,
    )

    # Open-position gate (admin-configurable, per-pool, default OFF). When the
    # user's effective WITHDRAWAL rule has `block_withdrawal_with_open_positions`
    # turned on by their owning admin / super-admin, they must flatten every
    # open trade before they can raise a withdrawal request.
    _wd_rule = await wd_rules_service.get_effective_rule(user.id, "WITHDRAWAL")
    if _wd_rule.get("block_withdrawal_with_open_positions"):
        from app.models.position import Position, PositionStatus

        _open_count = await Position.find(
            Position.user_id == user.id,
            Position.status == PositionStatus.OPEN,
        ).count()
        if _open_count > 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "You have open positions. Close all your open trades "
                    "before requesting a withdrawal."
                ),
            )

    # Duplicate-pending gate (admin-configurable, per-pool, default OFF). When the
    # user's effective WITHDRAWAL rule has `block_duplicate_pending` turned on,
    # they cannot raise a new withdrawal request while a PENDING one is still in
    # the admin queue. Reuses `_wd_rule` resolved just above.
    if _wd_rule.get("block_duplicate_pending"):
        _pending_wd = await WithdrawalRequest.find(
            WithdrawalRequest.user_id == user.id,
            WithdrawalRequest.status == WithdrawalStatus.PENDING,
        ).count()
        if _pending_wd > 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "You already have a pending withdrawal request. "
                    "Please wait for it to be processed."
                ),
            )

    # Per-day request cap (admin-configurable, per-pool, default 0 = no cap).
    # Counts EVERY withdrawal request this user created today (IST day,
    # regardless of status) so a user can't spam the admin queue. Reuses the
    # `_wd_rule` resolved above.
    _wd_max_req = int(_wd_rule.get("max_requests_per_day") or 0)
    if _wd_max_req > 0:
        from app.utils.time_utils import start_of_day_ist, to_utc

        _wd_today = await WithdrawalRequest.find(
            WithdrawalRequest.user_id == user.id,
            WithdrawalRequest.created_at >= to_utc(start_of_day_ist()),
        ).count()
        if _wd_today >= _wd_max_req:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Daily withdrawal request limit reached "
                    f"({_wd_max_req} per day). Try again tomorrow."
                ),
            )

    # Bonus wager gate (Bonus Management, gated). Block withdrawal while any
    # ACTIVE bonus still has an unmet wager target (>0). 409 + machine-readable
    # blocked_by so the UI can explain "trade ₹X more to unlock".
    from app.core.config import settings as _bset

    if _bset.BONUSES_ENABLED:
        from app.models.user_bonus import UserBonus, UserBonusStatus
        from app.utils.decimal_utils import to_decimal as _td

        _active = await UserBonus.find(
            UserBonus.user_id == user.id, UserBonus.status == UserBonusStatus.ACTIVE
        ).to_list()
        _blocked = []
        for _b in _active:
            _target = _td(_b.wager_target_volume)
            _prog = _td(_b.wager_progress_volume)
            if _target > 0 and _prog < _target:
                _blocked.append({
                    "bonus_id": str(_b.id),
                    "name": _b.template_name_snapshot,
                    "wager_remaining": str(_target - _prog),
                })
        if _blocked:
            raise HTTPException(
                status_code=409,
                detail={"code": "BONUS_WAGER_UNMET", "blocked_by": _blocked},
            )

    # Balance pre-check — reject immediately if user doesn't have
    # enough available_balance.  Without this, requests would sit in
    # the admin queue and only fail (or worse, book settlement
    # outstanding) at approval time.
    from app.models.wallet import Wallet
    from app.utils.decimal_utils import to_decimal

    wallet = await Wallet.find_one(Wallet.user_id == user.id)
    if wallet:
        avail = to_decimal(wallet.available_balance)
        req_amt = to_decimal(payload.amount)
        if avail < req_amt:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient balance: available ₹{avail:,.2f}, requested ₹{req_amt:,.2f}",
            )

    b = payload.bank or {}
    upi_id = (b.get("upi_id") or "").strip()
    account_number = (b.get("account_number") or "").strip()

    # UPI payout channel gate (admin-configurable). When the admin disabled UPI
    # withdrawals for this pool, drop any UPI id — the user must use a bank.
    if upi_id and not _wd_rule.get("allow_upi_payout", True):
        upi_id = ""
        if not (account_number and (b.get("ifsc") or "").strip()):
            raise HTTPException(
                status_code=400,
                detail="UPI withdrawals are currently disabled. Please withdraw to a bank account.",
            )

    # Require ONE of: a valid bank set (account+ifsc) OR a UPI ID. We do
    # not require the user to save the destination — every withdrawal
    # carries its own snapshot so they can pay to a different account
    # any time without managing a saved-banks list.
    if not upi_id and not (account_number and (b.get("ifsc") or "").strip()):
        raise HTTPException(
            status_code=400,
            detail="Provide either a UPI ID or full bank details (account number + IFSC).",
        )

    # Bank-required gate (admin-configurable, per-pool, default OFF). When the
    # user's effective WITHDRAWAL rule turns `require_bank_details` on, full bank
    # details are MANDATORY — a UPI ID alone won't do. `_wd_rule` resolved above.
    if _wd_rule.get("require_bank_details") and not (
        account_number and (b.get("ifsc") or "").strip()
    ):
        raise HTTPException(
            status_code=400,
            detail="Bank details (account holder, account number + IFSC) are required for withdrawals.",
        )

    snap = BankSnapshot(
        name=(b.get("name") or "").strip() or None,
        account_number=account_number or None,
        ifsc=(b.get("ifsc") or "").strip().upper() or None,
        holder=(b.get("holder") or "").strip() or (user.full_name if account_number else None),
        branch=(b.get("branch") or "").strip() or None,
        account_type=(b.get("account_type") or "").strip() or None,
        upi_id=upi_id or None,
        qr_url=(b.get("qr_url") or "").strip() or None,
    )
    # Idempotency: a client-supplied key dedups double / triple clicks and
    # retried-after-timeout requests — return the existing withdrawal instead
    # of creating a duplicate payout. Falls back to a fresh UUID when absent.
    client_idem = (payload.idempotency_key or "").strip() or None
    if client_idem:
        dup = await WithdrawalRequest.find_one(
            WithdrawalRequest.user_id == user.id,
            WithdrawalRequest.idempotency_key == client_idem,
        )
        if dup is not None:
            return APIResponse(data={"id": str(dup.id), "status": dup.status.value})
    idem = client_idem or uuid.uuid4().hex
    req = WithdrawalRequest(
        user_id=user.id,
        amount=to_decimal128(payload.amount),
        bank=snap,
        remarks=payload.remarks,
        status=WithdrawalStatus.PENDING,
        idempotency_key=idem,
    )
    await req.insert()
    # Scope-aware push + WS fan-out — see deposit hook for rationale.
    recipient_ids: list[str] = []
    try:
        import asyncio as _asyncio

        from app.services.push_service import (
            _compute_recipient_admin_ids as _compute_owners,
            send_to_user_owners as _push_owners,
        )

        owners = await _compute_owners(user.id)
        recipient_ids = [str(x) for x in owners]
        _asyncio.create_task(
            _push_owners(
                user.id,
                title="🏦 New withdrawal request",
                body=f"₹{payload.amount} · {user.full_name or user.user_code}",
                url="/payments?tab=withdrawals",
                tag=f"withdrawal-{req.id}",
            )
        )
    except Exception:  # pragma: no cover
        pass
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "withdrawal_update",
            {
                "event": "submitted",
                "user_id": str(user.id),
                "withdrawal_id": str(req.id),
                "user_name": user.full_name,
                "user_code": user.user_code,
                "amount": str(payload.amount),
                "recipient_admin_ids": recipient_ids,
            },
        )
    except Exception:  # pragma: no cover
        pass
    # Admin notification bell — see deposit-submit hook for rationale.
    try:
        from app.models.notification import (
            AdminNotificationEventType,
            NotificationLevel,
        )
        from app.services import notification_service

        dest_label = (
            f"UPI {snap.upi_id}"
            if snap.upi_id
            else f"A/C {snap.account_number} ({snap.ifsc or '—'})"
        )
        await notification_service.create_for_admins(
            source_user_id=user.id,
            event_type=AdminNotificationEventType.WITHDRAWAL_SUBMITTED,
            level=NotificationLevel.WARNING,
            title=f"Withdrawal request from {user.full_name}",
            message=f"₹{payload.amount} → {dest_label}",
            link="/payments?tab=withdrawals",
            reference_type="WithdrawalRequest",
            reference_id=str(req.id),
            data={
                "amount": str(payload.amount),
                "destination": dest_label,
            },
        )
    except Exception:  # pragma: no cover
        pass
    return APIResponse(data={"id": str(req.id), "status": req.status.value})


@router.get("/withdrawals", response_model=APIResponse[list])
async def my_withdrawals(user: CurrentUser):
    rows = await WithdrawalRequest.find(WithdrawalRequest.user_id == user.id).sort("-created_at").limit(100).to_list()
    return APIResponse(
        data=[
            {
                "id": str(r.id),
                "amount": str(r.amount),
                "bank": r.bank.model_dump(),
                "status": r.status.value,
                "remarks": r.remarks,
                "utr_number": r.utr_number,
                "rejection_reason": r.rejection_reason,
                "created_at": r.created_at,
                "processed_at": r.processed_at,
            }
            for r in rows
        ]
    )


@router.delete("/withdrawals/{withdrawal_id}", response_model=APIResponse[dict])
async def delete_withdrawal(withdrawal_id: str, user: CurrentUser):
    """Delete the user's OWN still-PENDING withdrawal request. A pending
    request hasn't touched the balance yet (funds are debited only at admin
    approval), so removal is money-neutral; approved/rejected requests are
    immutable."""
    try:
        oid = PydanticObjectId(withdrawal_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Withdrawal request not found")
    req = await WithdrawalRequest.find_one(
        WithdrawalRequest.id == oid, WithdrawalRequest.user_id == user.id
    )
    if req is None:
        raise HTTPException(status_code=404, detail="Withdrawal request not found")
    if req.status != WithdrawalStatus.PENDING:
        raise HTTPException(
            status_code=400, detail="Only a pending request can be deleted"
        )
    await req.delete()
    try:
        from app.services.admin_events import publish_admin_event

        await publish_admin_event(
            "withdrawal_update",
            {"event": "deleted", "user_id": str(user.id), "withdrawal_id": withdrawal_id},
        )
    except Exception:  # noqa: BLE001
        pass
    return APIResponse(data={"deleted": True})


@router.get("/wd-rules", response_model=APIResponse[dict])
async def my_wd_rules(user: CurrentUser):
    """Effective deposit + withdrawal rules for the calling user — resolved
    through the tier cascade (broker pool → admin pool → super-admin pool →
    global). Used by the user-side wallet UI to render the inline rules
    banner ("min ₹100, ₹10k daily, Mon–Fri 10–18 IST") so the user knows
    exactly what's allowed before they submit a request.

    Returns both rules in one payload to save a round trip — the wallet
    page typically renders the deposit info card and withdrawal info card
    side by side. Fields that the cascade left unset are still populated
    via the platform-global default, so the UI never has to handle nulls.
    """
    from app.services import wd_rules_service

    def _ser(values: dict) -> dict:
        out: dict = {}
        for k, v in values.items():
            if v is None:
                out[k] = None
            elif k == "allowed_days":
                out[k] = list(v) if v else None
            elif k == "allowed_times":
                out[k] = [
                    w.model_dump() if hasattr(w, "model_dump") else dict(w)
                    for w in v
                ] if v else None
            elif k == "charges_percent":
                out[k] = float(v)
            elif k in (
                "mandatory_remark",
                "block_withdrawal_with_open_positions",
                "block_duplicate_pending",
                "require_bank_details",
                "allow_upi_payout",
            ):
                out[k] = bool(v)
            else:
                out[k] = str(v)
        return out

    dep = await wd_rules_service.get_effective_rule(user.id, "DEPOSIT")
    wd = await wd_rules_service.get_effective_rule(user.id, "WITHDRAWAL")
    return APIResponse(data={"deposit": _ser(dep), "withdrawal": _ser(wd)})


def _serialize_bank(r: UserBankAccount) -> dict:
    return {
        "id": str(r.id),
        "method_type": r.method_type,
        "bank_name": r.bank_name,
        "account_holder": r.account_holder,
        "account_number": r.account_number,
        "ifsc_code": r.ifsc_code,
        "upi_id": r.upi_id,
        "is_default": r.is_default,
        "is_verified": r.is_verified,
        "nickname": r.nickname,
    }


async def _unset_other_primaries(user_id, method_type: str, keep_id) -> None:
    """Only ONE primary per channel — clear is_default on the user's other
    rows of the same method_type."""
    await UserBankAccount.get_motor_collection().update_many(
        {"user_id": user_id, "method_type": method_type, "_id": {"$ne": keep_id}},
        {"$set": {"is_default": False}},
    )


@router.get("/bank-accounts", response_model=APIResponse[list])
async def my_bank_accounts(user: CurrentUser):
    """The user's saved payout methods (banks + UPIs), primaries first."""
    rows = (
        await UserBankAccount.find(UserBankAccount.user_id == user.id)
        .sort("-is_default")
        .to_list()
    )
    return APIResponse(data=[_serialize_bank(r) for r in rows])


@router.post("/bank-accounts", response_model=APIResponse[dict])
async def add_bank_account(payload: dict, user: CurrentUser):
    """Save a new bank account or UPI id. The first method saved of a given
    channel auto-becomes that channel's primary; `is_default=true` forces it."""
    method = str(payload.get("method_type") or "BANK").upper()
    if method not in ("BANK", "UPI"):
        raise HTTPException(status_code=400, detail="Invalid payment method type.")

    holder = (payload.get("account_holder") or user.full_name or "").strip()
    if method == "UPI":
        upi = (payload.get("upi_id") or "").strip()
        if "@" not in upi or len(upi) < 3:
            raise HTTPException(status_code=400, detail="Enter a valid UPI id (e.g. name@bank).")
        row = UserBankAccount(
            user_id=user.id,
            method_type="UPI",
            upi_id=upi,
            account_number=upi,  # VPA anchors the unique (user_id, account_number) index
            account_holder=holder,
            nickname=(payload.get("nickname") or None),
        )
    else:
        acc = (payload.get("account_number") or "").strip()
        ifsc = (payload.get("ifsc_code") or "").strip().upper()
        if not acc or not ifsc or not holder:
            raise HTTPException(
                status_code=400,
                detail="Account holder, account number and IFSC are required.",
            )
        row = UserBankAccount(
            user_id=user.id,
            method_type="BANK",
            bank_name=(payload.get("bank_name") or "").strip(),
            account_holder=holder,
            account_number=acc,
            ifsc_code=ifsc,
            branch=(payload.get("branch") or None),
            nickname=(payload.get("nickname") or None),
        )

    existing = await UserBankAccount.find(
        UserBankAccount.user_id == user.id,
        UserBankAccount.method_type == row.method_type,
    ).count()
    make_primary = bool(payload.get("is_default")) or existing == 0
    row.is_default = make_primary
    try:
        await row.insert()
    except Exception:
        raise HTTPException(status_code=400, detail="This account / UPI is already saved.")
    if make_primary:
        await _unset_other_primaries(user.id, row.method_type, row.id)
    return APIResponse(data=_serialize_bank(row))


@router.post("/bank-accounts/{account_id}/primary", response_model=APIResponse[dict])
async def set_primary_bank_account(account_id: str, user: CurrentUser):
    try:
        oid = PydanticObjectId(account_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Payment method not found")
    row = await UserBankAccount.get(oid)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Payment method not found")
    row.is_default = True
    await row.save()
    await _unset_other_primaries(user.id, row.method_type, row.id)
    return APIResponse(data={"id": account_id, "is_default": True})


@router.delete("/bank-accounts/{account_id}", response_model=APIResponse[dict])
async def delete_bank_account(account_id: str, user: CurrentUser):
    try:
        oid = PydanticObjectId(account_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Payment method not found")
    row = await UserBankAccount.get(oid)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Payment method not found")
    was_primary, method = row.is_default, row.method_type
    await row.delete()
    # Keep a primary alive: promote another method of the same channel.
    if was_primary:
        nxt = await UserBankAccount.find_one(
            UserBankAccount.user_id == user.id,
            UserBankAccount.method_type == method,
        )
        if nxt is not None:
            nxt.is_default = True
            await nxt.save()
    return APIResponse(data={"id": account_id, "deleted": True})
