"""Admin Zerodha integration endpoints — mirrors the bharat_indian_funded
ZerodhaConnect.jsx contract one-to-one so the same admin UI can drive it."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from app.core.config import settings as app_settings
from app.core.dependencies import CurrentAdmin
from app.models.user import UserRole
from app.services.zerodha_service import (
    FAILOVER_CMD_CHANNEL,
    FAILOVER_STATUS_KEY,
    zerodha,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/zerodha", tags=["admin-zerodha"])


def _admin_redirect(suffix: str) -> str:
    """Build the post-OAuth admin SPA URL using the configured admin origin
    so a localhost-only dev setup and a deployed admin both work."""
    base = (app_settings.CORS_ADMIN_ORIGIN or "http://localhost:3001").rstrip("/")
    return f"{base}/zerodha{suffix}"


# ─────────────────────────── Settings ────────────────────────────────


@router.get("/settings")
async def get_settings(
    admin: CurrentAdmin,
    account: int = Query(default=0, ge=0, le=1),
):
    return {"success": True, "settings": await zerodha.get_settings_full(account)}


@router.post("/settings")
async def update_settings(
    payload: dict[str, Any],
    admin: CurrentAdmin,
    account: int = Query(default=0, ge=0, le=1),
):
    await zerodha.update_settings(payload, account)
    return {"success": True, "settings": await zerodha.get_settings_full(account)}


# ─────────────────── Feed routing & HA failover ──────────────────────


def _routing_dict(cfg) -> dict[str, Any]:
    return {
        "exchange_account_map": cfg.exchange_account_map,
        "failover_enabled": cfg.failover_enabled,
        "failover_confirm_down_sec": cfg.failover_confirm_down_sec,
        "failback_confirm_up_sec": cfg.failback_confirm_up_sec,
        "health_stale_sec": cfg.health_stale_sec,
    }


@router.get("/routing")
async def get_routing(admin: CurrentAdmin):
    """Exchange→account map + failover knobs + LIVE health/route snapshot."""
    from app.models.zerodha_feed_routing import ZerodhaFeedRouting

    cfg = await ZerodhaFeedRouting.find_one()
    if cfg is None:
        cfg = ZerodhaFeedRouting()
        try:
            await cfg.insert()
        except Exception:
            logger.debug("routing_seed_failed", exc_info=True)
    # Live status lives in the FEED process; it publishes to Redis. Read it
    # here (backend worker) so the panel shows real health across processes.
    from app.core.redis_client import cache_get

    live = await cache_get(FAILOVER_STATUS_KEY)
    if not live:
        live = zerodha.get_failover_status()  # fallback if feed hasn't published
    return {"success": True, "config": _routing_dict(cfg), "live": live}


@router.put("/routing")
async def update_routing(payload: dict[str, Any], admin: CurrentAdmin):
    """Update routing map / failover knobs. Super-admin only."""
    if getattr(admin, "role", None) != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super-admin only")
    from app.models.zerodha_feed_routing import ZerodhaFeedRouting

    cfg = await ZerodhaFeedRouting.find_one() or ZerodhaFeedRouting()
    m = payload.get("exchange_account_map")
    if isinstance(m, dict):
        clean: dict[str, int] = {}
        for k, v in m.items():
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv in (0, 1):
                clean[str(k).upper()] = iv
        if clean:
            cfg.exchange_account_map = clean
    if "failover_enabled" in payload:
        cfg.failover_enabled = bool(payload["failover_enabled"])
    for f in ("failover_confirm_down_sec", "failback_confirm_up_sec", "health_stale_sec"):
        if f in payload:
            try:
                setattr(cfg, f, max(1, int(payload[f])))
            except (TypeError, ValueError):
                pass
    await cfg.save()
    return {"success": True, "config": _routing_dict(cfg)}


@router.post("/routing/test-disconnect")
async def routing_test_disconnect(
    admin: CurrentAdmin,
    account: int = Query(ge=0, le=1),
):
    """OFF-MARKET TEST ONLY — tear down one account's WS (account-scoped) to
    exercise failover. Its tokens re-home onto the survivor; the failover loop
    then re-routes and self-heal reconnects the account. Super-admin only."""
    if getattr(admin, "role", None) != UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super-admin only")
    # The WS pool lives in the FEED process; publish the command for its
    # listener to execute (backend workers have no pool to disconnect).
    from app.core.redis_client import publish

    await publish(FAILOVER_CMD_CHANNEL, {"action": "disconnect", "account": account})
    return {
        "success": True,
        "message": f"Drop Account {'A' if account == 0 else 'B'} sent to feed",
    }


@router.get("/status")
async def status(
    admin: CurrentAdmin,
    account: int = Query(default=0, ge=0, le=1),
):
    return {"success": True, "status": await zerodha.get_status(account)}


# ─────────────────────────── OAuth flow ──────────────────────────────


@router.get("/login-url")
async def login_url(
    admin: CurrentAdmin,
    account: int = Query(default=0, ge=0, le=1),
):
    try:
        return {"success": True, "loginUrl": await zerodha.get_login_url(account)}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/callback")
async def oauth_callback(
    request_token: str | None = Query(default=None),
    account: int = Query(default=0, ge=0, le=1),
):
    """Browser-facing OAuth callback. We accept the request_token, exchange
    it for an access_token, and redirect back to the admin SPA. No JWT here."""
    if not request_token:
        return RedirectResponse(url=_admin_redirect("?error=missing_request_token"))
    try:
        await zerodha.generate_session(request_token, account)
        suffix = f"?success=true&account={account}"
        return RedirectResponse(url=_admin_redirect(suffix))
    except Exception as e:  # pragma: no cover
        logger.exception("zerodha_callback_failed")
        return RedirectResponse(url=_admin_redirect(f"?error={str(e)[:200]}"))


@router.post("/connect-with-token")
async def connect_with_token(
    payload: dict[str, Any],
    admin: CurrentAdmin,
    account: int = Query(default=0, ge=0, le=1),
):
    """Manual fallback when the OAuth redirect can't reach the backend —
    paste the request_token from the Kite URL here."""
    rt = (payload.get("request_token") or "").strip()
    if not rt:
        raise HTTPException(status_code=400, detail="request_token is required")
    try:
        result = await zerodha.connect_with_token(rt, account)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "result": result}


@router.post("/logout")
async def logout(
    admin: CurrentAdmin,
    account: int = Query(default=0, ge=0, le=1),
):
    await zerodha.disconnect(account)
    return {"success": True}


# ─────────────────────────── WebSocket ──────────────────────────────


@router.post("/connect-ws")
async def connect_ws(admin: CurrentAdmin):
    try:
        await zerodha.connect_ws()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "status": await zerodha.get_status()}


@router.post("/disconnect-ws")
async def disconnect_ws(admin: CurrentAdmin):
    await zerodha.disconnect_ws()
    return {"success": True}


@router.post("/force-reconnect-ws")
async def force_reconnect_ws(admin: CurrentAdmin):
    """Operator-friendly "act like backend restart" reconnect.

    Same effect as `systemctl restart marginwealth-backend` for the
    Zerodha ticker — stops the live ticker, resets the self-heal
    failure counter (so the next reconnect uses the base 30 s cadence
    instead of the 5 min cap), refreshes the captured event loop, and
    then runs `connect_ws(force=True)`. Admin can drive this from the
    Zerodha page after a daily manual login to skip the wait for
    self-heal to climb back down.
    """
    try:
        await zerodha.force_reconnect_ws()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "status": await zerodha.get_status()}


# ─────────────────────────── Instruments ────────────────────────────


@router.get("/instruments/search")
async def search_instruments(
    admin: CurrentAdmin,
    query: str = Query(..., min_length=1),
    segment: str | None = Query(default=None),
):
    try:
        results = await zerodha.search_instruments(query, segment)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "instruments": results}


@router.get("/instruments/subscribed")
async def list_subscribed(admin: CurrentAdmin):
    return {"success": True, "instruments": await zerodha.get_subscribed()}


@router.post("/instruments/subscribe-defaults")
async def subscribe_defaults(admin: CurrentAdmin):
    """Bulk-subscribe the curated 600-instrument default set: NSE top 100
    equities + indices + NIFTY/BANKNIFTY/FINNIFTY current+next expiry option
    chains around ATM. Resolved against the local Instrument catalogue so
    lot sizes / tick sizes come straight from the Kite contract file. Safe
    to re-run — `add_subscriptions_bulk` skips already-subscribed tokens."""
    try:
        added = await zerodha._auto_load_default_subscriptions()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Default seed failed: {e}")
    return {"success": True, "added": added, "status": await zerodha.get_status()}


@router.post("/instruments/subscribe-all")
async def subscribe_all(payload: dict[str, Any] | None = None, admin: CurrentAdmin = None):
    """Subscribe ALL instruments from specified exchanges on-demand via
    the multi-WebSocket pool. Instruments are held IN-MEMORY only (not
    saved to MongoDB). New WebSocket connections spawn automatically
    when existing ones hit the 3000-token limit.

    Body (optional): {"exchanges": ["NSE", "NFO", "MCX"]}
    Default: all exchanges (NSE, NFO, BSE, MCX, BFO)."""
    exchanges = None
    if payload and "exchanges" in payload:
        exchanges = payload["exchanges"]
    try:
        result = await zerodha.subscribe_all_instruments(exchanges)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Subscribe all failed: {e}")
    return {
        "success": True,
        **result,
        "ws_pool": zerodha.get_ws_pool_info(),
        "status": await zerodha.get_status(),
    }


@router.get("/ws-pool")
async def ws_pool_info(admin: CurrentAdmin):
    """Return multi-WebSocket pool diagnostics: connections, tokens per
    connection, capacity etc."""
    return {"success": True, "pool": zerodha.get_ws_pool_info()}


@router.get("/instruments/all")
async def list_all_cached(
    admin: CurrentAdmin,
    exchange: str | None = Query(default=None),
):
    return {
        "success": True,
        "instruments": await zerodha.get_all_cached_instruments(exchange),
    }


@router.post("/instruments/sync")
async def sync_instruments(admin: CurrentAdmin):
    """Drop in-memory CSV cache + remove expired subscribed instruments. The
    next search will fetch fresh CSV from Kite."""
    info = await zerodha.sync_instrument_cache()
    return {"success": True, **info}


@router.post("/instruments/clear")
async def clear_instruments(admin: CurrentAdmin):
    """Wipe subscribed instruments and the cache. The ticker is unsubscribed."""
    removed = await zerodha.clear_subscriptions_and_cache()
    return {"success": True, "cleared": removed}


@router.post("/instruments/trim")
async def trim_instruments(payload: dict[str, Any], admin: CurrentAdmin):
    """LRU-trim subscribed instruments to `keep_count` (default 700).
    Preserves tokens with open positions and LRU-exempt tokens.
    Frees the WS pool of stale option-chain mirror subscriptions."""
    keep_count = int(payload.get("keep_count") or 700)
    keep_count = max(50, min(3000, keep_count))
    result = await zerodha.trim_subscriptions_lru(keep_count)
    return {"success": True, **result}


@router.post("/instruments/subscribe")
async def subscribe_instrument(payload: dict[str, Any], admin: CurrentAdmin):
    inst = payload.get("instrument") or payload
    try:
        added = await zerodha.add_subscription(inst)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "added": added}


@router.post("/instruments/subscribe-bulk")
async def subscribe_bulk(payload: dict[str, Any], admin: CurrentAdmin):
    instruments = payload.get("instruments") or []
    if not isinstance(instruments, list):
        raise HTTPException(status_code=400, detail="instruments must be a list")
    count = await zerodha.add_subscriptions_bulk(instruments)
    return {"success": True, "count": count}


@router.delete("/instruments/{token}")
async def unsubscribe_instrument(token: int, admin: CurrentAdmin):
    removed = await zerodha.remove_subscription(token)
    if not removed:
        raise HTTPException(status_code=404, detail="Instrument not found in subscriptions")
    return {"success": True}


@router.get("/instruments/exchange/{exchange}")
async def list_for_exchange(exchange: str, admin: CurrentAdmin):
    """Fetch (and cache) every instrument for an exchange — used by the admin
    "Subscribe all from exchange" button."""
    try:
        instruments = await zerodha.fetch_instruments(exchange.upper())
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "instruments": instruments}


# ─────────────────────────── Quotes / history ───────────────────────


@router.get("/quote")
async def get_quote(
    admin: CurrentAdmin,
    keys: str = Query(..., description="comma-separated Kite keys e.g. NSE:RELIANCE,NSE:TCS"),
):
    parts = [k.strip() for k in keys.split(",") if k.strip()]
    try:
        return {"success": True, "quotes": await zerodha.get_quote(parts)}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/historical")
async def get_historical(
    admin: CurrentAdmin,
    token: int,
    interval: str = Query(default="5minute"),
    days: int = Query(default=5, ge=1, le=365),
):
    from datetime import datetime, timedelta, timezone

    to_dt = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=days)
    try:
        candles = await zerodha.get_historical(token, from_dt, to_dt, interval)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "candles": candles}


@router.get("/historical/symbol/{symbol}")
async def get_historical_by_symbol(
    symbol: str,
    admin: CurrentAdmin,
    interval: str = Query(default="5minute"),
    days: int = Query(default=5, ge=1, le=365),
):
    """Resolve symbol → instrument_token, then return Kite historical candles."""
    inst = await zerodha.find_instrument_by_symbol(symbol)
    if not inst:
        raise HTTPException(status_code=404, detail=f"Symbol '{symbol}' not found in Zerodha catalogue")

    from datetime import datetime, timedelta, timezone

    to_dt = datetime.now(timezone.utc)
    from_dt = to_dt - timedelta(days=days)
    try:
        candles = await zerodha.get_historical(int(inst["token"]), from_dt, to_dt, interval)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "instrument": inst, "candles": candles}


# ─────────────────────────── Debug ──────────────────────────────────


@router.get("/debug-csv")
async def debug_csv(
    admin: CurrentAdmin,
    exchange: str = Query(default="NFO"),
):
    """Quick smoke test: confirms credentials work by pulling one row from the
    instruments CSV. Returns the first parsed instrument."""
    try:
        return {"success": True, **await zerodha.debug_csv_sample(exchange)}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/diagnose")
async def diagnose(admin: CurrentAdmin):
    """End-to-end pipeline check: credentials, auth, ticker, REST quote, and
    instruments fetch — each graded independently. Use this to pinpoint why
    live data isn't flowing on the user terminal."""
    return {"success": True, "report": await zerodha.diagnose()}
