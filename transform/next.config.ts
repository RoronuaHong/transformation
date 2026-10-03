import type { NextConfig } from "next";
import path from "path";

const API_UPSTREAM =
  process.env.VITUAL_API_UPSTREAM?.replace(/\/$/, "") || "http://127.0.0.1:8901";

const HUB_UPSTREAM =
  process.env.VITUAL_HUB_UPSTREAM?.replace(/\/$/, "") || "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  outputFileTracingRoot: path.join(__dirname),
  async rewrites() {
    // Same-origin: ops FastAPI + materials_hub Python API.
    // UI for materials lives at /[locale]/hub (Next), not the legacy :8000 HTML.
    return [
      {
        source: "/ops-api/:path*",
        destination: `${API_UPSTREAM}/:path*`,
      },
      {
        source: "/hub-api/:path*",
        destination: `${HUB_UPSTREAM}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
