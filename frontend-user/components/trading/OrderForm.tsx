"use client";

import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { useQueryClient } from "@tanstack/react-query";
import { OrderAPI, WalletAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { isInstrumentMarketOpen, marketLabel } from "@/lib/marketHours";
import { cn, formatINR } from "@/lib/utils";

interface Props {
  instrument: any;
  ltp: number;
}

const ORDER_TYPES = ["MARKET", "LIMIT", "SL", "SL_M"] as const;
const PRODUCTS = ["MIS", "CNC", "NRML"] as const;

export function OrderForm({ instrument, ltp }: Props) {
  const qc = useQueryClient();
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [orderType, setOrderType] = useState<(typeof ORDER_TYPES)[number]>("MARKET");
  const [productType, setProductType] = useState<(typeof PRODUCTS)[number]>("MIS");
  const [lots, setLots] = useState<number>(1);
  const [price, setPrice] = useState<string>("");
  const [trigger, setTrigger] = useState<string>("");
  const [validity, setValidity] = useState<"DAY" | "IOC">("DAY");
  const [isAmo, setIsAmo] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [available, setAvailable] = useState<number>(0);

  useEffect(() => {
    WalletAPI.summary().then((s: any) => setAvailable(Number(s.available_balance))).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (orderType !== "MARKET" && !price) setPrice(ltp ? ltp.toFixed(2) : "");
  }, [orderType, ltp, price]);

  const lotSize = instrument?.lot_size ?? 1;
  const qty = lots * lotSize;
  const refPrice = orderType === "MARKET" ? ltp : Number(price || ltp);
  const notional = qty * refPrice;

  // Rough margin estimate: equity ≈ notional, F&O ≈ 12-15%
  const isFno = (instrument?.segment ?? "").includes("FUTURE") || (instrument?.segment ?? "").includes("OPTION");
  const isCrypto = (instrument?.segment ?? "").includes("CRYPTO");
  const marginPct = isFno ? 0.13 : isCrypto ? 0.2 : 1.0;
  const leverage = productType === "MIS" ? 5 : 1;
  const marginRequired = useMemo(() => (notional * marginPct) / leverage, [notional, marginPct, leverage]);

  // Brokerage is the only charge on this platform — naive client-side
  // preview; the server computes the authoritative figure from the admin's
  // segment settings / brokerage plan.
  const charges = useMemo(() => {
    if (!notional) return 0;
    return isFno ? 20 : Math.min(20, notional * 0.0003);
  }, [notional, isFno]);

  async function submit() {
    if (!instrument) return;
    if (!lots || lots < 1) {
      toast.error("Lots must be at least 1");
      return;
    }
    // Market-closed guard — same pattern as OrderPanel / TradeDetailSheet
    // / MobileQuickTradeBar. Without it a click outside trading hours
    // pops the green success toast for ~1 s before the backend rejection
    // replaces it. AMO orders bypass the guard since they're explicitly
    // queued for the next session.
    if (
      !isAmo &&
      !isInstrumentMarketOpen(
        instrument.segment as string | undefined,
        instrument.exchange as string | undefined,
      )
    ) {
      const label = marketLabel(
        instrument.segment as string | undefined,
        instrument.exchange as string | undefined,
      );
      toast.error(`${label} market is closed. Try placing an AMO instead.`, {
        duration: 5000,
      });
      return;
    }
    setSubmitting(true);
    // Pop the success toast in the same frame as the click — the API
    // round-trip would otherwise delay it by ~500-2000 ms. Dismissed
    // on rejection.
    const pendingToastId = toast.success(`${side} placed`);
    try {
      await OrderAPI.place({
        token: instrument.token,
        action: side,
        order_type: orderType,
        product_type: productType,
        lots,
        price: orderType === "MARKET" ? 0 : Number(price || 0),
        trigger_price: orderType.startsWith("SL") ? Number(trigger || 0) : 0,
        validity,
        is_amo: isAmo,
      });
      qc.invalidateQueries({ queryKey: ["orders"] });
      qc.invalidateQueries({ queryKey: ["positions"] });
      qc.invalidateQueries({ queryKey: ["wallet"] });
    } catch (e: any) {
      toast.dismiss(pendingToastId);
      toast.error(e.message || "Order rejected");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex h-full flex-col rounded-lg border border-border bg-card">
      {/* Side tabs */}
      <div className="grid grid-cols-2 border-b border-border">
        {(["BUY", "SELL"] as const).map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => setSide(s)}
            className={cn(
              "py-2.5 text-sm font-semibold transition-colors",
              side === s
                ? s === "BUY"
                  ? "bg-buy text-buy-foreground"
                  : "bg-sell text-sell-foreground"
                : "bg-card text-muted-foreground hover:bg-muted/30"
            )}
          >
            {s}
          </button>
        ))}
      </div>

      <div className="flex-1 space-y-3 p-3 text-sm">
        {/* Product */}
        <div>
          <Label className="mb-1 block text-[11px] uppercase tracking-wider text-muted-foreground">Product</Label>
          <div className="grid grid-cols-3 gap-1 rounded-md bg-muted/40 p-1">
            {PRODUCTS.map((p) => (
              <button
                key={p}
                type="button"
                onClick={() => setProductType(p)}
                className={cn(
                  "rounded py-1 text-xs font-medium",
                  productType === p ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground"
                )}
              >
                {p}
              </button>
            ))}
          </div>
        </div>

        {/* Order type */}
        <div>
          <Label className="mb-1 block text-[11px] uppercase tracking-wider text-muted-foreground">Order type</Label>
          <div className="grid grid-cols-4 gap-1 rounded-md bg-muted/40 p-1">
            {ORDER_TYPES.map((o) => (
              <button
                key={o}
                type="button"
                onClick={() => setOrderType(o)}
                className={cn(
                  "rounded py-1 text-xs font-medium",
                  orderType === o ? "bg-primary/15 text-primary" : "text-muted-foreground hover:text-foreground"
                )}
              >
                {o.replace("_", "-")}
              </button>
            ))}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2">
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wider text-muted-foreground">Lots</Label>
            <Input
              type="number"
              min={1}
              value={lots}
              onChange={(e) => setLots(Math.max(1, Number(e.target.value || 1)))}
              className="h-9"
            />
          </div>
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wider text-muted-foreground">Qty</Label>
            <Input value={qty} readOnly className="h-9 bg-muted/30" />
          </div>
        </div>

        {orderType !== "MARKET" && (
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wider text-muted-foreground">Price (₹)</Label>
            <Input
              type="number"
              step="0.05"
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              className="h-9"
            />
          </div>
        )}
        {(orderType === "SL" || orderType === "SL_M") && (
          <div className="space-y-1">
            <Label className="text-[11px] uppercase tracking-wider text-muted-foreground">Trigger price (₹)</Label>
            <Input
              type="number"
              step="0.05"
              value={trigger}
              onChange={(e) => setTrigger(e.target.value)}
              className="h-9"
            />
          </div>
        )}

        <div className="grid grid-cols-2 gap-2 text-xs">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="validity"
              checked={validity === "DAY"}
              onChange={() => setValidity("DAY")}
              className="size-3 accent-primary"
            />
            DAY
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              name="validity"
              checked={validity === "IOC"}
              onChange={() => setValidity("IOC")}
              className="size-3 accent-primary"
            />
            IOC
          </label>
          <label className="col-span-2 flex items-center gap-2">
            <input
              type="checkbox"
              checked={isAmo}
              onChange={(e) => setIsAmo(e.target.checked)}
              className="size-3 accent-primary"
            />
            After-Market Order (AMO)
          </label>
        </div>

        {/* Charges + margin */}
        <div className="space-y-1 rounded-md border border-border bg-muted/20 p-2 text-xs">
          <Row label="LTP" value={ltp ? ltp.toFixed(2) : "—"} />
          <Row label="Notional" value={formatINR(notional)} />
          <Row label="Margin required" value={formatINR(marginRequired)} />
          <Row label="Charges (est.)" value={formatINR(charges)} />
          <Row label="Available balance" value={formatINR(available)} />
        </div>
      </div>

      <div className="border-t border-border p-3">
        <Button
          type="button"
          variant={side === "BUY" ? "buy" : "sell"}
          className="w-full"
          loading={submitting}
          onClick={submit}
        >
          {side} {instrument?.symbol ?? ""}
        </Button>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-tabular">{value}</span>
    </div>
  );
}
