/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  eslint:     { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: true },  // proxy route uses intentional any-casts

  /**
   * DEV ONLY: rewrite /api/* → localhost:8000 so `npm run dev` works
   * without needing the proxy route to act as a real HTTP server.
   * On Vercel (production) this block returns [] — the proxy route handles it.
   */
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    const backend = (
      process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
    ).replace(/\/+$/, "");
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
