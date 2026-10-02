"use client";

import { useEffect, useRef, useState } from "react";
import { WS_URL } from "@/lib/constants";

export type MarketQuote = {
  token: string;
  ltp?: number;
  bid?: number;
  ask?: number;
  change?: number;
  change_pct?: number;
  fx_rate?: number;
  [key: string]: any;
};

/**
 * Admin-side mirror of the user app's market-data WS hook.
 *
 * Opens a single WebSocket to `/ws/marketdata`, subscribes to the given
 * tokens, and returns a `{token → quote}` map that updates as ticks
 * arrive. Auto-reconnects with exponential backoff.
 *
 * Two display-quality fixes ported from the user-side hook
 * (frontend-user/lib/useMarketStream.ts):
 *
 *   • **500 ms display throttle** — the backend tick pump runs at
 *     ~250 ms and upstream Kite ticks can land every 50-200 ms. Without
 *     throttling each tick triggers a React re-render, so the admin
 *     PnL number flickers 4-10× per second. Coalesce to ~2 fps so the
 *     value is readable AND admin updates feel as snappy as the user
 *     app screen the operator is comparing it to.
 *   • **Sticky bid / ask / ltp** — exchanges occasionally publish a 0
 *     for one of these fields (depth refresh gap, illiquid moment).
 *     A 0 isn't a real price; preserve the last positive value so the
 *     downstream P&L calc doesn't get yanked to LTP for one frame and
 *     spike by hundreds of rupees only to snap back.
 *
 * Used by the admin positions page and the per-user Live Trade Stats
 * dialog so admins see floating P&L in real time, not behind a 5 s
 * REST poll.
 */
const DISPLAY_THROTTLE_MS = 500;

// Heartbeat: send a ping this often. The server replies {type:"pong"},
// which also counts as activity for the staleness watchdog below.
const HEARTBEAT_MS = 15_000;
// If no message (tick / snapshot / pong) arrives within this window the
// socket is treated as a zombie — iOS Safari PWAs frequently keep a
// connection in readyState OPEN after a suspend while it silently stops
// delivering data. We force-close + reconnect instead of trusting it.
const STALE_TIMEOUT_MS = 35_000;

export function useMarketStream(tokens: string[]): Map<string, MarketQuote> {
  const [quotes, setQuotes] = useState<Map<string, MarketQuote>>(new Map());
  const wsRef = useRef<WebSocket | null>(null);
  const subscribedRef = useRef<Set<string>>(new Set());
  const tokensKey = tokens.filter(Boolean).join(",");

  useEffect(() => {
    let stopped = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
    let lastMsgAt = Date.now();
    let attempt = 0;

    function stopHeartbeat() {
      if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
    }

    function startHeartbeat() {
      stopHeartbeat();
      lastMsgAt = Date.now();
      heartbeatTimer = setInterval(() => {
        const ws = wsRef.current;
        if (!ws) return;
        // Zombie detection — no inbound data for too long despite an
        // "open" socket. Drop it; onclose schedules the reconnect.
        if (Date.now() - lastMsgAt > STALE_TIMEOUT_MS) {
          try {
            ws.close();
          } catch {
            /* noop */
          }
          return;
        }
        if (ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(JSON.stringify({ type: "ping" }));
          } catch {
            /* send failed — onclose/onerror will recover */
          }
        }
      }, HEARTBEAT_MS);
    }

    // Sticky per-token cache + dirty set. See user-side hook for the
    // full rationale; in short: every incoming tick is merged INTO
    // the cached entry (zero/null fields preserve the prior positive
    // value), and a single setTimeout flushes the dirty tokens into
    // React state at most every DISPLAY_THROTTLE_MS.
    const sticky = new Map<string, MarketQuote>();
    const dirty = new Set<string>();
    let flushTimer: ReturnType<typeof setTimeout> | null = null;

    function isPositive(x: any): boolean {
      const n = Number(x);
      return Number.isFinite(n) && n > 0;
    }

    function mergeSticky(prev: MarketQuote | undefined, next: MarketQuote): MarketQuote {
      const merged: MarketQuote = { ...(prev ?? {}), ...next };
      if (!isPositive(next.bid) && isPositive(prev?.bid)) merged.bid = prev!.bid;
      if (!isPositive(next.ask) && isPositive(prev?.ask)) merged.ask = prev!.ask;
      if (!isPositive(next.ltp) && isPositive(prev?.ltp)) merged.ltp = prev!.ltp;
      return merged;
    }

    function flushPending() {
      flushTimer = null;
      if (dirty.size === 0) return;
      const drainedTokens = [...dirty];
      dirty.clear();
      setQuotes((prevState) => {
        const nextState = new Map(prevState);
        for (const tok of drainedTokens) {
          const q = sticky.get(tok);
          if (q) nextState.set(tok, q);
        }
        return nextState;
      });
    }

    function applyTicks(snaps: any[]) {
      for (const q of snaps) {
        const tok = String(q?.token ?? "");
        if (!tok) continue;
        const prev = sticky.get(tok);
        sticky.set(tok, mergeSticky(prev, q as MarketQuote));
        dirty.add(tok);
      }
      if (flushTimer === null) {
        flushTimer = setTimeout(flushPending, DISPLAY_THROTTLE_MS);
      }
    }

    function connect() {
      if (stopped) return;
      // iOS Safari PWA: if an existing socket is present, close it first
      // so we don't leak a frozen connection before opening a fresh one.
      wsRef.current?.close();
      const url = `${WS_URL.replace(/\/$/, "")}/ws/marketdata`;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        attempt = 0;
        startHeartbeat();
        const list = [...subscribedRef.current];
        if (list.length > 0) {
          ws.send(JSON.stringify({ type: "subscribe", tokens: list }));
        }
      };

      ws.onmessage = (ev) => {
        // Any inbound frame (tick / snapshot / pong / hello) means the
        // connection is alive — reset the staleness watchdog.
        lastMsgAt = Date.now();
        let msg: any;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (
          (msg?.type === "tick" || msg?.type === "snapshot") &&
          Array.isArray(msg.payload)
        ) {
          applyTicks(msg.payload);
        }
      };

      ws.onclose = () => {
        stopHeartbeat();
        if (stopped) return;
        attempt += 1;
        const delay = Math.min(15_000, 1_000 * 2 ** Math.min(attempt, 4));
        reconnectTimer = setTimeout(connect, delay);
      };

      ws.onerror = () => ws.close();
    }
    connect();

    // iOS Safari PWA: the system suspends WebSockets when the app is
    // backgrounded / phone is locked. The socket may appear open but
    // stops receiving ticks, so positions look "stuck". Reconnect on
    // returning to foreground and when the network comes back.
    function onVisible() {
      if (document.visibilityState !== "visible") return;
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        attempt = 0;
        connect();
        return;
      }
      // Force a fresh subscribe even if the socket still reports OPEN;
      // iOS sometimes keeps a zombie connection that no longer receives
      // data after a suspend. This is safe because the server is idempotent.
      const list = [...subscribedRef.current];
      if (list.length > 0) {
        try {
          ws.send(JSON.stringify({ type: "unsubscribe", tokens: list }));
          ws.send(JSON.stringify({ type: "subscribe", tokens: list }));
        } catch {
          connect();
        }
      }
    }
    function onOnline() {
      attempt = 0;
      connect();
    }
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onOnline);

    return () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (flushTimer) clearTimeout(flushTimer);
      stopHeartbeat();
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onOnline);
      sticky.clear();
      dirty.clear();
      wsRef.current?.close();
    };
    // intentional: WS stays open for component lifetime; the
    // subscribe-diff effect below handles token changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const ws = wsRef.current;
    const next = new Set(tokens.filter(Boolean));
    const prev = subscribedRef.current;
    const toAdd = [...next].filter((t) => !prev.has(t));
    const toRemove = [...prev].filter((t) => !next.has(t));
    subscribedRef.current = next;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (toAdd.length > 0) {
      ws.send(JSON.stringify({ type: "subscribe", tokens: toAdd }));
    }
    if (toRemove.length > 0) {
      ws.send(JSON.stringify({ type: "unsubscribe", tokens: toRemove }));
    }
  }, [tokensKey]);

  return quotes;
}
