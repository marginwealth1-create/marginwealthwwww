"""Per-minute bid/ask aggregator for the admin "Rate History" feature.

The feed-leader worker's `market_data_service.tick_loop` already computes an
overlaid live quote (`ltp` / `bid` / `ask` / OHLC / volume) for every
subscribed token every tick. This module taps that same stream — via a cheap,
synchronous `record()` call from the tick loop — and folds it into in-memory
per-minute high/low buckets. A separate `tick_aggregator_flush_loop` (started
under the SAME `leader:feed` gate in `app/main.py`, because the buckets live in
the leader's process memory) persists each COMPLETED minute to the
`tick_snapshots` collection, which the admin Rate History modal reads back.

Design notes:
- Leader-only. Only the worker running `tick_loop` populates `_buckets`, so
  there is exactly one writer — no cross-worker duplicate rows.
- `record()` does zero I/O so it never slows the 100 ms tick loop.
- A completed minute is flushed exactly ONCE then dropped, so `insert_many`
  (not upsert) is correct and there is no unbounded memory growth (at most the
  current + previous minute per token exist between flushes).
- Forward-only: history exists only from the moment this runs. Bid/ask cannot
  be back-filled from any feed (Zerodha historical is LTP candles only).
- 30-day retention comes from the model's `expires_at` TTL index.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.models.tick_snapshot import TickSnapshot

logger = logging.getLogger(__name__)

# token -> minute_epoch(sec) -> bucket dict. Lives in the leader process only.
_buckets: dict[str, dict[int, dict[str, Any]]] = {}

_MINUTE_MS = 60_000


def _new_bucket() -> dict[str, Any]:
    # `*_low` / `open` start as None ("unset") so the first real value seeds
    # both the high and the low instead of a spurious 0 pinning the low.
    return {
        "bid_high": 0.0,
        "bid_low": None,
        "ask_high": 0.0,
        "ask_low": None,
        "open": None,
        "high": 0.0,
        "low": None,
        "close": 0.0,
        "volume": 0.0,
    }


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def record(items: list[tuple[str, dict[str, Any]]], now_ms: int) -> None:
    """Fold one tick batch into the current minute's buckets. Synchronous and
    allocation-light — safe to call from the hot tick loop. Never raises (the
    caller also guards, but a bad tick must not corrupt the loop)."""
    if not items:
        return
    minute = (now_ms // _MINUTE_MS) * 60  # minute-aligned epoch SECONDS
    for token, q in items:
        try:
            ltp = _to_float(q.get("ltp"))
            if ltp <= 0:
                # No real feed for this token this tick — don't record a
                # zero-priced bucket (mirrors the tick loop's own skip).
                continue
            bid = _to_float(q.get("bid"))
            ask = _to_float(q.get("ask"))
            vol = _to_float(q.get("volume"))

            tok_buckets = _buckets.get(str(token))
            if tok_buckets is None:
                tok_buckets = {}
                _buckets[str(token)] = tok_buckets
            b = tok_buckets.get(minute)
            if b is None:
                b = _new_bucket()
                tok_buckets[minute] = b

            # LTP → OHLC
            if b["open"] is None:
                b["open"] = ltp
            b["high"] = ltp if b["high"] <= 0 else max(b["high"], ltp)
            b["low"] = ltp if b["low"] is None else min(b["low"], ltp)
            b["close"] = ltp
            if vol > b["volume"]:
                b["volume"] = vol

            # Bid high/low — ignore non-positive (illiquid ticks skip depth).
            if bid > 0:
                b["bid_high"] = bid if b["bid_high"] <= 0 else max(b["bid_high"], bid)
                b["bid_low"] = bid if b["bid_low"] is None else min(b["bid_low"], bid)

            # Ask high/low.
            if ask > 0:
                b["ask_high"] = ask if b["ask_high"] <= 0 else max(b["ask_high"], ask)
                b["ask_low"] = ask if b["ask_low"] is None else min(b["ask_low"], ask)
        except Exception:  # pragma: no cover - one bad tick must not break the rest
            continue


async def flush_completed(now_ms: int | None = None) -> int:
    """Persist every bucket whose minute is already OVER (< current minute) to
    `tick_snapshots`, then drop it. Returns the number of rows written. Never
    raises — a Mongo hiccup must not kill the flush loop."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    current_minute = (now_ms // _MINUTE_MS) * 60

    docs: list[TickSnapshot] = []
    for token in list(_buckets.keys()):
        tok_buckets = _buckets.get(token)
        if not tok_buckets:
            _buckets.pop(token, None)
            continue
        for minute in [m for m in tok_buckets if m < current_minute]:
            b = tok_buckets.pop(minute)
            docs.append(
                TickSnapshot(
                    token=token,
                    timestamp=datetime.fromtimestamp(minute, tz=timezone.utc),
                    bid_high=float(b["bid_high"] or 0.0),
                    bid_low=float(b["bid_low"] or 0.0),
                    ask_high=float(b["ask_high"] or 0.0),
                    ask_low=float(b["ask_low"] or 0.0),
                    open=float(b["open"] or 0.0),
                    high=float(b["high"] or 0.0),
                    low=float(b["low"] or 0.0),
                    close=float(b["close"] or 0.0),
                    volume=float(b["volume"] or 0.0),
                )
            )
        if not tok_buckets:
            _buckets.pop(token, None)

    if not docs:
        return 0
    try:
        await TickSnapshot.insert_many(docs)
        return len(docs)
    except Exception:
        logger.debug("tick_snapshot_flush_failed", exc_info=True)
        return 0


async def tick_aggregator_flush_loop(interval_sec: float = 60.0) -> None:
    """LEADER-ONLY: every `interval_sec` persist all completed-minute buckets.
    Co-located with `tick_loop` under the `leader:feed` gate (the buckets live
    in this worker's memory). Exits cleanly on cancel (leadership lost /
    shutdown)."""
    logger.info("tick_aggregator_flush_loop_started")
    try:
        while True:
            try:
                n = await flush_completed()
                if n:
                    logger.debug("tick_snapshot_flushed", extra={"rows": n})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("tick_aggregator_flush_iter_failed", exc_info=True)
            await asyncio.sleep(interval_sec)
    finally:
        logger.info("tick_aggregator_flush_loop_stopped")


def stop_tick_aggregator() -> None:
    """Drop all in-memory buckets. Called on shutdown / leadership loss; the
    in-progress (unflushed) minute is intentionally discarded."""
    _buckets.clear()
