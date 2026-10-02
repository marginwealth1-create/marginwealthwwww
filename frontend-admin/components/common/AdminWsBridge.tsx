"use client";

import { useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useAdminAuthStore } from "@/stores/authStore";
import { ensureFreshAccessToken, isExpiringSoon } from "@/lib/api";
import { ADMIN_API_KEY, STORAGE_KEYS, WS_URL } from "@/lib/constants";
import {
  ensureNotificationPermission,
  playNotifyPing,
  playSound,
  showNativeNotification,
  subscribeForWebPush,
} from "@/lib/notify-sound";

// Master notification toggle — controlled from /settings/platform.
// When the operator flips the switch off there, this guard suppresses
// the deposit/withdrawal toast + ping without unmounting the bridge,
// so React Query invalidation still runs (data stays fresh) but the
// audio + visual nag is silenced.
const NOTIFY_KEY = "admin.notifications.enabled";
function notificationsEnabled(): boolean {
  if (typeof window === "undefined") return true;
  const v = window.localStorage.getItem(NOTIFY_KEY);
  return v === null ? true : v === "1";
}

/**
 * Live admin-side updates from the backend's `admin:events` pub/sub channel.
 *
 * Opens a single WebSocket to `/ws/admin?token=…&key=…` (browsers can't
 * send custom headers on a WS handshake, so the X-Admin-Api-Key check is
 * mirrored as a query param). Whenever the backend publishes one of the
 * known event types (`position_update`, `order_update`, `wallet_update`,
 * `deposit_update`, `withdrawal_update`, `kyc_update`) we invalidate the
 * matching React Query keys so every open admin tab — Positions / Orders /
 * Payments / KYC / Dashboard — refreshes within the same event-loop tick
 * the user takes the action on the trader side. No more F5.
 *
 * Mounted once near the top of the admin layout; renders nothing.
 *
 * Reconnects with exponential backoff (max 15 s) on close / error. The
 * existing `refetchInterval` polls on each page are still in place as a
 * safety net — they just become rarely-triggered when the WS is healthy.
 */
export function AdminWsBridge() {
  const qc = useQueryClient();
  const admin = useAdminAuthStore((s) => s.admin);
  // Keep the latest router in a ref so the WS message handler (created once
  // per connect inside the effect) can deep-link without becoming a dep.
  const router = useRouter();
  const routerRef = useRef(router);
  routerRef.current = router;

  useEffect(() => {
    if (!admin) return;
    if (!ADMIN_API_KEY) return;
    // Ask the OS for notification permission once per admin session. The
    // prompt only shows the first time; subsequent mounts no-op. Without
    // this, `new Notification(...)` calls below would silently fall back
    // to the in-app toast, which doesn't surface in the Android tray
    // when the PWA is minimised. After permission lands we kick off the
    // Web Push subscribe so the backend can pingthe phone even when the
    // PWA is force-stopped and the WS bridge below is dead.
    void (async () => {
      const ok = await ensureNotificationPermission();
      if (ok) await subscribeForWebPush();
    })();

    let stopped = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    // Re-read the access token from localStorage on EVERY connect attempt
    // (not just once on mount). Previously the token was snapshot in the
    // outer effect closure, so once that JWT expired (~15 min) every
    // subsequent reconnect kept sending the same dead token → server
    // replied 403 in a tight loop → admin dashboards stopped getting
    // live `position_update` / `order_update` events and every page
    // appeared frozen until F5. With the re-read, the rotated token
    // that `ensureFreshAccessToken` wrote to localStorage in some other
    // tab / preflight call is picked up automatically.
    async function connect() {
      if (stopped) return;
      // Belt-and-braces: if the locally cached token is already past the
      // refresh window, rotate it BEFORE opening the WS. Otherwise the
      // first connect would hit 403 and we'd waste a reconnect cycle.
      let access: string | null =
        typeof window !== "undefined"
          ? window.localStorage.getItem(STORAGE_KEYS.accessToken)
          : null;
      if (!access || isExpiringSoon(access)) {
        try {
          access = (await ensureFreshAccessToken()) || access;
        } catch {
          // Refresh failed — fall through and let the WS get 403, which
          // will trigger another retry with backoff. No throw here so
          // we don't unmount the bridge on a transient network blip.
        }
      }
      if (!access) {
        // No token at all (logged out / refresh refused) — schedule a
        // delayed retry instead of giving up; the layout's refresh
        // effect may re-populate localStorage shortly.
        reconnectTimer = setTimeout(() => void connect(), 3000);
        return;
      }
      const url =
        `${WS_URL.replace(/\/$/, "")}/ws/admin` +
        `?token=${encodeURIComponent(access)}` +
        `&key=${encodeURIComponent(ADMIN_API_KEY)}`;
      ws = new WebSocket(url);

      // 25 s heartbeat. Many corporate / mobile proxies kill idle WS
      // connections after 30-60 s, and Android backgrounds the WebSocket
      // on minimised PWAs. Sending a tiny ping every 25 s keeps the
      // connection warm so events fire reliably even when the operator
      // switches to WhatsApp for a few minutes. Server discards unknown
      // string frames; the keep-alive is purely a TCP keepalive proxy.
      let pingTimer: ReturnType<typeof setInterval> | null = null;
      ws.onopen = () => {
        attempt = 0;
        // eslint-disable-next-line no-console
        console.info("[admin-ws] open");
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
        // The publisher's `type` tells us which slice of admin queries to
        // refresh. Keys mirror the queryKey prefixes the admin pages use
        // (`["admin", "positions", ...]`, `["admin", "orders"]`, etc.).
        switch (msg?.type) {
          case "position_update":
            qc.invalidateQueries({ queryKey: ["admin", "positions"] });
            qc.invalidateQueries({ queryKey: ["admin", "dashboard"] });
            qc.invalidateQueries({ queryKey: ["admin", "accounts"] });
            // Per-user trade watch. The backend only attaches `alert` for
            // users whose trade_alert flag is on (Users → user → Trade
            // alerts), so there's nothing to filter by here beyond the
            // same ownership scope the deposit toasts use.
            if (msg.alert && notificationsEnabled()) {
              const recipients: string[] | undefined = msg.recipient_admin_ids;
              const myId = String(admin?.id || "");
              if (Array.isArray(recipients) && myId && !recipients.includes(myId)) {
                break;
              }
              const a = msg.alert;
              const who = [a.user_name, a.user_code && `(${a.user_code})`]
                .filter(Boolean)
                .join(" ");
              const verb =
                a.event === "closed" ? "closed" : a.event === "opened" ? "opened" : "changed";
              const body = [
                `${a.action} ${a.qty} ${a.symbol}`,
                a.price ? `@ ${Number(a.price).toLocaleString("en-IN")}` : "",
              ]
                .filter(Boolean)
                .join(" ");
              const show = a.event === "closed" ? toast.warning : toast.success;
              show(`${who} ${verb} a trade`, {
                description: body,
                duration: 8000,
                action: {
                  label: "View",
                  // Straight at the position: that user's book, filtered to
                  // the symbol, on the tab the fill left it in.
                  onClick: () =>
                    routerRef.current.push(
                      `/positions?user_id=${encodeURIComponent(msg.user_id)}` +
                        `&q=${encodeURIComponent(a.symbol || "")}` +
                        (a.event === "closed" ? "&tab=closed" : ""),
                    ),
                },
              });
              playSound(a.sound);
            }
            break;
          case "order_update":
            qc.invalidateQueries({ queryKey: ["admin", "orders"] });
            qc.invalidateQueries({ queryKey: ["admin", "trades"] });
            qc.invalidateQueries({ queryKey: ["admin", "positions"] });
            qc.invalidateQueries({ queryKey: ["admin", "dashboard"] });
            qc.invalidateQueries({ queryKey: ["admin", "accounts"] });
            break;
          case "wallet_update":
            // Admin wallet / margin tiles + per-user wallet drill-downs.
            qc.invalidateQueries({ queryKey: ["admin", "wallets"] });
            qc.invalidateQueries({ queryKey: ["admin", "ledger"] });
            qc.invalidateQueries({ queryKey: ["admin", "dashboard"] });
            qc.invalidateQueries({ queryKey: ["admin", "users"] });
            break;
          case "deposit_update":
            qc.invalidateQueries({ queryKey: ["admin", "deposits"] });
            qc.invalidateQueries({ queryKey: ["admin", "payments"] });
            qc.invalidateQueries({ queryKey: ["admin", "dashboard"] });
            // Live "WhatsApp-style" toast + chime when a NEW request lands
            // (only on submit — not on the admin's own approve/reject echo).
            // Suppressed when /settings/platform → Notifications is off.
            if (msg.event === "submitted" && notificationsEnabled()) {
              // Scope filter: backend includes the list of admin/broker
              // ids that own the source user. Only ping THIS admin if
              // they're in that list. Without this every admin would
              // hear every other admin's pool, which the operator
              // explicitly rejected: "ek admin ka dusre admin ko nahi
              // jaye". When the field is missing (older backend), fall
              // back to the old broadcast behaviour so we don't go
              // silent during a partial roll-out.
              const recipients: string[] | undefined = msg.recipient_admin_ids;
              const myId = String(admin?.id || "");
              if (Array.isArray(recipients) && myId && !recipients.includes(myId)) {
                break;
              }
              const who = msg.user_name || msg.user_code || "a user";
              const code = msg.user_name && msg.user_code ? ` (${msg.user_code})` : "";
              const amt = msg.amount ? `₹${Number(msg.amount).toLocaleString("en-IN")}` : "";
              const mode = msg.mode ? String(msg.mode).toUpperCase() : "";
              const body = [amt, `${who}${code}`, mode].filter(Boolean).join(" · ");
              toast.success("💰 New deposit request", {
                description: body,
                duration: 9000,
                action: {
                  label: "View",
                  onClick: () => routerRef.current.push("/payments?tab=deposits"),
                },
              });
              playNotifyPing();
              // OS tray notification — fires even when the PWA is
              // minimised / behind another app. Single tag so multiple
              // deposits collapse into one notification badge instead
              // of stacking five separate ones.
              // Unique tag per deposit so multiple submits each
               // surface as separate tray rows. The earlier shared
               // "mp-deposit" tag was collapsing repeats — operator
               // saw the first one then stopped getting alerted to
               // subsequent ones if the system didn't dismiss the
               // previous notification first.
              showNativeNotification("💰 New deposit request", body, {
                tag: `mp-deposit-${msg.deposit_id || Date.now()}`,
                onClick: () => routerRef.current.push("/payments?tab=deposits"),
              });
            }
            break;
          case "withdrawal_update":
            qc.invalidateQueries({ queryKey: ["admin", "withdrawals"] });
            qc.invalidateQueries({ queryKey: ["admin", "payments"] });
            qc.invalidateQueries({ queryKey: ["admin", "dashboard"] });
            if (msg.event === "submitted" && notificationsEnabled()) {
              // Same scope filter as the deposit branch — only admins
              // who own this user get the toast + tray notification.
              const recipients: string[] | undefined = msg.recipient_admin_ids;
              const myId = String(admin?.id || "");
              if (Array.isArray(recipients) && myId && !recipients.includes(myId)) {
                break;
              }
              const who = msg.user_name || msg.user_code || "a user";
              const code = msg.user_name && msg.user_code ? ` (${msg.user_code})` : "";
              const amt = msg.amount ? `₹${Number(msg.amount).toLocaleString("en-IN")}` : "";
              const body = [amt, `${who}${code}`].filter(Boolean).join(" · ");
              toast.warning("🏦 New withdrawal request", {
                description: body,
                duration: 12000,
                action: {
                  label: "View",
                  onClick: () => routerRef.current.push("/payments?tab=withdrawals"),
                },
              });
              playNotifyPing();
              showNativeNotification("🏦 New withdrawal request", body, {
                tag: `mp-withdrawal-${msg.withdrawal_id || Date.now()}`,
                onClick: () => routerRef.current.push("/payments?tab=withdrawals"),
              });
            }
            break;
          case "pnl_sharing_update":
            // Refetch all P&L sharing data (list, agreement detail, report
            // rows, settlement history, me/agreement) so the live SharingCard
            // refreshes as positions close. Phase C invalidates broadly;
            // per-agreement filtering by broker_id is a future optimisation.
            qc.invalidateQueries({ queryKey: ["pnl-sharing"] });
            break;
          case "settlement_update":
            // Settlement Requests tab on /payments — refresh the queue
            // when an admin elsewhere approves / rejects, or when a new
            // pending request lands.
            qc.invalidateQueries({ queryKey: ["admin", "settlement-requests"] });
            qc.invalidateQueries({ queryKey: ["admin", "user"] });
            break;
          case "notification_created":
            // Admin notification bell — refresh badge count + open
            // dropdown list. Backend's payload carries
            // `recipient_admin_ids`, but we don't bother filtering
            // here: every admin's notifications query is server-side
            // scoped to their own id, so a refetch on a notification
            // that wasn't theirs is just a cheap O(1) unread-count
            // probe.
            qc.invalidateQueries({ queryKey: ["admin", "notifications"] });
            break;
          // hello / heartbeat — ignore
        }
      };

      ws.onclose = (ev) => {
        if (pingTimer) {
          clearInterval(pingTimer);
          pingTimer = null;
        }
        if (stopped) return;
        attempt += 1;
        const delay = Math.min(15_000, 1_000 * 2 ** Math.min(attempt, 4));
        // eslint-disable-next-line no-console
        console.warn("[admin-ws] closed", {
          code: ev.code,
          reason: ev.reason,
          retryInMs: delay,
        });
        reconnectTimer = setTimeout(() => void connect(), delay);
      };

      ws.onerror = (ev) => {
        // eslint-disable-next-line no-console
        console.error("[admin-ws] error", ev);
        ws?.close();
      };
    }

    // Reconnect proactively when the tab becomes visible again. Browsers
    // often suspend or close idle WebSockets in background tabs; without
    // this, an admin who switched to another tab for an hour would come
    // back to a silently dead socket and stale live data until they
    // hit F5.
    const onVisible = () => {
      if (document.visibilityState === "visible" && (!ws || ws.readyState >= 2)) {
        if (reconnectTimer) {
          clearTimeout(reconnectTimer);
          reconnectTimer = null;
        }
        attempt = 0;
        void connect();
      }
    };
    document.addEventListener("visibilitychange", onVisible);

    void connect();

    return () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      document.removeEventListener("visibilitychange", onVisible);
      ws?.close();
    };
  }, [qc, admin?.id]);

  return null;
}
