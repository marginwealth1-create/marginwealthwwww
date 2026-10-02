"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  Activity,
  ArrowDownRight,
  ArrowUpRight,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  Users as UsersIcon,
  X,
} from "lucide-react";
import { AdminMarketwatchAPI, UsersAPI } from "@/lib/api";
import { useMarketStream } from "@/lib/useMarketStream";
import { PageHeader } from "@/components/common/PageHeader";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

/**
 * Admin Market Watch — per-segment instrument lists + place orders on
 * behalf of users in the admin's scope.
 *
 * Reference layout is the sibling project's admin.stock4x.com/marketwatch
 * — chip strip across the top, search bar that scopes to the active
 * chip, a Bid/Ask/LTP/Change table for the items the admin has added
 * under that chip, and a Place Order modal triggered by tapping a row.
 *
 * Backend lives at /admin/marketwatch/* and reuses the existing
 * Watchlist + WatchlistItem collections (admin.id as owner, a
 * dedicated "__adminseg_<SEG>" name prefix to keep these rows visually
 * separate from trader watchlists).
 */

type Bucket = {
  key: string; // backend segment key (must match _SEG_MAP in marketwatch.py)
  label: string;
};

const BUCKETS: Bucket[] = [
  { key: "NSE_EQUITY",     label: "NSE Equity" },
  { key: "NSE_FUTURES",    label: "NSE Futures" },
  { key: "NSE_OPTIONS",    label: "NSE Options" },
  { key: "BSE_EQUITY",     label: "BSE Equity" },
  { key: "BSE_FUTURES",    label: "BSE Futures" },
  { key: "BSE_OPTIONS",    label: "BSE Options" },
  { key: "MCX_FUTURES",    label: "MCX Futures" },
  { key: "MCX_OPTIONS",    label: "MCX Options" },
  { key: "CRYPTO_OPTIONS", label: "Crypto Options" },
  { key: "CRYPTO",         label: "Crypto" },
  { key: "FOREX",          label: "Forex" },
  { key: "STOCKS",         label: "Stocks" },
  { key: "INDICES",        label: "Indices" },
  { key: "COMMODITIES",    label: "Commodities" },
];

const LIVE_TOKEN_CAP = 30;

export default function AdminMarketWatchPage() {
  const qc = useQueryClient();
  const [bucketKey, setBucketKey] = useState<string>("NSE_EQUITY");
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [orderToken, setOrderToken] = useState<string | null>(null);
  const [orderSymbol, setOrderSymbol] = useState<string>("");
  const [orderExchange, setOrderExchange] = useState<string>("");

  const bucket = BUCKETS.find((b) => b.key === bucketKey) ?? BUCKETS[0];

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 200);
    return () => clearTimeout(t);
  }, [search]);

  // Clear search when bucket changes — different segment, stale results.
  useEffect(() => {
    setSearch("");
    setDebouncedSearch("");
  }, [bucketKey]);

  // ── Data ────────────────────────────────────────────────────────
  const { data: items, refetch: refetchItems, isFetching: itemsFetching } = useQuery<any[]>({
    queryKey: ["admin-marketwatch", "items", bucketKey],
    queryFn: () => AdminMarketwatchAPI.segmentItems(bucketKey),
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });

  const { data: searchHits } = useQuery<any[]>({
    queryKey: ["admin-marketwatch", "search", bucketKey, debouncedSearch],
    queryFn: () => AdminMarketwatchAPI.search(bucketKey, debouncedSearch, 30),
    enabled: debouncedSearch.length >= 2,
    staleTime: 60_000,
  });

  const itemTokens = useMemo<string[]>(
    () => (items ?? []).map((it) => String(it.instrument_token)),
    [items],
  );

  // Live ticks for everything currently on the screen, capped at 30.
  const visibleTokens = useMemo<string[]>(
    () => itemTokens.slice(0, LIVE_TOKEN_CAP),
    [itemTokens],
  );

  // REST seed: items + live quote snapshot in one round-trip. Keeps
  // bid/ask filled even before the WS connects.
  const { data: seedQuotes } = useQuery<any[]>({
    queryKey: ["admin-marketwatch", "quotes", bucketKey],
    queryFn: () => AdminMarketwatchAPI.quotes(bucketKey),
    enabled: itemTokens.length > 0,
    refetchInterval: 5_000,
    staleTime: 4_000,
    refetchOnWindowFocus: false,
  });

  const streamQuotes = useMarketStream(visibleTokens);

  const quoteByToken = useMemo(() => {
    const map = new Map<string, any>();
    for (const q of seedQuotes ?? []) map.set(String(q.instrument_token), q);
    streamQuotes.forEach((q, tok) => map.set(tok, q));
    return map;
  }, [seedQuotes, streamQuotes]);

  // Set of tokens already added — used to gate the "+" button in the
  // search results dropdown.
  const addedTokenSet = useMemo(() => {
    const s = new Set<string>();
    for (const tok of itemTokens) s.add(tok);
    return s;
  }, [itemTokens]);

  // ── Mutations ───────────────────────────────────────────────────
  async function handleAdd(token: string, symbol: string) {
    try {
      await AdminMarketwatchAPI.addItem(bucketKey, token);
      qc.invalidateQueries({ queryKey: ["admin-marketwatch", "items", bucketKey] });
      qc.invalidateQueries({ queryKey: ["admin-marketwatch", "quotes", bucketKey] });
      toast.success(`Added ${symbol} to ${bucket.label}`, { duration: 1500 });
    } catch (e: any) {
      toast.error(e?.message || "Failed to add");
    }
  }

  async function handleRemove(token: string, symbol: string) {
    try {
      await AdminMarketwatchAPI.removeItem(bucketKey, token);
      qc.invalidateQueries({ queryKey: ["admin-marketwatch", "items", bucketKey] });
      qc.invalidateQueries({ queryKey: ["admin-marketwatch", "quotes", bucketKey] });
      toast.success(`Removed ${symbol}`, { duration: 1500 });
    } catch (e: any) {
      toast.error(e?.message || "Failed to remove");
    }
  }

  function openOrderModal(it: any) {
    setOrderToken(String(it.instrument_token));
    setOrderSymbol(it.symbol);
    setOrderExchange(it.exchange);
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Market Watch"
        description="Maintain a per-segment instrument list with live quotes and place orders on behalf of users in your scope."
        actions={
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              refetchItems();
              qc.invalidateQueries({ queryKey: ["admin-marketwatch", "quotes", bucketKey] });
            }}
            disabled={itemsFetching}
          >
            <RefreshCw className={cn("mr-1 size-4", itemsFetching && "animate-spin")} />
            Refresh
          </Button>
        }
      />

      {/* Segment chips */}
      <div className="rounded-lg border border-border bg-card p-3">
        <div
          className="-mx-1 flex flex-wrap gap-1.5 px-1"
        >
          {BUCKETS.map((b) => {
            const active = b.key === bucketKey;
            return (
              <button
                key={b.key}
                type="button"
                onClick={() => setBucketKey(b.key)}
                className={cn(
                  "h-8 shrink-0 whitespace-nowrap rounded-md border px-3 text-xs font-medium transition-colors",
                  active
                    ? "border-primary bg-primary text-primary-foreground shadow-sm"
                    : "border-border bg-card text-muted-foreground hover:border-primary/40 hover:text-foreground",
                )}
              >
                {b.label}
              </button>
            );
          })}
        </div>
      </div>

      {/* Search bar */}
      <div className="rounded-lg border border-border bg-card p-3">
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={`Search ${bucket.label} (type at least 2 characters)...`}
            className="h-10 pl-9 pr-9 text-sm"
          />
          {search && (
            <button
              type="button"
              onClick={() => setSearch("")}
              aria-label="Clear search"
              className="absolute right-2 top-1/2 grid size-7 -translate-y-1/2 place-items-center rounded text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              <X className="size-4" />
            </button>
          )}
        </div>

        {/* Search results — dropdown panel that shows symbols matching
            the query within the current chip's segment. Each row has a
            "+" to add (or a ✓ if already added). */}
        {debouncedSearch.length >= 2 && (
          <div className="mt-2 max-h-72 overflow-y-auto rounded-md border border-border bg-background">
            {(searchHits ?? []).length === 0 && (
              <div className="px-3 py-6 text-center text-xs text-muted-foreground">
                No instruments match "{debouncedSearch}" in {bucket.label}.
              </div>
            )}
            {(searchHits ?? []).map((s: any) => {
              const tok = String(s.token);
              const already = addedTokenSet.has(tok);
              return (
                <div
                  key={tok}
                  className="flex items-center gap-3 border-b border-border/60 px-3 py-2 text-sm last:border-b-0"
                >
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-medium">{s.symbol}</div>
                    <div className="truncate text-[11px] text-muted-foreground">
                      {s.exchange} · {s.segment}
                      {s.name && s.name !== s.symbol ? ` · ${s.name}` : null}
                    </div>
                  </div>
                  {already ? (
                    <span className="text-[11px] font-semibold text-emerald-500">✓ Added</span>
                  ) : (
                    <button
                      type="button"
                      onClick={() => handleAdd(tok, s.symbol)}
                      className="grid size-8 place-items-center rounded-md text-primary hover:bg-primary/10"
                      title={`Add to ${bucket.label}`}
                    >
                      <Plus className="size-4" />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Items table */}
      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[920px] text-sm">
            <thead className="bg-muted/30 text-[11px] uppercase tracking-wider text-muted-foreground">
              <tr>
                <th className="px-3 py-2.5 text-left">Symbol</th>
                <th className="px-3 py-2.5 text-right">Bid</th>
                <th className="px-3 py-2.5 text-right">Ask</th>
                <th className="px-3 py-2.5 text-right">LTP</th>
                <th className="px-3 py-2.5 text-right">Change</th>
                <th className="px-3 py-2.5 text-right">Change%</th>
                <th className="px-3 py-2.5 text-right">High</th>
                <th className="px-3 py-2.5 text-right">Low</th>
                <th className="px-3 py-2.5 text-right">Open</th>
                <th className="px-3 py-2.5 text-right">Close</th>
                <th className="px-3 py-2.5 text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="font-tabular">
              {(items ?? []).length === 0 && (
                <tr>
                  <td
                    colSpan={11}
                    className="px-3 py-16 text-center text-sm text-muted-foreground"
                  >
                    No instruments yet in <span className="font-semibold text-foreground">{bucket.label}</span>.
                    Search above to add.
                  </td>
                </tr>
              )}
              {(items ?? []).map((it: any) => {
                const tok = String(it.instrument_token);
                const q = quoteByToken.get(tok);
                const bid = q?.bid;
                const ask = q?.ask;
                const ltp = q?.ltp;
                const change = q?.change;
                const chgPct = q?.change_pct;
                const high = q?.high;
                const low = q?.low;
                const open = q?.open;
                const close = q?.close;
                return (
                  <tr
                    key={tok}
                    onClick={() => openOrderModal(it)}
                    className="cursor-pointer border-t border-border/60 transition-colors hover:bg-accent/30"
                  >
                    <td className="px-3 py-2.5">
                      <div className="font-semibold">{it.symbol}</div>
                      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                        {it.exchange}
                      </div>
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-red-500">
                      {bid != null && Number(bid) > 0 ? Number(bid).toFixed(2) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-emerald-500">
                      {ask != null && Number(ask) > 0 ? Number(ask).toFixed(2) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums font-medium">
                      {ltp != null && Number(ltp) > 0 ? Number(ltp).toFixed(2) : "—"}
                    </td>
                    <td
                      className={cn(
                        "px-3 py-2.5 text-right tabular-nums",
                        change == null
                          ? "text-muted-foreground"
                          : Number(change) > 0
                            ? "text-emerald-500"
                            : Number(change) < 0
                              ? "text-red-500"
                              : "text-muted-foreground",
                      )}
                    >
                      {change != null && Number(change) !== 0
                        ? `${Number(change) >= 0 ? "+" : ""}${Number(change).toFixed(2)}`
                        : "—"}
                    </td>
                    <td
                      className={cn(
                        "px-3 py-2.5 text-right tabular-nums",
                        chgPct == null
                          ? "text-muted-foreground"
                          : Number(chgPct) > 0
                            ? "text-emerald-500"
                            : Number(chgPct) < 0
                              ? "text-red-500"
                              : "text-muted-foreground",
                      )}
                    >
                      {chgPct != null ? `${Number(chgPct) >= 0 ? "+" : ""}${Number(chgPct).toFixed(2)}%` : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-muted-foreground">
                      {high != null && Number(high) > 0 ? Number(high).toFixed(2) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-muted-foreground">
                      {low != null && Number(low) > 0 ? Number(low).toFixed(2) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-muted-foreground">
                      {open != null && Number(open) > 0 ? Number(open).toFixed(2) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-muted-foreground">
                      {close != null && Number(close) > 0 ? Number(close).toFixed(2) : "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          handleRemove(tok, it.symbol);
                        }}
                        className="grid size-8 place-items-center rounded-md text-muted-foreground hover:bg-red-500/10 hover:text-red-500"
                        title="Remove from list"
                        aria-label={`Remove ${it.symbol}`}
                      >
                        <Trash2 className="size-4" />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <PlaceOrderModal
        token={orderToken}
        symbol={orderSymbol}
        exchange={orderExchange}
        quote={orderToken ? quoteByToken.get(orderToken) : undefined}
        onClose={() => {
          setOrderToken(null);
          setOrderSymbol("");
          setOrderExchange("");
        }}
      />
    </div>
  );
}

// ── Place Order modal ─────────────────────────────────────────────
// Bottom-sheet on mobile, centred card on desktop. Wires up:
//   • Multi-user search + checkbox select (admin's pool)
//   • Order type toggle (Market / Manual=Limit)
//   • Product toggle (Intraday MIS / Carry NRML)
//   • Lots + (for LIMIT) price
//   • BUY (green) / SELL (red) submit → /admin/marketwatch/place-orders
//
// Reports per-user successes/failures from the bulk response. Closes on
// the first all-success batch; stays open if any user failed so the
// operator can see why.

function PlaceOrderModal({
  token,
  symbol,
  exchange,
  quote,
  onClose,
}: {
  token: string | null;
  symbol: string;
  exchange: string;
  quote: any | undefined;
  onClose: () => void;
}) {
  const [orderType, setOrderType] = useState<"MARKET" | "MANUAL">("MARKET");
  const [productType, setProductType] = useState<"MIS" | "NRML">("MIS");
  const [lots, setLots] = useState("1");
  const [price, setPrice] = useState("");
  // Historical entry — blank means "now", which is the normal case.
  const [historicalAt, setHistoricalAt] = useState("");
  const [historyReason, setHistoryReason] = useState("");
  const [selectedUsers, setSelectedUsers] = useState<Map<string, any>>(new Map());
  const [userSearch, setUserSearch] = useState("");
  const [debouncedUserSearch, setDebouncedUserSearch] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // Reset state every time the modal opens for a new instrument.
  useEffect(() => {
    if (token) {
      setOrderType("MARKET");
      setProductType("MIS");
      setLots("1");
      setPrice("");
      setHistoricalAt("");
      setHistoryReason("");
      setSelectedUsers(new Map());
      setUserSearch("");
      setDebouncedUserSearch("");
    }
  }, [token]);

  useEffect(() => {
    const t = setTimeout(() => setDebouncedUserSearch(userSearch.trim()), 200);
    return () => clearTimeout(t);
  }, [userSearch]);

  // Auto-fill price with LTP when switching to Manual — saves the
  // operator a copy-paste in the common case (they then tweak it).
  useEffect(() => {
    if (orderType === "MANUAL" && !price && quote?.ltp != null && Number(quote.ltp) > 0) {
      setPrice(String(Number(quote.ltp).toFixed(2)));
    }
  }, [orderType, quote?.ltp, price]);

  const { data: userResp } = useQuery({
    queryKey: ["admin-marketwatch", "users", debouncedUserSearch],
    queryFn: () =>
      UsersAPI.list({
        q: debouncedUserSearch || undefined,
        status: "ACTIVE",
        page: 1,
        page_size: 30,
      }),
    enabled: !!token,
    staleTime: 10_000,
  });

  const users = userResp?.items ?? [];

  function toggleUser(u: any) {
    setSelectedUsers((prev) => {
      const next = new Map(prev);
      if (next.has(u.id)) next.delete(u.id);
      else next.set(u.id, u);
      return next;
    });
  }

  async function submitOrder(action: "BUY" | "SELL") {
    if (!token) return;
    if (selectedUsers.size === 0) {
      toast.error("Select at least one user");
      return;
    }
    const lotsNum = Number(lots);
    if (!Number.isFinite(lotsNum) || lotsNum <= 0) {
      toast.error("Lots must be greater than 0");
      return;
    }
    const priceNum = orderType === "MANUAL" ? Number(price) : undefined;
    if (orderType === "MANUAL" && (!Number.isFinite(priceNum!) || (priceNum as number) <= 0)) {
      toast.error("Manual order needs a positive price");
      return;
    }

    if (historicalAt && !historyReason.trim()) {
      toast.error("A reason is required to book a past-dated trade");
      return;
    }

    setSubmitting(true);
    try {
      const res = await AdminMarketwatchAPI.placeOrders({
        token,
        user_ids: Array.from(selectedUsers.keys()),
        action,
        order_type: orderType,
        product_type: productType,
        lots: lotsNum,
        price: priceNum,
        executed_at: historicalAt ? new Date(historicalAt).toISOString() : undefined,
        reason: historicalAt ? historyReason.trim() : undefined,
      });
      const ok = res.placed.length;
      const fail = res.failed.length;
      if (fail === 0) {
        toast.success(`${action} placed for ${ok} user${ok === 1 ? "" : "s"}`);
        onClose();
      } else {
        toast.warning(
          `${ok} placed · ${fail} failed — see details below`,
          { duration: 4000 },
        );
        // Drop the placed ones from the picker so the operator can
        // see exactly which users failed and retry just those.
        setSelectedUsers((prev) => {
          const next = new Map(prev);
          for (const p of res.placed) next.delete(p.user_id);
          return next;
        });
        // Surface each failure as a separate toast — the screen-reader
        // reads them out and the operator can copy the message.
        for (const f of res.failed.slice(0, 5)) {
          toast.error(`${f.user_id.slice(-6)} · ${f.error}`, { duration: 6000 });
        }
      }
    } catch (e: any) {
      toast.error(e?.message || "Failed to place orders");
    } finally {
      setSubmitting(false);
    }
  }

  if (!token) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-background/80 backdrop-blur-sm sm:items-center"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex max-h-[92vh] w-full max-w-lg flex-col overflow-hidden rounded-t-2xl border border-border bg-card shadow-2xl sm:rounded-2xl"
      >
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-2">
            <div className="grid size-8 place-items-center rounded-md bg-primary/10 text-primary">
              <Activity className="size-4" />
            </div>
            <div>
              <h2 className="text-base font-semibold">Place Order</h2>
              <p className="text-[11px] text-muted-foreground">
                Execute buy or sell order for {symbol}
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="grid size-8 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
            aria-label="Close"
          >
            <X className="size-4" />
          </button>
        </div>

        {/* Body */}
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
          {/* Instrument card */}
          <div className="rounded-lg border border-border bg-background p-3">
            <div className="flex items-start justify-between">
              <div>
                <div className="text-sm font-semibold">{symbol}</div>
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  {exchange}
                </div>
              </div>
              <div className="text-right">
                <div className="text-lg font-bold tabular-nums">
                  {quote?.ltp != null && Number(quote.ltp) > 0
                    ? Number(quote.ltp).toFixed(2)
                    : "—"}
                </div>
                <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
                  LTP
                </div>
              </div>
            </div>
            <div className="mt-2 flex items-center justify-between text-[11px]">
              <span>
                Bid:{" "}
                <span className="text-red-500 tabular-nums">
                  {quote?.bid != null && Number(quote.bid) > 0
                    ? Number(quote.bid).toFixed(2)
                    : "—"}
                </span>
              </span>
              <span>
                Ask:{" "}
                <span className="text-emerald-500 tabular-nums">
                  {quote?.ask != null && Number(quote.ask) > 0
                    ? Number(quote.ask).toFixed(2)
                    : "—"}
                </span>
              </span>
            </div>
          </div>

          {/* Users multi-select */}
          <div className="mt-4">
            <div className="mb-1.5 flex items-center justify-between">
              <label className="flex items-center gap-1.5 text-xs font-semibold">
                <UsersIcon className="size-3.5" /> Users
              </label>
              <span className="text-[11px] text-muted-foreground">
                {selectedUsers.size} selected
              </span>
            </div>
            <Input
              value={userSearch}
              onChange={(e) => setUserSearch(e.target.value)}
              placeholder="Search user by code / name / mobile / email…"
              className="h-9 text-sm"
            />
            {selectedUsers.size > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {Array.from(selectedUsers.values()).map((u) => (
                  <span
                    key={u.id}
                    className="inline-flex items-center gap-1 rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-[10px] text-primary"
                  >
                    {u.user_code}
                    <button
                      type="button"
                      onClick={() => toggleUser(u)}
                      className="grid size-3.5 place-items-center rounded-full hover:bg-primary/20"
                      aria-label={`Remove ${u.user_code}`}
                    >
                      <X className="size-2.5" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div className="mt-2 max-h-44 overflow-y-auto rounded-md border border-border bg-background scrollbar-thin">
              {users.length === 0 && (
                <div className="px-3 py-4 text-center text-xs text-muted-foreground">
                  No users in your scope match.
                </div>
              )}
              {users.map((u: any) => {
                const checked = selectedUsers.has(u.id);
                return (
                  <label
                    key={u.id}
                    className={cn(
                      "flex cursor-pointer items-center gap-2 border-b border-border/60 px-3 py-2 text-sm last:border-b-0 hover:bg-accent/30",
                      checked && "bg-primary/5",
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleUser(u)}
                      className="size-4 rounded border-border accent-primary"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-xs font-medium">
                        <span className="font-mono">{u.user_code}</span> · {u.full_name || "—"}
                      </div>
                      <div className="truncate text-[10px] text-muted-foreground">
                        {u.mobile || "—"} · {u.email || "—"}
                      </div>
                    </div>
                  </label>
                );
              })}
            </div>
          </div>

          {/* Order type */}
          <div className="mt-4">
            <label className="mb-1.5 block text-xs font-semibold">Order Type</label>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => setOrderType("MARKET")}
                className={cn(
                  "h-10 rounded-md border text-sm font-medium transition-colors",
                  orderType === "MARKET"
                    ? "border-emerald-500 bg-emerald-500/15 text-emerald-500"
                    : "border-border text-muted-foreground hover:bg-accent",
                )}
              >
                Market
              </button>
              <button
                type="button"
                onClick={() => setOrderType("MANUAL")}
                className={cn(
                  "h-10 rounded-md border text-sm font-medium transition-colors",
                  orderType === "MANUAL"
                    ? "border-primary bg-primary/15 text-primary"
                    : "border-border text-muted-foreground hover:bg-accent",
                )}
              >
                Manual
              </button>
            </div>
          </div>

          {/* Product */}
          <div className="mt-3">
            <label className="mb-1.5 block text-xs font-semibold">Product</label>
            <div className="grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => setProductType("MIS")}
                className={cn(
                  "h-10 rounded-md border text-sm font-medium transition-colors",
                  productType === "MIS"
                    ? "border-brand-500 bg-brand-500/15 text-brand-400"
                    : "border-border text-muted-foreground hover:bg-accent",
                )}
              >
                Intraday (MIS)
              </button>
              <button
                type="button"
                onClick={() => setProductType("NRML")}
                className={cn(
                  "h-10 rounded-md border text-sm font-medium transition-colors",
                  productType === "NRML"
                    ? "border-brand-500 bg-brand-500/15 text-brand-400"
                    : "border-border text-muted-foreground hover:bg-accent",
                )}
              >
                Carry (NRML)
              </button>
            </div>
          </div>

          {/* Price + Lots */}
          <div className="mt-3 grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-xs font-semibold">
                Price {orderType === "MARKET" && <span className="text-muted-foreground">(market)</span>}
              </label>
              <Input
                value={price}
                onChange={(e) => setPrice(e.target.value)}
                disabled={orderType === "MARKET"}
                placeholder={
                  quote?.ltp != null && Number(quote.ltp) > 0
                    ? Number(quote.ltp).toFixed(2)
                    : "—"
                }
                className="h-9 text-sm tabular-nums"
                inputMode="decimal"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-semibold">Lots</label>
              <Input
                value={lots}
                onChange={(e) => setLots(e.target.value)}
                className="h-9 text-sm tabular-nums"
                inputMode="numeric"
              />
            </div>
          </div>

          {/* Past-dated entry. Leave empty for a normal trade booked now.
              The server accepts this from a super-admin only, and records the
              reason in the audit trail beside the order. */}
          <details className="mt-3 rounded-md border border-border/60 px-3 py-2">
            <summary className="cursor-pointer text-xs font-semibold text-muted-foreground">
              Book as a past trade
            </summary>
            <div className="mt-2 space-y-2">
              <div>
                <label className="mb-1.5 block text-xs font-semibold">
                  Trade date &amp; time
                </label>
                <Input
                  type="datetime-local"
                  value={historicalAt}
                  onChange={(e) => setHistoricalAt(e.target.value)}
                  className="h-9 text-sm"
                />
              </div>
              <div>
                <label className="mb-1.5 block text-xs font-semibold">Reason</label>
                <Input
                  value={historyReason}
                  onChange={(e) => setHistoryReason(e.target.value)}
                  placeholder="Why is this being booked late?"
                  className="h-9 text-sm"
                />
              </div>
              <p className="text-[11px] text-muted-foreground">
                Money moves now either way — only the timestamp is moved back,
                and the entry stays marked as placed by an admin.
              </p>
            </div>
          </details>
        </div>

        {/* Footer — BUY / SELL */}
        <div className="shrink-0 border-t border-border bg-card p-3">
          <div className="grid grid-cols-2 gap-2">
            <button
              type="button"
              onClick={() => submitOrder("BUY")}
              disabled={submitting || selectedUsers.size === 0}
              className="flex h-11 items-center justify-center gap-1.5 rounded-md bg-emerald-600 text-sm font-semibold text-white shadow transition-colors hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <ArrowUpRight className="size-4" /> BUY
            </button>
            <button
              type="button"
              onClick={() => submitOrder("SELL")}
              disabled={submitting || selectedUsers.size === 0}
              className="flex h-11 items-center justify-center gap-1.5 rounded-md bg-red-600 text-sm font-semibold text-white shadow transition-colors hover:bg-red-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <ArrowDownRight className="size-4" /> SELL
            </button>
          </div>
          {selectedUsers.size === 0 && (
            <p className="mt-1.5 text-center text-[11px] text-muted-foreground">
              Select at least one user to enable BUY / SELL.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
