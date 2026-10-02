"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowDownToLine,
  ArrowUpToLine,
  Bell,
  CheckCheck,
  CircleDollarSign,
  FileCheck,
  Info,
  ShieldAlert,
  UserPlus,
} from "lucide-react";
import { toast } from "sonner";
import { NotificationsAPI } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Admin notification bell — drops into the top-right of the admin
 * chrome next to the theme toggle. Polls `/admin/notifications` every
 * 15 s for fresh rows (and is also invalidated by the WS bridge on
 * every `notification_created` event for sub-second freshness when
 * the WebSocket is connected). Click the bell to open a dropdown
 * panel showing the latest 50 rows; click a row to mark-read +
 * navigate to its deep-link target.
 *
 * Sizing decisions:
 *   • Dropdown is `max-h-[70vh]` so the longest panels still fit on a
 *     phone-emulator viewport without overflowing the page.
 *   • Badge caps at "9+" — three-digit counts wreck the bell icon
 *     alignment on the top bar.
 */
export function NotificationBell() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  // Click-outside dismiss. The dropdown is anchored to the bell
  // button via absolute positioning, so a global mousedown listener
  // is the simplest way to close it without pulling in a portal
  // library just for one component.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // Unread count — drives the badge. Cheap O(1) backend query; safe
  // to poll at 10 s even with hundreds of admins connected.
  const { data: unreadData } = useQuery({
    queryKey: ["admin", "notifications", "unread-count"],
    queryFn: () => NotificationsAPI.unreadCount(),
    refetchInterval: 10_000,
    staleTime: 5_000,
  });
  const unread = Number(unreadData?.count ?? 0);

  // Full list only fetched when the dropdown is open (saves bandwidth
  // when the bell is closed but still mounted in the layout).
  const { data: rows, isFetching } = useQuery({
    queryKey: ["admin", "notifications", "list"],
    queryFn: () => NotificationsAPI.list({ limit: 50 }),
    enabled: open,
    refetchInterval: open ? 15_000 : false,
  });

  const markReadMut = useMutation({
    mutationFn: (id: string) => NotificationsAPI.markRead(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin", "notifications"] });
    },
  });

  const markAllReadMut = useMutation({
    mutationFn: () => NotificationsAPI.markAllRead(),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["admin", "notifications"] });
      toast.success(
        data?.marked
          ? `Marked ${data.marked} notification${data.marked === 1 ? "" : "s"} read`
          : "Nothing to mark",
      );
    },
  });

  const badge = unread > 9 ? "9+" : unread > 0 ? String(unread) : null;

  return (
    <div ref={wrapperRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={`Notifications${badge ? ` (${unread} unread)` : ""}`}
        title="Notifications"
        className={cn(
          "relative inline-flex size-9 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
          open && "bg-accent text-foreground",
        )}
      >
        <Bell className={cn("size-4", unread > 0 && "text-foreground")} />
        {badge && (
          <span
            className="absolute -right-0.5 -top-0.5 grid min-w-[16px] h-[16px] place-items-center rounded-full bg-destructive px-1 text-[9px] font-bold text-destructive-foreground shadow"
            aria-hidden
          >
            {badge}
          </span>
        )}
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full z-50 mt-1.5 w-[380px] max-w-[calc(100vw-1rem)] overflow-hidden rounded-md border border-border bg-popover text-popover-foreground shadow-lg"
        >
          {/* Header */}
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <Bell className="size-3.5" /> Notifications
              {unread > 0 && (
                <span className="rounded-full bg-destructive/15 px-1.5 py-0.5 text-[10px] font-bold text-destructive">
                  {unread} new
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={() => markAllReadMut.mutate()}
              disabled={markAllReadMut.isPending || unread === 0}
              className="inline-flex items-center gap-1 text-[11px] text-primary hover:underline disabled:cursor-not-allowed disabled:text-muted-foreground disabled:no-underline"
            >
              <CheckCheck className="size-3" /> Mark all read
            </button>
          </div>

          {/* List */}
          <div className="max-h-[70vh] overflow-y-auto">
            {isFetching && !rows ? (
              <div className="px-4 py-6 text-center text-xs text-muted-foreground">
                Loading…
              </div>
            ) : !rows || rows.length === 0 ? (
              <div className="px-4 py-8 text-center">
                <Bell className="mx-auto size-6 text-muted-foreground/40" />
                <div className="mt-2 text-xs font-medium text-muted-foreground">
                  All caught up
                </div>
                <div className="text-[10px] text-muted-foreground/70">
                  New deposits, withdrawals, KYC and settlement requests will show up here.
                </div>
              </div>
            ) : (
              <ul className="divide-y divide-border/60">
                {rows.map((n: any) => (
                  <NotificationRow
                    key={n.id}
                    n={n}
                    onClick={() => {
                      if (!n.is_read) markReadMut.mutate(n.id);
                      setOpen(false);
                    }}
                  />
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function NotificationRow({
  n,
  onClick,
}: {
  n: any;
  onClick: () => void;
}) {
  const Icon = iconFor(n.event_type);
  const tint = levelTint(n.level);

  const ago = relativeTime(n.created_at);

  const inner = (
    <div
      className={cn(
        "flex gap-3 px-3 py-2.5 transition-colors hover:bg-muted/40",
        !n.is_read && "bg-primary/[0.04]",
      )}
    >
      <div
        className={cn(
          "mt-0.5 grid size-8 shrink-0 place-items-center rounded-full",
          tint.bg,
          tint.fg,
        )}
      >
        <Icon className="size-4" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-2">
          <div
            className={cn(
              "truncate text-xs",
              n.is_read ? "font-medium text-foreground" : "font-semibold text-foreground",
            )}
            title={n.title}
          >
            {n.title}
          </div>
          {!n.is_read && (
            <span
              className="mt-1 size-1.5 shrink-0 rounded-full bg-primary"
              aria-hidden
            />
          )}
        </div>
        <div className="line-clamp-2 text-[11px] text-muted-foreground" title={n.message}>
          {n.message}
        </div>
        <div className="mt-1 flex items-center gap-2 text-[10px] text-muted-foreground/70">
          <span>{ago}</span>
          {n.data?.user_code && (
            <>
              <span>·</span>
              <span className="font-mono">{n.data.user_code}</span>
            </>
          )}
        </div>
      </div>
    </div>
  );

  if (n.link) {
    return (
      <li>
        <Link href={n.link} onClick={onClick} className="block">
          {inner}
        </Link>
      </li>
    );
  }
  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        className="block w-full text-left"
      >
        {inner}
      </button>
    </li>
  );
}

function iconFor(eventType: string) {
  switch (eventType) {
    case "DEPOSIT_SUBMITTED":
      return ArrowDownToLine;
    case "WITHDRAWAL_SUBMITTED":
      return ArrowUpToLine;
    case "KYC_SUBMITTED":
      return FileCheck;
    case "SETTLEMENT_REQUESTED":
      return CircleDollarSign;
    case "USER_REGISTERED":
      return UserPlus;
    default:
      return Info;
  }
}

function levelTint(level: string): { bg: string; fg: string } {
  switch (level) {
    case "WARNING":
      return {
        bg: "bg-amber-500/15",
        fg: "text-amber-700 dark:text-amber-300",
      };
    case "DANGER":
      return {
        bg: "bg-destructive/15",
        fg: "text-destructive",
      };
    case "SUCCESS":
      return {
        bg: "bg-emerald-500/15",
        fg: "text-emerald-700 dark:text-emerald-300",
      };
    case "INFO":
    default:
      return {
        bg: "bg-primary/15",
        fg: "text-primary",
      };
  }
}

function relativeTime(iso: string): string {
  // Tight inline "X mins ago" formatter — avoiding a full date-fns
  // import for a single top-bar component. Falls back to absolute
  // date once we cross a day.
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "—";
  const diff = Date.now() - t;
  const sec = Math.floor(diff / 1000);
  if (sec < 30) return "just now";
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} min${min === 1 ? "" : "s"} ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}d ago`;
  return new Date(iso).toLocaleDateString();
}

// Suppress unused-import lint for icons we keep available for future
// event types — small and intentional.
void AlertTriangle;
void ShieldAlert;
