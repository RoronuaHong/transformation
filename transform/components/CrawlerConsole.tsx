"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

/**
 * 素材采集控制台 —— 从 materials_hub/static/crawler.html 迁移而来。
 * 后端走 transform 的 /ops-api 网关（next.config.ts 已代理到 :8901 ops-api），
 * 同源调用，Admin Token 默认 local-admin。
 *
 * 流程：4 平台（B站/抖音/YouTube/Telegram）关键词搜索 → 候选卡片 →
 * 人工勾选 → 批量加入加工队列。修改后端接口时同步本文件。
 */

const API_BASE = "/ops-api/admin/crawler";
const TOKEN_KEY = "vitual_crawler_token";
const HISTORY_KEY = "vitual_crawler_history";
const HISTORY_LIMIT = 8;
const EXAMPLE_QUERIES = ["露营", "AI 教程", "街头美食", "收纳技巧", "城市骑行"];
const SEARCH_TIMEOUT_MS = 65_000;

type Suggestion = { text: string; group: "recent" | "example" };

function normalizeKeyword(s: string): string {
  return s.normalize("NFKC").replace(/\s+/g, " ").trim().slice(0, 80);
}
function readHistory(): string[] {
  if (typeof localStorage === "undefined") return [];
  try {
    const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]");
    return Array.isArray(raw)
      ? raw.filter((x): x is string => typeof x === "string").slice(0, HISTORY_LIMIT)
      : [];
  } catch {
    return [];
  }
}
function saveHistory(items: string[]) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items));
  } catch {
    /* 隐私模式下 localStorage 不可用，历史仅保留在本次会话 */
  }
}
/** 视觉区分「用户已输入」与「系统补充」的部分（NN/g 搜索建议准则）。 */
function highlightQuery(text: string, query: string) {
  const q = query.trim();
  const at = q ? text.toLowerCase().indexOf(q.toLowerCase()) : -1;
  if (at < 0) return <>{text}</>;
  return (
    <>
      {at > 0 ? <strong className="sug-rest">{text.slice(0, at)}</strong> : null}
      <span className="sug-hit">{text.slice(at, at + q.length)}</span>
      {at + q.length < text.length ? <strong className="sug-rest">{text.slice(at + q.length)}</strong> : null}
    </>
  );
}

type PlatKey = "bilibili" | "douyin" | "youtube" | "telegram";
const PLAT: Record<PlatKey, { label: string; short: string; color: string; sub: string; tok?: boolean }> = {
  bilibili: { label: "哔哩哔哩", short: "B", color: "var(--bilibili)", sub: "公开接口·直接搜索" },
  douyin: { label: "抖音", short: "抖", color: "var(--douyin)", sub: "需登录 Token", tok: true },
  youtube: { label: "YouTube", short: "YT", color: "var(--youtube)", sub: "需 API Key·经代理", tok: true },
  telegram: { label: "Telegram", short: "TG", color: "var(--telegram)", sub: "需填公开频道" },
};
const KEYS = Object.keys(PLAT) as PlatKey[];

type Cap = { key: string; ready?: boolean; needs_login?: boolean; needs_channels?: boolean; needs_token?: boolean };
type Note = { key: string; label?: string; note?: string; error?: string; count?: number };
type Item = {
  id: string;
  platform: string;
  title?: string;
  author?: string;
  views?: number;
  published_at?: number;
  duration_sec?: number;
  thumb_url?: string;
  url?: string;
  original_url?: string;
};
type Recent = {
  platform?: string;
  video_id?: string;
  title?: string;
  status?: string;
  canonical_url?: string;
  original_url?: string;
  created_at?: number;
  updated_at?: number;
};
type RecentAction = "pause" | "resume" | "reprocess" | "delete";

const RECENT_STATUS: Record<string, { label: string; cls: string }> = {
  pending: { label: "待处理", cls: "pending" },
  processing: { label: "处理中", cls: "processing" },
  cancelled: { label: "已暂停", cls: "paused" },
  failed: { label: "失败", cls: "error" },
  dead: { label: "已终止", cls: "error" },
  done: { label: "已完成", cls: "done" },
  published: { label: "已发布", cls: "done" },
};
const RECENT_FILTERS = [
  { key: "all", label: "全部" },
  { key: "active", label: "待处理" },
  { key: "paused", label: "已暂停" },
  { key: "done", label: "已完成" },
  { key: "error", label: "失败" },
] as const;
type RecentFilter = (typeof RECENT_FILTERS)[number]["key"];

/** 抽屉里的 id 与后端 action 端点的 (platform, video_id) 一一对应。 */
function recentId(r: Recent): string {
  return `${r.platform || ""}:${r.video_id || ""}`;
}
function recentGroup(status?: string): RecentFilter {
  const s = String(status || "");
  if (s === "pending" || s === "processing") return "active";
  if (s === "cancelled") return "paused";
  if (s === "done" || s === "published") return "done";
  if (s === "failed" || s === "dead") return "error";
  return "all";
}
function canDoRecent(r: Recent, action: RecentAction): boolean {
  const s = String(r.status || "");
  if (action === "delete") return true;
  if (action === "pause") return s === "pending";
  if (action === "resume") return s === "cancelled" || s === "failed" || s === "dead";
  return s === "done" || s === "published";
}
function fmtWhen(ts?: number): string {
  const n = Number(ts);
  if (!n || !isFinite(n)) return "";
  return new Date(n * 1000).toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function esc(s: unknown): string {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string),
  );
}
function decodeEntities(s: unknown): string {
  if (typeof document === "undefined") return String(s == null ? "" : s);
  const box = document.createElement("textarea");
  box.innerHTML = String(s == null ? "" : s);
  return box.value;
}
function safeUrl(u: unknown): string {
  const s = String(u || "");
  return /^https?:\/\//i.test(s) ? s : "#";
}
function safeThumb(u: unknown): string {
  let s = String(u || "");
  if (s.startsWith("//")) s = "https:" + s;
  return /^https?:\/\//i.test(s) ? s : "";
}
function fmtViews(v: unknown): string {
  const n = Number(v);
  if (v == null || !isFinite(n)) return "";
  if (n >= 1e8) return (n / 1e8).toFixed(1) + "亿播放";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + "万播放";
  return n + " 播放";
}
function fmtDate(ts: unknown): string {
  const n = Number(ts);
  if (!ts || !isFinite(n)) return "";
  return new Date(n * 1000).toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
}
function fmtDuration(sec: unknown): string {
  const n = Number(sec);
  if (!sec || !isFinite(n)) return "";
  const s = Math.floor(n);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const pad = (x: number) => String(x).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(r)}` : `${m}:${pad(r)}`;
}

export default function CrawlerConsole() {
  const [token, setToken] = useState<string>("local-admin");
  const [mode, setMode] = useState<"idle" | "loading" | "done" | "error">("idle");
  const [platforms, setPlatforms] = useState<Record<string, boolean>>(
    () => Object.fromEntries(KEYS.map((k) => [k, true])),
  );
  const [caps, setCaps] = useState<Cap[]>([]);
  const [results, setResults] = useState<Item[]>([]);
  const [notes, setNotes] = useState<Note[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [imported, setImported] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<string>("all");
  const [keyword, setKeyword] = useState("");
  const [quota, setQuota] = useState("12");
  const [tgChannels, setTgChannels] = useState("");
  const [ytToken, setYtToken] = useState("");
  const [dyToken, setDyToken] = useState("");
  const [recentCount, setRecentCount] = useState<number | "—">("—");
  const [recentItems, setRecentItems] = useState<Recent[]>([]);
  const [recentSelected, setRecentSelected] = useState<Set<string>>(new Set());
  const [recentFilter, setRecentFilter] = useState<RecentFilter>("all");
  const [recentBusy, setRecentBusy] = useState(false);
  const [recentError, setRecentError] = useState("");
  const [deleteIds, setDeleteIds] = useState<Set<string>>(new Set());
  const [bulkDeleteConfirm, setBulkDeleteConfirm] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);
  const [authMsg, setAuthMsg] = useState("需要 Admin Token");
  const [authInput, setAuthInput] = useState("local-admin");
  const [searching, setSearching] = useState(false);
  const [importing, setImporting] = useState(false);
  const [toasts, setToasts] = useState<{ id: number; msg: string; type: string }[]>([]);
  const [history, setHistory] = useState<string[]>([]);
  const [suggestOpen, setSuggestOpen] = useState(false);
  const [activeSuggest, setActiveSuggest] = useState(-1);
  const [lastQuery, setLastQuery] = useState("");
  const [lastDuration, setLastDuration] = useState<number | null>(null);
  const [errorDetail, setErrorDetail] = useState("");
  const [sortBy, setSortBy] = useState<"relevance" | "views" | "latest">("relevance");
  const abortRef = useRef<AbortController | null>(null);
  const seqRef = useRef(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const comboRef = useRef<HTMLDivElement | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const tid = useMemo(() => ({ v: 0 }), []);
  const toast = useCallback((msg: string, type = "info") => {
    const id = ++tid.v;
    setToasts((t) => [...t, { id, msg, type }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4600);
  }, [tid]);

  const api = useCallback(
    async (path: string, opts: RequestInit = {}) => {
      const r = await fetch(`${API_BASE}${path}`, {
        ...opts,
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + token,
          ...(opts.headers || {}),
        },
      });
      if (r.status === 401) {
        setAuthMsg("Token 无效（默认 local-admin）。修改后点「保存并重试」。");
        setAuthInput(token);
        setAuthOpen(true);
        throw new Error("401 未授权");
      }
      if (!r.ok) throw new Error(r.status + " " + (await r.text()).slice(0, 200));
      return r.json();
    },
    [token],
  );

  const loadCaps = useCallback(async () => {
    try {
      const j = await api("/capabilities");
      setCaps(j.platforms || []);
    } catch {
      /* 保持默认 chip 状态 */
    }
  }, [api]);

  const loadRecent = useCallback(async (): Promise<Recent[]> => {
    try {
      const j = await api(`/links?limit=30`);
      const items: Recent[] = j.items || [];
      const alive = new Set(items.map(recentId));
      setRecentCount(items.length);
      setRecentItems(items);
      setRecentError("");
      setRecentSelected((prev) => new Set([...prev].filter((id) => alive.has(id))));
      setDeleteIds((prev) => new Set([...prev].filter((id) => alive.has(id))));
      return items;
    } catch (e) {
      setRecentCount("—");
      setRecentError("读取入库记录失败：" + (e as Error).message);
      return [];
    }
  }, [api]);

  useEffect(() => {
    document.title = "素材采集 · Vitual";
    const saved = typeof localStorage !== "undefined" ? localStorage.getItem(TOKEN_KEY) : null;
    if (saved) setToken(saved);
    setHistory(readHistory());
    loadCaps();
    void loadRecent();
  }, [loadCaps, loadRecent]);

  const applyRecentAction = useCallback(
    async (action: RecentAction, ids: string[]) => {
      if (!ids.length || recentBusy) return;
      const byId = new Map(recentItems.map((r) => [recentId(r), r]));
      const payload = ids
        .map((id) => byId.get(id))
        .filter((r): r is Recent => !!r)
        .map((r) => ({ platform: String(r.platform || ""), video_id: String(r.video_id || "") }))
        .filter((x) => x.platform && x.video_id);
      if (!payload.length) return;
      setRecentBusy(true);
      try {
        const j = await api("/links/action", {
          method: "POST",
          body: JSON.stringify({ action, items: payload, limit: 30 }),
        });
        const succeeded: string[] = Array.isArray(j.succeeded) ? j.succeeded : [];
        const skipped: unknown[] = Array.isArray(j.skipped) ? j.skipped : [];
        const failed: unknown[] = Array.isArray(j.failed) ? j.failed : [];
        if (Array.isArray(j.items)) {
          setRecentItems(j.items as Recent[]);
          setRecentCount((j.items as Recent[]).length);
        } else {
          await loadRecent();
        }
        const done = new Set(succeeded);
        // 失败 / 跳过项保留选中，方便换一种操作重试（bulk-actions 反馈规范）。
        setRecentSelected((prev) => new Set([...prev].filter((id) => !done.has(id))));
        setDeleteIds(new Set());
        setBulkDeleteConfirm(false);
        const label = { pause: "暂停", resume: "继续", reprocess: "重新处理", delete: "删除" }[action];
        const parts = [`${label}：成功 ${succeeded.length} 项`];
        if (skipped.length) parts.push(`跳过 ${skipped.length} 项`);
        if (failed.length) parts.push(`失败 ${failed.length} 项`);
        toast(parts.join("，"), failed.length ? "error" : skipped.length ? "warn" : "success");
      } catch (e) {
        toast("操作失败：" + (e as Error).message, "error");
      } finally {
        setRecentBusy(false);
      }
    },
    [recentBusy, recentItems, api, toast, loadRecent],
  );

  const visibleRecent = useMemo(
    () => recentItems.filter((r) => recentFilter === "all" || recentGroup(r.status) === recentFilter),
    [recentItems, recentFilter],
  );
  const selectedRecentRows = useMemo(
    () => recentItems.filter((r) => recentSelected.has(recentId(r))),
    [recentItems, recentSelected],
  );
  const allVisiblePicked = visibleRecent.length > 0 && visibleRecent.every((r) => recentSelected.has(recentId(r)));
  const recentFilterCounts = useMemo(() => {
    const c: Record<string, number> = { all: recentItems.length };
    RECENT_FILTERS.forEach((f) => {
      if (f.key !== "all") c[f.key] = recentItems.filter((r) => recentGroup(r.status) === f.key).length;
    });
    return c;
  }, [recentItems]);
  const bulkRecentActions = useMemo(() => {
    const acts: { action: RecentAction; label: string }[] = [];
    if (selectedRecentRows.some((r) => canDoRecent(r, "resume"))) acts.push({ action: "resume", label: "继续" });
    if (selectedRecentRows.some((r) => canDoRecent(r, "pause"))) acts.push({ action: "pause", label: "暂停" });
    return acts;
  }, [selectedRecentRows]);

  const toggleRecentSelect = useCallback((id: string) => {
    setRecentSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const setDeleteConfirm = useCallback((id: string, on: boolean) => {
    setDeleteIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);
  const toggleAllVisibleRecent = useCallback(() => {
    setRecentSelected((prev) => {
      const next = new Set(prev);
      visibleRecent.forEach((r) => {
        const id = recentId(r);
        if (allVisiblePicked) next.delete(id);
        else next.add(id);
      });
      return next;
    });
  }, [visibleRecent, allVisiblePicked]);
  const changeRecentFilter = useCallback((key: RecentFilter) => {
    setRecentFilter(key);
    setRecentSelected(new Set());
    setDeleteIds(new Set());
    setBulkDeleteConfirm(false);
  }, []);
  const runBulkRecent = useCallback(
    (action: RecentAction) => {
      const ids = selectedRecentRows.filter((r) => canDoRecent(r, action)).map(recentId);
      void applyRecentAction(action, ids);
    },
    [selectedRecentRows, applyRecentAction],
  );

  /** 抽屉打开时跟进队列状态变化；确认删除或有请求在途时先不打扰用户。 */
  useEffect(() => {
    if (!drawerOpen) return;
    const timer = setInterval(() => {
      if (recentBusy || deleteIds.size || bulkDeleteConfirm) return;
      void loadRecent();
    }, 20_000);
    return () => clearInterval(timer);
  }, [drawerOpen, recentBusy, deleteIds, bulkDeleteConfirm, loadRecent]);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const t = e.target as HTMLElement | null;
      const typing =
        !!t &&
        (["TEXTAREA", "SELECT"].includes(t.tagName) ||
          (t.tagName === "INPUT" && (t as HTMLInputElement).type !== "checkbox") ||
          t.isContentEditable);
      if (typing) return;
      if (bulkDeleteConfirm) return setBulkDeleteConfirm(false);
      if (deleteIds.size) return setDeleteIds(new Set());
      setDrawerOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawerOpen, deleteIds, bulkDeleteConfirm]);

  /** / 或 Ctrl/Cmd+K 聚焦搜索框；焦点落在别的输入控件时不抢键。 */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const typing = !!t && (["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName) || t.isContentEditable);
      const hotkey =
        (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) ||
        ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k");
      if (hotkey) {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
        setSuggestOpen(true);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!suggestOpen) return;
    const onDown = (e: MouseEvent) => {
      if (!comboRef.current?.contains(e.target as Node)) {
        setSuggestOpen(false);
        setActiveSuggest(-1);
      }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [suggestOpen]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );

  const clearSelection = useCallback(() => setSelected(new Set()), []);

  const capOf = (key: string) => caps.find((c) => c.key === key);

  const togglePlatform = useCallback(
    (key: string) => {
      setPlatforms((p) => ({ ...p, [key]: !p[key] }));
      clearSelection();
    },
    [clearSelection],
  );

  const togglePick = useCallback(
    (id: string) => {
      if (imported.has(id)) return;
      setSelected((sel) => {
        const next = new Set(sel);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        return next;
      });
    },
    [imported],
  );

  const selectAllVisible = useCallback(() => {
    const ids = results
      .filter((r) => (filter === "all" || r.platform === filter) && !imported.has(r.id))
      .map((r) => r.id);
    if (!ids.length) return;
    setSelected((sel) => {
      const next = new Set(sel);
      ids.forEach((id) => next.add(id));
      return next;
    });
  }, [results, filter, imported]);

  const cancelSearch = useCallback(() => {
    seqRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setSearching(false);
    setMode((m) => (m === "loading" ? "idle" : m));
    setResults([]);
    setNotes([]);
    clearSelection();
    toast("已取消搜索", "info");
  }, [clearSelection, toast]);

  const search = useCallback(
    async (override?: string) => {
      const kw = normalizeKeyword(override ?? keyword);
      if (!kw) {
        toast("请先输入关键词", "warn");
        inputRef.current?.focus();
        return;
      }
      const active = KEYS.filter((k) => platforms[k]);
      if (!active.length) {
        toast("请至少选择一个平台", "warn");
        return;
      }
      if (override !== undefined) setKeyword(kw);
      setSuggestOpen(false);
      setActiveSuggest(-1);

      // 新搜索作废旧请求：AbortController 取消传输，序号丢弃迟到响应，
      // 避免慢平台的结果覆盖用户最新一次搜索。
      seqRef.current += 1;
      const seq = seqRef.current;
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      if (timerRef.current) clearTimeout(timerRef.current);
      let timedOut = false;
      timerRef.current = setTimeout(() => {
        timedOut = true;
        ctrl.abort();
      }, SEARCH_TIMEOUT_MS);

      setSearching(true);
      setMode("loading");
      clearSelection();
      setFilter("all");
      setNotes([]);
      setResults([]);
      setLastQuery(kw);
      setLastDuration(null);
      setErrorDetail("");

      try {
        const j = await api("/search", {
          method: "POST",
          signal: ctrl.signal,
          body: JSON.stringify({
            keyword: kw,
            platforms: active,
            quota: Number(quota) || 12,
            telegram_channels: tgChannels.trim(),
            youtube_token: ytToken.trim(),
            douyin_token: dyToken.trim(),
          }),
        });
        if (seq !== seqRef.current) return;
        const items: Item[] = j.items || [];
        setResults(items);
        setNotes(j.platforms || []);
        setMode("done");
        if (typeof j.keyword === "string" && j.keyword) setLastQuery(j.keyword);
        setLastDuration(typeof j.duration_ms === "number" ? j.duration_ms : null);
        const zero = (j.platforms || []).filter((p: Note) => !p.count).length;
        const timed = Array.isArray(j.timed_out) ? j.timed_out.length : 0;
        if (!items.length) toast("所选平台都没有返回候选，看看下方提示", "warn");
        else if (zero || timed) toast(`找到 ${items.length} 条候选，有平台无结果或超时`, "warn");
        else toast(`找到 ${items.length} 条候选，勾选后入库`, "success");
        if (items.length) {
          // 只记录确实有结果的查询，不把用户引向零结果词（NN/g: Don't Suggest Bad Results）。
          const canonical = String(j.keyword || kw);
          setHistory((prev) => {
            const next = [canonical, ...prev.filter((x) => x !== canonical)].slice(0, HISTORY_LIMIT);
            saveHistory(next);
            return next;
          });
        }
      } catch (e) {
        if (seq !== seqRef.current || ctrl.signal.aborted) return;
        setMode("error");
        setResults([]);
        const msg = (e as Error).message;
        setErrorDetail(msg);
        toast("搜索失败：" + msg, "error");
      } finally {
        if (timerRef.current) {
          clearTimeout(timerRef.current);
          timerRef.current = null;
        }
        if (seq === seqRef.current) {
          setSearching(false);
          abortRef.current = null;
          if (timedOut) {
            setMode("error");
            setResults([]);
            setErrorDetail(`超过 ${SEARCH_TIMEOUT_MS / 1000} 秒仍未返回，请减少平台或稍后重试。`);
            toast("搜索超时，请减少平台或稍后重试", "error");
          }
        }
      }
    },
    [keyword, platforms, quota, tgChannels, ytToken, dyToken, api, clearSelection, toast],
  );

  const bulkImport = useCallback(async () => {
    const picked = results.filter((r) => selected.has(r.id));
    if (!picked.length) return;
    setImporting(true);
    try {
      const j = await api("/import", {
        method: "POST",
        body: JSON.stringify({ keyword: keyword.trim(), items: picked }),
      });
      const done: string[] = j.succeeded || [];
      setImported((imp) => {
        const next = new Set(imp);
        done.forEach((id) => next.add(id));
        return next;
      });
      setSelected((sel) => {
        const next = new Set(sel);
        done.forEach((id) => next.delete(id));
        return next;
      });
      const parts = [`已加入 ${j.added || 0} 项`];
      if (j.existing) parts.push(`${j.existing} 项已在队列`);
      if ((j.failed || []).length) parts.push(`${j.failed.length} 项失败（仍保持选中）`);
      toast(parts.join("，"), (j.failed || []).length ? "warn" : "success");
      void loadRecent();
    } catch (e) {
      toast("入库失败：" + (e as Error).message, "error");
    } finally {
      setImporting(false);
    }
  }, [results, selected, keyword, api, toast, loadRecent]);

  const openDrawer = useCallback(() => {
    setDrawerOpen(true);
    setRecentSelected(new Set());
    setDeleteIds(new Set());
    setBulkDeleteConfirm(false);
    void loadRecent();
  }, [loadRecent]);

  const syncDouyin = useCallback(async () => {
    try {
      const j = await api("/douyin_cookies_from_browser", { method: "POST" });
      if (j.ok) {
        toast("抖音登录态已同步：" + (j.hint || `${j.count || 0} 条 Cookie`), "success");
        loadCaps();
      } else toast(j.error || "同步失败", "error");
    } catch (e) {
      toast("同步失败：" + (e as Error).message, "error");
    }
  }, [api, toast, loadCaps]);

  const saveAuth = useCallback(() => {
    const t = authInput.trim() || "local-admin";
    setToken(t);
    if (typeof localStorage !== "undefined") localStorage.setItem(TOKEN_KEY, t);
    setAuthOpen(false);
    loadCaps();
    void loadRecent();
  }, [authInput, loadCaps, loadRecent]);

  const activePlatforms = useMemo(() => KEYS.filter((k) => platforms[k]), [platforms]);
  const suggestions = useMemo<Suggestion[]>(() => {
    const q = keyword.trim().toLowerCase();
    const recent = history
      .filter((h) => !q || h.toLowerCase().includes(q))
      .map((text) => ({ text, group: "recent" as const }));
    const examples = EXAMPLE_QUERIES.filter(
      (x) => !recent.some((r) => r.text === x) && (!q || x.toLowerCase().includes(q)),
    ).map((text) => ({ text, group: "example" as const }));
    return [...recent, ...examples].slice(0, 6);
  }, [history, keyword]);
  const grouped = useMemo(
    () => ({
      recent: suggestions.map((s, i) => ({ ...s, i })).filter((s) => s.group === "recent"),
      example: suggestions.map((s, i) => ({ ...s, i })).filter((s) => s.group === "example"),
    }),
    [suggestions],
  );

  const chooseSuggestion = useCallback(
    (text: string) => {
      setKeyword(text);
      setSuggestOpen(false);
      setActiveSuggest(-1);
      void search(text);
    },
    [search],
  );
  const removeHistory = useCallback((text: string) => {
    setHistory((prev) => {
      const next = prev.filter((x) => x !== text);
      saveHistory(next);
      return next;
    });
  }, []);
  const onKeywordKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLInputElement>) => {
      const n = suggestions.length;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!n) return;
        e.preventDefault();
        setSuggestOpen(true);
        setActiveSuggest((i) =>
          e.key === "ArrowDown" ? (i < 0 ? 0 : (i + 1) % n) : i < 0 ? n - 1 : (i - 1 + n) % n,
        );
      } else if (e.key === "Escape") {
        if (suggestOpen) {
          e.preventDefault();
          setSuggestOpen(false);
          setActiveSuggest(-1);
        } else if (keyword) {
          e.preventDefault();
          setKeyword("");
        }
      } else if (e.key === "Tab") {
        setSuggestOpen(false);
        setActiveSuggest(-1);
      }
    },
    [suggestions.length, suggestOpen, keyword],
  );

  const visibleResults = useMemo(() => {
    const rows = results.filter((r) => filter === "all" || r.platform === filter);
    if (sortBy === "views") return [...rows].sort((a, b) => Number(b.views || 0) - Number(a.views || 0));
    if (sortBy === "latest") return [...rows].sort((a, b) => Number(b.published_at || 0) - Number(a.published_at || 0));
    return rows;
  }, [results, filter, sortBy]);
  const counts = useMemo(() => {
    const c: Record<string, number> = { all: results.length };
    KEYS.forEach((k) => (c[k] = results.filter((r) => r.platform === k).length));
    return c;
  }, [results]);
  const visibleAllPicked = useMemo(
    () =>
      visibleResults.length > 0 &&
      visibleResults.every((r) => selected.has(r.id) || imported.has(r.id)),
    [visibleResults, selected, imported],
  );

  const douyinNeedsLogin = caps.some((c) => c.key === "douyin" && c.needs_login);
  const douyinEmpty =
    mode === "done" && notes.some((n) => n.key === "douyin" && !n.count);
  const douyinBarOpen = platforms.douyin && (douyinNeedsLogin || douyinEmpty);

  const renderCard = (r: Item, i: number) => {
    const p = PLAT[r.platform as PlatKey] || { label: r.platform, short: "?", color: "var(--primary)" };
    const sel = selected.has(r.id);
    const done = imported.has(r.id);
    const thumb = safeThumb(r.thumb_url);
    const url = safeUrl(r.url || r.original_url);
    const meta = [decodeEntities(r.author), fmtViews(r.views), fmtDate(r.published_at)].filter(Boolean).join(" · ");
    return (
      <article
        key={r.id}
        className={`vcard${sel ? " selected" : ""}`}
        data-id={r.id}
        tabIndex={0}
        role="button"
        aria-pressed={sel}
        style={{ "--pc": p.color, animationDelay: `${Math.min(i, 12) * 18}ms` } as CSSProperties}
        onClick={(e) => {
          if ((e.target as HTMLElement).closest("[data-stop]")) return;
          togglePick(r.id);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            togglePick(r.id);
          }
        }}
      >
        <div className="thumb">
          <span className="fallback">{esc(p.short)}</span>
          {thumb ? (
            <img
              src={esc(thumb)}
              alt=""
              loading="lazy"
              referrerPolicy="no-referrer"
              onError={(e) => (e.currentTarget.closest(".thumb") as HTMLElement)?.classList.add("img-failed")}
            />
          ) : null}
          <span className="plat">{esc(p.label)}</span>
          {fmtDuration(r.duration_sec) ? <span className="dur">{fmtDuration(r.duration_sec)}</span> : null}
          <span className="pick" aria-hidden="true">✓</span>
          {done ? <span className="plat done-pos">已入库</span> : null}
        </div>
        <div className="vbody">
          <div className="vtitle">{esc(decodeEntities(r.title))}</div>
          <div className="vmeta">{esc(meta || "—")}</div>
          <div className="vfoot">
            <a href={esc(url)} target="_blank" rel="noreferrer" data-stop="1">查看原视频 ↗</a>
            {done ? <span className="done-tag">已入库</span> : null}
          </div>
        </div>
      </article>
    );
  };

  /** 零结果 / 失败页：明确说明、给可点击的下一步，语气中性（NN/g 三准则）。 */
  const renderEmpty = () => {
    if (mode === "error") {
      return (
        <div className="empty">
          <div className="icon">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 8v5m0 3h.01" /><circle cx="12" cy="12" r="9" />
            </svg>
          </div>
          <h3>搜索没有完成</h3>
          <p>{errorDetail || "请重试，或检查 8901 ops-api 是否在运行。"}</p>
          <div className="empty-actions">
            <button className="small-btn" onClick={() => void search(lastQuery || keyword)}>重试搜索</button>
            <span className="adv-hint">也可以只勾选一个平台、减少每平台条数后重试</span>
          </div>
        </div>
      );
    }
    if (mode === "idle") {
      return null;
    }
    const alternatives = [
      ...history.slice(0, 2),
      ...EXAMPLE_QUERIES.filter((x) => !history.includes(x)).slice(0, 3),
    ];
    return (
      <div className="empty">
        <div className="icon">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M4 7h16M4 12h10M4 17h7" />
          </svg>
        </div>
        <h3>没有找到与「{lastQuery || "该关键词"}」匹配的候选</h3>
        <p>请确认拼写，或换更短、更通用的词。搜索框已保留原词，可以直接修改后重新搜索。</p>
        <div className="empty-actions">
          {alternatives.map((q) => (
            <button key={q} className="empty-chip" onClick={() => void search(q)}>{q}</button>
          ))}
          <button className="small-btn" onClick={() => void search(lastQuery)}>重试原词</button>
        </div>
        <div className="empty-tip">检查上方平台提示：抖音登录态、YouTube 代理与 Telegram 频道都会影响是否有结果。</div>
      </div>
    );
  };

  return (
    <div className="crawler-root">
      <header className="hero">
        <div className="wrap">
          <div className="topbar">
            <div className="brand"><span className="brand-dot" /> Materials Hub / 素材采集</div>
            <div className="hero-actions">
              <button className="ghost-btn" onClick={openDrawer}>已入库 <span className="cnt">{recentCount}</span></button>
              <button className="ghost-btn" onClick={() => { setAuthMsg("可修改调用 ops-api 的 Admin Token（默认 local-admin）"); setAuthInput(token); setAuthOpen(true); }}>Admin Token</button>
            </div>
          </div>
          <div className="hero-copy">
            <h1>先搜索，再挑选，最后入库</h1>
            <p>输入一个关键词，同时在哔哩哔哩、抖音、YouTube 与 Telegram 查找候选视频。看过封面与标题后，勾选想要的条目，一次性加入加工队列。</p>
            <div className="hero-pills">
              <span className="hero-pill">4 个平台</span>
              <span className="hero-pill">关键词搜索</span>
              <span className="hero-pill">人工挑选</span>
              <span className="hero-pill">批量入库</span>
            </div>
          </div>
        </div>
      </header>

      <main className="wrap">
        <section className="search-card">
          <form
            className="search-row"
            role="search"
            onSubmit={(e) => {
              e.preventDefault();
              const pick = activeSuggest >= 0 && suggestOpen ? suggestions[activeSuggest]?.text : undefined;
              void search(pick);
            }}
          >
            <div className="search-combo" ref={comboRef}>
              <label className="search-input" htmlFor="crawler-kw">
                <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.6-3.6" /></svg>
                <input
                  id="crawler-kw"
                  ref={inputRef}
                  type="search"
                  role="combobox"
                  aria-autocomplete="list"
                  aria-expanded={suggestOpen && suggestions.length > 0}
                  aria-controls="crawler-suggestions"
                  aria-activedescendant={activeSuggest >= 0 && suggestOpen ? `sug-opt-${activeSuggest}` : undefined}
                  aria-label="搜索关键词"
                  autoComplete="off"
                  inputMode="search"
                  enterKeyHint="search"
                  maxLength={80}
                  placeholder="输入关键词，例如：露营、AI 教程、街头美食…"
                  value={keyword}
                  onChange={(e) => { setKeyword(e.target.value); setSuggestOpen(true); setActiveSuggest(-1); }}
                  onFocus={() => setSuggestOpen(true)}
                  onClick={() => setSuggestOpen(true)}
                  onKeyDown={onKeywordKeyDown}
                />
              </label>
              {keyword ? (
                <button
                  type="button"
                  className="clear-btn"
                  aria-label="清空关键词"
                  onClick={() => { setKeyword(""); setActiveSuggest(-1); setSuggestOpen(true); inputRef.current?.focus(); }}
                >×</button>
              ) : (
                <kbd className="kbd-hint" aria-hidden="true">/</kbd>
              )}
              {suggestOpen && suggestions.length ? (
                <div className="suggest" id="crawler-suggestions" role="listbox" aria-label="关键词建议">
                  {(["recent", "example"] as const).map((g) =>
                    grouped[g].length ? (
                      <div
                        key={g}
                        className="sug-group"
                        role="group"
                        aria-label={g === "recent" ? "最近搜索（有结果）" : "试试这些关键词"}
                      >
                        <div className="sug-group-title" aria-hidden="true">
                          {g === "recent" ? "最近搜索（有结果）" : "试试这些关键词"}
                        </div>
                        {grouped[g].map((s) => (
                          <div
                            key={`${g}-${s.text}`}
                            id={`sug-opt-${s.i}`}
                            role="option"
                            aria-selected={activeSuggest === s.i}
                            className={`sug-option${activeSuggest === s.i ? " active" : ""}`}
                            onMouseDown={(e) => e.preventDefault()}
                            onMouseEnter={() => setActiveSuggest(s.i)}
                            onClick={() => chooseSuggestion(s.text)}
                          >
                            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                              {g === "recent"
                                ? <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>
                                : <><circle cx="11" cy="11" r="7" /><path d="M20 20l-3.6-3.6" /></>}
                            </svg>
                            <span className="sug-text">{highlightQuery(s.text, keyword)}</span>
                            {g === "recent" ? (
                              <button
                                type="button"
                                className="sug-del"
                                tabIndex={-1}
                                aria-label={`删除历史：${s.text}`}
                                onMouseDown={(e) => e.stopPropagation()}
                                onClick={(e) => { e.stopPropagation(); removeHistory(s.text); }}
                              >×</button>
                            ) : (
                              <span className="sug-scope" aria-hidden="true">示例</span>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : null,
                  )}
                  <div className="sug-foot" aria-hidden="true">↑↓ 选择 · Enter 搜索 · Esc 关闭</div>
                </div>
              ) : null}
            </div>
            <select className="quota" aria-label="每平台候选数" title="每个平台最多返回多少条候选" value={quota} onChange={(e) => setQuota(e.target.value)}>
              <option value="8">8 条/平台</option>
              <option value="12">12 条/平台</option>
              <option value="20">20 条/平台</option>
              <option value="30">30 条/平台</option>
            </select>
            <button className="primary-btn" type="submit" disabled={searching}>
              {searching ? <><span className="btn-spinner" aria-hidden="true" />搜索中…</> : "搜索素材"}
            </button>
            {searching ? <button type="button" className="cancel-btn" onClick={cancelSearch}>取消</button> : null}
          </form>
          <div className="search-meta">
            <span>
              搜索范围：<b>{activePlatforms.length} 个平台</b>
              {activePlatforms.length ? ` · ${activePlatforms.map((k) => PLAT[k].label).join(" / ")}` : " · 未选择平台"}
            </span>
            <span className="shortcut-hint"><kbd>/</kbd> 或 <kbd>Ctrl K</kbd> 快速聚焦</span>
          </div>

          <div className="section-label">
            <h2>选择平台</h2>
            <span>点击可开关；搜索结果按平台分组，可再筛选</span>
          </div>
          <div className="chips">
            {KEYS.map((key) => {
              const p = PLAT[key];
              const cap = capOf(key);
              const on = platforms[key];
              const needsToken = cap?.needs_token ?? p.tok;
              let sub = p.sub;
              if (cap) {
                if (cap.ready === false) sub = "暂不可用";
                else if (cap.needs_login) sub = "需登录态";
                else if (cap.needs_channels) sub = "需填频道";
                else if (cap.ready === true && !cap.needs_token) sub = "可用";
              }
              const warn = needsToken || cap?.needs_login ? " warn" : "";
              return (
                <button
                  key={key}
                  className={`chip${on ? " on" : ""}${warn}`}
                  data-key={key}
                  style={{ "--pc": p.color } as CSSProperties}
                  aria-pressed={on}
                  onClick={() => togglePlatform(key)}
                >
                  <span className="chip-logo">{p.short}</span>
                  <span className="chip-text">
                    <b>{p.label}{needsToken ? <span className="tok">·Token</span> : null}</b>
                    <small>{esc(sub)}</small>
                  </span>
                  <span className="chip-status" aria-hidden={!needsToken}>{needsToken ? "🔑" : "✓"}</span>
                </button>
              );
            })}
          </div>

          <div className="advanced">
            <div className="adv-row" hidden={!platforms.telegram}>
              <label htmlFor="tgChannels">Telegram 频道</label>
              <input
                id="tgChannels"
                placeholder="@channel1, @channel2（逗号分隔）"
                value={tgChannels}
                onChange={(e) => setTgChannels(e.target.value)}
              />
              <span className="adv-hint">无全站搜索，按频道内关键词搜索并自动翻页</span>
            </div>
            <div className="adv-row" hidden={!platforms.youtube}>
              <label htmlFor="ytToken">YouTube Key</label>
              <input
                id="ytToken"
                type="password"
                autoComplete="off"
                placeholder="YouTube Data API Key（选填，随请求带上）"
                value={ytToken}
                onChange={(e) => setYtToken(e.target.value)}
              />
              <span className="adv-hint">搜索走 yt-dlp 代理；填入官方 API Key 后随请求带上</span>
            </div>
            <div className="adv-row" hidden={!platforms.douyin}>
              <label htmlFor="dyToken">抖音 Token</label>
              <input
                id="dyToken"
                type="password"
                autoComplete="off"
                placeholder="已登录抖音的 sessionid（等价登录态）"
                value={dyToken}
                onChange={(e) => setDyToken(e.target.value)}
              />
              <span className="adv-hint">粘贴 sessionid 或整段登录 Cookie，等价于 9223 窗口登录态</span>
            </div>
            <div className="login-bar" hidden={!douyinBarOpen}>
              <div className="txt"><b>抖音对登录态与风控敏感。</b> 若该平台没有结果，请在 9223 CDP 窗口登录抖音后点右侧按钮同步 Cookie。</div>
              <button className="small-btn" onClick={syncDouyin}>同步登录态</button>
            </div>
          </div>

          <div className="auth-bar" hidden={!authOpen}>
            <span>{authMsg}</span>
            <input value={authInput} onChange={(e) => setAuthInput(e.target.value)} placeholder="local-admin" />
            <button className="small-btn" onClick={saveAuth}>保存并重试</button>
          </div>
        </section>

        <section className="results">
          <div className="results-head">
            <div className="results-title">
              <h2>搜索结果</h2>
              <span aria-live="polite">
                {mode === "loading"
                  ? `正在检索「${lastQuery}」… ${activePlatforms.length} 个平台`
                  : mode === "done"
                    ? `「${lastQuery}」 · 共 ${results.length} 条候选 · 当前显示 ${visibleResults.length} 条${lastDuration !== null ? ` · ${(lastDuration / 1000).toFixed(1)}s` : ""}`
                    : mode === "error"
                      ? "搜索未完成，可点下方按钮重试"
                      : "尚未搜索"}
              </span>
            </div>
            <div className="filters">
              {[{ key: "all", label: "全部" }, ...KEYS.map((k) => ({ key: k, label: PLAT[k].label }))].map((f) => (
                <button
                  key={f.key}
                  className={`filter${filter === f.key ? " on" : ""}`}
                  data-filter={f.key}
                  aria-pressed={filter === f.key}
                  onClick={() => { setFilter(f.key); clearSelection(); }}
                >
                  {esc(f.label)}<span className="n">{counts[f.key] || 0}</span>
                </button>
              ))}
            </div>
          </div>
          <div className={`results-tools${selected.size ? " selecting" : ""}`}>
            <div className="rt-left">
              {selected.size ? (
                <>
                  <span className="rt-sel">
                    <span className="rt-tick" aria-hidden="true">✓</span>
                    已选 <strong>{selected.size}</strong> 项
                  </span>
                  <button
                    className="tool-btn"
                    onClick={selectAllVisible}
                    disabled={visibleAllPicked}
                    title={visibleAllPicked ? "当前筛选已全选" : "选中当前筛选下的全部候选"}
                  >
                    {visibleAllPicked ? "已全选" : "全选当前筛选"}
                  </button>
                  <button className="tool-btn" onClick={clearSelection}>清除</button>
                </>
              ) : (
                <span className="rt-count">
                  {mode === "done"
                    ? `共 ${results.length} 条候选 · 显示 ${visibleResults.length} 条`
                    : mode === "loading"
                      ? "检索中…"
                      : "尚未搜索"}
                </span>
              )}
            </div>
            <div className="rt-right">
              <label className="sort-wrap">排序
                <select
                  className="sort-select"
                  aria-label="结果排序"
                  value={sortBy}
                  onChange={(e) => setSortBy(e.target.value as typeof sortBy)}
                >
                  <option value="relevance">相关度优先</option>
                  <option value="views">播放量最高</option>
                  <option value="latest">最新发布</option>
                </select>
              </label>
              {selected.size ? (
                <button className="tool-btn primary solid" onClick={bulkImport} disabled={importing}>
                  {importing ? "入库中…" : `加入加工队列（${selected.size}）`}
                </button>
              ) : null}
            </div>
          </div>
          {mode === "loading" ? (
            <div className="loading-note" role="status">
              <span className="mini-spinner" aria-hidden="true" />
              正在 {activePlatforms.length} 个平台检索「{lastQuery}」，最长约 40 秒；可以先取消再调整平台。
              <button className="tool-btn" onClick={cancelSearch}>取消搜索</button>
            </div>
          ) : null}
          <div className="notes">
            {notes.filter((n) => n.note || n.error).map((n, i) => {
              const p = PLAT[n.key as PlatKey] || {};
              return (
                <div key={i} className="note" style={{ "--pc": p.color || "var(--primary)" } as CSSProperties}>
                  <span className="dot" /><b>{esc(n.label || p.label || n.key)}</b><span>{esc(n.note || n.error)}</span>
                </div>
              );
            })}
          </div>
          <div className="grid">
            {mode === "loading"
              ? Array.from({ length: 8 }, (_, i) => (
                  <div key={i} className="skeleton"><div className="sk-thumb" /><div className="sk-line" /><div className="sk-line short" /></div>
                ))
              : visibleResults.length
                ? visibleResults.map(renderCard)
                : renderEmpty()}
          </div>
        </section>

        <footer>仅检索平台公开内容，请遵守各平台服务条款；入库条目会进入本机的加工队列。</footer>
      </main>

      <div className="drawer" hidden={!drawerOpen} onClick={(e) => { if (e.target === e.currentTarget) setDrawerOpen(false); }}>
        <div className="drawer-panel" role="dialog" aria-modal="true" aria-label="最近入库">
          <div className="drawer-head">
            <div>
              <h2>最近入库</h2>
              <p className="drawer-sub">共 {recentItems.length} 条 · 操作会同步到加工队列</p>
            </div>
            <div className="drawer-head-actions">
              <button className="icon-btn" aria-label="刷新" title="刷新" disabled={recentBusy} onClick={() => void loadRecent()}>⟳</button>
              <button className="drawer-close" aria-label="关闭" onClick={() => setDrawerOpen(false)}>×</button>
            </div>
          </div>
          <div className="drawer-tools">
            <div className="recent-filters">
              {RECENT_FILTERS.map((f) => (
                <button
                  key={f.key}
                  className={`rf${recentFilter === f.key ? " on" : ""}`}
                  aria-pressed={recentFilter === f.key}
                  onClick={() => changeRecentFilter(f.key)}
                >
                  {f.label}<span className="n">{recentFilterCounts[f.key] || 0}</span>
                </button>
              ))}
            </div>
            <label className="recent-all">
              <input type="checkbox" checked={allVisiblePicked} onChange={toggleAllVisibleRecent} disabled={!visibleRecent.length} />
              全选当前筛选（{visibleRecent.length}）
            </label>
          </div>
          <div className="drawer-list" id="recentList">
            {recentError ? (
              <div className="recent-alert">
                <span>{recentError}</span>
                <button className="row-btn" onClick={() => void loadRecent()}>重试</button>
              </div>
            ) : null}
            {visibleRecent.length ? (
              visibleRecent.map((r) => {
                const id = recentId(r);
                const p = PLAT[r.platform as PlatKey] || { label: r.platform, short: "?", color: "var(--primary)" };
                const st = RECENT_STATUS[String(r.status || "")] || { label: String(r.status || "未知"), cls: "unknown" };
                const confirming = deleteIds.has(id);
                const when = fmtWhen(r.created_at);
                const title = decodeEntities(r.title || r.video_id || "");
                return (
                  <div key={id} className={`recent-item${recentSelected.has(id) ? " picked" : ""}`} style={{ "--pc": p.color } as CSSProperties}>
                    <div className="recent-line">
                      <input
                        type="checkbox"
                        className="recent-check"
                        checked={recentSelected.has(id)}
                        onChange={() => toggleRecentSelect(id)}
                        aria-label="选择该条目"
                      />
                      <span className="tag">{p.short}</span>
                      <div className="txt">
                        <b title={title}>{title}</b>
                        <small>
                          <span className={`st ${st.cls}`}>{st.label}</span>
                          <span>{p.label}</span>
                          {when ? <span>{when}</span> : null}
                        </small>
                      </div>
                      <a className="rlink" href={safeUrl(r.canonical_url || r.original_url)} target="_blank" rel="noreferrer" aria-label="打开原视频">↗</a>
                    </div>
                    <div className="recent-ops">
                      {confirming ? (
                        <>
                          <span className="confirm-tip">删除后队列记录也会移除，确认？</span>
                          <button className="row-btn" onClick={() => setDeleteConfirm(id, false)}>取消</button>
                          <button className="row-btn danger solid" disabled={recentBusy} onClick={() => void applyRecentAction("delete", [id])}>确认删除</button>
                        </>
                      ) : (
                        <>
                          {r.status === "processing" ? <span className="adv-hint">处理中，删除只移除记录</span> : null}
                          {canDoRecent(r, "resume") ? <button className="row-btn primary" disabled={recentBusy} onClick={() => void applyRecentAction("resume", [id])}>继续</button> : null}
                          {canDoRecent(r, "reprocess") ? <button className="row-btn" disabled={recentBusy} onClick={() => void applyRecentAction("reprocess", [id])}>重新处理</button> : null}
                          {canDoRecent(r, "pause") ? <button className="row-btn" disabled={recentBusy} onClick={() => void applyRecentAction("pause", [id])}>暂停</button> : null}
                          <button className="row-btn danger" onClick={() => setDeleteConfirm(id, true)}>删除</button>
                        </>
                      )}
                    </div>
                  </div>
                );
              })
            ) : (
              <div className="recent-empty">
                <b>{recentItems.length ? "该状态下没有条目" : "还没有入库条目"}</b>
                <span>{recentItems.length ? "换个筛选条件看看，或切回「全部」。" : "在上方搜索并勾选候选，加入加工队列后就会出现在这里。"}</span>
              </div>
            )}
          </div>
          {selectedRecentRows.length ? (
            <div className={`drawer-bulk${bulkDeleteConfirm ? " confirm" : ""}`}>
              {bulkDeleteConfirm ? (
                <>
                  <span>确认删除 <strong>{selectedRecentRows.length}</strong> 项？队列记录与入库记录都会移除。</span>
                  <span className="bulk-spacer" />
                  <button className="row-btn" onClick={() => setBulkDeleteConfirm(false)}>取消</button>
                  <button className="row-btn danger solid" disabled={recentBusy} onClick={() => void applyRecentAction("delete", selectedRecentRows.map(recentId))}>确认删除</button>
                </>
              ) : (
                <>
                  <span>已选 <strong>{selectedRecentRows.length}</strong> 项</span>
                  <button className="row-btn" onClick={() => setRecentSelected(new Set())}>清除</button>
                  <span className="bulk-spacer" />
                  {bulkRecentActions.map((a) => (
                    <button key={a.action} className="row-btn primary" disabled={recentBusy} onClick={() => runBulkRecent(a.action)}>{a.label}</button>
                  ))}
                  <button className="row-btn danger" disabled={recentBusy} onClick={() => setBulkDeleteConfirm(true)}>删除</button>
                </>
              )}
            </div>
          ) : null}
        </div>
      </div>

      <div className="toast" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast-item ${t.type}`}>{t.msg}</div>
        ))}
      </div>

      <style jsx>{`
        .crawler-root {
          --bg: #f4f6fb;
          --card: #ffffff;
          --ink: #141a2e;
          --muted: #6b7590;
          --line: #e6e9f2;
          --primary: #5b5bd6;
          --primary-2: #8a5cf6;
          --shadow: 0 18px 48px rgba(24, 32, 72, 0.1);
          --shadow-sm: 0 6px 20px rgba(24, 32, 72, 0.07);
          --bilibili: #fb7299;
          --douyin: #252733;
          --youtube: #f4303d;
          --telegram: #229ed9;
          background: var(--bg);
          color: var(--ink);
          font: 14px/1.6 "Inter", "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, sans-serif;
          -webkit-font-smoothing: antialiased;
          min-height: 100vh;
        }
        * { box-sizing: border-box; }
        [hidden] { display: none !important; }
        button, input, select { font: inherit; }
        button { cursor: pointer; }
        a { color: inherit; }
        .wrap { width: min(1180px, calc(100% - 48px)); margin: 0 auto; }
        :focus-visible { outline: 3px solid rgba(91, 91, 214, 0.45); outline-offset: 2px; }

        .hero { position: relative; overflow: hidden; color: #fff; padding: 26px 0 92px;
          background: linear-gradient(118deg, #0b1024 0%, #1a2250 48%, #44307e 100%); }
        .hero::before { content: ""; position: absolute; width: 620px; height: 620px; right: -120px; top: -300px;
          background: radial-gradient(circle, rgba(138, 92, 246, 0.55), transparent 66%); filter: blur(10px); }
        .hero::after { content: ""; position: absolute; width: 460px; height: 460px; left: -140px; bottom: -330px;
          background: radial-gradient(circle, rgba(56, 189, 248, 0.3), transparent 68%); }
        .hero > * { position: relative; z-index: 1; }
        .topbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
        .brand { display: inline-flex; align-items: center; gap: 10px; font-size: 13px; letter-spacing: 0.12em;
          text-transform: uppercase; color: rgba(255, 255, 255, 0.82); font-weight: 600; }
        .brand-dot { width: 10px; height: 10px; border-radius: 50%; background: #7af0c8;
          box-shadow: 0 0 0 5px rgba(122, 240, 200, 0.16); }
        .hero-actions { display: flex; gap: 10px; }
        .ghost-btn { background: rgba(255, 255, 255, 0.1); border: 1px solid rgba(255, 255, 255, 0.22);
          color: #fff; padding: 9px 16px; border-radius: 11px; font-size: 13px; font-weight: 600;
          transition: background 0.18s ease, transform 0.18s ease; }
        .ghost-btn:hover { background: rgba(255, 255, 255, 0.2); transform: translateY(-1px); }
        .ghost-btn .cnt { display: inline-block; margin-left: 6px; padding: 0 7px; border-radius: 20px;
          background: rgba(255, 255, 255, 0.2); font-size: 12px; }
        .hero-copy { margin-top: 58px; max-width: 720px; }
        .hero-copy h1 { margin: 0 0 14px; font-size: clamp(28px, 4vw, 42px); line-height: 1.2;
          letter-spacing: -0.02em; font-weight: 750; }
        .hero-copy p { margin: 0; color: rgba(255, 255, 255, 0.74); font-size: 15px; max-width: 620px; }
        .hero-pills { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 26px; }
        .hero-pill { padding: 6px 13px; border-radius: 999px; font-size: 12.5px; font-weight: 600;
          border: 1px solid rgba(255, 255, 255, 0.18); background: rgba(255, 255, 255, 0.08); color: rgba(255, 255, 255, 0.9); }

        .search-card { position: relative; margin-top: -58px; background: var(--card); border-radius: 22px;
          padding: 24px; box-shadow: var(--shadow); border: 1px solid rgba(255, 255, 255, 0.7); }
        .search-row { display: flex; align-items: center; gap: 10px; }
        .search-combo { position: relative; flex: 1; min-width: 0; }
        .search-input { position: relative; display: flex; align-items: center; gap: 12px; width: 100%;
          background: #f7f8fc; border: 1.5px solid var(--line); border-radius: 14px; padding: 0 44px 0 14px;
          transition: border-color 0.18s ease, box-shadow 0.18s ease; }
        .search-input:focus-within { border-color: var(--primary); box-shadow: 0 0 0 4px rgba(91, 91, 214, 0.1); }
        .search-input svg { flex: none; color: #6b7590; }
        .search-input input { flex: 1; min-width: 0; border: 0; outline: 0; background: transparent;
          padding: 15px 0; font-size: 15px; color: var(--ink); }
        .search-input input::placeholder { color: #6b7590; opacity: 1; }
        .search-input input::-webkit-search-cancel-button { display: none; }
        .clear-btn, .kbd-hint { position: absolute; right: 12px; top: 50%; transform: translateY(-50%); }
        .clear-btn { width: 26px; height: 26px; border: 0; border-radius: 50%; background: #e6e9f2; color: #5a6480;
          font-size: 17px; line-height: 1; display: grid; place-items: center; }
        .clear-btn:hover { background: #d9deee; color: var(--ink); }
        .kbd-hint { pointer-events: none; padding: 1px 7px; border: 1px solid var(--line); border-radius: 6px;
          background: #fff; color: #6b7590; font: 12px/1.5 ui-monospace, monospace; }
        kbd { font: inherit; }

        .suggest { position: absolute; z-index: 30; top: calc(100% + 8px); left: 0; right: 0;
          background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: 6px;
          box-shadow: 0 18px 44px rgba(24, 32, 72, 0.16); max-height: 330px; overflow: auto; }
        .sug-group + .sug-group { border-top: 1px solid #f0f2f8; margin-top: 4px; padding-top: 4px; }
        .sug-group-title { padding: 7px 10px 5px; font-size: 11px; font-weight: 700;
          letter-spacing: 0.04em; color: var(--muted); }
        .sug-option { display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 9px;
          cursor: pointer; color: var(--ink); font-size: 13.5px; }
        .sug-option svg { flex: none; color: #8b95ad; }
        .sug-option.active, .sug-option:hover { background: #f1f0ff; }
        .sug-option.active svg { color: var(--primary); }
        .sug-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .sug-del { flex: none; border: 0; background: transparent; color: var(--muted); font-size: 16px; line-height: 1;
          padding: 2px 6px; border-radius: 6px; }
        .sug-del:hover { background: #e9ebf5; color: #c33; }
        .sug-scope { flex: none; font-size: 11px; color: var(--muted); background: #f2f4fa;
          padding: 2px 7px; border-radius: 6px; }
        .sug-foot { padding: 8px 10px 4px; border-top: 1px solid #f0f2f8; margin-top: 4px;
          font-size: 11px; color: var(--muted); }

        .search-meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap;
          margin-top: 10px; padding: 0 2px; font-size: 12px; color: var(--muted); }
        .search-meta b { color: var(--ink); font-weight: 700; }
        .shortcut-hint kbd { display: inline-block; padding: 1px 6px; border: 1px solid var(--line);
          border-radius: 5px; background: #fff; font-size: 11px; color: var(--muted); }

        .quota { border: 1.5px solid var(--line); border-radius: 12px; padding: 13px 10px; background: #fff;
          color: var(--muted); font-size: 13px; outline: none; }
        .primary-btn { border: 0; border-radius: 13px; padding: 14px 26px; font-weight: 700; font-size: 14px;
          display: inline-flex; align-items: center; justify-content: center; gap: 8px; white-space: nowrap;
          color: #fff; background: linear-gradient(120deg, var(--primary), var(--primary-2));
          box-shadow: 0 10px 22px rgba(91, 91, 214, 0.28); transition: transform 0.18s ease, box-shadow 0.18s ease; }
        .primary-btn:hover { transform: translateY(-2px); box-shadow: 0 14px 26px rgba(91, 91, 214, 0.36); }
        .primary-btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
        .cancel-btn { border: 1px solid var(--line); background: #fff; color: var(--muted); border-radius: 11px;
          padding: 13px 14px; font-size: 13px; font-weight: 600; white-space: nowrap; }
        .cancel-btn:hover { border-color: #e2454e; color: #e2454e; }
        .btn-spinner, .mini-spinner { display: inline-block; border-radius: 50%; border: 2px solid rgba(255, 255, 255, 0.45);
          border-top-color: #fff; animation: spin 0.8s linear infinite; }
        .btn-spinner { width: 14px; height: 14px; }
        .mini-spinner { flex: none; width: 14px; height: 14px; border-color: rgba(91, 91, 214, 0.2); border-top-color: var(--primary); }

        .sort-wrap { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--muted); }
        .sort-select { border: 1px solid var(--line); border-radius: 9px; padding: 7px 9px; background: #fff;
          color: var(--ink); font-size: 12.5px; outline: none; }
        .sort-select:focus { border-color: var(--primary); }
        .loading-note { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 0 0 14px;
          padding: 11px 14px; border-radius: 11px; background: #f1f0ff; border: 1px solid #e0dcff;
          color: #4a4780; font-size: 12.5px; }
        .loading-note .tool-btn { margin-left: auto; background: transparent; }

        .section-label { display: flex; align-items: center; justify-content: space-between; gap: 12px;
          margin: 22px 0 12px; }
        .section-label h2 { margin: 0; font-size: 13px; font-weight: 700; color: var(--ink); letter-spacing: 0.01em; }
        .section-label span { font-size: 12.5px; color: var(--muted); }
        .chips { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
        .chip { display: flex; align-items: center; gap: 11px; text-align: left; padding: 12px 13px;
          border-radius: 14px; border: 1.5px solid var(--line); background: #fff; color: var(--ink);
          transition: border-color 0.18s ease, background 0.18s ease, transform 0.18s ease; }
        .chip:hover { transform: translateY(-2px); }
        .chip.on { border-color: var(--pc); background: color-mix(in srgb, var(--pc) 7%, #fff); }
        .chip-logo { flex: none; width: 34px; height: 34px; border-radius: 10px; display: grid; place-items: center;
          color: #fff; background: var(--pc); font-size: 12px; font-weight: 800; letter-spacing: -0.02em; }
        .chip-text { min-width: 0; display: flex; flex-direction: column; line-height: 1.35; }
        .chip-text b { font-size: 13.5px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .chip-text b .tok { font-size: 10px; font-weight: 800; font-style: normal; color: var(--pc); margin-left: 3px; letter-spacing: 0.02em; }
        .chip.on .chip-text b .tok { color: var(--pc); }
        .chip-text small { font-size: 11.5px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .chip-status { margin-left: auto; flex: none; width: 18px; height: 18px; border-radius: 50%;
          border: 1.5px solid var(--line); display: grid; place-items: center; font-size: 11px; color: transparent; }
        .chip.on .chip-status { background: var(--pc); border-color: var(--pc); color: #fff; }
        .chip.warn .chip-text small { color: #c2741a; }

        .advanced { margin-top: 14px; display: grid; gap: 10px; }
        .adv-row { display: flex; align-items: center; gap: 12px; padding: 12px 14px; border-radius: 12px;
          background: #f7f8fc; border: 1px dashed var(--line); }
        .adv-row label { font-size: 13px; font-weight: 700; white-space: nowrap; }
        .adv-row input { flex: 1; min-width: 0; border: 1px solid var(--line); border-radius: 9px;
          padding: 9px 11px; outline: 0; background: #fff; }
        .adv-row input:focus { border-color: var(--primary); }
        .adv-hint { font-size: 12px; color: var(--muted); }
        .login-bar { display: flex; align-items: center; gap: 12px; padding: 12px 14px; border-radius: 12px;
          background: linear-gradient(100deg, #fff5f7, #f6f4ff); border: 1px solid #f0e6f5; }
        .login-bar .txt { flex: 1; font-size: 12.5px; color: #6b5a78; }
        .login-bar .txt b { color: var(--ink); }
        .small-btn { border: 1px solid var(--primary); color: var(--primary); background: #fff;
          border-radius: 9px; padding: 8px 14px; font-size: 12.5px; font-weight: 700; white-space: nowrap; }
        .small-btn:hover { background: #f2f1ff; }

        .auth-bar { margin-top: 16px; display: flex; align-items: center; gap: 10px; padding: 12px 16px;
          border-radius: 12px; background: #fff4f4; border: 1px solid #ffd8d8; color: #a5232c; font-size: 13px; }
        .auth-bar input { flex: 1; max-width: 260px; border: 1px solid #f3b8b8; border-radius: 8px; padding: 7px 10px; }

        .results { margin-top: 30px; padding-bottom: 48px; }
        .results-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
        .results-title { display: flex; align-items: baseline; gap: 10px; }
        .results-title h2 { margin: 0; font-size: 19px; font-weight: 750; letter-spacing: -0.01em; }
        .results-title span { font-size: 13px; color: var(--muted); }
        .filters { display: flex; gap: 6px; flex-wrap: wrap; }
        .filter { border: 1px solid transparent; background: transparent; color: var(--muted);
          padding: 7px 13px; border-radius: 10px; font-size: 13px; font-weight: 600; }
        .filter:hover { background: #eceefa; color: var(--ink); }
        .filter.on { background: #fff; color: var(--primary); border-color: var(--line); box-shadow: var(--shadow-sm); }
        .filter .n { opacity: 0.65; margin-left: 4px; font-size: 11.5px; }
        .results-tools { position: sticky; top: 0; z-index: 6; display: flex; align-items: center;
          justify-content: space-between; gap: 12px; flex-wrap: wrap; margin: 14px 0 16px;
          padding: 11px 14px; background: var(--bg); border: 1px solid var(--line); border-radius: 14px; }
        .results-tools.selecting { border-color: #dcd9ff; background: #f7f6ff; }
        .rt-left, .rt-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
        .rt-right { margin-left: auto; }
        .rt-count { font-size: 12.5px; color: var(--muted); }
        .rt-sel { display: inline-flex; align-items: center; gap: 7px; font-size: 13px; color: var(--ink); font-weight: 600; }
        .rt-sel strong { color: var(--primary); font-size: 15px; }
        .rt-tick { width: 20px; height: 20px; border-radius: 50%; background: var(--primary); color: #fff;
          display: grid; place-items: center; font-size: 12px; }
        .tool-btn { border: 1px solid var(--line); background: #fff; color: var(--muted); border-radius: 9px;
          padding: 7px 13px; font-size: 12.5px; font-weight: 600; }
        .tool-btn:hover:not(:disabled) { border-color: var(--primary); color: var(--primary); }
        .tool-btn:disabled { opacity: 0.45; cursor: not-allowed; }
        .tool-btn.primary.solid { color: #fff; border-color: transparent;
          background: linear-gradient(120deg, var(--primary), var(--primary-2)); }
        .tool-btn.primary.solid:hover:not(:disabled) { filter: brightness(1.06); color: #fff; }
        .notes { display: grid; gap: 8px; margin-bottom: 16px; }
        .note { display: flex; align-items: flex-start; gap: 9px; padding: 10px 13px; border-radius: 11px;
          background: #fff; border: 1px solid var(--line); font-size: 12.5px; color: var(--muted); }
        .note b { color: var(--ink); flex: none; }
        .note .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--pc); margin-top: 6px; flex: none; }

        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 16px; }

        .skeleton { background: #fff; border-radius: 16px; overflow: hidden; box-shadow: var(--shadow-sm); }
        .skeleton .sk-thumb { aspect-ratio: 16 / 9; }
        .skeleton .sk-line { height: 11px; border-radius: 6px; margin: 12px 14px; }
        .skeleton .sk-line.short { width: 55%; }
        .sk-thumb, .sk-line { background: linear-gradient(100deg, #eceefa 20%, #f7f8fc 40%, #eceefa 60%);
          background-size: 220% 100%; animation: shimmer 1.3s linear infinite; }

        .drawer { position: fixed; inset: 0; z-index: 50; background: rgba(12, 18, 40, 0.38); }
        .drawer-panel { position: absolute; right: 0; top: 0; bottom: 0; width: min(460px, 94vw);
          display: flex; flex-direction: column; background: #fff; overflow: hidden;
          box-shadow: -20px 0 60px rgba(12, 18, 40, 0.22); animation: slide 0.24s ease both; }
        .drawer-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
          padding: 22px 22px 14px; border-bottom: 1px solid #f0f2f8; }
        .drawer-head h2 { margin: 0; font-size: 18px; }
        .drawer-sub { margin: 4px 0 0; font-size: 12px; color: var(--muted); }
        .drawer-head-actions { display: flex; gap: 8px; }
        .icon-btn, .drawer-close { flex: none; border: 0; background: #f2f4fa; width: 32px; height: 32px;
          border-radius: 9px; font-size: 17px; color: var(--ink); line-height: 1; }
        .icon-btn:hover, .drawer-close:hover { background: #e6e9f2; }
        .icon-btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .drawer-tools { display: grid; gap: 10px; padding: 14px 22px 10px; }
        .recent-filters { display: flex; gap: 6px; flex-wrap: wrap; }
        .rf { border: 1px solid var(--line); background: #fff; color: var(--muted); border-radius: 9px;
          padding: 6px 11px; font-size: 12px; font-weight: 600; }
        .rf:hover { border-color: var(--primary); color: var(--primary); }
        .rf.on { color: var(--primary); border-color: var(--primary); background: #f2f1ff; }
        .rf .n { margin-left: 5px; opacity: 0.7; font-size: 11px; }
        .recent-all { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; color: var(--muted); cursor: pointer; }
        .recent-all input { accent-color: var(--primary); cursor: pointer; }
        .recent-all input:disabled { cursor: not-allowed; }
        .drawer-list { flex: 1; min-height: 0; overflow: auto; padding: 0 22px 18px; }
        .recent-alert { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 14px;
          padding: 10px 12px; border-radius: 10px; background: #fff4f4; border: 1px solid #ffd8d8;
          color: #a5232c; font-size: 12px; }
        .recent-alert span { flex: 1; }
        .recent-item { padding: 12px 0; border-bottom: 1px solid #f0f2f8; }
        .recent-item.picked { background: linear-gradient(90deg, rgba(91, 91, 214, 0.07), transparent 70%); }
        .recent-line { display: flex; align-items: flex-start; gap: 10px; }
        .recent-check { flex: none; margin-top: 4px; accent-color: var(--primary); cursor: pointer; }
        .recent-item .tag { flex: none; height: 22px; padding: 0 8px; border-radius: 7px; color: #fff;
          background: var(--pc); font-size: 11px; font-weight: 700; line-height: 22px; }
        .recent-item .txt { min-width: 0; flex: 1; }
        .recent-item .txt b { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
          overflow: hidden; font-size: 13px; line-height: 1.45; }
        .recent-item .txt small { display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
          margin-top: 5px; color: var(--muted); font-size: 11.5px; }
        .st { padding: 1px 7px; border-radius: 6px; font-size: 11px; font-weight: 700;
          background: #eef0f7; color: #5b6480; }
        .st.pending { background: #eef0ff; color: #4a51b8; }
        .st.processing { background: #fff2dc; color: #9a6810; }
        .st.paused { background: #f0f0f3; color: #6b6f7d; }
        .st.error { background: #ffe9ea; color: #b42c37; }
        .st.done { background: #e6f7ef; color: #137a53; }
        .rlink { flex: none; padding: 0 3px; font-size: 14px; color: var(--primary); text-decoration: none; }
        .rlink:hover { text-decoration: underline; }
        .recent-ops { display: flex; align-items: center; justify-content: flex-end; gap: 6px;
          flex-wrap: wrap; margin-top: 9px; }
        .row-btn { border: 1px solid var(--line); background: #fff; color: var(--muted); border-radius: 8px;
          padding: 5px 10px; font-size: 11.5px; font-weight: 600; white-space: nowrap; }
        .row-btn:hover { border-color: var(--primary); color: var(--primary); }
        .row-btn.primary { color: var(--primary); border-color: #dcd9ff; background: #f7f6ff; }
        .row-btn.danger { color: #b42c37; }
        .row-btn.danger:hover { border-color: #e2454e; color: #e2454e; background: #fff5f5; }
        .row-btn.danger.solid { background: #d93b45; border-color: #d93b45; color: #fff; }
        .row-btn.danger.solid:hover { background: #c5323c; color: #fff; }
        .row-btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .confirm-tip { margin-right: auto; color: #a5232c; font-size: 11.5px; font-weight: 600; }
        .recent-empty { display: grid; gap: 6px; justify-items: center; padding: 54px 12px;
          color: var(--muted); font-size: 12.5px; text-align: center; }
        .recent-empty b { color: var(--ink); font-size: 14px; }
        .drawer-bulk { flex: none; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
          padding: 12px 22px; border-top: 1px solid var(--line); background: #fff;
          box-shadow: 0 -8px 20px rgba(24, 32, 72, 0.06); font-size: 12.5px; }
        .drawer-bulk.confirm { background: #fff5f5; }
        .drawer-bulk.confirm > span { color: #a5232c; }
        .drawer-bulk strong { color: var(--primary); font-size: 16px; margin: 0 2px; }
        .bulk-spacer { flex: 1 1 auto; }
        .toast { position: fixed; left: 50%; top: 96px; transform: translateX(-50%); z-index: 60; display: grid; gap: 10px; justify-items: center; pointer-events: none; }
        .toast-item { min-width: 260px; max-width: 420px; padding: 13px 16px; border-radius: 12px; background: #fff;
          box-shadow: var(--shadow); border-left: 4px solid var(--primary); font-size: 13px; color: var(--ink);
          animation: pop 0.2s ease both; }
        .toast-item.success { border-left-color: #18a06a; }
        .toast-item.error { border-left-color: #e2454e; }
        .toast-item.warn { border-left-color: #e2a23a; }
        footer { color: #9aa3ba; font-size: 12px; text-align: center; padding: 0 0 34px; }

        @keyframes rise { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
        @keyframes pop { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
        @keyframes shimmer { to { background-position-x: -220%; } }
        @keyframes slide { from { transform: translateX(24px); opacity: 0.6; } to { transform: none; opacity: 1; } }
        @keyframes spin { to { transform: rotate(360deg); } }
        @media (max-width: 820px) {
          .chips { grid-template-columns: repeat(2, minmax(0, 1fr)); }
          .search-row { flex-wrap: wrap; }
          .search-combo { flex-basis: 100%; }
          .hero-copy { margin-top: 40px; }
        }
        @media (max-width: 560px) {
          .wrap { width: min(100% - 28px, 1180px); }
          .chips { grid-template-columns: 1fr; }
          .grid { grid-template-columns: 1fr; }
          .primary-btn { flex: 1; }
          .shortcut-hint { display: none; }
          .quota { flex: 1; }
          .bulk { left: 12px; right: 12px; bottom: 12px; transform: none; flex-wrap: wrap;
            animation: rise 0.2s ease both; }
          @keyframes pop { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
        }
        @media (prefers-reduced-motion: reduce) {
          * { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
        }
      `}</style>

      {/* renderCard / renderEmpty 辅助函数返回的 JSX 不会拿到 styled-jsx 的
          scope class，对应样式必须放全局块并用 .crawler-root 前缀隔离。 */}
      <style jsx global>{`
        .crawler-root .vcard { background: var(--card); border-radius: 16px; overflow: hidden; border: 1.5px solid transparent;
          box-shadow: var(--shadow-sm); cursor: pointer; transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
          animation: rise 0.32s ease both; }
        .crawler-root .vcard:hover { transform: translateY(-4px); box-shadow: var(--shadow); }
        .crawler-root .vcard.selected { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(91, 91, 214, 0.14), var(--shadow-sm); }
        .crawler-root .thumb { position: relative; aspect-ratio: 16 / 9; background: linear-gradient(135deg, color-mix(in srgb, var(--pc) 30%, #1b2140), #1b2140);
          overflow: hidden; }
        .crawler-root .thumb .fallback { position: absolute; inset: 0; display: grid; place-items: center; color: rgba(255, 255, 255, 0.9);
          font-size: 30px; font-weight: 800; letter-spacing: -0.03em; }
        .crawler-root .thumb img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; z-index: 1; }
        .crawler-root .thumb.img-failed img { display: none; }
        .crawler-root .plat { position: absolute; left: 10px; top: 10px; z-index: 2; padding: 4px 9px; border-radius: 7px;
          background: rgba(12, 16, 30, 0.72); backdrop-filter: blur(6px); color: #fff; font-size: 11px; font-weight: 700; }
        .crawler-root .plat::before { content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%;
          background: var(--pc); margin-right: 6px; vertical-align: 1px; }
        .crawler-root .plat.done-pos { left: auto; right: 10px; top: auto; bottom: 10px; background: #17845a; }
        .crawler-root .dur { position: absolute; right: 10px; bottom: 10px; z-index: 2; padding: 3px 7px; border-radius: 6px;
          background: rgba(12, 16, 30, 0.78); color: #fff; font-size: 11px; font-variant-numeric: tabular-nums; }
        .crawler-root .pick { position: absolute; right: 10px; top: 10px; z-index: 2; width: 27px; height: 27px; border-radius: 50%;
          border: 1.5px solid rgba(255, 255, 255, 0.85); background: rgba(12, 16, 30, 0.35); color: transparent;
          display: grid; place-items: center; font-size: 14px; font-weight: 800; }
        .crawler-root .vcard.selected .pick { background: var(--primary); border-color: var(--primary); color: #fff; }
        .crawler-root .vbody { padding: 13px 14px 14px; }
        .crawler-root .vtitle { font-size: 13.5px; font-weight: 700; line-height: 1.45; min-height: 39px;
          display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .crawler-root .vmeta { margin-top: 8px; font-size: 11.5px; color: var(--muted); white-space: nowrap;
          overflow: hidden; text-overflow: ellipsis; min-height: 18px; }
        .crawler-root .vfoot { margin-top: 11px; padding-top: 10px; border-top: 1px solid #f0f2f8; display: flex;
          align-items: center; justify-content: space-between; gap: 8px; }
        .crawler-root .vfoot a { font-size: 11.5px; color: var(--primary); font-weight: 700; text-decoration: none; }
        .crawler-root .vfoot a:hover { text-decoration: underline; }
        .crawler-root .done-tag { font-size: 11px; color: #17845a; background: #e7f7ef; padding: 3px 8px; border-radius: 7px; font-weight: 700; }
        .crawler-root .empty { grid-column: 1 / -1; text-align: center; padding: 62px 20px; background: #fff;
          border-radius: 18px; border: 1px dashed var(--line); }
        .crawler-root .empty .icon { width: 64px; height: 64px; margin: 0 auto 18px; border-radius: 20px; display: grid;
          place-items: center; background: #f1f0ff; color: var(--primary); }
        .crawler-root .empty h3 { margin: 0 0 8px; font-size: 17px; }
        .crawler-root .empty p { margin: 0 auto; max-width: 500px; color: var(--muted); font-size: 13px; }
        .crawler-root .empty-actions { display: flex; align-items: center; justify-content: center; gap: 9px;
          flex-wrap: wrap; margin-top: 20px; }
        .crawler-root .empty-chip { border: 1px solid var(--line); background: #fff; color: var(--primary);
          border-radius: 999px; padding: 7px 14px; font-size: 12.5px; font-weight: 600; }
        .crawler-root .empty-chip:hover { background: #f1f0ff; border-color: var(--primary); }
        .crawler-root .empty .small-btn { border: 1px solid var(--primary); color: var(--primary); background: #fff;
          border-radius: 9px; padding: 8px 14px; font-size: 12.5px; font-weight: 700; }
        .crawler-root .empty .small-btn:hover { background: #f2f1ff; }
        .crawler-root .empty .adv-hint { font-size: 12px; color: var(--muted); }
        .crawler-root .empty-tip { margin-top: 16px; font-size: 12px; color: var(--muted); }
        .crawler-root .sug-hit { color: var(--muted); }
        .crawler-root .sug-rest { color: var(--ink); font-weight: 700; }
      `}</style>
    </div>
  );
}
