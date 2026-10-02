"use client";

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAuthStore } from "@/stores/authStore";
import { STORAGE_KEYS, WS_URL } from "@/lib/constants";
import {
  ensureNotificationPermission,
  playNotifyPing,
  primeVoiceOnFirstGesture,
  showNativeNotification,
  speakNotification,
  subscribeForWebPush,
  userNotificationsEnabled,
} from "@/lib/notify-sound";

/** Format an INR amount string ("1500.00" → "₹1,500.00"). Defensive
 *  against junk values — falls back to the raw string if Number() can't
 *  parse it. */
function fmtINR(raw: string | number | undefined | null): string {
  if (raw === undefined || raw === null || raw === "") return "";
  const n = Number(raw);
  if (!Number.isFinite(n)) return String(raw);
  return `₹${Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Translate a wallet-event `reason` + signed amount into a toast title +
 *  body. Returns null when this kind of wallet move shouldn't ping the
 *  user (intra-trade brokerage, margin lock/release, etc.). */
function walletReasonToToast(
  reason: string | undefined,
  amount: string | undefined,
): { kind: "in" | "out"; title: string; body: string } | null {
  const r = String(reason || "").toUpperCase();
  const n = Number(amount ?? 0);
  const credit = Number.isFinite(n) ? n > 0 : true;
  switch (r) {
    case "DEPOSIT":
      return {
        kind: "in",
        title: "✅ Deposit approved",
        body: `${fmtINR(amount)} added to your wallet`,
      };
    case "WITHDRAWAL":
      return {
        kind: "out",
        title: "✅ Withdrawal processed",
        body: `${fmtINR(amount)} sent to your bank`,
      };
    case "ADJUSTMENT":
      // Admin manual Add / Deduct Fund — sign tells us which way.
      return credit
        ? {
            kind: "in",
            title: "💰 Funds added by admin",
            body: `${fmtINR(amount)} credited to your wallet`,
          }
        : {
            kind: "out",
            title: "⚠️ Funds deducted by admin",
            body: `${fmtINR(amount)} debited from your wallet`,
          };
    default:
      // Brokerage / margin / settlement / P&L bookings — silent. The
      // wallet card still refreshes via the query invalidate below;
      // we just don't pop a toast for every trade fill.
      return null;
  }
}

/**
 * Live updates from the backend's per-user pub/sub channels.
 *
 * Opens a single WebSocket to `/ws/user?token=…` (auth via JWT in query
 * because browsers don't allow custom headers on WS handshakes). Whenever
 * the server pushes a `position_update`, `order_update`, `trade_update` or
 * `wallet_update`, we invalidate the matching React Query keys so the
 * affected pages re-render without a manual refresh.
 *
 * Drop this component once near the top of the dashboard tree (e.g. in
 * `app/(dashboard)/layout.tsx`); it renders nothing.
 */
export function UserWsBridge() {
  const qc = useQueryClient();
  const user = useAuthStore((s) => s.user);

  useEffect(() => {
    if (!user) return;
    const access =
      typeof window !== "undefined"
        ? window.localStorage.getItem(STORAGE_KEYS.accessToken)
        : null;
    if (!access) return;
    // Request OS notification permission once per session so the wallet
    // toasts below can surface in the Android tray / desktop banner
    // when the PWA is backgrounded. After permission lands, subscribe
    // for Web Push so the backend can wake the phone even when the PWA
    // is force-stopped (the WebSocket above is dead in that case).
    void (async () => {
      const ok = await ensureNotificationPermission();
      if (ok) await subscribeForWebPush();
    })();
    // Unlock the Web Speech API on the user's first tap/click so the voice
    // announcement in the `notification` case below can actually play —
    // mobile browsers block speech until a real user gesture happens.
    const detachVoicePrime = primeVoiceOnFirstGesture();

    let stopped = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    function connect() {
      if (stopped) return;
      // Read the access token fresh on every connect attempt. The token
      // captured at mount becomes stale after the 15-min expiry — the
      // axios interceptor rotates it in localStorage but the closure
      // here would still carry the old value, causing an endless 403
      // loop (backend rejects the expired JWT before the WS upgrade).
      const freshToken =
        typeof window !== "undefined"
          ? window.localStorage.getItem(STORAGE_KEYS.accessToken)
          : null;
      if (!freshToken) return;
      const url = `${WS_URL.replace(/\/$/, "")}/ws/user?token=${encodeURIComponent(freshToken)}`;
      ws = new WebSocket(url);

      // 25 s heartbeat — mirrors AdminWsBridge. Stops corporate / mobile
      // proxies and Android battery savers from idling out the WS while
      // the PWA sits in the background.
      let pingTimer: ReturnType<typeof setInterval> | null = null;
      ws.onopen = () => {
        attempt = 0;
        if (pingTimer) clearInterval(pingTimer);
        pingTimer = setInterval(() => {
          try {
            if (ws && ws.readyState === WebSocket.OPEN) ws.send("ping");
          } catch {}
        }, 25_000);
      };

      ws.onmessage = (ev) => {
        let msg: any;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        switch (msg?.type) {
          case "position_update":
            qc.invalidateQueries({ queryKey: ["positions"] });
            qc.invalidateQueries({ queryKey: ["positions", "open"] });
            qc.invalidateQueries({ queryKey: ["wallet"] });
            break;
          case "order_update":
            qc.invalidateQueries({ queryKey: ["orders"] });
            qc.invalidateQueries({ queryKey: ["orders", "recent"] });
            break;
          case "trade_update":
            qc.invalidateQueries({ queryKey: ["trades"] });
            break;
          case "wallet_update":
            qc.invalidateQueries({ queryKey: ["wallet"] });
            break;
          case "stop_out_warning": {
            // Risk enforcer pings this when floating loss crosses the admin's
            // "Stop-out warning" % of wallet balance (once per crossing). The
            // switch previously had NO case for it, so the ping was silently
            // dropped and the user never saw the margin warning. Show a loud
            // in-app toast + OS notification so they can add funds / reduce
            // risk before the auto square-off at the stop-out %.
            const p = (msg as any).payload || msg || {};
            const lossPct = Number(p.loss_pct ?? 0);
            const thr = Number(p.threshold_pct ?? 0);
            const title = "⚠️ Margin warning";
            const body =
              `Floating loss is ${lossPct.toFixed(1)}% of your balance` +
              (thr > 0 ? ` (warning at ${thr.toFixed(0)}%)` : "") +
              `. Add funds or reduce positions to avoid auto square-off.`;
            toast.warning(title, { description: body, duration: 10000 });
            if (userNotificationsEnabled()) {
              playNotifyPing();
              showNativeNotification(title, body, { tag: "mp-risk-warning" });
            }
            break;
          }
          case "wallet":
            // Backend `_publish_wallet_event` uses type="wallet" (not
            // "wallet_update") and ships a {reason, amount, balance_after}
            // payload. Refresh the wallet cache AND show a WhatsApp-style
            // toast + ping when the move is operator-facing — deposit
            // approval, withdrawal payout, admin Add/Deduct Fund.
            qc.invalidateQueries({ queryKey: ["wallet"] });
            qc.invalidateQueries({ queryKey: ["ledger"] });
            {
              const p = (msg as any).payload || {};
              const t = walletReasonToToast(p.reason, p.amount);
              if (t && userNotificationsEnabled()) {
                if (t.kind === "in") {
                  toast.success(t.title, { description: t.body, duration: 7000 });
                } else {
                  toast.warning(t.title, { description: t.body, duration: 7000 });
                }
                playNotifyPing();
                // Unique tag per event so successive admin Add Fund /
                // deposit approvals each show as their own tray row
                // instead of collapsing onto the first one.
                showNativeNotification(t.title, t.body, {
                  tag: `mp-wallet-${Date.now()}`,
                });
              }
            }
            break;
          case "bonus_granted": {
            // Bonus Management — a deposit auto-granted a bonus. Refresh wallet
            // + bonus caches and toast the good news.
            qc.invalidateQueries({ queryKey: ["wallet"] });
            qc.invalidateQueries({ queryKey: ["wallet-summary"] });
            qc.invalidateQueries({ queryKey: ["my-bonuses"] });
            {
              const p = (msg as any).payload || {};
              if (userNotificationsEnabled()) {
                toast.success("Bonus credited! 🎁", {
                  description: p.name ? `${p.name}: ₹${p.amount} bonus credit` : `₹${p.amount} bonus credit`,
                  duration: 7000,
                });
                playNotifyPing();
              }
            }
            break;
          }
          case "notification": {
            // Admin broadcast — a one-off message (optionally with a link)
            // pushed to every user in the sender's pool. Refresh the bell /
            // notifications list and pop a toast + ping so it lands live.
            qc.invalidateQueries({ queryKey: ["notifications"] });
            qc.invalidateQueries({ queryKey: ["notifications", "unread-count"] });
            {
              const p = (msg as any).payload || {};
              const title = p.title || "Notification";
              const body = String(p.message || "");
              const lvl = String(p.level || "INFO").toUpperCase();
              if (userNotificationsEnabled()) {
                const opts = { description: body, duration: 8000 } as const;
                if (lvl === "DANGER") toast.error(title, opts);
                else if (lvl === "WARNING") toast.warning(title, opts);
                else if (lvl === "SUCCESS") toast.success(title, opts);
                else toast(title, opts);
                playNotifyPing();
                // Native OS tray popup — same path the wallet events use so
                // an admin broadcast surfaces in the Android tray / desktop
                // banner even when the tab is backgrounded. Unique tag per
                // event so successive broadcasts each show their own row.
                showNativeNotification(title, body, {
                  tag: `mp-broadcast-${Date.now()}`,
                  url: p.link || "/notifications",
                });
                // Voice announcement so it lands like a "real" notification.
                speakNotification(body ? `${title}. ${body}` : title);
              }
            }
            break;
          }
          case "marketwatch":
            // Cross-tab / cross-device sync: when this user adds /
            // removes an instrument on web, the apk (or another web
            // tab) repaints within ~1 s instead of waiting for the
            // next REST poll. `segment-items` is keyed by segment
            // name, so we don't have the name in scope — broad
            // invalidate the whole prefix.
            qc.invalidateQueries({ queryKey: ["watchlists"] });
            qc.invalidateQueries({ queryKey: ["watchlist-quotes"] });
            qc.invalidateQueries({ queryKey: ["segment-items"] });
            break;
          // hello / heartbeat — ignore
        }
      };

      ws.onclose = () => {
        if (pingTimer) {
          clearInterval(pingTimer);
          pingTimer = null;
        }
        if (stopped) return;
        attempt += 1;
        const delay = Math.min(15_000, 1_000 * 2 ** Math.min(attempt, 4));
        reconnectTimer = setTimeout(connect, delay);
      };

      ws.onerror = () => {
        // Let onclose handle reconnect cadence.
        ws?.close();
      };
    }

    // Reconnect when the PWA comes back from background. Browsers
    // (especially Android) kill idle WS in hidden tabs to save battery;
    // without this nudge the user would have to sit and wait for the
    // exponential-backoff retry to climb back down to a fresh open.
    const onVisible = () => {
      if (document.visibilityState === "visible" && (!ws || ws.readyState >= 2)) {
        if (reconnectTimer) {
          clearTimeout(reconnectTimer);
          reconnectTimer = null;
        }
        attempt = 0;
        connect();
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    connect();

    return () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      document.removeEventListener("visibilitychange", onVisible);
      if (typeof detachVoicePrime === "function") detachVoicePrime();
      ws?.close();
    };
  }, [qc, user?.id]);

  return null;
}
