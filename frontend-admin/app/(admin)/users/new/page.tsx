"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { toast } from "sonner";
import { Check, ChevronDown, Eye, EyeOff, Search, X } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { BrokerMgmtAPI, UsersAPI } from "@/lib/api";
import { useAdminAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/common/PageHeader";
import { cn } from "@/lib/utils";

// This page creates regular trading users (CLIENT). Sub-admins are minted
// from /management/sub-admins (super-admin only) — role is therefore not
// exposed here.
const schema = z
  .object({
    full_name: z.string().min(2),
    // Optional: an admin-created user logs in with the user code they're
    // handed, so a phone / email the admin doesn't have yet shouldn't
    // block the account. A self-signup still requires both.
    mobile: z
      .string()
      .trim()
      .refine((v) => v === "" || /^[6-9]\d{9}$/.test(v), "10-digit Indian mobile")
      .optional(),
    email: z
      .string()
      .trim()
      .refine((v) => v === "" || /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v), "Invalid email")
      .optional(),
    password: z.string().min(6),
    // Confirmation field — added after a sub-admin typo'd the initial
    // password (Prachi / 18-May-2026) and the new user couldn't log in.
    // Backend never gets this field; the schema-level `refine` below
    // catches the mismatch client-side before submit.
    confirm_password: z.string().min(6),
    initial_balance: z.coerce.number().min(0).default(0),
    credit_limit: z.coerce.number().min(0).default(0),
    // "" = Self (keep user directly under caller).  Otherwise a broker /
    // sub-broker id to place the user inside that broker's subtree.
    assign_to_broker_id: z.string().optional(),
    is_demo: z.boolean().default(false),
  })
  .refine((v) => v.password === v.confirm_password, {
    path: ["confirm_password"],
    message: "Passwords don't match",
  });
type Values = z.infer<typeof schema>;

export default function NewUserPage() {
  const router = useRouter();
  const [showPassword, setShowPassword] = useState(false);
  const admin = useAdminAuthStore((s) => s.admin);
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      full_name: "",
      mobile: "",
      email: "",
      password: "",
      confirm_password: "",
      initial_balance: 0,
      credit_limit: 0,
      assign_to_broker_id: "",
      is_demo: false,
    },
  });

  // Brokers + sub-brokers in the caller's scope. `include_sub=true`
  // drops the "top brokers only" filter so the dropdown also lists every
  // sub-broker under the caller — admins can place a new client directly
  // under any descendant, brokers can pick any of their downline.
  const brokersQuery = useQuery({
    queryKey: ["admin", "brokers", "active-with-sub"],
    queryFn: () =>
      BrokerMgmtAPI.list({
        status: "ACTIVE",
        page_size: 200,
        include_sub: true,
      }),
  });

  const brokerOptions = (brokersQuery.data?.items ?? []).filter((b: any) => {
    // Hide the caller themselves from the dropdown — "Self" handles that.
    return String(b.id) !== String(admin?.id ?? "");
  });

  async function onSubmit(v: Values, gotoSegments = false) {
    // confirm_password is a client-only guard — strip before the POST so
    // the backend's CreateUserRequest schema (which doesn't know about
    // this field) doesn't reject the request as `extra=forbid`.
    const { confirm_password: _confirm, assign_to_broker_id, ...rest } = v;
    void _confirm;
    // Blank contacts are dropped rather than sent as "" — the backend
    // fills in its own placeholder (email and mobile are uniquely indexed
    // and non-null) and knows not to show it back as a real address.
    const payload: any = { ...rest, role: "CLIENT" };
    if (!payload.email) delete payload.email;
    if (!payload.mobile) delete payload.mobile;
    if (assign_to_broker_id) payload.assign_to_broker_id = assign_to_broker_id;
    try {
      const created = await UsersAPI.create(payload);
      toast.success(`Created ${created.user_code}`);
      // "Create & set segment settings" drops the admin straight onto this new
      // user's per-user override editor (the full matrix UI). Plain create
      // goes to the user detail page as before.
      router.push(
        gotoSegments
          ? `/segment-settings?tab=users&user=${created.id}`
          : `/users/${created.id}`,
      );
    } catch (e: any) {
      toast.error(e.message || "Failed to create");
    }
  }

  const selfLabel =
    admin?.role === "BROKER"
      ? "Self (this broker)"
      : admin?.role === "ADMIN"
        ? "Self (this admin)"
        : "Self (platform)";

  return (
    <div className="space-y-6">
      <PageHeader title="Create user" description="Provision a new account. Use “Create & set segment settings” to configure this user's per-user overrides right after creating." />

      <form onSubmit={form.handleSubmit((v) => onSubmit(v, false))} className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Basics</CardTitle>
            <CardDescription>Identity + login</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Field label="Full name" error={form.formState.errors.full_name?.message}>
              <Input {...form.register("full_name")} />
            </Field>
            <Field label="Mobile (optional)" error={form.formState.errors.mobile?.message}>
              <Input maxLength={10} placeholder="Leave blank if unknown" {...form.register("mobile")} />
            </Field>
            <Field label="Email (optional)" error={form.formState.errors.email?.message}>
              <Input placeholder="Leave blank if unknown" {...form.register("email")} />
            </Field>
            <Field label="Initial password" error={form.formState.errors.password?.message}>
              <div className="relative">
                <Input
                  type={showPassword ? "text" : "password"}
                  className="pr-10"
                  {...form.register("password")}
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((s) => !s)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  title={showPassword ? "Hide password" : "Show password"}
                  className="absolute inset-y-0 right-0 grid w-10 place-items-center text-muted-foreground hover:text-foreground"
                >
                  {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                </button>
              </div>
            </Field>
            <Field
              label="Confirm password"
              error={form.formState.errors.confirm_password?.message}
            >
              <Input
                type={showPassword ? "text" : "password"}
                {...form.register("confirm_password")}
              />
            </Field>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Access &amp; balances</CardTitle>
            <CardDescription>Placement + opening balance + credit limit</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Field
              label="Place user under"
              error={form.formState.errors.assign_to_broker_id?.message}
            >
              <BrokerCombobox
                value={form.watch("assign_to_broker_id") || ""}
                onChange={(v) =>
                  form.setValue("assign_to_broker_id", v, { shouldValidate: true })
                }
                selfLabel={selfLabel}
                options={brokerOptions}
                loading={brokersQuery.isLoading}
              />
              <p className="text-[11px] text-muted-foreground">
                Choose the broker / sub-broker this user belongs under. Default is
                Self.
              </p>
            </Field>
            <Field label="Initial balance (₹)">
              <Input type="number" step="0.01" {...form.register("initial_balance")} />
            </Field>
            <Field label="Credit limit (₹)">
              <Input type="number" step="0.01" {...form.register("credit_limit")} />
            </Field>
            <div className="flex items-start gap-3 rounded-md border border-amber-500/30 bg-amber-500/5 p-3">
              <input
                id="is_demo"
                type="checkbox"
                {...form.register("is_demo")}
                className="mt-0.5 h-4 w-4 cursor-pointer accent-amber-500"
              />
              <div>
                <label htmlFor="is_demo" className="cursor-pointer text-sm font-medium text-amber-600 dark:text-amber-400">
                  Demo Account
                </label>
                <p className="mt-0.5 text-[11px] text-muted-foreground">
                  Demo accounts are invisible in all admin views (accounts, payments, orders). Virtual balance of ₹1,00,000 is auto-credited if initial balance is 0. Trades &amp; data auto-purge after 7 days.
                </p>
              </div>
            </div>
          </CardContent>
        </Card>

        <div className="flex flex-wrap items-center justify-end gap-2 lg:col-span-2">
          <Button variant="outline" type="button" onClick={() => router.back()}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="outline"
            loading={form.formState.isSubmitting}
            onClick={form.handleSubmit((v) => onSubmit(v, true))}
          >
            Create &amp; set segment settings
          </Button>
          <Button type="submit" loading={form.formState.isSubmitting}>
            Create user
          </Button>
        </div>
      </form>
    </div>
  );
}

function Field({ label, error, children }: { label: string; error?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      {children}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}

/** Searchable broker / sub-broker picker. Native <select> can't filter
 *  as you type, so admins with 50+ brokers had no way to find a name
 *  short of scrolling. This combobox shows a search input on focus and
 *  filters the list by name / user_code substring. */
function BrokerCombobox({
  value,
  onChange,
  selfLabel,
  options,
  loading,
}: {
  value: string;
  onChange: (id: string) => void;
  selfLabel: string;
  options: any[];
  loading: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Close on outside click — without this the menu stays open after the
  // user picks something elsewhere on the form.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const selected = useMemo(
    () => options.find((b: any) => String(b.id) === value),
    [options, value],
  );

  const selectedLabel = selected
    ? `${selected.assigned_broker_id ? "Sub-broker" : "Broker"} · ${
        selected.full_name || selected.user_code
      }${selected.user_code ? ` (${selected.user_code})` : ""}`
    : selfLabel;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter((b: any) => {
      const name = String(b.full_name ?? "").toLowerCase();
      const code = String(b.user_code ?? "").toLowerCase();
      return name.includes(q) || code.includes(q);
    });
  }, [options, query]);

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        disabled={loading}
        className="flex h-9 w-full items-center justify-between gap-2 rounded-md border border-border bg-background px-2 text-left text-sm disabled:cursor-not-allowed disabled:opacity-50"
      >
        <span className={cn("truncate", !selected && "text-muted-foreground")}>
          {loading ? "Loading…" : selectedLabel}
        </span>
        <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
      </button>

      {open && (
        <div className="absolute z-50 mt-1 w-full overflow-hidden rounded-md border border-border bg-popover shadow-lg">
          <div className="flex items-center gap-2 border-b border-border px-2">
            <Search className="size-3.5 shrink-0 text-muted-foreground" />
            <Input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search by name or code…"
              className="h-8 border-0 px-0 focus-visible:ring-0"
            />
            {query && (
              <button
                type="button"
                onClick={() => setQuery("")}
                aria-label="Clear search"
                className="grid size-5 place-items-center rounded text-muted-foreground hover:bg-muted/40 hover:text-foreground"
              >
                <X className="size-3" />
              </button>
            )}
          </div>

          <ul className="max-h-64 overflow-y-auto py-1">
            <li>
              <button
                type="button"
                onClick={() => {
                  onChange("");
                  setOpen(false);
                  setQuery("");
                }}
                className={cn(
                  "flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-sm hover:bg-muted/40",
                  !value && "bg-primary/10",
                )}
              >
                <Check className={cn("size-3.5", !value ? "opacity-100" : "opacity-0")} />
                <span>{selfLabel}</span>
              </button>
            </li>
            {filtered.length === 0 && query && (
              <li className="px-2.5 py-2 text-center text-xs text-muted-foreground">
                No matches for &ldquo;{query}&rdquo;
              </li>
            )}
            {filtered.map((b: any) => {
              const isSub = !!b.assigned_broker_id;
              const isPicked = String(b.id) === value;
              return (
                <li key={b.id}>
                  <button
                    type="button"
                    onClick={() => {
                      onChange(String(b.id));
                      setOpen(false);
                      setQuery("");
                    }}
                    className={cn(
                      "flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-sm hover:bg-muted/40",
                      isPicked && "bg-primary/10",
                    )}
                  >
                    <Check className={cn("size-3.5", isPicked ? "opacity-100" : "opacity-0")} />
                    <span
                      className={cn(
                        "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider",
                        isSub
                          ? "bg-brand-500/10 text-brand-500 ring-1 ring-inset ring-brand-500/30"
                          : "bg-brand-500/10 text-brand-500 ring-1 ring-inset ring-brand-500/30",
                      )}
                    >
                      {isSub ? "Sub-broker" : "Broker"}
                    </span>
                    <span className="truncate font-medium">
                      {b.full_name || b.user_code}
                    </span>
                    {b.user_code && (
                      <span className="ml-auto font-mono text-[10px] text-muted-foreground">
                        {b.user_code}
                      </span>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
