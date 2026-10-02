/** @type {import('next').NextConfig} */
const nextConfig = {
  // Build into a staging dir so a deploy never serves from a half-written
  // .next. scripts/deploy.sh builds with NEXT_DIST_DIR=.next-build and only
  // then swaps it in, cutting the broken window from the whole build down
  // to a single reload.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  reactStrictMode: true,
  poweredByHeader: false,
  compress: true,
  images: { formats: ["image/avif", "image/webp"] },
  experimental: {
    optimizePackageImports: ["lucide-react", "date-fns"],
  },
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "no-referrer" },
          { key: "X-Robots-Tag", value: "noindex, nofollow" },
        ],
      },
      {
        // PWA "old code keeps showing" fix. The installed standalone
        // shell was serving stale HTML chunks because Chrome's HTTP
        // cache held onto them across opens (no service worker here
        // — pure manifest install). Force the HTML / route docs to
        // never cache; the underlying _next/static/* chunks keep
        // their long immutable cache because their filenames carry
        // a content hash.
        source: "/((?!_next/static|_next/image|api|.*\\.).*)",
        headers: [
          { key: "Cache-Control", value: "no-store, must-revalidate" },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
