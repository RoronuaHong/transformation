import type { NextConfig } from "next";
import path from "path";

const API_UPSTREAM =
  process.env.VITUAL_API_UPSTREAM?.replace(/\/$/, "") || "http://127.0.0.1:8901";

const HUB_UPSTREAM =
  process.env.VITUAL_HUB_UPSTREAM?.replace(/\/$/, "") || "http://127.0.0.1:3402";

// 生产(pm2 vitual-hub-web)与本地 next dev 共用 transform/ 目录。
// 若都用默认 .next，dev 会清掉生产构建的 BUILD_ID 与共享 chunk，
// 使 next start 的 /_next/static/* 全部 400、页面只剩 SSR 空壳。
// 故生产产物独立到 .next-prod，dev 继续用 .next；VITUAL_DIST_DIR 可覆盖。
const distDir =
  process.env.VITUAL_DIST_DIR ||
  (process.env.NODE_ENV === "production" ? ".next-prod" : ".next");

const nextConfig: NextConfig = {
  outputFileTracingRoot: path.join(__dirname),
  distDir,
  async rewrites() {
    // Same-origin: ops FastAPI + materials_hub Python API.
    // UI for materials lives at /[locale]/hub (Next), not the legacy :3402 HTML.
    // Vitual 专属端口段(pm2 托管): 3401=UI, 3402=hub API。
    // 避开 FunUnion(3000/8000-8005)、admin(3001)、MineTools(5180/8015)、vitual-api(8901)。
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
