/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  eslint:     { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: false },

  /**
   * Rewrite /api/* → Railway backend in LOCAL DEV ONLY.
   *
   * On Vercel, the /api/proxy/[...path]/route.ts API route handles this
   * server-side (so the Railway URL never leaks to the browser).
   *
   * In local dev, next dev doesn't run the proxy route as a real server
   * function, so we use next.config rewrites to forward to localhost:8000.
   */
  async rewrites() {
    const isDev = process.env.NODE_ENV === "development";
    if (!isDev) return [];

    const backend = (
      process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
    ).replace(/\/+$/, "");

    return [
      {
        source:      "/api/:path*",
        destination: `${backend}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
