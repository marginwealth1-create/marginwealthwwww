/**
 * Frontend market-hours guard — used so the UI doesn't fire close / new
 * orders when the backend will obviously reject them with MARKET_CLOSED.
 *
 * Why: previously, clicking "Close" on a position outside market hours
 * triggered the same optimistic-remove pipeline as any other close — the
 * row disappeared for ~1 s, then the backend rejected and the row came
 * back with a tiny error toast. Traders kept thinking the close worked
 * for a moment and then "got reversed." Pre-checking here means we show
 * one clear "Market is closed" message and the position stays put.
 *
 * Segment hours mirror the backend's `app/utils/time_utils.py` schedule.
 * If the backend ever queues after-hours closes as AMO orders, flip the
 * relevant branch to `true` here.
 */

/** Minutes since IST midnight for the given JS Date (no tz library needed). */
function _istMinutes(date: Date): number {
  // toLocaleString → "Asia/Kolkata" gives us H:M without DST headaches.
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const h = Number(parts.find((p) => p.type === "hour")?.value ?? "0");
  const m = Number(parts.find((p) => p.type === "minute")?.value ?? "0");
  return h * 60 + m;
}

/** IST day-of-week (0 = Sun, 6 = Sat). */
function _istDay(date: Date): number {
  // `getDay()` on a UTC date returns UTC weekday — for IST we shift the
  // epoch forward by 5h30m, then read it back as a UTC weekday. Avoids
  // tz-aware Date construction.
  const ist = new Date(date.getTime() + (5 * 60 + 30) * 60_000);
  return ist.getUTCDay();
}

/**
 * Returns true when the segment's exchange is currently accepting trades.
 *
 * `segment_type` is the canonical Position.segment_type value the backend
 * sends; `exchange` is a fallback when segment_type is empty (legacy
 * positions). Both are uppercased before matching.
 */
export function isInstrumentMarketOpen(
  segmentType?: string | null,
  exchange?: string | null,
  now: Date = new Date(),
): boolean {
  const seg = (segmentType || "").toUpperCase();
  const exch = (exchange || "").toUpperCase();
  const min = _istMinutes(now);
  const day = _istDay(now);
  const weekday = day !== 0 && day !== 6;

  // Crypto trades 24/7. AllTick/Infoway feed never closes, the matching
  // engine accepts orders at any time.
  if (seg.includes("CRYPTO") || exch === "CRYPTO" || exch === "BINANCE") return true;

  // International FX / spot metals (XAUUSD, XAGUSD…) / energy / global
  // equities / indices — all Infoway-mirrored and trade on the OTC forex
  // clock, which is anchored to New York time and DST-aware: the market
  // runs Sun 17:00 ET → Fri 17:00 ET with a daily 17:00–18:00 ET
  // maintenance break. We compute ET DIRECTLY (not a hard-coded IST
  // offset) so summer/winter DST never drifts. In IST this lands as
  // ~Mon 03:30 → Sat 02:30 with a ~02:30–03:30 daily break in summer,
  // shifting +1h in winter — handled automatically.
  //
  // Prior bug this replaces: the WHOLE of Saturday (IST) was treated as
  // closed from midnight, but Friday's session actually runs until
  // ~02:30 IST Saturday — so a trader holding XAUUSD at ~01:00 IST
  // Saturday could NOT close a live, in-profit position (the market was
  // still open). The ET clock fixes that exactly, and also stops the old
  // wrong "open" it showed on Sunday evenings IST.
  if (
    seg === "FOREX" ||
    seg === "STOCKS" ||
    seg === "INDICES" ||
    seg === "COMMODITIES" ||
    seg.includes("FOREX") ||
    seg.includes("FX") ||
    exch === "CDS"
  ) {
    const etParts = new Intl.DateTimeFormat("en-GB", {
      timeZone: "America/New_York",
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(now);
    const etDay = etParts.find((p) => p.type === "weekday")?.value ?? "";
    // en-GB can render midnight as "24" — normalise to 0 so 00:00–00:59 ET
    // isn't mis-bucketed.
    const etH = Number(etParts.find((p) => p.type === "hour")?.value ?? "0") % 24;
    const etM = Number(etParts.find((p) => p.type === "minute")?.value ?? "0");
    const etMin = etH * 60 + etM;
    const CLOSE = 17 * 60; // 17:00 ET — daily close + Friday weekly close
    const REOPEN = 18 * 60; // 18:00 ET — daily reopen (also Sunday open)
    if (etDay === "Sat") return false; // weekend — fully closed
    if (etDay === "Sun") return etMin >= CLOSE; // reopens Sun 17:00 ET
    if (etDay === "Fri") return etMin < CLOSE; // weekly close Fri 17:00 ET
    // Mon–Thu: open except the 17:00–18:00 ET daily maintenance break.
    return !(etMin >= CLOSE && etMin < REOPEN);
  }

  // MCX commodities: Mon-Fri 09:00-23:30 IST (evening session merged).
  if (seg.startsWith("MCX") || exch === "MCX") {
    if (!weekday) return false;
    return min >= 9 * 60 && min <= 23 * 60 + 30;
  }

  // NSE / BSE equity, F&O — Mon-Fri 09:15-15:30 IST.
  // Catch-all: anything we couldn't classify falls into this bucket,
  // which is safer than defaulting to "open" because Indian equity is
  // the dominant segment and a wrong "closed" is better than a wrong
  // "open" (the backend rejects either way; we just avoid the flicker).
  if (!weekday) return false;
  return min >= 9 * 60 + 15 && min <= 15 * 60 + 30;
}

/** Friendly label used in the "Market is closed" toast. */
export function marketLabel(segmentType?: string | null, exchange?: string | null): string {
  const seg = (segmentType || "").toUpperCase();
  const exch = (exchange || "").toUpperCase();
  if (seg.includes("CRYPTO") || exch === "CRYPTO") return "Crypto";
  if (seg === "FOREX" || seg.includes("FOREX") || seg.includes("FX") || exch === "CDS") return "Forex";
  if (seg === "COMMODITIES") return "Commodities";
  if (seg === "STOCKS") return "Global stocks";
  if (seg === "INDICES") return "Global indices";
  if (seg.startsWith("MCX") || exch === "MCX") return "MCX";
  if (seg.startsWith("BSE") || exch === "BSE") return "BSE";
  return "NSE";
}
