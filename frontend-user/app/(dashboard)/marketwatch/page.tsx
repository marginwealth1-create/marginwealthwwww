"use client";

import { useState } from "react";
import { MobileInstrumentsBar } from "@/components/trading/MobileInstrumentsBar";
import { TradeDetailSheet } from "@/components/trading/TradeDetailSheet";

/**
 * Markets page — browse + search every tradable instrument, star favorites,
 * tap a row to open the slide-up trade card with all order-placement
 * controls (no route change, so the user returns to the same scroll
 * position when the card closes).
 */
type SeedQuote = {
  ltp?: number | null;
  bid?: number | null;
  ask?: number | null;
  symbol?: string | null;
  exchange?: string | null;
  segment?: string | null;
} | null;

export default function MarketsPage() {
  const [tradeToken, setTradeToken] = useState<string | null>(null);
  // Last-known price of the tapped row, handed to the trade card so it
  // paints a price INSTANTLY instead of sitting at 0.00 while its own WS
  // connection warms up on first open.
  const [seedQuote, setSeedQuote] = useState<SeedQuote>(null);

  return (
    // Full-bleed on mobile: negative margins cancel the dashboard layout's
    // p-4 / pb-24 so the markets view runs edge-to-edge (no floating card),
    // sized to fill exactly between the sticky TopBar (h-14) and the fixed
    // BottomNav (h-14). Desktop keeps the normal padded panel.
    //
    // The height ALSO subtracts the top + bottom safe-area insets. Without
    // them the container was ~80 px TALLER than the real gap on iOS notch
    // devices (Dynamic Island + home-bar), so the watchlist's last rows fell
    // behind the fixed BottomNav / off the bottom edge and couldn't be
    // scrolled into view — the "marketwatch iOS me scroll nahi hota" bug.
    // The insets resolve to 0 on non-notch / Android / desktop, so this is a
    // no-op everywhere else.
    <div
      className="-mx-4 -mt-4 -mb-24 flex flex-col md:mx-0 md:mt-0 md:mb-0 md:h-[calc(100vh-7rem)] md:min-h-[480px]"
      style={{
        height:
          "calc(100dvh - 7rem - env(safe-area-inset-top) - env(safe-area-inset-bottom))",
      }}
    >
      <MobileInstrumentsBar
        activeToken={tradeToken}
        onSelect={(token, seed) => {
          setTradeToken(token);
          setSeedQuote(seed ?? null);
        }}
      />
      <TradeDetailSheet
        token={tradeToken}
        open={!!tradeToken}
        seedQuote={seedQuote}
        onClose={() => setTradeToken(null)}
        // In-sheet Option Chain picker on mobile swaps the displayed
        // strike instead of full-route bouncing to /terminal — the
        // user stays in the marketwatch → trade flow.
        onSwap={(tok) => setTradeToken(tok)}
      />
    </div>
  );
}
