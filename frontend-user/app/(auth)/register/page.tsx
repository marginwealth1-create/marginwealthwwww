"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useBranding } from "@/lib/branding-context";
import { STORAGE_KEYS } from "@/lib/constants";
import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { toast } from "sonner";
import { ArrowRight, Check, X } from "lucide-react";
import { AuthAPI, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { cn } from "@/lib/utils";
import { FIELD, GOLD_BTN } from "../styles";

const schema = z.object({
  full_name: z.string().min(2, "Enter your full name").max(128),
  email: z.string().email("Invalid email"),
  mobile: z
    .string()
    .regex(/^[6-9]\d{9}$/, "10-digit Indian mobile starting 6/7/8/9"),
  password: z
    .string()
    .min(6, "Minimum 6 characters")
    .regex(/[A-Z]/, "Must contain an uppercase letter")
    .regex(/[a-z]/, "Must contain a lowercase letter")
    .regex(/\d/, "Must contain a digit")
    .regex(/[^A-Za-z0-9]/, "Must contain a special character (e.g. @, #, $)"),
});
type FormValues = z.infer<typeof schema>;

const PWD_RULES = [
  { id: "len",   label: "At least 6 characters",       test: (s: string) => s.length >= 6 },
  { id: "upper", label: "One uppercase letter (A–Z)",  test: (s: string) => /[A-Z]/.test(s) },
  { id: "lower", label: "One lowercase letter (a–z)",  test: (s: string) => /[a-z]/.test(s) },
  { id: "digit", label: "One number (0–9)",            test: (s: string) => /\d/.test(s) },
  { id: "spec",  label: "One special character (@, #, $…)", test: (s: string) => /[^A-Za-z0-9]/.test(s) },
];

type Strength = {
  score: number;
  label: string;
  chipClass: string;
  barClass: string;
};

function passwordStrength(pwd: string): Strength {
  const score = PWD_RULES.reduce((n, r) => n + (r.test(pwd) ? 1 : 0), 0);
  if (!pwd) {
    return { score: 0, label: "", chipClass: "", barClass: "bg-muted" };
  }
  if (score <= 2) {
    return {
      score,
      label: "Weak",
      chipClass: "bg-sell/15 text-sell ring-1 ring-sell/30",
      barClass: "bg-sell",
    };
  }
  if (score <= 4) {
    return {
      score,
      label: "Medium",
      chipClass: "bg-atm/20 text-atm ring-1 ring-atm/40",
      barClass: "bg-atm",
    };
  }
  return {
    score,
    label: "Strong",
    chipClass: "bg-buy/15 text-buy ring-1 ring-buy/30",
    barClass: "bg-buy",
  };
}

export default function RegisterPage() {
  return (
    <Suspense fallback={null}>
      <RegisterPageInner />
    </Suspense>
  );
}

function RegisterPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const urlRef = (searchParams?.get("ref") || "").trim().toUpperCase();

  // Persist the referral code so admin/broker/sub-broker attribution survives
  // a demo-login → logout → register round-trip (the ?ref= query is dropped
  // across those hops, and the footer "Sign in" link doesn't carry it). A
  // fresh ?ref= in the URL always wins and overwrites; otherwise we fall back
  // to the last captured code. clearTokens() (logout) does NOT touch this key.
  const [storedRef, setStoredRef] = useState("");
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (urlRef) {
      window.localStorage.setItem(STORAGE_KEYS.referralCode, urlRef);
      setStoredRef(urlRef);
    } else {
      setStoredRef(
        (window.localStorage.getItem(STORAGE_KEYS.referralCode) || "")
          .trim()
          .toUpperCase(),
      );
    }
  }, [urlRef]);
  const refCode = urlRef || storedRef;

  // On a tenant custom domain (e.g. stockcafe.live) the URL has no ?ref=,
  // so fall back to the resolved brand's admin code. Belt-and-suspenders
  // alongside the backend's Origin/Referer detection — covers the case
  // where a proxy strips the Origin header.
  const { branding } = useBranding();
  const [showPwd, setShowPwd] = useState(false);
  const [pwdFocused, setPwdFocused] = useState(false);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { full_name: "", email: "", mobile: "", password: "" },
    mode: "onChange",
  });

  const pwd = form.watch("password") || "";
  const strength = passwordStrength(pwd);
  const showRules = pwdFocused || pwd.length > 0;
  const [blockedMsg, setBlockedMsg] = useState<string | null>(null);

  async function onSubmit(values: FormValues) {
    try {
      await AuthAPI.register({
        full_name: values.full_name,
        email: values.email,
        mobile: values.mobile,
        password: values.password,
        referral_code: refCode || branding?.user_code || undefined,
      });
      // Consumed — drop the persisted code so a later unrelated signup on
      // this browser isn't mis-attributed to a stale referrer.
      try {
        window.localStorage.removeItem(STORAGE_KEYS.referralCode);
      } catch {
        /* ignore */
      }
      toast.success("Account created. Please sign in.");
      router.push(refCode ? `/login?ref=${encodeURIComponent(refCode)}` : "/login");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Registration failed";
      // Registration turned off for this pool → prominent popup, not a toast.
      if (/registration is (temporarily )?disabled/i.test(msg)) {
        setBlockedMsg(msg);
      } else {
        toast.error(msg);
      }
    }
  }

  return (
    <div>
      {/* Registration-disabled popup — shown when this pool has signups off. */}
      {blockedMsg && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={() => setBlockedMsg(null)}>
          <div className="w-full max-w-sm rounded-2xl bg-card p-6 text-center shadow-2xl" onClick={(e) => e.stopPropagation()}>
            <div className="mx-auto mb-3 grid size-12 place-items-center rounded-full bg-amber-500/15 text-amber-500">
              <X className="size-6" />
            </div>
            <h3 className="text-lg font-semibold">Registration closed</h3>
            <p className="mt-1.5 text-sm text-muted-foreground">{blockedMsg}</p>
            <Button className="mt-5 w-full" onClick={() => setBlockedMsg(null)}>OK</Button>
          </div>
        </div>
      )}

      <p className="text-xs font-semibold uppercase tracking-[0.2em] text-gold">Open account</p>
      <h1 className="mt-2 font-display text-3xl font-bold tracking-tight">Start trading today</h1>
      <p className="mt-2 text-sm text-muted-foreground">Free account, ready in under a minute.</p>

      <form onSubmit={form.handleSubmit(onSubmit)} className="mt-7 space-y-5">
        <div className="space-y-2">
          <Label htmlFor="full_name">Full name</Label>
          <Input id="full_name" placeholder="Rohan Sharma" autoComplete="name" className={FIELD} {...form.register("full_name")} />
          {form.formState.errors.full_name && (
            <p className="text-xs text-destructive">{form.formState.errors.full_name.message}</p>
          )}
        </div>

        <div className="grid gap-5 sm:grid-cols-2 sm:gap-4">
          <div className="space-y-2">
            <Label htmlFor="email">Email</Label>
            <Input id="email" type="email" placeholder="you@example.com" autoComplete="email" className={FIELD} {...form.register("email")} />
            {form.formState.errors.email && (
              <p className="text-xs text-destructive">{form.formState.errors.email.message}</p>
            )}
          </div>
          <div className="space-y-2">
            <Label htmlFor="mobile">Mobile</Label>
            <Input
              id="mobile"
              inputMode="numeric"
              maxLength={10}
              autoComplete="tel"
              placeholder="9999900000"
              className={FIELD}
              {...form.register("mobile")}
            />
            {form.formState.errors.mobile && (
              <p className="text-xs text-destructive">{form.formState.errors.mobile.message}</p>
            )}
          </div>
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label htmlFor="password">Password</Label>
            {pwd && (
              <span className={cn("rounded-full px-2.5 py-0.5 text-[10px] font-bold uppercase tracking-wider", strength.chipClass)}>
                {strength.label}
              </span>
            )}
          </div>
          <div className="relative">
            <Input
              id="password"
              type={showPwd ? "text" : "password"}
              placeholder="e.g. Abc@1234"
              autoComplete="new-password"
              className={cn(FIELD, "pr-16")}
              {...form.register("password", { onBlur: () => setPwdFocused(false) })}
              onFocus={() => setPwdFocused(true)}
            />
            <button
              type="button"
              onClick={() => setShowPwd((v) => !v)}
              aria-label={showPwd ? "Hide password" : "Show password"}
              aria-pressed={showPwd}
              tabIndex={-1}
              className="absolute inset-y-0 right-0 px-4 text-xs font-semibold text-muted-foreground hover:text-foreground"
            >
              {showPwd ? "Hide" : "Show"}
            </button>
          </div>

          <div className="flex gap-1.5" aria-hidden>
            {[0, 1, 2, 3, 4].map((i) => (
              <div
                key={i}
                className={cn(
                  "h-1 flex-1 rounded-full transition-colors duration-300",
                  i < strength.score ? strength.barClass : "bg-white/10",
                )}
              />
            ))}
          </div>

          {showRules && (
            <ul className="grid gap-x-3 gap-y-1.5 pt-1 sm:grid-cols-2" aria-live="polite">
              {PWD_RULES.map((r) => {
                const ok = r.test(pwd);
                return (
                  <li
                    key={r.id}
                    className={cn("flex items-center gap-1.5 text-[11px] transition-colors", ok ? "text-buy" : "text-muted-foreground")}
                  >
                    {ok ? <Check className="size-3 shrink-0" strokeWidth={3} /> : <X className="size-3 shrink-0" strokeWidth={3} />}
                    <span className="leading-tight">{r.label}</span>
                  </li>
                );
              })}
            </ul>
          )}

          {form.formState.errors.password && !showRules && (
            <p className="text-xs text-destructive">{form.formState.errors.password.message}</p>
          )}
        </div>

        <Button type="submit" className={GOLD_BTN} loading={form.formState.isSubmitting}>
          Create account {!form.formState.isSubmitting && <ArrowRight />}
        </Button>
      </form>

      <p className="mt-7 text-center text-sm text-muted-foreground">
        Already have an account?{" "}
        <Link
          href={refCode ? `/login?ref=${encodeURIComponent(refCode)}` : "/login"}
          className="font-semibold text-gold hover:text-gold/80"
        >
          Sign in
        </Link>
      </p>
    </div>
  );
}
