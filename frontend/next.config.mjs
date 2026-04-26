/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  eslint:     { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: true },

  /**
   * Rewrites:
   *
   * DEV  → /api/:path* proxied directly to localhost:8000 by Next.js dev server
   * PROD → /api/:path* rewritten to /api/proxy/:path* so the App Router
   *         catch-all at /api/proxy/[...path] forwards to Railway.
   *
   * The proxy handler at /api/proxy/[...path]/route.ts prepends /api/ to the
   * upstream path so Railway receives the full correct path.
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

    // Production: rewrite /api/:path* → /api/proxy/:path*
    // Excludes /api/proxy/* to avoid infinite rewrite loop.
    // The proxy handler prepends /api/ before forwarding to Railway.
    return [
      {
        source:      "/api/:path((?!proxy/).*)",
        destination: "/api/proxy/:path*",
      },
    ];
  },
};

export default nextConfig;
