/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  eslint:     { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: true },

  /**
   * Rewrites:
   *
   * DEV  → /api/:path*  proxied directly to localhost:8000 by Next.js dev server
   * PROD → /api/:path*  rewritten to /api/proxy/:path* so the App Router
   *         proxy route at /api/proxy/[...path]/route.ts forwards to Railway.
   *
   * This keeps api.ts using clean /api/auth/register style URLs everywhere
   * while correctly routing through the proxy on Vercel.
   */
  async rewrites() {
    if (process.env.NODE_ENV === "development") {
      const backend = (
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
      ).replace(/\/+$/, "");
      return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
    }

    // Production: rewrite /api/:path* → /api/proxy/:path*
    // Excludes /api/proxy itself to avoid infinite loop
    return [
      {
        source: "/api/:path*",
        destination: "/api/proxy/:path*",
      },
    ];
  },
};

export default nextConfig;
