"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Copy, Bitcoin } from "lucide-react";
import { CryptoConfigAPI, type CryptoConfig } from "@/lib/api";
import { PageHeader } from "@/components/common/PageHeader";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// Admin self-service crypto deposit setup. Manual (address -> QR + approve) and
// / or oxapay gateway (auto-credit). Users only see Crypto once this is enabled.
export default function CryptoPaymentsPage() {
  const qc = useQueryClient();
  // refetchOnWindowFocus OFF — the operator flips tabs (oxapay, mail); without
  // this a refetch on return re-seeds the form and silently reverts an unsaved
  // toggle, so it looked like "enable won't turn on".
  const { data } = useQuery({
    queryKey: ["crypto-config"],
    queryFn: () => CryptoConfigAPI.get(),
    refetchOnWindowFocus: false,
  });

  const [enabled, setEnabled] = useState(false);
  const [mode, setMode] = useState<"manual" | "gateway" | "both">("manual");
  const [address, setAddress] = useState("");
  const [network, setNetwork] = useState("");
  const [asset, setAsset] = useState("");
  const [oxapayKey, setOxapayKey] = useState("");

  useEffect(() => {
    if (!data) return;
    setEnabled(data.enabled);
    setMode(data.mode);
    setAddress(data.wallet_address ?? "");
    setNetwork(data.network ?? "");
    setAsset(data.asset ?? "");
  }, [data]);

  const save = useMutation({
    mutationFn: (body: Parameters<typeof CryptoConfigAPI.update>[0]) => CryptoConfigAPI.update(body),
    onSuccess: (fresh: CryptoConfig) => {
      qc.setQueryData(["crypto-config"], fresh);
      setOxapayKey("");
      toast.success("Crypto settings saved");
    },
    onError: (e: any) => toast.error(e?.message || "Save failed"),
  });

  const showManual = mode === "manual" || mode === "both";
  const showGateway = mode === "gateway" || mode === "both";

  return (
    <div className="space-y-4">
      <PageHeader
        title="Crypto Payments"
        description="Let your users deposit in crypto. Off by default — users see the Crypto option only after you set it up."
      />

      <Card className="max-w-2xl space-y-5 p-5">
        {/* Enable */}
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-sm font-semibold">Enable crypto deposits</div>
            <div className="text-xs text-muted-foreground">When off, your users only see the INR flow.</div>
          </div>
          <button
            type="button"
            onClick={() => setEnabled((v) => !v)}
            className={cn(
              "relative h-6 w-11 shrink-0 rounded-full transition-colors",
              enabled ? "bg-emerald-500" : "bg-muted-foreground/30",
            )}
          >
            <span className={cn("absolute top-0.5 size-5 rounded-full bg-white transition-all", enabled ? "left-[22px]" : "left-0.5")} />
          </button>
        </div>

        {/* Mode */}
        <div className="space-y-1.5">
          <Label>Method</Label>
          <div className="flex flex-wrap gap-2">
            {(["manual", "gateway", "both"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={cn(
                  "rounded-lg border px-3 py-1.5 text-xs font-medium capitalize transition-colors",
                  mode === m ? "border-primary bg-primary/10 text-primary" : "border-border text-muted-foreground hover:bg-muted/50",
                )}
              >
                {m === "manual" ? "Manual (address + QR)" : m === "gateway" ? "oxapay gateway" : "Both"}
              </button>
            ))}
          </div>
        </div>

        {showManual && (
          <div className="space-y-3 rounded-lg border border-border/70 bg-muted/20 p-3">
            <div className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Manual — your wallet</div>
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="space-y-1 sm:col-span-3">
                <Label htmlFor="addr">Wallet address</Label>
                <Input id="addr" value={address} onChange={(e) => setAddress(e.target.value)} placeholder="e.g. TXyz… / 0x…" maxLength={200} />
              </div>
              <div className="space-y-1">
                <Label htmlFor="net">Network</Label>
                <Input id="net" value={network} onChange={(e) => setNetwork(e.target.value)} placeholder="TRC20 / ERC20 / BEP20" maxLength={40} />
              </div>
              <div className="space-y-1">
                <Label htmlFor="ast">Asset</Label>
                <Input id="ast" value={asset} onChange={(e) => setAsset(e.target.value)} placeholder="USDT / BTC" maxLength={20} />
              </div>
            </div>
            <p className="text-[11px] text-muted-foreground">A QR is shown to the user automatically from this address. The user pays and submits the tx hash; you approve it in Payments (like UPI).</p>
          </div>
        )}

        {showGateway && (
          <div className="space-y-3 rounded-lg border border-border/70 bg-muted/20 p-3">
            <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              <Bitcoin className="size-3.5" /> oxapay gateway {data?.has_gateway_key && <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] normal-case text-emerald-600">key set</span>}
            </div>
            <div className="space-y-1">
              <Label htmlFor="key">oxapay Merchant API key</Label>
              <Input id="key" value={oxapayKey} onChange={(e) => setOxapayKey(e.target.value)} placeholder={data?.has_gateway_key ? "•••••• (leave blank to keep)" : "paste your oxapay.com merchant key"} maxLength={400} />
              <p className="text-[11px] text-muted-foreground">Stored encrypted, never shown again. Auto-confirms payments — no manual approval.</p>
            </div>
            <div className="space-y-1">
              <Label>Webhook / callback URL (paste this in your oxapay dashboard)</Label>
              <div className="flex items-center gap-2">
                <Input readOnly value={data?.webhook_url ?? ""} className="font-mono text-[11px]" />
                <Button size="sm" variant="outline" type="button" onClick={() => { navigator.clipboard.writeText(data?.webhook_url ?? ""); toast.success("Copied"); }}>
                  <Copy className="size-3.5" />
                </Button>
              </div>
            </div>
          </div>
        )}

        <Button
          onClick={() => save.mutate({ enabled, mode, wallet_address: address.trim(), network: network.trim(), asset: asset.trim(), gateway: showGateway ? "oxapay" : "none", ...(oxapayKey.trim() ? { oxapay_api_key: oxapayKey.trim() } : {}) })}
          loading={save.isPending}
        >
          Save crypto settings
        </Button>
      </Card>
    </div>
  );
}
