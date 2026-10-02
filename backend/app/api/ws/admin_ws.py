"""Admin WebSocket channel — pushes platform-wide events live.

Auth: query params `?token=<admin_jwt>&key=<admin_api_key>` because
browsers can't add custom headers on a WebSocket handshake (so we mirror
the X-Admin-Api-Key check from `get_current_admin` via the query string).

Once authenticated the socket subscribes to a single global pub/sub
channel:

    admin:events   — every admin-relevant event across the platform
                     (position close, order fill, deposit submit/approve,
                     withdrawal submit/approve, KYC submit, etc.)

Any message published on that channel is forwarded as-is to the browser,
which then invalidates the right React Query cache and re-renders the
affected admin page without anyone hitting F5.

A single shared channel (rather than per-admin) keeps the publish path
cheap — emitters fire one message no matter how many admins are watching.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.api.ws._helpers import safe_send_text
from app.core.config import settings
from app.core.security import decode_token
from app.core.ws_hub import admin_event_hub
from app.core.ws_limiter import acquire as ws_limit_acquire
from app.core.ws_limiter import client_ip as ws_client_ip
from app.core.ws_limiter import release as ws_limit_release
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)
router = APIRouter()


# Roles that are allowed to attach to the admin WS. Mirrors the
# `ADMIN_ROLES` set used by `get_current_admin`. Kept local so a future
# scoped-broker view (only sees their users' events) can be added without
# touching the HTTP dependency.
_ADMIN_ROLES = {UserRole.SUPER_ADMIN, UserRole.ADMIN, UserRole.BROKER}


@router.websocket("/ws/admin")
async def admin_ws(
    ws: WebSocket,
    token: str = Query(...),
    key: str = Query(..., description="Admin API key — same value as the X-Admin-Api-Key header"),
):
    """Admin event WebSocket.

    Internal change: connects to the process-wide ``AdminEventHub``
    instead of opening a dedicated Redis pub/sub connection. The hub
    subscribes to ``admin:events`` once per worker and fans every
    message out to every attached admin socket. Auth, message format
    and heartbeat behaviour are unchanged.
    """
    # ── Auth ────────────────────────────────────────────────────────
    # 1) API-key gate (same value the HTTP dependency checks). Without
    #    this the admin role check below would be the only barrier — and
    #    the HTTP layer enforces both, so the WS should match.
    expected = settings.ADMIN_API_KEY.get_secret_value()
    if not expected or key != expected:
        await ws.close(code=4401)
        return

    # 2) JWT — admin access token, same shape as the HTTP bearer.
    try:
        payload = decode_token(token, expected_type="access")
        user_id = payload.get("sub")
    except Exception:
        await ws.close(code=4401)
        return
    if not user_id:
        await ws.close(code=4401)
        return

    # 3) Admin role gate (DB lookup so a freshly-demoted account can't
    #    keep a stale socket open). Beanie is initialised by the lifespan
    #    so a direct `.get` is safe here.
    try:
        from beanie import PydanticObjectId

        user = await User.get(PydanticObjectId(user_id))
    except Exception:
        await ws.close(code=4401)
        return
    if user is None or user.role not in _ADMIN_ROLES:
        await ws.close(code=4403)
        return

    # Per-IP rate limit — reject before accept() so a flooding client
    # doesn't get a slot. Code 4429 mirrors HTTP 429.
    ip = ws_client_ip(ws)
    if not await ws_limit_acquire(ip, max_per_ip=settings.WS_MAX_CONNECTIONS_PER_IP):
        await ws.close(code=4429)
        return

    await ws.accept()
    await safe_send_text(
        ws,
        json.dumps({"type": "hello", "user_id": str(user.id), "role": user.role.value}),
    )

    # Hub is started eagerly in the FastAPI lifespan; ``start()`` is
    # idempotent so a stray race here is still safe.
    try:
        await admin_event_hub.start()
    except Exception as e:  # pragma: no cover
        logger.warning("admin_ws_hub_start_failed", extra={"error": str(e)})
        await ws.close(code=4500)
        return

    admin_event_hub.attach(ws)

    async def heartbeat():
        try:
            while True:
                await asyncio.sleep(25)
                if not await safe_send_text(ws, json.dumps({"type": "heartbeat"})):
                    return
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except Exception:  # pragma: no cover
            return

    hb_task = asyncio.create_task(heartbeat())

    try:
        # The hub forwards admin events to this socket. We only need
        # to keep the connection alive and drain anything the client
        # sends (so receive_text raises WebSocketDisconnect on close).
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        return
    except Exception as e:  # pragma: no cover
        logger.warning("admin_ws_failed", extra={"error": str(e)})
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):  # pragma: no cover
            pass
        try:
            admin_event_hub.detach(ws)
        except Exception:  # pragma: no cover
            pass
        try:
            await ws_limit_release(ip)
        except Exception:  # pragma: no cover
            pass
