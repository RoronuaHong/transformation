"use client";

import { useCallback, useRef, useState } from "react";

const API =
  process.env.NEXT_PUBLIC_KB_URL?.replace(/\/$/, "") || "http://127.0.0.1:8910";

type Msg = { role: "user" | "assistant"; content: string; sources?: { source: string; label: string }[] };

export default function AskPanel() {
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Msg[]>([]);
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const send = useCallback(async () => {
    const q = input.trim();
    if (!q || busy) return;
    setBusy(true);
    setInput("");
    const history = messages.map((m) => ({ role: m.role, content: m.content }));
    setMessages((m) => [...m, { role: "user", content: q }, { role: "assistant", content: "思考中…" }]);
    try {
      const r = await fetch(`${API}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, top_k: 5, history }),
      });
      const data = await r.json();
      setMessages((m) => {
        const next = [...m];
        next[next.length - 1] = { role: "assistant", content: data.answer, sources: data.sources };
        return next;
      });
    } catch (e) {
      setMessages((m) => {
        const next = [...m];
        next[next.length - 1] = { role: "assistant", content: "调用知识库失败，请确认本地服务已启动（kb/server.py）。" };
        return next;
      });
    } finally {
      setBusy(false);
      scrollRef.current?.scrollTo({ top: 1e9, behavior: "smooth" });
    }
  }, [input, busy, messages]);

  return (
    <div style={{ maxWidth: 720, margin: "0 auto", display: "flex", flexDirection: "column", height: "70vh" }}>
      <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: 16, display: "flex", flexDirection: "column", gap: 12 }}>
        {messages.length === 0 && (
          <div style={{ opacity: 0.6, textAlign: "center", marginTop: 40 }}>
            向「一桌」知识库提问，例如：周报怎么写？资产货架有哪些包？
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} style={{ alignSelf: m.role === "user" ? "flex-end" : "flex-start", maxWidth: "80%" }}>
            <div style={{ padding: "10px 14px", borderRadius: 12, background: m.role === "user" ? "#3b82f6" : "#1f2937", color: "#fff", whiteSpace: "pre-wrap" }}>
              {m.content}
            </div>
            {m.sources && m.sources.length > 0 && (
              <div style={{ fontSize: 11, opacity: 0.6, marginTop: 4 }}>
                参考：{m.sources.map((s, j) => `${j + 1}.${s.label}(${s.source})`).join("  ")}
              </div>
            )}
          </div>
        ))}
      </div>
      <div style={{ display: "flex", gap: 8, padding: 12, borderTop: "1px solid #333" }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="输入问题…"
          style={{ flex: 1, padding: 10, borderRadius: 8, border: "1px solid #444", background: "#111", color: "#fff" }}
        />
        <button onClick={send} disabled={busy} style={{ padding: "10px 18px", borderRadius: 8, border: 0, background: "#3b82f6", color: "#fff", cursor: "pointer" }}>
          发送
        </button>
      </div>
    </div>
  );
}
