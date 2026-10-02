"""Single source for the MarginWealth mark — emits every raster + vector asset.

    py frontend-user/public/icons/_gen_brand_assets.py

The mark is a geometric "M" monogram (brand blue) crowned with a gold gem —
"Margin" + "Wealth". It is defined ONCE below as normalised polygons (0..1
across the mark's width) and every output is derived from it, so the favicon,
the PWA tiles, the inline `BrandGlyph` component and the standalone wordmarks
can never drift apart.

Writes:
    frontend-user/public/icons/icon-{192,512}.png, icon-maskable-512.png
    frontend-user/public/febicon.png                 (favicon + OG card)
    frontend-user/public/icon.svg
    frontend-user/public/logo.svg, logo-light.svg
    frontend-admin/public/icon-{192,512}.png
    frontend-admin/app/icon.svg
    frontend-admin/public/logo.svg

`components/layout/BrandGlyph.tsx` (both apps) carries the same polygons in a
24x24 box — regenerate its coordinates from the numbers printed at the end.
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
USER_PUBLIC = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(USER_PUBLIC))
ADMIN = os.path.join(ROOT, "frontend-admin")

BLUE = "#3B5BDB"         # keep in sync with `brand` in both tailwind.config.ts
BLUE_RGB = (59, 91, 219)
BLUE_ON_DARK = "#6E89F7"  # brand-400 — the "Wealth" half on dark grounds
GOLD = "#E6B839"         # the gem; same gold the landing page accents use
GOLD_RGB = (230, 184, 57)
INK = "#0B0B0B"          # wordmark text on light, tile ground on the icons
PAPER = "#FFFFFF"

# ── The mark, normalised to its own bounding box ──────────────────────
# u across the width, v down from the top; the mark is square. The M's
# stems are 0.19 wide and its diagonals keep that same perpendicular
# weight; the gem sits centred on the M's top line, inside the V notch.
M_SHAPE = [
    (0.0, 1.0), (0.0, 0.2), (0.19, 0.2), (0.5, 0.62), (0.81, 0.2), (1.0, 0.2),
    (1.0, 1.0), (0.81, 1.0), (0.81, 0.52), (0.5, 0.94), (0.19, 0.52), (0.19, 1.0),
]
GEM = [(0.5, 0.0), (0.66, 0.2), (0.5, 0.4), (0.34, 0.2)]
ASPECT = 1.0             # height / width

SS = 4                   # supersample factor — anti-aliasing, no extra deps


def _place(pts, x, y, w):
    return [(x + u * w, y + v * w) for u, v in pts]


# ── PNG tiles ────────────────────────────────────────────────────────
def make_tile(size: int, *, maskable: bool = False) -> Image.Image:
    """Dark tile + blue M + gold gem — matches the app's own #0a0a0a theme."""
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = 0 if maskable else int(s * 0.22)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=(10, 10, 10, 255))

    # Maskable icons keep the mark inside the 80% safe zone so an OS
    # circle-crop can't clip the M's stems.
    mw = s * (0.5 if maskable else 0.6)
    x = (s - mw) / 2
    y = (s - mw * ASPECT) / 2
    d.polygon(_place(M_SHAPE, x, y, mw), fill=BLUE_RGB + (255,))
    d.polygon(_place(GEM, x, y, mw), fill=GOLD_RGB + (255,))
    return img.resize((size, size), Image.LANCZOS)


def _check(img: Image.Image, *, maskable: bool) -> None:
    """Cheap regression guard: M painted, gem painted, and a rounded tile
    actually has transparent corners."""
    px = img.load()
    w = img.width
    near = lambda c, rgb: all(abs(a - b) < 14 for a, b in zip(c[:3], rgb))  # noqa: E731
    cells = [px[x, y] for x in range(w) for y in range(w)]
    assert any(near(c, BLUE_RGB) for c in cells), "blue M missing"
    assert any(near(c, GOLD_RGB) for c in cells), "gold gem missing"
    assert (px[0, 0][3] == 255) is maskable, "corner alpha does not match tile shape"


# ── SVG ──────────────────────────────────────────────────────────────
def _poly(pts, x, y, w, fill):
    body = " ".join(f"{px:.2f},{py:.2f}" for px, py in _place(pts, x, y, w))
    return f'<polygon points="{body}" fill="{fill}"/>'


def mark_svg(x, y, w):
    return f'{_poly(M_SHAPE, x, y, w, BLUE)}\n  {_poly(GEM, x, y, w, GOLD)}'


def icon_svg() -> str:
    """512 app icon — same dark tile as the PNGs."""
    w = 512 * 0.6
    x = (512 - w) / 2
    y = (512 - w * ASPECT) / 2
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">\n'
            f'  <rect width="512" height="512" rx="113" fill="#0a0a0a"/>\n  '
            + mark_svg(x, y, w) + '\n</svg>\n')


FONT = ("'Space Grotesk', Inter, ui-sans-serif, system-ui, -apple-system, "
        "'Segoe UI', Roboto, sans-serif")


def wordmark_svg(ink: str, accent: str, *, width: int = 264, suffix: str = "") -> str:
    """Horizontal lockup for share cards, email, and anywhere outside the
    apps. In-app the `Wordmark` component renders the same thing with real
    text so it picks up the bundled Space Grotesk."""
    # The M's foot sits on the text baseline (y=46) and its body runs a
    # little taller than the caps; the gem rides above, like an accent.
    mw = 36.0
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} 64" width="{width}" height="64" role="img" aria-label="MarginWealth">
  {mark_svg(6, 46 - mw * ASPECT, mw)}
  <text x="52" y="46" font-family="{FONT}" font-size="31" font-weight="700" letter-spacing="-0.8">
    <tspan fill="{ink}">Margin</tspan><tspan fill="{accent}">Wealth</tspan>{suffix}
  </text>
</svg>
'''


def main() -> None:
    pngs = [
        (os.path.join(HERE, "icon-192.png"), 192, False),
        (os.path.join(HERE, "icon-512.png"), 512, False),
        (os.path.join(HERE, "icon-maskable-512.png"), 512, True),
        (os.path.join(USER_PUBLIC, "febicon.png"), 512, False),
        (os.path.join(ADMIN, "public", "icon-192.png"), 192, False),
        (os.path.join(ADMIN, "public", "icon-512.png"), 512, False),
    ]
    for path, size, maskable in pngs:
        img = make_tile(size, maskable=maskable)
        _check(img, maskable=maskable)
        img.save(path, "PNG")
        print(f"wrote {path} ({size}x{size})")

    svgs = {
        os.path.join(USER_PUBLIC, "icon.svg"): icon_svg(),
        os.path.join(ADMIN, "app", "icon.svg"): icon_svg(),
        os.path.join(USER_PUBLIC, "logo.svg"): wordmark_svg(INK, BLUE),
        os.path.join(USER_PUBLIC, "logo-light.svg"): wordmark_svg(PAPER, BLUE_ON_DARK),
        os.path.join(ADMIN, "public", "logo.svg"): wordmark_svg(
            INK, BLUE, width=360,
            suffix='<tspan fill="#94a3b8" font-weight="500"> Admin</tspan>'),
    }
    for path, body in svgs.items():
        open(path, "w", encoding="utf-8").write(body)
        print(f"wrote {path}")

    # The 24x24 numbers BrandGlyph.tsx hard-codes, printed so a geometry
    # change is a copy-paste away instead of a re-derivation.
    mw = 24 * 0.92
    x = (24 - mw) / 2
    y = (24 - mw * ASPECT) / 2
    print("\nBrandGlyph 24x24 — M:",
          " ".join(f"{a:.2f},{b:.2f}" for a, b in _place(M_SHAPE, x, y, mw)))
    print("BrandGlyph 24x24 — gem:",
          " ".join(f"{a:.2f},{b:.2f}" for a, b in _place(GEM, x, y, mw)))


if __name__ == "__main__":
    main()
