"use client";

import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Activity, ShieldAlert, ShieldCheck } from "lucide-react";
import { ZerodhaAPI } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

// Kite exchange codes the operator routes. NIFTY opts = NFO, SENSEX opts = BFO,
// all MCX futures/options = MCX. (Map by Kite exchange code, not SegmentType.)
const EXCHANGES: { code: string; label: string }[] = [
  { code: "NSE", label: "NSE (equity)" },
  { code: "BSE", label: "BSE (equity)" },
  { code: "NFO", label: "NFO (NSE F&O)" },
  { code: "BFO", label: "BFO (SENSEX F&O)" },
  { code: "CDS", label: "CDS (currency)" },
  { code: "MCX", label: "MCX (commodities)" },
];

type AcctHealth = {
  account_index: number;
  label: string;
  configured: boolean;
  connected: boolean;
  healthy: boolean;
  last_tick_age_sec: number | null;
};

function HealthPill({ acct }: { acct?: AcctHealth }) {
  const healthy = !!acct?.healthy;
  const configured = !!acct?.configured;
  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-lg border px-3 py-2.5 text-sm",
        !configured
          ? "border-border bg-muted/40 text-muted-foreground"
          : healthy
            ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-500"
            : "border-red-500/40 bg-red-500/10 text-red-500",
      )}
    >
      {healthy ? (
        <ShieldCheck className="size-4 shrink-0" />
      ) : (
        <ShieldAlert className="size-4 shrink-0" />
      )}
      <span className="font-semibold">Account {acct?.label ?? "?"}</span>
      <span className="ml-auto text-xs opacity-90">
        {!configured
          ? "not configured"
          : healthy
            ? `HEALTHY${acct?.last_tick_age_sec != null ? ` · ${acct.last_tick_age_sec}s` : ""}`
            : acct?.connected
              ? "UNHEALTHY"
              : "DISCONNECTED"}
      </span>
    </div>
  );
}

export function FeedRoutingCard() {
  const qc = useQueryClient();
  const { data } = useQuery<any>({
    queryKey: ["zerodha-routing"],
    queryFn: () => ZerodhaAPI.routing(),
    refetchInterval: 3000, // live health/route pulse
  });

  const cfg = data?.config;
  const live = data?.live;

  const [map, setMap] = useState<Record<string, number>>({});
  const [failoverEnabled, setFailoverEnabled] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (cfg?.exchange_account_map && Object.keys(map).length === 0) {
      setMap({ ...cfg.exchange_account_map });
      setFailoverEnabled(!!cfg.failover_enabled);
    }
  }, [cfg, map]);

  const save = async () => {
    setSaving(true);
    try {
      await ZerodhaAPI.updateRouting({ exchange_account_map: map, failover_enabled: failoverEnabled });
      toast.success("Feed routing saved");
      qc.invalidateQueries({ queryKey: ["zerodha-routing"] });
    } catch (e: any) {
      toast.error(e?.message || "Save failed (super-admin only)");
    } finally {
      setSaving(false);
    }
  };

  const testDisconnect = async (account: number) => {
    try {
      await ZerodhaAPI.failoverTestDisconnect(account);
      toast.success(`Account ${account === 0 ? "A" : "B"} WS dropped — watch failover`);
      qc.invalidateQueries({ queryKey: ["zerodha-routing"] });
    } catch (e: any) {
      toast.error(e?.message || "Test disconnect failed");
    }
  };

  const activeFailovers: Record<string, number> = live?.active_failovers || {};
  const effective: Record<string, number> = live?.effective_route || {};
  const hasFailover = Object.keys(activeFailovers).length > 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Activity className="size-5 text-emerald-500" />
          Feed Routing &amp; Failover
        </CardTitle>
        <CardDescription>
          Normal: Account A = NSE/BSE (+ F&amp;O), Account B = MCX. If one account fails, the other
          streams everything automatically — <b className="text-foreground">data never stops</b>.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {/* Live health */}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <HealthPill acct={live?.accounts?.A} />
          <HealthPill acct={live?.accounts?.B} />
        </div>

        {/* Active failover banner */}
        {hasFailover && (
          <div className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm text-amber-500">
            <ShieldAlert className="mt-0.5 size-4 shrink-0" />
            <span>
              Failover ACTIVE:{" "}
              {Object.entries(activeFailovers)
                .map(([ex, acct]) => `${ex} → Account ${acct === 0 ? "A" : "B"}`)
                .join(", ")}
            </span>
          </div>
        )}

        {/* Per-exchange routing */}
        <div className="space-y-2">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Exchange → desired account
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {EXCHANGES.map(({ code, label }) => {
              const desired = map[code] ?? 0;
              const eff = effective[code];
              const failedOver = eff != null && eff !== desired;
              return (
                <div
                  key={code}
                  className="flex items-center justify-between gap-3 rounded-lg border border-border bg-muted/40 px-3 py-2.5"
                >
                  <div className="flex min-w-0 flex-col">
                    <span className="truncate text-sm text-foreground">{label}</span>
                    {failedOver && (
                      <span className="text-xs text-amber-500">
                        now on {eff === 0 ? "A" : "B"} (failover)
                      </span>
                    )}
                  </div>
                  <select
                    value={desired}
                    onChange={(e) => setMap((m) => ({ ...m, [code]: Number(e.target.value) }))}
                    className="shrink-0 rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground outline-none focus:ring-2 focus:ring-emerald-500/40"
                  >
                    <option value={0}>Account A</option>
                    <option value={1}>Account B</option>
                  </select>
                </div>
              );
            })}
          </div>
        </div>

        {/* Failover toggle */}
        <label className="flex flex-wrap items-center gap-2 text-sm text-foreground">
          <input
            type="checkbox"
            checked={failoverEnabled}
            onChange={(e) => setFailoverEnabled(e.target.checked)}
            className="size-4 accent-emerald-500"
          />
          Automatic failover enabled
          <span className="text-muted-foreground">(off = kill-switch: Account A carries everything)</span>
        </label>

        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <Button onClick={save} disabled={saving} className="w-full sm:w-auto">
            {saving ? "Saving…" : "Save routing"}
          </Button>
          {/* Off-market test helpers */}
          <div className="flex items-center gap-2 sm:ml-auto">
            <span className="text-xs text-muted-foreground">Off-market test:</span>
            <Button variant="outline" size="sm" onClick={() => testDisconnect(0)}>
              Drop A
            </Button>
            <Button variant="outline" size="sm" onClick={() => testDisconnect(1)}>
              Drop B
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
