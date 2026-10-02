"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  CheckCircle2,
  KeyRound,
  Loader2,
  Pause,
  Play,
  Repeat,
  RotateCcw,
  ShieldCheck,
  Timer,
  XCircle,
  Zap,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  ZerodhaAutoLoginAPI,
  type ZerodhaAutoLoginStatus,
} from "@/lib/api";
import { isSuperAdmin } from "@/lib/permissions";
import { useAdminAuthStore } from "@/stores/authStore";

import { CredentialsModal } from "./CredentialsModal";

const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;

/**
 * Daily Kite auto-login card — compact and professional. Sits ABOVE the
 * Credentials/Status grid on the Zerodha admin page. Super-admin only.
 *
 * Sized for high information density: small fonts, tight padding, no
 * decorative orbs / massive hero — just one terminal-style countdown
 * row, a small stat strip, and a single tight controls row.
 */
export function AutoLoginPanel({ account = 0 }: { account?: number }) {
  const STATUS_QUERY_KEY = ["zerodha", "auto-login", "status", account] as const;

  const admin = useAdminAuthStore((s) => s.admin);
  const qc = useQueryClient();
  const [credsOpen, setCredsOpen] = useState(false);
  const [scheduleInput, setScheduleInput] = useState<string>("");
  const [fastPollUntil, setFastPollUntil] = useState(0);

  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const statusQuery = useQuery<ZerodhaAutoLoginStatus>({
    queryKey: STATUS_QUERY_KEY,
    queryFn: () => ZerodhaAutoLoginAPI.status(account),
    // A queued test runs on the feed server within ~30 s — poll fast until it lands.
    refetchInterval: (q) =>
      q.state.data?.last_stage === "in_progress" || Date.now() < fastPollUntil
        ? 3_000
        : 15_000,
    enabled: isSuperAdmin(admin),
  });

  const status = statusQuery.data;

  function applyStatusToCache(next: ZerodhaAutoLoginStatus | undefined) {
    if (next) qc.setQueryData(STATUS_QUERY_KEY, next);
  }

  const credsMut = useMutation({
    mutationFn: (body: { username: string; password: string; totp_secret: string }) =>
      ZerodhaAutoLoginAPI.updateCredentials(body, account),
    onSuccess: (next) => {
      applyStatusToCache(next);
      toast.success("Credentials saved");
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Failed to save credentials"),
  });

  const toggleMut = useMutation({
    mutationFn: (enabled: boolean) => ZerodhaAutoLoginAPI.toggle(enabled, account),
    onSuccess: (next, enabled) => {
      applyStatusToCache(next);
      toast.success(`Auto-login ${enabled ? "enabled" : "disabled"}`);
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Toggle failed"),
  });

  const scheduleMut = useMutation({
    mutationFn: (s: string) => ZerodhaAutoLoginAPI.setSchedule(s, account),
    onSuccess: (next) => {
      applyStatusToCache(next);
      toast.success("Schedule updated");
      setScheduleInput("");
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Schedule update failed"),
  });

  const testMut = useMutation({
    mutationFn: () => ZerodhaAutoLoginAPI.testNow(account),
    onSuccess: (next) => {
      applyStatusToCache(next);
      setFastPollUntil(Date.now() + 120_000);
      toast.success("Test login queued — it runs on the feed server within ~30 s. Watch Last attempt below.");
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Test login failed"),
  });

  const resetLockMut = useMutation({
    mutationFn: () => ZerodhaAutoLoginAPI.resetLock(account),
    onSuccess: (next) => {
      applyStatusToCache(next);
      toast.success("Lock cleared — you can run Test Login now");
    },
    onError: (e: unknown) =>
      toast.error(e instanceof Error ? e.message : "Reset failed"),
  });

  if (!isSuperAdmin(admin)) return null;

  const isConfigured = !!status?.is_configured;
  const isEnabled = !!status?.is_enabled;
  const lastStatus = status?.last_status ?? "";
  const lastStage = status?.last_stage ?? "";
  const consecutiveFailures = status?.consecutive_failures ?? 0;
  const isStuckInProgress = lastStage === "in_progress";
  const schedule = status?.schedule_time_ist ?? "07:00";
  const lastDurationSec = status?.last_duration_ms
    ? (status.last_duration_ms / 1000).toFixed(1)
    : null;

  const healthState: "good" | "warning" | "bad" | "off" = !isConfigured
    ? "off"
    : !isEnabled
      ? "warning"
      : lastStatus === "failed"
        ? "bad"
        : "good";

  const nextRun = computeNextRun(schedule, nowTick);
  const countdown = formatCountdown(nextRun.deltaMs);

  return (
    <section className="overflow-hidden rounded-lg border border-border/60 bg-card/40 shadow-sm">
      {/* ── Top: title + countdown + token health.
          Stacks vertically on phones (<sm) so the title block, the
          countdown, and the token-health column don't collide on a
          280-360 px column. From sm+ it returns to the original
          three-column row layout. */}
      <div
        className={`flex flex-col gap-3 border-b border-border/60 px-4 py-3 sm:flex-row sm:flex-wrap sm:items-start ${heroBgClass(healthState)}`}
      >
        {/* Left: icon + heading + description */}
        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          <div
            className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-md ${heroIconBgClass(healthState)}`}
          >
            <Repeat className={`h-3.5 w-3.5 ${heroIconColorClass(healthState)}`} />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-sm font-semibold leading-tight">Daily auto-login</h3>
              <HealthPill state={healthState} />
            </div>
            <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground line-clamp-2 sm:truncate">
              Refreshes Kite token daily before market open · AES-256-GCM encrypted
            </p>
          </div>
        </div>

        {/* Mobile-only divider so the metric strip below feels separated. */}
        <div className="-mx-4 h-px bg-border/40 sm:hidden" />

        {/* Middle + Right become a 2-up grid on phones so both metrics
            sit side-by-side instead of stacking vertically and pushing
            the controls way down the page. */}
        <div className="grid grid-cols-2 gap-3 sm:contents">
          {/* Middle: countdown */}
          <div className="flex min-w-0 flex-col items-start gap-0.5 sm:min-w-[160px] sm:items-end">
            <div className="flex items-center gap-1 text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
              <Timer className="h-2.5 w-2.5" />
              Next run
            </div>
            <div
              className={`truncate font-mono text-base font-semibold tabular-nums leading-none ${
                isEnabled && isConfigured ? "text-foreground" : "text-muted-foreground"
              }`}
            >
              {isEnabled && isConfigured ? countdown.primary : "Paused"}
            </div>
            <div className="truncate text-[10px] text-muted-foreground">
              {isEnabled && isConfigured
                ? `${schedule} IST · ${nextRun.dateLabel}`
                : !isConfigured
                  ? "Not configured"
                  : "Scheduler off"}
            </div>
          </div>

          {/* Right: token health */}
          <div className="flex min-w-0 flex-col items-start gap-0.5 sm:min-w-[140px] sm:items-end">
            <div className="flex items-center gap-1 text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
              <ShieldCheck className="h-2.5 w-2.5" />
              Token health
            </div>
            <div
              className={`truncate text-base font-semibold leading-none ${
                lastStatus === "success"
                  ? "text-emerald-400"
                  : lastStatus === "failed"
                    ? "text-destructive"
                    : "text-muted-foreground"
              }`}
            >
              {lastStatus === "success"
                ? "Healthy"
                : lastStatus === "failed"
                  ? "Attention"
                  : "Idle"}
            </div>
            <div className="truncate text-[10px] text-muted-foreground">
              {lastDurationSec && lastStatus === "success"
                ? `${lastDurationSec}s · ${consecutiveFailures} fails`
                : status?.last_attempt_at
                  ? formatTs(status.last_attempt_at)
                  : "Never run"}
            </div>
          </div>
        </div>
      </div>

      {/* ── Progress bar (full-width thin line) ────────────────────── */}
      <ProgressBar
        percent={isEnabled && isConfigured ? countdown.progressPct : 0}
        state={healthState}
      />

      {/* ── Stat strip (4 compact cells) ──────────────────────────── */}
      <div className="grid grid-cols-2 gap-px bg-border/40 md:grid-cols-4">
        <StatCell
          label="Schedule"
          value={`${schedule} IST`}
          subtitle="Daily · all 7 days"
        />
        <StatCell
          label="Last attempt"
          value={formatTs(status?.last_attempt_at)}
          subtitle={
            lastStatus === "success" ? (
              <span className="inline-flex items-center gap-0.5 text-emerald-400">
                <CheckCircle2 className="h-2.5 w-2.5" />
                Success
              </span>
            ) : lastStatus === "failed" ? (
              <span className="inline-flex items-center gap-0.5 text-destructive">
                <XCircle className="h-2.5 w-2.5" />
                {status?.last_stage ? `at ${status.last_stage}` : "Failed"}
              </span>
            ) : (
              "Never run"
            )
          }
        />
        <StatCell
          label="Last success"
          value={formatTs(status?.last_success_at)}
          subtitle={lastDurationSec ? `${lastDurationSec}s` : "—"}
        />
        <StatCell
          label="Kite user"
          value={isConfigured ? status?.username_masked || "Saved" : "Not set"}
          subtitle={
            consecutiveFailures > 0 ? (
              <span className="text-destructive">
                {consecutiveFailures} fail{consecutiveFailures === 1 ? "" : "s"}
              </span>
            ) : (
              "Encrypted"
            )
          }
        />
      </div>

      {/* Inline error banner */}
      {lastStatus === "failed" && status?.last_error_detail && (
        <div className="mx-4 mt-3 flex items-start gap-1.5 rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-1.5 text-[11px] text-destructive">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <div className="min-w-0">
            <span className="font-medium">
              Last run failed{status?.last_stage ? ` at "${status.last_stage}"` : ""}:
            </span>{" "}
            <span className="opacity-80">{status.last_error_detail}</span>
          </div>
        </div>
      )}

      {/* ── Controls ──────────────────────────────────────────────── */}
      <div className="space-y-3 px-4 py-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="min-w-[160px] flex-1 space-y-1">
            <Label htmlFor="auto-login-schedule" className="text-[11px] font-medium">
              Trigger time (HH:MM IST)
            </Label>
            <Input
              id="auto-login-schedule"
              autoComplete="off"
              name="zerodha-schedule-time"
              placeholder={schedule}
              value={scheduleInput}
              onChange={(e) => setScheduleInput(e.target.value)}
              className="h-8 font-mono text-sm"
            />
          </div>
          <Button
            variant="outline"
            size="sm"
            className="h-8 text-xs"
            disabled={!scheduleInput.trim() || scheduleMut.isPending}
            onClick={() => {
              const v = scheduleInput.trim();
              if (!/^\d{1,2}:\d{2}$/.test(v)) {
                toast.error("Use HH:MM 24-hour format (e.g. 07:00)");
                return;
              }
              scheduleMut.mutate(v);
            }}
          >
            Save time
          </Button>
        </div>
        <p className="text-[10px] text-muted-foreground">
          The token is treated as expired at 07:00 IST (Kite flushes it around
          06:00) and self-heal logs in again then. A schedule like 07:15 is the
          backup run, with retries before the 09:15 market open.
        </p>

        <div className="flex flex-wrap items-center gap-1.5 border-t border-border/40 pt-3">
          <Button
            variant="outline"
            size="sm"
            className="h-8 text-xs"
            onClick={() => setCredsOpen(true)}
          >
            <KeyRound className="mr-1 h-3.5 w-3.5" />
            {isConfigured ? "Update credentials" : "Save credentials"}
          </Button>

          <Button
            size="sm"
            className="h-8 bg-gradient-to-br from-emerald-500 to-emerald-600 text-xs text-white shadow-sm shadow-emerald-500/20 hover:from-emerald-400 hover:to-emerald-500 focus-visible:ring-emerald-400 disabled:from-emerald-500/40 disabled:to-emerald-600/40 disabled:shadow-none"
            disabled={!isConfigured || testMut.isPending || isStuckInProgress}
            onClick={() => testMut.mutate()}
          >
            {testMut.isPending ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <Zap className="mr-1 h-3.5 w-3.5" />
            )}
            {testMut.isPending ? "Testing…" : "Test login now"}
          </Button>

          {isStuckInProgress && !testMut.isPending && (
            <Button
              variant="outline"
              size="sm"
              className="h-8 border-orange-400/40 text-xs text-orange-600 hover:border-orange-400/60 hover:bg-orange-50 dark:text-orange-400 dark:hover:bg-orange-950/30"
              disabled={resetLockMut.isPending}
              onClick={() => resetLockMut.mutate()}
            >
              {resetLockMut.isPending ? (
                <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              ) : (
                <RotateCcw className="mr-1 h-3.5 w-3.5" />
              )}
              Reset stuck lock
            </Button>
          )}

          <Button
            variant={isEnabled ? "ghost" : "secondary"}
            size="sm"
            className="ml-auto h-8 text-xs"
            disabled={!isConfigured || toggleMut.isPending}
            onClick={() => toggleMut.mutate(!isEnabled)}
          >
            {isEnabled ? (
              <>
                <Pause className="mr-1 h-3.5 w-3.5" />
                Disable
              </>
            ) : (
              <>
                <Play className="mr-1 h-3.5 w-3.5" />
                Enable
              </>
            )}
          </Button>
        </div>
      </div>

      <CredentialsModal
        open={credsOpen}
        onClose={() => setCredsOpen(false)}
        hasExisting={isConfigured}
        onSubmit={async (body) => {
          await credsMut.mutateAsync(body);
        }}
      />
    </section>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Sub-components                                                       */
/* ─────────────────────────────────────────────────────────────────── */

function StatCell({
  label,
  value,
  subtitle,
}: {
  label: string;
  value: string;
  subtitle?: React.ReactNode;
}) {
  return (
    <div className="bg-card/40 px-3 py-2">
      <div className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </div>
      <div className="mt-0.5 truncate text-xs font-semibold" title={value}>
        {value || "—"}
      </div>
      {subtitle ? (
        <div className="mt-0.5 truncate text-[10px] text-muted-foreground">
          {subtitle}
        </div>
      ) : null}
    </div>
  );
}

function HealthPill({ state }: { state: "good" | "warning" | "bad" | "off" }) {
  const map = {
    good: {
      label: "Enabled",
      pill: "border-emerald-500/30 bg-emerald-500/15 text-emerald-300",
      dot: "bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.7)] animate-pulse",
    },
    warning: {
      label: "Disabled",
      pill: "border-yellow-500/30 bg-yellow-500/10 text-yellow-300",
      dot: "bg-yellow-400",
    },
    bad: {
      label: "Failed",
      pill: "border-destructive/30 bg-destructive/10 text-destructive",
      dot: "bg-destructive animate-pulse",
    },
    off: {
      label: "Not configured",
      pill: "border-border bg-muted/40 text-muted-foreground",
      dot: "bg-muted-foreground/60",
    },
  } as const;
  const m = map[state];
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] font-medium ${m.pill}`}
    >
      <span className={`h-1 w-1 rounded-full ${m.dot}`} />
      {m.label}
    </span>
  );
}

function ProgressBar({
  percent,
  state,
}: {
  percent: number;
  state: "good" | "warning" | "bad" | "off";
}) {
  const fillClass =
    state === "good"
      ? "bg-emerald-400"
      : state === "bad"
        ? "bg-destructive"
        : state === "warning"
          ? "bg-yellow-400"
          : "bg-muted-foreground/40";
  return (
    <div className="h-0.5 w-full overflow-hidden bg-border/50">
      <div
        className={`h-full transition-[width] duration-500 ease-out ${fillClass}`}
        style={{ width: `${Math.min(100, Math.max(0, percent))}%` }}
      />
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Helpers                                                              */
/* ─────────────────────────────────────────────────────────────────── */

function heroBgClass(state: "good" | "warning" | "bad" | "off") {
  switch (state) {
    case "good":
      return "bg-gradient-to-r from-emerald-500/8 via-emerald-500/3 to-transparent";
    case "warning":
      return "bg-gradient-to-r from-yellow-500/8 via-yellow-500/3 to-transparent";
    case "bad":
      return "bg-gradient-to-r from-destructive/12 via-destructive/4 to-transparent";
    default:
      return "bg-gradient-to-r from-muted/20 via-muted/5 to-transparent";
  }
}

function heroIconBgClass(state: "good" | "warning" | "bad" | "off") {
  switch (state) {
    case "good":
      return "bg-emerald-500/15";
    case "warning":
      return "bg-yellow-500/15";
    case "bad":
      return "bg-destructive/15";
    default:
      return "bg-muted/30";
  }
}

function heroIconColorClass(state: "good" | "warning" | "bad" | "off") {
  switch (state) {
    case "good":
      return "text-emerald-400";
    case "warning":
      return "text-yellow-400";
    case "bad":
      return "text-destructive";
    default:
      return "text-muted-foreground";
  }
}

function computeNextRun(
  scheduleHHMM: string,
  now: number,
): { target: Date; deltaMs: number; dateLabel: string } {
  const [hStr, mStr] = scheduleHHMM.split(":");
  const h = Number.parseInt(hStr ?? "7", 10) || 7;
  const m = Number.parseInt(mStr ?? "0", 10) || 0;

  const nowIst = new Date(now + IST_OFFSET_MS);
  const target = new Date(nowIst);
  target.setUTCHours(h, m, 0, 0);
  if (target.getTime() <= nowIst.getTime()) {
    target.setUTCDate(target.getUTCDate() + 1);
  }
  const deltaMs = target.getTime() - nowIst.getTime();

  const dateLabel = target.toLocaleDateString("en-IN", {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });

  return { target, deltaMs, dateLabel };
}

function formatCountdown(deltaMs: number): {
  primary: string;
  progressPct: number;
} {
  if (deltaMs <= 0) {
    return { primary: "00:00:00", progressPct: 100 };
  }
  const totalSec = Math.floor(deltaMs / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  const primary =
    h >= 1
      ? `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
      : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  const dayMs = 24 * 60 * 60 * 1000;
  const elapsed = dayMs - Math.min(deltaMs, dayMs);
  const progressPct = (elapsed / dayMs) * 100;
  return { primary, progressPct };
}

function formatTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-IN", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    });
  } catch {
    return iso;
  }
}
