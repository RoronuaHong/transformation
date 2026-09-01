"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { adminFetch, getToken, setToken } from "@/lib/api";

type Tab = "status" | "links" | "logs" | "alerts" | "run";

export default function AdminHome() {
  const [token, setTok] = useState("");
  const [authed, setAuthed] = useState(false);
  const [tab, setTab] = useState<Tab>("status");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [data, setData] = useState<unknown>(null);
  const [url, setUrl] = useState("");

  useEffect(() => {
    if (getToken()) setAuthed(true);
  }, []);

  const load = useCallback(async (t: Tab) => {
    setErr("");
    setMsg("");
    const path =
      t === "status"
        ? "/admin/status"
        : t === "links"
          ? "/admin/links"
          : t === "logs"
            ? "/admin/logs"
            : "/admin/alerts";
    try {
      const json = await adminFetch(path);
      setData(json);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  async function login(e: React.FormEvent) {
    e.preventDefault();
    setToken(token);
    setAuthed(true);
    await load("status");
  }

  async function post(path: string, body?: unknown) {
    setErr("");
    try {
      const json = await adminFetch(path, {
        method: "POST",
        body: body ? JSON.stringify(body) : "{}",
      });
      setMsg(JSON.stringify(json));
      await load(tab === "run" ? "status" : tab);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  const items = useMemo(() => {
    if (data && typeof data === "object" && "items" in data) {
      return (data as { items: Record<string, unknown>[] }).items || [];
    }
    return [];
  }, [data]);

  if (!authed) {
    return (
      <div className="wrap">
        <span className="badge">后台管理系统 · 非前台</span>
        <h1>Vitual Admin</h1>
        <p className="muted">前台读者站在 :3000；这里只给运营。Token 默认 local-admin。</p>
        <form className="card" onSubmit={login}>
          <label>Admin token</label>
          <input value={token} onChange={(e) => setTok(e.target.value)} />
          <p>
            <button type="submit">进入后台</button>
          </p>
        </form>
      </div>
    );
  }

  return (
    <div className="wrap">
      <span className="badge">后台管理系统</span>
      <h1>运营控制台</h1>
      <p className="muted">
        前台 SEO：<a href="http://127.0.0.1:3000">:3000</a>
        {" · "}
        API 文档：<a href="http://127.0.0.1:8901/admin/docs">:8901/admin/docs</a>
      </p>
      <nav className="tabs">
        {(["status", "links", "logs", "alerts", "run"] as Tab[]).map((t) => (
          <button
            key={t}
            className={tab === t ? "on" : ""}
            onClick={() => {
              setTab(t);
              if (t !== "run") void load(t);
            }}
          >
            {t === "status" ? "状态" : t === "links" ? "原始链接" : t === "logs" ? "日志" : t === "alerts" ? "告警" : "触发任务"}
          </button>
        ))}
      </nav>
      {err ? <p className="err">{err}</p> : null}
      {msg ? <p className="ok">{msg}</p> : null}

      {tab === "status" && data && typeof data === "object" ? (
        <pre className="card">{JSON.stringify(data, null, 2)}</pre>
      ) : null}

      {tab === "links" ? (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>原始链接</th>
                <th>canonical</th>
                <th>平台</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <tr key={String(row.video_id)}>
                  <td>{String(row.original_url || "")}</td>
                  <td>{String(row.canonical_url || "")}</td>
                  <td>
                    {String(row.platform)} / {String(row.video_id)}
                  </td>
                  <td>{String(row.status || "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {tab === "logs" ? (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>level</th>
                <th>action</th>
                <th>message</th>
              </tr>
            </thead>
            <tbody>
              {items.map((row, i) => (
                <tr key={String(row.id || i)}>
                  <td>{String(row.level)}</td>
                  <td>{String(row.action)}</td>
                  <td>{String(row.message)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {tab === "alerts" ? (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th>级别</th>
                <th>标题</th>
                <th>内容</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {items.map((row) => (
                <tr key={String(row.id)}>
                  <td className={row.severity === "error" ? "sev-error" : "sev-warn"}>
                    {String(row.severity)}
                  </td>
                  <td>{String(row.title)}</td>
                  <td>{String(row.message)}</td>
                  <td>
                    {row.acked ? (
                      "已确认"
                    ) : (
                      <button type="button" onClick={() => void post(`/admin/alerts/${row.id}/ack`)}>
                        确认
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {tab === "run" ? (
        <div className="card">
          <p className="muted">任务在 FastAPI 后台线程跑；失败会写日志并产生告警。</p>
          <div className="actions row">
            <button type="button" onClick={() => void post("/admin/discover", { topic: "life_hacks" })}>
              日更发现
            </button>
            <button type="button" onClick={() => void post("/admin/batch", { fast: true, limit: 1 })}>
              短加工
            </button>
            <button type="button" onClick={() => void post("/admin/export")}>
              导出站点
            </button>
          </div>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void post("/admin/inbox", { url, topic: "life_hacks" });
            }}
          >
            <label>原始链接入库</label>
            <div className="row">
              <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://..." />
              <button type="submit">inbox</button>
            </div>
          </form>
        </div>
      ) : null}
    </div>
  );
}
