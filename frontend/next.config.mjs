/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  eslint:     { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: true },

  /**
   * Rewrites:
   *
   * DEV  → /api/:path* and /health/:path* proxied directly to localhost:8000
   * PROD → rewritten to /api/proxy/:path* so the App Router
   *         proxy route at /api/proxy/[...path] forwards to Railway.
   *
   * The source regex for /api/* explicitly excludes /api/proxy/* to prevent
   * an infinite rewrite loop.
   */
  async rewrites() {
    if (process.env.NODE_ENV === "development") {
      const backend = (
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
      ).replace(/\/+$/, "");
      return [
        { source: "/api/:path*",    destination: `${backend}/api/:path*` },
        { source: "/health/:path*", destination: `${backend}/health/:path*` },
      ];
    }

    // Production: rewrite through the App Router proxy handler.
    // /api/proxy/* excluded to avoid infinite loop.
    return [
      {
        source: "/api/:path((?!proxy/).*)",
        destination: "/api/proxy/:path*",
      },
      {
        source: "/health/:path*",
        destination: "/api/proxy/health/:path*",
      },
    ];
  },
};

export default nextConfig;
