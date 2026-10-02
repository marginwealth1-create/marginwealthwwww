"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Search as SearchIcon } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { X as XIcon } from "lucide-react";
import { SettingsAPI, UsersAPI } from "@/lib/api";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/common/PageHeader";
import { DataTable, type Column } from "@/components/common/DataTable";

export default function AuditLogsPage() {
  return (
    <Suspense fallback={null}>
      <AuditLogsInner />
    </Suspense>
  );
}


/** Search-a-user box. Lets the admin type a name / user code / email /
 *  mobile, see live matches, and click one to re-scope the ENTIRE audit
 *  feed to that user (actor OR target) via `?involving_user_id=`. This is
 *  the "kis user ne kya kiya" entry point — once scoped, every row shows
 *  the order/position/login with its exact timestamp, so a user can't
 *  claim "maine order place nahi kiya". Debounced 250 ms; queries the same
 *  `/admin/users?q=` endpoint the Users table uses (which matches
 *  full_name / user_code / email / mobile). */
function UserSearchBox() {
  const [term, setTerm] = useState("");
  const [debounced, setDebounced] = useState("");
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setDebounced(term.trim()), 250);
    return () => clearTimeout(t);
  }, [term]);

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "audit", "user-search", debounced],
    queryFn: () => UsersAPI.list({ q: debounced, page_size: 8 }),
    enabled: debounced.length >= 2,
    staleTime: 60_000,
  });
  const results = ((data as any)?.items ?? []) as any[];

  return (
    <div className="relative w-full max-w-md">
      <div className="relative">
        <SearchIcon className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={term}
          onChange={(e) => {
            setTerm(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          // Delay close so a click on a result registers before blur.
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          placeholder="Search by User ID, name, email or mobile…"
          className="h-9 pl-8"
        />
      </div>
      {open && debounced.length >= 2 && (
        <div className="absolute z-20 mt-1 max-h-80 w-full overflow-auto rounded-md border border-border bg-background shadow-lg">
          {isFetching && results.length === 0 ? (
            <div className="px-3 py-2 text-xs text-muted-foreground">Searching…</div>
          ) : results.length === 0 ? (
            <div className="px-3 py-2 text-xs text-muted-foreground">No users found</div>
          ) : (
            results.map((u) => (
              <Link
                key={u.id}
                href={`/audit?involving_user_id=${u.id}`}
                onClick={() => {
                  setOpen(false);
                  setTerm("");
                }}
                className="flex flex-col gap-0.5 border-b border-border/50 px-3 py-2 text-xs last:border-0 hover:bg-muted/50"
              >
                <span className="font-medium text-foreground">
                  {u.full_name || u.user_code || String(u.id).slice(-8)}
                </span>
                <span className="font-mono text-[10px] text-muted-foreground">
                  {[u.user_code, u.email, u.mobile].filter(Boolean).join(" · ") || String(u.id)}
                </span>
              </Link>
            ))
          )}
        </div>
      )}
    </div>
  );
}


/** Preset filter chips for the audit page. Each chip maps to a
 *  semantic category that the admin actually thinks in (Edit trade,
 *  Reopen, Deposit, etc.) — internally we hand a comma-separated list
 *  of action codes + an optional entity_type whitelist to the backend.
 *  Keeping the mapping table here (not on the backend) lets the
 *  category set evolve without a deploy.
 */
const PRESETS: {
  id: string;
  label: string;
  actions?: string[];        // matches AuditAction enum values
  entity_types?: string[];   // matches the entity_type strings the
                             // log_event helpers stamp (e.g. "Position",
                             // "DepositRequest", "WithdrawalRequest")
}[] = [
  { id: "all", label: "All" },
  {
    id: "edit_trade",
    label: "Edit trade",
    actions: ["POSITION_EDIT"],
    entity_types: ["Position"],
  },
  {
    id: "close_admin",
    label: "Close by admin",
    actions: ["SQUAREOFF", "SQUAREOFF_FORCE"],
    entity_types: ["Position"],
  },
  {
    id: "reopen",
    label: "Reopen",
    actions: ["POSITION_REOPEN"],
    entity_types: ["Position"],
  },
  {
    id: "position_delete",
    label: "Position delete",
    actions: ["POSITION_DELETE"],
    entity_types: ["Position"],
  },
  {
    id: "deposit",
    label: "Deposit",
    actions: ["APPROVE", "REJECT"],
    entity_types: ["DepositRequest"],
  },
  {
    id: "withdrawal",
    label: "Withdrawal",
    actions: ["APPROVE", "REJECT"],
    entity_types: ["WithdrawalRequest"],
  },
  {
    id: "settlement",
    label: "Settlement",
    actions: ["APPROVE", "REJECT"],
    entity_types: ["SettlementRequest"],
  },
  {
    id: "kyc",
    label: "KYC",
    actions: ["APPROVE", "REJECT", "CREATE", "UPDATE"],
    entity_types: ["KycSubmission"],
  },
  {
    id: "wallet_adjust",
    label: "Wallet adjust",
    actions: ["WALLET_ADJUST"],
  },
  {
    id: "block",
    label: "Block / Unblock",
    actions: ["BLOCK", "UNBLOCK"],
  },
  {
    id: "login",
    label: "Login",
    actions: ["LOGIN", "LOGOUT", "LOGIN_FAILED"],
  },
  {
    id: "settings",
    label: "Settings change",
    actions: ["SETTING_CHANGE"],
  },
];


function AuditLogsInner() {
  const searchParams = useSearchParams();
  // `involving_user_id` is the "events involving this user as actor OR
  // target" filter — used by the user-detail Activity link so admin sees
  // user-initiated events too. `target_user_id` kept for backward compat
  // with any existing deep links.
  const queryInvolvingUserId = searchParams?.get("involving_user_id") ?? null;
  const queryTargetUserId = searchParams?.get("target_user_id") ?? null;
  const scopedUserId = queryInvolvingUserId ?? queryTargetUserId;
  const [preset, setPreset] = useState<string>("all");
  const [fromDate, setFromDate] = useState<string>("");
  const [toDate, setToDate] = useState<string>("");
  const [page, setPage] = useState(1);

  // Resolve the active preset → backend params. Empty preset = no
  // category filter.
  const activePreset = PRESETS.find((p) => p.id === preset);
  const presetActions =
    activePreset?.actions && activePreset.actions.length > 0
      ? activePreset.actions.join(",")
      : undefined;
  const presetEntityTypes =
    activePreset?.entity_types && activePreset.entity_types.length > 0
      ? activePreset.entity_types.join(",")
      : undefined;

  function selectPreset(id: string) {
    setPreset(id);
    setPage(1);
  }

  // Reset to page 1 whenever the FILTER changes (scoped user / date range).
  // Clicking a user in the search box is a client-side navigation — the
  // component never remounts, so `page` survived the filter change. If the
  // admin was on page 3 of the 44k-event global feed and then scoped to a
  // user who only has ~30 events (1 page), the query asked for page 3 of a
  // 1-page result and rendered an EMPTY table — the reported "search me naam
  // aata hai, click karo to us user ka record nahi dikhta" bug. Preset chips
  // already reset via selectPreset(); this covers the other two filters.
  useEffect(() => {
    setPage(1);
  }, [scopedUserId, fromDate, toDate]);

  const { data: scopedUser } = useQuery({
    queryKey: ["admin", "user", scopedUserId],
    queryFn: () => UsersAPI.detail(scopedUserId!),
    enabled: !!scopedUserId,
    staleTime: 5 * 60_000,
  });

  const { data, isFetching } = useQuery({
    queryKey: [
      "admin",
      "audit",
      {
        preset,
        fromDate,
        toDate,
        page,
        queryInvolvingUserId,
        queryTargetUserId,
      },
    ],
    queryFn: () =>
      SettingsAPI.audit({
        actions: presetActions,
        entity_types: presetEntityTypes,
        from_date: fromDate ? new Date(fromDate).toISOString() : undefined,
        to_date: toDate
          ? new Date(`${toDate}T23:59:59.999`).toISOString()
          : undefined,
        involving_user_id: queryInvolvingUserId || undefined,
        target_user_id: queryTargetUserId || undefined,
        page,
        page_size: 50,
      }),
  });

  // Clean 4-column ledger — Date & Time | User ID | Action By | Message.
  // Everything else (raw action code, entity, entity_id, IP, device,
  // technical JSON) intentionally dropped: the Message column already says
  // WHO did WHAT to WHOM in plain broker English.
  const cols: Column<any>[] = [
    {
      key: "created_at",
      header: "Date & Time",
      className: "whitespace-nowrap",
      render: (r) => (
        <span className="text-xs text-foreground">{fmtAuditWhen(r.created_at)}</span>
      ),
    },
    {
      // "User ID" — the account this event concerns (target if the action
      // was performed ON someone, else the actor). Rendered as a code pill
      // like the old ledger. Click to scope the whole feed to them.
      key: "user_id",
      header: "User ID",
      className: "whitespace-nowrap",
      render: (r) => {
        const owner = auditOwner(r);
        const code = (owner?.code || "").trim();
        const name = (owner?.name || "").trim();
        if (!code && !name) {
          return <span className="text-xs text-muted-foreground">—</span>;
        }
        return (
          <Link
            href={owner?.id ? `/audit?involving_user_id=${owner.id}` : "/audit"}
            className="inline-flex flex-col gap-0.5 leading-tight"
            title={`${name} ${code ? `(${code})` : ""}`.trim()}
          >
            {code && (
              <span className="inline-block w-fit rounded border border-border bg-muted/40 px-2 py-0.5 font-mono text-[11px] font-medium text-foreground group-hover:border-primary/50">
                {code}
              </span>
            )}
            {name && (
              <span className="text-[11px] font-medium text-muted-foreground hover:text-primary">
                {name}
              </span>
            )}
          </Link>
        );
      },
    },
    {
      // "Action By" — who actually performed the action (the actor). For a
      // user's own order this equals the User ID; for an admin action it's
      // the admin (e.g. TRADEVOLT-AD deleting a user's trade).
      key: "action_by",
      header: "Action By",
      className: "whitespace-nowrap",
      render: (r) => {
        const name = (r.actor?.name || r.actor?.code || "System").toString().trim();
        return <span className="text-xs font-medium text-foreground">{name}</span>;
      },
    },
    {
      key: "message",
      header: "Message",
      className: "whitespace-normal align-top",
      render: (r) => <AuditMessageCell row={r} />,
    },
    {
      // IP + Device folded into one column — IP on top, a short
      // "Chrome on Windows" / "Android app" device hint below. Full UA on
      // hover. "—" when the row has neither (system/boot rows).
      key: "ip_device",
      header: "IP / Device",
      className: "whitespace-nowrap align-top",
      render: (r) => (
        <div className="flex flex-col gap-0.5 leading-tight">
          <span className="font-mono text-[11px] text-foreground" title={r.ip_address ?? ""}>
            {r.ip_address || "—"}
          </span>
          <span className="text-[11px] text-muted-foreground" title={r.user_agent ?? ""}>
            {shortDevice(r.user_agent)}
          </span>
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <PageHeader title="Activity Logs" description={`${data?.meta?.total ?? 0} events`} />

      {/* Search a user by User ID / name / email / mobile and jump straight
          to their full activity (orders, positions, logins) with exact
          time — the "kis user ne kaunsa order place kiya" lookup. Selecting
          a result scopes the feed via involving_user_id. */}
      <UserSearchBox />

      {scopedUserId && (
        <div className="inline-flex items-center gap-2 rounded-md border border-primary/40 bg-primary/10 px-3 py-1.5 text-xs">
          <span className="text-muted-foreground">
            {queryInvolvingUserId ? "Filtered by user:" : "Filtered by target user:"}
          </span>
          <span className="font-semibold text-primary">
            {(scopedUser as any)?.user_code ?? scopedUserId.slice(-8)}
            {(scopedUser as any)?.full_name ? ` · ${(scopedUser as any).full_name}` : ""}
          </span>
          <Link
            href="/audit"
            className="grid size-5 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
            aria-label="Clear user filter"
          >
            <XIcon className="size-3" />
          </Link>
        </div>
      )}

      {/* Action Type — each chip maps to a backend `actions=...` +
          `entity_types=...` combo so the operator picks "Edit trade" /
          "Reopen" / "Deposit" / etc. without remembering enum names. The
          "All" chip clears everything to the unfiltered view. */}
      <div className="flex flex-wrap items-center gap-1.5">
        {PRESETS.map((p) => (
          <button
            key={p.id}
            type="button"
            onClick={() => selectPreset(p.id)}
            className={
              "rounded-full border px-3 py-1 text-xs font-medium transition-colors " +
              (preset === p.id
                ? "border-primary bg-primary text-primary-foreground"
                : "border-border bg-background text-muted-foreground hover:bg-muted/40 hover:text-foreground")
            }
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Date range — "today's events" / "yesterday only" investigations.
          HTML5 date pickers so no extra dep is needed. Empty either bound =
          open-ended. */}
      <div className="flex flex-wrap items-end gap-2">
        <div className="space-y-1">
          <label className="block text-[10px] uppercase tracking-wider text-muted-foreground">
            From
          </label>
          <Input
            type="date"
            value={fromDate}
            onChange={(e) => {
              setPage(1);
              setFromDate(e.target.value);
            }}
            className="h-9 w-[150px]"
          />
        </div>
        <div className="space-y-1">
          <label className="block text-[10px] uppercase tracking-wider text-muted-foreground">
            To
          </label>
          <Input
            type="date"
            value={toDate}
            onChange={(e) => {
              setPage(1);
              setToDate(e.target.value);
            }}
            className="h-9 w-[150px]"
          />
        </div>
        {(fromDate || toDate) && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setFromDate("");
              setToDate("");
              setPage(1);
            }}
            className="h-9"
          >
            <XIcon className="size-3" /> Clear dates
          </Button>
        )}
      </div>

      {/* Desktop: full table. Mobile: card list (below) — a table with 5
          columns is unreadable on a phone, so md-and-up gets the table and
          smaller screens get one card per event. */}
      <div className="hidden md:block">
        <DataTable columns={cols} rows={data?.items} keyExtractor={(r) => r.id} loading={isFetching && !data} />
      </div>

      <div className="space-y-2.5 md:hidden">
        {isFetching && !data ? (
          <div className="rounded-lg border border-border bg-card px-4 py-12 text-center text-sm text-muted-foreground">
            Loading…
          </div>
        ) : !data?.items || data.items.length === 0 ? (
          <div className="rounded-lg border border-border bg-card px-4 py-12 text-center text-sm text-muted-foreground">
            No events
          </div>
        ) : (
          data.items.map((r: any) => <AuditMobileCard key={r.id} row={r} />)
        )}
      </div>

      {(data?.meta?.total_pages ?? 1) > 1 && (
        <div className="flex justify-end gap-2 text-xs">
          <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
            Prev
          </Button>
          <span className="self-center text-muted-foreground">
            {page} / {data?.meta?.total_pages}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={page >= (data?.meta?.total_pages ?? 1)}
            onClick={() => setPage((p) => p + 1)}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────

/** Lightweight UA → "Chrome on macOS" style summariser. The Device column
 *  just needs a glance-readable hint, not a perfect parse — so no
 *  `ua-parser-js` dep. Picks "iOS app" / "Android app" for our mobile
 *  bundle, otherwise browser-on-OS. */
function shortDevice(ua: string | null | undefined): string {
  if (!ua) return "—";
  const s = ua;
  if (/MarginWealth[-\s]?Mobile|marginwealth.+Capacitor|marginwealth.+Cordova/i.test(s)) {
    if (/iPhone|iPad|iOS/i.test(s)) return "iOS app";
    if (/Android/i.test(s)) return "Android app";
    return "Mobile app";
  }
  const browser =
    /Edg\//.test(s) ? "Edge" :
    /Chrome\//.test(s) && !/Chromium/.test(s) ? "Chrome" :
    /Firefox\//.test(s) ? "Firefox" :
    /Safari\//.test(s) ? "Safari" :
    /OPR\//.test(s) ? "Opera" :
    "Browser";
  const os =
    /iPhone|iPad/.test(s) ? "iOS" :
    /Android/.test(s) ? "Android" :
    /Mac OS X|Macintosh/.test(s) ? "macOS" :
    /Windows/.test(s) ? "Windows" :
    /Linux/.test(s) ? "Linux" :
    "";
  return os ? `${browser} on ${os}` : browser;
}

/** The account an audit row is ABOUT — target if the action was performed
 *  on someone (admin edits/deletes a user's trade), else the actor (the
 *  user's own order/login). Drives the "User ID" pill. */
function auditOwner(row: any): { id?: string; name?: string | null; code?: string | null } | null {
  return row?.target && (row.target.name || row.target.code) ? row.target : (row?.actor ?? null);
}

/** "02/07/26, 09:25:06 pm" — compact date+time like the old ledger. */
function fmtAuditWhen(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-IN", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });
}

// ── Human-readable Message ───────────────────────────────────────────
//
// Renders each audit row as one plain-English broker-style sentence —
// "Order Execution : RAMESH1(RAMESH1) buy 200 Qty Of XAUUSD At 4117.11" —
// so a non-technical operator reads it at a glance. No raw JSON, no
// technical columns.

const ACTION_LABELS: Record<string, string> = {
  ORDER_PLACE: "New order",
  ORDER_CANCEL: "Order cancelled",
  ORDER_MODIFY: "Order modified",
  SQUAREOFF: "Position squared off",
  SQUAREOFF_FORCE: "Force squareoff",
  POSITION_EDIT: "Position edited",
  POSITION_REOPEN: "Position reopened",
  POSITION_DELETE: "Position deleted",
  UPDATE: "Updated",
  WALLET_ADJUST: "Wallet adjusted",
  SETTING_CHANGE: "Setting changed",
  BLOCK: "User blocked",
  UNBLOCK: "User unblocked",
  IMPERSONATE: "Impersonated user",
  LOGIN: "Logged in",
  LOGOUT: "Logged out",
  LOGIN_FAILED: "Failed login",
  CREATE: "Created",
  DELETE: "Deleted",
  APPROVE: "Approved",
  REJECT: "Rejected",
};

// `kind` discriminator on Position UPDATE rows — sub-actions live here.
const KIND_LABELS: Record<string, string> = {
  INTRADAY_TO_CARRY_CONVERSION: "MIS → NRML carry-forward",
};

function fmtMoney(v: any): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v ?? "");
  return "₹" + n.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Bare rate (no ₹) with Indian grouping, up to 4 decimals — matches the
 *  broker "At 1,278" order style. "—" for missing. */
function fmtRate(v: any): string {
  if (v == null || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return n.toLocaleString("en-IN", { maximumFractionDigits: 4 });
}

/** True when a value actually changed. Compares numerically when both
 *  sides parse as numbers (so "1302.40" == "1302.4"), else as strings. */
function changed(a: any, b: any): boolean {
  if (a == null && b == null) return false;
  const na = Number(a);
  const nb = Number(b);
  if (Number.isFinite(na) && Number.isFinite(nb)) return na !== nb;
  return String(a ?? "") !== String(b ?? "");
}

/** Build "Label old → new" fragments for every field that actually
 *  changed between old_values and new_values. `fmt` renders each side
 *  (rate / money / plain). */
function diffFragments(
  ov: any,
  nv: any,
  fields: { key: string; label: string; fmt: (v: any) => string }[],
): string[] {
  const out: string[] = [];
  for (const f of fields) {
    const a = ov?.[f.key];
    const b = nv?.[f.key];
    if (a == null && b == null) continue;
    if (!changed(a, b)) continue;
    out.push(`${f.label} ${f.fmt(a)} → ${f.fmt(b)}`);
  }
  return out;
}

/** "Name (CODE)" for a row's actor/target. Falls back to the code, the
 *  name alone, or a supplied placeholder when the backend couldn't
 *  resolve the user (e.g. system/boot rows). */
function personLabel(info: any, fallback: string): string {
  if (!info) return fallback;
  const name = (info.name || "").trim();
  const code = (info.code || "").trim();
  if (name && code) return `${name} (${code})`;
  return name || code || fallback;
}

// Sensitive actions an admin should never miss in a scan — these get an
// amber left-border + coloured text. Impersonation, money moves,
// hand-edits to positions, and block/unblock all qualify.
const WARN_ACTIONS = new Set<string>([
  "IMPERSONATE",
  "SQUAREOFF_FORCE",
  "WALLET_ADJUST",
  "POSITION_EDIT",
  "POSITION_REOPEN",
  "POSITION_DELETE",
  "BLOCK",
  "UNBLOCK",
  "DELETE",
]);

/** Build a plain-English, full-sentence description of an audit row that
 *  names WHO did WHAT to WHOM — written so a non-technical operator gets
 *  it at a glance. */
function fmtAuditMessage(row: any): { summary: string; tone: "normal" | "warn" } {
  const m = row.metadata ?? {};
  const ov = row.old_values ?? {};
  const nv = row.new_values ?? {};
  const action = String(row.action ?? "");
  const actor = personLabel(row.actor, "System");
  const target = personLabel(row.target, "");
  const sym = m.symbol ? String(m.symbol) : "";
  const tone: "normal" | "warn" = WARN_ACTIONS.has(action) ? "warn" : "normal";

  // Trading-log helpers — render order/trade rows in the clean broker
  // style: "Order Execution : amarjeet(VM1197) sell 35 Qty Of NIFTY…CE At
  // 187.04" / "Limit Order Created : ..." / "Pending Order Modified : ...".
  const ownerInfo = auditOwner(row);
  const ownerTight = (() => {
    const n = (ownerInfo?.name || "").trim();
    const c = (ownerInfo?.code || "").trim();
    return n && c ? `${n}(${c})` : n || c || actor;
  })();
  const sideTxt = m.action ? String(m.action).toLowerCase() : "";
  const qtyTxt =
    m.quantity != null ? String(m.quantity) : m.closed_qty != null ? String(m.closed_qty) : "";
  const pxTxt =
    m.price != null && m.price !== "0"
      ? Number(m.price).toLocaleString("en-IN", { maximumFractionDigits: 4 })
      : "";
  const otype = String(m.order_type || "").toUpperCase();
  const otypeLabel =
    otype === "LIMIT" ? "Limit" : otype === "SL_M" || otype === "SL" ? "SL-M" : "Market";
  const ofSym = sym ? ` Qty Of ${sym}` : "";
  const atPx = pxTxt ? ` At ${pxTxt}` : "";

  let summary: string;
  switch (action) {
    case "IMPERSONATE": {
      const role = m.as_role ? String(m.as_role).toLowerCase() : "user";
      const tgt = target || "another account";
      summary =
        `${actor} logged into ${tgt}'s account and can now act as them (${role}). ` +
        `Anything done after this — orders, closes, edits — is really ${actor}, ` +
        `even though it shows under ${tgt}'s name.`;
      break;
    }
    case "SQUAREOFF":
    case "SQUAREOFF_FORCE": {
      summary =
        `Square-off : ${ownerTight}${sym ? ` ${sym}` : ""}` +
        (qtyTxt ? ` ${qtyTxt} Qty` : "") +
        atPx +
        (action === "SQUAREOFF_FORCE" ? " (forced)" : "");
      break;
    }
    case "ORDER_PLACE": {
      // Market = immediate fill → "Order Execution". Limit / SL-M create a
      // pending order that fills later → "<Type> Order Created".
      summary =
        otype === "MARKET" || otype === ""
          ? `Order Execution : ${ownerTight} ${sideTxt} ${qtyTxt}${ofSym}${atPx}`
          : `${otypeLabel} Order Created : ${ownerTight} ${sideTxt} ${qtyTxt}${ofSym}${atPx}`;
      break;
    }
    case "ORDER_CANCEL":
      summary = `Pending Order Cancelled : ${ownerTight} ${sideTxt} ${qtyTxt}${ofSym}${atPx}`;
      break;
    case "ORDER_MODIFY": {
      // Show exactly what the user/admin changed: rate X → Y, trigger X →
      // Y, lots X → Y. Falls back to the plain "Rate: <new>" line for old
      // rows that predate old_values capture.
      const mods = diffFragments(ov, nv, [
        { key: "price", label: "Rate", fmt: fmtRate },
        { key: "trigger_price", label: "Trigger", fmt: fmtRate },
        { key: "lots", label: "Lots", fmt: (v) => (v == null ? "—" : String(v)) },
      ]);
      const chg = mods.length
        ? ` — ${mods.join(", ")}`
        : pxTxt
          ? ` — Rate: ${pxTxt}`
          : "";
      summary = `Pending Order Modified : ${ownerTight} ${otypeLabel} of ${sym || "order"}${chg}`;
      break;
    }
    case "ORDER_REJECT":
      summary = `Order Rejected : ${ownerTight} ${sideTxt} ${qtyTxt}${ofSym}${atPx}`;
      break;
    case "POSITION_EDIT": {
      // Spell out every field the admin hand-changed — avg/close price, qty,
      // SL, target, realized P&L — each as "old → new" so the operator sees
      // kya-se-kya badla, not just "edited".
      const edits = diffFragments(ov, nv, [
        { key: "avg_price", label: "Avg", fmt: fmtRate },
        { key: "close_price", label: "Close", fmt: fmtRate },
        { key: "quantity", label: "Qty", fmt: (v) => (v == null ? "—" : String(v)) },
        { key: "stop_loss", label: "SL", fmt: fmtRate },
        { key: "target", label: "Target", fmt: fmtRate },
        { key: "realized_pnl", label: "P&L", fmt: fmtMoney },
      ]);
      const chg = edits.length ? ` — ${edits.join(", ")}` : "";
      summary = `${actor} edited ${target ? `${target}'s` : "a"} ${sym || "position"}${chg}`;
      break;
    }
    case "POSITION_REOPEN":
      summary = `${actor} re-opened ${target ? `${target}'s` : "a"} closed ${sym || "position"}`;
      break;
    case "POSITION_DELETE":
      summary = `${actor} deleted ${target ? `${target}'s` : "a"} ${sym || "position"} record`;
      break;
    case "WALLET_ADJUST": {
      const amt = m.amount != null ? ` by ${fmtMoney(m.amount)}` : "";
      const kind = m.type ? ` (${m.type})` : "";
      summary = `${actor} changed ${target ? `${target}'s` : "a user's"} wallet balance${amt}${kind}`;
      break;
    }
    case "BLOCK":
      summary = `${actor} blocked ${target || "a user"} from the platform`;
      break;
    case "UNBLOCK":
      summary = `${actor} unblocked ${target || "a user"}`;
      break;
    case "APPROVE":
      summary = `${actor} approved ${row.entity_type || "a request"}${target && target !== actor ? ` for ${target}` : ""}`;
      break;
    case "REJECT":
      summary = `${actor} rejected ${row.entity_type || "a request"}${target && target !== actor ? ` for ${target}` : ""}`;
      break;
    case "LOGIN":
      summary = `${actor} logged in`;
      break;
    case "LOGOUT":
      summary = `${actor} logged out`;
      break;
    case "LOGIN_FAILED":
      summary = `Failed login attempt for ${target || actor}`;
      break;
    case "SETTING_CHANGE":
      summary = `${actor} changed a setting${m.tier ? ` (${m.tier})` : ""}`;
      break;
    case "UPDATE":
      summary =
        m.kind && KIND_LABELS[m.kind]
          ? `${actor} — ${KIND_LABELS[m.kind]}${target && target !== actor ? ` on ${target}'s position` : ""}`
          : `${actor} updated ${target && target !== actor ? `${target}'s record` : (row.entity_type || "a record")}`;
      break;
    default: {
      const label = ACTION_LABELS[action] ?? action;
      summary =
        target && target !== actor
          ? `${actor} — ${label} → ${target}`
          : `${actor} — ${label}`;
    }
  }

  return { summary, tone };
}

function AuditMessageCell({ row }: { row: any }) {
  const { summary, tone } = fmtAuditMessage(row);
  const warn = tone === "warn";
  return (
    <div
      className={
        "whitespace-normal break-words text-xs leading-snug " +
        (warn
          ? "border-l-2 border-amber-500/70 pl-2 font-medium text-amber-600 dark:text-amber-400"
          : "text-foreground")
      }
    >
      {summary}
    </div>
  );
}

/** One audit event as a phone-friendly card. Header row = User ID pill +
 *  name (left) and timestamp (right); the broker-style message fills the
 *  body; a muted footer carries "Action By" + IP / device. Sensitive
 *  actions (money moves, impersonation, hand-edits) get an amber left rail
 *  so they stand out in a scroll — same warn cue as the desktop table. */
function AuditMobileCard({ row }: { row: any }) {
  const owner = auditOwner(row);
  const code = (owner?.code || "").trim();
  const name = (owner?.name || "").trim();
  const actionBy = (row.actor?.name || row.actor?.code || "System").toString().trim();
  const { summary, tone } = fmtAuditMessage(row);
  const warn = tone === "warn";
  const device = shortDevice(row.user_agent);

  return (
    <div
      className={
        "rounded-lg border bg-card p-3 shadow-sm " +
        (warn ? "border-amber-500/40 border-l-2 border-l-amber-500/70" : "border-border")
      }
    >
      {/* Header: who + when */}
      <div className="flex items-start justify-between gap-2">
        <Link
          href={owner?.id ? `/audit?involving_user_id=${owner.id}` : "/audit"}
          className="flex min-w-0 flex-col gap-0.5"
        >
          {code && (
            <span className="w-fit rounded border border-border bg-muted/40 px-2 py-0.5 font-mono text-[11px] font-medium text-foreground">
              {code}
            </span>
          )}
          {name && (
            <span className="truncate text-[12px] font-medium text-foreground">{name}</span>
          )}
        </Link>
        <span className="shrink-0 text-[10px] leading-tight text-muted-foreground">
          {fmtAuditWhen(row.created_at)}
        </span>
      </div>

      {/* Message */}
      <p
        className={
          "mt-2 whitespace-normal break-words text-[13px] leading-snug " +
          (warn ? "font-medium text-amber-600 dark:text-amber-400" : "text-foreground")
        }
      >
        {summary}
      </p>

      {/* Footer: action-by + ip/device */}
      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border/60 pt-2 text-[10px] text-muted-foreground">
        {actionBy && actionBy !== name && (
          <span>
            By <span className="font-medium text-foreground/80">{actionBy}</span>
          </span>
        )}
        {row.ip_address && (
          <span className="font-mono" title={row.ip_address}>
            {row.ip_address}
          </span>
        )}
        {device && device !== "—" && <span title={row.user_agent ?? ""}>{device}</span>}
      </div>
    </div>
  );
}
