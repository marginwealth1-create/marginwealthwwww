// Self-check for lib/time.ts: `node lib/time.check.mjs` (Node >= 23; on 22 add
// --experimental-strip-types). Runs under several device timezones: the week
// must be the IST one whatever the browser's clock says.
import assert from "node:assert/strict";
import { istPeriods } from "./time.ts";

const ranges = (now) => istPeriods(new Date(now)).map((p) => [p.label, p.from, p.to]);

for (const tz of ["Asia/Kolkata", "UTC", "America/New_York"]) {
  process.env.TZ = tz;
  // Fri 13 Nov 2026, 11:30 IST.
  assert.deepEqual(ranges("2026-11-13T06:00:00Z"), [
    ["This week", "2026-11-09", "2026-11-13"],
    ["Last week", "2026-11-02", "2026-11-08"],
    ["This month", "2026-11-01", "2026-11-13"],
    ["Last month", "2026-10-01", "2026-10-31"],
  ], tz);
  // Mon 9 Nov 00:10 IST is Sunday 18:40 UTC: already the week of the 9th.
  assert.deepEqual(ranges("2026-11-08T18:40:00Z")[0], ["This week", "2026-11-09", "2026-11-09"], tz);
  // Sun 15 Nov 23:50 IST still closes that week.
  assert.deepEqual(ranges("2026-11-15T18:20:00Z")[0], ["This week", "2026-11-09", "2026-11-15"], tz);
  // Thu 1 Jan 2026, 03:00 IST (31 Dec UTC): the week spans the year end, last month is December.
  assert.deepEqual(ranges("2025-12-31T21:30:00Z"), [
    ["This week", "2025-12-29", "2026-01-01"],
    ["Last week", "2025-12-22", "2025-12-28"],
    ["This month", "2026-01-01", "2026-01-01"],
    ["Last month", "2025-12-01", "2025-12-31"],
  ], tz);
  // 1 Mar in a non-leap year: last month ends on the 28th.
  assert.deepEqual(ranges("2027-03-01T06:00:00Z")[3], ["Last month", "2027-02-01", "2027-02-28"], tz);
}
console.log("time.ts ok");
