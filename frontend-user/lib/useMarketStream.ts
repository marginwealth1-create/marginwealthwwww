"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { getAccessToken } from "@/lib/api";
import { WS_URL } from "@/lib/constants";

/**
 * Per-WS-connection cap on live token subscriptions. Mirrors the backend
 * `WS_MAX_SUBSCRIPTIONS_PER_CONN` (app/core/config.py). When the caller
 * passes more than this many tokens we trim to the first N, toast the
 * user, and never put the over-flow on the wire — the server would
 * reject the whole batch anyway and the user wouldn't see any quote
 * stream until they manually trimmed. The pre-trim keeps the first 70
 * streaming so the panel is usable while the user prunes the watchlist.
 */
const MAX_SUBSCRIPTIONS = 70;

export type MarketQuote = {
  token: string;
  ltp?: number;
  bid?: number;
  ask?: number;
  change?: number;
  change_pct?: number;
  [key: string]: any;
};

/**
 * Live market-data stream. Opens a single WebSocket to `/ws/marketdata`,
 * subscribes to the given tokens, and returns a `{token → quote}` map that
 * updates as ticks arrive. Auto-reconnects with exponential backoff.
 *
 * Throttling — display refresh is coalesced to 100 ms (10 Hz) to line up
 * with the backend tick pump (MARKET_TICK_SEC = 0.1s) — every upstream
 * tick paints on the next render cycle without piling up between flushes.
 * Still visibly tick-by-tick on web + APK. The WS itself still receives
 * every upstream tick at full rate so data freshness is preserved; the
 * coalesce only bounds how often React re-renders, not what data arrives.
 */
const DISPLAY_THROTTLE_MS = 100;

// Heartbeat: send a ping this often. The server replies {type:"pong"},
// which also resets the staleness watchdog below.
const HEARTBEAT_MS = 15_000;
// If no inbound message arrives within this window the socket is treated
// as a zombie — iOS Safari PWAs keep a connection in readyState OPEN
// after a suspend while it silently stops delivering data. Force-close +
// reconnect instead of trusting it.
const STALE_TIMEOUT_MS = 35_000;

/**
 * MODULE-LEVEL last-known-good quote cache, shared by every hook instance for
 * the lifetime of the tab.
 *
 * The per-instance sticky map below only survives while ONE component stays
 * mounted. Navigating (Positions -> Market) unmounts the consumer, tears the
 * WS down and mounts a brand-new hook whose state starts EMPTY — so the first
 * frame after every navigation had no price at all, and the backend's
 * zero-valued `_empty_quote` skeleton (sent for a freshly re-subscribed token
 * before the real Zerodha/Infoway overlay lands on the next tick) was the only
 * thing on screen for ~1 s. That is the "market page par 1 second ke liye 0
 * dikhta hai" report.
 *
 * Hoisting the cache to module scope makes the last real price survive across
 * unmounts, so a remount paints the previous price on the FIRST frame and the
 * WS simply refreshes it. Bounded by the number of tokens the user actually
 * looks at in a session, and never persisted — a reload starts clean.
 */
const lastKnownQuotes = new Map<string, MarketQuote>();

export function useMarketStream(tokens: string[]): Map<string, MarketQuote> {
  // Seed from the cross-mount cache so a navigation never shows a blank /
  // zero price for tokens we already have a real price for.
  const [quotes, setQuotes] = useState<Map<string, MarketQuote>>(
    () => new Map(lastKnownQuotes),
  );
  const wsRef = useRef<WebSocket | null>(null);
  const subscribedRef = useRef<Set<string>>(new Set());
  // Last requested-list length we saw, so we only toast on the EDGE —
  // re-renders that pass the same over-limit list don't spam the user.
  const lastRequestedLenRef = useRef(0);
  const tokensKey = tokens.join(",");

  // One-shot WS lifecycle — open on mount, close on unmount, reconnect on close.
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

    // Per-token sticky cache. Every incoming tick is merged INTO the
    // previous cached entry instead of replacing it wholesale, with one
    // crucial rule: if a numeric quote field (bid / ask / ltp) arrives
    // as 0 / null / undefined / NaN, KEEP the previous non-zero value
    // for that field. Upstream Zerodha / Infoway ticks legitimately
    // skip bid or ask on illiquid options + the millisecond between a
    // depth refresh and a trade — without the sticky merge, the
    // position page's `(isLong ? bid : ask) || liveLtp` chain falls
    // through to LTP for that single tick, and on a wide-spread
    // instrument the PnL jumps several hundred / thousand rupees only
    // to snap back on the next tick. User-reported symptom: "bid/ask
    // ek millisecond ke liye band hoti hai, position LTP par shift ho
    // jata hai, PnL 2000 se 5000 ho jata hai". Sticky preservation
    // makes the display continuous — the LAST KNOWN good bid/ask
    // stays until a new non-zero value arrives.
    // Seeded from (and written through to) the module-level cache so ticks
    // received before a navigation keep protecting the display after it.
    const sticky = lastKnownQuotes;
    const dirty = new Set<string>();
    let flushTimer: ReturnType<typeof setTimeout> | null = null;

    function isPositive(x: any): boolean {
      const n = Number(x);
      return Number.isFinite(n) && n > 0;
    }

    function mergeSticky(prev: MarketQuote | undefined, next: MarketQuote): MarketQuote {
      // Start from the previous quote so unrelated fields (volume, OHLC,
      // change_pct, etc.) are preserved when the new tick only carries
      // a subset of fields.
      const merged: MarketQuote = { ...(prev ?? {}), ...next };
      // Sticky overrides — keep prior value when the incoming field is
      // missing or non-positive. Zero is treated as "no data" for
      // bid / ask / ltp because exchanges never quote them as 0 for a
      // live tradable instrument.
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
        const next = new Map(prevState);
        for (const tok of drainedTokens) {
          const q = sticky.get(tok);
          if (q) next.set(tok, q);
        }
        return next;
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
      // iOS Safari PWA: close any existing socket before opening a new one
      // to avoid leaking a frozen connection after the app was backgrounded.
      wsRef.current?.close();
      const base = `${WS_URL.replace(/\/$/, "")}/ws/marketdata`;
      const jwt = getAccessToken();
      const url = jwt ? `${base}?token=${encodeURIComponent(jwt)}` : base;
      // Production-debugging breadcrumb: log the resolved WS origin once per
      // connect attempt. If the panel shows "—" everywhere in prod, this is
      // the first thing to check in DevTools console — if it prints
      // `ws://localhost:8000` from marginwealth.com, NEXT_PUBLIC_WS_URL wasn't set
      // at build time (or the build wasn't redeployed after fixing the env).
      // eslint-disable-next-line no-console
      console.info("[market-ws] connecting", url);
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        attempt = 0;
        startHeartbeat();
        // eslint-disable-next-line no-console
        console.info("[market-ws] open", url);
        // Re-send subscriptions for whatever tokens the consumer last asked
        // about. The cleanup effect below mirrors `subscribedRef` to the
        // outside world; on reconnect we re-establish that exact set.
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
        if ((msg?.type === "tick" || msg?.type === "snapshot") && Array.isArray(msg.payload)) {
          applyTicks(msg.payload);
        } else if (msg?.type === "error" && msg?.code === "subscription_limit") {
          // Safety net — the pre-trim below should keep us off this
          // path, but if a race ever sends an over-limit batch (e.g.
          // tokens prop grew mid-flight) the server rejects and we
          // surface the same toast as the client-side cap.
          toast.error(
            msg.message ||
              `Subscription limit reached (${MAX_SUBSCRIPTIONS}). Unsubscribe some symbols before adding new ones.`,
            { duration: 5000 },
          );
        }
      };

      ws.onclose = (ev) => {
        stopHeartbeat();
        if (stopped) return;
        attempt += 1;
        const delay = Math.min(15_000, 1_000 * 2 ** Math.min(attempt, 4));
        // eslint-disable-next-line no-console
        console.warn("[market-ws] closed", { code: ev.code, reason: ev.reason, retryInMs: delay });
        reconnectTimer = setTimeout(connect, delay);
      };

      ws.onerror = (ev) => {
        // eslint-disable-next-line no-console
        console.error("[market-ws] error", ev);
        ws.close();
      };
    }
    connect();

    // iOS Safari PWA: system suspends WebSockets when the app is backgrounded
    // or the phone is locked. Reconnect / re-subscribe when coming back.
    function onVisible() {
      if (document.visibilityState !== "visible") return;
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        attempt = 0;
        connect();
        return;
      }
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
      // `sticky` is the module-level cache now — deliberately NOT cleared, so
      // the next mount can paint the last real price immediately.
      dirty.clear();
      wsRef.current?.close();
    };
    // intentionally empty deps — the WS stays open for the lifetime of the
    // component; the subscribe-set sync effect below handles token changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Diff the subscription set whenever the consumer's `tokens` change.
  // Sends `subscribe` for new tokens and `unsubscribe` for removed ones,
  // so the server can free per-token state and we don't get tick spam
  // for symbols we no longer care about.
  useEffect(() => {
    const ws = wsRef.current;
    const requested = tokens.filter(Boolean);
    // Pre-trim to the per-WS cap. We keep the FIRST N tokens (callers
    // pass watchlist order so the user's most-recently-arranged items
    // win) and drop the rest. The dropped tokens never hit the wire —
    // the backend would reject the whole batch otherwise.
    const trimmed = requested.slice(0, MAX_SUBSCRIPTIONS);
    const droppedCount = requested.length - trimmed.length;
    if (droppedCount > 0 && requested.length > lastRequestedLenRef.current) {
      // Only toast when the requested list GREW past the cap, so a
      // steady-state over-limit watchlist toasts once on initial mount
      // and once per additional add — not on every unrelated re-render.
      toast.error(
        `Subscription limit reached (${MAX_SUBSCRIPTIONS}). ${droppedCount} symbol${droppedCount > 1 ? "s" : ""} will not stream — remove some from your watchlist to add new ones.`,
        { duration: 5000 },
      );
    }
    lastRequestedLenRef.current = requested.length;

    const next = new Set(trimmed);
    const prev = subscribedRef.current;
    const toAdd = [...next].filter((t) => !prev.has(t));
    const toRemove = [...prev].filter((t) => !next.has(t));
    subscribedRef.current = next;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (toAdd.length > 0) ws.send(JSON.stringify({ type: "subscribe", tokens: toAdd }));
    if (toRemove.length > 0) ws.send(JSON.stringify({ type: "unsubscribe", tokens: toRemove }));
  }, [tokensKey]);

  return quotes;
}
