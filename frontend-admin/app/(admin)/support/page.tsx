"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { FileText, Megaphone, MessageCircle, Plus, Save, Trash2 } from "lucide-react";
import { SupportAPI } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";
import { useAdminAuthStore } from "@/stores/authStore";

/** Builds the wa.me link from a raw input the way the apk's
 *  `buildWhatsappUrl` does — strip non-digits, require 8+ length, then
 *  prepend the wa.me prefix. Returns null when the input is unusable
 *  so the preview row hides the link entirely. */
function buildWaPreview(raw: string): string | null {
  const digits = String(raw || "").replace(/[^0-9]/g, "");
  if (digits.length < 8) return null;
  return `https://wa.me/${digits}`;
}

const ROLE_HINT: Record<string, string> = {
  SUPER_ADMIN:
    "You're the platform's root admin. The number you set here is the LAST-RESORT fallback for every user whose broker chain hasn't configured their own.",
  ADMIN:
    "You're a sub-admin. The number you set here is shown to YOUR pool of users (clients + your brokers' clients) unless a downstream broker overrides it with their own.",
  BROKER:
    "You're a broker. The number you set here is shown to YOUR clients (and any sub-brokers' clients who haven't set their own). Leave blank to inherit from your parent admin.",
};

export default function AdminSupportPage() {
  const qc = useQueryClient();
  const me = useAdminAuthStore((s) => s.admin);

  const { data, isFetching } = useQuery({
    queryKey: ["admin", "support"],
    queryFn: () => SupportAPI.get(),
  });

  // Form state — hydrated from the server on first load, then left alone
  // so a background refetch can't clobber an in-progress edit.
  const [draft, setDraft] = useState<string>("");
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    if (!data || hydrated) return;
    setDraft(data.whatsapp || "");
    setHydrated(true);
  }, [data, hydrated]);

  const saveMut = useMutation({
    mutationFn: (val: string) => SupportAPI.set(val),
    onSuccess: (resp) => {
      toast.success("Support number updated");
      qc.setQueryData(["admin", "support"], resp);
      // Invalidate the user-side support cache so any apk session that
      // hits /user/support next sees the fresh value without waiting
      // for the 10-minute query staleTime to expire.
      qc.invalidateQueries({ queryKey: ["support", "contacts"] });
    },
    onError: (e: any) => {
      toast.error(e?.message || "Could not save the number");
    },
  });

  const role = me?.role || data?.role || "";
  const hint =
    ROLE_HINT[role] ||
    "Set the WhatsApp number your downstream users will see on the Contact-support button.";
  const waPreview = buildWaPreview(draft);
  const trimmed = draft.trim();
  const serverVal = (data?.whatsapp || "").trim();
  const isDirty = trimmed !== serverVal;
  // Brokers can only edit their own support number when the admin granted the
  // `support` permission. Backend returns `can_edit`; default to true while the
  // query is still loading (or for admin/super where it's always true) so the
  // form doesn't flicker disabled. When false we lock the form and explain why.
  const canEdit = data?.can_edit !== false;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Support contact"
        description="Per-admin WhatsApp number shown to your users on the Add-funds / Contact-support button. Cascades down the broker hierarchy — leave blank to inherit from your parent admin."
      />

      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <MessageCircle className="size-5 text-primary" />
            Your support WhatsApp
          </CardTitle>
          <CardDescription>{hint}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {!canEdit && (
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-600 dark:text-amber-400">
              Your admin hasn&apos;t enabled the <b>Support</b> permission for
              you, so you can&apos;t set your own number yet. Your clients
              currently see your parent admin&apos;s support number. Ask your
              admin to turn on the Support permission to override it.
            </div>
          )}
          <div className="space-y-1.5">
            <label className="text-xs font-medium text-muted-foreground">
              WhatsApp number (include country code, e.g. +91 98765 43210)
            </label>
            <input
              type="tel"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="+91 98765 43210"
              disabled={(isFetching && !hydrated) || !canEdit}
              className="h-10 w-full rounded-md border border-border bg-muted/20 px-3 text-sm outline-none placeholder:text-muted-foreground focus:border-primary disabled:opacity-50"
            />
            <p className="text-[11px] text-muted-foreground">
              Spaces, dashes and the leading + are kept verbatim. The apk's
              wa.me link strips them automatically.
            </p>
          </div>

          {/* Preview of how the link will resolve. Helps the admin spot a
              missing country code or wrong digit count BEFORE saving. */}
          <div className="rounded-md border border-border bg-muted/10 px-3 py-2 text-xs">
            <div className="text-muted-foreground">Preview</div>
            {trimmed === "" ? (
              <div className="mt-0.5 text-foreground">
                <span className="font-medium">Cleared</span> — your users will
                fall back to your parent admin's number (or the platform
                default if you're a super-admin).
              </div>
            ) : waPreview ? (
              <div className="mt-0.5 break-all font-tabular text-primary">{waPreview}</div>
            ) : (
              <div className="mt-0.5 text-loss">
                Too short to be a valid WhatsApp number. Add the country code.
              </div>
            )}
          </div>

          <div className="flex items-center gap-2">
            <Button
              type="button"
              onClick={() => saveMut.mutate(trimmed)}
              loading={saveMut.isPending}
              disabled={!canEdit || !isDirty || saveMut.isPending || (trimmed !== "" && !waPreview)}
            >
              <Save className="size-4" />
              Save
            </Button>
            {isDirty && (
              <Button
                type="button"
                variant="ghost"
                onClick={() => setDraft(serverVal)}
                disabled={saveMut.isPending}
              >
                Reset
              </Button>
            )}
            {!isDirty && hydrated && serverVal && (
              <span className="text-xs text-muted-foreground">
                Saved · users see this number now
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* ── Home-page ticker card ────────────────────────────────── */}
      <TickerCard />

      {/* ── Terms & Conditions card ──────────────────────────────── */}
      <TermsCard />
    </div>
  );
}

/** Per-admin home-page ticker editor. Add multiple lines; they scroll
 *  continuously on the user's HOME screen until cleared. */
function TickerCard() {
  const qc = useQueryClient();
  const { data, isFetching } = useQuery({
    queryKey: ["admin", "support", "ticker"],
    queryFn: () => SupportAPI.getTicker(),
  });
  const [items, setItems] = useState<string[]>([""]);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    if (!data || hydrated) return;
    setItems(data.messages && data.messages.length ? data.messages : [""]);
    setHydrated(true);
  }, [data, hydrated]);

  const saveMut = useMutation({
    mutationFn: (msgs: string[]) => SupportAPI.setTicker(msgs),
    onSuccess: (resp) => {
      toast.success(
        resp.messages.length
          ? `${resp.messages.length} ticker line(s) live on home`
          : "Ticker cleared",
      );
      qc.setQueryData(["admin", "support", "ticker"], { messages: resp.messages, role: data?.role });
      qc.invalidateQueries({ queryKey: ["user", "ticker"] });
      setItems(resp.messages.length ? resp.messages : [""]);
    },
    onError: (e: any) => toast.error(e?.message || "Could not save ticker"),
  });

  const setAt = (i: number, v: string) =>
    setItems((a) => a.map((x, idx) => (idx === i ? v : x)));
  const removeAt = (i: number) =>
    setItems((a) => (a.length <= 1 ? [""] : a.filter((_, idx) => idx !== i)));
  const add = () => setItems((a) => (a.length >= 20 ? a : [...a, ""]));
  const cleaned = items.map((s) => s.trim()).filter(Boolean);

  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Megaphone className="size-5 text-primary" />
          Home ticker
        </CardTitle>
        <CardDescription>
          A scrolling notice strip shown on your users&apos; HOME page only. Add
          multiple lines — they scroll one after another, continuously, until you
          remove them. Cascades to your pool (closest admin with lines wins).
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-2">
          {items.map((val, i) => (
            <div key={i} className="flex items-center gap-2">
              <span className="w-5 shrink-0 text-center text-xs font-medium text-muted-foreground">
                {i + 1}
              </span>
              <input
                value={val}
                onChange={(e) => setAt(i, e.target.value)}
                maxLength={300}
                disabled={isFetching && !hydrated}
                placeholder="e.g. Dollar rate USD → INR is 96 today"
                className="flex-1 rounded-md border border-border bg-muted/20 px-3 py-2 text-sm outline-none placeholder:text-muted-foreground focus:border-primary disabled:opacity-50"
              />
              <button
                type="button"
                onClick={() => removeAt(i)}
                aria-label="Remove line"
                className="grid size-9 shrink-0 place-items-center rounded-md border border-border text-muted-foreground transition-colors hover:bg-muted/40 hover:text-destructive"
              >
                <Trash2 className="size-4" />
              </button>
            </div>
          ))}
        </div>
        <Button type="button" variant="outline" size="sm" onClick={add} disabled={items.length >= 20}>
          <Plus className="size-4" /> Add line
        </Button>
        <div className="flex items-center gap-2 pt-1">
          <Button
            type="button"
            onClick={() => saveMut.mutate(cleaned)}
            loading={saveMut.isPending}
          >
            <Save className="size-4" /> Save ticker
          </Button>
          <span className="text-xs text-muted-foreground">
            {cleaned.length} line(s) · {cleaned.length ? "scrolls on home page" : "ticker hidden when empty"}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}

/** Per-admin Terms & Conditions editor. Saving with `enabled=true`
 *  triggers a downstream reset so all clients in this admin's pool
 *  re-accept the new version on next login. */
function TermsCard() {
  const qc = useQueryClient();
  const { data, isFetching } = useQuery({
    queryKey: ["admin", "support", "terms"],
    queryFn: () => SupportAPI.getTerms(),
  });
  const [text, setText] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    if (!data || hydrated) return;
    setText(data.text || "");
    setEnabled(!!data.enabled);
    setHydrated(true);
  }, [data, hydrated]);

  const saveMut = useMutation({
    mutationFn: ({ t, e }: { t: string; e: boolean }) =>
      SupportAPI.setTerms(t, e),
    onSuccess: (resp) => {
      toast.success("Terms updated · clients will re-accept on next visit");
      qc.setQueryData(["admin", "support", "terms"], resp);
      qc.invalidateQueries({ queryKey: ["user", "terms"] });
    },
    onError: (e: any) => {
      toast.error(e?.message || "Could not save terms");
    },
  });

  const serverText = (data?.text || "").trim();
  const serverEnabled = !!data?.enabled;
  const dirty = text.trim() !== serverText || enabled !== serverEnabled;

  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FileText className="size-5 text-primary" />
          Terms &amp; Conditions
        </CardTitle>
        <CardDescription>
          Show a one-time Terms &amp; Conditions popup to every CLIENT in your
          pool right after they register. Each admin tier sets their own; the
          closest ancestor with terms enabled wins. Toggle off to disable.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <label className="flex items-center gap-3 rounded-md border border-border bg-muted/20 px-3 py-2 text-sm">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="size-4 accent-primary"
            disabled={isFetching && !hydrated}
          />
          <span className="flex-1">
            <span className="font-medium">Enable T&amp;C popup</span>
            <span className="ml-1 text-xs text-muted-foreground">
              — clients see this once after register, then again whenever you
              update the text below.
            </span>
          </span>
        </label>

        <div className="space-y-1.5">
          <label className="text-xs font-medium text-muted-foreground">
            Terms text (plain text, 20,000 char max)
          </label>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Type or paste your Terms & Conditions here. Line breaks are preserved when shown to the user."
            rows={10}
            disabled={isFetching && !hydrated}
            className="w-full rounded-md border border-border bg-muted/20 px-3 py-2 text-sm outline-none placeholder:text-muted-foreground focus:border-primary disabled:opacity-50"
          />
          <p className="text-[11px] text-muted-foreground">
            {text.length.toLocaleString("en-IN")} / 20,000 characters. Saving a
            changed version resets all clients&apos; acceptance so they
            re-confirm on next visit.
          </p>
        </div>

        <div className="flex items-center gap-2">
          <Button
            type="button"
            onClick={() => saveMut.mutate({ t: text, e: enabled })}
            loading={saveMut.isPending}
            disabled={
              !dirty ||
              saveMut.isPending ||
              (enabled && !text.trim())
            }
          >
            <Save className="size-4" />
            Save terms
          </Button>
          {dirty && (
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setText(serverText);
                setEnabled(serverEnabled);
              }}
              disabled={saveMut.isPending}
            >
              Reset
            </Button>
          )}
          {!dirty && hydrated && (
            <span className="text-xs text-muted-foreground">
              {serverEnabled
                ? "Live · users see this popup"
                : "Disabled · users skip the popup"}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
