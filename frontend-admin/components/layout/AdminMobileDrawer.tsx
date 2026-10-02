"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { LogOut } from "lucide-react";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { BrandLogo } from "@/components/layout/BrandLogo";
import { useAdminNav, resolveNavLabel } from "@/components/layout/adminNav";
import { useAdminAuthStore } from "@/stores/authStore";
import { useMobileNav } from "@/components/layout/MobileNavContext";
import { cn } from "@/lib/utils";

/**
 * Mobile drawer (md:hidden) — left slide-in panel that mirrors the
 * desktop sidebar's nav. Auto-closes on route change so tapping a link
 * dismisses the overlay without needing a second tap.
 */
export function AdminMobileDrawer() {
  const pathname = usePathname();
  const admin = useAdminAuthStore((s) => s.admin);
  const logout = useAdminAuthStore((s) => s.logout);
  const visible = useAdminNav();
  const { open, setOpen } = useMobileNav();

  // Close on route change. Pathname-as-dep is the cleanest signal here;
  // it fires after the link is followed.
  React.useEffect(() => {
    setOpen(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  return (
    <Sheet open={open} onOpenChange={setOpen}>
      <SheetContent
        side="left"
        className="w-72 p-0 md:hidden"
      >
        {/* SR-only title for a11y — Radix Dialog requires a DialogTitle */}
        <SheetTitle className="sr-only">Admin navigation</SheetTitle>

        <div className="flex h-14 items-center border-b border-border px-4">
          <BrandLogo size="sm" />
        </div>

        <nav className="flex-1 space-y-4 overflow-y-auto px-2 py-3 scrollbar-thin">
          {visible.map((g) => (
            <div key={g.title} className="space-y-1">
              <div className="px-2 text-[10px] uppercase tracking-wider text-muted-foreground">
                {g.title}
              </div>
              {g.items.map((it) => {
                const active =
                  pathname === it.href || pathname?.startsWith(it.href + "/");
                const Icon = it.icon;
                return (
                  <Link
                    key={it.href}
                    href={it.href}
                    onClick={() => setOpen(false)}
                    className={cn(
                      "flex items-center gap-3 rounded-md px-3 py-2.5 text-sm transition-colors",
                      active
                        ? "bg-primary/10 text-primary"
                        : "text-foreground/80 hover:bg-accent hover:text-foreground"
                    )}
                  >
                    <Icon className="size-4 shrink-0" />
                    <span className="truncate">{resolveNavLabel(it, admin)}</span>
                  </Link>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="border-t border-border p-3">
          <div className="mb-2 px-1 text-xs text-muted-foreground">
            Signed in as{" "}
            <span className="text-foreground">{admin?.full_name ?? "Admin"}</span>
            {" · "}
            <span className="text-primary">
              {admin?.role === "BROKER" && admin?.assigned_broker_id
                ? "SUB-BROKER"
                : admin?.role}
            </span>
          </div>
          <button
            type="button"
            onClick={() => logout().then(() => (window.location.href = "/login"))}
            className="tap-target flex w-full items-center gap-2 rounded-md border border-border px-3 py-2 text-sm text-foreground/80 hover:bg-accent hover:text-foreground"
          >
            <LogOut className="size-4" />
            Sign out
          </button>
        </div>
      </SheetContent>
    </Sheet>
  );
}
