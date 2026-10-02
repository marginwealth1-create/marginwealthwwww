"use client";

import { useState } from "react";
import Link from "next/link";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { toast } from "sonner";
import { Mail, KeyRound, Lock, ArrowLeft } from "lucide-react";
import { AuthAPI, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const requestSchema = z.object({ identifier: z.string().min(3) });
const resetSchema = z.object({
  identifier: z.string().min(3),
  otp: z.string().min(4).max(8),
  new_password: z
    .string()
    .min(6)
    .regex(/[A-Z]/)
    .regex(/[a-z]/)
    .regex(/\d/),
});

export default function ForgotPasswordPage() {
  const [step, setStep] = useState<"request" | "reset">("request");
  const [identifier, setIdentifier] = useState("");

  const requestForm = useForm({ resolver: zodResolver(requestSchema), defaultValues: { identifier: "" } });
  const resetForm = useForm({
    resolver: zodResolver(resetSchema),
    defaultValues: { identifier: "", otp: "", new_password: "" },
  });

  async function onRequest(v: { identifier: string }) {
    try {
      await AuthAPI.forgotPassword(v.identifier);
      toast.success("If the account exists, a reset code was sent.");
      setIdentifier(v.identifier);
      resetForm.setValue("identifier", v.identifier);
      setStep("reset");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not send reset code");
    }
  }

  async function onReset(v: { identifier: string; otp: string; new_password: string }) {
    try {
      await AuthAPI.resetPassword(v);
      toast.success("Password updated. Please sign in.");
      window.location.href = "/login";
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Reset failed");
    }
  }

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="space-y-2">
        <div className="mb-4 grid size-12 place-items-center rounded-2xl bg-primary/10 text-primary ring-1 ring-primary/15">
          <KeyRound className="size-5" />
        </div>
        <h2 className="text-3xl font-bold tracking-tight">Forgot password</h2>
        <p className="text-sm text-muted-foreground">
          {step === "request"
            ? "Enter your email or mobile and we'll send a reset code."
            : `Enter the code sent to ${identifier} and choose a new password.`}
        </p>
      </div>

      {/* Step indicator */}
      <div className="flex items-center gap-3">
        <div className={`flex items-center gap-2 text-xs font-semibold ${step === "request" ? "text-primary" : "text-muted-foreground"}`}>
          <span className={`grid size-6 place-items-center rounded-full text-[10px] font-bold ${step === "request" ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"}`}>
            1
          </span>
          Verify identity
        </div>
        <div className="h-px flex-1 bg-border/60" />
        <div className={`flex items-center gap-2 text-xs font-semibold ${step === "reset" ? "text-primary" : "text-muted-foreground"}`}>
          <span className={`grid size-6 place-items-center rounded-full text-[10px] font-bold ${step === "reset" ? "bg-primary text-primary-foreground" : "bg-muted text-muted-foreground"}`}>
            2
          </span>
          Reset password
        </div>
      </div>

      {step === "request" ? (
        <form onSubmit={requestForm.handleSubmit(onRequest)} className="space-y-5">
          <div className="space-y-2">
            <Label htmlFor="identifier" className="text-sm font-medium">Email or Mobile</Label>
            <div className="relative">
              <Mail className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                id="identifier"
                placeholder="you@example.com or 9999900000"
                className="h-12 rounded-xl border-border/60 bg-muted/30 pl-10 text-sm transition-colors focus:border-primary/50 focus:bg-background"
                {...requestForm.register("identifier")}
              />
            </div>
          </div>
          <Button type="submit" className="h-12 w-full rounded-xl text-sm font-semibold shadow-lg shadow-primary/20" loading={requestForm.formState.isSubmitting}>
            Send reset code
          </Button>
        </form>
      ) : (
        <form onSubmit={resetForm.handleSubmit(onReset)} className="space-y-5">
          <div className="space-y-2">
            <Label htmlFor="otp" className="text-sm font-medium">Reset code</Label>
            <div className="relative">
              <KeyRound className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                id="otp"
                inputMode="numeric"
                maxLength={6}
                placeholder="Enter 6-digit code"
                className="h-12 rounded-xl border-border/60 bg-muted/30 pl-10 text-sm transition-colors focus:border-primary/50 focus:bg-background"
                {...resetForm.register("otp")}
              />
            </div>
          </div>
          <div className="space-y-2">
            <Label htmlFor="new_password" className="text-sm font-medium">New password</Label>
            <div className="relative">
              <Lock className="absolute left-3.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                id="new_password"
                type="password"
                placeholder="Enter new password"
                className="h-12 rounded-xl border-border/60 bg-muted/30 pl-10 text-sm transition-colors focus:border-primary/50 focus:bg-background"
                {...resetForm.register("new_password")}
              />
            </div>
          </div>
          <Button type="submit" className="h-12 w-full rounded-xl text-sm font-semibold shadow-lg shadow-primary/20" loading={resetForm.formState.isSubmitting}>
            Reset password
          </Button>
        </form>
      )}

      <p className="text-center text-sm text-muted-foreground">
        <Link href="/login" className="inline-flex items-center gap-1.5 font-semibold text-primary hover:text-primary/80">
          <ArrowLeft className="size-3.5" />
          Back to sign in
        </Link>
      </p>
    </div>
  );
}
