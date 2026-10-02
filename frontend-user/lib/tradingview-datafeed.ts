/**
 * Custom TradingView datafeed adapter.
 *
 * Price source rules (so the chart and the order panel never disagree):
 *  - Crypto  → Binance klines for history, live quote bid/ask mid for the
 *              streaming bar. AllTick (the quote feed) mirrors Binance
 *              closely, so historical Binance bars line up with the live
 *              bid/ask the order panel shows.
 *  - Non-crypto → backend `InstrumentAPI.history` for history, live quote
 *              bid/ask mid for the streaming bar.
 *  - In both modes the streaming bar uses (bid + ask) / 2 from the same
 *    quote endpoint the OrderPanel consumes — so the chart's last candle
 *    always sits between the SELL and BUY prices.
 */
import { InstrumentAPI } from "./api";

/* ── Resolution mapping ──────────────────────────────────────────── */
const RESOLUTION_MAP: Record<string, string> = {
  "1": "minute",
  "3": "3minute",
  "5": "5minute",
  "15": "15minute",
  "30": "30minute",
  "60": "60minute",
  "1D": "day",
  "1W": "day",
  "1M": "day",
};

const DAYS_FOR_RESOLUTION: Record<string, number> = {
  "1": 2,
  "3": 5,
  "5": 5,
  "15": 15,
  "30": 30,
  "60": 60,
  "1D": 365,
  "1W": 365,
  "1M": 365,
};

const RESOLUTION_TO_BINANCE: Record<string, string> = {
  "1": "1m",
  "3": "3m",
  "5": "5m",
  "15": "15m",
  "30": "30m",
  "60": "1h",
  "240": "4h",
  "1D": "1d",
  "1W": "1w",
  "1M": "1M",
};

const RESOLUTION_TO_SECONDS: Record<string, number> = {
  "1": 60,
  "3": 180,
  "5": 300,
  "15": 900,
  "30": 1800,
  "60": 3600,
  "240": 14400,
  "1D": 86400,
  "1W": 604800,
  "1M": 2592000,
};

/* ── Crypto detection ───────────────────────────────────────────────
 * The chart receives the instrument's `token` as its symbol. For our
 * crypto instruments that token IS the Binance pair (BTCUSDT, ETHUSDT
 * etc.), so we can hand it straight to /api/v3/klines.
 */
const KNOWN_BINANCE_PAIRS = new Set([
  "BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT", "SOLUSDT",
  "BNBUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT", "LINKUSDT",
  "DOTUSDT", "AVAXUSDT", "MATICUSDT", "ATOMUSDT", "NEARUSDT",
  "ARBUSDT", "OPUSDT", "APTUSDT", "SUIUSDT", "PEPEUSDT",
  "SHIBUSDT", "BCHUSDT", "FILUSDT", "TONUSDT", "INJUSDT",
]);

function isCryptoSymbol(token: string, meta?: SymbolMeta): boolean {
  const t = (token || "").toUpperCase();
  if (KNOWN_BINANCE_PAIRS.has(t)) return true;
  const seg = (meta as any)?.segment?.toString().toUpperCase() ?? "";
  const exch = meta?.exchange?.toUpperCase() ?? "";
  // Metals (XAU/XAG/XPT/XPD), energy (USOIL/UKOIL/NATGAS) and most forex
  // pairs end in USD but are NOT crypto — Binance doesn't serve them and
  // the fetch fails with a CORS-y 400. Exclude these explicitly so the
  // chart falls through to the backend history endpoint instead.
  if (/^(XAU|XAG|XPT|XPD)/.test(t)) return false;
  if (/^(USOIL|UKOIL|NATGAS|BRENT|XBR|XTI|XNG)/.test(t)) return false;
  if (seg.includes("FOREX") || seg.includes("COMMODITIES") || seg.includes("ENERGY")) return false;
  if (t.endsWith("USDT")) return true;
  return seg.includes("CRYPTO") || exch === "CRYPTO" || exch === "BINANCE";
}

/** Forex, metals, energy, commodities — trade extended / nearly 24h sessions. */
function isExtendedHoursSymbol(token: string, meta?: SymbolMeta): boolean {
  const t = (token || "").toUpperCase();
  if (/^(XAU|XAG|XPT|XPD)/.test(t)) return true;
  if (/^(USOIL|UKOIL|NATGAS|BRENT|XBR|XTI|XNG)/.test(t)) return true;
  const seg = (meta as any)?.segment?.toString().toUpperCase() ?? "";
  if (seg.includes("FOREX") || seg.includes("COMMODITIES") || seg.includes("ENERGY")) return true;
  // Common forex pairs
  if (/USD|EUR|GBP|JPY|AUD|NZD|CAD|CHF/.test(t) && t.length <= 8 && !t.endsWith("USDT")) return true;
  return false;
}

function toBinancePair(token: string): string {
  const t = (token || "").toUpperCase();
  if (KNOWN_BINANCE_PAIRS.has(t)) return t;
  // BTCUSD → BTCUSDT (the form AllTick uses sometimes)
  if (t.endsWith("USD") && !t.endsWith("USDT")) return `${t}T`;
  return t;
}

/* ── Types ───────────────────────────────────────────────────────── */
interface SymbolMeta {
  token: string;
  symbol: string;
  name: string;
  exchange: string;
  tick_size: string;
  lot_size: number;
  instrument_type: string;
  segment?: string;
}

interface Subscriber {
  symbolInfo: any;
  resolution: string;
  token: string;
  barSec: number;
  onTick: (bar: any) => void;
  timer: ReturnType<typeof setInterval> | null;
  lastBar: any | null;
}

/* ── Live tick fan-out ───────────────────────────────────────────────
 * Every active subscriber, module-level so `pushLiveQuote` (called by the
 * chart component on EVERY WebSocket tick) can find the right one and grow
 * its candle synchronously. This is what makes the bar move per-tick
 * (sub-second), the smooth Upstox/Zerodha feel — the old code only sampled
 * the price on a 1 s timer, so the candle moved in visible 1 Hz steps.
 */
const _activeSubs = new Set<Subscriber>();

function priceFromQuote(ltp: number, bid: number, ask: number): number {
  if (Number.isFinite(bid) && Number.isFinite(ask) && bid > 0 && ask > 0) return (bid + ask) / 2;
  if (Number.isFinite(ltp) && ltp > 0) return ltp;
  return NaN;
}

function applyTickToBar(sub: Subscriber, price: number) {
  if (!(price > 0)) return;
  const now = Math.floor(Date.now() / 1000);
  const barTime = Math.floor(now / sub.barSec) * sub.barSec * 1000;
  if (sub.lastBar && sub.lastBar.time === barTime) {
    sub.lastBar = {
      ...sub.lastBar,
      high: Math.max(sub.lastBar.high, price),
      low: Math.min(sub.lastBar.low, price),
      close: price,
    };
  } else if (!sub.lastBar || barTime > sub.lastBar.time) {
    // Roll a fresh bar at the new bucket (or bootstrap the very first one).
    sub.lastBar = { time: barTime, open: price, high: price, low: price, close: price, volume: 0 };
  } else {
    return; // tick older than the current bar — ignore
  }
  try {
    sub.onTick(sub.lastBar);
  } catch {
    /* TradingView teardown race — ignore */
  }
}

/* ── Binance history fetch ──────────────────────────────────────── */
const _binanceCache = new Map<string, { bars: any[]; ts: number }>();

/* ── Backend history fetch cache ────────────────────────────────────
 * Keyed by `${token}:${interval}:${days}`, 30 s TTL. Without this, every
 * chart swap (or resolution change, or in-place setSymbol) re-fetches
 * candles from the backend — which on a cold backend cache hits Zerodha
 * for another 200-800 ms. With the cache, switching back to a recently-
 * viewed instrument paints instantly from the previous response.
 */
const _backendHistoryCache = new Map<string, { bars: any[]; ts: number }>();
const BACKEND_HISTORY_TTL_MS = 30_000;

/* ── Symbol metadata cache (cross-widget) ───────────────────────────
 * Module-level so a symbol swap — or a widget rebuild on theme flip —
 * reuses the resolved instrument detail instead of re-hitting
 * InstrumentAPI.detail(), which was a serial network blocker before any
 * candle could load. Instant repeat resolves = faster chart loads.
 */
const _symbolDetailCache = new Map<string, SymbolMeta>();

async function fetchBinanceKlines(
  pair: string,
  resolution: string,
  from: number,
  to: number
): Promise<any[]> {
  const interval = RESOLUTION_TO_BINANCE[resolution] || "5m";
  const cacheKey = `${pair}:${interval}`;

  const cached = _binanceCache.get(cacheKey);
  if (cached && Date.now() - cached.ts < 30_000) {
    return cached.bars.filter((b) => b.time >= from * 1000 && b.time <= to * 1000);
  }

  try {
    const params = new URLSearchParams({
      symbol: pair,
      interval,
      startTime: String(from * 1000),
      endTime: String(to * 1000),
      limit: "1000",
    });
    const resp = await fetch(`https://api.binance.com/api/v3/klines?${params}`);
    if (!resp.ok) return [];
    const data = (await resp.json()) as number[][];
    const bars = data.map((k) => ({
      time: Number(k[0]),
      open: Number(k[1]),
      high: Number(k[2]),
      low: Number(k[3]),
      close: Number(k[4]),
      volume: Number(k[5]),
    }));
    _binanceCache.set(cacheKey, { bars, ts: Date.now() });
    return bars;
  } catch {
    return [];
  }
}

/* ── Synthetic bar fallback ─────────────────────────────────────────
 * Used when neither Binance nor the backend has history for this symbol
 * (e.g. weekly NIFTY options — Zerodha doesn't keep their OHLC). We
 * anchor the candles to the live quote so the chart's last bar matches
 * the BUY/SELL price the user sees in the order panel.
 */
function seededRand(seed: number) {
  let s = Math.abs(seed) % 2147483647;
  if (s === 0) s = 1;
  return () => {
    s = (s * 16807) % 2147483647;
    return (s - 1) / 2147483646;
  };
}

function generateSyntheticBars(
  symbol: string,
  mid: number,
  spread: number,
  resolution: string,
  from: number,
  to: number
): any[] {
  if (mid <= 0) return [];
  const resSec = RESOLUTION_TO_SECONDS[resolution] ?? 300;
  // Volatility tuned per symbol category — options move a lot more in
  // percentage terms than indices/equities, so give them more wiggle.
  const isOption = /CE$|PE$/i.test(symbol);
  const volPct = isOption ? 0.008 : 0.0005;
  const resFactor = Math.sqrt(resSec / 300);
  const volatility = Math.max(spread * 1.5, mid * volPct * resFactor);

  const nowSec = Math.floor(Date.now() / 1000);
  const toSec = Math.min(to, nowSec);
  const fromAligned = Math.floor(from / resSec) * resSec;
  const toAligned = Math.floor(toSec / resSec) * resSec;
  if (fromAligned >= toAligned) return [];

  const count = Math.min(Math.floor((toAligned - fromAligned) / resSec) + 1, 500);
  const startSec = toAligned - (count - 1) * resSec;

  const seed = symbol.split("").reduce((a, c) => a + c.charCodeAt(0), 0) + Math.floor(startSec / 86400);
  const rand = seededRand(seed);

  const increments = Array.from({ length: count }, () => (rand() - 0.5) * volatility * 2);
  let cumSum = 0;
  const cumSums = increments.map((inc) => {
    cumSum += inc;
    return cumSum;
  });
  const lastCum = cumSums[cumSums.length - 1];
  // Anchor the last bar's close to `mid` so the chart sits exactly on
  // the live quote when subscribeBars takes over.
  const prices = cumSums.map((c) => Math.max(0.01, mid + (c - lastCum)));

  const bars: any[] = [];
  let prev = Math.max(0.01, mid - (cumSums[0] - lastCum));
  for (let i = 0; i < count; i++) {
    const open = prev;
    const close = prices[i];
    bars.push({
      time: (startSec + i * resSec) * 1000,
      open,
      close,
      high: Math.max(open, close) + Math.abs(rand() * volatility * 0.4),
      low: Math.max(0.01, Math.min(open, close) - Math.abs(rand() * volatility * 0.4)),
      volume: Math.floor(rand() * 500) + 50,
    });
    prev = close;
  }
  return bars;
}

/* ── Live quote cache (WebSocket → chart bridge) ─────────────────────
 * The terminal's `useMarketStream` hook delivers sub-second tick data
 * via /ws/marketdata, but the datafeed's subscribeBars runs in a
 * separate closure and can't read React state. This module-level Map
 * bridges the gap: the chart component calls `pushLiveQuote()` on
 * every WebSocket tick; subscribeBars reads the cache instead of
 * polling REST. Result: chart price = order panel price always.
 */
const _liveQuoteCache = new Map<string, { ltp: number; bid: number; ask: number; ts: number }>();

export function pushLiveQuote(token: string, ltp: number, bid: number, ask: number) {
  if (!token) return;
  const t = String(token);
  _liveQuoteCache.set(t, { ltp, bid, ask, ts: Date.now() });
  const price = priceFromQuote(ltp, bid, ask);
  if (!(price > 0)) return;
  // Push the tick into every subscriber on this token RIGHT NOW so the
  // candle grows on every WS tick — no waiting on the fallback poll.
  for (const sub of _activeSubs) {
    if (sub.token === t) applyTickToBar(sub, price);
  }
}

/* ── Datafeed class ──────────────────────────────────────────────── */
export class CustomDatafeed {
  private subscribers: Map<string, Subscriber> = new Map();
  private symbolCache: Map<string, SymbolMeta> = new Map();
  private lastHistoryBar: Map<string, any> = new Map();

  onReady(callback: (config: any) => void) {
    setTimeout(() => {
      callback({
        supported_resolutions: ["1", "3", "5", "15", "30", "60", "1D", "1W", "1M"],
        supports_marks: false,
        supports_timescale_marks: false,
        supports_time: true,
      });
    }, 0);
  }

  async searchSymbols(
    userInput: string,
    _exchange: string,
    _symbolType: string,
    onResult: (items: any[]) => void
  ) {
    try {
      const instruments = await InstrumentAPI.search(userInput);
      const items = (instruments ?? []).map((i: any) => ({
        symbol: `${i.token}`,
        full_name: `${i.exchange}:${i.symbol}`,
        description: i.name || i.symbol,
        exchange: i.exchange,
        ticker: `${i.token}`,
        type: i.instrument_type === "EQ" ? "stock" : "futures",
      }));
      onResult(items);
    } catch {
      onResult([]);
    }
  }

  async resolveSymbol(
    symbolName: string,
    onResolve: (info: any) => void,
    onError: (reason: string) => void
  ) {
    try {
      const token = symbolName.split(":").pop() || symbolName;
      // Serve symbol metadata from cache when we've seen this token before —
      // a symbol swap then skips the InstrumentAPI.detail() round-trip
      // entirely, so the chart resolves + paints noticeably faster on every
      // instrument click (the detail fetch was a serial blocker before bars).
      let inst = this.symbolCache.get(token) ?? _symbolDetailCache.get(token);
      if (!inst) {
        inst = await InstrumentAPI.detail(token);
        if (!inst) {
          onError("Symbol not found");
          return;
        }
        _symbolDetailCache.set(token, inst);
      }
      this.symbolCache.set(token, inst);
      const tickSize = parseFloat(inst.tick_size) || 0.05;
      // TradingView's pricescale is "price units per whole" and must be a
      // POWER OF TEN — it decides how many decimals the chart renders. The
      // old `Math.round(1 / tickSize)` gave 20 for a 0.05 tick, which quantises
      // every price to 0.05 steps: DOGE at 0.0812 and POL at 0.0906 collapsed
      // onto the same two levels and the candles rendered as flat blocks.
      // Round UP to the next power of ten so a 0.05 tick renders 2 decimals
      // and a 0.0001 tick renders 4.
      const rawScale = tickSize > 0 ? 1 / tickSize : 20;
      const pricescale = Math.pow(
        10,
        Math.min(8, Math.max(0, Math.ceil(Math.log10(rawScale)))),
      );
      const crypto = isCryptoSymbol(token, inst);
      const extended = isExtendedHoursSymbol(token, inst);
      const is24h = crypto || extended;
      // Indian exchange session strings — include ":23456" (Mon-Fri only) so
      // TradingView doesn't render weekend gaps on the chart. Without the day
      // suffix the library treats every day as a trading day, leaving two
      // large blank spaces (Sat + Sun) between every Friday close and Monday
      // open, which makes the chart look broken ("bahut gaffe").
      // MCX metals/energy trade till 11:30 PM IST — use extended MCX session.
      const exch = (inst.exchange || "").toUpperCase();
      const isMCX = exch === "MCX";
      const indianSession = isMCX ? "0900-2330:23456" : "0915-1530:23456";
      onResolve({
        name: inst.symbol,
        description: inst.name || inst.symbol,
        type: crypto ? "crypto" : inst.instrument_type === "EQ" ? "stock" : "futures",
        session: is24h ? "24x7" : indianSession,
        timezone: is24h ? "Etc/UTC" : "Asia/Kolkata",
        ticker: token,
        exchange: inst.exchange,
        listed_exchange: inst.exchange,
        minmov: 1,
        pricescale: pricescale > 0 ? pricescale : 20,
        has_intraday: true,
        has_daily: true,
        has_weekly_and_monthly: true,
        supported_resolutions: ["1", "3", "5", "15", "30", "60", "1D", "1W", "1M"],
        volume_precision: crypto ? 4 : 0,
        data_status: "streaming",
      });
    } catch (err: any) {
      onError(err?.message || "Error resolving symbol");
    }
  }

  async getBars(
    symbolInfo: any,
    resolution: string,
    periodParams: any,
    onResult: (bars: any[], meta: any) => void,
    onError: (reason: string) => void
  ) {
    try {
      const token = symbolInfo.ticker || symbolInfo.name;
      const meta = this.symbolCache.get(token);
      const crypto = isCryptoSymbol(token, meta);

      // Crypto history is served by the backend (Infoway feed) — do NOT call
      // Binance directly because their API blocks cross-origin requests from
      // production domains (CORS). Fall through to the backend history path.

      const interval = RESOLUTION_MAP[resolution] || "5minute";
      // Compute the lookback so it covers TradingView's requested window.
      // TradingView paginates backward by calling getBars repeatedly with
      // older `from` timestamps; if we only request the default 5 days, the
      // older pages come back empty and the chart paints blank. Pad +2 days
      // so the window's left edge isn't a hard cut against a weekend.
      const nowSec = Math.floor(Date.now() / 1000);
      const fromSec = periodParams.from || nowSec - 5 * 86400;
      const dynamicDays = Math.max(
        DAYS_FOR_RESOLUTION[resolution] || 5,
        Math.ceil((nowSec - fromSec) / 86400) + 2,
      );

      let candles: any[] = [];
      const histKey = `${token}:${interval}:${dynamicDays}`;
      const cached = _backendHistoryCache.get(histKey);
      if (cached && Date.now() - cached.ts < BACKEND_HISTORY_TTL_MS) {
        candles = cached.bars;
      } else {
        try {
          candles = (await InstrumentAPI.history(token, interval, dynamicDays)) ?? [];
          _backendHistoryCache.set(histKey, { bars: candles, ts: Date.now() });
        } catch {
          candles = [];
        }
      }

      // Map to TradingView's bar shape and sort. NO client-side `from`
      // filter — TradingView handles its own viewport clipping, and our
      // earlier filter was throwing away the whole result when the backend
      // returned bars older than `periodParams.from` on the first paint
      // (the case that left option charts blank).
      const bars = candles
        .map((c: any) => ({
          time: c.time * 1000,
          open: c.open,
          high: c.high,
          low: c.low,
          close: c.close,
          volume: c.volume || 0,
        }))
        .filter((b: any) =>
          Number.isFinite(b.time) && Number.isFinite(b.open) &&
          Number.isFinite(b.close) && b.open > 0 && b.close > 0
        )
        .sort((a: any, b: any) => a.time - b.time);

      const fromMs = (periodParams.from || 0) * 1000;
      const toMs = (periodParams.to || Math.floor(Date.now() / 1000)) * 1000;

      if (bars.length > 0) {
        if (periodParams.firstDataRequest) {
          // First paint: return ALL bars regardless of the requested window.
          // TradingView clips the viewport itself. This is the fix for
          // options that only started trading today — they have valid bars
          // but only from today, so the range filter used to remove them all
          // and hand the chart a blank synthetic series instead.
          this.lastHistoryBar.set(token, bars[bars.length - 1]);
          onResult(bars, { noData: false });
          return;
        }
        // Pagination backward (firstDataRequest=false): return only bars
        // within the requested window. If all bars are newer than `to`
        // (TradingView scrolled back further than our history), signal
        // noData:true so TradingView stops paginating — without this it
        // loops endlessly until the synthetic fallback fires.
        const inRange = bars.filter((b: any) => b.time >= fromMs && b.time <= toMs);
        if (inRange.length > 0) {
          this.lastHistoryBar.set(token, inRange[inRange.length - 1]);
          onResult(inRange, { noData: false });
          return;
        }
        onResult([], { noData: true });
        return;
      }

      // ── Fallback: synthetic candles anchored to the live quote ─────
      // Only reached when the backend returns zero bars (Zerodha not
      // connected, very new contract with no history yet, or API error).
      // Anchoring to the bid/ask mid means the chart's last candle
      // matches the BUY/SELL price the order panel shows.
      try {
        const from = periodParams.from || Math.floor(Date.now() / 1000) - 30 * 86400;
        const to = periodParams.to || Math.floor(Date.now() / 1000);
        const q = await InstrumentAPI.quote(token);
        const bid = Number(q?.bid ?? q?.ltp ?? NaN);
        const ask = Number(q?.ask ?? q?.ltp ?? NaN);
        const ltp = Number(q?.ltp ?? NaN);
        let mid = NaN;
        let spread = 0;
        if (Number.isFinite(bid) && Number.isFinite(ask) && bid > 0 && ask > 0) {
          mid = (bid + ask) / 2;
          spread = Math.abs(ask - bid);
        } else if (Number.isFinite(ltp) && ltp > 0) {
          mid = ltp;
          spread = ltp * 0.0005;
        }
        if (mid > 0) {
          const synth = generateSyntheticBars(token, mid, spread, resolution, from, to);
          if (synth.length > 0) {
            this.lastHistoryBar.set(token, synth[synth.length - 1]);
            onResult(synth, { noData: false });
            return;
          }
        }
      } catch {
        // ignore — fall through to noData
      }

      onResult([], { noData: true });
    } catch (err: any) {
      onError(err?.message || "Error loading bars");
    }
  }

  subscribeBars(
    symbolInfo: any,
    resolution: string,
    onTick: (bar: any) => void,
    listenerGuid: string
  ) {
    const token = symbolInfo.ticker || symbolInfo.name;
    const barSec = RESOLUTION_TO_SECONDS[resolution] ?? 300;
    const seedBar = this.lastHistoryBar.get(token) ?? null;

    // Register subscriber immediately (timer=null until gap-fill done).
    // Also add it to the module-level fan-out set so pushLiveQuote can grow
    // its candle on every WebSocket tick (the smooth per-tick movement).
    const subscriber: Subscriber = {
      symbolInfo,
      resolution,
      token,
      barSec,
      onTick,
      timer: null,
      lastBar: seedBar ? { ...seedBar } : null,
    };
    this.subscribers.set(listenerGuid, subscriber);
    _activeSubs.add(subscriber);

    // Gap-fill first, then start live interval — so bars arrive in
    // chronological order. If gap bars were pushed AFTER the first live
    // tick, TradingView would discard the out-of-order historical bars
    // and the blank column would remain.
    void (async () => {
      // ── 1. Fill the gap between last history bar and now ──────────
      const sub = this.subscribers.get(listenerGuid);
      if (sub && seedBar) {
        const nowSec = Math.floor(Date.now() / 1000);
        const lastSec = seedBar.time / 1000;
        if (nowSec - lastSec >= barSec * 1.5) {
          try {
            const interval = RESOLUTION_MAP[resolution] || "5minute";
            const gapDays = Math.min(Math.ceil((nowSec - lastSec) / 86400) + 1, 5);
            const recent = (await InstrumentAPI.history(token, interval, gapDays)) ?? [];
            const gapBars = recent
              .map((c: any) => ({
                time: c.time * 1000,
                open: c.open,
                high: c.high,
                low: c.low,
                close: c.close,
                volume: c.volume || 0,
              }))
              .filter(
                (b: any) =>
                  b.time > seedBar.time &&
                  Number.isFinite(b.open) && b.open > 0 &&
                  Number.isFinite(b.close) && b.close > 0,
              )
              .sort((a: any, b: any) => a.time - b.time);
            for (const bar of gapBars) {
              // Re-read sub each iteration in case it was removed
              const s = this.subscribers.get(listenerGuid);
              if (!s) break;
              s.lastBar = bar;
              onTick(bar);
            }
          } catch {
            // ignore — live bar will appear on next interval tick
          }
        }
      }

      // ── 2. Start the REST fallback poll AFTER gap-fill completes ──────
      // The smooth per-tick updates now come from pushLiveQuote() (WS) →
      // applyTickToBar(), so this timer is only a SAFETY NET for instruments
      // whose WS stream is quiet (no recent pushLiveQuote). It runs slower
      // (2.5 s) and skips entirely while the WS is delivering fresh ticks, so
      // it never fights the live stream or doubles the work.
      if (!this.subscribers.has(listenerGuid)) return; // removed during gap-fill

      const timer = setInterval(async () => {
        try {
          const sub = this.subscribers.get(listenerGuid);
          if (!sub) return;

          // WS is actively driving this token (a tick landed < 5 s ago) →
          // pushLiveQuote already moved the candle. Nothing to do.
          const cached = _liveQuoteCache.get(token);
          if (cached && Date.now() - cached.ts < 5000) return;

          // No fresh WS tick — pull the price over REST and apply it.
          const q = await InstrumentAPI.quote(token);
          if (!q) return;
          const price = priceFromQuote(
            Number(q.ltp ?? NaN),
            Number(q.bid ?? NaN),
            Number(q.ask ?? NaN),
          );
          applyTickToBar(sub, price);
        } catch {
          // ignore polling errors
        }
      }, 2500);

      const s = this.subscribers.get(listenerGuid);
      if (s) s.timer = timer;
    })();
  }

  unsubscribeBars(listenerGuid: string) {
    const sub = this.subscribers.get(listenerGuid);
    if (sub?.timer) clearInterval(sub.timer);
    if (sub) _activeSubs.delete(sub);
    this.subscribers.delete(listenerGuid);
  }
}
