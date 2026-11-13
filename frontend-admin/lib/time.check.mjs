// Self-check for lib/time.ts: `node lib/time.check.mjs` (Node >= 23; on 22 add
// --experimental-strip-types). Runs under several device timezones, because the
// bug it guards (§6.3) only showed on a machine whose clock is not UTC.
import assert from "node:assert/strict";
import { isoToLocalInput, localInputToIso } from "./time.ts";

const naive = "2026-11-12T04:30:15.123000"; // backend naive UTC = 10:00:15 IST
const aware = "2026-11-12T04:30:15.123000+00:00"; // tz_aware Mongo client

for (const tz of ["Asia/Kolkata", "UTC", "America/New_York"]) {
  process.env.TZ = tz;
  // Prefill is the IST wall clock whatever the device timezone.
  assert.equal(isoToLocalInput(naive), "2026-11-12T10:00", tz);
  assert.equal(isoToLocalInput(aware), "2026-11-12T10:00", tz);
  // Open-then-save is the identity (to the minute the input can hold).
  assert.equal(localInputToIso(isoToLocalInput(aware)), "2026-11-12T04:30:00.000Z", tz);
  // The positions dialog's untouched-field test: same instant, so not re-sent.
  const prefill = isoToLocalInput(aware);
  assert.equal(localInputToIso(prefill), localInputToIso(isoToLocalInput(naive)), tz);
  // An edit to 09:15 IST is sent as 03:45Z.
  assert.equal(localInputToIso("2026-11-12T09:15"), "2026-11-12T03:45:00.000Z", tz);
  // Day rollover across UTC midnight.
  assert.equal(isoToLocalInput("2026-11-11T20:00:00Z"), "2026-11-12T01:30", tz);
  assert.equal(isoToLocalInput(""), "", tz);
  assert.equal(localInputToIso(""), undefined, tz);
}
console.log("lib/time.ts ok");
