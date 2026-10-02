"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";
import { STORAGE_KEYS } from "@/lib/constants";
import { AuthAPI, clearTokens, setTokens } from "@/lib/api";
import { clearWalletSnapshot } from "@/lib/walletSnapshot";
import type { AuthUser, TokenPair } from "@/types";

interface AuthState {
  user: AuthUser | null;
  hydrated: boolean;
  loading: boolean;
  setSession: (pair: TokenPair) => void;
  setUser: (u: AuthUser | null) => void;
  setHydrated: (v: boolean) => void;
  login: (identifier: string, password: string, two_fa_code?: string) => Promise<TokenPair>;
  logout: () => Promise<void>;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      user: null,
      hydrated: false,
      loading: false,

      setSession: (pair) => {
        setTokens(pair.access_token, pair.refresh_token);
        set({ user: pair.user });
      },
      setUser: (u) => set({ user: u }),
      setHydrated: (v) => set({ hydrated: v }),

      login: async (identifier, password, two_fa_code) => {
        set({ loading: true });
        try {
          const pair = await AuthAPI.login({ identifier, password, two_fa_code });
          get().setSession(pair);
          return pair;
        } finally {
          set({ loading: false });
        }
      },

      logout: async () => {
        try {
          const refresh =
            typeof window !== "undefined"
              ? window.localStorage.getItem(STORAGE_KEYS.refreshToken) ?? undefined
              : undefined;
          await AuthAPI.logout(refresh);
        } catch {
          // ignore — we still clear local state
        } finally {
          clearTokens();
          // Wipe the wallet snapshot so the next person who logs in on this
          // device doesn't briefly see the previous user's balance while
          // their own /wallet/summary fetch is in flight.
          clearWalletSnapshot();
          // Also wipe the persisted React Query cache (key = RQ_CACHE_KEY in
          // app/providers.tsx) for the same reason — otherwise the next user
          // would briefly see the previous user's cached positions / orders /
          // portfolio restored from localStorage on first paint.
          try {
            if (typeof window !== "undefined")
              window.localStorage.removeItem("mp-rq-cache-v1");
          } catch {
            // ignore — best-effort cleanup
          }
          set({ user: null });
        }
      },
    }),
    {
      name: "nb.auth",
      storage: {
        getItem: (k) => {
          if (typeof window === "undefined") return null;
          const raw = window.localStorage.getItem(k);
          return raw ? JSON.parse(raw) : null;
        },
        setItem: (k, v) => {
          if (typeof window !== "undefined") window.localStorage.setItem(k, JSON.stringify(v));
        },
        removeItem: (k) => {
          if (typeof window !== "undefined") window.localStorage.removeItem(k);
        },
      },
      partialize: (s) => ({ user: s.user }) as AuthState,
      onRehydrateStorage: () => (s) => s?.setHydrated(true),
    }
  )
);
