/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  eslint:     { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: true },

  /**
   * Rewrites:
   *
   * DEV  → /api/:path* proxied directly to localhost:8000 by Next.js dev server
   * PROD → /api/:path* rewritten to /api/proxy/api/:path* so the App Router
   *         catch-all at /api/proxy/[...path] receives path segments that
   *         INCLUDE the leading "api" segment, preserving the full upstream path.
   *
   * Example:
   *   Browser:   GET /api/health/stream
   *   Rewrite:   GET /api/proxy/api/health/stream
   *   [...path]: ["api", "health", "stream"]
   *   Upstream:  BACKEND_URL/api/health/stream  ✓
   *
   * The source regex explicitly excludes /api/proxy/* to prevent an infinite
   * rewrite loop where /api/proxy/foo re-matches /api/:path* and loops forever.
   */
  async rewrites() {
    if (process.env.NODE_ENV === "development") {
      const backend = (
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
      ).replace(/\/+$/, "");
      return [
        { source: "/api/:path*", destination: `${backend}/api/:path*` },
      ];
    }

    // Production: rewrite /api/:path* → /api/proxy/api/:path*
    // The extra "api" segment ensures the proxy forwards /api/:path* upstream.
    // Excludes /api/proxy/* to avoid infinite rewrite loop.
    return [
      {
        source:      "/api/:path((?!proxy/).*)",
        destination: "/api/proxy/api/:path*",
      },
    ];
  },
};

export default nextConfig;
