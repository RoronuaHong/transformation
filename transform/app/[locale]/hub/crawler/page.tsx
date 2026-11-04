"use client";

import { useState } from "react";

const TOKEN = "local-admin"; // 临时默认值，后续接入登录态

type Store = {
  key: string;
  file: string;
  platform: string;
  path: string;
  exists: boolean;
  bytes?: number;
  mtime?: string;
  line_count?: number;
  domains?: string[];
  logged_in?: boolean | null;
};

export default function CrawlerPage() {
  const [stores, setStores] = useState<Store[] | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  async function loadCookies() {
    setLoading(true);
    setErr("");
    try {
      const r = await fetch("/ops-api/admin/crawler/cookies", {
        headers: { Authorization: "Bearer " + TOKEN },
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      setStores(d.stores ?? []);
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="home-page">
      <h1>爬虫管理控制台</h1>
      <p style={{ opacity: 0.7 }}>
        路由已建立（/zh/hub/crawler）。内容待补充；以下为「所有 Cookie」首次抓取结果。
      </p>
      <button onClick={loadCookies} disabled={loading}>
        {loading ? "加载中…" : "加载全部 Cookie"}
      </button>
      {err && <p style={{ color: "crimson" }}>{err}</p>}
      {stores && (
        <pre
          style={{
            marginTop: 16,
            background: "#111",
            padding: 12,
            borderRadius: 8,
            overflowX: "auto",
          }}
        >
          {JSON.stringify(stores, null, 2)}
        </pre>
      )}
    </main>
  );
}
