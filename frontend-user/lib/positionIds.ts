import type { QueryClient } from "@tanstack/react-query";

/**
 * A just-placed trade shows up instantly as an optimistic row carrying a
 * synthetic id (`optimistic_<ts>`) rather than a Mongo ObjectId, because the
 * real position only lands on the next poll / WS push. Acting on that row —
 * setting SL/TP, closing it — must resolve the real id first, or the backend
 * answers "Position not found" (it can't parse the synthetic id as an
 * ObjectId).
 */
export function isOptimisticId(id: unknown): boolean {
  return typeof id === "string" && id.startsWith("optimistic_");
}

/**
 * Swap an `optimistic_…` row for the real position id by matching the
 * instrument token against a freshly-refetched open-positions list.
 *
 * Patient on purpose: a "buy then immediately set SL" taps while the BUY's own
 * POST may still be in flight, so the real row can take a second or two to
 * appear. Eight tries at 400 ms (~3.2 s) catches nearly all of them; most
 * resolve on the first or second.
 *
 * Returns null when it never appears — callers should say "still settling"
 * rather than send the synthetic id to the server.
 */
export async function resolveRealPositionId(
  qc: QueryClient,
  optimisticId: string,
  attempts = 8,
): Promise<string | null> {
  const cur = qc.getQueryData<any[]>(["positions", "open"]) ?? [];
  const opt = cur.find((p) => p?.id === optimisticId);
  // Token off the optimistic row; when the poll already swapped it out from
  // under the tap and only one position is open, that row is the one.
  const token =
    String(opt?.instrument_token ?? opt?.token ?? "") ||
    (cur.length === 1 ? String(cur[0]?.instrument_token ?? cur[0]?.token ?? "") : "");
  if (!token) return null;

  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      await qc.refetchQueries({ queryKey: ["positions", "open"] });
    } catch {
      /* ignore — the cache may still have been updated by a poll */
    }
    const fresh = qc.getQueryData<any[]>(["positions", "open"]) ?? [];
    const real = fresh.find(
      (p) =>
        !isOptimisticId(p?.id) &&
        String(p?.instrument_token ?? p?.token ?? "") === token &&
        (p?.status ?? "OPEN") === "OPEN",
    );
    if (real?.id) return String(real.id);
    if (attempt < attempts - 1) await new Promise((r) => setTimeout(r, 400));
  }
  return null;
}
