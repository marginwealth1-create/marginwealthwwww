"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { LedgerAPI } from "@/lib/api";
import { PageHeader } from "@/components/common/PageHeader";
import { Pagination } from "@/components/common/Pagination";
import { cn, formatINR } from "@/lib/utils";

type LedgerRow = {
  id: string;
  date: string;
  type: string;
  label: string;
  is_settlement: boolean;
  particulars: string;
  debit: number;
  credit: number;
  balance: number;
  reference_type?: string | null;
  reference_id?: string | null;
};

// The user ledger is deliberately restricted to real cash movements the user
// made — Deposit, Withdrawal, and Settlement (booked + recovered). Everything
// else (Trade, Brokerage / Charges, Realised P&L, P&L-sharing, Adjustment,
// Bonus, …) is hidden per the operator: the ledger is a money-in / money-out /
// settlement record, NOT a trade or P&L statement. The summary tiles (opening /
// closing / net change) were removed for the same reason — they mixed in P&L.
const VISIBLE_TYPES = new Set<string>([
  "DEPOSIT",
  "WITHDRAWAL",
  "SETTLEMENT_OUTSTANDING_BOOKED",
  "SETTLEMENT_OUTSTANDING_RECOVERY",
  // Admin manual wallet changes — the backend relabels these to
  // "Deposit by admin" / "Withdrawal by admin" by direction.
  "ADJUSTMENT",
  "BONUS",
  "PENALTY",
  "PROMO",
]);

export default function UserLedgerPage() {
  // Last 30 days. Backend sorts ascending then limits, so pass an explicit
  // 1-month window (a bare call returns only the OLDEST rows).
  const range = useMemo(() => {
    const now = new Date();
    const start = new Date(now);
    start.setMonth(start.getMonth() - 1);
    return { from_date: start.toISOString(), to_date: now.toISOString() };
  }, []);
  // Ask the backend for ONLY the visible types so a busy account's trade /
  // brokerage / P&L rows can't push a deposit past the row limit. The
  // client-side filter below stays as a belt-and-braces guard.
  const typesParam = useMemo(() => [...VISIBLE_TYPES].join(","), []);
  const { data, isFetching } = useQuery({
    queryKey: ["ledger", range, typesParam],
    queryFn: () => LedgerAPI.list({ ...range, limit: 1000, types: typesParam }),
  });
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

  const allRows = (data?.rows ?? []) as LedgerRow[];
  // Keep only Deposit / Withdrawal / Settlement, newest first.
  const visibleRows = useMemo(
    () =>
      allRows
        .filter((r) => VISIBLE_TYPES.has(r.type))
        .sort((a, b) => new Date(b.date).getTime() - new Date(a.date).getTime()),
    [allRows],
  );
  const pagedRows = useMemo(() => {
    const start = (page - 1) * pageSize;
    return visibleRows.slice(start, start + pageSize);
  }, [visibleRows, page, pageSize]);

  return (
    <div className="space-y-4">
      <PageHeader
        back
        title="Ledger"
        description={`${visibleRows.length} ${visibleRows.length === 1 ? "entry" : "entries"} — deposits, withdrawals & settlements`}
      />

      {isFetching && !data ? (
        <div className="rounded-lg border border-border p-8 text-center text-xs text-muted-foreground">
          Loading…
        </div>
      ) : pagedRows.length === 0 ? (
        <div className="rounded-lg border border-border p-8 text-center text-xs text-muted-foreground">
          No deposits, withdrawals or settlements yet.
        </div>
      ) : (
        <>
          {/* Mobile (< md): stacked cards. */}
          <div className="space-y-2 md:hidden">
            {pagedRows.map((r) => (
              <LedgerCardMobile key={r.id} row={r} />
            ))}
          </div>

          {/* Desktop (md+): DATE · TYPE · DETAIL · AMOUNT. */}
          <div className="hidden overflow-x-auto rounded-lg border border-border md:block">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-[11px] uppercase tracking-wider text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 text-left font-semibold">Date</th>
                  <th className="px-3 py-2 text-left font-semibold">Type</th>
                  <th className="px-3 py-2 text-left font-semibold">Detail</th>
                  <th className="px-3 py-2 text-right font-semibold">Amount</th>
                </tr>
              </thead>
              <tbody>
                {pagedRows.map((r) => (
                  <LedgerRowView key={r.id} row={r} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <Pagination
        page={page}
        pageSize={pageSize}
        total={visibleRows.length}
        onPageChange={setPage}
        onPageSizeChange={setPageSize}
        pageSizeOptions={[25, 50, 100, 200]}
      />
    </div>
  );
}

function signedAmount(row: LedgerRow) {
  const isDebit = row.debit > 0;
  const amount = isDebit ? row.debit : row.credit;
  return { isDebit, amount, hasAmount: amount > 0 };
}

function LedgerRowView({ row }: { row: LedgerRow }) {
  const isSettlement = row.is_settlement || row.type.startsWith("SETTLEMENT_");
  const { isDebit, amount, hasAmount } = signedAmount(row);

  return (
    <tr
      className={cn(
        "border-t border-border/60 transition-colors hover:bg-muted/15",
        isSettlement && "bg-amber-500/10 hover:bg-amber-500/15",
      )}
    >
      <td className="whitespace-nowrap px-3 py-2 font-tabular text-xs text-muted-foreground">
        {new Date(row.date).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" })}{" "}IST
      </td>
      <td className="px-3 py-2">
        <CategoryPill label={row.label} isSettlement={isSettlement} />
      </td>
      <td className="px-3 py-2 text-xs">
        <span
          className={cn(
            "block max-w-[420px] truncate",
            isSettlement && "font-semibold text-amber-700 dark:text-amber-300",
          )}
          title={row.particulars}
        >
          {row.particulars}
        </span>
      </td>
      <td
        className={cn(
          "whitespace-nowrap px-3 py-2 text-right font-tabular tabular-nums font-semibold",
          hasAmount && isDebit && "text-destructive",
          hasAmount && !isDebit && "text-emerald-600 dark:text-emerald-400",
          !hasAmount && "text-muted-foreground",
        )}
      >
        {hasAmount ? `${isDebit ? "−" : "+"}${formatINR(amount)}` : "—"}
      </td>
    </tr>
  );
}

function LedgerCardMobile({ row }: { row: LedgerRow }) {
  const isSettlement = row.is_settlement || row.type.startsWith("SETTLEMENT_");
  const { isDebit, amount, hasAmount } = signedAmount(row);

  return (
    <div
      className={cn(
        "rounded-xl border border-border/60 bg-card p-3",
        isSettlement && "border-amber-500/40 bg-amber-500/10",
      )}
    >
      {/* Top: type pill + timestamp */}
      <div className="flex items-center justify-between gap-2">
        <CategoryPill label={row.label} isSettlement={isSettlement} />
        <span className="shrink-0 font-tabular text-[10px] text-muted-foreground">
          {new Date(row.date).toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" })}{" "}IST
        </span>
      </div>

      {/* Detail */}
      <p
        className={cn(
          "mt-2 text-xs leading-snug text-foreground/90",
          isSettlement && "font-semibold text-amber-700 dark:text-amber-300",
        )}
      >
        {row.particulars}
      </p>

      {/* Signed amount */}
      <div className="mt-2.5 border-t border-border/50 pt-2">
        <div
          className={cn(
            "font-tabular text-base font-semibold tabular-nums",
            !hasAmount && "text-muted-foreground",
            hasAmount && isDebit && "text-destructive",
            hasAmount && !isDebit && "text-emerald-600 dark:text-emerald-400",
          )}
        >
          {hasAmount ? `${isDebit ? "−" : "+"}${formatINR(amount)}` : "—"}
        </div>
      </div>
    </div>
  );
}

function CategoryPill({
  label,
  isSettlement,
}: {
  label: string;
  isSettlement: boolean;
}) {
  const cls = cn(
    "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
    isSettlement
      ? "bg-amber-500/20 text-amber-700 dark:text-amber-300"
      : "bg-muted text-muted-foreground",
  );
  return <span className={cls}>{label}</span>;
}
