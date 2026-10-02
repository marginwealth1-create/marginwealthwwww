"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  DownloadCloud,
  KeyRound,
  Loader2,
  Plug,
  Search,
  Unlink,
} from "lucide-react";

import { MetaApiAPI, type MetaApiSettings, type MetaApiStatus } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";
import { StatusPill } from "@/components/common/StatusPill";
import { cn } from "@/lib/utils";

const SETTINGS_KEY = ["metaapi", "settings"] as const;
const STATUS_KEY = ["metaapi", "status"] as const;
const SYMBOLS_KEY = ["metaapi", "broker-symbols"] as const;

export default function MetaApiPage() {
  const qc = useQueryClient();

  const settingsQuery = useQuery<MetaApiSettings>({
    queryKey: SETTINGS_KEY,
    queryFn: MetaApiAPI.settings,
  });
  const statusQuery = useQuery<MetaApiStatus>({
    queryKey: STATUS_KEY,
    queryFn: MetaApiAPI.status,
    refetchInterval: 5_000,
  });

  const settings = settingsQuery.data;
  const status = statusQuery.data;

  const [token, setToken] = useState("");
  const [accountId, setAccountId] = useState("");
  const [region, setRegion] = useState("");
  const [maxSymbols, setMaxSymbols] = useState("");
  const [aliases, setAliases] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [filter, setFilter] = useState("");

  // Hydrate the form once the saved settings land (token is never returned).
  useEffect(() => {
    if (!settings) return;
    setAccountId(settings.account_id ?? "");
    setRegion(settings.region ?? "");
    setMaxSymbols(String(settings.max_symbols ?? 24));
    setSelected(settings.symbols ?? []);
    setAliases(
      Object.entries(settings.symbol_map ?? {})
        .map(([platform, broker]) => `${platform}:${broker}`)
        .join(", "),
    );
  }, [settings]);

  // Broker symbol list is fetched on demand — it opens a connection upstream.
  const symbolsQuery = useQuery<{ symbols: string[]; cached: boolean }>({
    queryKey: SYMBOLS_KEY,
    queryFn: () => MetaApiAPI.brokerSymbols(false),
    enabled: false,
  });

  const saveSettingsMut = useMutation({
    mutationFn: () =>
      MetaApiAPI.saveSettings({
        token: token.trim() || undefined,
        account_id: accountId.trim(),
        region: region.trim(),
        max_symbols: Number(maxSymbols) || undefined,
        symbol_map: parseAliases(aliases),
      }),
    onSuccess: (next) => {
      qc.setQueryData(SETTINGS_KEY, next);
      setToken("");
      toast.success("MetaAPI settings saved");
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Could not save settings"),
  });

  const connectMut = useMutation({
    mutationFn: MetaApiAPI.connect,
    onSuccess: (next) => {
      qc.setQueryData(STATUS_KEY, next);
      qc.invalidateQueries({ queryKey: SETTINGS_KEY });
      toast.success("Connecting — the feed server picks this up in a few seconds");
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Connect failed"),
  });

  const disconnectMut = useMutation({
    mutationFn: MetaApiAPI.disconnect,
    onSuccess: (next) => {
      qc.setQueryData(STATUS_KEY, next);
      qc.invalidateQueries({ queryKey: SETTINGS_KEY });
      toast.success("MetaAPI feed disconnected");
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Disconnect failed"),
  });

  const saveSymbolsMut = useMutation({
    mutationFn: () => MetaApiAPI.saveSymbols(selected),
    onSuccess: (res) => {
      qc.setQueryData(SETTINGS_KEY, res.settings);
      qc.setQueryData(STATUS_KEY, res.status);
      toast.success(`${res.settings.symbols.length} symbols saved`);
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Could not save symbols"),
  });

  const cap = settings?.max_symbols ?? 24;
  const brokerSymbols = symbolsQuery.data?.symbols ?? [];
  const visible = useMemo(() => {
    const q = filter.trim().toUpperCase();
    const list = brokerSymbols.length ? brokerSymbols : selected;
    return q ? list.filter((s) => s.includes(q)) : list;
  }, [brokerSymbols, selected, filter]);

  const connected = !!status?.connected;
  const pill = connected
    ? "CONNECTED"
    : status?.enabled
      ? "CONNECTING"
      : "DISCONNECTED";

  function toggle(sym: string) {
    setSelected((prev) =>
      prev.includes(sym) ? prev.filter((s) => s !== sym) : [...prev, sym],
    );
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="MetaAPI Connect"
        description="Streams forex, metals, indices and energy from your MT4/MT5 account. Symbols you don't pick keep using the Infoway feed."
        actions={
          <div className="flex gap-2">
            <Button
              variant="outline"
              disabled={disconnectMut.isPending || !status?.enabled}
              onClick={() => disconnectMut.mutate()}
            >
              <Unlink className="size-4" /> Disconnect
            </Button>
            <Button
              disabled={connectMut.isPending || !settings?.has_token}
              onClick={() => connectMut.mutate()}
            >
              <Plug className="size-4" /> Connect
            </Button>
          </div>
        }
      />

      {/* ── Status ─────────────────────────────────────────────── */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between space-y-0">
          <div>
            <CardTitle>Feed status</CardTitle>
            <CardDescription>
              Reported by the feed server, refreshed every 5 s.
            </CardDescription>
          </div>
          <StatusPill status={pill} />
        </CardHeader>
        <CardContent className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Account" value={status?.account_id || settings?.account_id || "—"} />
          <Field label="Region" value={status?.region || settings?.region || "default"} />
          <Field
            label="Symbols streaming"
            value={`${status?.symbols?.length ?? 0} / ${cap}`}
          />
          <Field
            label="Last tick"
            value={
              status?.last_rx_age_sec != null ? `${status.last_rx_age_sec}s ago` : "—"
            }
          />
          <Field label="Config from" value={settings?.source === "admin" ? "Admin panel" : ".env file"} />
          <Field label="Prices cached" value={String(status?.tick_count ?? 0)} />
        </CardContent>
        {status && !status.live && (
          <CardContent className="pt-0">
            <p className="flex items-start gap-2 rounded-md border border-yellow-500/30 bg-yellow-500/10 px-3 py-2 text-xs text-yellow-600 dark:text-yellow-400">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              The feed server hasn&apos;t reported recently. It may be starting up, or
              the feed process is down.
            </p>
          </CardContent>
        )}
        {status?.last_error && (
          <CardContent className="pt-0">
            <p className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span>{status.last_error}</span>
            </p>
          </CardContent>
        )}
      </Card>

      {/* ── Credentials ────────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="size-4" /> Credentials
          </CardTitle>
          <CardDescription>
            Token is encrypted before it is stored and never shown again. The
            account must be deployed and connected on metaapi.cloud.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="mt-token">API token</Label>
            <Input
              id="mt-token"
              type="password"
              autoComplete="off"
              placeholder={settings?.has_token ? "•••••••• saved — type to replace" : "metaapi.cloud token"}
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="mt-account">Account id</Label>
            <Input
              id="mt-account"
              autoComplete="off"
              placeholder="e.g. 0a1b2c3d-...."
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="mt-region">Region (optional)</Label>
            <Input
              id="mt-region"
              autoComplete="off"
              placeholder="e.g. new-york"
              value={region}
              onChange={(e) => setRegion(e.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="mt-cap">Max symbols (plan limit)</Label>
            <Input
              id="mt-cap"
              inputMode="numeric"
              value={maxSymbols}
              onChange={(e) => setMaxSymbols(e.target.value)}
            />
          </div>
          <div className="space-y-1.5 sm:col-span-2">
            <Label htmlFor="mt-aliases">
              Symbol names at your broker (optional)
            </Label>
            <Input
              id="mt-aliases"
              autoComplete="off"
              placeholder="SPX500:US500, USOIL:SPOTCRUDE, AAPL:AAPL.US"
              value={aliases}
              onChange={(e) => setAliases(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Use this when your broker names a symbol differently: our name
              first, the broker&apos;s name after the colon. Load the symbol
              list below to see the exact names.
            </p>
          </div>
          <div>
            <Button
              disabled={saveSettingsMut.isPending}
              onClick={() => saveSettingsMut.mutate()}
            >
              {saveSettingsMut.isPending && <Loader2 className="size-4 animate-spin" />}
              Save credentials
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* ── Symbols ────────────────────────────────────────────── */}
      <Card>
        <CardHeader>
          <CardTitle>Symbols</CardTitle>
          <CardDescription>
            Picked {selected.length} of {cap}. The list comes from your broker, so
            names match exactly (US30, XAUUSD, EURUSD.raw…).
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              disabled={symbolsQuery.isFetching || !settings?.has_token}
              onClick={() => symbolsQuery.refetch()}
            >
              {symbolsQuery.isFetching ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <DownloadCloud className="size-4" />
              )}
              Load symbols from broker
            </Button>
            <div className="relative min-w-[200px] flex-1">
              <Search className="pointer-events-none absolute left-2 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                className="pl-8"
                placeholder="Search symbols"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            </div>
            <Button
              disabled={saveSymbolsMut.isPending || selected.length > cap}
              onClick={() => saveSymbolsMut.mutate()}
            >
              {saveSymbolsMut.isPending && <Loader2 className="size-4 animate-spin" />}
              Save symbols
            </Button>
          </div>

          {selected.length > cap && (
            <p className="text-xs text-destructive">
              {selected.length} picked — this plan streams {cap} at once. Remove{" "}
              {selected.length - cap} before saving.
            </p>
          )}

          <div className="max-h-80 overflow-y-auto rounded-md border">
            {visible.length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">
                {symbolsQuery.isFetching
                  ? "Loading symbols from your broker…"
                  : "No symbols yet — click “Load symbols from broker”."}
              </p>
            ) : (
              <ul className="divide-y">
                {visible.map((sym) => {
                  const on = selected.includes(sym);
                  return (
                    <li key={sym}>
                      <button
                        type="button"
                        onClick={() => toggle(sym)}
                        className={cn(
                          "flex w-full items-center justify-between px-3 py-1.5 text-left text-sm hover:bg-muted/50",
                          on && "bg-buy/10",
                        )}
                      >
                        <span className="font-mono">{sym}</span>
                        <span
                          className={cn(
                            "text-[11px] uppercase tracking-wider",
                            on ? "text-buy" : "text-muted-foreground",
                          )}
                        >
                          {on ? "streaming" : "add"}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

/** "SPX500:US500, AAPL:AAPL.US" → { SPX500: "US500", AAPL: "AAPL.US" } */
function parseAliases(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const part of text.split(/[\n,]/)) {
    const [platform, broker] = part.split(":");
    if (platform?.trim() && broker?.trim()) {
      out[platform.trim().toUpperCase()] = broker.trim();
    }
  }
  return out;
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="truncate font-medium" title={value}>
        {value}
      </div>
    </div>
  );
}
