/* MarginWealth marketing UI primitives.
 *
 * Thin, presentational building blocks that read straight from the locked
 * `--mp-*` design tokens (registered in tailwind.config as the `mp` colour
 * family + `font-display` / `font-numeric`). Everything here is a server
 * component — no client JS — so marketing pages stay fast and SSR-clean.
 *
 * Rules from the design system that live here:
 *   • Buttons: 12px radius, firm (not pill). Primary = solid green + white
 *     text; Secondary = ghost/outline.
 *   • Cards: 16px radius, 1px --mp-border, subtle inner glow on hover, NO
 *     heavy drop shadows.
 *   • Section padding: 96px desktop / 56px mobile.
 *   • Content cap 1200px.
 */
import Link from "next/link";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { cn } from "@/lib/utils";

/* ── Layout ─────────────────────────────────────────────────────────── */

export function MpContainer({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("mx-auto w-full max-w-mp-content px-5 sm:px-8", className)}>
      {children}
    </div>
  );
}

/** A full-bleed section with the design-system vertical rhythm. Pass
 *  `dark` to flip the section to the dark surface palette. */
export function MpSection({
  id,
  dark = false,
  className,
  containerClassName,
  children,
}: {
  id?: string;
  dark?: boolean;
  className?: string;
  containerClassName?: string;
  children: ReactNode;
}) {
  return (
    <section
      id={id}
      className={cn(
        "py-14 sm:py-24",
        dark && "mp-dark bg-mp-bg text-mp-text",
        className,
      )}
    >
      <MpContainer className={containerClassName}>{children}</MpContainer>
    </section>
  );
}

/* ── Type ───────────────────────────────────────────────────────────── */

export function MpEyebrow({
  children,
  className,
  plain = false,
}: {
  children: ReactNode;
  className?: string;
  /** When true, render the original underline-style eyebrow (used on the
   *  homepage so it stays unchanged). Default is the solid green pill tag. */
  plain?: boolean;
}) {
  if (plain) {
    return (
      <span
        className={cn(
          "inline-flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-mp-primary",
          className,
        )}
      >
        <span className="h-px w-6 bg-mp-primary/50" aria-hidden />
        {children}
      </span>
    );
  }
  return (
    <span
      className={cn(
        "inline-flex w-fit items-center gap-1.5 rounded-full bg-mp-primary px-3.5 py-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-white shadow-sm shadow-mp-primary/20",
        className,
      )}
    >
      <span className="size-1.5 rounded-full bg-mp-accent" aria-hidden />
      {children}
    </span>
  );
}

/** Standard section header: eyebrow → title → optional lead paragraph. */
export function MpHeading({
  eyebrow,
  title,
  lead,
  align = "left",
  className,
  plain = false,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  lead?: ReactNode;
  align?: "left" | "center";
  className?: string;
  /** Pass through to MpEyebrow — homepage uses `plain` to keep its look. */
  plain?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex flex-col gap-4",
        align === "center" && "items-center text-center",
        className,
      )}
    >
      {eyebrow ? <MpEyebrow plain={plain}>{eyebrow}</MpEyebrow> : null}
      <h2 className="font-display text-3xl font-bold leading-[1.1] text-mp-text sm:text-4xl">
        {title}
      </h2>
      {lead ? (
        <p
          className={cn(
            "max-w-mp-prose text-base leading-[1.65] text-mp-text-mut",
            align === "center" && "mx-auto",
          )}
        >
          {lead}
        </p>
      ) : null}
    </div>
  );
}

/** Dark page header band used at the top of inner marketing pages.
 *  eyebrow → title → lead, with optional CTA buttons passed as children. */
export function MpPageHero({
  eyebrow,
  title,
  lead,
  children,
}: {
  eyebrow?: ReactNode;
  title: ReactNode;
  lead?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <section className="mp-dark relative overflow-hidden bg-mp-bg text-mp-text">
      <div className="mp-grid-lines absolute inset-0 opacity-30" aria-hidden />
      <div
        className="absolute -top-32 left-1/2 h-[320px] w-[680px] -translate-x-1/2 rounded-full bg-mp-primary/15 blur-[130px]"
        aria-hidden
      />
      <MpContainer className="relative pb-16 pt-28 sm:pb-20 sm:pt-32">
        <div className="flex max-w-3xl flex-col gap-5">
          {eyebrow ? <MpEyebrow>{eyebrow}</MpEyebrow> : null}
          <h1 className="font-display text-4xl font-bold leading-[1.08] text-mp-text sm:text-5xl">
            {title}
          </h1>
          {lead ? (
            <p className="max-w-2xl text-lg leading-[1.6] text-mp-text-mut">
              {lead}
            </p>
          ) : null}
          {children ? (
            <div className="mt-2 flex flex-col gap-3 sm:flex-row">{children}</div>
          ) : null}
        </div>
      </MpContainer>
    </section>
  );
}

/** Prose paragraph at the design-system reading measure. */
export function MpProse({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <p
      className={cn(
        "max-w-mp-prose text-base leading-[1.65] text-mp-text-mut",
        className,
      )}
    >
      {children}
    </p>
  );
}

/* ── Card ───────────────────────────────────────────────────────────── */

export function MpCard({
  className,
  hover = true,
  children,
}: {
  className?: string;
  hover?: boolean;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-2xl border border-mp-border bg-mp-surface p-6",
        hover && "mp-card-glow",
        className,
      )}
    >
      {children}
    </div>
  );
}

/* ── Stat grid ──────────────────────────────────────────────────────── */

// Reference-style big-number stat cards in a mixed green / dark / lime / white
// palette. Cards cycle through tones for visual rhythm, like the deck mockup.
const STAT_TONES = [
  { box: "bg-mp-primary", num: "text-white", label: "text-white", sub: "text-white/70" },
  { box: "bg-[#0c2a1e]", num: "text-white", label: "text-white", sub: "text-white/60" },
  { box: "bg-mp-accent", num: "text-[#0c2a1e]", label: "text-[#0c2a1e]", sub: "text-[#0c2a1e]/70" },
  {
    box: "border border-mp-border bg-mp-surface",
    num: "text-mp-primary",
    label: "text-mp-text",
    sub: "text-mp-text-mut",
  },
] as const;

export function MpStatGrid({
  items,
}: {
  items: { value: string; label: string; sub?: string }[];
}) {
  return (
    <div className="grid grid-cols-2 gap-4 sm:gap-5 lg:grid-cols-4">
      {items.map((s, i) => {
        const t = STAT_TONES[i % STAT_TONES.length];
        return (
          <div
            key={s.label}
            className={cn(
              "flex min-h-[150px] flex-col justify-between rounded-3xl p-6 transition-transform duration-300 hover:-translate-y-1",
              t.box,
            )}
          >
            <span className={cn("mp-num font-display text-4xl font-bold leading-none", t.num)}>
              {s.value}
            </span>
            <div className="mt-4">
              <div className={cn("text-sm font-semibold", t.label)}>{s.label}</div>
              {s.sub ? <div className={cn("mt-1 text-xs", t.sub)}>{s.sub}</div> : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ── Button ─────────────────────────────────────────────────────────── */

type MpButtonVariant = "primary" | "secondary" | "ghost";
type MpButtonSize = "md" | "lg";

const BUTTON_BASE =
  "inline-flex items-center justify-center gap-2 rounded-xl font-semibold transition-all duration-200 disabled:cursor-not-allowed disabled:opacity-60";

const BUTTON_VARIANTS: Record<MpButtonVariant, string> = {
  primary:
    "bg-mp-primary text-white shadow-sm hover:bg-mp-primary-2 hover:shadow-md hover:shadow-mp-primary/20",
  secondary:
    "border border-mp-border bg-transparent text-mp-text hover:border-mp-primary/60 hover:text-mp-primary",
  ghost: "bg-transparent text-mp-text-mut hover:text-mp-text",
};

const BUTTON_SIZES: Record<MpButtonSize, string> = {
  md: "h-11 px-5 text-sm",
  lg: "h-12 px-6 text-[15px]",
};

type MpButtonProps = {
  variant?: MpButtonVariant;
  size?: MpButtonSize;
  href?: string;
  className?: string;
  children: ReactNode;
} & Omit<ComponentPropsWithoutRef<"button">, "ref">;

export function MpButton({
  variant = "primary",
  size = "md",
  href,
  className,
  children,
  ...rest
}: MpButtonProps) {
  const classes = cn(
    BUTTON_BASE,
    BUTTON_VARIANTS[variant],
    BUTTON_SIZES[size],
    className,
  );
  if (href) {
    return (
      <Link href={href} className={classes}>
        {children}
      </Link>
    );
  }
  return (
    <button className={classes} {...rest}>
      {children}
    </button>
  );
}
