"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import type { HubCopy } from "@/lib/copy";
import { hubFetch, hubFileUrl, hubThumbUrl, hubUploadFile } from "@/lib/hub-api";

type Todo = { id?: number; text?: string; status?: string };
type AgentProgress = {
  done?: number;
  total?: number;
  pct?: number;
  label?: string;
};
type CropWindow = {
  id?: string;
  label?: string;
  start?: number;
  end?: number;
  duration?: number;
};
type AgentStatus = {
  task_id?: string;
  task?: string;
  status?: string;
  summary?: string;
  todos?: Todo[];
  steps?: number;
  result_ids?: string[];
  citations?: { claim?: string; id?: string }[];
  error?: string;
  reason?: string;
  running?: boolean;
  progress?: AgentProgress;
  skill?: string;
  crop_windows?: CropWindow[];
};

type Attachment = {
  localId: string;
  id: string;
  name: string;
  kind: string;
  status: string;
  previewUrl?: string;
  mime: string;
};

type Turn = {
  id: string;
  role: "user" | "assistant";
  content: string;
  attachments?: Attachment[];
  taskId?: string;
  status?: string;
  todos?: Todo[];
  steps?: number;
  live?: boolean;
  progress?: AgentProgress;
  skill?: string;
  cropWindows?: CropWindow[];
};

type ThreadSummary = {
  thread_id: string;
  title?: string;
  updated_at?: string;
  message_count?: number;
};

const TERMINAL = new Set([
  "done",
  "error",
  "skipped",
  "cancelled",
  "max_steps_reached",
]);

const MEDIA_ACCEPT = "image/*,video/*,.jpg,.jpeg,.png,.webp,.gif,.bmp,.mp4,.mov,.mkv,.webm,.avi";

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

function uid(prefix: string) {
  return `${prefix}_${Math.random().toString(36).slice(2, 9)}`;
}

function guessKind(file: File): string {
  const t = (file.type || "").toLowerCase();
  const n = file.name.toLowerCase();
  if (t.includes("gif") || n.endsWith(".gif")) return "anim";
  if (t.startsWith("image/") || /\.(jpe?g|png|webp|bmp)$/i.test(n)) return "images";
  if (t.startsWith("video/") || /\.(mp4|mov|mkv|webm|avi)$/i.test(n)) return "videos";
  return "other";
}

function formatLiveStatus(
  st: AgentStatus,
  runningLabel: string,
): string {
  const p = st.progress;
  if (p?.label) {
    const frac =
      typeof p.done === "number" && typeof p.total === "number" && p.total > 0
        ? ` · ${p.done}/${p.total}`
        : "";
    const pct =
      typeof p.pct === "number" && p.pct > 0 ? ` · ${p.pct}%` : "";
    return `${runningLabel}${frac}${pct}\n${p.label}`;
  }
  if (st.summary) return st.summary;
  return runningLabel + (st.steps ? ` · ${st.steps} steps` : "");
}

function formatReply(st: AgentStatus, fallback: string): string {
  if (st.error) return st.error;
  if (st.status === "skipped") {
    return st.reason ? `${fallback} (${st.reason})` : fallback;
  }
  const parts: string[] = [];
  if (st.summary) parts.push(st.summary);
  if (st.result_ids?.length) {
    parts.push(`ids: ${st.result_ids.join(", ")}`);
  }
  if (st.citations?.length) {
    parts.push(
      st.citations
        .map((c) => `· ${c.claim || ""}${c.id ? ` [${c.id}]` : ""}`)
        .join("\n"),
    );
  }
  if (!parts.length && st.status) parts.push(`status: ${st.status}`);
  return parts.join("\n\n") || fallback;
}

/** Skill D/E：把上传素材写成 Agent 可执行的结构化任务（id 真源，禁止幻觉）。 */
function buildAgentTask(userText: string, attachments: Attachment[]): string {
  const q = userText.trim();
  if (!attachments.length) return q;

  const lines = attachments.map(
    (a) =>
      `- id=${a.id} kind=${a.kind} name=${a.name} ingest=${a.status}`,
  );
  const hasImage = attachments.some(
    (a) => a.kind === "images" || a.kind === "anim",
  );
  const hasVideo = attachments.some(
    (a) => a.kind === "videos" || a.kind === "silent",
  );

  const rules: string[] = [
    "只使用下面真实存在的素材 id，禁止编造 id。",
  ];
  if (hasImage) {
    rules.push(
      "图片：优先 search_by_image(query=<id>, mode=auto)；无 CLIP 权重时自动降级 dHash/phash，不要空等下载。",
    );
  }
  if (hasVideo) {
    rules.push(
      "视频按方案 A 分析(每步换工具,禁止同参重复): ①get_material(id)一次 → ②related(id,rel=children) → ③get_shots(id)读 scenes 时间轴 → ④基于 start/end 给出裁剪区间建议后 finish。silent/audio 是组件不是剪辑切片;不要做设备兼容性测试(无此工具)。",
    );
  }

  const defaultAsk = hasImage
    ? "以图搜图：找出库里相似画面，汇报 id/name/距离或相似度，并简述差异。"
    : "分析这些上传视频：汇报元数据、关联素材、缺封面/缺镜头等体检要点。";

  return [
    "用户刚上传素材到素材中心（已入库）：",
    ...lines,
    "",
    "规则：",
    ...rules.map((r) => `- ${r}`),
    "",
    `用户请求：${q || defaultAsk}`,
  ].join("\n");
}

async function uploadMedia(file: File): Promise<Attachment> {
  const data = await hubUploadFile(file);
  if (data.status === "rejected" || !data.id) {
    // 拒收件没有 id,不能进入后续引用/追踪流程;blocked_ext 由调用方映射为人读文案
    throw new Error(
      data.reason === "blocked_ext" ? "blocked_ext" : data.error || "upload failed",
    );
  }
  const kind =
    data.path?.match(/materials[\\/]([^\\/]+)/)?.[1] || guessKind(file);
  const previewUrl = file.type.startsWith("image/")
    ? URL.createObjectURL(file)
    : undefined;
  return {
    localId: uid("f"),
    id: data.id!,
    name: file.name,
    kind,
    status: data.status || "added",
    previewUrl,
    mime: file.type || "",
  };
}

export function HubAgentPanel({ copy }: { copy: HubCopy }) {
  const params = useParams();
  const locale = typeof params?.locale === "string" ? params.locale : "zh";
  const titleId = useId();
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [modelReady, setModelReady] = useState<boolean | null>(null);
  const [modelName, setModelName] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [pending, setPending] = useState<Attachment[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [historyOpen, setHistoryOpen] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const abortPoll = useRef(false);
  const threadIdRef = useRef<string | null>(null);
  const turnsRef = useRef<Turn[]>([]);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    threadIdRef.current = threadId;
  }, [threadId]);
  useEffect(() => {
    turnsRef.current = turns;
  }, [turns]);

  const refreshThreads = useCallback(async () => {
    try {
      const data = await hubFetch<{ threads?: ThreadSummary[] }>("/agent/threads");
      setThreads(data?.threads || []);
    } catch {
      /* hub offline — keep current list */
    }
  }, []);

  const persistThread = useCallback(
    async (nextTurns: Turn[], existingId: string | null) => {
      if (!nextTurns.length) return existingId;
      const payload = nextTurns.map((t) => ({
        id: t.id,
        role: t.role,
        content: t.content,
        taskId: t.taskId,
        status: t.status,
        todos: t.todos,
        steps: t.steps,
        progress: t.progress,
        skill: t.skill,
        cropWindows: t.cropWindows,
        attachments: (t.attachments || []).map((a) => ({
          localId: a.localId,
          id: a.id,
          name: a.name,
          kind: a.kind,
          status: a.status,
          mime: a.mime,
        })),
      }));
      try {
        const saved = await hubFetch<{
          thread_id?: string;
          error?: string;
        }>("/agent/threads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            thread_id: existingId || undefined,
            messages: payload,
          }),
        });
        if (saved?.thread_id) {
          setThreadId(saved.thread_id);
          threadIdRef.current = saved.thread_id;
          void refreshThreads();
          return saved.thread_id;
        }
      } catch {
        /* keep local turns even if save fails */
      }
      return existingId;
    },
    [refreshThreads],
  );

  const scheduleSave = useCallback(
    (nextTurns: Turn[]) => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(() => {
        void persistThread(nextTurns, threadIdRef.current);
      }, 400);
    },
    [persistThread],
  );

  useEffect(() => {
    let cancelled = false;
    void hubFetch<{ model_ready?: boolean; model?: string }>("/agent/caps")
      .then((c) => {
        if (cancelled) return;
        setModelReady(Boolean(c?.model_ready));
        setModelName(c?.model || "");
      })
      .catch(() => {
        if (!cancelled) setModelReady(false);
      });
    void (async () => {
      try {
        const data = await hubFetch<{ threads?: ThreadSummary[] }>("/agent/threads");
        if (cancelled) return;
        const list = data?.threads || [];
        setThreads(list);
        const first = list[0];
        if (!first || threadIdRef.current || turnsRef.current.length) return;
        const full = await hubFetch<{
          thread_id?: string;
          messages?: Turn[];
          error?: string;
        }>(`/agent/threads?id=${encodeURIComponent(first.thread_id)}`);
        if (cancelled || full?.error || !full?.thread_id) return;
        const msgs = (full.messages || []).map((m) => ({ ...m, live: false }));
        setThreadId(full.thread_id);
        threadIdRef.current = full.thread_id;
        setTurns(msgs);
        turnsRef.current = msgs;
      } catch {
        /* hub offline */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    requestAnimationFrame(() => taRef.current?.focus());
  }, []);

  useEffect(() => {
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      for (const a of pending) {
        if (a.previewUrl) URL.revokeObjectURL(a.previewUrl);
      }
    };
  }, [pending]);

  const scrollBottom = useCallback(() => {
    requestAnimationFrame(() =>
      scrollRef.current?.scrollTo({
        top: scrollRef.current.scrollHeight,
        behavior: "smooth",
      }),
    );
  }, []);

  const resizeTa = useCallback(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  }, []);

  const stopTask = useCallback(async () => {
    abortPoll.current = true;
    const tid = activeTaskId;
    if (tid) {
      try {
        await hubFetch("/agent/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ task_id: tid }),
        });
      } catch {
        /* ignore */
      }
    }
  }, [activeTaskId]);

  const clearChat = useCallback(() => {
    if (busy) void stopTask();
    if (saveTimer.current) clearTimeout(saveTimer.current);
    setTurns([]);
    turnsRef.current = [];
    setThreadId(null);
    threadIdRef.current = null;
    setActiveTaskId(null);
    setInput("");
    setPending((prev) => {
      for (const a of prev) if (a.previewUrl) URL.revokeObjectURL(a.previewUrl);
      return [];
    });
  }, [busy, stopTask]);

  const openThread = useCallback(
    async (id: string) => {
      if (busy) return;
      try {
        const data = await hubFetch<{
          thread_id?: string;
          messages?: Turn[];
          error?: string;
        }>(`/agent/threads?id=${encodeURIComponent(id)}`);
        if (data?.error || !data?.thread_id) return;
        const msgs = (data.messages || []).map((m) => ({
          ...m,
          live: false,
          attachments: (m.attachments || []).map((a) => ({
            ...a,
            previewUrl: undefined,
          })),
        }));
        setThreadId(data.thread_id);
        threadIdRef.current = data.thread_id;
        setTurns(msgs);
        turnsRef.current = msgs;
        setActiveTaskId(null);
        scrollBottom();
      } catch {
        /* ignore */
      }
    },
    [busy, scrollBottom],
  );

  const deleteThread = useCallback(
    async (id: string) => {
      try {
        await hubFetch("/agent/threads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "delete", thread_id: id }),
        });
      } catch {
        /* ignore */
      }
      if (threadIdRef.current === id) {
        setTurns([]);
        turnsRef.current = [];
        setThreadId(null);
        threadIdRef.current = null;
      }
      void refreshThreads();
    },
    [refreshThreads],
  );

  const onPickFiles = useCallback(
    async (files: FileList | null) => {
      if (!files?.length || busy || uploading) return;
      setUploading(true);
      try {
        const next: Attachment[] = [];
        for (const f of Array.from(files)) {
          if (!f.type.startsWith("image/") && !f.type.startsWith("video/")) {
            const k = guessKind(f);
            if (k !== "images" && k !== "anim" && k !== "videos") continue;
          }
          next.push(await uploadMedia(f));
        }
        if (next.length) setPending((p) => [...p, ...next]);
      } catch (e) {
        const msg = e instanceof Error ? e.message : "";
        const body =
          msg === "blocked_ext"
            ? copy.agentUploadRejected
            : e instanceof Error
              ? `${copy.agentAttachFail}: ${msg}`
              : copy.agentAttachFail;
        setTurns((prev) => [
          ...prev,
          {
            id: uid("a"),
            role: "assistant",
            content: body,
            status: "error",
          },
        ]);
        scrollBottom();
      } finally {
        setUploading(false);
        if (fileRef.current) fileRef.current.value = "";
      }
    },
    [busy, copy.agentAttachFail, copy.agentUploadRejected, scrollBottom, uploading],
  );

  const removePending = useCallback((localId: string) => {
    setPending((prev) => {
      const hit = prev.find((a) => a.localId === localId);
      if (hit?.previewUrl) URL.revokeObjectURL(hit.previewUrl);
      return prev.filter((a) => a.localId !== localId);
    });
  }, []);

  const runTask = useCallback(
    async (task: string, extraAttachments?: Attachment[]) => {
      const attachments = extraAttachments ?? pending;
      const display = task.trim();
      if ((!display && !attachments.length) || busy || uploading) return;

      const agentTask = buildAgentTask(display, attachments);
      abortPoll.current = false;
      setBusy(true);
      setInput("");
      requestAnimationFrame(resizeTa);

      const attached = [...attachments];
      setPending([]);

      const userId = uid("u");
      const botId = uid("a");
      const commit = (
        updater: (prev: Turn[]) => Turn[],
        save: "now" | "later" | false,
      ) => {
        const next = updater(turnsRef.current);
        turnsRef.current = next;
        setTurns(next);
        if (save === "now") void persistThread(next, threadIdRef.current);
        else if (save === "later") scheduleSave(next);
      };
      commit(
        (prev) => [
          ...prev,
          {
            id: userId,
            role: "user",
            content:
              display ||
              (attached.some((a) => a.kind === "images" || a.kind === "anim")
                ? copy.agentDefaultImgAsk
                : copy.agentDefaultVidAsk),
            attachments: attached,
          },
          {
            id: botId,
            role: "assistant",
            content: copy.agentRunning,
            live: true,
            status: "planning",
            todos: [],
          },
        ],
        "now",
      );
      scrollBottom();

      try {
        const start = await hubFetch<{
          task_id?: string;
          running?: boolean;
          error?: string;
          job?: { task_id?: string };
        }>("/agent", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            task: agentTask,
            allow_write: /打标|写入|登记|打标签|改标签|update_tags|register_asset/.test(
              agentTask,
            ),
            max_steps: 12,
            thread_id: threadIdRef.current || undefined,
          }),
        });
        if (start.error && !start.task_id) {
          const existing = start.job?.task_id;
          if (!existing) throw new Error(start.error);
        }
        const tid = start.task_id || start.job?.task_id;
        if (!tid) throw new Error(copy.error);
        setActiveTaskId(tid);

        let last: AgentStatus = { task_id: tid, status: "planning" };
        for (let i = 0; i < 180; i++) {
          if (abortPoll.current) break;
          await sleep(i === 0 ? 350 : 1000);
          last = await hubFetch<AgentStatus>(
            `/agent?task_id=${encodeURIComponent(tid)}`,
          );
          const terminal = Boolean(last.status && TERMINAL.has(last.status));
          commit(
            (prev) =>
              prev.map((t) =>
                t.id === botId
                  ? {
                      ...t,
                      taskId: tid,
                      status: last.status || t.status,
                      todos: last.todos || t.todos,
                      steps: last.steps,
                      progress: last.progress || t.progress,
                      skill: last.skill || t.skill,
                      cropWindows: last.crop_windows || t.cropWindows,
                      content: terminal
                        ? formatReply(last, copy.agentNoModel)
                        : formatLiveStatus(last, copy.agentRunning),
                      live: !terminal,
                    }
                  : t,
              ),
            terminal ? "now" : i % 4 === 0 ? "later" : false,
          );
          scrollBottom();
          if (terminal) break;
        }

        if (abortPoll.current && !(last.status && TERMINAL.has(last.status))) {
          commit(
            (prev) =>
              prev.map((t) =>
                t.id === botId
                  ? {
                      ...t,
                      live: false,
                      status: "cancelled",
                      content: t.content || "cancelled",
                    }
                  : t,
              ),
            "now",
          );
        }
      } catch (e) {
        commit(
          (prev) =>
            prev.map((t) =>
              t.id === botId
                ? {
                    ...t,
                    live: false,
                    status: "error",
                    content: e instanceof Error ? e.message : copy.error,
                  }
                : t,
            ),
          "now",
        );
      } finally {
        setBusy(false);
        setActiveTaskId(null);
        void persistThread(turnsRef.current, threadIdRef.current);
        scrollBottom();
        taRef.current?.focus();
      }
    },
    [
      busy,
      copy.agentDefaultImgAsk,
      copy.agentDefaultVidAsk,
      copy.agentNoModel,
      copy.agentRunning,
      copy.error,
      pending,
      persistThread,
      resizeTa,
      scheduleSave,
      scrollBottom,
      uploading,
    ],
  );

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void runTask(input);
    }
  };

  const suggestions = [
    { label: copy.agentSugCover, task: copy.agentSugCoverTask },
    { label: copy.agentSugDupes, task: copy.agentSugDupesTask },
    { label: copy.agentSugMaintain, task: copy.agentSugMaintainTask },
    { label: copy.agentSugSegment, task: copy.agentSugSegmentTask },
  ];

  const canSend = Boolean(input.trim() || pending.length) && !uploading;

  return (
    <div className="hub-page hub-agent-page">
      <section
        className={`hub-chat hub-chat--page${historyOpen ? " hist-open" : ""}`}
        aria-labelledby={titleId}
      >
        <aside className="hub-chat-hist" aria-label={copy.agentChats}>
          <div className="hub-chat-hist-head">
            <span>{copy.agentChats}</span>
            <button type="button" className="hub-chat-iconbtn" onClick={clearChat}>
              {copy.agentNew}
            </button>
          </div>
          <ul className="hub-chat-hist-list">
            {threads.length === 0 ? (
              <li className="hub-chat-hist-empty muted">{copy.agentUntitled}</li>
            ) : (
              threads.map((th) => (
                <li key={th.thread_id}>
                  <button
                    type="button"
                    className={`hub-chat-hist-item${threadId === th.thread_id ? " on" : ""}`}
                    disabled={busy}
                    onClick={() => void openThread(th.thread_id)}
                  >
                    <span className="hub-chat-hist-title">
                      {th.title || copy.agentUntitled}
                    </span>
                    <span className="hub-chat-hist-meta">
                      {th.updated_at || ""}
                      {typeof th.message_count === "number"
                        ? ` · ${th.message_count}`
                        : ""}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="hub-chat-hist-del"
                    aria-label={copy.agentDeleteThread}
                    disabled={busy}
                    onClick={() => void deleteThread(th.thread_id)}
                  >
                    ✕
                  </button>
                </li>
              ))
            )}
          </ul>
        </aside>
        <div className="hub-chat-main">
        <header className="hub-chat-head">
          <div className="hub-chat-brand">
            <span className="hub-chat-avatar" aria-hidden>
              AI
            </span>
            <div>
              <h1 id={titleId}>{copy.agentHeadline}</h1>
              <p className="hub-chat-status">
                <span
                  className={`hub-chat-dot${modelReady ? " on" : modelReady === false ? " off" : ""}`}
                />
                {modelReady
                  ? `${copy.agentOnline}${modelName ? ` · ${modelName}` : ""}`
                  : modelReady === false
                    ? copy.agentOffline
                    : "…"}
              </p>
            </div>
          </div>
          <div className="hub-chat-head-acts">
            <Link className="hub-chat-iconbtn" href={`/${locale}/hub`}>
              {copy.agentBack}
            </Link>
            <button
              type="button"
              className="hub-chat-iconbtn"
              onClick={() => setHistoryOpen((v) => !v)}
            >
              {copy.agentChats}
            </button>
            <button
              type="button"
              className="hub-chat-iconbtn"
              onClick={clearChat}
              disabled={busy && !activeTaskId}
            >
              {copy.agentNew}
            </button>
          </div>
        </header>

        {modelReady === false ? (
          <p className="hub-chat-banner">{copy.agentNoModel}</p>
        ) : null}

        <div className="hub-chat-scroll" ref={scrollRef} aria-live="polite">
          {turns.length === 0 ? (
            <div className="hub-chat-empty">
              <p>{copy.agentEmpty}</p>
              <p className="muted">{copy.agentAttachHint}</p>
              <div className="hub-chat-sugs">
                {suggestions.map((s) => (
                  <button
                    key={s.label}
                    type="button"
                    className="hub-chat-sug"
                    disabled={busy}
                    onClick={() => void runTask(s.task, [])}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            turns.map((t) => (
              <div
                key={t.id}
                className={`hub-chat-row ${t.role === "user" ? "user" : "bot"}`}
              >
                {t.role === "assistant" ? (
                  <span className="hub-chat-mini-av" aria-hidden>
                    AI
                  </span>
                ) : null}
                <div className="hub-chat-block">
                  {t.attachments && t.attachments.length > 0 ? (
                    <div className="hub-chat-atts">
                      {t.attachments.map((a) => (
                        <a
                          key={a.localId}
                          className="hub-chat-att"
                          href={hubFileUrl(a.id)}
                          target="_blank"
                          rel="noreferrer"
                          title={`${a.name} (${a.id})`}
                        >
                          {a.kind === "images" || a.kind === "anim" ? (
                            // eslint-disable-next-line @next/next/no-img-element
                            <img
                              src={a.previewUrl || hubThumbUrl(a.id)}
                              alt={a.name}
                            />
                          ) : (
                            <span className="hub-chat-att-vid">VIDEO</span>
                          )}
                          <span className="hub-chat-att-meta">
                            {a.kind} · {a.id}
                          </span>
                        </a>
                      ))}
                    </div>
                  ) : null}
                  <div
                    className={`hub-chat-bubble ${t.role === "user" ? "user" : "bot"}${t.live ? " live" : ""}`}
                  >
                    {t.content}
                  </div>
                  {t.cropWindows && t.cropWindows.length > 0 ? (
                    <ul className="hub-chat-crops">
                      {t.cropWindows.map((w, j) => (
                        <li key={`${w.id || "w"}-${j}`}>
                          <b>{w.label || "裁剪窗"}</b>
                          <span>
                            {Number(w.start ?? 0).toFixed(2)}–{Number(w.end ?? 0).toFixed(2)}s
                            {typeof w.duration === "number"
                              ? ` · ${w.duration.toFixed(1)}s`
                              : ""}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {t.live || (t.progress && t.progress.total) ? (
                    <div
                      className={`hub-chat-progress${t.live ? " live" : ""}`}
                      aria-label={
                        t.progress?.label ||
                        copy.agentRunning
                      }
                    >
                      <div className="hub-chat-progress-track">
                        <i
                          style={{
                            width: `${Math.min(100, Math.max(0, t.progress?.pct ?? (t.live ? 8 : 100)))}%`,
                          }}
                        />
                      </div>
                      <p className="hub-chat-progress-label">
                        {t.progress?.label ||
                          (t.live ? copy.agentRunning : t.status || "")}
                        {typeof t.progress?.done === "number" &&
                        typeof t.progress?.total === "number" &&
                        t.progress.total > 0
                          ? ` · ${t.progress.done}/${t.progress.total}`
                          : typeof t.steps === "number"
                            ? ` · ${t.steps} steps`
                            : ""}
                        {typeof t.progress?.pct === "number"
                          ? ` · ${t.progress.pct}%`
                          : ""}
                      </p>
                    </div>
                  ) : null}
                  {t.todos && t.todos.length > 0 ? (
                    <ul className="hub-chat-todos">
                      {t.todos.map((td, j) => (
                        <li
                          key={j}
                          className={`hub-chat-todo ${(td.status || "").toLowerCase()}`}
                        >
                          <span className="hub-chat-todo-mark" aria-hidden>
                            {td.status === "done"
                              ? "✓"
                              : td.status === "running" ||
                                  td.status === "in_progress"
                                ? "…"
                                : "○"}
                          </span>
                          {td.text}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {t.taskId && !t.live && t.skill !== "chitchat" && t.skill !== "offtopic" && t.skill !== "clarify" ? (
                    <p className="hub-chat-meta">
                      {t.status}
                      {t.skill ? ` · ${t.skill}` : ""}
                      {typeof t.steps === "number"
                        ? ` · ${t.steps} steps`
                        : ""}{" "}
                      · {t.taskId}
                    </p>
                  ) : null}
                </div>
              </div>
            ))
          )}
        </div>

        <form
          className="hub-chat-composer"
          onSubmit={(e) => {
            e.preventDefault();
            void runTask(input);
          }}
          onDragOver={(e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = "copy";
          }}
          onDrop={(e) => {
            e.preventDefault();
            void onPickFiles(e.dataTransfer.files);
          }}
        >
          {pending.length > 0 ? (
            <div className="hub-chat-pending">
              {pending.map((a) => (
                <div key={a.localId} className="hub-chat-pending-item">
                  {a.previewUrl ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={a.previewUrl} alt={a.name} />
                  ) : (
                    <span className="hub-chat-att-vid">VIDEO</span>
                  )}
                  <span className="hub-chat-pending-name" title={a.id}>
                    {a.name}
                  </span>
                  <button
                    type="button"
                    className="hub-chat-pending-x"
                    aria-label={copy.close}
                    onClick={() => removePending(a.localId)}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          ) : null}

          <textarea
            ref={taRef}
            rows={1}
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              resizeTa();
            }}
            onKeyDown={onKeyDown}
            placeholder={copy.agentPh}
            aria-label={copy.agentPh}
          />
          <div className="hub-chat-toolbar">
            <button
              type="button"
              className="hub-chat-attach"
              disabled={busy || uploading}
              onClick={() => fileRef.current?.click()}
            >
              {uploading ? copy.agentUploading : copy.agentAttach}
            </button>
            <input
              ref={fileRef}
              type="file"
              accept={MEDIA_ACCEPT}
              multiple
              hidden
              onChange={(e) => void onPickFiles(e.target.files)}
            />
            <span className="hub-chat-hint muted">{copy.agentHint}</span>
            <span className="hub-chat-sp" />
            {busy ? (
              <button
                type="button"
                className="hub-chat-send stop"
                onClick={() => void stopTask()}
              >
                {copy.agentStop}
              </button>
            ) : (
              <button type="submit" className="hub-chat-send" disabled={!canSend}>
                {copy.agentSend}
              </button>
            )}
          </div>
        </form>
        </div>
      </section>
    </div>
  );
}
