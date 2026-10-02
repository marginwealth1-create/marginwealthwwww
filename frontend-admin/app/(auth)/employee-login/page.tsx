"use client";

import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { toast } from "sonner";
import { Users } from "lucide-react";
import { useAdminAuthStore } from "@/stores/authStore";
import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { BrandLogo } from "@/components/layout/BrandLogo";
import { InstallPWAButton } from "@/components/pwa/InstallPWAButton";

const schema = z.object({
  identifier: z.string().min(3, "Enter your employee email or user code"),
  password: z.string().min(6, "Minimum 6 characters"),
});
type FormValues = z.infer<typeof schema>;

export default function EmployeeLoginPage() {
  const router = useRouter();
  const employeeLogin = useAdminAuthStore((s) => s.employeeLogin);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { identifier: "", password: "" },
  });

  async function onSubmit(values: FormValues) {
    try {
      await employeeLogin(values.identifier, values.password);
      toast.success("Welcome");
      router.push("/dashboard");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Login failed");
    }
  }

  return (
    <main className="grid min-h-screen place-items-center bg-background p-6">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-3">
          <BrandLogo href={null} size="md" showAdminBadge={false} />
          <div className="inline-flex w-fit items-center gap-2 rounded-md bg-primary/10 px-2 py-1 text-xs uppercase tracking-wider text-primary">
            <Users className="size-3" />
            Employee access
          </div>
          <CardTitle className="text-2xl">Employee Login</CardTitle>
          <CardDescription>
            Sign in with the credentials your admin gave you. You&apos;ll see only the sections you were granted.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="identifier">Employee email or user code</Label>
              <Input id="identifier" autoComplete="username" {...form.register("identifier")} />
              {form.formState.errors.identifier && (
                <p className="text-xs text-destructive">{form.formState.errors.identifier.message}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input id="password" type="password" autoComplete="current-password" {...form.register("password")} />
              {form.formState.errors.password && (
                <p className="text-xs text-destructive">{form.formState.errors.password.message}</p>
              )}
            </div>
            <Button type="submit" className="w-full" loading={form.formState.isSubmitting}>
              Sign in
            </Button>
            <p className="text-xs text-muted-foreground">
              Activity is logged. IP allow-listing and rate-limiting are enforced server-side.
            </p>
          </form>

          <div className="mt-5 space-y-2 border-t border-border pt-4">
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="text-xs font-semibold">Install MP Admin app</div>
                <p className="text-[11px] leading-snug text-muted-foreground">
                  One-tap home-screen launcher. Stays signed in like a native app.
                </p>
              </div>
            </div>
            <InstallPWAButton />
          </div>
        </CardContent>
      </Card>
    </main>
  );
}
