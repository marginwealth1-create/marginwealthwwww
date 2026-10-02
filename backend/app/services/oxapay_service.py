"""OxaPay crypto payment gateway (per-admin merchant key).

Uses the OxaPay v1 Merchant API:
  • Create invoice: POST https://api.oxapay.com/v1/payment/invoice
      header  merchant_api_key: <admin's key>
      body    {amount, currency, callback_url, return_url, order_id, lifetime}
      resp    {data: {track_id, payment_url, ...}}
  • Webhook: OxaPay POSTs the raw JSON to callback_url with an `HMAC` header =
      HMAC-SHA512(raw_body, merchant_api_key).hexdigest()   (verify below).

The merchant key is PER-ADMIN (passed in), never a global setting — each admin
routes their users' crypto to their own OxaPay account.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from decimal import Decimal

import httpx

logger = logging.getLogger("oxapay_service")

OXAPAY_INVOICE_URL = "https://api.oxapay.com/v1/payment/invoice"


async def create_invoice(
    *,
    api_key: str,
    amount: Decimal | float,
    currency: str,
    callback_url: str,
    return_url: str,
    order_id: str,
    description: str = "",
    lifetime: int = 60,
    sandbox: bool = False,
) -> dict:
    """Create an OxaPay invoice with THIS admin's merchant key. Returns
    ``{track_id, payment_url}``; raises ValueError on any API/config error."""
    if not api_key:
        raise ValueError("OxaPay merchant key not configured for this admin")

    payload: dict = {
        "amount": float(amount),
        "currency": currency,  # e.g. "INR" — OxaPay converts to crypto for the payer
        "callback_url": callback_url,
        "return_url": return_url,
        "order_id": order_id,
        "lifetime": lifetime,
        "description": description or f"Deposit {order_id[:8]}",
    }
    if sandbox:
        payload["sandbox"] = True

    headers = {"merchant_api_key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(OXAPAY_INVOICE_URL, json=payload, headers=headers)
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code >= 400:
            logger.error("oxapay_invoice_http_%s: %s", resp.status_code, data or resp.text)
            msg = (data.get("error") or {}).get("message") or data.get("message") or f"HTTP {resp.status_code}"
            raise ValueError(f"OxaPay: {msg}")

    d = (data or {}).get("data") or {}
    payment_url = d.get("payment_url")
    track_id = d.get("track_id")
    if not payment_url or not track_id:
        logger.error("oxapay_invoice_bad_response: %s", data)
        raise ValueError(f"OxaPay: {data.get('message') or 'invalid response'}")
    logger.info("oxapay_invoice_created order=%s track=%s", order_id, track_id)
    return {"track_id": str(track_id), "payment_url": payment_url}


def verify_webhook_signature(api_key: str | None, raw_body: bytes, received_hmac: str | None) -> bool:
    """HMAC-SHA512 of the raw request body keyed with the merchant key, compared
    (constant-time) to the `HMAC` header. Fail-closed: any missing/bad input →
    False, so a forged webhook can never credit a wallet."""
    if not api_key or not received_hmac or not raw_body:
        return False
    try:
        computed = hmac.new(api_key.encode(), raw_body, hashlib.sha512).hexdigest()
    except Exception:
        return False
    return hmac.compare_digest(computed, received_hmac)


def is_paid(status: str | None) -> bool:
    return str(status or "").strip().lower() == "paid"


def is_dead(status: str | None) -> bool:
    """Terminal non-paid states — the invoice will never be paid now."""
    return str(status or "").strip().lower() in ("expired", "failed", "cancelled", "canceled")
