/**
 * Client-side lot-size resolver — deliberately a no-op pass-through.
 *
 * Source of truth for Indian F&O lot sizes is the **Zerodha instruments
 * CSV** (refreshed on every backend boot) for NSE / BSE, and the
 * `MCX_LOT_SIZES` canonical table in `backend/app/services/index_lots.py`
 * for MCX. The backend ships the resolved lot on each Instrument row
 * (`instrument.lot_size`) and on the effective-segment-settings response
 * (`effSettings.lot_size`). The frontend trusts that.
 *
 * Why this file used to hold a hardcoded table: it was the self-heal
 * shim used when stored DB rows had stale lots (NIFTY=50 etc.). After
 * the boot-time backfill + per-order CSV resync went in, that shim
 * actively MISLED the order panel — it would override the freshest
 * CSV value with a stale constant, producing the exact "1 lot = 75
 * units when Zerodha says 65" mismatch reported in the field.
 *
 * Keeping the export signature so callers don't have to change shape;
 * the function now always returns `null` so they fall back to the
 * backend-supplied lot size.
 */

export const INDEX_LOT_SIZES: ReadonlyArray<[string, number]> = [];

export function getIndexLotSize(
  ..._args: Array<string | null | undefined | { instrumentType?: string | null; segment?: string | null }>
): number | null {
  return null;
}
