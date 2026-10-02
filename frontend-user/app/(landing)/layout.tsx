import type { Metadata } from "next";

export const metadata: Metadata = {
  title: { absolute: "MarginWealth — India's Modern Multi-Segment Trading Platform" },
  description:
    "Trade NSE, BSE, MCX, currency, crypto and global forex on one fast terminal. Flat ₹20 brokerage, real-time risk controls, transparent statutory breakdown.",
};

export default function LandingLayout({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}
