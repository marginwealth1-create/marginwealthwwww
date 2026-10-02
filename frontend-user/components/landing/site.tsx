"use client";

import { useState } from "react";
import Link from "next/link";
import { MenuIcon, X } from "lucide-react";
import { Wordmark } from "@/components/layout/Wordmark";
import TradingViewLegalNotice from "./TradingViewLegalNotice";

/** Public-site chrome shared by the home page and the legal pages. */

const NAV = [
  { label: "Markets", href: "/#markets" },
  { label: "Platform", href: "/#platform" },
  { label: "Pricing", href: "/#pricing" },
  { label: "FAQ", href: "/#faq" },
];

export function SiteHeader() {
  const [open, setOpen] = useState(false);
  return (
    <header className="sticky top-0 z-50 border-b border-slate-200/70 bg-white/85 backdrop-blur-md">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
        <Link href="/" aria-label="MarginWealth home" className="rounded-md">
          <Wordmark markClassName="size-7" textClassName="text-xl" className="text-slate-900" />
        </Link>

        <nav className="hidden items-center gap-8 md:flex">
          {NAV.map((l) => (
            <a key={l.label} href={l.href} className="text-sm font-medium text-slate-600 transition-colors hover:text-slate-900">
              {l.label}
            </a>
          ))}
        </nav>

        <div className="flex items-center gap-2">
          <Link href="/login" className="hidden rounded-lg px-3 py-2 text-sm font-semibold text-slate-700 transition-colors hover:text-slate-950 sm:inline-flex">
            Log in
          </Link>
          <Link href="/register" className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-brand-700">
            Open account
          </Link>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-label="Toggle menu"
            aria-expanded={open}
            className="rounded-lg p-2 text-slate-700 hover:bg-slate-100 md:hidden"
          >
            {open ? <X className="size-5" /> : <MenuIcon className="size-5" />}
          </button>
        </div>
      </div>

      {open && (
        <div className="border-t border-slate-200 bg-white px-4 py-3 md:hidden">
          {[...NAV, { label: "Log in", href: "/login" }].map((l) => (
            <a
              key={l.label}
              href={l.href}
              onClick={() => setOpen(false)}
              className="block rounded-lg px-3 py-2.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              {l.label}
            </a>
          ))}
        </div>
      )}
    </header>
  );
}

const FOOTER_COLS = [
  {
    title: "Markets",
    links: [
      { label: "Equity & F&O", href: "/#markets" },
      { label: "Commodities", href: "/#markets" },
      { label: "Forex", href: "/#markets" },
      { label: "Crypto", href: "/#markets" },
      { label: "US Stocks", href: "/#markets" },
    ],
  },
  {
    title: "Platform",
    links: [
      { label: "Pricing", href: "/#pricing" },
      { label: "Download app", href: "/download" },
      { label: "Open account", href: "/register" },
      { label: "Log in", href: "/login" },
      { label: "FAQ", href: "/#faq" },
    ],
  },
  {
    title: "Legal",
    links: [
      { label: "Terms & Conditions", href: "/terms" },
      { label: "Privacy Policy", href: "/privacy-policy" },
      { label: "Refund Policy", href: "/refund-policy" },
    ],
  },
];

export function SiteFooter() {
  return (
    <footer className="border-t border-slate-200 bg-white text-slate-600">
      <div className="mx-auto max-w-6xl px-4 py-14 sm:px-6">
        <div className="grid gap-10 md:grid-cols-[1.5fr_1fr_1fr_1fr]">
          <div>
            <Wordmark markClassName="size-7" textClassName="text-xl" className="text-slate-900" />
            <p className="mt-4 max-w-xs text-sm leading-relaxed text-slate-500">
              One account for Indian and global markets — equity, F&amp;O, commodities, forex, crypto and US stocks.
            </p>
            <a href="mailto:support@marginwealth.com" className="mt-4 inline-block text-sm font-semibold text-brand-600 hover:text-brand-700">
              support@marginwealth.com
            </a>
          </div>
          {FOOTER_COLS.map((col) => (
            <div key={col.title}>
              <p className="text-xs font-semibold uppercase tracking-widest text-slate-900">{col.title}</p>
              <ul className="mt-4 space-y-2.5">
                {col.links.map((l) => (
                  <li key={l.label}>
                    <Link href={l.href} className="text-sm text-slate-500 transition-colors hover:text-slate-900">
                      {l.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>

        <div className="mt-12">
          <TradingViewLegalNotice />
        </div>
        <p className="text-xs leading-relaxed text-slate-500">
          <strong className="text-slate-700">Risk disclaimer:</strong> Trading in financial instruments involves
          significant risk of loss and is not suitable for all investors. Past performance is not indicative of future
          results. Make sure you understand the risks involved and seek independent advice if necessary.
        </p>
        <div className="mt-8 flex flex-col gap-2 border-t border-slate-200 pt-6 text-xs text-slate-500 sm:flex-row sm:justify-between">
          <p>© {new Date().getFullYear()} MarginWealth. All rights reserved.</p>
          <p>Built for Indian traders.</p>
        </div>
      </div>
    </footer>
  );
}
