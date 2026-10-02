"""Process-wide WebSocket fan-out hubs.

The original design opened a dedicated Redis pub/sub connection per
connected WebSocket — one for ``/ws/marketdata``, one for ``/ws/user``,
one for ``/ws/admin``. With 100 K concurrent users the platform would
need ~200 K Redis connections (the default pool tops out at 300), so
the WS layer fell over long before the matching engine became the
bottleneck.

This module replaces that pattern with three singleton hubs — one per
WS endpoint type. Each hub:

  * Opens **exactly one** Redis pub/sub connection for the whole worker.
  * Maintains an in-memory ``key -> set[WebSocket]`` map. The "key" is
    a market-data token (``MarketTickHub``), a user_id (``UserChannelHub``)
    or just a constant (``AdminEventHub``).
  * Runs a single listener task that decodes each pub/sub message once
    and fans it out concurrently to every subscribed socket via
    ``safe_send_text``. Failed sends auto-unsubscribe their socket from
    every key the hub knows about, so a dead client never lingers.

This is **pure plumbing** — the wire-level WebSocket protocol the
clients see, the message shapes, the channel names, the subscribe /
unsubscribe semantics and the heartbeat/snapshot pumps are all
unchanged. Only the in-process routing is consolidated.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

from app.core.redis_client import pubsub

# ``safe_send_text`` is imported lazily inside ``_BaseHub._listen_loop``
# because eager import would close a circular reference via
# ``app.api.ws.__init__`` (which imports the WS handler modules, each of
# which imports this hub module). Lazy import is fine: the helper is
# only needed once a pub/sub message arrives, well after both modules
# have finished initialising.

logger = logging.getLogger(__name__)


def _decode_pubsub_msg(msg: dict[str, Any]) -> tuple[str | None, Any]:
    """Common pub/sub message decoder. Returns (channel, parsed_payload).

    ``parsed_payload`` is the JSON-decoded dict when possible; falls back
    to the raw string wrapped as ``{"data": raw}`` so the downstream
    forwarder can always send a JSON-serialisable object."""
    if msg.get("type") not in ("message", "pmessage"):
        return None, None
    raw_channel = msg.get("channel")
    if isinstance(raw_channel, bytes):
        try:
            channel = raw_channel.decode("utf-8")
        except UnicodeDecodeError:
            channel = None
    else:
        channel = raw_channel if isinstance(raw_channel, str) else None

    raw_data = msg.get("data")
    if isinstance(raw_data, bytes):
        try:
            raw_data = raw_data.decode("utf-8")
        except UnicodeDecodeError:
            return channel, None
    if not raw_data:
        return channel, None
    try:
        parsed = json.loads(raw_data)
    except (ValueError, TypeError):
        parsed = {"data": raw_data}
    return channel, parsed


class _BaseHub:
    """Shared lifecycle + subscriber-map plumbing."""

    name: str = "hub"

    def __init__(self) -> None:
        self._subscribers: dict[str, set[WebSocket]] = {}
        self._ws_meta: dict[WebSocket, Any] = {}
        self._lock = asyncio.Lock()
        self._listener_task: asyncio.Task | None = None
        self._pubsub: Any = None
        self._started = False

    # ── lifecycle ──────────────────────────────────────────────────
    async def start(self) -> None:
        """Idempotent. Opens the shared pub/sub connection and spawns
        the listener task. Called from the FastAPI lifespan so the hub
        is ready before the first WebSocket arrives."""
        async with self._lock:
            if self._started:
                return
            self._pubsub = pubsub()
            await self._do_subscribe(self._pubsub)
            self._listener_task = asyncio.create_task(
                self._listen_loop(), name=f"{self.name}_listener"
            )
            self._started = True
            logger.info("ws_hub_started", extra={"hub": self.name})

    async def stop(self) -> None:
        async with self._lock:
            if not self._started:
                return
            self._started = False
            if self._listener_task is not None:
                self._listener_task.cancel()
                try:
                    await self._listener_task
                except (asyncio.CancelledError, Exception):  # pragma: no cover
                    pass
                self._listener_task = None
            if self._pubsub is not None:
                try:
                    await self._do_unsubscribe(self._pubsub)
                    await self._pubsub.close()
                except Exception:  # pragma: no cover
                    pass
                self._pubsub = None
            self._subscribers.clear()
            logger.info("ws_hub_stopped", extra={"hub": self.name})

    # ── subscriber map ─────────────────────────────────────────────
    def add(self, key: str, ws: WebSocket) -> None:
        self._subscribers.setdefault(key, set()).add(ws)

    def remove(self, key: str, ws: WebSocket) -> None:
        s = self._subscribers.get(key)
        if not s:
            return
        s.discard(ws)
        if not s:
            self._subscribers.pop(key, None)

    def remove_ws(self, ws: WebSocket) -> None:
        """Drop ``ws`` from every key it was subscribed to. O(K) where K
        is the number of distinct keys this hub holds — small in practice
        because each WS only attaches to the keys it cares about."""
        self._ws_meta.pop(ws, None)
        empty: list[str] = []
        for key, s in self._subscribers.items():
            s.discard(ws)
            if not s:
                empty.append(key)
        for key in empty:
            self._subscribers.pop(key, None)

    def attach_meta(self, ws: WebSocket, data: Any) -> None:
        """Associate arbitrary metadata with a WebSocket connection.
        Used to carry per-user context (e.g. spread overrides) into
        the per-connection framing hook without breaking the shared
        broadcast path for connections that don't need customisation."""
        self._ws_meta[ws] = data

    def detach_meta(self, ws: WebSocket) -> None:
        """Remove metadata for a WebSocket connection."""
        self._ws_meta.pop(ws, None)

    def subscriber_count(self) -> int:
        return sum(len(s) for s in self._subscribers.values())

    # ── overridable hooks ──────────────────────────────────────────
    async def _do_subscribe(self, ps: Any) -> None:
        """Subclass: subscribe / psubscribe to the upstream Redis channels."""
        raise NotImplementedError

    async def _do_unsubscribe(self, ps: Any) -> None:
        """Subclass: matching unsubscribe call. Best-effort during shutdown."""
        raise NotImplementedError

    def _route_keys(self, channel: str | None, payload: Any) -> list[str]:
        """Subclass: map an incoming pub/sub message to the keys whose
        subscribers should receive it. Return an empty list to drop."""
        raise NotImplementedError

    def _frame(self, channel: str | None, payload: Any) -> str | None:
        """Subclass: serialise the outgoing WS frame. Return ``None`` to drop."""
        raise NotImplementedError

    def _frame_for_ws(
        self,
        channel: str | None,
        payload: Any,
        ws: WebSocket,
        default_frame: str | None,
    ) -> str | None:
        """Per-connection frame builder. Override in subclasses for
        per-ws customization (e.g. per-user price transformation).
        Default: return the shared default_frame unchanged."""
        return default_frame

    # ── listener ───────────────────────────────────────────────────
    async def _listen_loop(self) -> None:
        """Single consumer of the shared pub/sub. Decodes each message
        once, builds the WS frame once, then fans it out concurrently to
        every subscribed socket. Bad sockets are removed from the hub
        on the spot so a dead client never accumulates.

        Wrapped in an outer reconnect loop: if the underlying Redis
        connection drops mid-``listen()`` (TLS proxy reset, momentary
        network blip, AWS ElastiCache failover) the inner loop's
        exception is caught, the pub/sub is re-created and re-subscribed,
        and listening resumes. Backoff is bounded so we don't hammer a
        truly-down Redis. Without this, a single one-second hiccup would
        permanently freeze every WS feed until the worker was restarted.
        """
        # Deferred import — see top-of-module note. Both packages are
        # fully initialised by the time the listener task is awaited.
        from app.api.ws._helpers import safe_send_text

        backoff = 1.0
        while self._started:
            try:
                if self._pubsub is None:
                    # Recreate after a reconnect.
                    self._pubsub = pubsub()
                    await self._do_subscribe(self._pubsub)
                async for msg in self._pubsub.listen():
                    if not self._started:
                        return
                    channel, payload = _decode_pubsub_msg(msg)
                    if payload is None and channel is None:
                        continue
                    keys = self._route_keys(channel, payload)
                    if not keys:
                        continue
                    # Snapshot the subscriber sets so concurrent
                    # add/remove during the fan-out doesn't mutate the
                    # iterator we're walking.
                    targets: set[WebSocket] = set()
                    for k in keys:
                        s = self._subscribers.get(k)
                        if s:
                            targets.update(s)
                    if not targets:
                        continue
                    default_frame = self._frame(channel, payload)
                    # Per-connection send — allows per-user price
                    # transformation (e.g. user-specific spread).
                    # Falls back to the shared default_frame for
                    # connections without metadata.
                    async def _do_send(sock: WebSocket) -> bool:
                        frame = self._frame_for_ws(
                            channel, payload, sock, default_frame
                        )
                        if frame is None:
                            return True
                        return bool(await safe_send_text(sock, frame))
                    results = await asyncio.gather(
                        *(_do_send(sock) for sock in targets),
                        return_exceptions=True,
                    )
                    # Remove sockets whose send failed.
                    for ws, ok in zip(targets, results):
                        if isinstance(ok, BaseException) or ok is False:
                            self.remove_ws(ws)
                # Iterator exhausted (Redis closed the connection
                # cleanly). Fall through to the reconnect block.
                backoff = 1.0
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(
                    "ws_hub_listener_error_reconnecting",
                    extra={"hub": self.name, "backoff": backoff},
                )
            # Tear down the dead pub/sub before sleeping so the next
            # iteration gets a fresh connection.
            old_ps, self._pubsub = self._pubsub, None
            if old_ps is not None:
                try:
                    await old_ps.close()
                except Exception:  # pragma: no cover
                    pass
            if not self._started:
                return
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                return
            backoff = min(backoff * 2, 30.0)


# ── Market data hub ────────────────────────────────────────────────────
class MarketTickHub(_BaseHub):
    """Routes ``market:tick:*`` and ``infoway:tick:*`` messages by token.

    Subscriber key = string token / symbol. Every market-data WS adds
    itself under each token it cares about; the listener fans every
    inbound tick out to the matching set."""

    name = "market_tick_hub"

    async def _do_subscribe(self, ps: Any) -> None:
        await ps.psubscribe("market:tick:*", "infoway:tick:*")

    async def _do_unsubscribe(self, ps: Any) -> None:
        try:
            await ps.punsubscribe("market:tick:*", "infoway:tick:*")
        except Exception:  # pragma: no cover
            pass

    def _route_keys(self, channel: str | None, payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        # Same normalisation as the original per-client listener — APK
        # expects ``token`` as a string; Infoway publishes with ``symbol``.
        tok_raw = payload.get("token") or payload.get("symbol")
        if tok_raw is None:
            return []
        tok = str(tok_raw)
        payload["token"] = tok
        return [tok]

    def _frame(self, channel: str | None, payload: Any) -> str | None:
        # Wire format identical to the original WS handler:
        #   {"type":"tick","payload":[<dict>]}
        return json.dumps({"type": "tick", "payload": [payload]}, default=str)

    def _frame_for_ws(
        self,
        channel: str | None,
        payload: Any,
        ws: WebSocket,
        default_frame: str | None,
    ) -> str | None:
        """Apply per-user spread override when this WS has user meta.

        The tick broadcast already carries the admin segment-level spread
        baked into bid/ask. For a user whose admin set a UserSegmentOverride
        spread, we re-derive bid/ask from the unchanged LTP so their
        personal markup is applied on top — without touching any other
        user's frame.

        Fixed mode  → bid = ltp − pips/2,  ask = ltp + pips/2
        Floating    → keep market spread when wider; widen to pips when not.
        """
        meta = self._ws_meta.get(ws)
        if not meta:
            return default_frame
        # Per-TOKEN (symbol) spread, resolved via the full cascade at subscribe
        # time — so each script's own spread shows (BTCUSD 30, ETHUSD 3), not
        # one segment-wide value applied to every symbol.
        token_spread: dict = meta.get("token_spread") or {}
        if not token_spread:
            return default_frame
        token = str(payload.get("token", ""))
        spread_cfg = token_spread.get(token)
        if not spread_cfg:
            return default_frame
        pips = float(spread_cfg.get("pips") or 0)
        if pips <= 0:
            return default_frame
        ltp = float(payload.get("ltp") or 0)
        if ltp <= 0:
            return default_frame
        half = pips / 2.0
        mode = str(spread_cfg.get("type") or "fixed").lower()
        tick = dict(payload)
        if mode == "fixed":
            tick["bid"] = round(ltp - half, 8)
            tick["ask"] = round(ltp + half, 8)
        else:
            live_bid = float(payload.get("bid") or 0)
            live_ask = float(payload.get("ask") or 0)
            live_spread = (
                (live_ask - live_bid) if (live_bid > 0 and live_ask > 0) else 0.0
            )
            if live_spread < pips:
                tick["bid"] = round(ltp - half, 8)
                tick["ask"] = round(ltp + half, 8)
        return json.dumps({"type": "tick", "payload": [tick]}, default=str)


# ── User-channel hub ───────────────────────────────────────────────────
class UserChannelHub(_BaseHub):
    """Routes per-user pub/sub messages (``user:{id}:positions`` etc.).

    Subscriber key = ``user_id`` (string). Each ``/ws/user`` connection
    attaches under one key; every event published on any of that user's
    sub-channels (``positions`` / ``orders`` / ``wallet`` / ``kyc`` /
    ``risk`` / ``deposit_update`` / ``withdrawal_update`` / …) is fanned
    out as-is so the existing client routing keeps working."""

    name = "user_channel_hub"

    # Same allowlist the original per-client `user_ws.py` subscribed to,
    # plus ``marketwatch`` — without it the marketwatch publish from
    # backend/app/api/v1/user/marketwatch.py was silently dropped here,
    # so cross-tab / cross-device watchlist sync never reached open
    # clients. Adding it is purely additive: existing topics behave
    # identically, and new clients listening for ``marketwatch`` now
    # actually receive the event.
    _ALLOWED_TOPICS = frozenset({"positions", "orders", "wallet", "kyc", "marketwatch", "risk"})

    async def _do_subscribe(self, ps: Any) -> None:
        await ps.psubscribe("user:*")

    async def _do_unsubscribe(self, ps: Any) -> None:
        try:
            await ps.punsubscribe("user:*")
        except Exception:  # pragma: no cover
            pass

    def _route_keys(self, channel: str | None, payload: Any) -> list[str]:
        if not channel:
            return []
        # Channel layout is "user:{id}:{topic}" — pull the id and topic
        # out without importing regex; the split is enough.
        parts = channel.split(":", 2)
        if len(parts) < 3 or parts[0] != "user":
            return []
        user_id, topic = parts[1], parts[2]
        if not user_id or topic not in self._ALLOWED_TOPICS:
            return []
        return [user_id]

    def _frame(self, channel: str | None, payload: Any) -> str | None:
        # Original handler forwarded the parsed JSON as-is; preserve that
        # exactly so frontend route handlers don't need to change.
        if payload is None:
            return None
        try:
            return json.dumps(payload, default=str)
        except (TypeError, ValueError):  # pragma: no cover
            return None


# ── Admin events hub ───────────────────────────────────────────────────
class AdminEventHub(_BaseHub):
    """Routes the single ``admin:events`` channel to every admin WS."""

    name = "admin_event_hub"
    _SINGLE_KEY = "_all"
    _CHANNEL = "admin:events"

    async def _do_subscribe(self, ps: Any) -> None:
        await ps.subscribe(self._CHANNEL)

    async def _do_unsubscribe(self, ps: Any) -> None:
        try:
            await ps.unsubscribe(self._CHANNEL)
        except Exception:  # pragma: no cover
            pass

    def _route_keys(self, channel: str | None, payload: Any) -> list[str]:
        if channel != self._CHANNEL:
            return []
        return [self._SINGLE_KEY]

    def _frame(self, channel: str | None, payload: Any) -> str | None:
        if payload is None:
            return None
        try:
            return json.dumps(payload, default=str)
        except (TypeError, ValueError):  # pragma: no cover
            return None

    # Convenience wrappers so the WS handler can stay agnostic of the
    # hub's internal "_all" key.
    def attach(self, ws: WebSocket) -> None:
        self.add(self._SINGLE_KEY, ws)

    def detach(self, ws: WebSocket) -> None:
        self.remove(self._SINGLE_KEY, ws)


# ── Module-level singletons ────────────────────────────────────────────
market_tick_hub = MarketTickHub()
user_channel_hub = UserChannelHub()
admin_event_hub = AdminEventHub()


async def start_all_hubs() -> None:
    """Start all three hubs. Called once from the FastAPI lifespan."""
    await asyncio.gather(
        market_tick_hub.start(),
        user_channel_hub.start(),
        admin_event_hub.start(),
        return_exceptions=True,
    )


async def stop_all_hubs() -> None:
    """Stop all three hubs. Called once from the FastAPI lifespan shutdown."""
    await asyncio.gather(
        market_tick_hub.stop(),
        user_channel_hub.stop(),
        admin_event_hub.stop(),
        return_exceptions=True,
    )
