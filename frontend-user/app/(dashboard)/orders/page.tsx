"use client";

import { useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Pencil, X } from "lucide-react";
import { OrderAPI } from "@/lib/api";
import { cn, formatIST, formatPrice } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

// Orders book — Open / Executed / Rejected tabs. Separate from the unified
// /positions blotter (Position / Active / Closed / Cancelled / Rejected),
// which is left untouched. Pure read view over OrderAPI.list() + a cancel
// action on still-working orders. No trading/data logic is duplicated.

type Tab = "open" | "executed" | "rejected";

// Working orders that haven't filled / been killed yet.
const OPEN_STATUSES = ["PENDING", "OPEN", "TRIGGERED", "PARTIAL"];

function num(v: any): number {
  const n = typeof v === "string" ? Number(v) : (v ?? 0);
  return Number.isFinite(n) ? n : 0;
}

// Clean qty for display — SAME logic as the Positions/Closed tabs' `fmtQty`
// so all three sections show an identical, tidy number (no "65.00999999"
// float garbage from lots×lot_size roundtrips). Rounds to 3dp, snaps ≥1
// values within 0.02 of an integer back to the integer (whole-lot futures);
// leaves genuine sub-1 crypto/forex sizes (0.001, 0.5) untouched.
function fmtQty(q: any): string {
  const n = Math.abs(Number(q) || 0);
  if (n === 0) return "0";
  const r = Math.round(n * 1000) / 1000;
  const nearest = Math.round(r);
  if (n >= 1 && Math.abs(r - nearest) < 0.02) return String(nearest);
  return String(r);
}

function prettyType(t: string): string {
  const u = (t || "").toUpperCase();
  if (u === "SL_M") return "SL-M";
  if (u === "MARKET") return "MKT";
  if (u === "LIMIT") return "LMT";
  return u;
}

export default function OrdersPage() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("open");
  const [editOrder, setEditOrder] = useState<any | null>(null);

  const { data: orders = [], isLoading } = useQuery<any[]>({
    queryKey: ["orders", "book"],
    queryFn: () => OrderAPI.list() as Promise<any[]>,
    // Light poll so a fill / reject lands without a manual refresh. The
    // backend list is cheap + cached server-side.
    refetchInterval: 4000,
    staleTime: 2000,
    placeholderData: (prev) => prev,
  });

  const buckets = useMemo(() => {
    const open: any[] = [];
    const executed: any[] = [];
    const rejected: any[] = [];
    for (const o of orders) {
      const s = String(o?.status ?? "").toUpperCase();
      if (s === "EXECUTED") executed.push(o);
      else if (s === "REJECTED") rejected.push(o);
      else if (OPEN_STATUSES.includes(s)) open.push(o);
    }
    return { open, executed, rejected };
  }, [orders]);

  const rows = buckets[tab];

  async function cancelOrder(id: string) {
    try {
      await OrderAPI.cancel(id);
      toast.success("Order cancelled");
      qc.invalidateQueries({ queryKey: ["orders"] });
    } catch (e: any) {
      toast.error(e?.message || "Failed to cancel order");
    }
  }

  const tabs: { key: Tab; label: string; count: number }[] = [
    { key: "open", label: "Open", count: buckets.open.length },
    { key: "executed", label: "Executed", count: buckets.executed.length },
    { key: "rejected", label: "Rejected", count: buckets.rejected.length },
  ];

  return (
    // Full-bleed on mobile (matches the markets page): negative margins cancel
    // the dashboard layout's p-4 / pb-24, sized to fill between the sticky
    // TopBar (h-14) and fixed BottomNav (h-14). Desktop keeps the padded panel.
    <div className="-mx-4 -mt-4 -mb-24 flex h-[calc(100dvh-7rem)] flex-col md:mx-0 md:mt-0 md:mb-0 md:h-[calc(100vh-7rem)] md:min-h-[480px] md:overflow-hidden md:rounded-lg md:border md:border-border">
      {/* Tabs */}
      <div className="flex shrink-0 border-b border-border bg-background md:bg-card">
        {tabs.map((t) => {
          const active = tab === t.key;
          return (
            <button
              key={t.key}
              type="button"
              onClick={() => setTab(t.key)}
              className="relative flex-1 py-3 text-center"
            >
              <span
                className={cn(
                  "text-sm font-semibold transition-colors",
                  active ? "text-foreground" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {t.label}
                {t.count > 0 && (
                  <span className="ml-1 text-xs font-normal text-muted-foreground">
                    {t.count}
                  </span>
                )}
              </span>
              {active && (
                <span className="absolute bottom-0 left-1/2 h-0.5 w-12 -translate-x-1/2 rounded-full bg-primary" />
              )}
            </button>
          );
        })}
      </div>

      {/* List */}
      <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin">
        {isLoading && rows.length === 0 ? (
          <EmptyState text="Loading…" />
        ) : rows.length === 0 ? (
          <EmptyState text={`No ${tab} orders`} />
        ) : (
          rows.map((o) => (
            <OrderRow
              key={o.id}
              order={o}
              canCancel={tab === "open"}
              onCancel={() => cancelOrder(o.id)}
              onEdit={() => setEditOrder(o)}
            />
          ))
        )}
      </div>

      <OrderEditDialog
        order={editOrder}
        onClose={() => setEditOrder(null)}
        onSaved={() => qc.invalidateQueries({ queryKey: ["orders"] })}
      />
    </div>
  );
}

function OrderRow({
  order,
  canCancel,
  onCancel,
  onEdit,
}: {
  order: any;
  canCancel: boolean;
  onCancel: () => void;
  onEdit: () => void;
}) {
  const buy = String(order.action ?? "").toUpperCase() === "BUY";
  const type = String(order.order_type ?? "").toUpperCase();
  const status = String(order.status ?? "").toUpperCase();
  // Only working LIMIT / SL / SL-M orders carry an editable price or qty; a
  // MARKET order fills instantly so there's nothing to modify.
  const canEdit = canCancel && (type === "LIMIT" || type === "SL" || type === "SL_M");

  // Most relevant price per state: executed → fill (average), LIMIT → limit,
  // SL / SL-M → trigger, MARKET → "MKT".
  let priceText: string;
  let priceTag = "";
  if (status === "EXECUTED") {
    priceText = formatPrice(num(order.average_price), order.segment, order.exchange);
    priceTag = "Avg";
  } else if (type === "LIMIT") {
    priceText = formatPrice(num(order.price), order.segment, order.exchange);
    priceTag = "Limit";
  } else if (type === "SL" || type === "SL_M") {
    priceText = formatPrice(num(order.trigger_price), order.segment, order.exchange);
    priceTag = "Trigger";
  } else {
    priceText = "MKT";
  }

  const lots = num(order.lots);
  // Show QTY (not lots) — fractional lots like "1.167 lots" confused users on
  // MCX (SILVER/GOLD mini) where lot sizes aren't 1. Prefer the backend's
  // absolute quantity; fall back to lots × lot_size.
  const qty = Math.abs(num(order.quantity) || lots * (num(order.lot_size) || 1));
  const when = order.executed_at ?? order.created_at;

  return (
    <div className="flex items-center gap-3 border-b border-border/40 px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span
            className={cn(
              "rounded px-1.5 py-0.5 text-[10px] font-bold tracking-wide",
              buy ? "bg-buy/15 text-buy" : "bg-sell/15 text-sell",
            )}
          >
            {buy ? "BUY" : "SELL"}
          </span>
          <span className="truncate text-sm font-semibold">{order.symbol}</span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
          <span>
            Qty {fmtQty(qty)} · {prettyType(type)} · {order.product_type}
          </span>
          {when && <span>· {formatIST(when)}</span>}
        </div>
        {status === "REJECTED" && order.rejection_reason && (
          <div className="mt-1 line-clamp-2 text-[11px] text-sell">
            {order.rejection_reason}
          </div>
        )}
      </div>

      <div className="flex shrink-0 flex-col items-end gap-1">
        <div className="font-tabular tabular-nums text-sm font-bold leading-none">
          {priceText}
        </div>
        {priceTag ? (
          <div className="text-[9px] uppercase tracking-wider text-muted-foreground">
            {priceTag}
          </div>
        ) : (
          <StatusPill status={status} />
        )}
      </div>

      {canEdit ? (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onEdit();
          }}
          aria-label="Edit order"
          title="Edit qty / price"
          className="grid size-8 shrink-0 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-primary"
        >
          <Pencil className="size-4" />
        </button>
      ) : null}

      {canCancel ? (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onCancel();
          }}
          aria-label="Cancel order"
          title="Cancel order"
          className="grid size-8 shrink-0 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
        >
          <X className="size-4" />
        </button>
      ) : null}
    </div>
  );
}

// Edit a still-working LIMIT / SL / SL-M order's qty (lots), limit price and/or
// trigger. Only sends the fields the user changed to the existing
// `OrderAPI.modify` (PUT /user/orders/{id}) endpoint. Mirrors the validation
// the positions-blotter modify dialog uses so behaviour is identical.
function OrderEditDialog({
  order,
  onClose,
  onSaved,
}: {
  order: any | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [price, setPrice] = useState("");
  const [trigger, setTrigger] = useState("");
  const [lots, setLots] = useState("");
  const [synced, setSynced] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Reseed the inputs once per opened order (state-during-render reseed — the
  // same pattern the positions modify dialog uses).
  if (order && synced !== order.id) {
    setPrice(order.price != null && Number(order.price) > 0 ? String(Number(order.price)) : "");
    setTrigger(
      order.trigger_price != null && Number(order.trigger_price) > 0
        ? String(Number(order.trigger_price))
        : "",
    );
    setLots(order.lots != null ? String(Number(order.lots)) : "");
    setSynced(order.id);
  }
  if (!order && synced !== null) {
    setPrice("");
    setTrigger("");
    setLots("");
    setSynced(null);
  }

  const type = String(order?.order_type ?? "LIMIT").toUpperCase();
  const isLimit = type === "LIMIT";
  const isSlm = type === "SL_M";
  const isSl = type === "SL";
  const lotSize = Number(order?.lot_size ?? order?.instrument?.lot_size ?? 1) || 1;

  async function save() {
    if (!order) return;
    const priceNum = price ? Number(price) : NaN;
    const triggerNum = trigger ? Number(trigger) : NaN;
    const lotsNum = lots ? Number(lots) : NaN;

    if (isLimit && (!Number.isFinite(priceNum) || priceNum <= 0)) {
      toast.error("Enter a valid limit price");
      return;
    }
    if ((isSl || isSlm) && (!Number.isFinite(triggerNum) || triggerNum <= 0)) {
      toast.error("Enter a valid trigger price");
      return;
    }
    if (isSl && (!Number.isFinite(priceNum) || priceNum <= 0)) {
      toast.error("Enter a valid limit price");
      return;
    }
    if (Number.isFinite(lotsNum) && lotsNum > 0) {
      const filled = Number(order.filled_quantity ?? 0);
      if (filled > 0 && lotsNum < filled / lotSize) {
        toast.error(
          `Already filled ${filled} qty — new lots must be ≥ ${(filled / lotSize).toFixed(2)}.`,
        );
        return;
      }
    }

    const body: Record<string, number> = {};
    if (isLimit && Number.isFinite(priceNum)) body.price = priceNum;
    if ((isSl || isSlm) && Number.isFinite(triggerNum)) body.trigger_price = triggerNum;
    if (isSl && Number.isFinite(priceNum)) body.price = priceNum;
    if (Number.isFinite(lotsNum) && lotsNum > 0) body.lots = lotsNum;
    if (Object.keys(body).length === 0) {
      toast.error("Nothing to update");
      return;
    }

    setSaving(true);
    try {
      await OrderAPI.modify(order.id, body);
      toast.success("Order updated");
      onSaved();
      onClose();
    } catch (e: any) {
      toast.error(e?.message || "Failed to update order");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={!!order} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Edit order{order ? ` · ${order.symbol}` : ""}</DialogTitle>
        </DialogHeader>
        {order && (
          <div className="space-y-3">
            <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
              {prettyType(type)} · {String(order.action ?? "").toUpperCase()} · {order.product_type}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="edit-lots">Quantity (lots)</Label>
              <Input
                id="edit-lots"
                inputMode="decimal"
                value={lots}
                onChange={(e) => setLots(e.target.value)}
                placeholder="Lots"
              />
              {lotSize > 1 && (
                <p className="text-[10px] text-muted-foreground">
                  1 lot = {lotSize} qty
                  {lots && Number(lots) > 0 ? ` · ${(Number(lots) * lotSize).toFixed(0)} qty` : ""}
                </p>
              )}
            </div>
            {(isLimit || isSl) && (
              <div className="space-y-1.5">
                <Label htmlFor="edit-price">Limit price</Label>
                <Input
                  id="edit-price"
                  inputMode="decimal"
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                  placeholder="Limit price"
                />
              </div>
            )}
            {(isSl || isSlm) && (
              <div className="space-y-1.5">
                <Label htmlFor="edit-trigger">Trigger price</Label>
                <Input
                  id="edit-trigger"
                  inputMode="decimal"
                  value={trigger}
                  onChange={(e) => setTrigger(e.target.value)}
                  placeholder="Trigger price"
                />
              </div>
            )}
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button onClick={save} loading={saving}>
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function StatusPill({ status }: { status: string }) {
  const s = status.toUpperCase();
  const cls =
    s === "EXECUTED"
      ? "bg-buy/15 text-buy"
      : s === "REJECTED"
        ? "bg-sell/15 text-sell"
        : s === "CANCELLED" || s === "EXPIRED"
          ? "bg-muted text-muted-foreground"
          : "bg-atm/15 text-atm";
  const label =
    s === "PARTIAL" ? "PARTIAL" : s === "TRIGGERED" ? "TRIGGERED" : s;
  return (
    <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-bold tracking-wide", cls)}>
      {label}
    </span>
  );
}

function EmptyState({ text }: { text: string }) {
  return (
    <div className="grid h-40 place-items-center px-6 text-center text-sm text-muted-foreground">
      {text}
    </div>
  );
}
