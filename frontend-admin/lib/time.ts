// <input type="datetime-local"> holds a bare wall clock. This is an India-markets
// app, so every such input reads and writes IST explicitly, never the device
// timezone: prefilling a UTC wall clock and parsing it back as local moved a
// position's opened_at 5h30m earlier on every save (§6.3).
const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;

const pad = (n: number) => String(n).padStart(2, "0");

/** Backend serialises UTC, sometimes naive; add `Z` if missing before parsing. */
export function parseDate(v: string | Date | null | undefined): Date | null {
  if (!v) return null;
  if (v instanceof Date) return isNaN(v.getTime()) ? null : v;
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(v);
  const d = new Date(hasTz ? v : v + "Z");
  return isNaN(d.getTime()) ? null : d;
}

// UTC ISO → "YYYY-MM-DDThh:mm" wall-clock in IST for <input type="datetime-local">.
export function isoToLocalInput(iso?: string | Date | null): string {
  const d = parseDate(iso);
  if (!d) return "";
  const ist = new Date(d.getTime() + IST_OFFSET_MS); // read UTC parts = IST wall-clock
  return `${ist.getUTCFullYear()}-${pad(ist.getUTCMonth() + 1)}-${pad(ist.getUTCDate())}T${pad(
    ist.getUTCHours(),
  )}:${pad(ist.getUTCMinutes())}`;
}

// IST wall-clock from <input type="datetime-local"> → UTC ISO for the API.
export function localInputToIso(v: string): string | undefined {
  if (!v) return undefined;
  const withSecs = v.length === 16 ? `${v}:00` : v; // datetime-local omits seconds
  const d = new Date(`${withSecs}+05:30`); // interpret AS IST, not device-local
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}
