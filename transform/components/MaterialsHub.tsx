"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { memo, useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type ReactNode } from "react";
import { fillCopy, type HubCopy } from "@/lib/copy";
import {
  chipTagsVisible,
  formatHubTag,
  hubApiBase,
  hubFetch,
  hubFileUrl,
  hubThumbUrl,
  HUB_UPLOAD_MAX_BYTES,
  hubUploadFile,
  type HubMaterial,
  type HubStats,
  type HubTag,
  type HubRunRecord,
  type HubUnderstand,
  type HubReadiness,
  type HubDeliverResult,
  hubRunRecord,
  hubUnderstand,
  hubReadiness,
  hubDeliver,
  hubSetReviewed,
  hubSetFavorite,
  hubBulk,
  hubColl,
} from "@/lib/hub-api";
import HubFilePreview from "./HubFilePreview";

/** 用户策展合集标签:album:<名称>,名称去逗号/空白,避免破坏 CSV tags。 */
function albumTagOf(name: string): string {
  const n = name.trim().replace(/[,，]+/g, " ").replace(/\s+/g, " ").slice(0, 40);
  return `album:${n || "untitled"}`;
}

function albumTitleOf(tag: string): string {
  return tag.startsWith("album:") ? (tag.slice(6) || tag) : tag;
}

function SecHead({ open, onToggle, children }: { open: boolean; onToggle: () => void; children: ReactNode }) {
  return (
    <button type="button" className="hub-sechead" aria-expanded={open} onClick={onToggle}>
      <span className="caret" aria-hidden="true">{open ? "▾" : "▸"}</span>
      {children}
    </button>
  );
}

// 可搜索的分组选择器:替代 42 项原生 <select>(扁平、不可搜索、不可扫读)。
// options 为 [id, "L1 › L2 › leaf"] 路径数组,按 L1 分组渲染;搜索同时命中 id 与路径。
function FormPicker({
  value, onChange, options, placeholder, searchPh, emptyPh,
}: {
  value: string;
  onChange: (id: string) => void;
  options: [string, string][];
  placeholder: string;
  searchPh: string;
  emptyPh: string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const boxRef = useRef<HTMLDivElement>(null);
  const cur = options.find(([id]) => id === value);
  const ql = q.trim().toLowerCase();
  const hit = ql
    ? options.filter(([id, path]) => id.toLowerCase().includes(ql) || path.toLowerCase().includes(ql))
    : options;
  // 按 L1(路径第一段)分组,保持 taxonomy 原有顺序
  const groups: [string, [string, string][]][] = [];
  for (const opt of hit) {
    const head = opt[1].split(" › ")[0];
    const last = groups[groups.length - 1];
    if (last && last[0] === head) last[1].push(opt);
    else groups.push([head, [opt]]);
  }
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="hub-picker" ref={boxRef}>
      <button
        type="button"
        className={`hub-picker-btn${open ? " open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="hub-picker-val">{cur ? cur[1] : placeholder}</span>
        <span className="caret" aria-hidden="true">{open ? "▴" : "▾"}</span>
      </button>
      {open && (
        <div className="hub-picker-pop">
          <input
            className="hub-picker-search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder={searchPh}
            aria-label={searchPh}
            autoFocus
          />
          <div className="hub-picker-list">
            {!cur && <button type="button" className="hub-picker-opt" onClick={() => { onChange(""); setOpen(false); }}>{placeholder}</button>}
            {groups.map(([head, opts]) => (
              <div key={head} className="hub-picker-grp">
                <div className="hub-picker-grph">{head}</div>
                {opts.map(([id, path]) => {
                  const segs = path.split(" › ");
                  const depth = segs.length - 1;
                  const parent = segs.slice(1, -1).join(" › ");
                  const name = segs[segs.length - 1];
                  return (
                    <button
                      key={id}
                      type="button"
                      className={`hub-picker-opt d${depth}${value === id ? " on" : ""}`}
                      onClick={() => { onChange(id); setOpen(false); setQ(""); }}
                    >
                      {parent && <span className="hub-picker-parent">{parent} › </span>}
                      {name}
                    </button>
                  );
                })}
              </div>
            ))}
            {!hit.length && <div className="hub-picker-empty">{emptyPh}</div>}
          </div>
        </div>
      )}
    </div>
  );
}

type FormNodeData = string | { [k: string]: FormNodeData };
type HubTaxonomy = {
  forms: Record<string, FormNodeData>;
  form_groups: Record<string, string>;
  form_groups_en: Record<string, string>;
  form_subgroups: Record<string, string>;
  form_options: [string, string][];
  form_options_en: [string, string][];
  form_subgroups_en: Record<string, string>;
  form_descendants: Record<string, string[]>;
  topics: string[];
  topics_en: Record<string, string>;
  topic_taxonomy: Record<string, string[]>;
  topic_groups_en: Record<string, string>;
  topic_descendants: Record<string, string[]>;
  usages: string[];
  usages_en: Record<string, string>;
};

const dedupe = (arr: string[]): string[] => Array.from(new Set(arr));

// 受控词表字段(usage/topics)在后端是逗号分隔字符串,但做防御性兼容:
// 万一某条记录是数组,也能正确按元素切分。trim + 去空,避免 "" 误匹配。
const csv = (v: string | string[] | undefined | null): string[] => {
  if (Array.isArray(v)) return v.map((s) => s.trim()).filter(Boolean);
  return (v || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
};

// 内容形式树节点(递归)与递归渲染组件
interface FormNode {
  id: string;
  label: string;
  n: number;
  active: boolean;
  open: boolean;
  children: FormNode[];
  isLeaf: boolean;
}

function FormNodeView({ node, onSelect, openIds, onToggleOpen, ensureOpen }: {
  node: FormNode; onSelect: (id: string) => void;
  openIds: Set<string>; onToggleOpen: (id: string) => void; ensureOpen: (id: string) => void;
}) {
  if (node.isLeaf) {
    return (
      <button type="button" className={`hub-facet leaf${node.active ? " on" : ""}`} onClick={() => onSelect(node.id)}>
        <span className="lbl">{node.label}</span>
        <span className="n">{node.n}</span>
      </button>
    );
  }
  const open = openIds.has(node.id);
  return (
    <div className={`hub-formgroup${open ? " open" : ""}`}>
      <button type="button" className={`hub-facet${node.active ? " on" : ""}`} onClick={() => { onSelect(node.id); ensureOpen(node.id); }}>
        <span className="lbl">
          <span className="caret" aria-hidden="true" onClick={(e) => { e.stopPropagation(); onToggleOpen(node.id); }}>{open ? "▾" : "▸"}</span>
          {node.label}
        </span>
        <span className="n">{node.n}</span>
      </button>
      {open && (
        <div className="hub-formleaves">
          {node.children.map((c) => <FormNodeView key={c.id} node={c} onSelect={onSelect} openIds={openIds} onToggleOpen={onToggleOpen} ensureOpen={ensureOpen} />)}
        </div>
      )}
    </div>
  );
}

// 规范媒体类型(对齐 DAM/图库最佳实践):图/视频/音/文档/矢量/3D/工程/字体/其他。
// 原片/GIF/字幕/静音 不再是一级 kind,而是 images/docs/videos 下的「子分面」(见 SUBFACETS)。
// UI:常显 CORE_KINDS;矢量/三维/工程/字体/其他仅在有库存或当前选中时出现,避免空 tab 刷屏。
const KINDS = [
  "images",
  "videos",
  "audio",
  "docs",
  "vector",
  "model3d",
  "project",
  "font",
  "other",
] as const;

const CORE_KINDS = new Set<string>(["images", "videos", "audio", "docs"]);

// 每个 kind 的图标(emoji,零依赖、通用、无需翻译);"" 为「全部」入口。
const KIND_ICON: Record<string, string> = {
  "": "📚",
  images: "🖼️",
  videos: "🎬",
  docs: "📄",
  audio: "🎵",
  project: "🗂️",
  vector: "✒️",
  model3d: "🧊",
  font: "🔤",
  other: "📦",
};

// kind 过滤器从后端 /api/stats.kinds 动态生成(单一真相源在后端):已知 kind 按 KINDS
// 既定顺序,后端新增的未知 kind 自动排后并按字母序,不再依赖前端硬编码同步。
function orderKinds(keys: string[]): string[] {
  const known = KINDS as readonly string[];
  const inOrder = known.filter((k) => keys.includes(k));
  const extra = keys.filter((k) => !known.includes(k)).sort();
  return [...inOrder, ...extra];
}

const KIND_LABEL: Record<string, keyof HubCopy> = {
  images: "kindImages",
  videos: "kindVideos",
  docs: "kindDocs",
  audio: "kindAudio",
  project: "kindProject",
  vector: "kindVector",
  model3d: "kindModel3d",
  font: "kindFont",
  other: "kindOther",
};

// 规范类型下的子分面(格式/角色/衍生)。按 DAM 最佳实践,它们应是类型内的筛选而非独立类型:
//   视频 → 静音(has_audio:0);图片 → 动图(type:gif)/相机原片(type:raw);
//   文档 → 字幕(含译文)/笔记/数据常显;PDF/文稿/表格/演示仅 n>0 或选中时显示。
// 这些子分面来自后端 /api/stats.subfacets 的实时计数。
const SUBFACETS: Record<string, { tag: string; icon: string; label: keyof HubCopy }[]> = {
  videos: [{ tag: "has_audio:0", icon: "🔇", label: "fSilent" }],
  images: [
    { tag: "type:gif", icon: "🎞️", label: "fGif" },
    { tag: "type:raw", icon: "📷", label: "fRaw" },
  ],
  docs: [
    { tag: "type:subs", icon: "💬", label: "fSubs" },
    { tag: "type:text", icon: "📝", label: "fText" },
    { tag: "type:data", icon: "🧾", label: "fData" },
    { tag: "type:pdf", icon: "📕", label: "fPdf" },
    { tag: "type:doc", icon: "📄", label: "fDoc" },
    { tag: "type:sheet", icon: "📊", label: "fSheet" },
    { tag: "type:slide", icon: "📽️", label: "fSlide" },
  ],
};

/** 文档子分面里始终展示的主路径项(与流水线产物对齐)。 */
const CORE_DOC_SUBFACETS = new Set(["type:subs", "type:text", "type:data"]);

// 卡片/详情上的子分面徽标(保留旧 tab 承载的信息)
function facetBadges(m: HubMaterial): { icon: string; label: keyof HubCopy }[] {
  const tags = (m.tags || "").split(",");
  const out: { icon: string; label: keyof HubCopy }[] = [];
  if (tags.includes("has_audio:0")) out.push({ icon: "🔇", label: "fSilent" });
  if (tags.includes("type:gif")) out.push({ icon: "🎞️", label: "fGif" });
  if (tags.includes("type:raw")) out.push({ icon: "📷", label: "fRaw" });
  if (tags.includes("type:subs")) out.push({ icon: "💬", label: "fSubs" });
  if (tags.includes("type:pdf")) out.push({ icon: "📕", label: "fPdf" });
  if (tags.includes("type:doc")) out.push({ icon: "📄", label: "fDoc" });
  if (tags.includes("type:text")) out.push({ icon: "📝", label: "fText" });
  if (tags.includes("type:sheet")) out.push({ icon: "📊", label: "fSheet" });
  if (tags.includes("type:slide")) out.push({ icon: "📽️", label: "fSlide" });
  if (tags.includes("type:data")) out.push({ icon: "🧾", label: "fData" });
  return out;
}

function displayName(m: HubMaterial): string {
  const n = (m.name || "").trim() || "file";
  const ext = (m.ext || "").trim();
  if (!ext) return n;
  const withDot = ext.startsWith(".") ? ext : `.${ext}`;
  if (n.toLowerCase().endsWith(withDot.toLowerCase())) return n;
  return `${n}${withDot}`;
}

function fmtSize(n: number): string {
  return `${(n / 1048576).toFixed(2)} MB`;
}

// 字幕/文稿类素材识别:带 type:subs 标签,或扩展名属于字幕格式。
const SUBS_EXT = ["srt", "vtt", "ass", "ssa", "sbv", "sub", "smi", "lrc"];
function isSubtitle(m: HubMaterial): boolean {
  const tags = (m.tags || "").split(",").map((t) => t.trim());
  if (tags.includes("type:subs")) return true;
  // hub 的 ext 带前导点(".srt"),必须归一后再比,否则此分支恒 false。
  const ext = (m.ext || "").replace(/^\./, "").toLowerCase();
  return SUBS_EXT.includes(ext);
}

/** 详情面板只按真实字幕格式走字幕播放器:带 type:subs 标签的 .txt 笔记仍按文本预览,
 *  否则 SubtitlePanel 解析不到 cue 会显示一片空白。合集归类仍用 isSubtitle(含标签)。 */
function isSubtitleFile(m: HubMaterial): boolean {
  return SUBS_EXT.includes((m.ext || "").replace(/^\./, "").toLowerCase());
}

// 字幕语言:优先取 lang: 标签,否则从文件名末段 `_vi.srt` 解析。
function subtitleLang(m: HubMaterial): string {
  const tags = (m.tags || "").split(",").map((t) => t.trim());
  const lt = tags.find((t) => t.startsWith("lang:"));
  if (lt) return lt.slice(5);
  const m2 = (m.name || "").match(/_([a-zA-Z]{2,5})(?:\.[^.]+)?$/);
  if (m2) return m2[1];
  return "";
}

type SubCue = { n: number; start: number; end: number; text: string };

function tsToSec(s: string): number {
  const t = s.trim().replace(",", ".");
  const p = t.split(":").map(Number);
  if (p.length === 3) return (p[0] || 0) * 3600 + (p[1] || 0) * 60 + (p[2] || 0);
  if (p.length === 2) return (p[0] || 0) * 60 + (p[1] || 0);
  return p[0] || 0;
}

function fmtStamp(sec: number): string {
  const s = Math.max(0, sec);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = Math.floor(s % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
  return `${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
}

// 解析 SRT / VTT(ASS/SSA 仅取文本,时间轴可能不准,足够预览用)。
function parseSubtitles(text: string): SubCue[] {
  const raw = text.replace(/\r\n?/g, "\n");
  const blocks = raw.split(/\n\s*\n/);
  const CUE = /(\d{1,2}:)?\d{1,2}:\d{2}[:.,]\d{1,3}\s*-->\s*(\d{1,2}:)?\d{1,2}:\d{2}[:.,]\d{1,3}/;
  const cues: SubCue[] = [];
  let auto = 0;
  for (const b of blocks) {
    const lines = b.split("\n").map((l) => l.trim()).filter((l) => l !== "");
    if (!lines.length) continue;
    let ti = -1;
    for (let j = 0; j < lines.length; j++) {
      if (CUE.test(lines[j])) { ti = j; break; }
    }
    if (ti === -1) continue;
    const mm = lines[ti].match(CUE);
    if (!mm) continue;
    const parts = mm[0].split("-->");
    const start = tsToSec(parts[0]);
    const end = tsToSec(parts[1]);
    const body = lines.slice(ti + 1).join("\n").trim();
    if (!body) continue;
    let n = auto + 1;
    if (ti > 0 && /^\d+$/.test(lines[0])) n = parseInt(lines[0], 10);
    auto = Math.max(auto, n);
    cues.push({ n, start, end, text: body });
  }
  return cues;
}

// 搜索命中高亮(仅首个命中,足够预览用)
function highlightText(text: string, kw: string): ReactNode {
  if (!kw) return text;
  const i = text.toLowerCase().indexOf(kw);
  if (i < 0) return text;
  return (
    <>
      {text.slice(0, i)}
      <mark>{text.slice(i, i + kw.length)}</mark>
      {text.slice(i + kw.length)}
    </>
  );
}

const SUBS_PAGE = 300; // 长字幕分页渲染,避免一次挂几千 DOM 节点

const CAP_LANG: Record<string, string> = {
  zh: "中文", "zh-Hant": "繁中", en: "英文", vi: "越南文", ja: "日文", ko: "韩文",
  de: "德文", fr: "法文", es: "西班牙文", ru: "俄文", th: "泰文", tr: "土耳其文",
  ar: "阿拉伯文", hi: "印地文", id: "印尼文", pt: "葡萄牙文",
  it: "意大利文", nl: "荷兰文", ms: "马来文", pl: "波兰文",
};
function capLangName(code: string): string {
  return CAP_LANG[code] || code || "字幕";
}

// 播放器字幕层照 YouTube 的做法:
// ① 字幕是播放器 DOM 里的一层(不是浏览器 ::cue 原生轨),字幕文件按「轨」挂到视频上,可开关/切换语言;
// ② 全屏的是整个播放器容器(含字幕层与按钮),不是 <video> 自己,所以放大后字幕不会丢;
// ③ 字幕位置随原生控制条显隐避让,字号可切换并随全屏放大。
// 字幕详情面板:真实文本预览 + 语言/条数/时长 + 搜索高亮 + 复制/下载 + 关联视频跳转/播放跟随。
function SubtitlePanel({ detail, copy }: { detail: HubMaterial; copy: HubCopy }) {
  const [cues, setCues] = useState<SubCue[]>([]);
  const [status, setStatus] = useState<"loading" | "ok" | "err">("loading");
  const [filter, setFilter] = useState("");
  const [videoId, setVideoId] = useState<string | null>(null);
  const [tracks, setTracks] = useState<HubMaterial[]>([]); // 同 job 的多语言字幕轨
  const [trackId, setTrackId] = useState(detail.id);
  const [ccOn, setCcOn] = useState(true);
  const [ccMenu, setCcMenu] = useState(false);
  const [capSize, setCapSize] = useState<"sm" | "md" | "lg">("md");
  const [copied, setCopied] = useState(false);
  const [curT, setCurT] = useState(-1);
  const [limit, setLimit] = useState(SUBS_PAGE);
  const [isFs, setIsFs] = useState(false);
  const vidRef = useRef<HTMLVideoElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const copyTimer = useRef<number | null>(null);
  const activeMat = tracks.find((t) => t.id === trackId) || detail;
  const activeLang = subtitleLang(activeMat) || subtitleLang(detail);

  useEffect(() => () => { if (copyTimer.current) window.clearTimeout(copyTimer.current); }, []);

  // 当前字幕轨文件:切语言只换这一份文本,视频不重载、播放不中断。
  useEffect(() => {
    let cancel = false;
    setStatus("loading");
    setCues([]);
    (async () => {
      try {
        const res = await fetch(hubFileUrl(trackId));
        if (!res.ok) throw new Error(String(res.status));
        const txt = await res.text();
        if (cancel) return;
        setCues(parseSubtitles(txt));
        setStatus("ok");
      } catch {
        if (!cancel) setStatus("err");
      }
    })();
    return () => { cancel = true; };
  }, [trackId]);

  useEffect(() => {
    const sync = () => {
      const fsEl = document.fullscreenElement;
      setIsFs(!!fsEl && (fsEl === wrapRef.current || fsEl === vidRef.current));
    };
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  // 字幕设置菜单:点击播放器其它位置收起(与 YouTube 设置面板一致)
  useEffect(() => {
    if (!ccMenu) return;
    const onDown = (e: PointerEvent) => {
      const el = e.target as Element | null;
      if (!el?.closest?.(".hub-subs-ccmenu, .hub-subs-cc")) setCcMenu(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [ccMenu]);

  const toggleFullscreen = () => {
    if (document.fullscreenElement) {
      void document.exitFullscreen().catch(() => undefined);
      return;
    }
    const el = wrapRef.current;
    if (el?.requestFullscreen) void el.requestFullscreen().catch(() => undefined);
  };

  // 同 job 下:关联视频 + 所有字幕文件(YouTube 的字幕语言菜单)
  useEffect(() => {
    let cancel = false;
    setTrackId(detail.id);
    setTracks([]);
    setCcMenu(false);
    setVideoId(null);
    setFilter("");
    setCurT(-1);
    setLimit(SUBS_PAGE);
    const jobTag = (detail.tags || "").split(",").map((t) => t.trim()).find((t) => t.startsWith("job:"));
    if (jobTag) {
      (async () => {
        const list = await hubFetch<HubMaterial[]>(`/list?tag=${encodeURIComponent(jobTag)}&limit=200`).catch(() => null);
        if (cancel) return;
        const all = list || [];
        const subs = all.filter((x) => isSubtitle(x) && !x.missing);
        subs.sort((a, b) => (a.id === detail.id ? -1 : b.id === detail.id ? 1 : subtitleLang(a).localeCompare(subtitleLang(b))));
        if (subs.length) setTracks(subs);
        // 优先「源片/无字幕」:排除已烧字幕的渲染片(type:render)与无声派生轨
        const tagsOf = (x: HubMaterial) => (x.tags || "").split(",").map((t) => t.trim());
        const has = (x: HubMaterial, t: string) => tagsOf(x).includes(t);
        const isDerived = (x: HubMaterial) => tagsOf(x).some((t) => t.startsWith("parent:"));
        const clean = all.filter((x) => !has(x, "type:render"));
        const v =
          clean.find((x) => x.kind === "videos" && has(x, "type:media") && has(x, "has_audio:1") && !isDerived(x)) ||
          clean.find((x) => x.kind === "videos" && has(x, "type:media") && has(x, "has_audio:1")) ||
          clean.find((x) => x.kind === "videos" && has(x, "type:media")) ||
          clean.find((x) => x.kind === "videos") ||
          all.find((x) => x.kind === "videos") ||
          all[0] ||
          null;
        if (v) setVideoId(v.id);
      })();
    }
    return () => { cancel = true; };
  }, [detail.id, detail.tags]);

  const kw = filter.trim().toLowerCase();
  const filtered = kw ? cues.filter((c) => c.text.toLowerCase().includes(kw)) : cues;
  const shown = filtered.slice(0, limit);
  const duration = cues.length ? cues[cues.length - 1].end - cues[0].start : 0;

  // 播放跟随:高亮当前时间所在的 cue;搜索过滤时不自动滚动,避免打断定位
  const activeCue = useMemo(() => {
    if (curT < 0) return undefined;
    return cues.find((x) => curT >= x.start && curT < x.end);
  }, [curT, cues]);
  const activeN = activeCue ? activeCue.n : -1;
  useEffect(() => {
    if (activeN < 0 || kw || !listRef.current) return;
    const el = listRef.current.querySelector(`[data-cue="${activeN}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }, [activeN, kw]);

  const copyText = () => {
    const body = cues.map((c) => `${fmtStamp(c.start)}\n${c.text}`).join("\n\n");
    void navigator.clipboard?.writeText(body);
    setCopied(true);
    if (copyTimer.current) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
  };
  const seek = (t: number) => {
    if (vidRef.current) {
      vidRef.current.currentTime = t;
      setCurT(t); // 点击立即高亮,不等 timeupdate
    }
  };

  return (
    <div className="hub-subs">
      <div className="hub-subs-bar">
        {activeLang ? <span className="chip ok">{copy.subsLang}: {capLangName(activeLang)}</span> : null}
        <span className="muted">{cues.length} {copy.subsCues}</span>
        {duration > 0 ? <span className="muted">{copy.subsDuration}: {fmtStamp(duration)}</span> : null}
        <span className="sp" />
        <button type="button" onClick={copyText}>{copied ? `✓ ${copy.copied}` : copy.subsCopyText}</button>
        <a href={hubFileUrl(trackId)} download>{copy.subsDownload}</a>
      </div>
      {videoId ? (
        <div className={`hub-subs-vwrap cap-${capSize}`} ref={wrapRef}>
          <video
            ref={vidRef}
            key={videoId}
            src={hubFileUrl(videoId)}
            controls
            controlsList="nofullscreen"
            playsInline
            preload="metadata"
            onTimeUpdate={(e) => setCurT(e.currentTarget.currentTime)}
          />
          {/* 字幕层与 CC/全屏按钮都属于播放器容器;全屏的是容器,不是 <video> 自己 */}
          <div className="hub-subs-tools">
            <button
              type="button"
              className={"hub-subs-cc" + (ccOn ? " on" : "")}
              title="字幕设置 (CC)"
              aria-label="字幕设置"
              aria-expanded={ccMenu}
              onClick={() => setCcMenu((v) => !v)}
            >
              CC
            </button>
            <button
              type="button"
              className="hub-subs-fs"
              title={isFs ? "退出全屏 (Esc)" : "放大播放(带字幕)"}
              aria-label={isFs ? "退出全屏" : "放大播放"}
              onClick={toggleFullscreen}
            >
              <svg viewBox="0 0 16 16" aria-hidden="true">
                {isFs ? (
                  <path d="M6 1.5v4.5H1.5M14.5 6H10v4.5M10 14.5V10h4.5M1.5 10H6v4.5" />
                ) : (
                  <path d="M1.5 6V1.5H6M10 1.5h4.5V6M14.5 10v4.5H10M6 14.5H1.5V10" />
                )}
              </svg>
            </button>
          </div>
          {ccMenu ? (
            <div className="hub-subs-ccmenu">
              <div className="hub-subs-cchead">
                <span>字幕设置</span>
                <button type="button" onClick={() => setCcMenu(false)} aria-label="关闭字幕设置">✕</button>
              </div>
              <button
                type="button"
                className="hub-subs-ccswitch"
                aria-pressed={ccOn}
                onClick={() => setCcOn((v) => !v)}
              >
                {ccOn ? "字幕 · 开" : "字幕 · 关"}
              </button>
              {tracks.length > 1 ? (
                <>
                  <div className="hub-subs-cclabel">字幕语言</div>
                  <div className="hub-subs-cclist">
                    {tracks.map((t) => (
                      <button
                        key={t.id}
                        type="button"
                        className={t.id === trackId ? "on" : ""}
                        onClick={() => setTrackId(t.id)}
                      >
                        {capLangName(subtitleLang(t))}
                      </button>
                    ))}
                  </div>
                </>
              ) : null}
              <div className="hub-subs-cclabel">字号</div>
              <div className="hub-subs-cclist">
                {([["sm", "小"], ["md", "中"], ["lg", "大"]] as const).map(([s, label]) => (
                  <button
                    key={s}
                    type="button"
                    className={capSize === s ? "on" : ""}
                    onClick={() => setCapSize(s)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          {ccOn && activeCue ? (
            <div className="hub-subs-cap" aria-live="polite">{activeCue.text}</div>
          ) : null}
        </div>
      ) : null}
      {status === "loading" ? <div className="hub-subs-msg">…</div> : null}
      {status === "err" ? <div className="hub-subs-msg warn">{copy.subsLoadErr}</div> : null}
      {status === "ok" ? (
        <>
          <input
            className="hub-field"
            placeholder={copy.subsFilter}
            value={filter}
            onChange={(e) => { setFilter(e.target.value); setLimit(SUBS_PAGE); }}
          />
          {videoId ? <div className="hub-subs-hint">{copy.subsSeekHint}</div> : null}
          <div className="hub-subs-list" ref={listRef}>
            {shown.length ? (
              shown.map((c) => (
                <div
                  key={c.n}
                  data-cue={c.n}
                  className={
                    "hub-cue" +
                    (videoId ? " seekable" : "") +
                    (activeN === c.n ? " active" : "")
                  }
                  onClick={videoId ? () => seek(c.start) : undefined}
                >
                  <span className="hub-cue-t">{fmtStamp(c.start)}</span>
                  <span className="hub-cue-tx">{highlightText(c.text, kw)}</span>
                </div>
              ))
            ) : (
              <div className="hub-subs-msg">—</div>
            )}
          </div>
          {filtered.length > shown.length ? (
            <button
              type="button"
              className="hub-subs-more"
              onClick={() => setLimit((l) => l + SUBS_PAGE)}
            >
              {copy.more} ({filtered.length - shown.length})
            </button>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

// 卡片抽成 memo 组件:父组件任意状态(toast/err/gridMode/busy/sideQ 等)变动时,
// 只要各 props(m / kindLabel / copy / tagLabelMap / locale / pendingIds / onOpen / onDelete)
// 引用不变,即可跳过重渲全部可见卡片。handlers 已 useCallback 稳定;其余依赖均为模块级
// 纯函数(displayName/fmtSize/dedupe/chipTagsVisible/formatHubTag/hubThumbUrl/hubFileUrl)。
interface HubCardProps {
  m: HubMaterial;
  copy: HubCopy;
  kindLabel: (k: string) => string;
  tagLabelMap: Record<string, string>;
  locale: string;
  pendingIds: Set<string>;
  onOpen: (m: HubMaterial) => void;
  onDelete: (m: HubMaterial) => void;
  onToggleFavorite?: (m: HubMaterial) => void;
  selected?: boolean;
  onToggleSelect?: (m: HubMaterial) => void;
}

const HubCard = memo(function HubCard({ m, copy, kindLabel, tagLabelMap, locale, pendingIds, onOpen, onDelete, onToggleFavorite, selected, onToggleSelect }: HubCardProps) {
  const vidRef = useRef<HTMLVideoElement>(null);
  // 整卡一点开详情:hub-card-body::after 铺满 article(含缩略图左右空白);
  // 选/删/藏 z-index 更高;视频画面叠在 stretch 之上保留 hover 预览。
  const open = () => onOpen(m);
  return (
    <article className={"hub-card" + (selected ? " selected" : "")}>
      <div className="hub-thumb">
        <button
          type="button"
          className={`hub-card-check${selected ? " on" : ""}`}
          title={copy.select}
          aria-label={copy.select}
          aria-pressed={!!selected}
          onClick={(e) => {
            e.stopPropagation();
            onToggleSelect?.(m);
          }}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="M3.5 8.5l3 3 6-7" />
          </svg>
        </button>
        <button
          type="button"
          className="hub-card-del"
          title={copy.del}
          aria-label={`${copy.del} ${displayName(m)}`}
          onClick={(e) => {
            e.stopPropagation();
            onDelete(m);
          }}
        >
          {/* 垃圾桶图标:aria-label 已含「删除 + 文件名」,无障碍不丢 */}
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="M2.5 4h11" />
            <path d="M5.5 4V2.8c0-.44.36-.8.8-.8h3.4c.44 0 .8.36.8.8V4" />
            <path d="M4 4l.7 9.2c.04.44.4.8.85.8h4.9c.45 0 .81-.36.85-.8L12 4" />
            <path d="M6.5 6.8v4.4M9.5 6.8v4.4" />
          </svg>
        </button>
        <button
          type="button"
          className={`hub-card-fav${m.favorite ? " on" : ""}`}
          title={m.favorite ? copy.unfavorite : copy.favorite}
          aria-label={m.favorite ? copy.unfavorite : copy.favorite}
          aria-pressed={!!m.favorite}
          onClick={(e) => {
            e.stopPropagation();
            onToggleFavorite?.(m);
          }}
        >
          <svg viewBox="0 0 16 16" aria-hidden="true">
            <path d="M8 1.5l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.1 4.2 13.3l.7-4.3-3.1-3 4.3-.6z" />
          </svg>
        </button>
        {m.missing ? (
          <span className="pill bad">{copy.missing}</span>
        ) : m.kind === "images" ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={m.thumb ? hubThumbUrl(m.id) : hubFileUrl(m.id)}
            alt=""
            loading="lazy"
            decoding="async"
          />
        ) : m.kind === "videos" ? (
          <>
            {/* 视频卡片:hover 静音预览;点击整卡开详情(与图片/文档一致)。
                z-index 高于 body::after,由本节点自己接 click。 */}
            <video
              ref={vidRef}
              src={hubFileUrl(m.id)}
              muted
              playsInline
              loop
              preload="metadata"
              poster={m.thumb ? hubThumbUrl(m.id) : undefined}
              onLoadedMetadata={(e) => {
                if (m.hit_t) e.currentTarget.currentTime = m.hit_t;
              }}
              onMouseEnter={() => {
                const v = vidRef.current;
                if (!v) return;
                if (m.hit_t != null) v.currentTime = m.hit_t;
                void v.play().catch(() => undefined);
              }}
              onMouseLeave={() => {
                const v = vidRef.current;
                if (!v) return;
                v.pause();
                if (m.hit_t != null) v.currentTime = m.hit_t;
              }}
              onClick={(e) => {
                e.stopPropagation();
                open();
              }}
            />
          </>
        ) : m.kind === "audio" ? (
          <span className="ph" role="presentation">
            ♪
          </span>
        ) : (
          <span className="pill" role="presentation">
            {(m.ext || "?").replace(/^\./, "").slice(0, 5).toUpperCase()}
          </span>
        )}
        {/* 类型角标:所有类型都显示,图标 + 强调色,一眼区分视频/图片/音频 */}
        <span className={`hub-kind-badge k-${m.kind}`} title={kindLabel(m.kind)}>
          <span className="ki" aria-hidden="true">{KIND_ICON[m.kind] || "📦"}</span>
          {kindLabel(m.kind)}
        </span>
      </div>
      <button
        type="button"
        className="hub-card-body"
        aria-label={displayName(m)}
        onClick={open}
      >
        <strong title={displayName(m)}>{displayName(m)}</strong>
        {facetBadges(m).length > 0 && (
          <span className="hub-card-badges">
            {facetBadges(m).map((b) => (
              <span key={b.label} className="hub-mini-badge" title={copy[b.label]}>
                <span aria-hidden="true">{b.icon}</span> {copy[b.label]}
              </span>
            ))}
          </span>
        )}
        {m.hit_t != null ? (
          <span className="hub-hit" title={copy.hitReason}>
            {m.hit_via === "asr"
              ? copy.hitBadgeSpeech
              : m.hit_via === "clip"
                ? copy.hitBadgeSim
                : copy.hitBadgeFrame}
            {" "}
            {m.hit_t.toFixed(1)}s
            {m.hit_shot != null
              ? ` · ${copy.hitShot} ${m.hit_shot.toFixed(1)}s`
              : ""}
          </span>
        ) : null}
        <span className="muted">
          {kindLabel(m.kind)} · {fmtSize(m.size || 0)}
        </span>
        {m.description ? (
          <p className="hub-desc" title={m.description}>
            {m.description.replace(/\n+/g, " ").slice(0, 90)}
          </p>
        ) : null}
        <div className="hub-chips">
          {pendingIds.has(m.id) ? (
            <span className="chip hub-pending" title={copy.pendingBadge}>
              {copy.pendingBadge}
            </span>
          ) : null}
          {dedupe(chipTagsVisible(m.tags))
            .slice(0, 4)
            .map((x, i) => (
              <span key={`${x}-${i}`} className="chip" title={x}>
                {tagLabelMap[x] ?? formatHubTag(x, locale)}
              </span>
            ))}
        </div>
      </button>
    </article>
  );
});

/* ───────────────── 视频合集 ─────────────────
 * 流水线给同一个视频(job:<id>)的源片、音轨、字幕、翻译都打了 job: 锚点,
 * 拆条子件只有 parent: 时沿父链回查同一任务。默认把 >=2 项的任务折成一张
 * 可展开的合集卡,避免一个视频刷出几十张平铺卡片;单件素材仍按原样展示。
 */
/** 字幕与译文是同一批语言产物,只用一个 tab 承载;流水线 sidecar JSON 归文档。
 * 图片(kind=images,如截图/预览/关键帧)单独成桶,不再被误算进「文档」。 */
type CollCat = "translate" | "videos" | "audio" | "images" | "docs";
const COLL_CATS: CollCat[] = ["translate", "videos", "audio", "images", "docs"];
const COLL_LABEL: Record<CollCat, keyof HubCopy> = {
  translate: "collSubsPack", videos: "kindVideos", audio: "kindAudio",
  images: "kindImages", docs: "kindDocs",
};
const COLL_ICON: Record<CollCat, string> = {
  translate: "💬", videos: "🎬", audio: "🎵", images: "🖼️", docs: "📄",
};

function collectionCat(m: HubMaterial): CollCat {
  const ext = (m.ext || "").replace(/^\./, "").toLowerCase();
  if (isSubtitle(m) || SUBS_EXT.includes(ext)) return "translate";
  if (m.kind === "videos") return "videos";
  if (m.kind === "audio") return "audio";
  if (m.kind === "images") return "images";
  return "docs";
}

function collectionKeyOf(m: HubMaterial, byId: Map<string, HubMaterial>): string | null {
  let cur: HubMaterial | undefined = m;
  const seen = new Set<string>();
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id);
    const tags = csv(cur.tags);
    const job = tags.find((t) => t.startsWith("job:"));
    if (job) return job.slice(4);
    const par = tags.find((t) => t.startsWith("parent:"));
    if (!par) {
      // 无流水线 job/parent 时,用户 album: 也可折成合集卡(单件也显示,便于策展)。
      if (cur === m) {
        const album = tags.find((t) => t.startsWith("album:"));
        if (album) return album;
      }
      return null;
    }
    const pid = par.slice(7);
    const next = byId.get(pid);
    if (!next) return `parent:${pid}`;
    cur = next;
  }
  return null;
}

/** 封面取被 parent: 引用最多的母版,其次 role:master / type:media 源片。 */
function collectionCover(members: HubMaterial[]): HubMaterial {
  const refs = new Map<string, number>();
  for (const m of members) {
    const par = csv(m.tags).find((t) => t.startsWith("parent:"));
    if (par) {
      const pid = par.slice(7);
      refs.set(pid, (refs.get(pid) || 0) + 1);
    }
  }
  const score = (m: HubMaterial) => {
    const tags = csv(m.tags);
    return (refs.get(m.id) || 0) * 100
      + (tags.includes("role:master") ? 50 : 0)
      + (m.kind === "videos" ? 20 : 0)
      + (tags.includes("type:media") ? 10 : 0)
      - (tags.some((t) => t.startsWith("parent:")) ? 10 : 0)
      - (/(silent|wm_only|dehardsub|demark|keepsub)/i.test(m.name || "") ? 5 : 0);
  };
  return members.reduce((best, m) => (score(m) > score(best) ? m : best), members[0]);
}

type HubGroup = {
  key: string;
  jobTag: string | null;
  title: string;
  cover: HubMaterial;
  members: HubMaterial[];
  counts: Record<CollCat, number>;
  platform: string;
};
type GridEntry = { type: "group"; group: HubGroup } | { type: "card"; m: HubMaterial };

function buildGridEntries(rows: HubMaterial[]): GridEntry[] {
  const byId = new Map(rows.map((r) => [r.id, r]));
  const buckets = new Map<string, HubMaterial[]>();
  const keys = new Map<string, string | null>();
  for (const m of rows) {
    const k = collectionKeyOf(m, byId);
    keys.set(m.id, k);
    if (!k) continue;
    const arr = buckets.get(k);
    if (arr) arr.push(m);
    else buckets.set(k, [m]);
  }
  const out: GridEntry[] = [];
  const loose: GridEntry[] = [];
  const emitted = new Set<string>();
  for (const m of rows) {
    const k = keys.get(m.id) || null;
    const isAlbum = !!k && k.startsWith("album:");
    // 流水线 job 合集至少 2 项才折叠;用户 album 单件也成卡,方便「刚建合集」可见。
    const minN = isAlbum ? 1 : 2;
    if (!k || (buckets.get(k)?.length || 0) < minN) {
      // 散件不按 rows 原序就地插入:合集卡是 grid-column:1/-1 全宽行,夹在两条
      // 全宽行之间的单张散件会独占一列、右侧大片留白,看着像挂在上面那个合集里。
      // 统一沉到末尾成块,合集连续排列、散件并排填满,避免错位。
      loose.push({ type: "card", m });
      continue;
    }
    if (emitted.has(k)) continue;
    emitted.add(k);
    const members = buckets.get(k)!;
    const counts: Record<CollCat, number> = { translate: 0, videos: 0, audio: 0, images: 0, docs: 0 };
    for (const x of members) counts[collectionCat(x)] += 1;
    const cover = collectionCover(members);
    const jobTag = isAlbum ? k : (k.startsWith("parent:") ? null : `job:${k}`);
    const platform = members
      .flatMap((x) => csv(x.tags))
      .find((t) => t === "bilibili" || t === "douyin" || t === "youtube") || "";
    out.push({
      type: "group",
      group: {
        key: k,
        jobTag,
        title: isAlbum ? albumTitleOf(k) : (jobTag ? k : displayName(cover)),
        cover,
        members,
        counts,
        platform,
      },
    });
  }
  return out.concat(loose);
}

type HubCardCtx = {
  copy: HubCopy;
  kindLabel: (k: string) => string;
  tagLabelMap: Record<string, string>;
  locale: string;
  pendingIds: Set<string>;
  selected: Set<string>;
  selectMode: boolean;
  openDetail: (m: HubMaterial) => void;
  removeMaterial: (m: HubMaterial) => void;
  toggleFav: (m: HubMaterial) => void;
  toggleSelect: (m: HubMaterial) => void;
};

const HubCollection = memo(function HubCollection({
  group, defaultOpen, cardCtx, onFilterJob, onSelectGroup, onRenameColl, onDissolveColl,
}: {
  group: HubGroup;
  defaultOpen: boolean;
  cardCtx: HubCardCtx;
  onFilterJob: (tag: string) => void;
  onSelectGroup: (ids: string[]) => void;
  onRenameColl?: (tag: string, title: string) => void;
  onDissolveColl?: (tag: string, title: string, kind: "album" | "job") => void;
}) {
  const { copy } = cardCtx;
  const [open, setOpen] = useState(defaultOpen);
  const [cat, setCat] = useState<CollCat | "all">("all");
  const isAlbum = group.key.startsWith("album:");
  const collKind = isAlbum ? "album" as const : "job" as const;
  const collLabel = isAlbum ? copy.collAlbumTitle : copy.collTitle;
  // 从合集点「只看此合集」后自动展开,省掉再点一次
  useEffect(() => { if (defaultOpen) setOpen(true); }, [defaultOpen]);
  const domId = `hub-coll-${group.key.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  const ids = group.members.map((x) => x.id);
  const allSel = ids.length > 0 && ids.every((id) => cardCtx.selected.has(id));
  const shown = cat === "all" ? group.members : group.members.filter((x) => collectionCat(x) === cat);
  const cover = group.cover;
  const coverSrc = cover.thumb
    ? hubThumbUrl(cover.id)
    : cover.kind === "images" ? hubFileUrl(cover.id) : "";
  const total = group.members.length;
  const collTag = group.jobTag || (isAlbum ? group.key : null);
  return (
    <section className={`hub-collection${open ? " open" : ""}`} aria-label={`${collLabel} ${group.title}`}>
      <div className="hub-collection-head">
        <button
          type="button"
          className="hub-collection-main"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls={domId}
        >
          <span className="hub-collection-cover">
            {coverSrc ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img className="face" src={coverSrc} alt="" loading="lazy" decoding="async" />
            ) : (
              <span className="face" aria-hidden="true">{KIND_ICON[cover.kind] || "📚"}</span>
            )}
            <span className="hub-collection-cover-n">{total}</span>
          </span>
          <span className="hub-collection-info">
            <span className="hub-collection-title">
              <span className="hub-collection-tag">{collLabel}</span>
              <strong title={group.title}>{group.title}</strong>
            </span>
            <span className="hub-collection-sub" title={displayName(cover)}>{displayName(cover)}</span>
            <span className="hub-collection-stats">
              <span className="hub-collection-stat total">{fillCopy(copy.collCount, { n: total })}</span>
              {group.platform ? (
                <span className="hub-collection-stat">
                  {cardCtx.tagLabelMap[group.platform] ?? formatHubTag(group.platform, cardCtx.locale)}
                </span>
              ) : null}
              {COLL_CATS.filter((c) => group.counts[c] > 0).map((c) => (
                <span key={c} className="hub-collection-stat">
                  <span aria-hidden="true">{COLL_ICON[c]}</span> {copy[COLL_LABEL[c]]} {group.counts[c]}
                </span>
              ))}
            </span>
          </span>
          <span className="hub-collection-caret" aria-hidden="true">{open ? "▴" : "▾"}</span>
        </button>
        <div className="hub-collection-actions">
          {collTag ? (
            <button
              type="button"
              title={copy.collFilterJob}
              onClick={() => onFilterJob(collTag)}
            >
              {copy.collFilterJob}
            </button>
          ) : null}
          <button type="button" aria-pressed={allSel} onClick={() => onSelectGroup(ids)}>
            {allSel ? copy.collDeselectAll : copy.collSelectAll}
          </button>
          {isAlbum && collTag && onRenameColl ? (
            <button type="button" title={copy.albumRename} onClick={() => onRenameColl(collTag, group.title)}>
              {copy.albumRename}
            </button>
          ) : null}
          {collTag && onDissolveColl ? (
            <button
              type="button"
              className="hub-coll-danger"
              title={isAlbum ? copy.albumDelete : copy.jobDissolve}
              onClick={() => onDissolveColl(collTag, group.title, collKind)}
            >
              {isAlbum ? copy.albumDelete : copy.jobDissolve}
            </button>
          ) : null}
          <button type="button" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            {open ? copy.collCollapse : copy.collExpand}
          </button>
        </div>
      </div>
      {open && (
        <div className="hub-collection-body" id={domId}>
          <div className="hub-collection-tabs" role="group" aria-label={copy.collTitle}>
            <button
              type="button"
              className={`hub-collection-tab${cat === "all" ? " on" : ""}`}
              onClick={() => setCat("all")}
            >
              {copy.all} <span className="n">{total}</span>
            </button>
            {COLL_CATS.filter((c) => group.counts[c] > 0).map((c) => (
              <button
                key={c}
                type="button"
                className={`hub-collection-tab${cat === c ? " on" : ""}`}
                onClick={() => setCat(c)}
              >
                <span aria-hidden="true">{COLL_ICON[c]}</span> {copy[COLL_LABEL[c]]}
                <span className="n">{group.counts[c]}</span>
              </button>
            ))}
          </div>
          <div className="hub-grid hub-collection-grid">
            {shown.map((x) => (
              <HubCard
                key={x.id}
                m={x}
                copy={cardCtx.copy}
                kindLabel={cardCtx.kindLabel}
                tagLabelMap={cardCtx.tagLabelMap}
                locale={cardCtx.locale}
                pendingIds={cardCtx.pendingIds}
                selected={cardCtx.selected.has(x.id)}
                onOpen={cardCtx.selectMode ? cardCtx.toggleSelect : cardCtx.openDetail}
                onDelete={cardCtx.removeMaterial}
                onToggleFavorite={cardCtx.toggleFav}
                onToggleSelect={cardCtx.toggleSelect}
              />
            ))}
          </div>
        </div>
      )}
    </section>
  );
});

export function MaterialsHub({ copy }: { copy: HubCopy }) {
  const params = useParams();
  const locale = typeof params?.locale === "string" ? params.locale : "zh";
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<string>("");
  const [tag, setTag] = useState("");
  const [mode, setMode] = useState("lexical");
  const [sort, setSort] = useState("");
  const [page, setPage] = useState(1);
  const pageSize = 48;
  const [rows, setRows] = useState<HubMaterial[]>([]);
  const [total, setTotal] = useState(0);
  // 无限滚动累积用:记录上一次查询签名与页码,判断本次是「翻页追加」还是「筛选重置/替换」
  const lastQuerySig = useRef("");
  const lastPage = useRef(1);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const [stats, setStats] = useState<HubStats | null>(null);
  const [tags, setTags] = useState<HubTag[]>([]);
  // 首屏默认 busy:首帧就出骨架,避免先闪「没有匹配的素材」空态
  const [busy, setBusy] = useState(true);
  const [err, setErr] = useState("");
  const [toast, setToast] = useState("");
  const [upNote, setUpNote] = useState("");
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());
  const [filtersOpen, setFiltersOpen] = useState(false);

  // 多选:卡片左上勾选框 → 底部批量浮条(加入合集/移动/删除/收藏)
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // 拖拽上传:落区高亮
  const [dragOver, setDragOver] = useState(false);
  // 批量管理模式(B 站「批量管理」):开启后卡片常显勾选框、点卡片即选中而非打开
  const [selectMode, setSelectMode] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const bulkBusyRef = useRef(false);
  const [albumOpen, setAlbumOpen] = useState(false);
  const [moveOpen, setMoveOpen] = useState(false);
  const [leaveOpen, setLeaveOpen] = useState(false);
  const [albumName, setAlbumName] = useState("");
  // 结构筛选切换时清列表出骨架,避免旧结果闪一下再变
  const [skelReset, setSkelReset] = useState(false);
  // 视图 Tab:合集 / 散件 / 全部。
  // SSR 铁律(同 UP_KEY):首帧固定 "coll",挂载后再读 localStorage。
  // 未 hydrate 前绝不回写,否则会把用户偏好「散件」覆盖成默认「合集」。
  type CollView = "coll" | "loose" | "flat";
  const [collView, setCollView] = useState<CollView>("coll");
  const [collHydrated, setCollHydrated] = useState(false);
  useEffect(() => {
    try {
      const saved = localStorage.getItem("vitual.hub.collectionView");
      if (saved === "coll" || saved === "loose" || saved === "flat") setCollView(saved);
      else if (localStorage.getItem("vitual.hub.collectionMode") === "0") setCollView("flat");
    } catch { /* 隐私模式 */ }
    setCollHydrated(true);
  }, []);
  useEffect(() => {
    if (!collHydrated) return;
    try { localStorage.setItem("vitual.hub.collectionView", collView); } catch { /* ignore */ }
  }, [collView, collHydrated]);
  // page 不进 loadQuery deps:校正页码时用 pageRef,避免 setPage 再触发一轮 busy/骨架
  const pageRef = useRef(page);
  pageRef.current = page;
  const bootedQuery = useRef(false);
  const pageQueryBoot = useRef(true);
  const [maxUp, setMaxUp] = useState(HUB_UPLOAD_MAX_BYTES);
  const [detail, setDetail] = useState<HubMaterial | null>(null);
  // removeMaterial 用 useCallback 稳定身份,但需读当前 detail 决定是否清面板——
  // 若直接捕获 detail 会使回调随 detail 变而变(deps 不稳定,memo 失效),故用 ref。
  const detailRef = useRef<HubMaterial | null>(null);
  detailRef.current = detail;
  // 步骤 1/4/5:入库链逐步状态 + 结构化理解记录(派生 sidecar,只读展示)
  const [runRec, setRunRec] = useState<HubRunRecord | null>(null);
  const [understandRec, setUnderstandRec] = useState<HubUnderstand | null>(null);
  // 步骤 5/7:该素材所属 job 的分发渠道就绪度
  const [readiness, setReadiness] = useState<HubReadiness | null>(null);
  // 步骤 6:按需交付(计划 → 确认导出 → 下载)
  const [exportLang, setExportLang] = useState<string>("");
  const [exportResult, setExportResult] = useState<HubDeliverResult | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  // 批量栏「导出」:与详情导出独立,支持一次交付多个 job 的素材 + 指定语种
  const [bulkExportOpen, setBulkExportOpen] = useState(false);
  const [bulkExportLang, setBulkExportLang] = useState("");
  const [bulkExportResult, setBulkExportResult] = useState<HubDeliverResult | null>(null);
  const [bulkExportBusy, setBulkExportBusy] = useState(false);
  const [editTags, setEditTags] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [zoom, setZoom] = useState(false);
  const [one2one, setOne2One] = useState(false);

  // 内容形式多层级筛选(主轴)+ 题材/用途(多选标签)
  const [taxonomy, setTaxonomy] = useState<HubTaxonomy | null>(null);
  const [formSel, setFormSel] = useState("");   // 选中一级分组 或 二级 leaf
  const [topicSel, setTopicSel] = useState("");
  const [usageSel, setUsageSel] = useState("");
  // 仅看收藏:置顶筛选视图(后端 favorites_only);与形式/题材/用途分面正交。
  const [favOnly, setFavOnly] = useState(false);
  // 上传默认分类(传入新素材的 form/topics/usage)
  // 上传默认分类:持久化到 localStorage——否则刷新页面后设置就丢(用户以为"点了没用")。
  // SSR 安全铁律:首渲染一律空值(SSR 无 localStorage),挂载后再读回;在 useState 初始化器里
  // 读 localStorage 会导致服务端/客户端首帧不一致 → hydration mismatch(Recoverable Error)。
  const UP_KEY = "vitual.hub.uploadDefaults";
  const [upForm, setUpForm] = useState("");
  const [upTopics, setUpTopics] = useState<string[]>([]);
  const [upUsage, setUpUsage] = useState<string[]>([]);
  const [upHydrated, setUpHydrated] = useState(false);
  useEffect(() => {
    try {
      const raw = localStorage.getItem(UP_KEY);
      if (raw) {
        const o = JSON.parse(raw) as { form?: string; topics?: string[]; usage?: string[] };
        if (o.form) setUpForm(o.form);
        if (Array.isArray(o.topics)) setUpTopics(o.topics);
        if (Array.isArray(o.usage)) setUpUsage(o.usage);
      }
    } catch { /* 存档损坏:忽略,按空值开始 */ }
    setUpHydrated(true);
  }, []);
  useEffect(() => {
    if (!upHydrated) return; // 读回之前绝不回写,否则挂载首跑会把存档清空
    try {
      localStorage.setItem(UP_KEY, JSON.stringify({ form: upForm, topics: upTopics, usage: upUsage }));
    } catch { /* 隐私模式/配额满:忽略,不影响上传 */ }
  }, [upForm, upTopics, upUsage, upHydrated]);
  const fileRef = useRef<HTMLInputElement>(null);
  const loadSeq = useRef(0);

  useEffect(() => {
    if (!detail) {
      setZoom(false);
      setOne2One(false);
    }
  }, [detail]);
  useEffect(() => {
    // Esc 关闭:有 lightbox 时先退 lightbox,否则关详情弹窗。
    // 原实现只在 zoom(lightbox)打开时才挂监听,详情弹窗按 Esc 无任何反应。
    if (!detail) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (zoom) {
        setZoom(false);
        setOne2One(false);
      } else {
        setDetail(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detail, zoom]);

  const kindLabel = useCallback(
    (k: string) => {
      const key = KIND_LABEL[k];
      return key ? copy[key] : k; // 未知 kind(后端新增)显示原名,动态化不再依赖硬编码
    },
    [copy],
  );

  // 切换类型时清空子分面筛选(类型与子分面是正交的两级筛选)
  const chooseKind = useCallback(
    (k: string) => {
      const next = kind === k ? "" : k;
      setKind(next);
      setTag("");
      setPage(1);
    },
    [kind],
  );

  const subfacets = stats?.subfacets;

  // 查询类数据(count + 当前页 list)。所有维度(含 form/topic/usage 分面)均由服务端筛选,
  // 服务端在分页前过滤;大库下不再把整库 JSON 拉到浏览器做本地过滤。
  const loadQuery = useCallback(async () => {
    const seq = ++loadSeq.current;
    setBusy(true);
    setErr("");
    try {
      const qs = new URLSearchParams();
      if (q.trim()) qs.set("q", q.trim());
      if (kind) qs.set("kind", kind);
      if (tag) qs.set("tag", tag);
      if (mode) qs.set("mode", mode);
      if (sort) qs.set("sort", sort);
      if (favOnly) qs.set("favorites_only", "1");
      // 受控词表分面:后端 /api/list 与 /api/count 已支持,口径统一、分页正确。
      if (formSel) qs.set("form", formSel);
      if (topicSel) qs.set("topic", topicSel);
      if (usageSel) qs.set("usage", usageSel);
      // 查询签名:仅含影响数据集合的参数;locale 只影响文案,不计。
      const querySig = [q.trim(), kind, tag, mode, sort, favOnly ? "1" : "", formSel, topicSel, usageSel].join("|");
      const count = await hubFetch<{ total: number }>(`/count?${qs}`);
      if (loadSeq.current !== seq) return;
      const tTotal = count.total || 0;
      setTotal(tTotal);
      const pages = Math.max(1, Math.ceil(tTotal / pageSize));
      const pageNow = pageRef.current;
      const p = Math.min(pageNow, pages);
      if (p !== pageNow) {
        pageRef.current = p;
        setPage(p);
      }
      // 同签名且页码「严格递增一页」→ 只取新一页追加;其余情况(筛选变化 / 跳页 /
      // 同页重复请求 / 并发请求被 loadSeq 取消)一律重取 0..p*pageSize 全窗口。
      // 必须用 p === lastPage.current + 1 而非 p > lastPage.current:
      // 无限滚动哨兵(rootMargin 提前翻页)与「加载更多」按钮同批次连触发会让 page
      // 从 2 直接跳到 4,若只取最后一页,中间页会永久丢失(实测 138/186 = 48+48+42,
      // 缺第 3 页;page 到顶后按钮与哨兵都停,用户再无法补回)。合集折叠后网格变矮,
      // 这个竞态更容易撞上(另一次实测 59 条只剩最后 11 条)。
      const append = querySig === lastQuerySig.current && p === lastPage.current + 1;
      if (append) {
        qs.set("offset", String((p - 1) * pageSize));
        qs.set("limit", String(pageSize));
      } else {
        qs.set("offset", "0");
        qs.set("limit", String(p * pageSize));
      }
      const list = await hubFetch<HubMaterial[]>(`/list?${qs}`);
      if (loadSeq.current !== seq) return;
      const rowsAll: HubMaterial[] = Array.isArray(list) ? list : [];
      // 分页路径:total 已由 /count 写入,不能再用本页 rows 长度覆盖(否则共 N 条会变成 pageSize)。
      setRows((prev) => (append ? [...prev, ...rowsAll] : rowsAll));
      setSkelReset(false);
      lastQuerySig.current = querySig;
      lastPage.current = p;
    } catch (e) {
      if (loadSeq.current !== seq) return;
      setRows([]);
      setSkelReset(false);
      setErr(e instanceof Error ? e.message : copy.error);
    } finally {
      if (loadSeq.current === seq) setBusy(false);
    }
  }, [q, kind, tag, mode, sort, formSel, topicSel, usageSel, favOnly, copy.error]);

  // 筛选集合变化(搜索/排序/类型/标签/分面/仅看收藏)时,已选行可能不在新结果里,
  // 继续批量操作会打到不可见数据 —— 清空选择并退出批量模式。
  // 翻页(page)刻意不在依赖里:跨页保留选择是有意行为。
  useEffect(() => {
    setSelected(new Set());
    setSelectMode(false);
  }, [q, kind, tag, mode, sort, formSel, topicSel, usageSel, favOnly]);

  // 切换收藏:乐观更新本页 rows(收藏置顶用稳定排序,组内保持当前顺序);
  // 若处于「仅看收藏」模式且取消收藏,该卡片立即移出列表。失败回滚。
  const toggleFav = useCallback((m: HubMaterial) => {
    const next = !m.favorite;
    setRows((prev) => {
      let out = prev.map((r) => (r.id === m.id ? { ...r, favorite: next } : r));
      out = out.slice().sort((a, b) => (b.favorite ? 1 : 0) - (a.favorite ? 1 : 0));
      if (favOnly && !next) out = out.filter((r) => r.id !== m.id);
      return out;
    });
    hubSetFavorite(m.id, next).catch(() => {
      setRows((prev) => {
        // 仅看收藏下取消收藏会立即移除卡片,回滚时必须把原条目插回,
        // 否则失败后「取消」了收藏却永久看不到这张卡片。
        let out = prev.map((r) => (r.id === m.id ? { ...r, favorite: !next } : r));
        if (favOnly && !next && !out.some((r) => r.id === m.id)) {
          out = [...out, m];
        }
        return out.slice().sort((a, b) => (b.favorite ? 1 : 0) - (a.favorite ? 1 : 0));
      });
    });
  }, [favOnly]);

  // 全局统计/词表(stats + tags):与筛选无关,只在挂载与结构性变更(上传/删除/保存)时刷新,
  // 不再每次敲字都重拉,避免无谓的串行请求拖慢交互。
  const loadMeta = useCallback(async () => {
    try {
      const [st, tg] = await Promise.all([
        hubFetch<HubStats>("/stats"),
        hubFetch<HubTag[]>(`/tags?ui=1&lang=${encodeURIComponent(locale.startsWith("zh") ? "zh" : "en")}`).catch(() => [] as HubTag[]),
      ]);
      setStats(st);
      setTags(Array.isArray(tg) ? tg : []);
    } catch {
      /* 保留上一次的 stats/tags,不让一次失败清空分面 */
    }
  }, [locale, copy.error]);

  // 完整刷新(上传/删除/保存后调用):查询 + 全局元信息一起并行拉。
  const load = useCallback(async () => {
    await Promise.all([loadQuery(), loadMeta()]);
  }, [loadQuery, loadMeta]);

  // 结构筛选(类型/分面/收藏)变化 → 立刻清空出骨架;纯搜索词变化保留旧结果防抖,不闪空。
  const structSig = [kind, tag, formSel, topicSel, usageSel, favOnly ? "1" : ""].join("|");
  const structSigRef = useRef(structSig);
  useEffect(() => {
    if (structSigRef.current !== structSig) {
      structSigRef.current = structSig;
      setSkelReset(true);
      setRows([]);
      setSelected(new Set());
    }
  }, [structSig]);

  useEffect(() => {
    // 250ms 防抖:搜索框每敲一个字不再立刻发请求。首屏 delay=0 少空等。
    // busy 立即置 true:防抖窗口内不能闪现「没有匹配的素材」空态。
    setBusy(true);
    const delay = bootedQuery.current ? 250 : 0;
    bootedQuery.current = true;
    const t = window.setTimeout(() => void loadQuery(), delay);
    return () => window.clearTimeout(t);
  }, [loadQuery]);

  // 仅翻页(无限滚动 / 加载更多):page 已从 loadQuery deps 拆出,这里单独跟。
  // 故意不把 loadQuery 放进 deps,否则筛选一变会和上面的防抖 effect 各打一枪。
  const loadQueryRef = useRef(loadQuery);
  loadQueryRef.current = loadQuery;
  useEffect(() => {
    if (pageQueryBoot.current) {
      pageQueryBoot.current = false;
      return;
    }
    void loadQueryRef.current();
  }, [page]);

  // Esc:关闭浮层 / 退出多选(DAM 批量栏惯例)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // 详情/灯箱的 Esc 由详情弹窗自己的监听独占处理;这里跳过,
      // 否则关详情的同时会把批量选择也清空。
      if (detail) return;
      if (albumOpen) { setAlbumOpen(false); return; }
      if (moveOpen) { setMoveOpen(false); return; }
      if (leaveOpen) { setLeaveOpen(false); return; }
      if (bulkExportOpen) { setBulkExportOpen(false); return; }
      if (selected.size || selectMode) {
        setSelected(new Set());
        setSelectMode(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detail, albumOpen, moveOpen, leaveOpen, bulkExportOpen, selected.size, selectMode]);

  // 无限滚动(Douyin / B 站式):滚到底部(提前 400px)自动加载下一页;
  // 同时保留「加载更多」按钮作为无观察者环境/手动兜底。仅在没有更多页时停用。
  useEffect(() => {
    if (busy) return;
    const pg = Math.max(1, Math.ceil(total / pageSize));
    if (page >= pg) return;
    const el = sentinelRef.current;
    if (!el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting) setPage((p) => Math.min(pg, p + 1));
      },
      { rootMargin: "400px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [busy, page, total, pageSize]);

  // 挂载时拉一次全局统计/词表(分面计数、标签 UI),之后仅在上传/删除/保存后由 load() 刷新。
  useEffect(() => {
    void loadMeta();
  }, [loadMeta]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(""), 2200);
    return () => window.clearTimeout(t);
  }, [toast]);

  const pages = Math.max(1, Math.ceil(total / pageSize));

  const facets = useMemo(() => {
    const kinds = stats?.kinds || {};
    // 常显图/视频/音/文档;冷门 kind 仅有库存或当前选中时出现。
    // 后端未回报的 CORE_KINDS 也补 0,保证主路径 tab 稳定。
    const keys = new Set([...Object.keys(kinds), ...CORE_KINDS]);
    const ordered = orderKinds([...keys]).filter(
      (k) => CORE_KINDS.has(k) || (kinds[k] || 0) > 0 || kind === k,
    );
    return [
      { val: "", label: copy.all, n: stats?.total || 0 },
      ...ordered.map((k) => ({
        val: k,
        label: kindLabel(k),
        n: kinds[k] || 0,
      })),
    ];
  }, [stats, copy.all, kindLabel, kind]);

  // 工具栏「筛选 (N)」按钮的激活分面计数:侧栏分面(内容形式/题材/用途/标签),不含顶部类型标签与收藏
  const activeFilterCount = useMemo(
    () => [formSel, topicSel, usageSel, tag].filter(Boolean).length,
    [formSel, topicSel, usageSel, tag],
  );

  // 内容形式筛选数据:递归构建多级树(计数来自 stats.forms 按 leaf;上层由 form_descendants 聚合)
  const formGroups = useMemo(() => {
    const forms = stats?.forms || {};
    const sub = taxonomy?.form_subgroups || {};
    const subEn = taxonomy?.form_subgroups_en || {};
    const grp = taxonomy?.form_groups || {};
    const grpEn = taxonomy?.form_groups_en || {};
    const desc = taxonomy?.form_descendants || {};
    const leafCount = (id: string) => {
      const d = desc[id];
      // 防御性去重:taxonomy 的 descendants 若含重复 id(历史上 other 曾重复),
      // 累加会把 facet 计数翻倍;去重不改变「选父即选全部后代」的筛选语义。
      if (d) return Array.from(new Set(d)).reduce((s: number, lid: string) => s + (forms[lid] || 0), 0);
      return forms[id] || 0;
    };
    // 英文模式:叶子取 form_options_en 路径末段(树里叶子值是中文标签,见 core.FORM_TAXONOMY)
    const optEn = new Map((taxonomy?.form_options_en || []) as [string, string][]);
    const build = (node: Record<string, FormNodeData>): FormNode[] =>
      Object.entries(node).map(([id, val]) => {
        const isLeaf = typeof val === "string";
        const label = isLeaf
          ? (locale.startsWith("en") ? (optEn.get(id)?.split(" › ").pop() ?? val) : val)
          : (
            locale.startsWith("en")
              ? (grpEn[id] ?? subEn[id] ?? grp[id] ?? sub[id] ?? id)
              : (grp[id] ?? sub[id] ?? id)
          );
        const children = isLeaf ? [] : build(val as Record<string, FormNodeData>);
        const active = formSel === id;
        const open = active || (formSel !== "" && (desc[id]?.includes(formSel) ?? false));
        return { id, label, n: leafCount(id), active, open, children, isLeaf };
      });
    return build(taxonomy?.forms || {});
  }, [stats, taxonomy, formSel, locale]);

  // 内容形式树展开/折叠状态(独立 caret 控制);选中节点自动展开祖先路径
  const [openForms, setOpenForms] = useState<Set<string>>(new Set());
  const toggleFormOpen = (id: string) =>
    setOpenForms((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const ensureFormOpen = (id: string) => setOpenForms((s) => new Set(s).add(id));
  useEffect(() => {
    if (!formSel) return;
    setOpenForms((s) => {
      const n = new Set(s);
      for (const [pid, d] of Object.entries(taxonomy?.form_descendants || {}))
        if (d.includes(formSel)) n.add(pid);
      return n;
    });
  }, [formSel, taxonomy]);

  // 题材分组折叠状态(默认展开)
  const [collapsedTopics, setCollapsedTopics] = useState<Set<string>>(new Set());
  const toggleTopicOpen = (g: string) =>
    setCollapsedTopics((s) => { const n = new Set(s); if (n.has(g)) n.delete(g); else n.add(g); return n; });
  // 上传默认分类弹层(挂在工具栏上传按钮旁;它属上传设置,不是筛选,故不进侧边栏)
  const [upCfgOpen, setUpCfgOpen] = useState(false);
  const upCfgRef = useRef<HTMLDivElement>(null);
  const upDefaultsCount = (upForm ? 1 : 0) + upTopics.length + upUsage.length;
  useEffect(() => {
    if (!upCfgOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (upCfgRef.current && !upCfgRef.current.contains(e.target as Node)) setUpCfgOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setUpCfgOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [upCfgOpen]);
  // 工具栏「⋯ 更多」溢出菜单:收纳检索方式 / 刷新 / 批量管理(低频、权力项,不抢主行)
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!moreOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (moreRef.current && !moreRef.current.contains(e.target as Node)) setMoreOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMoreOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [moreOpen]);
  // 侧边栏顶部统一搜索:跨 内容形式/题材/用途 受控词表检索
  const [sideQ, setSideQ] = useState("");
  // 题材/用途标签的中英切换(对齐 UI locale,避免分类语言不一致)
  const enMode = locale.startsWith("en");
  const topicGroupLabel = useCallback(
    (g: string) => enMode ? (taxonomy?.topic_groups_en?.[g] ?? g) : g,
    [enMode, taxonomy],
  );
  const topicLabel = useCallback(
    (t: string) => enMode ? (taxonomy?.topics_en?.[t] ?? t) : t,
    [enMode, taxonomy],
  );
  const usageLabel = useCallback(
    (u: string) => enMode ? (taxonomy?.usages_en?.[u] ?? u) : u,
    [enMode, taxonomy],
  );
  const sideMatches = useMemo(() => {
    const k = sideQ.trim().toLowerCase();
    if (!k) return [];
    const out: { key: string; dim: "form" | "topic" | "usage"; id: string; label: string; dimLabel: string }[] = [];
    const formOpts = (locale.startsWith("en")
      ? (taxonomy?.form_options_en || taxonomy?.form_options || [])
      : (taxonomy?.form_options || [])) as [string, string][];
    for (const [id, path] of formOpts)
      if (id.toLowerCase().includes(k) || path.toLowerCase().includes(k))
        out.push({ key: "f" + id, dim: "form", id, label: path, dimLabel: copy.formFilter });
    for (const t of taxonomy?.topics || [])
      if (topicLabel(t).toLowerCase().includes(k))
        out.push({ key: "t" + t, dim: "topic", id: t, label: topicLabel(t), dimLabel: copy.topicFilter });
    for (const u of taxonomy?.usages || [])
      if (usageLabel(u).toLowerCase().includes(k))
        out.push({ key: "u" + u, dim: "usage", id: u, label: usageLabel(u), dimLabel: copy.usageFilter });
    return out.slice(0, 40);
  }, [sideQ, taxonomy, locale, copy.formFilter, copy.topicFilter, copy.usageFilter, topicLabel, usageLabel]);

  // 侧边栏分区折叠状态:默认展开「内容形式」,其余收起
  const [openSec, setOpenSec] = useState<Record<string, boolean>>({
    form: true, kind: false, topic: false, usage: false, albums: true, tags: false,
  });
  // 标签分业务/系统:系统标签来自流水线内部(sp/bilibili/job:/type:/parent:/role:/lang:/asr/demux/upload),默认收起
  // album: 在「我的合集」专区展示,不进普通业务标签列表。
  const SYS_TAG_RE = /^(sp|bilibili|job:|type:|parent:|role:|lang:|asr|demux|upload)/;
  const bizTags = useMemo(
    () => (tags || []).filter((t) => !SYS_TAG_RE.test(t.tag) && !t.tag.startsWith("album:")),
    [tags],
  );
  const sysTags = useMemo(() => (tags || []).filter((t) => SYS_TAG_RE.test(t.tag)), [tags]);

  // 标签人读文案以服务端 /tags?ui=1 为准(hub-api.ts 的 formatHubTag 仅作兜底)。
  const tagLabelMap = useMemo(() => {
    const map: Record<string, string> = {};
    for (const t of tags) {
      if (t.label) map[t.tag] = t.label;
    }
    return map;
  }, [tags]);

  // 已应用的筛选 → 可单独移除的气泡(chips)。让用户随时看到"选了哪些、能清哪个",
  // 不必滚到侧边栏底部用「清除筛选」一键全清。
  const formPathMap = useMemo(() => {
    const m: Record<string, string> = {};
    for (const [id, path] of ((locale.startsWith("en")
      ? (taxonomy?.form_options_en || taxonomy?.form_options || [])
      : (taxonomy?.form_options || [])) as [string, string][])) m[id] = path;
    return m;
  }, [taxonomy, locale]);
  const activeChips = useMemo(() => {
    const out: { key: string; dim: string; label: string; title: string; clear: () => void }[] = [];
    const bump = () => setPage(1);
    if (q.trim())
      out.push({
        key: "q", dim: copy.filterSearch, label: q.trim(), title: q.trim(),
        clear: () => { setQ(""); bump(); },
      });
    if (kind)
      out.push({
        key: "kind", dim: copy.mediaType, label: kindLabel(kind), title: kindLabel(kind),
        clear: () => { setKind(""); bump(); },
      });
    if (formSel)
      out.push({
        key: "form", dim: copy.formFilter, label: formPathMap[formSel] ?? formSel,
        title: formPathMap[formSel] ?? formSel,
        clear: () => { setFormSel(""); bump(); },
      });
    if (topicSel)
      out.push({
        key: "topic", dim: copy.topicFilter, label: topicLabel(topicSel), title: topicLabel(topicSel),
        clear: () => { setTopicSel(""); bump(); },
      });
    if (usageSel)
      out.push({
        key: "usage", dim: copy.usageFilter, label: usageLabel(usageSel), title: usageLabel(usageSel),
        clear: () => { setUsageSel(""); bump(); },
      });
    if (tag)
      out.push({
        key: "tag", dim: copy.tags, label: tagLabelMap[tag] ?? formatHubTag(tag, locale),
        title: tag,
        clear: () => { setTag(""); bump(); },
      });
    if (sort)
      out.push({
        key: "sort", dim: copy.filterSort,
        label: sort === "newest" ? copy.sortNewest : sort === "name" ? copy.sortName : sort === "size" ? copy.sortSize : copy.sortDefault,
        title: copy.filterSort,
        clear: () => { setSort(""); bump(); },
      });
    return out;
    // formatHubTag 为模块级纯函数(外层作用域,不随渲染变化),按 exhaustive-deps 规则不进依赖
  }, [q, kind, formSel, topicSel, usageSel, tag, sort, locale, copy,
      kindLabel, formPathMap, topicLabel, usageLabel, tagLabelMap]);

  // 上传最佳实践:「理解中」徽章数据(auto 链后台跑完,下次刷新清零)
  const loadPending = useCallback(async () => {
    try {
      const r = await hubFetch<Record<string, unknown>>("/pending");
      setPendingIds(new Set(Object.keys(r || {})));
    } catch {
      setPendingIds(new Set());
    }
  }, []);

  useEffect(() => {
    void loadPending();
  }, [loadPending]);

  // 加载内容形式分类树(单一真相源来自后端 /api/taxonomy)
  useEffect(() => {
    hubFetch<HubTaxonomy>("/taxonomy").then(setTaxonomy).catch(() => {});
  }, []);

  // 上传上限从后端下发(与 VITUAL_UPLOAD_MAX_MB 一致);默认兜底 2GB
  useEffect(() => {
    hubFetch<{ upload_max_mb?: number }>("/health")
      .then((h) => {
        if (h?.upload_max_mb) setMaxUp(h.upload_max_mb * 1024 * 1024);
      })
      .catch(() => {});
  }, []);

  // 有素材在「理解中」时每 8s 轮询,auto 链跑完徽章自动消失
  useEffect(() => {
    if (pendingIds.size === 0) return;
    const t = setTimeout(() => {
      void loadPending();
    }, 8000);
    return () => clearTimeout(t);
  }, [pendingIds, loadPending]);

  // 移动端筛选抽屉:应用筛选后自动收起,让结果区可见(桌面侧栏常驻,不被频繁收起)
  useEffect(() => {
    if (typeof window !== "undefined" && window.matchMedia("(max-width: 720px)").matches) {
      setFiltersOpen(false);
    }
  }, [kind, tag, formSel, topicSel, usageSel]);

  async function onUpload(files: FileList | null) {
    if (!files?.length) return;
    let added = 0;
    let dup = 0;
    let failed = 0;
    const failNote: string[] = [];
    for (const f of Array.from(files)) {
      if (f.size > maxUp) {
        failed++;
        failNote.push(copy.tooLarge);
        continue;
      }
      try {
        const r = await hubUploadFile(
          f,
          (pct) => setUpNote(fillCopy(copy.uploading, { name: f.name, pct: String(pct) })),
          { form: upForm, topics: upTopics.join(","), usage: upUsage.join(",") },
        );
        if (r.status === "duplicate") dup++;
        else if (r.id) added++;
        else {
          failed++;
          failNote.push(f.name);
        }
      } catch {
        failed++;
        failNote.push(f.name);
      }
    }
    setUpNote("");
    let summary = fillCopy(copy.uploadSummary, {
      a: String(added),
      d: String(dup),
      f: String(failed),
    });
    if (failNote.length) summary += ` — ${failNote.slice(0, 3).join(" · ")}`;
    setToast(summary);
    setPage(1);
    await Promise.all([load(), loadPending()]);
    if (fileRef.current) fileRef.current.value = "";
  }

  async function saveDetail() {
    if (!detail) return;
    await hubFetch("/tag", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: detail.id, tags: editTags }),
    });
    await hubFetch("/describe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: detail.id, description: editDesc }),
    });
    setToast(copy.saved);
    setDetail(null);
    await load();
  }

  const removeMaterial = useCallback(async (m: HubMaterial) => {
    if (!window.confirm(`${copy.del}「${displayName(m)}」？`)) return;
    await hubFetch("/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: m.id }),
    });
    setToast(copy.deleted);
    if (detailRef.current?.id === m.id) setDetail(null);
    // 乐观移除:直接从已加载列表摘掉该卡片并递减总数,刷新分面计数;
    // 不调用 load()(否则当前累积的多页会被整页替换、丢失滚动位置)。
    setRows((prev) => prev.filter((r) => r.id !== m.id));
    setTotal((t) => Math.max(0, t - 1));
    void loadMeta();
  }, [copy, loadMeta, setToast, setDetail]);

  async function removeDetail() {
    if (!detail) return;
    await removeMaterial(detail);
  }

  const openDetail = useCallback((m: HubMaterial) => {
    setDetail(m);
    setEditTags(m.tags || "");
    setEditDesc(m.description || "");
    // 派生记录随素材切换重取,避免看到上一条的残留
    setRunRec(null);
    setUnderstandRec(null);
    void hubRunRecord(m.id).then(setRunRec);
    void hubUnderstand(m.id).then(setUnderstandRec);
    // 步骤 5/7:该素材所属 job 的渠道就绪度(英文命名空间 blocking,界面按语言呈现)
    const jobTag = (m.tags || "").split(",").find((t) => t.startsWith("job:"));
    setReadiness(null);
    setExportLang("");
    setExportResult(null);
    if (jobTag) {
      void hubReadiness(jobTag.slice(4)).then(setReadiness);
    }
  }, [setDetail, setEditTags, setEditDesc, setRunRec, setUnderstandRec, setReadiness, setExportLang, setExportResult]);

  // 多选 / 批量操作
  const toggleSelect = useCallback((m: HubMaterial) => {
    setSelected((prev) => {
      const n = new Set(prev);
      if (n.has(m.id)) n.delete(m.id);
      else n.add(m.id);
      return n;
    });
  }, []);
  const clearSelect = useCallback(() => setSelected(new Set()), []);
  const selectAllVisible = useCallback(() => {
    setSelected(new Set(rows.map((r) => r.id)));
  }, [rows]);

  // 归组结果始终算好(合集候选也用);Tab 只决定展示哪一块。
  const groupedEntries = useMemo(() => buildGridEntries(rows), [rows]);
  const collGroups = useMemo(
    () => groupedEntries.filter((e): e is Extract<GridEntry, { type: "group" }> => e.type === "group"),
    [groupedEntries],
  );
  const collLoose = useMemo(
    () => groupedEntries.filter((e): e is Extract<GridEntry, { type: "card" }> => e.type === "card"),
    [groupedEntries],
  );
  const flatEntries = useMemo(
    () => rows.map((m) => ({ type: "card" as const, m })),
    [rows],
  );
  const cardCtx = useMemo<HubCardCtx>(() => ({
    copy, kindLabel, tagLabelMap, locale, pendingIds, selected, selectMode,
    openDetail, removeMaterial, toggleFav, toggleSelect,
  }), [copy, kindLabel, tagLabelMap, locale, pendingIds, selected, selectMode,
       openDetail, removeMaterial, toggleFav, toggleSelect]);
  const filterJob = useCallback((jobTag: string) => {
    setKind("");
    setTag(jobTag);
    setPage(1);
  }, []);
  const selectGroup = useCallback((ids: string[]) => {
    setSelected((prev) => {
      const next = new Set(prev);
      const all = ids.every((id) => next.has(id));
      for (const id of ids) {
        if (all) next.delete(id);
        else next.add(id);
      }
      return next;
    });
  }, []);
  const renderCard = (m: HubMaterial) => (
    <HubCard
      key={m.id}
      m={m}
      copy={copy}
      kindLabel={kindLabel}
      tagLabelMap={tagLabelMap}
      locale={locale}
      pendingIds={pendingIds}
      selected={selected.has(m.id)}
      onOpen={selectMode ? toggleSelect : openDetail}
      onDelete={removeMaterial}
      onToggleFavorite={toggleFav}
      onToggleSelect={toggleSelect}
    />
  );
  // 「加入合集」候选:流水线 job 视频合集 + 用户 album 合集(此前漏了 job,页面上那俩合集选不了)
  const collPickOptions = useMemo(() => {
    type Opt = { tag: string; count: number; title: string; kind: "job" | "album" };
    const map = new Map<string, Opt>();
    const put = (tag: string, count: number, title?: string) => {
      if (tag.startsWith("job:")) {
        const id = tag.slice(4);
        const prev = map.get(tag);
        map.set(tag, {
          tag,
          kind: "job",
          title: title || id,
          count: Math.max(prev?.count || 0, count),
        });
      } else if (tag.startsWith("album:")) {
        const prev = map.get(tag);
        map.set(tag, {
          tag,
          kind: "album",
          title: title || albumTitleOf(tag),
          count: Math.max(prev?.count || 0, count),
        });
      }
    };
    // album 标题用纯名(albumTitleOf);tags.label 带「合集·」前缀,空合集也会难看
    for (const tg of tags) {
      if (tg.tag.startsWith("album:")) put(tg.tag, tg.count || 0);
      else put(tg.tag, tg.count || 0, tg.label);
    }
    for (const r of rows) {
      for (const t of csv(r.tags)) {
        if (t.startsWith("job:") || t.startsWith("album:")) put(t, 0);
      }
    }
    // 当前网格里已折叠的合集卡(标题更可读,如 BV 号)
    for (const e of collGroups) {
      if (!e.group.jobTag) continue;
      put(e.group.jobTag, e.group.members.length, e.group.title);
    }
    const all = Array.from(map.values());
    const jobs = all.filter((o) => o.kind === "job")
      .sort((a, b) => b.count - a.count || a.title.localeCompare(b.title, locale));
    const albums = all.filter((o) => o.kind === "album")
      .sort((a, b) => a.title.localeCompare(b.title, locale));
    return [...jobs, ...albums];
  }, [tags, rows, collGroups, locale]);
  // 侧栏「我的合集」仍只展示用户 album
  const albumOptions = useMemo(
    () => collPickOptions.filter((o) => o.kind === "album"),
    [collPickOptions],
  );

  const beginBulk = useCallback(() => {
    if (bulkBusyRef.current) return false;
    bulkBusyRef.current = true;
    setBulkBusy(true);
    return true;
  }, []);
  const endBulk = useCallback(() => {
    bulkBusyRef.current = false;
    setBulkBusy(false);
  }, []);

  const bulkFavorite = useCallback(async () => {
    const ids = Array.from(selected);
    if (!ids.length || !beginBulk()) return;
    try {
      await hubBulk("favorite", ids);
      setRows((prev) => prev.map((r) => (selected.has(r.id) ? { ...r, favorite: true } : r)));
      setToast(copy.favMany);
      clearSelect();
      setSelectMode(false);
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    } finally {
      endBulk();
    }
  }, [selected, copy, clearSelect, beginBulk, endBulk]);

  // 批量取消收藏:仅看收藏模式下,取消成功的卡片应立即移出列表。
  const bulkUnfavorite = useCallback(async () => {
    const ids = Array.from(selected);
    if (!ids.length || !beginBulk()) return;
    try {
      const r = await hubBulk("unfavorite", ids);
      const done = r.done ?? ids.length;
      if (!done) throw new Error(r.error || r.errors?.[0]?.error || copy.error);
      const failedIds = new Set((r.errors || []).map((x) => x.id));
      const okIds = new Set(ids.filter((id) => !failedIds.has(id)));
      setRows((prev) => prev
        .map((r) => (okIds.has(r.id) ? { ...r, favorite: false } : r))
        .filter((r) => !(favOnly && okIds.has(r.id))));
      setToast(copy.unfavorite);
      clearSelect();
      setSelectMode(false);
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    } finally {
      endBulk();
    }
  }, [selected, favOnly, copy, clearSelect, beginBulk, endBulk]);

  const bulkDelete = useCallback(async () => {
    const ids = Array.from(selected);
    if (!ids.length || bulkBusyRef.current) return;
    if (!window.confirm(`${copy.delMany}？(${ids.length})`)) return;
    if (!beginBulk()) return;
    try {
      const r = await hubBulk("remove", ids);
      // 只移除服务端确认删除的条目,成功数用后端 done(权威)。
      // 不能用 r.done || ids.length —— done=0 会被当成全删,部分失败时行数与总数也对不上。
      const done = r.done ?? 0;
      if (!done) throw new Error(r.error || r.errors?.[0]?.error || copy.error);
      const failedIds = new Set((r.errors || []).map((x) => x.id));
      const removedIds = ids.filter((id) => !failedIds.has(id));
      setToast(fillCopy(copy.deletedMany, { n: String(done) }));
      clearSelect();
      setSelectMode(false);
      setRows((prev) => prev.filter((x) => !removedIds.includes(x.id)));
      setTotal((t) => Math.max(0, t - done));
      void loadMeta();
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    } finally {
      endBulk();
    }
  }, [selected, copy, clearSelect, beginBulk, endBulk, loadMeta]);

  const bulkAddToAlbum = useCallback(async (collTag: string) => {
    const ids = Array.from(selected);
    if (!ids.length || !collTag || !beginBulk()) return;
    try {
      // join_coll:album 合并;job 改写归属(否则护栏删不了旧 job:,合集卡也进不去)
      const r = await hubBulk("join_coll", ids, { tags: collTag });
      if ((r.errors || []).length && !r.done) {
        throw new Error(r.errors?.[0]?.error || copy.error);
      }
      setRows((prev) => prev.map((row) => {
        if (!selected.has(row.id)) return row;
        let cur = csv(row.tags);
        if (collTag.startsWith("job:")) cur = cur.filter((t) => !t.startsWith("job:"));
        if (!cur.includes(collTag)) cur = [...cur, collTag];
        return { ...row, tags: cur.join(",") };
      }));
      setToast(copy.albumAdded);
      setAlbumOpen(false);
      setLeaveOpen(false);
      setAlbumName("");
      clearSelect();
      setSelectMode(false);
      void loadMeta();
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    } finally {
      endBulk();
    }
  }, [selected, copy, clearSelect, beginBulk, endBulk, loadMeta]);

  const bulkMoveForm = useCallback(async (formId: string) => {
    const ids = Array.from(selected);
    if (!ids.length || !beginBulk()) return;
    try {
      const r = await hubBulk("set_form", ids, { form: formId });
      const done = r.done ?? 0;
      if (!done) throw new Error(r.error || r.errors?.[0]?.error || copy.error);
      const failedIds = new Set((r.errors || []).map((x) => x.id));
      setRows((prev) => prev.map((x) =>
        (selected.has(x.id) && !failedIds.has(x.id) ? { ...x, form: formId } : x)));
      setToast(fillCopy(copy.movedMany, { n: String(done) }));
      setMoveOpen(false);
      setLeaveOpen(false);
      clearSelect();
      setSelectMode(false);
      void loadMeta();
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    } finally {
      endBulk();
    }
  }, [selected, copy, clearSelect, beginBulk, endBulk, loadMeta]);

  // 当前筛选若是合集 tag,置顶到「移出合集」候选
  const activeCollTag = (tag.startsWith("album:") || tag.startsWith("job:")) ? tag : "";
  // 选中的素材也可能来自展开的合集卡(此时 tag 为空),按成员实际标签给出候选
  const leaveCollOptions = useMemo(() => {
    const inSelection = new Set<string>();
    for (const r of rows) {
      if (!selected.has(r.id)) continue;
      for (const t of csv(r.tags)) {
        if (t.startsWith("album:") || t.startsWith("job:")) inSelection.add(t);
      }
    }
    if (activeCollTag) inSelection.add(activeCollTag);
    const opts = collPickOptions.filter((o) => inSelection.has(o.tag));
    const known = new Set(opts.map((o) => o.tag));
    for (const t of inSelection) {
      if (known.has(t)) continue;
      const isJob = t.startsWith("job:");
      opts.push({
        tag: t,
        kind: isJob ? "job" : "album",
        title: isJob ? t.slice(4) : albumTitleOf(t),
        count: 0,
      });
    }
    return activeCollTag
      ? [...opts.filter((o) => o.tag === activeCollTag), ...opts.filter((o) => o.tag !== activeCollTag)]
      : opts;
  }, [rows, selected, activeCollTag, collPickOptions]);

  const bulkLeaveColl = useCallback(async (collTag: string) => {
    const ids = Array.from(selected);
    if (!ids.length || !collTag || !beginBulk()) return;
    const title = leaveCollOptions.find((o) => o.tag === collTag)?.title || collTag;
    if (!window.confirm(fillCopy(copy.leaveCollConfirm, { n: String(ids.length), name: title }))) {
      endBulk();
      return;
    }
    try {
      const r = await hubBulk("leave_coll", ids, { tags: collTag });
      const done = r.done ?? 0;
      if (!done) throw new Error(r.error || r.errors?.[0]?.error || copy.error);
      setToast(copy.leftColl);
      const failedIds = new Set((r.errors || []).map((x) => x.id));
      const leaveIds = new Set(ids.filter((id) => !failedIds.has(id)));
      setRows((prev) => prev
        .map((row) => {
          if (!leaveIds.has(row.id)) return row;
          return { ...row, tags: csv(row.tags).filter((t) => t !== collTag).join(",") };
        })
        // 当前正在看这个合集时,移出的条目立刻从列表消失
        .filter((row) => !(leaveIds.has(row.id) && tag === collTag)));
      clearSelect();
      setSelectMode(false);
      setLeaveOpen(false);
      setTotal((n) => Math.max(0, n - done));
      void loadMeta();
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    } finally {
      endBulk();
    }
  }, [selected, leaveCollOptions, copy, tag, clearSelect, beginBulk, endBulk, loadMeta]);

  // 批量导出语种取所选素材自身的 lang: 标签;详情 readiness 只覆盖单个 job
  const bulkExportLangs = useMemo(() => {
    const set = new Set<string>();
    for (const r of rows) {
      if (!selected.has(r.id)) continue;
      for (const t of csv(r.tags)) {
        if (t.startsWith("lang:")) set.add(t.slice(5));
      }
    }
    return Array.from(set).sort();
  }, [rows, selected]);

  const bulkDeliver = useCallback(async (confirm: boolean) => {
    const ids = Array.from(selected);
    if (!ids.length || bulkExportBusy) return;
    setBulkExportBusy(true);
    try {
      const r = await hubDeliver({ ids, lang: bulkExportLang || null, confirm });
      if (r.error) {
        setBulkExportResult(null);
        throw new Error(r.error);
      }
      setBulkExportResult(r);
      if (confirm) setToast(`${copy.exportResult}: ${(r.written || []).length}`);
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    } finally {
      setBulkExportBusy(false);
    }
  }, [selected, bulkExportLang, bulkExportBusy, copy]);

  const renameColl = useCallback(async (collTag: string, title: string) => {
    if (!collTag.startsWith("album:")) return;
    const next = window.prompt(copy.albumRenamePh, title || albumTitleOf(collTag));
    if (next == null) return;
    const newTag = albumTagOf(next);
    if (newTag === collTag) return;
    try {
      const r = await hubColl("rename", { from: collTag, to: newTag });
      if (!r.ok) throw new Error(r.error || copy.error);
      setToast(copy.albumRenamed);
      if (tag === collTag) setTag(newTag);
      setRows((prev) => prev.map((row) => {
        const cur = csv(row.tags);
        if (!cur.includes(collTag)) return row;
        return { ...row, tags: cur.map((t) => (t === collTag ? newTag : t)).join(",") };
      }));
      void loadMeta();
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    }
  }, [copy, tag, loadMeta]);

  const dissolveColl = useCallback(async (collTag: string, title: string, kind: "album" | "job") => {
    const msg = kind === "album"
      ? fillCopy(copy.albumDeleteConfirm, { name: title })
      : fillCopy(copy.jobDissolveConfirm, { name: title });
    if (!window.confirm(msg)) return;
    try {
      const r = await hubColl("delete", { tag: collTag });
      if (!r.ok && (r.errors || []).length) throw new Error(r.errors?.[0]?.error || copy.error);
      setToast(copy.albumDeleted);
      if (tag === collTag) { setTag(""); setPage(1); }
      setRows((prev) => prev.map((row) => {
        const cur = csv(row.tags);
        if (!cur.includes(collTag)) return row;
        return { ...row, tags: cur.filter((t) => t !== collTag).join(",") };
      }));
      void load();
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    }
  }, [copy, tag, load]);

  const createEmptyAlbum = useCallback(async () => {
    const name = window.prompt(copy.albumNamePh, "");
    if (name == null || !name.trim()) return;
    try {
      const r = await hubColl("create", { name: name.trim() });
      if (!r.ok || !r.tag) throw new Error(r.error || copy.error);
      // 空合集尚无成员时,先用 toast 提示;有选中则直接加入
      if (selected.size > 0) {
        await bulkAddToAlbum(r.tag);
      } else {
        setToast(`${copy.albumNew}: ${r.title || name.trim()}`);
        setTag(r.tag);
        setPage(1);
        void loadMeta();
      }
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    }
  }, [copy, selected.size, bulkAddToAlbum, loadMeta]);
  const onDropFiles = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDragOver(false);
      const files = e.dataTransfer?.files;
      if (files && files.length) void onUpload(files);
    },
    [onUpload],
  );

  return (
    <div className="hub-page">
      <div className="hub-layout">
        <button
          type="button"
          className="hub-aside-toggle"
          aria-expanded={filtersOpen}
          aria-controls="hub-aside-body"
          title={filtersOpen ? copy.asideCollapse : copy.asideExpand}
          aria-label={filtersOpen ? copy.asideCollapse : copy.asideExpand}
          onClick={() => setFiltersOpen((v) => !v)}
        >
          <span className="hub-aside-toggle-ico" aria-hidden="true">{filtersOpen ? "‹" : "›"}</span>
        </button>
        <aside id="hub-aside-body" className={`hub-aside${filtersOpen ? " open" : ""}`}>
          <div className="hub-aside-top">
            <span className="hub-aside-title">{copy.asideFilter}</span>
          </div>
          {/* 侧边栏顶部统一搜索:跨 内容形式/题材/用途 三个受控词表检索,点结果即套用筛选。
              「上传默认分类」已移至工具栏上传按钮旁的 ⚙(它是上传设置,不是筛选,不应混在筛选区) */}
          <div className="hub-sidesearch">
            <input
              className="hub-picker-search"
              value={sideQ}
              onChange={(e) => setSideQ(e.target.value)}
              placeholder={copy.formPickSearch}
              aria-label={copy.formPickSearch}
            />
            {sideQ.trim() && (
              <button
                type="button"
                className="hub-sidesearch-clear"
                onClick={() => setSideQ("")}
                aria-label={copy.clear}
                title={copy.clear}
              >
                ✕
              </button>
            )}
          </div>
          {sideQ.trim() ? (
            <div className="hub-sidesearch-res">
              {sideMatches.length ? sideMatches.map((m) => (
                <button
                  key={m.key}
                  type="button"
                  className="hub-picker-opt"
                  onClick={() => {
                    if (m.dim === "form") setFormSel(m.id);
                    else if (m.dim === "topic") setTopicSel(m.id);
                    else setUsageSel(m.id);
                    setPage(1);
                    setSideQ("");
                  }}
                >
                  <span className={`hub-sidesearch-dim dim-${m.dim}`}>{m.dimLabel}</span>
                  {m.label}
                </button>
              )) : <div className="hub-picker-empty">{copy.formPickEmpty}</div>}
            </div>
          ) : (
            <div className="hub-aside-secs">

          {/* 内容形式:多层级树(主轴) */}
          <div className={`hub-sec${openSec.form ? " open" : ""}`}>
            <SecHead open={openSec.form} onToggle={() => setOpenSec((s) => ({ ...s, form: !s.form }))}>{copy.formFilter}</SecHead>
            <div className="hub-sec-body">
              <div className="hub-formtree" id="formFacets">
                <button
                  type="button"
                  className={`hub-facet${formSel === "" ? " on" : ""}`}
                  onClick={() => { setFormSel(""); setPage(1); }}
                >
                  <span className="lbl">{copy.formAll}</span>
                  <span className="n">{stats?.total ?? 0}</span>
                </button>
                {formGroups.map((g) => (
                  <FormNodeView key={g.id} node={g} onSelect={(id) => { setFormSel(formSel === id ? "" : id); setPage(1); }} openIds={openForms} onToggleOpen={toggleFormOpen} ensureOpen={ensureFormOpen} />
                ))}
              </div>
            </div>
          </div>

          {/* 媒体类型(辅助 facet) */}
          <div className={`hub-sec${openSec.kind ? " open" : ""}`}>
            <SecHead open={openSec.kind} onToggle={() => setOpenSec((s) => ({ ...s, kind: !s.kind }))}>{copy.mediaType}</SecHead>
            <div className="hub-sec-body">
              <div className="hub-facets">
                {facets.filter((f) => f.val !== "").map((f) => (
                  <button
                    key={f.val || "all"}
                    type="button"
                    className={`hub-facet${kind === f.val ? " on" : ""}${f.n === 0 ? " zero" : ""}`}
                    onClick={() => chooseKind(f.val)}
                  >
                    <span className="lbl">
                      {f.label}
                    </span>
                    <span className="n">{f.n}</span>
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* 题材(多选标签,按组分层) */}
          <div className={`hub-sec${openSec.topic ? " open" : ""}`}>
            <SecHead open={openSec.topic} onToggle={() => setOpenSec((s) => ({ ...s, topic: !s.topic }))}>{copy.topicFilter}</SecHead>
            <div className="hub-sec-body">
              {(Object.keys(taxonomy?.topic_taxonomy || {}).length
                ? Object.entries(taxonomy!.topic_taxonomy)
                : [["", taxonomy?.topics || []]] as [string, string[]][]
              ).map(([g, leaves]) => {
                const grpCount = leaves.reduce((a, t) => a + (stats?.topics?.[t] ?? 0), 0);
                const open = !collapsedTopics.has(g);
                return (
                  <div key={g || "all"} className="hub-topicgroup">
                    {g && (
                      <button
                        type="button"
                        className={`hub-tag hub-topicgroup-h${topicSel === g ? " on" : ""}`}
                        onClick={() => { setTopicSel(topicSel === g ? "" : g); setPage(1); }}
                      >
                        <span className="caret" aria-hidden="true" onClick={(e) => { e.stopPropagation(); toggleTopicOpen(g); }}>{open ? "▾" : "▸"}</span>
                        {topicGroupLabel(g)}
                        <span className="n">{grpCount}</span>
                      </button>
                    )}
                    {open && (
                    <div className="hub-tags">
                      {leaves.map((t) => (
                        <button
                          key={t}
                          type="button"
                          className={`hub-tag${topicSel === t ? " on" : ""}`}
                          onClick={() => { setTopicSel(topicSel === t ? "" : t); setPage(1); }}
                        >
                          {topicLabel(t)}
                          <span className="n">{stats?.topics?.[t] ?? 0}</span>
                        </button>
                      ))}
                    </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {/* 用途(多选标签) */}
          <div className={`hub-sec${openSec.usage ? " open" : ""}`}>
            <SecHead open={openSec.usage} onToggle={() => setOpenSec((s) => ({ ...s, usage: !s.usage }))}>{copy.usageFilter}</SecHead>
            <div className="hub-sec-body">
              <div className="hub-tags">
                {(taxonomy?.usages || []).map((u) => (
                  <button
                    key={u}
                    type="button"
                    className={`hub-tag${usageSel === u ? " on" : ""}`}
                    onClick={() => { setUsageSel(usageSel === u ? "" : u); setPage(1); }}
                  >
                    {usageLabel(u)}
                    <span className="n">{stats?.usages?.[u] ?? 0}</span>
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* 我的合集:增删改查(点选=查;新建/重命名/删除=增改删) */}
          <div className={`hub-sec${openSec.albums !== false ? " open" : ""}`}>
            <SecHead
              open={openSec.albums !== false}
              onToggle={() => setOpenSec((s) => ({ ...s, albums: s.albums === false }))}
            >
              {copy.albumMine}
            </SecHead>
            <div className="hub-sec-body">
              {albumOptions.length ? (
                <div className="hub-album-list">
                  {albumOptions.map((a) => (
                    <div key={a.tag} className={`hub-album-row${tag === a.tag ? " on" : ""}`}>
                      <button
                        type="button"
                        className="hub-album-main"
                        title={a.tag}
                        onClick={() => { setTag(tag === a.tag ? "" : a.tag); setPage(1); }}
                      >
                        <span className="hub-album-title">{a.title}</span>
                        <span className="n">{a.count}</span>
                      </button>
                      <button
                        type="button"
                        className="hub-album-act"
                        title={copy.albumRename}
                        aria-label={copy.albumRename}
                        onClick={() => void renameColl(a.tag, a.title)}
                      >
                        ✎
                      </button>
                      <button
                        type="button"
                        className="hub-album-act danger"
                        title={copy.albumDelete}
                        aria-label={copy.albumDelete}
                        onClick={() => void dissolveColl(a.tag, a.title, "album")}
                      >
                        ×
                      </button>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="hub-sec-hint">{copy.albumEmpty}</p>
              )}
              <button type="button" className="hub-album-new" onClick={() => void createEmptyAlbum()}>
                + {copy.albumNew}
              </button>
            </div>
          </div>

          {/* 标签:业务标签常显,系统/流水线标签收起 */}
          <div className={`hub-sec${openSec.tags ? " open" : ""}`}>
            <SecHead open={openSec.tags} onToggle={() => setOpenSec((s) => ({ ...s, tags: !s.tags }))}>{copy.tags}</SecHead>
            <div className="hub-sec-body">
              <div className="hub-tags">
                {bizTags.map((tg) => (
                  <button
                    key={tg.tag}
                    type="button"
                    className={`hub-tag${tag === tg.tag ? " on" : ""}`}
                    title={`${tg.hint || tg.tag} (${tg.count})`}
                    onClick={() => {
                      setTag(tag === tg.tag ? "" : tg.tag);
                      setPage(1);
                    }}
                  >
                    {tg.label || formatHubTag(tg.tag, locale)}
                    <span className="n">{tg.count}</span>
                  </button>
                ))}
              </div>
              {sysTags.length > 0 && (
                <details className="hub-sysdetails">
                  <summary>{copy.sysTags} ({sysTags.length})</summary>
                  <div className="hub-tags">
                    {sysTags.map((tg) => (
                      <button
                        key={tg.tag}
                        type="button"
                        className={`hub-tag${tag === tg.tag ? " on" : ""}`}
                        title={`${tg.hint || tg.tag} (${tg.count})`}
                        onClick={() => {
                          setTag(tag === tg.tag ? "" : tg.tag);
                          setPage(1);
                        }}
                      >
                        {tg.label || formatHubTag(tg.tag, locale)}
                        <span className="n">{tg.count}</span>
                      </button>
                    ))}
                  </div>
                </details>
              )}
            </div>
          </div>

          {(kind || tag || q || formSel || topicSel || usageSel) && (
            <button
              type="button"
              className="hub-clear"
              onClick={() => {
                setKind("");
                setTag("");
                setQ("");
                setSort("");
                setFormSel("");
                setTopicSel("");
                setUsageSel("");
                setPage(1);
              }}
            >
              {copy.clear}
            </button>
          )}
            </div>
          )}
        </aside>

        <div className="hub-main">
          <header className="home-intro try-intro hub-hero">
            <div className="hub-hero-text">
              <p className="kicker">{copy.kicker}</p>
              <h1 className="hero-line">{copy.headline}</h1>
              <p className="lede">{copy.lede}</p>
            </div>
            <div className="hub-hero-cta">
              <Link className="watch-btn hub-agent-cta" href={`/${locale}/hub/agent`}>
                {copy.openAgent}
              </Link>
              <Link className="watch-btn hub-cta-secondary" href={`/${locale}`}>
                {copy.openCrop}
              </Link>
            </div>
          </header>

          <div className="hub-subnav">
          {/* B 站风格:顶部醒目的媒体类型分段标签栏(带图标 + 计数),直接筛选 kind —— 一眼区分视频/图片/音频 */}
          <div className="hub-typetabs" role="tablist" aria-label={copy.mediaType}>
            {facets.map((f) => (
              <button
                key={f.val || "all"}
                type="button"
                role="tab"
                aria-selected={kind === f.val}
                className={`hub-typetab${kind === f.val ? " on" : ""}${f.n === 0 ? " zero" : ""}`}
                onClick={() => chooseKind(f.val)}
              >
                <span className="ki" aria-hidden="true">{KIND_ICON[f.val] || "📦"}</span>
                <span className="kl">{f.label}</span>
                <span className="kn">{f.n}</span>
              </button>
            ))}
          </div>

          {/* 子分面(格式/角色/衍生):DAM 最佳实践把它们作为类型内的筛选项,而非独立类型。
              视频→静音;图片→动图/相机原片;文档→字幕。计数来自后端 /api/stats.subfacets。 */}
          {SUBFACETS[kind || ""] && (
            <div className="hub-subfacets" style={{ display: "flex", gap: 6, marginTop: 8, flexWrap: "wrap" }}>
              <button
                type="button"
                className={`hub-typetab${tag === "" ? " on" : ""}`}
                onClick={() => { setTag(""); setPage(1); }}
              >
                <span className="ki" aria-hidden="true">{KIND_ICON[kind] || "📦"}</span>
                <span className="kl">{kindLabel(kind)} · {copy.all}</span>
              </button>
              {SUBFACETS[kind || ""]
                .filter((sf) => {
                  // 图片/视频子分面本身就少,全显;文档冷门格式(PDF/文稿/表格/演示)仅有库存或选中时显示。
                  if (kind !== "docs") return true;
                  const n = (subfacets?.docs as Record<string, number> | undefined)?.[sf.tag] ?? 0;
                  return CORE_DOC_SUBFACETS.has(sf.tag) || n > 0 || tag === sf.tag;
                })
                .map((sf) => (
                <button
                  key={sf.tag}
                  type="button"
                  className={`hub-typetab${tag === sf.tag ? " on" : ""}${
                    (((subfacets?.[kind || ""] as Record<string, number> | undefined)?.[sf.tag]) ?? 0) === 0
                      ? " zero"
                      : ""
                  }`}
                  onClick={() => { setTag(tag === sf.tag ? "" : sf.tag); setPage(1); }}
                >
                  <span className="ki" aria-hidden="true">{sf.icon}</span>
                  <span className="kl">{copy[sf.label]}</span>
                  <span className="kn">
                    {((subfacets?.[kind || ""] as Record<string, number> | undefined)?.[sf.tag]) ?? 0}
                  </span>
                </button>
              ))}
            </div>
          )}

          <div className="hub-toolbar">
            <input
              className="hub-search"
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setPage(1);
              }}
              placeholder={copy.searchPh}
              aria-label={copy.searchPh}
            />
            {/* 唯一带框的主筛选入口:点击展开/收起侧栏分面面板,并显示激活分面数 */}
            <button
              type="button"
              className={`hub-filter-btn${activeFilterCount ? " on" : ""}`}
              onClick={() => setFiltersOpen((v) => !v)}
              aria-expanded={filtersOpen}
              aria-controls="hub-aside-body"
              title={copy.asideFilter}
            >
              {copy.asideFilter}
              {activeFilterCount > 0 ? <span className="hub-filter-n">({activeFilterCount})</span> : null}
            </button>
            <div className="hub-toolbar-actions">
              {/* 合集 / 散件 / 全部：互斥 Tab，点合集不再夹带散件 */}
              <div className="hub-coll-tabs" role="tablist" aria-label={copy.collModeHint} title={copy.collModeHint}>
                {([
                  ["coll", copy.collSection],
                  ["loose", copy.collLooseSection],
                  ["flat", copy.collFlat],
                ] as const).map(([id, label]) => (
                  <button
                    key={id}
                    type="button"
                    role="tab"
                    // 偏好在 hydrate 后才从 localStorage 恢复;挂载前与 SSR 一致压在「合集」,
                    // 避免 aria-selected 首帧漂移触发 hydration mismatch。
                    aria-selected={(collHydrated ? collView : "coll") === id}
                    onClick={() => setCollView(id)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {/* 排序:无边框、显当前值;「相关」仅在有关键词时出现 */}
              <div className="hub-sort">
                <span className="hub-sort-label">{copy.filterSort}</span>
                <select
                  value={q.trim() ? sort : sort === "" ? "newest" : sort}
                  onChange={(e) => { setSort(e.target.value); setPage(1); }}
                  aria-label={copy.filterSort}
                >
                  {q.trim() ? <option value="">{copy.sortDefault}</option> : null}
                  <option value="newest">{copy.sortNewest}</option>
                  <option value="name">{copy.sortName}</option>
                  <option value="size">{copy.sortSize}</option>
                </select>
              </div>
              {/* 收藏筛选:图标切换,选中填 ⭐ */}
              <button
                type="button"
                className={`hub-fav${favOnly ? " on" : ""}`}
                onClick={() => { setFavOnly((v) => !v); setPage(1); }}
                aria-pressed={favOnly}
                title={copy.favOnly}
                aria-label={copy.favOnly}
              >
                <span aria-hidden="true">{favOnly ? "★" : "☆"}</span>
              </button>
              {/* 批量管理:主行常显(DAM 最佳实践——选中/加入合集要找得到,不埋进 ⋯) */}
              <button
                type="button"
                className={`hub-batch-toggle${selectMode ? " on" : ""}`}
                onClick={() => {
                  setSelectMode((v) => !v);
                  if (selectMode) {
                    setSelected(new Set());
                    setAlbumOpen(false);
                    setMoveOpen(false);
                    setLeaveOpen(false);
                    setBulkExportOpen(false);
                  }
                }}
                aria-pressed={selectMode}
                title={copy.batch}
                aria-label={copy.batch}
              >
                {selectMode ? copy.exitSelect : copy.batch}
              </button>
              {/* ⋯ 更多:收纳检索方式 / 刷新(低频) */}
              <div className="hub-more" ref={moreRef}>
                <button
                  type="button"
                  className={`hub-more-btn${moreOpen ? " on" : ""}`}
                  onClick={() => setMoreOpen((v) => !v)}
                  aria-expanded={moreOpen}
                  aria-haspopup="true"
                  title={copy.more}
                  aria-label={copy.more}
                >
                  <span aria-hidden="true">⋯</span>
                </button>
                {moreOpen && (
                  <div className="hub-more-pop" role="menu">
                    <div className="hub-more-row">
                      <span className="hub-more-k">{copy.searchMode}</span>
                      <select value={mode} onChange={(e) => { setMode(e.target.value); setPage(1); }} aria-label={copy.searchMode}>
                        <option value="auto">{copy.modeAuto}</option>
                        <option value="lexical">{copy.modeLex}</option>
                        <option value="semantic">{copy.modeSem}</option>
                      </select>
                    </div>
                    <button
                      type="button"
                      className="hub-more-item"
                      onClick={() => { void load(); setMoreOpen(false); }}
                      disabled={busy}
                    >
                      {copy.refresh}
                    </button>
                  </div>
                )}
              </div>
              {upNote ? <span className="hub-upnote">{upNote}</span> : null}
              <div className="hub-upwrap" ref={upCfgRef}>
                <button type="button" className="watch-btn hub-upload" onClick={() => fileRef.current?.click()}>
                  {copy.upload}
                </button>
                <button
                  type="button"
                  className={`hub-upcfg${upCfgOpen ? " on" : ""}${upDefaultsCount ? " set" : ""}`}
                  aria-expanded={upCfgOpen}
                  aria-controls="hub-upcfg-pop"
                  onClick={() => setUpCfgOpen((v) => !v)}
                  title={copy.uploadDefaults}
                  aria-label={copy.uploadDefaults}
                >
                  <span aria-hidden="true">⚙</span>
                  {upDefaultsCount > 0 && <span className="hub-upcfg-n">{upDefaultsCount}</span>}
                </button>
                {upCfgOpen && (
                  <div className="hub-upcfg-pop" id="hub-upcfg-pop">
                    <div className="hub-upcfg-h">{copy.uploadDefaults}</div>
                    <p className="hub-sec-hint">{copy.uploadDefaultsHint}</p>
                    <div className="hub-sublabel">{copy.uploadForm}</div>
                    <FormPicker
                      value={upForm}
                      onChange={setUpForm}
                      options={(locale.startsWith("en")
                        ? (taxonomy?.form_options_en || taxonomy?.form_options || [])
                        : (taxonomy?.form_options || [])) as [string, string][]}
                      placeholder={copy.formUnclassified}
                      searchPh={copy.formPickSearch}
                      emptyPh={copy.formPickEmpty}
                    />
                    <div className="hub-sublabel">{copy.uploadTopics}</div>
                    <div className="hub-tags">
                      {(taxonomy?.topics || []).map((t) => (
                        <button
                          key={t}
                          type="button"
                          className={`hub-tag${upTopics.includes(t) ? " on" : ""}`}
                          onClick={() => setUpTopics((p) => p.includes(t) ? p.filter((x) => x !== t) : [...p, t])}
                        >
                          {topicLabel(t)}
                        </button>
                      ))}
                    </div>
                    <div className="hub-sublabel">{copy.uploadUsage}</div>
                    <div className="hub-tags">
                      {(taxonomy?.usages || []).map((u) => (
                        <button
                          key={u}
                          type="button"
                          className={`hub-tag${upUsage.includes(u) ? " on" : ""}`}
                          onClick={() => setUpUsage((p) => p.includes(u) ? p.filter((x) => x !== u) : [...p, u])}
                        >
                          {usageLabel(u)}
                        </button>
                      ))}
                    </div>
                    {upDefaultsCount > 0 && (
                      <button
                        type="button"
                        className="hub-upcfg-reset"
                        onClick={() => { setUpForm(""); setUpTopics([]); setUpUsage([]); }}
                      >
                        {copy.clear}
                      </button>
                    )}
                  </div>
                )}
              </div>
            </div>
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              onChange={(e) => void onUpload(e.target.files)}
            />
          </div>
          </div>

          {err ? (
            <p className="hub-error">
              {copy.error}: {err}
              <br />
              <span className="muted">{copy.backendHint}</span>
              <br />
              <span className="muted">API: {hubApiBase()}</span>
            </p>
          ) : null}

          {activeChips.length > 0 && (
            <div className="hub-activefilters" role="group" aria-label={copy.clear}>
              {activeChips.map((c) => (
                <button
                  key={c.key}
                  type="button"
                  className="hub-chipfx"
                  title={`${c.dim}: ${c.title}`}
                  onClick={c.clear}
                >
                  <span className="hub-chipfx-dim">{c.dim}</span>
                  <span className="hub-chipfx-label">{c.label}</span>
                  <span className="hub-chipfx-x" aria-hidden="true">✕</span>
                  <span className="sr-only">{copy.clear}</span>
                </button>
              ))}
            </div>
          )}

          {(() => {
            const stageCls = "hub-stage" + (dragOver ? " drag" : "") + (selectMode ? " selmode" : "");
            const stageProps = {
              "data-tip": copy.dropUpload,
              onDragOver: (e: DragEvent<HTMLDivElement>) => {
                e.preventDefault();
                setDragOver(true);
              },
              onDragLeave: (e: DragEvent<HTMLDivElement>) => {
                if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragOver(false);
              },
              onDrop: onDropFiles,
            };
            const gridCls =
              "hub-grid" + (busy && rows.length ? " refreshing" : "");
            // 首屏 / 结构筛选切换:骨架屏。加载期固定卡片骨架(不跟合集 Tab 切换)。
            // collHydrated 前也压骨架,避免偏好「散件」时先闪一帧默认「合集」内容。
            // 骨架网格不加 .busy,避免 opacity:0.5 把 shimmer 冲成「空白页」。
            if ((busy && !rows.length) || skelReset || !collHydrated) {
              return (
                <div className={stageCls} {...stageProps} aria-busy="true" aria-label={copy.loading}>
                  <div className="hub-grid hub-skel-grid">
                    {Array.from({ length: 12 }).map((_, i) => (
                      <div className="hub-skel" key={i}>
                        <div className="hub-skel-thumb" />
                        <div className="hub-skel-body">
                          <div className="hub-skel-line" />
                          <div className="hub-skel-line short" />
                          <div className="hub-skel-line chip" />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              );
            }
            if (!rows.length) {
              return (
                <div className={stageCls} {...stageProps}>
                  <div className="hub-empty" role="status">
                    <p>{copy.empty}</p>
                    <p className="muted">{copy.emptyHint}</p>
                  </div>
                </div>
              );
            }
            const tabEmpty =
              (collView === "coll" && !collGroups.length)
                ? copy.collEmpty
                : (collView === "loose" && !collLoose.length)
                  ? copy.collLooseEmpty
                  : null;
            if (tabEmpty) {
              return (
                <div className={stageCls} {...stageProps}>
                  <div className="hub-empty" role="status">
                    <p>{tabEmpty}</p>
                  </div>
                </div>
              );
            }
            return (
              <div className={stageCls} {...stageProps}>
                <div className={gridCls}>
                  {collView === "coll"
                    ? collGroups.map((e) => (
                        <HubCollection
                          key={`g-${e.group.key}`}
                          group={e.group}
                          defaultOpen={!!e.group.jobTag && tag === e.group.jobTag}
                          cardCtx={cardCtx}
                          onFilterJob={filterJob}
                          onSelectGroup={selectGroup}
                          onRenameColl={(t, title) => void renameColl(t, title)}
                          onDissolveColl={(t, title, kind) => void dissolveColl(t, title, kind)}
                        />
                      ))
                    : collView === "loose"
                      ? collLoose.map((e) => renderCard(e.m))
                      : flatEntries.map((e) => renderCard(e.m))}
                </div>
              </div>
            );
          })()}

          <div className="hub-loadmore">
            <span className="hub-loadcount">
              {fillCopy(copy.pageOf, {
                p: String(Math.min(rows.length, total)),
                n: String(pages),
                t: String(total),
              })}
            </span>
            {page < pages && (
              <button
                type="button"
                className="watch-btn hub-loadbtn"
                disabled={busy}
                onClick={() => setPage((p) => Math.min(pages, p + 1))}
              >
                {copy.loadMore}
              </button>
            )}
          </div>
          <div ref={sentinelRef} className="hub-sentinel" aria-hidden="true" />
          {(selected.size > 0 || selectMode) && (
            <div className={`hub-bulkbar${bulkBusy ? " busy" : ""}`} role="toolbar" aria-label={copy.select}>
              <span className="hub-bulk-count" aria-live="polite">
                {selected.size > 0
                  ? fillCopy(copy.selCount, { n: String(selected.size) })
                  : copy.selHint}
              </span>
              <button type="button" className="watch-btn" disabled={bulkBusy} onClick={selectAllVisible}>{copy.selectAll}</button>
              <div className="hub-bulk-popwrap">
                <button
                  type="button"
                  className={`watch-btn hub-bulk-primary${albumOpen ? " on" : ""}`}
                  disabled={bulkBusy || selected.size === 0}
                  aria-expanded={albumOpen}
                  onClick={() => { setAlbumOpen((v) => !v); setMoveOpen(false); setLeaveOpen(false); setBulkExportOpen(false); }}
                >
                  {copy.addToAlbum}
                </button>
                {albumOpen && (
                  <div className="hub-bulk-pop" role="dialog" aria-label={copy.addToAlbum}>
                    <p className="hub-bulk-pop-hint">{copy.addToAlbumHint}</p>
                    {collPickOptions.length ? (
                      <div className="hub-bulk-pop-list">
                        {collPickOptions.map((a) => (
                          <button
                            key={a.tag}
                            type="button"
                            className="hub-bulk-pop-item"
                            disabled={bulkBusy}
                            title={a.tag}
                            onClick={() => void bulkAddToAlbum(a.tag)}
                          >
                            <span className="hub-bulk-pop-item-main">
                              <span className={`hub-bulk-pop-kind k-${a.kind}`}>
                                {a.kind === "job" ? copy.collTitle : copy.collAlbumTitle}
                              </span>
                              <span>{a.title}</span>
                            </span>
                            <span className="n">{a.count}</span>
                          </button>
                        ))}
                      </div>
                    ) : (
                      <p className="hub-bulk-pop-empty">{copy.albumEmpty}</p>
                    )}
                    <div className="hub-bulk-pop-new">
                      <span className="hub-bulk-pop-k">{copy.albumNew}</span>
                      <input
                        className="hub-field"
                        value={albumName}
                        onChange={(e) => setAlbumName(e.target.value)}
                        placeholder={copy.albumNamePh}
                        aria-label={copy.albumNamePh}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && albumName.trim()) {
                            e.preventDefault();
                            void bulkAddToAlbum(albumTagOf(albumName));
                          }
                        }}
                      />
                      <button
                        type="button"
                        className="watch-btn hub-bulk-primary"
                        disabled={bulkBusy || !albumName.trim() || selected.size === 0}
                        onClick={() => void bulkAddToAlbum(albumTagOf(albumName))}
                      >
                        {copy.albumCreate}
                      </button>
                    </div>
                  </div>
                )}
              </div>
              <div className="hub-bulk-popwrap">
                <button
                  type="button"
                  className={`watch-btn${moveOpen ? " on" : ""}`}
                  disabled={bulkBusy || selected.size === 0}
                  aria-expanded={moveOpen}
                  onClick={() => { setMoveOpen((v) => !v); setAlbumOpen(false); setLeaveOpen(false); setBulkExportOpen(false); }}
                >
                  {copy.moveMany}
                </button>
                {moveOpen && (
                  <div className="hub-bulk-pop hub-bulk-pop-move" role="dialog" aria-label={copy.moveMany}>
                    <p className="hub-bulk-pop-hint">{copy.moveManyHint}</p>
                    <div className="hub-bulk-pop-k">{copy.moveForm}</div>
                    <FormPicker
                      value=""
                      onChange={(id) => void bulkMoveForm(id)}
                      options={(locale.startsWith("en")
                        ? (taxonomy?.form_options_en || taxonomy?.form_options || [])
                        : (taxonomy?.form_options || [])) as [string, string][]}
                      placeholder={copy.formUnclassified}
                      searchPh={copy.formPickSearch}
                      emptyPh={copy.formPickEmpty}
                    />
                    <button
                      type="button"
                      className="hub-bulk-pop-item muted"
                      disabled={bulkBusy}
                      onClick={() => void bulkMoveForm("")}
                    >
                      {copy.formUnclassified}
                    </button>
                  </div>
                )}
              </div>
              <div className="hub-bulk-popwrap">
                <button
                  type="button"
                  className={`watch-btn${leaveOpen ? " on" : ""}`}
                  disabled={bulkBusy || selected.size === 0 || leaveCollOptions.length === 0}
                  aria-expanded={leaveOpen}
                  onClick={() => { setLeaveOpen((v) => !v); setAlbumOpen(false); setMoveOpen(false); setBulkExportOpen(false); }}
                >
                  {copy.leaveColl}
                </button>
                {leaveOpen && (
                  <div className="hub-bulk-pop" role="dialog" aria-label={copy.leaveColl}>
                    <p className="hub-bulk-pop-hint">{copy.leaveCollHint}</p>
                    {leaveCollOptions.length ? (
                      <div className="hub-bulk-pop-list">
                        {leaveCollOptions.map((a) => (
                          <button
                            key={a.tag}
                            type="button"
                            className="hub-bulk-pop-item"
                            disabled={bulkBusy}
                            title={a.tag}
                            onClick={() => void bulkLeaveColl(a.tag)}
                          >
                            <span className="hub-bulk-pop-item-main">
                              <span className={`hub-bulk-pop-kind k-${a.kind}`}>
                                {a.kind === "job" ? copy.collTitle : copy.collAlbumTitle}
                              </span>
                              <span>{a.title}</span>
                            </span>
                            <span className="n">{a.count}</span>
                          </button>
                        ))}
                      </div>
                    ) : (
                      <p className="hub-bulk-pop-empty">{copy.leaveCollEmpty}</p>
                    )}
                  </div>
                )}
              </div>
              <button type="button" className="watch-btn" disabled={bulkBusy || selected.size === 0} onClick={() => void bulkFavorite()}>{copy.favorite}</button>
              <button type="button" className="watch-btn" disabled={bulkBusy || selected.size === 0} onClick={() => void bulkUnfavorite()}>{copy.unfavorite}</button>
              <div className="hub-bulk-popwrap">
                <button
                  type="button"
                  className={`watch-btn${bulkExportOpen ? " on" : ""}`}
                  disabled={bulkBusy || selected.size === 0}
                  aria-expanded={bulkExportOpen}
                  onClick={() => {
                    setBulkExportOpen((v) => {
                      if (!v) setBulkExportResult(null);
                      return !v;
                    });
                    setAlbumOpen(false); setMoveOpen(false); setLeaveOpen(false);
                  }}
                >
                  {copy.bulkExport}
                </button>
                {bulkExportOpen && (
                  <div className="hub-bulk-pop hub-bulk-pop-move" role="dialog" aria-label={copy.bulkExport}>
                    <p className="hub-bulk-pop-hint">{copy.bulkExportHint}</p>
                    <select
                      className="hub-field"
                      aria-label={copy.exportOnlySource}
                      value={bulkExportLang}
                      onChange={(e) => { setBulkExportLang(e.target.value); setBulkExportResult(null); }}
                    >
                      <option value="">{copy.exportOnlySource}</option>
                      {bulkExportLangs.map((lg) => <option key={lg} value={lg}>{lg}</option>)}
                    </select>
                    <div className="hub-bulk-pop-list">
                      <button
                        type="button"
                        className="watch-btn hub-bulk-primary"
                        disabled={bulkExportBusy || selected.size === 0}
                        onClick={() => void bulkDeliver(false)}
                      >{copy.exportPlan}</button>
                      <button
                        type="button"
                        className="watch-btn"
                        disabled={bulkExportBusy || !bulkExportResult || bulkExportResult.dry_run === false}
                        onClick={() => void bulkDeliver(true)}
                      >{copy.exportConfirm}</button>
                    </div>
                    {bulkExportResult ? (
                      <div className="hub-export-result">
                        <span className="chip warn">
                          {copy.exportResult}: {bulkExportResult.dry_run ? copy.exportPlan : copy.exportConfirm}
                        </span>
                        {(bulkExportResult.written && bulkExportResult.written.length
                          ? bulkExportResult.written
                          : (bulkExportResult.plan || [])).map((e, i) => (
                          <div key={i} className="hub-export-file">
                            <span className="chip">{e.mode || e.status || "file"}</span>
                            <span className="hub-export-name">
                              {e.dst ? e.dst.split(/[\\/]/).pop() : e.id}
                            </span>
                            {e.url ? <a className="chip" href={e.url} download>{copy.exportDownload}</a> : null}
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </div>
                )}
              </div>
              <button type="button" className="watch-btn hub-danger" disabled={bulkBusy || selected.size === 0} onClick={() => void bulkDelete()}>{copy.del}</button>
              <button
                type="button"
                className="watch-btn"
                disabled={bulkBusy}
                onClick={() => {
                  clearSelect();
                  setSelectMode(false);
                  setAlbumOpen(false);
                  setMoveOpen(false);
                  setLeaveOpen(false);
                  setBulkExportOpen(false);
                  setBulkExportResult(null);
                }}
              >
                {copy.exitSelect}
              </button>
              {bulkBusy ? <span className="hub-bulk-busy" aria-live="polite">{copy.bulkWorking}</span> : null}
            </div>
          )}
        </div>
      </div>

      {detail ? (
        <div className="hub-modal" role="dialog" onClick={() => setDetail(null)}>
          <div className="hub-modal-box" onClick={(e) => e.stopPropagation()}>
            <div className="hub-modal-head">
              <h2>{displayName(detail)}</h2>
              <button
                type="button"
                className="hub-modal-x"
                aria-label="close"
                onClick={() => setDetail(null)}
              >
                ✕
              </button>
            </div>
            {detail.missing ? (
              <div className="hub-prev"><span className="ph">⚠</span></div>
            ) : isSubtitleFile(detail) ? (
              <SubtitlePanel detail={detail} copy={copy} />
            ) : (
              <HubFilePreview
                key={detail.id}
                detail={detail}
                copy={copy}
                onZoom={() => setZoom(true)}
              />
            )}
            <dl className="hub-kv">
              <dt>{copy.typeSize}</dt>
              <dd>
                {kindLabel(detail.kind)} · {fmtSize(detail.size || 0)}
              </dd>
              <dt>{copy.path}</dt>
              <dd className="hub-path" title={detail.external_path || detail.rel_path || ""}>
                {detail.external_path || detail.rel_path || "—"}
                {detail.missing ? ` (${copy.missing})` : ""}
              </dd>
            </dl>
            {runRec || understandRec ? (
              <div className="hub-run">
                {understandRec ? (
                  <button
                    type="button"
                    className={`chip${understandRec.reviewed ? " ok" : ""}`}
                    onClick={() => {
                      void hubSetReviewed(detail.id, !understandRec.reviewed)
                        .then(setUnderstandRec)
                        .catch(() => undefined);
                    }}
                  >
                    {understandRec.reviewed ? copy.reviewedYes : copy.reviewedNo}
                  </button>
                ) : null}
                {understandRec?.speech?.lang ? (
                  <span className="chip">
                    {copy.hitBadgeSpeech} · {understandRec.speech.lang}
                  </span>
                ) : null}
                {runRec?.steps
                  ? Object.entries(runRec.steps).map(([k, v]) => (
                      <span key={k} className={`chip${v === "ok" ? "" : " warn"}`}>
                        {k}
                        {v === "ok" ? "" : ` · ${copy.missingStep}`}
                      </span>
                    ))
                  : null}
                {runRec?.bad_reason ? (
                  <span className="chip warn">{runRec.bad_reason}</span>
                ) : null}
              </div>
            ) : null}
            {readiness ? (
              <div className="hub-run">
                <span className={`chip${readiness.channel_ready ? " ok" : " warn"}`}>
                  {readiness.channel_ready ? copy.readyYes : copy.readyNo}
                </span>
                {readiness.blocking && readiness.blocking.length ? (
                  <span className="chip warn">
                    {copy.readyReason}: {readiness.blocking.join(" · ")}
                  </span>
                ) : null}
                {readiness.subs_langs && readiness.subs_langs.length ? (
                  <span className="chip">lang: {readiness.subs_langs.join(",")}</span>
                ) : null}
              </div>
            ) : null}
            {/* 步骤 6:按需交付(计划 → 确认 → 下载) — 仅视频需要字幕烧录/嵌入,其它类型不显示 */}
            {detail.kind === "videos" ? (
            <div className="hub-run hub-export">
              <div className="hub-export-title">{copy.exportTitle}</div>
              <div className="hub-export-row">
                <label className="hub-export-label">{copy.exportPickLang}:</label>
                <select
                  className="hub-select"
                  value={exportLang}
                  onChange={(e) => setExportLang(e.target.value)}
                >
                  <option value="">{copy.exportOnlySource}</option>
                  {(readiness?.subs_langs || []).map((lg) => (
                    <option key={lg} value={lg}>{lg}</option>
                  ))}
                </select>
                <button
                  className="btn"
                  disabled={exportBusy || !!detail.missing}
                  onClick={async () => {
                    setExportBusy(true);
                    try {
                      const r = await hubDeliver({ ids: [detail.id], lang: exportLang || null, confirm: false });
                      setExportResult(r);
                    } finally { setExportBusy(false); }
                  }}
                >{copy.exportPlan}</button>
                <button
                  className="btn"
                  disabled={exportBusy || !exportResult || exportResult.dry_run === false}
                  onClick={async () => {
                    setExportBusy(true);
                    try {
                      const r = await hubDeliver({ ids: [detail.id], lang: exportLang || null, confirm: true });
                      setExportResult(r);
                    } finally { setExportBusy(false); }
                  }}
                >{copy.exportConfirm}</button>
              </div>
              {exportResult ? (
                <div className="hub-export-result">
                  {exportResult.dry_run ? (
                    <span className="chip warn">{copy.exportResult}: {copy.exportPlan}</span>
                  ) : (
                    <span className="chip ok">{copy.exportResult}</span>
                  )}
                  {(exportResult.written && exportResult.written.length
                    ? exportResult.written
                    : (exportResult.plan || [])).map((e, i) => (
                    <div key={i} className="hub-export-file">
                      <span className="chip">{e.mode || e.status || "file"}</span>
                      <span className="hub-export-name">
                        {e.dst ? e.dst.split(/[\\/]/).pop() : e.id}
                      </span>
                      {e.url ? (
                        <a className="chip" href={e.url} download>{copy.exportDownload}</a>
                      ) : null}
                    </div>
                  ))}
                  {(exportResult.errors && exportResult.errors.length) ? (
                    <span className="chip warn">{exportResult.errors.length} error(s)</span>
                  ) : null}
                </div>
              ) : null}
            </div>
            ) : null}
            <input
              className="hub-field"
              value={editTags}
              onChange={(e) => setEditTags(e.target.value)}
              placeholder={copy.tags}
            />
            <textarea
              className="hub-field"
              value={editDesc}
              onChange={(e) => setEditDesc(e.target.value)}
              rows={3}
            />
            <div className="hub-acts">
              <button
                type="button"
                onClick={() => {
                  const p = detail.external_path || detail.rel_path || "";
                  void navigator.clipboard?.writeText(p);
                }}
              >
                {copy.copyPath}
              </button>
              {!detail.missing ? (
                <a className="watch-btn" href={hubFileUrl(detail.id)} target="_blank" rel="noreferrer">
                  {copy.openFile}
                </a>
              ) : null}
              <span className="sp" />
              <button type="button" className="watch-btn" onClick={() => void saveDetail()}>
                {copy.save}
              </button>
              <button type="button" className="hub-danger" onClick={() => void removeDetail()}>
                {copy.del}
              </button>
              <button type="button" onClick={() => setDetail(null)}>
                {copy.close}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {zoom && detail && !detail.missing && detail.kind === "images" ? (
        <div
          className="hub-lightbox"
          onClick={() => {
            setZoom(false);
            setOne2One(false);
          }}
        >
          <div
            className={"hub-lb-view" + (one2one ? " one2one" : "")}
            onClick={(e) => e.stopPropagation()}
          >
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={hubFileUrl(detail.id)}
              alt=""
              className={one2one ? "one2one" : ""}
              decoding="async"
              onClick={() => setOne2One((v) => !v)}
            />
          </div>
          <div className="hub-lb-hint" onClick={(e) => e.stopPropagation()}>
            {one2one ? copy.img1to1 : copy.imgFit}
          </div>
        </div>
      ) : null}

      {toast ? <div className="hub-toast">{toast}</div> : null}
    </div>
  );
}
