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
  images: {
    formats: ["image/avif", "image/webp"],
    remotePatterns: [{ protocol: "https", hostname: "**" }],
  },
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
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "geolocation=(), camera=(), microphone=()",
          },
        ],
      },
      {
        // PWA "shows old code" fix. Mirrors the admin app: the standalone
        // install was serving stale HTML because Chrome's HTTP cache
        // kept the index doc across opens (no service worker here —
        // pure manifest install). Force HTML / route docs to never
        // cache; _next/static/* keeps its long immutable cache because
        // those filenames embed a content hash.
        source: "/((?!_next/static|_next/image|api|.*\\.).*)",
        headers: [
          { key: "Cache-Control", value: "no-store, must-revalidate" },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
