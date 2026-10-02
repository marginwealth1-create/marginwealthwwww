import { cn } from "@/lib/utils";
import { BrandGlyph } from "./BrandGlyph";

/**
 * Platform lockup — mark + "Margin" (current text colour) + "Wealth" (brand
 * blue), matching the brand artwork. Rendered as real text rather than an
 * image so it stays crisp at any DPI, picks up the bundled Space Grotesk,
 * and flips ink/white with the surface it sits on.
 *
 * Used ONLY where the platform's own identity is correct — never as the
 * fallback on a tenant's branded domain.
 */
export function Wordmark({
  className,
  markClassName = "size-9",
  textClassName = "text-2xl",
}: {
  className?: string;
  markClassName?: string;
  textClassName?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <BrandGlyph className={markClassName} />
      <span className={cn("font-display font-bold leading-none tracking-tight", textClassName)}>
        Margin<span className="text-brand">Wealth</span>
      </span>
    </span>
  );
}
