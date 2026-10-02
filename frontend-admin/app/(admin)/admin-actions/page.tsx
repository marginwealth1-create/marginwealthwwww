"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { SettingsAPI } from "@/lib/api";
import { PageHeader } from "@/components/common/PageHeader";
import { cn } from "@/lib/utils";

// ─────────────────────────────────────────────────────────────────────────
// Admin Actions — a focused audit view of the three irreversible-ish admin
// operations the operator wanted on one clean screen, each with WHO did it,
// WHEN (IST), and (for edits) the BEFORE → AFTER values. All three read the
// existing /admin/audit/logs feed, just filtered by action:
//   • Deleted Users     → action=DELETE,         entity_type=User
//   • Deleted Positions → action=POSITION_DELETE  (symbol + reversed P&L in metadata)
//   • Edited Positions  → action=POSITION_EDIT    (old_values vs new_values)
// Data is the canonical audit_logs collection, so it stays in sync with the
// generic Audit Logs page — this is just the operator's curated cut.
//
// Layout: desktop gets a clean colour-coded table; on phones each row becomes
// a tap-friendly card (no horizontal scroll). Edits show ONLY the fields that
// actually changed so the "what did the admin touch" answer is obvious at a
// glance instead of a wall of "—".
// ─────────────────────────────────────────────────────────────────────────

type TabKey = "deleted_users" | "deleted_positions" | "edited_positions";

const TABS: {
  key: TabKey;
  label: string;
  params: Record<string, string>;
  accent: string; // left-accent + chip colour per action
  chip: string;
}[] = [
  { key: "deleted_users", label: "Deleted Users", params: { actions: "DELETE", entity_types: "User" }, accent: "before:bg-destructive", chip: "bg-destructive/10 text-destructive" },
  { key: "deleted_positions", label: "Deleted Positions", params: { actions: "POSITION_DELETE" }, accent: "before:bg-atm", chip: "bg-atm/10 text-atm" },
  { key: "edited_positions", label: "Edited Positions", params: { actions: "POSITION_EDIT" }, accent: "before:bg-info", chip: "bg-info/10 text-info" },
];

function fmtDT(x: any): string {
  if (!x) return "—";
  try {
    return new Date(x).toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return String(x);
  }
}

function userLabel(u: any): { name: string; code: string } {
  if (!u) return { name: "—", code: "" };
  return { name: u.name ?? u.id ?? "—", code: u.code ?? "" };
}

// ── value formatters ──────────────────────────────────────────────────────
function money(x: any): string {
  if (x == null || x === "") return "—";
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return `${n < 0 ? "-" : ""}₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}
function price(x: any): string {
  if (x == null || x === "") return "—";
  const n = Number(x);
  if (!Number.isFinite(n)) return "—";
  return `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 4 })}`;
}
function plain(x: any): string {
  if (x == null || x === "") return "—";
  return String(x);
}

/** BUY = profit-green, SELL = loss-red (matches the locked trade theme). */
function sideTone(side: any): string {
  const s = String(side ?? "").toUpperCase();
  if (s === "BUY") return "text-profit";
  if (s === "SELL") return "text-loss";
  return "text-foreground";
}

/** "N lot · Q qty" from a deleted-position's opening_quantity + lot_size. */
function lotsLabel(md: any): string {
  const qty = Number(md?.opening_quantity);
  const ls = Number(md?.lot_size);
  if (!Number.isFinite(qty) || qty <= 0) return "—";
  if (Number.isFinite(ls) && ls > 0) {
    const lots = qty / ls;
    const lotStr = Number.isInteger(lots) ? String(lots) : lots.toFixed(2);
    return `${lotStr} lot · ${qty} qty`;
  }
  return `${qty} qty`;
}

/** Small label (muted) over a BOLD value — deleted-position detail grid. */
function Detail({ label, value, valueClass }: { label: string; value: any; valueClass?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className={cn("truncate font-bold", valueClass)}>{value ?? "—"}</div>
    </div>
  );
}

type Tone = "money" | "price" | "plain";
function hasChanged(before: any, after: any): boolean {
  const b = Number(before),
    a = Number(after);
  if (Number.isFinite(b) && Number.isFinite(a)) return b !== a;
  return (before ?? null) !== (after ?? null);
}

/** Two-line user cell: name on top, mono code below. */
function User({ u }: { u: any }) {
  const { name, code } = userLabel(u);
  return (
    <div className="min-w-0">
      <div className="truncate font-medium">{name}</div>
      {code && <div className="truncate font-mono text-[11px] text-muted-foreground">{code}</div>}
    </div>
  );
}

/** A symbol chip with optional exchange · side subtitle. */
function SymbolChip({ md, ov, nv }: { md: any; ov: any; nv: any }) {
  const sym = md?.symbol ?? md?.trading_symbol ?? ov?.symbol ?? nv?.symbol ?? null;
  const exch = md?.exchange ?? md?.segment ?? "";
  const side = md?.opened_side ?? "";
  if (!sym) return <span className="text-muted-foreground">—</span>;
  return (
    <div className="flex flex-col leading-tight">
      <span className="font-medium">{sym}</span>
      {(exch || side) && (
        <span className="text-[11px] text-muted-foreground">{[exch, side].filter(Boolean).join(" · ")}</span>
      )}
    </div>
  );
}

/** ALWAYS renders `before → after`, both values shown. When nothing
 *  changed, both sides are the SAME value in muted grey (no colour, no
 *  strike-through) — so every cell reads the same way and the operator
 *  can scan "kya tha → kya hua" without guessing. When it changed, the
 *  BEFORE is struck-through grey and the AFTER is coloured. */
function BeforeAfter({ before, after, tone = "plain" }: { before: any; after: any; tone?: Tone }) {
  const fmt = tone === "money" ? money : tone === "price" ? price : plain;
  const changed = hasChanged(before, after);
  const b = Number(before),
    a = Number(after);
  const numeric = Number.isFinite(b) && Number.isFinite(a);
  let afterClass = "text-muted-foreground";
  if (changed) {
    if (tone === "money" && numeric) afterClass = a >= b ? "font-semibold text-profit" : "font-semibold text-loss";
    else if (tone === "price") afterClass = "font-semibold text-primary";
    else afterClass = "font-semibold text-foreground";
  }
  return (
    <span className="inline-flex items-center gap-1 whitespace-nowrap font-tabular tabular-nums text-xs">
      <span className={changed ? "text-muted-foreground line-through" : "text-muted-foreground"}>{fmt(before)}</span>
      <span className="text-muted-foreground/70">→</span>
      <span className={afterClass}>{fmt(after)}</span>
    </span>
  );
}

/** Small coloured chips naming WHICH fields the admin touched. */
function EditedChips({ ov, nv }: { ov: any; nv: any }) {
  const changes = editChanges(ov, nv);
  if (changes.length === 0) {
    return <span className="text-[10px] text-muted-foreground">no value change</span>;
  }
  return (
    <div className="flex flex-wrap gap-1">
      {changes.map((c, i) => (
        <span key={i} className="rounded bg-info/10 px-1.5 py-0.5 text-[10px] font-medium text-info">
          {c.label}
        </span>
      ))}
    </div>
  );
}

/** Plain-language one-liner explaining WHY the values moved. */
function editReason(ov: any, nv: any): string | null {
  const avg = hasChanged(ov?.avg_price, nv?.avg_price);
  const close = hasChanged(ov?.close_price, nv?.close_price);
  const realized = hasChanged(ov?.realized_pnl, nv?.realized_pnl);
  const qty = hasChanged(ov?.quantity, nv?.quantity);
  if ((avg || close) && realized) {
    const which = avg && close ? "open & close price" : avg ? "open price" : "close price";
    return `Realized P&L auto-recalculated from the ${which} edit.`;
  }
  if (realized && !avg && !close) return "Realized P&L overridden manually.";
  if (qty) return "Position quantity edited.";
  if (avg || close) return `${avg ? "Open" : "Close"} price edited (no P&L change recorded).`;
  return null;
}

/** Build the list of fields that actually changed on an edit (for cards). */
function editChanges(ov: any, nv: any): { label: string; before: any; after: any; tone: Tone }[] {
  const candidates: { label: string; key: string; tone: Tone }[] = [
    { label: "Avg / open", key: "avg_price", tone: "price" },
    { label: "Close price", key: "close_price", tone: "price" },
    { label: "Realized P&L", key: "realized_pnl", tone: "money" },
    { label: "Qty", key: "quantity", tone: "plain" },
    { label: "Stop loss", key: "stop_loss", tone: "price" },
    { label: "Target", key: "target", tone: "price" },
    { label: "Status", key: "status", tone: "plain" },
    { label: "Close reason", key: "close_reason", tone: "plain" },
  ];
  return candidates
    .filter((c) => hasChanged(ov?.[c.key], nv?.[c.key]))
    .map((c) => ({ label: c.label, before: ov?.[c.key], after: nv?.[c.key], tone: c.tone }));
}

export default function AdminActionsPage() {
  const [tab, setTab] = useState<TabKey>("edited_positions");
  const active = TABS.find((t) => t.key === tab)!;

  const { data, isFetching } = useQuery({
    queryKey: ["admin-actions", tab],
    queryFn: () => SettingsAPI.audit({ ...active.params, page: 1, page_size: 100 }),
    refetchOnWindowFocus: false,
  });
  const items: any[] = (data as any)?.items ?? [];
  const empty = !isFetching && items.length === 0;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Admin Actions"
        description="Deleted users, deleted positions & position edits — kisne, kab (IST) & before → after"
      />

      {/* Tabs */}
      <div className="flex flex-wrap gap-2">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            onClick={() => setTab(t.key)}
            className={cn(
              "rounded-full px-3.5 py-1.5 text-sm font-medium transition-colors",
              tab === t.key
                ? "bg-primary text-primary-foreground shadow-sm"
                : "bg-muted text-muted-foreground hover:bg-muted/70 hover:text-foreground",
            )}
          >
            {t.label}
            {tab === t.key && items.length > 0 && (
              <span className="ml-2 rounded-full bg-primary-foreground/20 px-1.5 text-xs">{items.length}</span>
            )}
          </button>
        ))}
      </div>

      {/* Loading / empty */}
      {isFetching && (
        <div className="rounded-xl border border-border bg-card p-8 text-center text-sm text-muted-foreground">
          Loading…
        </div>
      )}
      {empty && (
        <div className="rounded-xl border border-dashed border-border bg-card p-10 text-center text-sm text-muted-foreground">
          Is category me kuch nahi mila.
        </div>
      )}

      {/* ── DESKTOP TABLE (md+) ─────────────────────────────────────── */}
      {!isFetching && items.length > 0 && (
        <div className="hidden overflow-hidden rounded-xl border border-border bg-card md:block">
          <table className="w-full text-sm">
            <thead className="border-b border-border bg-muted/30 text-left text-[11px] uppercase tracking-wider text-muted-foreground">
              {tab === "deleted_users" && (
                <tr>
                  <th className="px-4 py-3">User (deleted)</th>
                  <th className="px-4 py-3">Deleted by</th>
                  <th className="px-4 py-3 text-right">Date & Time (IST)</th>
                </tr>
              )}
              {tab === "deleted_positions" && (
                <tr>
                  <th className="px-4 py-3">User</th>
                  <th className="px-4 py-3">Symbol</th>
                  <th className="px-4 py-3">Side / Lots</th>
                  <th className="px-4 py-3">Opened (IST)</th>
                  <th className="px-4 py-3">Closed (IST)</th>
                  <th className="px-4 py-3 text-right">P&L reversed</th>
                  <th className="px-4 py-3">Deleted by</th>
                  <th className="px-4 py-3 text-right">Deleted at (IST)</th>
                </tr>
              )}
              {tab === "edited_positions" && (
                <tr>
                  <th className="px-4 py-3">User</th>
                  <th className="px-4 py-3">Symbol / changed</th>
                  <th className="px-4 py-3">Qty (before → after)</th>
                  <th className="px-4 py-3">Open price (before → after)</th>
                  <th className="px-4 py-3">Close price (before → after)</th>
                  <th className="px-4 py-3">Realized P&L (before → after)</th>
                  <th className="px-4 py-3">Edited by</th>
                  <th className="px-4 py-3 text-right">Date & Time (IST)</th>
                </tr>
              )}
            </thead>
            <tbody>
              {tab === "deleted_users" &&
                items.map((r) => (
                  <tr key={r.id} className="border-b border-border/40 last:border-0 hover:bg-muted/20">
                    <td className="px-4 py-3"><User u={r.target ?? r.actor} /></td>
                    <td className="px-4 py-3"><User u={r.actor} /></td>
                    <td className="px-4 py-3 text-right text-xs text-muted-foreground">{fmtDT(r.created_at)}</td>
                  </tr>
                ))}

              {tab === "deleted_positions" &&
                items.map((r) => {
                  const md = r.metadata ?? {};
                  return (
                  <tr key={r.id} className="border-b border-border/40 last:border-0 align-top hover:bg-muted/20">
                    <td className="px-4 py-3"><User u={r.target} /></td>
                    <td className="px-4 py-3">
                      <div className="font-semibold">{md.symbol ?? "—"}</div>
                      <div className="text-[11px] text-muted-foreground">{[md.exchange, md.product_type].filter(Boolean).join(" · ")}</div>
                    </td>
                    <td className="px-4 py-3">
                      <div className={cn("font-bold", sideTone(md.opened_side))}>{md.opened_side ?? "—"}</div>
                      <div className="text-[11px] text-muted-foreground">{lotsLabel(md)}</div>
                    </td>
                    <td className="px-4 py-3 text-xs font-medium">{fmtDT(md.opened_at)}</td>
                    <td className="px-4 py-3 text-xs font-medium">{fmtDT(md.closed_at)}</td>
                    <td className={cn(
                      "px-4 py-3 text-right font-tabular tabular-nums font-bold",
                      Number(md.realized_pnl_reversed_inr) < 0 ? "text-loss" : Number(md.realized_pnl_reversed_inr) > 0 ? "text-profit" : "text-muted-foreground",
                    )}>
                      {money(md.realized_pnl_reversed_inr)}
                    </td>
                    <td className="px-4 py-3"><User u={r.actor} /></td>
                    <td className="px-4 py-3 text-right text-xs font-medium text-foreground">{fmtDT(r.created_at)}</td>
                  </tr>
                  );
                })}

              {tab === "edited_positions" &&
                items.map((r) => {
                  const ov = r.old_values ?? {};
                  const nv = r.new_values ?? {};
                  const md = r.metadata ?? {};
                  const qtyChanged = hasChanged(ov.quantity, nv.quantity);
                  // Qty: when unchanged, the position's signed quantity is
                  // 0 on a closed row, so fall back to the real lot size
                  // (opening_quantity) and show it on BOTH sides.
                  const qBefore = qtyChanged ? ov.quantity : (md.opening_quantity ?? ov.quantity);
                  const qAfter = qtyChanged ? nv.quantity : (md.opening_quantity ?? ov.quantity);
                  const reason = editReason(ov, nv);
                  return (
                    <tr key={r.id} className="border-b border-border/40 last:border-0 align-top hover:bg-muted/20">
                      <td className="px-4 py-3"><User u={r.target} /></td>
                      <td className="px-4 py-3">
                        <SymbolChip md={md} ov={ov} nv={nv} />
                        <div className="mt-1"><EditedChips ov={ov} nv={nv} /></div>
                      </td>
                      <td className="px-4 py-3"><BeforeAfter before={qBefore} after={qAfter} tone="plain" /></td>
                      <td className="px-4 py-3"><BeforeAfter before={ov.avg_price} after={nv.avg_price} tone="price" /></td>
                      <td className="px-4 py-3"><BeforeAfter before={ov.close_price} after={nv.close_price} tone="price" /></td>
                      <td className="px-4 py-3">
                        <BeforeAfter before={ov.realized_pnl} after={nv.realized_pnl} tone="money" />
                        {reason && hasChanged(ov.realized_pnl, nv.realized_pnl) && (
                          <div className="mt-1 max-w-[220px] text-[10px] italic leading-tight text-muted-foreground">{reason}</div>
                        )}
                      </td>
                      <td className="px-4 py-3"><User u={r.actor} /></td>
                      <td className="px-4 py-3 text-right text-xs text-muted-foreground">{fmtDT(r.created_at)}</td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>
      )}

      {/* ── MOBILE CARDS (<md) ──────────────────────────────────────── */}
      {!isFetching && items.length > 0 && (
        <div className="space-y-2.5 md:hidden">
          {items.map((r) => {
            const ov = r.old_values ?? {};
            const nv = r.new_values ?? {};
            const md = r.metadata ?? {};
            const chip = active.chip;
            const accent = active.accent;
            const chipLabel =
              tab === "deleted_users" ? "Deleted user" : tab === "deleted_positions" ? "Deleted position" : "Edited";
            const targetUser = tab === "deleted_users" ? (r.target ?? r.actor) : r.target;

            return (
              <div
                key={r.id}
                className={cn(
                  "relative overflow-hidden rounded-xl border border-border bg-card p-3 pl-4 shadow-sm",
                  "before:absolute before:inset-y-2 before:left-1 before:w-1 before:rounded-full",
                  accent,
                )}
              >
                {/* Header: user + action chip + time */}
                <div className="flex items-start justify-between gap-2">
                  <User u={targetUser} />
                  <div className="shrink-0 text-right">
                    <span className={cn("rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase", chip)}>
                      {chipLabel}
                    </span>
                    <div className="mt-1 text-[10px] text-muted-foreground">{fmtDT(r.created_at)}</div>
                  </div>
                </div>

                {/* Symbol + meta for position rows */}
                {tab !== "deleted_users" && (
                  <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                    <span className="rounded-md bg-muted px-2 py-0.5 font-medium">
                      {md.symbol ?? md.trading_symbol ?? "—"}
                    </span>
                    {(md.exchange || md.opened_side) && (
                      <span className="text-[11px] text-muted-foreground">
                        {[md.exchange ?? md.segment, md.opened_side].filter(Boolean).join(" · ")}
                      </span>
                    )}
                    {md.opening_quantity != null && (
                      <span className="text-[11px] text-muted-foreground">qty {md.opening_quantity}</span>
                    )}
                  </div>
                )}

                {/* Deleted position: full bold detail — side, lots, open /
                    close time, and exactly when the admin deleted it. */}
                {tab === "deleted_positions" && (
                  <>
                    <div className="mt-2.5 grid grid-cols-2 gap-x-3 gap-y-2.5 rounded-lg bg-muted/30 p-3">
                      <Detail label="Buy / Sell" value={md.opened_side ?? "—"} valueClass={sideTone(md.opened_side)} />
                      <Detail label="Lots / Qty" value={lotsLabel(md)} />
                      <Detail label="Open time" value={fmtDT(md.opened_at)} />
                      <Detail label="Close time" value={fmtDT(md.closed_at)} />
                      <Detail label="Deleted at" value={fmtDT(r.created_at)} />
                      <Detail label="Deleted by" value={userLabel(r.actor).name} />
                    </div>
                    <div className="mt-2 flex items-center justify-between rounded-lg bg-muted/30 px-3 py-2.5">
                      <span className="text-xs font-medium text-muted-foreground">P&L reversed</span>
                      <span className={cn(
                        "font-tabular text-base tabular-nums font-bold",
                        Number(md.realized_pnl_reversed_inr) < 0 ? "text-loss" : Number(md.realized_pnl_reversed_inr) > 0 ? "text-profit" : "text-foreground",
                      )}>
                        {money(md.realized_pnl_reversed_inr)}
                      </span>
                    </div>
                  </>
                )}

                {/* Edited: ALL four metrics, each before → after (same value
                    both sides when unchanged) + a plain-language why-line. */}
                {tab === "edited_positions" && (() => {
                  const qChanged = hasChanged(ov.quantity, nv.quantity);
                  const qB = qChanged ? ov.quantity : (md.opening_quantity ?? ov.quantity);
                  const qA = qChanged ? nv.quantity : (md.opening_quantity ?? ov.quantity);
                  const rows: { label: string; before: any; after: any; tone: Tone }[] = [
                    { label: "Qty", before: qB, after: qA, tone: "plain" },
                    { label: "Open price", before: ov.avg_price, after: nv.avg_price, tone: "price" },
                    { label: "Close price", before: ov.close_price, after: nv.close_price, tone: "price" },
                    { label: "Realized P&L", before: ov.realized_pnl, after: nv.realized_pnl, tone: "money" },
                  ];
                  const reason = editReason(ov, nv);
                  return (
                    <>
                      <div className="mt-2.5 divide-y divide-border/40 rounded-lg bg-muted/30 px-2.5">
                        {rows.map((c, i) => {
                          const ch = hasChanged(c.before, c.after);
                          return (
                            <div key={i} className="flex items-center justify-between gap-2 py-1.5 text-xs">
                              <span className="flex shrink-0 items-center gap-1.5 font-medium text-muted-foreground">
                                {c.label}
                                {ch && <span className="rounded bg-info/10 px-1 text-[9px] font-semibold uppercase text-info">edited</span>}
                              </span>
                              <BeforeAfter before={c.before} after={c.after} tone={c.tone} />
                            </div>
                          );
                        })}
                      </div>
                      {reason && (
                        <div className="mt-1.5 flex items-start gap-1 text-[11px] italic leading-snug text-muted-foreground">
                          <span aria-hidden>ℹ</span>
                          <span>{reason}</span>
                        </div>
                      )}
                    </>
                  );
                })()}

                {/* Footer: who did it */}
                <div className="mt-2 text-[10px] text-muted-foreground">
                  by <span className="font-medium text-foreground/70">{userLabel(r.actor).name}</span>
                  {userLabel(r.actor).code ? ` · ${userLabel(r.actor).code}` : ""}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {!isFetching && items.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Latest {items.length} dikha rahe hain. Data audit_logs se aata hai (same source as Audit Logs page) — scope tumhare pool tak. Symbol/close-price purane edits me blank ho sakte hain (wo fields baad me add hue).
        </p>
      )}
    </div>
  );
}
