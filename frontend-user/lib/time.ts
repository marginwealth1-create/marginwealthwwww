// Report periods as plain IST dates (YYYY-MM-DD) — the backend reads them as
// whole IST days. Computed off the IST wall clock, never the device timezone:
// a browser-local Monday is still Sunday's week before 05:30 IST.
const IST_OFFSET_MS = 5.5 * 60 * 60 * 1000;

export interface Period {
  label: string;
  from: string;
  to: string;
}

/** This week (Monday start) / Last week / This month / Last month. */
export function istPeriods(now: Date = new Date()): Period[] {
  const ist = new Date(now.getTime() + IST_OFFSET_MS); // read UTC parts = IST wall clock
  const [y, m, d] = [ist.getUTCFullYear(), ist.getUTCMonth(), ist.getUTCDate()];
  // Date.UTC rolls day 0 / negative days into the previous month.
  const day = (dd: number, mm = m) => new Date(Date.UTC(y, mm, dd)).toISOString().slice(0, 10);
  const monday = d - ((ist.getUTCDay() + 6) % 7);
  return [
    { label: "This week", from: day(monday), to: day(d) },
    { label: "Last week", from: day(monday - 7), to: day(monday - 1) },
    { label: "This month", from: day(1), to: day(d) },
    { label: "Last month", from: day(1, m - 1), to: day(0) },
  ];
}
