"use client";

import { useEffect, useRef, useState } from "react";
import { useTheme } from "next-themes";

interface Props {
  symbol?: string;
  exchange?: string;
}

/**
 * News tab for the mobile chart page. Embeds TradingView's FREE Timeline
 * widget (no API key, no backend). For NSE / BSE instruments we first try
 * symbol-specific news; if TV returns "no data" (common for mid-cap Indian
 * stocks), we auto-fall back to the market-wide feed after a short timeout
 * so the tab always shows useful content. Infoway-fed crypto / forex go
 * straight to the general market news.
 */
function resolveFeed(
  symbol?: string,
  exchange?: string,
  forceMarket?: boolean,
): { feedMode: "symbol" | "market" | "all_symbols"; symbol?: string; market?: string } {
  if (forceMarket) {
    return { feedMode: "market", market: "stock" };
  }
  const ex = (exchange ?? "").toUpperCase();
  const sym = (symbol ?? "").toUpperCase().replace(/\s+/g, "");
  if ((ex === "NSE" || ex === "BSE") && sym) {
    return { feedMode: "symbol", symbol: `${ex}:${sym}` };
  }
  if (ex === "MCX") {
    return { feedMode: "market", market: "commodity" };
  }
  return { feedMode: "market", market: "stock" };
}

export function MobileNews({ symbol, exchange }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const { resolvedTheme } = useTheme();
  const colorTheme = resolvedTheme === "light" ? "light" : "dark";
  // Auto-fallback: if symbol-specific feed shows "no data" (common for
  // Indian mid-caps on TradingView), switch to market-wide after 4 s.
  const [forceMarket, setForceMarket] = useState(false);
  // Reset fallback when symbol changes so a new stock gets a fresh try.
  useEffect(() => setForceMarket(false), [symbol, exchange]);

  const feed = resolveFeed(symbol, exchange, forceMarket);
  const feedMode = feed.feedMode;
  const feedSymbol = feed.symbol ?? "";
  const feedMarket = feed.market ?? "";

  useEffect(() => {
    const container = ref.current;
    if (!container) return;
    container.innerHTML =
      '<div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>';
    const script = document.createElement("script");
    script.src =
      "https://s3.tradingview.com/external-embedding/embed-widget-timeline.js";
    script.async = true;
    script.type = "text/javascript";
    const config: Record<string, unknown> = {
      feedMode,
      isTransparent: true,
      displayMode: "regular",
      width: "100%",
      height: "100%",
      colorTheme,
      locale: "en",
    };
    if (feedMode === "symbol" && feedSymbol) config.symbol = feedSymbol;
    if (feedMode === "market" && feedMarket) config.market = feedMarket;
    script.innerHTML = JSON.stringify(config);
    container.appendChild(script);

    // Auto-fallback: if the widget renders but shows "no data" for a
    // symbol-specific feed, switch to market-wide news after 4 s.
    let fallbackTimer: ReturnType<typeof setTimeout> | null = null;
    if (feedMode === "symbol" && !forceMarket) {
      fallbackTimer = setTimeout(() => {
        // Check if the widget rendered "no data" text
        const text = container.textContent ?? "";
        if (
          text.includes("No data here yet") ||
          text.includes("no Top Stories") ||
          container.querySelector(".tradingview-widget-container__widget")
            ?.childElementCount === 0
        ) {
          setForceMarket(true);
        }
      }, 4000);
    }

    return () => {
      if (fallbackTimer) clearTimeout(fallbackTimer);
      container.innerHTML = "";
    };
  }, [feedMode, feedSymbol, feedMarket, colorTheme, forceMarket]);

  return (
    <div
      ref={ref}
      className="tradingview-widget-container h-full w-full overflow-y-auto bg-background"
    />
  );
}
