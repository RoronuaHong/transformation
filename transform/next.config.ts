import type { NextConfig } from "next";
import path from "path";

const API_UPSTREAM =
  process.env.VITUAL_API_UPSTREAM?.replace(/\/$/, "") || "http://127.0.0.1:8901";

const nextConfig: NextConfig = {
  outputFileTracingRoot: path.join(__dirname),
  async rewrites() {
    // Same-origin alias for the :8901 FastAPI upstream. The browser always calls
    // /ops-api (see lib/api-base.ts) to avoid extensions blocking cross-origin :8901.
    return [
      {
        source: "/ops-api/:path*",
        destination: `${API_UPSTREAM}/:path*`,
      },
    ];
  },
};

export default nextConfig;
