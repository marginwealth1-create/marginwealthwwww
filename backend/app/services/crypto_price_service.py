"""Live crypto ↔ INR conversion for the deposit screen.

Uses CoinGecko's free simple-price endpoint (no key) to show a user how much
crypto they must actually send for a given ₹ amount. Cached ~60 s so opening
the deposit sheet doesn't hammer the API. Best-effort: on any failure returns
the last cached value, else None (frontend then just shows the ₹ amount).
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger("crypto_price")

_COINGECKO = "https://api.coingecko.com/api/v3/simple/price"
_ASSET_ID: dict[str, str] = {
    "USDT": "tether", "USDC": "usd-coin", "BTC": "bitcoin", "ETH": "ethereum",
    "TRX": "tron", "SOL": "solana", "XRP": "ripple", "BNB": "binancecoin",
    "DOGE": "dogecoin", "ADA": "cardano", "MATIC": "matic-network",
    "LTC": "litecoin", "BCH": "bitcoin-cash", "XMR": "monero", "NOT": "notcoin",
    "DAI": "dai", "DOGS": "dogs-2",
}
_TTL = 60
_cache: dict[str, tuple[float, float]] = {}  # coingecko id -> (ts, inr_price)


async def inr_price(asset: str) -> float | None:
    cid = _ASSET_ID.get((asset or "").strip().upper())
    if not cid:
        return None
    now = time.time()
    cached = _cache.get(cid)
    if cached and now - cached[0] < _TTL:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(_COINGECKO, params={"ids": cid, "vs_currencies": "inr"})
            r.raise_for_status()
            price = float((r.json().get(cid) or {}).get("inr") or 0)
    except Exception:
        logger.debug("crypto_price_fetch_failed asset=%s", asset, exc_info=True)
        return cached[1] if cached else None
    if price > 0:
        _cache[cid] = (now, price)
        return price
    return cached[1] if cached else None


async def convert_inr_to_asset(amount_inr: float, asset: str) -> dict | None:
    """{asset, inr_amount, inr_per_unit, crypto_amount} or None if no rate."""
    p = await inr_price(asset)
    if not p or p <= 0 or amount_inr <= 0:
        return None
    return {
        "asset": (asset or "").strip().upper(),
        "inr_amount": round(amount_inr, 2),
        "inr_per_unit": round(p, 4),
        "crypto_amount": round(amount_inr / p, 8),
    }
