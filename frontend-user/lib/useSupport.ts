import { useMutation, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/lib/api";

export interface SupportContacts {
  whatsapp: string;
  email: string;
}

export interface UserTerms {
  text: string;
  enabled: boolean;
  needs_accept: boolean;
  accepted_at: string | null;
}

/** Resolved T&C for the logged-in user (cascades up the broker chain).
 *  Used by the Terms gate that runs once after register / on first
 *  dashboard hit if the admin enabled it and the user hasn't accepted
 *  yet (or the admin updated the text since the last accept). */
export function useUserTerms(enabled = true) {
  return useQuery<UserTerms>({
    queryKey: ["user", "terms"],
    queryFn: () => unwrap<UserTerms>(api.get("/user/support/terms")),
    enabled,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });
}

export function useAcceptTerms() {
  return useMutation({
    mutationFn: () => unwrap<any>(api.post("/user/support/terms/accept", {})),
  });
}

/** Admin-managed home-page ticker lines (cascades up the broker chain).
 *  Empty ⇒ the home ticker is hidden. Refetched periodically so a line the
 *  broker just added shows up without a reload. */
export function useHomeTicker() {
  return useQuery<{ messages: string[] }>({
    queryKey: ["user", "ticker"],
    queryFn: () => unwrap<{ messages: string[] }>(api.get("/user/support/ticker")),
    staleTime: 30_000,
    refetchInterval: 120_000,
    refetchOnMount: "always",
    retry: 1,
  });
}

/**
 * Admin-managed WhatsApp + support email. Pulled from `/user/support`
 * — backed by `platform.support_whatsapp` + `platform.support_email`
 * PlatformSetting rows that the admin can edit on /admin/settings/platform.
 *
 * No hardcoded fallback — admin is the single source of truth. If
 * admin clears a value, the corresponding UI affordance disappears
 * immediately. If the backend hasn't been restarted with the new
 * route, the query errors and the section hides — the fix is "restart
 * the backend so the seed inserts default support keys", not stale
 * defaults in the JS bundle.
 *
 * Generously cached because these change at most once a quarter.
 */
export function useSupportContacts() {
  return useQuery<SupportContacts>({
    queryKey: ["support", "contacts"],
    queryFn: () => unwrap<SupportContacts>(api.get("/user/support")),
    // Admins set/change their support WhatsApp actively, so don't cache it for
    // long — refetch on every page mount so the number a broker just saved is
    // visible to their users right away (was 10 min stale → looked "not set").
    staleTime: 30_000,
    gcTime: 5 * 60_000,
    retry: 1,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
  });
}

/** wa.me URL builder — strips formatting from the admin-stored number
 *  (spaces, dashes, leading "+") since wa.me expects digits only.
 *  Returns null for unusable input so callers can hide the link. */
export function buildWhatsappUrl(
  raw: string | undefined | null,
  prefill?: string,
): string | null {
  if (!raw) return null;
  const digits = String(raw).replace(/[^0-9]/g, "");
  if (digits.length < 8) return null;
  const text = prefill ? `?text=${encodeURIComponent(prefill)}` : "";
  return `https://wa.me/${digits}${text}`;
}

export function buildMailtoUrl(
  email: string | undefined | null,
  opts?: { subject?: string; body?: string },
): string | null {
  if (!email || !email.includes("@")) return null;
  const params: string[] = [];
  if (opts?.subject) params.push(`subject=${encodeURIComponent(opts.subject)}`);
  if (opts?.body) params.push(`body=${encodeURIComponent(opts.body)}`);
  const qs = params.length > 0 ? `?${params.join("&")}` : "";
  return `mailto:${email}${qs}`;
}
