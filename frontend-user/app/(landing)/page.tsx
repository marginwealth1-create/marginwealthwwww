import Link from "next/link";
import type { LucideIcon } from "lucide-react";
import {
  ArrowRight,
  BarChart3,
  Bitcoin,
  CandlestickChart,
  Check,
  Coins,
  Globe,
  Landmark,
  ShieldCheck,
  TrendingUp,
  Wallet,
  Zap,
} from "lucide-react";
import "@/components/landing/landing.css";
import { SiteFooter, SiteHeader } from "@/components/landing/site";

export default function LandingHome() {
  return (
    <div className="min-h-screen bg-white text-slate-900">
      <SiteHeader />
      <main>
        <Hero />
        <Ticker />
        <Markets />
        <Platform />
        <Pricing />
        <Steps />
        <Faq />
        <AppCta />
      </main>
      <SiteFooter />
    </div>
  );
}

/* ── Shared bits ───────────────────────────────────────────────────── */

function Eyebrow({ children, dark = false }: { children: React.ReactNode; dark?: boolean }) {
  return (
    <p className={`text-xs font-semibold uppercase tracking-[0.18em] ${dark ? "text-gold" : "text-brand-600"}`}>
      {children}
    </p>
  );
}

const primaryBtn =
  "inline-flex items-center justify-center gap-2 rounded-xl bg-brand-600 px-6 py-3.5 text-sm font-semibold text-white shadow-lg shadow-brand-600/25 transition-colors hover:bg-brand-700";
const secondaryBtn =
  "inline-flex items-center justify-center gap-2 rounded-xl border border-slate-300 bg-white px-6 py-3.5 text-sm font-semibold text-slate-800 transition-colors hover:border-slate-400";

/* ── Hero ──────────────────────────────────────────────────────────── */

function Hero() {
  return (
    <section className="relative overflow-hidden">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_60%_55%_at_80%_0%,rgba(59,91,219,0.16),transparent_70%)]"
      />
      <div
        aria-hidden
        className="mw-dots pointer-events-none absolute inset-0 [mask-image:linear-gradient(to_bottom,black,transparent_80%)]"
      />

      <div className="relative mx-auto grid max-w-6xl items-center gap-16 px-4 pb-24 pt-14 sm:px-6 lg:grid-cols-[1.05fr_1fr] lg:pt-24">
        <div>
          <span className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs font-medium text-slate-600 shadow-sm">
            <span className="size-1.5 rounded-full bg-gold" />
            One account · Six asset classes
          </span>

          <h1 className="text-slate-900 mt-6 font-display text-5xl font-bold leading-[1.02] tracking-tight sm:text-6xl lg:text-7xl">
            Trade smart.
            <br />
            Build{" "}
            <span className="relative isolate whitespace-nowrap text-brand-600">
              wealth
              <span aria-hidden className="absolute inset-x-0 bottom-1 -z-10 h-3 rounded-sm bg-gold/40 sm:bottom-2" />
            </span>
            .
          </h1>

          <p className="mt-6 max-w-lg text-lg leading-relaxed text-slate-600">
            Indian equity, F&amp;O, commodities, forex, crypto and US stocks — on one fast terminal with flat
            brokerage and real-time risk controls.
          </p>

          <div className="mt-9 flex flex-wrap gap-3">
            <Link href="/register" className={primaryBtn}>
              Open free account <ArrowRight className="size-4" />
            </Link>
            <Link href="/login" className={secondaryBtn}>
              Try the ₹50L demo
            </Link>
          </div>

          <dl className="mt-12 grid max-w-lg grid-cols-3 gap-6 border-t border-slate-200 pt-6">
            {[
              ["₹20", "flat per order"],
              ["₹0", "account opening"],
              ["10 min", "to start trading"],
            ].map(([value, label]) => (
              <div key={label}>
                <dt className="font-display text-2xl font-bold">{value}</dt>
                <dd className="mt-0.5 text-xs text-slate-500">{label}</dd>
              </div>
            ))}
          </dl>
        </div>

        <TerminalPreview />
      </div>
    </section>
  );
}

const WATCHLIST = [
  ["NIFTY 50", "24,812.35", "+0.84%"],
  ["BANKNIFTY", "52,190.10", "+1.12%"],
  ["RELIANCE", "2,914.20", "-0.36%"],
  ["GOLD", "72,450.00", "+0.52%"],
  ["USDINR", "83.94", "-0.08%"],
  ["BTCUSD", "64,120.50", "+2.41%"],
];

const CHART_LINE =
  "M0,96 L20,90 L40,98 L60,82 L80,86 L100,72 L120,77 L140,60 L160,66 L180,52 L200,58 L220,42 L240,47 L260,32 L280,36 L300,20";

/** Illustrative product shot — a static mock of the trading terminal. */
function TerminalPreview() {
  return (
    <div className="relative" aria-label="Preview of the MarginWealth trading terminal" role="img">
      <div aria-hidden className="absolute -inset-6 rounded-[2rem] bg-gradient-to-tr from-brand-500/25 via-transparent to-gold/25 blur-2xl" />

      <div className="relative overflow-hidden rounded-2xl border border-white/10 bg-[#0B1020] text-white shadow-2xl shadow-brand-900/30">
        <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
          <div className="flex gap-1.5">
            <span className="size-2.5 rounded-full bg-white/20" />
            <span className="size-2.5 rounded-full bg-white/20" />
            <span className="size-2.5 rounded-full bg-white/20" />
          </div>
          <span className="text-[11px] font-medium text-white/50">MarginWealth Terminal</span>
          <span className="rounded bg-white/10 px-1.5 py-0.5 text-[10px] font-medium text-white/60">Preview</span>
        </div>

        <div className="grid sm:grid-cols-[1fr_190px]">
          <div className="border-b border-white/10 p-5 sm:border-b-0 sm:border-r">
            <div className="flex items-start justify-between gap-3">
              <div>
                <p className="text-xs text-white/50">NIFTY 50 · NSE</p>
                <p className="mt-1 font-numeric text-2xl font-semibold">24,812.35</p>
              </div>
              <span className="rounded-md bg-emerald-500/15 px-2 py-1 font-numeric text-xs text-emerald-400">
                +206.40 (0.84%)
              </span>
            </div>

            <svg viewBox="0 0 300 120" preserveAspectRatio="none" className="mt-5 h-36 w-full" aria-hidden>
              <defs>
                <linearGradient id="mw-area" x1="0" x2="0" y1="0" y2="1">
                  <stop offset="0" stopColor="#3B5BDB" stopOpacity="0.5" />
                  <stop offset="1" stopColor="#3B5BDB" stopOpacity="0" />
                </linearGradient>
              </defs>
              {[30, 60, 90].map((y) => (
                <line key={y} x1="0" x2="300" y1={y} y2={y} stroke="white" strokeOpacity="0.06" />
              ))}
              <path d={`${CHART_LINE} L300,120 L0,120 Z`} fill="url(#mw-area)" />
              <path d={CHART_LINE} fill="none" stroke="#6E89F7" strokeWidth="2" vectorEffect="non-scaling-stroke" />
            </svg>

            <div className="mt-4 grid grid-cols-2 gap-2 text-center text-sm font-semibold">
              <span className="rounded-lg bg-emerald-500 py-2">Buy</span>
              <span className="rounded-lg bg-red-500 py-2">Sell</span>
            </div>
          </div>

          <ul className="divide-y divide-white/5 text-xs">
            {WATCHLIST.map(([symbol, price, change]) => (
              <li key={symbol} className="flex items-center justify-between px-4 py-3">
                <span className="font-medium text-white/80">{symbol}</span>
                <span className="text-right font-numeric">
                  <span className="block text-white">{price}</span>
                  <span className={change.startsWith("-") ? "text-red-400" : "text-emerald-400"}>{change}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      <div className="absolute -bottom-7 -left-3 hidden items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 text-slate-900 shadow-xl sm:flex">
        <span className="grid size-9 place-items-center rounded-full bg-emerald-50 text-emerald-600">
          <Check className="size-4" />
        </span>
        <div>
          <p className="text-sm font-semibold">Order executed</p>
          <p className="font-numeric text-xs text-slate-500">BUY 50 RELIANCE @ 2,914.20</p>
        </div>
      </div>
    </div>
  );
}

/* ── Instrument ticker ─────────────────────────────────────────────── */

const INSTRUMENTS = [
  "NIFTY 50", "SENSEX", "BANKNIFTY", "RELIANCE", "HDFCBANK", "TCS", "INFY", "GOLD", "SILVER",
  "CRUDEOIL", "USDINR", "EURUSD", "GBPUSD", "BTCUSD", "ETHUSD", "AAPL", "TSLA", "NVDA",
];

function Ticker() {
  return (
    <div className="overflow-hidden border-y border-slate-200 bg-slate-50 py-4">
      <div className="mw-marquee flex w-max">
        {[0, 1].map((copy) => (
          <ul key={copy} aria-hidden={copy === 1} className="flex shrink-0 items-center">
            {INSTRUMENTS.map((s) => (
              <li key={s} className="flex items-center gap-3 px-6 font-numeric text-sm font-medium text-slate-500">
                <span className="size-1 rounded-full bg-gold" />
                {s}
              </li>
            ))}
          </ul>
        ))}
      </div>
    </div>
  );
}

/* ── Markets ───────────────────────────────────────────────────────── */

const MARKETS: { icon: LucideIcon; title: string; desc: string; tags: string }[] = [
  { icon: TrendingUp, title: "Equity", desc: "Cash market on NSE & BSE with instant fills and clean contract notes.", tags: "NSE · BSE" },
  { icon: CandlestickChart, title: "Futures & Options", desc: "Index and stock derivatives with live option chains and margin preview.", tags: "NFO · BFO" },
  { icon: Coins, title: "Commodities", desc: "Gold, silver, crude oil and base metals, traded on MCX.", tags: "MCX" },
  { icon: Globe, title: "Forex", desc: "INR and major currency pairs with tight, transparent spreads.", tags: "USDINR · EURUSD" },
  { icon: Bitcoin, title: "Crypto", desc: "Leading digital assets, traded around the clock.", tags: "BTC · ETH" },
  { icon: Landmark, title: "US Stocks", desc: "US-listed stocks and ETFs from the same wallet.", tags: "AAPL · TSLA · NVDA" },
];

function Markets() {
  return (
    <section id="markets" className="scroll-mt-16 py-24 sm:py-28">
      <div className="mx-auto max-w-6xl px-4 sm:px-6">
        <div className="max-w-2xl">
          <Eyebrow>Markets</Eyebrow>
          <h2 className="text-slate-900 mt-3 font-display text-4xl font-bold tracking-tight sm:text-5xl">
            One account. Every market that matters.
          </h2>
          <p className="mt-4 text-lg text-slate-600">
            Move between Indian and global markets without juggling brokers, apps or wallets.
          </p>
        </div>

        <div className="mt-14 grid gap-px overflow-hidden rounded-2xl border border-slate-200 bg-slate-200 sm:grid-cols-2 lg:grid-cols-3">
          {MARKETS.map(({ icon: Icon, title, desc, tags }) => (
            <div key={title} className="group bg-white p-8 transition-colors hover:bg-slate-50">
              <span className="grid size-11 place-items-center rounded-xl bg-brand-50 text-brand-600 transition-colors group-hover:bg-brand-600 group-hover:text-white">
                <Icon className="size-5" />
              </span>
              <h3 className="text-slate-900 mt-6 text-lg font-semibold">{title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-slate-600">{desc}</p>
              <p className="mt-5 font-numeric text-xs text-slate-400">{tags}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ── Platform (dark band) ──────────────────────────────────────────── */

const FEATURES: { icon: LucideIcon; title: string; desc: string }[] = [
  { icon: Zap, title: "Instant execution", desc: "Market orders fill the moment you place them; limit and stop orders trigger automatically." },
  { icon: BarChart3, title: "Pro-grade charts", desc: "TradingView-powered charts with indicators and drawing tools on every instrument." },
  { icon: ShieldCheck, title: "Real-time risk controls", desc: "Live margin tracking, alerts and automatic stop-out keep your capital protected." },
  { icon: Wallet, title: "Fast funding", desc: "Add funds over UPI or bank transfer and withdraw straight to your bank account." },
];

const STATS = [
  ["150K+", "active traders"],
  ["1M+", "orders every month"],
  ["99.9%", "platform uptime"],
  ["24/7", "live chat support"],
];

function Platform() {
  return (
    <section id="platform" className="scroll-mt-16 bg-[#0B1020] py-24 text-white sm:py-28">
      <div className="mx-auto grid max-w-6xl gap-16 px-4 sm:px-6 lg:grid-cols-[1.2fr_1fr] lg:items-center">
        <div>
          <Eyebrow dark>Platform</Eyebrow>
          <h2 className="text-white mt-3 font-display text-4xl font-bold tracking-tight sm:text-5xl">
            Built for traders who move fast.
          </h2>
          <div className="mt-12 grid gap-10 sm:grid-cols-2">
            {FEATURES.map(({ icon: Icon, title, desc }) => (
              <div key={title}>
                <Icon className="size-6 text-gold" />
                <h3 className="text-white mt-4 font-semibold">{title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-white/60">{desc}</p>
              </div>
            ))}
          </div>
        </div>

        <dl className="grid grid-cols-2 gap-4">
          {STATS.map(([value, label]) => (
            <div key={label} className="rounded-2xl border border-white/10 bg-white/[0.04] p-6">
              <dt className="font-display text-4xl font-bold">{value}</dt>
              <dd className="mt-2 text-sm text-white/55">{label}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}

/* ── Pricing ───────────────────────────────────────────────────────── */

const PLANS = [
  { label: "Account opening", price: "₹0", note: "Free, fully digital KYC. No paperwork.", featured: false },
  { label: "Brokerage", price: "₹20", unit: "/ order", note: "Flat on intraday and F&O — whatever the order size.", featured: true },
  { label: "Demo account", price: "₹50L", unit: "virtual", note: "Practise risk-free before you go live.", featured: false },
];

function Pricing() {
  return (
    <section id="pricing" className="scroll-mt-16 py-24 sm:py-28">
      <div className="mx-auto max-w-6xl px-4 sm:px-6">
        <div className="mx-auto max-w-2xl text-center">
          <Eyebrow>Pricing</Eyebrow>
          <h2 className="text-slate-900 mt-3 font-display text-4xl font-bold tracking-tight sm:text-5xl">Simple, honest pricing.</h2>
          <p className="mt-4 text-lg text-slate-600">No platform fees, no inactivity fees, no surprises.</p>
        </div>

        <div className="mt-14 grid gap-5 md:grid-cols-3">
          {PLANS.map((p) => (
            <div
              key={p.label}
              className={
                p.featured
                  ? "rounded-2xl bg-brand-600 p-8 text-white shadow-xl shadow-brand-600/25"
                  : "rounded-2xl border border-slate-200 p-8"
              }
            >
              <p className={`text-sm font-medium ${p.featured ? "text-white/75" : "text-slate-500"}`}>{p.label}</p>
              <p className="mt-4 flex items-baseline gap-1.5">
                <span className="font-display text-5xl font-bold tracking-tight">{p.price}</span>
                {p.unit && <span className={`text-sm ${p.featured ? "text-white/75" : "text-slate-500"}`}>{p.unit}</span>}
              </p>
              <p className={`mt-4 text-sm leading-relaxed ${p.featured ? "text-white/85" : "text-slate-600"}`}>{p.note}</p>
            </div>
          ))}
        </div>
        <p className="mt-6 text-center text-xs text-slate-500">
          Statutory charges (STT, exchange fees, GST, stamp duty) apply as per regulations and are shown on every contract note.
        </p>
      </div>
    </section>
  );
}

/* ── Steps ─────────────────────────────────────────────────────────── */

const STEPS = [
  ["Sign up", "Create your account with your mobile number and email in under a minute."],
  ["Verify", "Complete digital KYC with PAN and Aadhaar — usually done in about 10 minutes."],
  ["Fund & trade", "Add funds over UPI or bank transfer and place your first trade."],
];

function Steps() {
  return (
    <section className="border-y border-slate-200 bg-slate-50 py-24 sm:py-28">
      <div className="mx-auto max-w-6xl px-4 sm:px-6">
        <div className="flex flex-col justify-between gap-6 md:flex-row md:items-end">
          <div>
            <Eyebrow>Get started</Eyebrow>
            <h2 className="text-slate-900 mt-3 font-display text-4xl font-bold tracking-tight sm:text-5xl">Live in three steps.</h2>
          </div>
          <Link href="/register" className={`${primaryBtn} self-start md:self-auto`}>
            Open free account <ArrowRight className="size-4" />
          </Link>
        </div>

        <ol className="mt-14 grid gap-8 md:grid-cols-3">
          {STEPS.map(([title, desc], i) => (
            <li key={title} className="border-t-2 border-slate-900 pt-6">
              <span className="font-numeric text-sm text-brand-600">0{i + 1}</span>
              <h3 className="text-slate-900 mt-3 text-xl font-semibold">{title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-slate-600">{desc}</p>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

/* ── FAQ ───────────────────────────────────────────────────────────── */

const FAQS = [
  ["What do I need to open an account?", "A PAN card, Aadhaar card and a bank account. The whole process is digital and usually takes under 10 minutes."],
  ["How much brokerage do you charge?", "A flat ₹20 per order on intraday and F&O trades, whatever the order size. Statutory charges apply as per regulations."],
  ["Can I try the platform before funding?", "Yes. Start a demo account from the login page and get ₹50,00,000 in virtual funds to practise with — no signup needed."],
  ["How do withdrawals work?", "Request a withdrawal from the Wallet section and it is sent to your registered bank account, typically within 24 hours on working days."],
  ["Is my account secure?", "Accounts are protected with encrypted connections and optional two-factor authentication, and live risk controls watch every open position."],
  ["Can I trade on mobile?", "Yes. Install the MarginWealth app from the download page for the full experience — charts, orders, positions and funds."],
];

function Faq() {
  return (
    <section id="faq" className="scroll-mt-16 py-24 sm:py-28">
      <div className="mx-auto grid max-w-6xl gap-12 px-4 sm:px-6 lg:grid-cols-[1fr_1.6fr]">
        <div>
          <Eyebrow>FAQ</Eyebrow>
          <h2 className="text-slate-900 mt-3 font-display text-4xl font-bold tracking-tight">Questions, answered.</h2>
          <p className="mt-4 text-slate-600">
            Can&apos;t find what you need? Write to{" "}
            <a href="mailto:support@marginwealth.live" className="font-semibold text-brand-600 hover:text-brand-700">
              support@marginwealth.live
            </a>
            .
          </p>
        </div>

        <div className="divide-y divide-slate-200 border-y border-slate-200">
          {FAQS.map(([q, a]) => (
            <details key={q} className="group py-5">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-6 font-medium [&::-webkit-details-marker]:hidden">
                {q}
                <span className="grid size-7 shrink-0 place-items-center rounded-full border border-slate-300 text-slate-500 transition-transform group-open:rotate-45">
                  +
                </span>
              </summary>
              <p className="mt-3 pr-12 text-sm leading-relaxed text-slate-600">{a}</p>
            </details>
          ))}
        </div>
      </div>
    </section>
  );
}

/* ── App / closing CTA ─────────────────────────────────────────────── */

function AppCta() {
  return (
    <section className="px-4 pb-24 sm:px-6">
      <div className="mx-auto grid max-w-6xl overflow-hidden rounded-3xl bg-[#0B1020] text-white lg:grid-cols-2">
        <div className="p-10 sm:p-14">
          <Eyebrow dark>Start today</Eyebrow>
          <h2 className="text-white mt-3 font-display text-4xl font-bold tracking-tight sm:text-5xl">
            Your markets, in your pocket.
          </h2>
          <p className="mt-4 max-w-md text-white/65">
            Open an account in minutes and trade from the web or the MarginWealth app.
          </p>
          <div className="mt-9 flex flex-wrap gap-3">
            <Link
              href="/register"
              className="inline-flex items-center gap-2 rounded-xl bg-gold px-6 py-3.5 text-sm font-semibold text-[#0B1020] transition-opacity hover:opacity-90"
            >
              Open free account <ArrowRight className="size-4" />
            </Link>
            <Link
              href="/download"
              className="inline-flex items-center rounded-xl border border-white/20 px-6 py-3.5 text-sm font-semibold transition-colors hover:bg-white/5"
            >
              Download the app
            </Link>
          </div>
        </div>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/image1.png" alt="The MarginWealth app on a phone" className="h-72 w-full object-cover lg:h-full" />
      </div>
    </section>
  );
}
