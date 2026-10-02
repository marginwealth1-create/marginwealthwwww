import type { SVGProps } from "react";

/**
 * MarginWealth brand mark — blue "M" monogram crowned with a gold gem.
 * Geometry is the 512px mark in `public/icon.svg` scaled to lucide's 24x24
 * box, so it drops into every place an icon sat (same `className` sizing).
 *
 * The M is always the brand blue (`fill-brand` → tailwind.config.ts); the
 * gem is the fixed brand gold, so the mark reads the same on light and dark
 * surfaces. Regenerate these numbers with `public/icons/_gen_brand_assets.py`
 * (frontend-user) if the mark changes.
 */
export function BrandGlyph({ className, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={className}
      {...props}
    >
      <polygon
        className="fill-brand"
        points="0.96,23.04 0.96,5.38 5.16,5.38 12.00,14.65 18.84,5.38 23.04,5.38 23.04,23.04 18.84,23.04 18.84,12.44 12.00,21.72 5.16,12.44 5.16,23.04"
      />
      <polygon points="12.00,0.96 15.53,5.38 12.00,9.79 8.47,5.38" fill="#E6B839" />
    </svg>
  );
}
