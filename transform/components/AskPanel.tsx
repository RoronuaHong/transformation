"use client";

import { useCallback, useRef, useState } from "react";

const API =
  process.env.NEXT_PUBLIC_KB_URL?.replace(/\/$/, "") || "http://127.0.0.1:8910";

type Source = { source: string; label: string };
type Msg = { role: "user" | "assistant"; content: string; sources?: Source[] };

export default function AskPanel({ placeholder }: { placeholder?: string }) {
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
    setMessages((prev) => [...prev, { role: "user", content: q }]);

    try {
      const r = await fetch(`${API}/ask`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, top_k: 5, history }),
      });
      const data = await r.json();
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: data.answer ?? "（无回答）", sources: data.sources },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "调用知识库失败，请确认本地服务已启动（kb/server.py，端口 8910）。" },
      ]);
    } finally {
      setBusy(false);
      requestAnimationFrame(() =>
        scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" })
      );
    }
  }, [input, busy, messages]);

  return (
    <section className="ask-panel" aria-label="问问一桌">
      <div className="ask-scroll" ref={scrollRef} aria-live="polite">
        {messages.length === 0 ? (
          <p className="ask-empty">{placeholder ?? "向知识库提问，例如：有哪些职场资产包？分别多少钱？"}</p>
        ) : (
          messages.map((m, i) => (
            <div key={i} className={`ask-row ${m.role === "user" ? "user" : "bot"}`}>
              <div className="ask-block">
                <div className={`ask-bubble ${m.role === "user" ? "user" : "bot"}`}>{m.content}</div>
                {m.sources && m.sources.length > 0 && (
                  <p className="ask-sources">
                    参考：
                    {m.sources.map((s, j) => `${j + 1}. ${s.label}（${s.source}）`).join("　")}
                  </p>
                )}
              </div>
            </div>
          ))
        )}
        {busy && <p className="ask-empty">思考中…</p>}
      </div>

      <form
        className="ask-input"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="输入问题…"
          aria-label="问题"
        />
        <button type="submit" disabled={busy || !input.trim()}>
          发送
        </button>
      </form>
    </section>
  );
}
