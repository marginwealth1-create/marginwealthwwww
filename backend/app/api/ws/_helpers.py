"""Shared WebSocket helpers — keep sends safe against closed sockets.

Without `safe_send_text`, a client disconnect that lands mid-frame on
the server causes `ws.send_text()` to raise

    RuntimeError("Cannot call 'send' once a close message has been sent.")

This is NOT a `WebSocketDisconnect`, so handler-level
`except WebSocketDisconnect` clauses miss it; the exception bubbles up
through Starlette's ASGI middleware and surfaces as repeated

    ERROR: Exception in ASGI application

in production logs. On flaky mobile networks this happens dozens of
times per minute (pubsub forward lands while the client TCP is being
torn down), each one tears down the handler, and the browser's
auto-reconnect compounds the load. End-user symptom: market data feed
freezes, admin WS reconnect-loop, trades won't open.

Same race exists in the heartbeat / pump tasks: they sleep, wake up,
try to send — but the parent handler has already finished and the
socket is closed. Wrapping every send in this helper kills both paths
at once.
"""

from __future__ import annotations

import logging

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)


async def safe_send_text(ws: WebSocket, payload: str) -> bool:
    """Send text only if the socket is still connected; swallow disconnects.

    Returns True on a successful send, False if the socket was already
    closed or the send raised mid-flight. Callers should treat False as
    "stop forwarding to this client" and exit their loop.
    """
    if ws.client_state != WebSocketState.CONNECTED:
        return False
    try:
        await ws.send_text(payload)
        return True
    except (WebSocketDisconnect, RuntimeError):
        # RuntimeError catches "Cannot call 'send' once a close message
        # has been sent" — the exact race that was crashing handlers.
        return False
    except Exception:  # pragma: no cover
        # Network IO can raise OSError / ConnectionResetError etc.
        # Same treatment — stop forwarding, don't let it escape.
        return False


def is_connected(ws: WebSocket) -> bool:
    return ws.client_state == WebSocketState.CONNECTED
