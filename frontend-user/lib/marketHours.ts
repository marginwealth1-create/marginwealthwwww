/**
 * Frontend market-hours guard — backed by the admin-configured Market Trading
 * Hours (`GET /api/v1/user/support/market-hours`) so any change made in
 * Admin → Platform Settings → Market Trading Hours is enforced immediately
 * when a user places or closes a trade.
 */

import { API_URL } from "@/lib/constants";

export type MarketKey = "NSE" | "BSE" | "MCX" | "FOREX" | "CRYPTO";

export interface MarketHourEntry {
  enabled: boolean;
  start: string;
  end: string;
  days: string;
}

export type MarketHoursMap = Record<MarketKey, MarketHourEntry>;

export const DEFAULT_MARKET_HOURS: MarketHoursMap = {
  NSE: {
    enabled: true,
    start: "09:15",
    end: "15:30",
    days: "Mon - Fri",
  },
  BSE: {
    enabled: true,
    start: "09:15",
    end: "15:30",
    days: "Mon - Fri",
  },
  MCX: {
    enabled: true,
    start: "09:00",
    end: "23:55",
    days: "Mon - Fri",
  },
  FOREX: {
    enabled: true,
    start: "00:00",
    end: "23:59",
    days: "Mon - Fri",
  },
  CRYPTO: {
    enabled: true,
    start: "00:00",
    end: "23:59",
    days: "24/7 (Mon - Sun)",
  },
};

const STORAGE_KEY = "mw.market_hours";
let _hoursCache: MarketHoursMap = { ...DEFAULT_MARKET_HOURS };
let _lastFetchMs = 0;
let _inFlight: Promise<MarketHoursMap> | null = null;
const _FETCH_INTERVAL_MS = 15_000;

function _mergeHours(raw: unknown): MarketHoursMap {
  const out: MarketHoursMap = {
    NSE: { ...DEFAULT_MARKET_HOURS.NSE },
    BSE: { ...DEFAULT_MARKET_HOURS.BSE },
    MCX: { ...DEFAULT_MARKET_HOURS.MCX },
    FOREX: { ...DEFAULT_MARKET_HOURS.FOREX },
    CRYPTO: { ...DEFAULT_MARKET_HOURS.CRYPTO },
  };
  if (!raw || typeof raw !== "object") return out;
  const r = raw as Record<string, any>;
  (Object.keys(out) as MarketKey[]).forEach((k) => {
    const item = r[k];
    if (item && typeof item === "object") {
      out[k] = {
        enabled: item.enabled !== undefined ? Boolean(item.enabled) : out[k].enabled,
        start: typeof item.start === "string" && item.start.includes(":") ? item.start : out[k].start,
        end: typeof item.end === "string" && item.end.includes(":") ? item.end : out[k].end,
        days: typeof item.days === "string" && item.days ? item.days : out[k].days,
      };
    }
  });
  return out;
}

if (typeof window !== "undefined") {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved) {
      _hoursCache = _mergeHours(JSON.parse(saved));
    }
  } catch {
    // ignore storage read errors
  }
}

export async function refreshMarketHours(force = false): Promise<MarketHoursMap> {
  if (typeof window === "undefined") return _hoursCache;
  const now = Date.now();
  if (!force && now - _lastFetchMs < _FETCH_INTERVAL_MS) {
    return _hoursCache;
  }
  if (_inFlight) return _inFlight;
  _inFlight = (async () => {
    try {
      const res = await fetch(`${API_URL}/api/v1/user/support/market-hours`, {
        method: "GET",
        cache: "no-store",
      });
      if (res.ok) {
        const json = await res.json();
        const raw = json?.data?.hours ?? json?.hours;
        if (raw) {
          _hoursCache = _mergeHours(raw);
          _lastFetchMs = Date.now();
          try {
            window.localStorage.setItem(STORAGE_KEY, JSON.stringify(_hoursCache));
          } catch {
            // ignore storage write errors
          }
        }
      }
    } catch {
      // keep existing cache on transient network error
    } finally {
      _inFlight = null;
    }
    return _hoursCache;
  })();
  return _inFlight;
}

if (typeof window !== "undefined") {
  void refreshMarketHours(true);
  window.addEventListener("focus", () => {
    void refreshMarketHours(true);
  });
  setInterval(() => {
    void refreshMarketHours(false);
  }, _FETCH_INTERVAL_MS);
}

/** Minutes since IST midnight for the given JS Date (no tz library needed). */
function _istMinutes(date: Date): number {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const h = Number(parts.find((p) => p.type === "hour")?.value ?? "0") % 24;
  const m = Number(parts.find((p) => p.type === "minute")?.value ?? "0");
  return h * 60 + m;
}

/** IST day-of-week (0 = Sun, 1 = Mon, ..., 5 = Fri, 6 = Sat). */
function _istDay(date: Date): number {
  const ist = new Date(date.getTime() + (5 * 60 + 30) * 60_000);
  return ist.getUTCDay();
}

function _parseHhMm(hhmm: string, fallbackMin: number): number {
  const parts = (hhmm || "").trim().split(":");
  if (parts.length < 2) return fallbackMin;
  const h = Number(parts[0]);
  const m = Number(parts[1]);
  if (!Number.isFinite(h) || !Number.isFinite(m)) return fallbackMin;
  return h * 60 + m;
}

export function resolveMarketKey(
  segmentType?: string | null,
  exchange?: string | null,
): MarketKey {
  const seg = (segmentType || "").toUpperCase();
  const exch = (exchange || "").toUpperCase();
  if (seg.includes("CRYPTO") || exch === "CRYPTO" || exch === "BINANCE") {
    return "CRYPTO";
  }
  if (
    seg === "FOREX" ||
    seg === "STOCKS" ||
    seg === "INDICES" ||
    seg === "COMMODITIES" ||
    seg.includes("FOREX") ||
    seg.includes("FX") ||
    seg.startsWith("CDS") ||
    exch === "CDS"
  ) {
    return "FOREX";
  }
  if (seg.startsWith("MCX") || exch === "MCX") {
    return "MCX";
  }
  if (seg.startsWith("BSE") || seg.startsWith("BFO") || exch === "BSE" || exch === "BFO") {
    return "BSE";
  }
  return "NSE";
}

export function getMarketHourConfig(
  segmentType?: string | null,
  exchange?: string | null,
): MarketHourEntry & { key: MarketKey } {
  if (typeof window !== "undefined" && Date.now() - _lastFetchMs >= _FETCH_INTERVAL_MS) {
    void refreshMarketHours(false);
  }
  const key = resolveMarketKey(segmentType, exchange);
  const cfg = _hoursCache[key] ?? DEFAULT_MARKET_HOURS[key];
  return { key, ...cfg };
}

/**
 * Returns true when the segment's exchange is currently accepting trades
 * according to the admin-configured Market Trading Hours (IST).
 */
export function isInstrumentMarketOpen(
  segmentType?: string | null,
  exchange?: string | null,
  now: Date = new Date(),
): boolean {
  const cfg = getMarketHourConfig(segmentType, exchange);
  if (!cfg.enabled) return false;

  const min = _istMinutes(now);
  const day = _istDay(now); // 0 = Sun, 1..5 = Mon..Fri, 6 = Sat
  const startMin = _parseHhMm(cfg.start, 0);
  const endMin = _parseHhMm(cfg.end, 23 * 60 + 59);
  const is24x7Days =
    cfg.key === "CRYPTO" || cfg.days.includes("24/7") || cfg.days.includes("Sun");

  if (startMin <= endMin) {
    if (!is24x7Days && (day === 0 || day === 6)) {
      return false;
    }
    return min >= startMin && min <= endMin;
  } else {
    // Overnight session crossing midnight (e.g. 03:30 -> 02:30 IST)
    if (is24x7Days) {
      return min >= startMin || min <= endMin;
    }
    // Mon-Fri overnight:
    // - Mon..Fri (1..5): open from startMin..23:59
    // - Tue..Sat (2..6): open from 00:00..endMin (tail of previous weekday)
    return (day >= 1 && day <= 5 && min >= startMin) || (day >= 2 && day <= 6 && min <= endMin);
  }
}

/** Friendly label used in the "Market is closed" alert/toast. */
export function marketLabel(segmentType?: string | null, exchange?: string | null): string {
  const seg = (segmentType || "").toUpperCase();
  const exch = (exchange || "").toUpperCase();
  if (seg.includes("CRYPTO") || exch === "CRYPTO") return "Crypto";
  if (seg === "FOREX" || seg.includes("FOREX") || seg.includes("FX") || exch === "CDS") return "Forex";
  if (seg === "COMMODITIES") return "Commodities";
  if (seg === "STOCKS") return "Global stocks";
  if (seg === "INDICES") return "Global indices";
  if (seg.startsWith("MCX") || exch === "MCX") return "MCX";
  if (seg.startsWith("BSE") || seg.startsWith("BFO") || exch === "BSE" || exch === "BFO") return "BSE";
  return "NSE";
}

/** Detailed alert message shown when an order is attempted while the market is closed. */
export function getMarketClosedMessage(
  segmentType?: string | null,
  exchange?: string | null,
): string {
  const label = marketLabel(segmentType, exchange);
  const cfg = getMarketHourConfig(segmentType, exchange);
  if (!cfg.enabled) {
    return `${label} market is currently closed by admin. Orders are not accepted right now.`;
  }
  return `${label} market is closed. Trading hours: ${cfg.start} – ${cfg.end} IST (${cfg.days}).`;
}
