import type { NextConfig } from "next";
import path from "path";

const API_UPSTREAM =
  process.env.VITUAL_API_UPSTREAM?.replace(/\/$/, "") || "http://127.0.0.1:8900";

const nextConfig: NextConfig = {
  outputFileTracingRoot: path.join(__dirname),
  async rewrites() {
    // Optional same-origin alias: set NEXT_PUBLIC_API_URL=/ops-api
    // Prefer direct :8900 for long /api/try/probe (yt-dlp) to avoid proxy timeouts.
    return [
      {
        source: "/ops-api/:path*",
        destination: `${API_UPSTREAM}/:path*`,
      },
    ];
  },
};

export default nextConfig;
