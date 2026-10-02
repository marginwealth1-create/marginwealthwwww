"use client";

interface Props {
  bids: { price: number; qty: number; orders: number }[];
  asks: { price: number; qty: number; orders: number }[];
}

export function MarketDepth({ bids, asks }: Props) {
  const maxQty = Math.max(1, ...bids.map((b) => b.qty), ...asks.map((a) => a.qty));

  return (
    <div className="grid grid-cols-2 gap-3 text-xs font-tabular">
      <Side label="Bids" rows={bids} maxQty={maxQty} side="bid" />
      <Side label="Asks" rows={asks} maxQty={maxQty} side="ask" />
    </div>
  );
}

function Side({
  label,
  rows,
  maxQty,
  side,
}: {
  label: string;
  rows: { price: number; qty: number; orders: number }[];
  maxQty: number;
  side: "bid" | "ask";
}) {
  return (
    <div>
      <div className="mb-1 grid grid-cols-3 px-1 text-[10px] uppercase tracking-wider text-muted-foreground">
        <span>Price</span>
        <span className="text-right">Qty</span>
        <span className="text-right">Orders</span>
      </div>
      <div className="space-y-0.5">
        {rows.map((r, i) => {
          const pct = (r.qty / maxQty) * 100;
          return (
            <div key={i} className="relative grid grid-cols-3 rounded-sm px-1 py-0.5">
              <span
                className={`absolute inset-y-0 ${side === "bid" ? "left-0" : "right-0"} rounded-sm ${
                  side === "bid" ? "bg-buy/15" : "bg-sell/15"
                }`}
                style={{ width: `${pct}%` }}
              />
              <span className={`relative ${side === "bid" ? "text-buy" : "text-sell"}`}>
                {r.price.toFixed(2)}
              </span>
              <span className="relative text-right">{r.qty.toLocaleString("en-IN")}</span>
              <span className="relative text-right text-muted-foreground">{r.orders}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
