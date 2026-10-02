"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Home,
  Users as UsersIcon,
  Banknote,
  Activity,
  Menu,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useAdminAuthStore } from "@/stores/authStore";
import { useMobileNav } from "@/components/layout/MobileNavContext";
import { canSee, isSuperAdmin } from "@/lib/permissions";

/**
 * Mobile bottom tab bar (md:hidden). Five slots:
 *   Dashboard · Users · Payments · Positions · More (opens drawer)
 *
 * Items are permission-aware: a tab whose perm is denied is replaced
 * with the next available item (Orders → Risk → Reports → KYC → Audit)
 * so the bar still has 5 useful entries for every role. "More" never
 * moves.
 */

type Slot = {
  href: string;
  label: string;
  icon: LucideIcon;
  // Visibility predicate — gets the auth store admin object.
  show: (admin: ReturnType<typeof useAdminAuthStore.getState>["admin"]) => boolean;
};

// Ordered preference list — first 4 that match `show()` become the
// quick tabs (slot 5 is always "More"). Mirrors the same permission
// helpers used by the sidebar so there's no drift.
const CANDIDATES: Slot[] = [
  { href: "/dashboard", label: "Home", icon: Home, show: () => true },
  { href: "/users", label: "Users", icon: UsersIcon, show: (a) => canSee(a, "users") },
  { href: "/payments", label: "Pay", icon: Banknote, show: (a) => canSee(a, "deposits") },
  { href: "/positions", label: "Positions", icon: Activity, show: (a) => canSee(a, "trading_view") },
  // Fallbacks if any of the above is hidden for this role.
  { href: "/orders", label: "Orders", icon: Activity, show: (a) => canSee(a, "trading_view") },
  { href: "/risk-management", label: "Risk", icon: UsersIcon, show: (a) => canSee(a, "risk") },
  { href: "/reports/users", label: "Reports", icon: UsersIcon, show: (a) => canSee(a, "reports") },
  { href: "/audit", label: "Audit", icon: UsersIcon, show: () => true },
  { href: "/management/sub-admins", label: "Admins", icon: UsersIcon, show: (a) => isSuperAdmin(a) },
];

export function AdminBottomBar() {
  const pathname = usePathname();
  const admin = useAdminAuthStore((s) => s.admin);
  const { setOpen } = useMobileNav();

  const tabs: Slot[] = [];
  for (const c of CANDIDATES) {
    if (tabs.length >= 4) break;
    if (c.show(admin) && !tabs.some((t) => t.href === c.href)) tabs.push(c);
  }

  return (
    <nav
      className={cn(
        "fixed inset-x-0 bottom-0 z-30 border-t border-border bg-card/95 backdrop-blur md:hidden",
        "safe-area-bottom"
      )}
      aria-label="Primary"
    >
      <ul className="grid grid-cols-5">
        {tabs.map((t) => {
          const active =
            pathname === t.href || pathname?.startsWith(t.href + "/");
          const Icon = t.icon;
          return (
            <li key={t.href}>
              <Link
                href={t.href}
                className={cn(
                  "tap-target flex flex-col items-center justify-center gap-0.5 py-1.5 text-[11px]",
                  active ? "text-primary" : "text-muted-foreground"
                )}
              >
                <Icon className={cn("size-5", active && "stroke-[2.4]")} />
                <span className="leading-none">{t.label}</span>
              </Link>
            </li>
          );
        })}
        <li>
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="tap-target flex w-full flex-col items-center justify-center gap-0.5 py-1.5 text-[11px] text-muted-foreground"
            aria-label="Open navigation menu"
          >
            <Menu className="size-5" />
            <span className="leading-none">More</span>
          </button>
        </li>
      </ul>
    </nav>
  );
}
