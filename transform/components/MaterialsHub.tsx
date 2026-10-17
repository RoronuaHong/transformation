"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { memo, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
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
} from "@/lib/hub-api";

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

const KINDS = [
  "raw",
  "images",
  "videos",
  "silent",
  "docs",
  "audio",
  "subs",
  "anim",
  "project",
  "vector",
  "model3d",
  "font",
  "other",
] as const;

// 每个 kind 的图标(emoji,零依赖、通用、无需翻译);"" 为「全部」入口。
const KIND_ICON: Record<string, string> = {
  "": "📚",
  raw: "📷",
  images: "🖼️",
  videos: "🎬",
  silent: "🔇",
  docs: "📄",
  audio: "🎵",
  subs: "💬",
  anim: "🎞️",
  project: "🗂️",
  vector: "✒️",
  model3d: "🧊",
  font: "🔤",
  other: "📦",
};

type KindKey = (typeof KINDS)[number] | "";

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
  silent: "kindSilent",
  docs: "kindDocs",
  audio: "kindAudio",
  subs: "kindSubs",
  anim: "kindAnim",
  raw: "kindRaw",
  project: "kindProject",
  vector: "kindVector",
  model3d: "kindModel3d",
  font: "kindFont",
  other: "kindOther",
};

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
}

const HubCard = memo(function HubCard({ m, copy, kindLabel, tagLabelMap, locale, pendingIds, onOpen, onDelete }: HubCardProps) {
  return (
    <article className="hub-card">
      <div className="hub-thumb">
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
        {m.missing ? (
          <span className="pill bad">{copy.missing}</span>
        ) : m.kind === "images" || m.kind === "anim" ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={m.thumb ? hubThumbUrl(m.id) : hubFileUrl(m.id)}
            alt=""
            loading="lazy"
            decoding="async"
            onClick={() => onOpen(m)}
          />
        ) : m.kind === "videos" || m.kind === "silent" ? (
          <>
            {/* video 不能包在 <button> 里,否则原生进度条无法拖动 */}
            <video
              src={hubFileUrl(m.id)}
              muted
              playsInline
              controls
              preload="metadata"
              poster={m.thumb ? hubThumbUrl(m.id) : undefined}
              onLoadedMetadata={(e) => {
                if (m.hit_t) e.currentTarget.currentTime = m.hit_t;
              }}
              onClick={(e) => e.stopPropagation()}
              onPointerDown={(e) => e.stopPropagation()}
              onMouseDown={(e) => e.stopPropagation()}
            />
            {m.kind === "silent" ? (
              <span className="hub-kind-badge">{kindLabel("silent")}</span>
            ) : null}
          </>
        ) : m.kind === "audio" ? (
          <span className="ph" onClick={() => onOpen(m)} role="presentation">
            ♪
          </span>
        ) : (
          <span className="pill" onClick={() => onOpen(m)} role="presentation">
            {(m.ext || "?").replace(/^\./, "").slice(0, 5).toUpperCase()}
          </span>
        )}
      </div>
      <button
        type="button"
        className="hub-card-body"
        onClick={() => onOpen(m)}
      >
        <strong title={displayName(m)}>{displayName(m)}</strong>
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

export function MaterialsHub({ copy }: { copy: HubCopy }) {
  const params = useParams();
  const locale = typeof params?.locale === "string" ? params.locale : "zh";
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<string>("");
  const [tag, setTag] = useState("");
  const [mode, setMode] = useState("auto");
  const [sort, setSort] = useState("");
  const [page, setPage] = useState(1);
  const pageSize = 48;
  const [rows, setRows] = useState<HubMaterial[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<HubStats | null>(null);
  const [tags, setTags] = useState<HubTag[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [toast, setToast] = useState("");
  const [upNote, setUpNote] = useState("");
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [gridMode, setGridMode] = useState<"" | "2" | "1">("");
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
  const [editTags, setEditTags] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const [zoom, setZoom] = useState(false);
  const [one2one, setOne2One] = useState(false);

  // 内容形式多层级筛选(主轴)+ 题材/用途(多选标签)
  const [taxonomy, setTaxonomy] = useState<HubTaxonomy | null>(null);
  const [formSel, setFormSel] = useState("");   // 选中一级分组 或 二级 leaf
  const [topicSel, setTopicSel] = useState("");
  const [usageSel, setUsageSel] = useState("");
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

  const load = useCallback(async () => {
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
      // 内容形式/题材/用途 是受控词表分面,后端 /api/list 不支持这三个参数,
      // 故在前端按全量本地过滤(素材量级小,逻辑简单且零回归风险)。
      // 但文本检索 q / kind / tag / mode / sort 是后端能力,必须继续下传,否则叠加分面时
      // 文本检索会被悄悄丢弃(反之亦然:若丢弃 q 又会让人误以为"分面没生效")。
      const needLocal = !!(formSel || topicSel || usageSel);
      // 后端 /list 入参(分面之外的维度,始终下传)
      const srv = new URLSearchParams();
      if (q.trim()) srv.set("q", q.trim());
      if (kind) srv.set("kind", kind);
      if (tag) srv.set("tag", tag);
      if (mode) srv.set("mode", mode);
      if (sort) srv.set("sort", sort);
      let rowsAll: HubMaterial[];
      if (needLocal) {
        const list = await hubFetch<HubMaterial[]>(`/list?${srv}`); // 不带 limit = 全量
        if (loadSeq.current !== seq) return;
        rowsAll = Array.isArray(list) ? list : [];
        if (formSel) {
          // 选父节点 → 匹配其全部后代(含自身);叶子 → 精确匹配
          const desc = (taxonomy?.form_descendants && taxonomy.form_descendants[formSel]) || [formSel];
          rowsAll = rowsAll.filter((r) => !!r.form && desc.includes(r.form));
        }
        if (topicSel) {
          // 选父组 → 匹配其全部叶子;选叶子 → 精确匹配
          const leaves = (taxonomy?.topic_descendants && taxonomy.topic_descendants[topicSel]) || [topicSel];
          // 题材与用途存在同名标签(如「影视」):素材把影视只标在 usage 上、topics 不含,
          // 导致点「题材›影视」命中 0 而「用途›影视」有 7 条。同名叶子同时按 usage 兜底匹配,
          // 让按题材找影视内容也能捞到(仅对确实同名的叶子生效,不影响其它题材)。
          const usageLabels = new Set(taxonomy?.usages || []);
          const topicOrUsage = leaves.filter((l) => usageLabels.has(l));
          rowsAll = rowsAll.filter((r) =>
            csv(r.topics).some((t) => leaves.includes(t)) ||
            (topicOrUsage.length > 0 && csv(r.usage).some((u) => topicOrUsage.includes(u))));
        }
        if (usageSel)
          rowsAll = rowsAll.filter((r) => csv(r.usage).includes(usageSel));
      } else {
        const count = await hubFetch<{ total: number }>(`/count?${qs}`);
        if (loadSeq.current !== seq) return;
        const tTotal = count.total || 0;
        setTotal(tTotal);
        const pages = Math.max(1, Math.ceil(tTotal / pageSize));
        const p = Math.min(page, pages);
        if (p !== page) setPage(p);
        qs.set("offset", String((p - 1) * pageSize));
        qs.set("limit", String(pageSize));
        const list = await hubFetch<HubMaterial[]>(`/list?${qs}`);
        if (loadSeq.current !== seq) return;
        rowsAll = Array.isArray(list) ? list : [];
      }
      setTotal(rowsAll.length);
      const pages = Math.max(1, Math.ceil(rowsAll.length / pageSize));
      const p = Math.min(page, pages);
      if (p !== page) setPage(p);
      setRows(rowsAll.slice((p - 1) * pageSize, p * pageSize));
      const st = await hubFetch<HubStats>("/stats");
      setStats(st);
      const tg = await hubFetch<HubTag[]>(`/tags?ui=1&lang=${encodeURIComponent(locale.startsWith("zh") ? "zh" : "en")}`).catch(() => [] as HubTag[]);
      if (loadSeq.current !== seq) return;
      setTags(Array.isArray(tg) ? tg : []);
    } catch (e) {
      if (loadSeq.current !== seq) return;
      setRows([]);
      setStats(null);
      setErr(e instanceof Error ? e.message : copy.error);
    } finally {
      if (loadSeq.current === seq) setBusy(false);
    }
  }, [q, kind, tag, mode, sort, page, formSel, topicSel, usageSel, taxonomy, copy.error, locale]);

  useEffect(() => {
    // 250ms 防抖:搜索框每敲一个字不再立刻发 count/list/stats/tags 四个请求
    // (count+list 各做一次全量排序,auto 模式还要过 embedding,连续击键会堆积请求)。
    // busy 立即置 true:防抖窗口内不能闪现「没有匹配的素材」空态。
    setBusy(true);
    const t = window.setTimeout(() => void load(), 250);
    return () => window.clearTimeout(t);
  }, [load]);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(""), 2200);
    return () => window.clearTimeout(t);
  }, [toast]);

  const pages = Math.max(1, Math.ceil(total / pageSize));

  const facets = useMemo(() => {
    const kinds = stats?.kinds || {};
    return [
      { val: "", label: copy.all, n: stats?.total || 0 },
      ...orderKinds(Object.keys(kinds)).map((k) => ({
        val: k,
        label: kindLabel(k),
        n: kinds[k] || 0,
      })),
    ];
  }, [stats, copy.all, kindLabel]);

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
      if (d) return d.reduce((s: number, lid: string) => s + (forms[lid] || 0), 0);
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
    form: true, kind: false, topic: false, usage: false, tags: false,
  });
  // 标签分业务/系统:系统标签来自流水线内部(sp/bilibili/job:/type:/parent:/role:/lang:/asr/demux/upload),默认收起
  const SYS_TAG_RE = /^(sp|bilibili|job:|type:|parent:|role:|lang:|asr|demux|upload)/;
  const bizTags = useMemo(() => (tags || []).filter((t) => !SYS_TAG_RE.test(t.tag)), [tags]);
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

  // 移动端筛选抽屉:应用筛选后自动收起,让结果区可见
  useEffect(() => {
    setFiltersOpen(false);
  }, [kind, tag, formSel, topicSel, usageSel]);

  // 网格密度:localStorage 记忆(自适应/一行2个/一行1个)
  useEffect(() => {
    try {
      const saved = window.localStorage.getItem("hubGridMode");
      if (saved === "2" || saved === "1") setGridMode(saved);
    } catch {}
  }, []);

  function applyGridMode(m: "" | "2" | "1") {
    setGridMode(m);
    try {
      window.localStorage.setItem("hubGridMode", m);
    } catch {}
  }

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
    await load();
  }, [copy, load, setToast, setDetail]);

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

  return (
    <div className="hub-page">
      <div className="hub-layout">
        <button
          type="button"
          className="hub-aside-toggle"
          aria-expanded={filtersOpen}
          aria-controls="hub-aside-body"
          onClick={() => setFiltersOpen((v) => !v)}
        >
          <span className="nav-burger-box" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
          {copy.asideFilter}
        </button>
        <aside id="hub-aside-body" className={`hub-aside${filtersOpen ? " open" : ""}`}>
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
                    onClick={() => { setKind(kind === f.val ? "" : f.val); setPage(1); }}
                  >
                    <span className="lbl">
                      {f.val === "raw" && <span className="ico" aria-hidden="true">{KIND_ICON[f.val]}</span>}
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
          <header className="home-intro try-intro">
            <p className="kicker">{copy.kicker}</p>
            <h1 className="hero-line">{copy.headline}</h1>
            <p className="lede">{copy.lede}</p>
            <div className="hub-hero-cta">
              <Link className="watch-btn hub-agent-cta" href={`/${locale}/hub/agent`}>
                {copy.openAgent}
              </Link>
              <Link className="watch-btn hub-cta-secondary" href={`/${locale}`}>
                {copy.openCrop}
              </Link>
            </div>
          </header>

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
            <div className="hub-toolbar-actions">
              <select value={mode} onChange={(e) => { setMode(e.target.value); setPage(1); }} aria-label={copy.modeAuto}>
                <option value="auto">{copy.modeAuto}</option>
                <option value="lexical">{copy.modeLex}</option>
                <option value="semantic">{copy.modeSem}</option>
              </select>
              <select value={sort} onChange={(e) => { setSort(e.target.value); setPage(1); }} aria-label={copy.sortDefault}>
                <option value="">{copy.sortDefault}</option>
                <option value="newest">{copy.sortNewest}</option>
                <option value="name">{copy.sortName}</option>
                <option value="size">{copy.sortSize}</option>
              </select>
              {upNote ? <span className="hub-upnote">{upNote}</span> : null}
              <button type="button" className="watch-btn" onClick={() => void load()} disabled={busy}>
                {copy.refresh}
              </button>
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

          <div className="hub-resultbar">
            {q.trim() ? (
              <p className="hub-stats">
                {fillCopy(copy.hitOf, { q: q.trim(), n: String(total) })}
              </p>
            ) : (
              <p className="hub-stats muted">
                {copy.statTotal} {stats?.total ?? "—"}
                {stats
                  ? ` · ${Object.entries(stats.kinds || {})
                      .map(([k, v]) => `${kindLabel(k)} ${v}`)
                      .join(" · ")}`
                  : ""}
              </p>
            )}
            <div className="hub-gridmode" role="group" aria-label={copy.gridAuto}>
              <button
                type="button"
                className={gridMode === "" ? "on" : ""}
                aria-pressed={gridMode === ""}
                title={copy.gridAuto}
                aria-label={copy.gridAuto}
                onClick={() => applyGridMode("")}
              >
                {/* auto-fill:2x2 自适应方块 */}
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <rect x="1.5" y="1.5" width="5" height="5" rx="1" />
                  <rect x="9.5" y="1.5" width="5" height="5" rx="1" />
                  <rect x="1.5" y="9.5" width="5" height="5" rx="1" />
                  <rect x="9.5" y="9.5" width="5" height="5" rx="1" />
                </svg>
              </button>
              <button
                type="button"
                className={gridMode === "2" ? "on" : ""}
                aria-pressed={gridMode === "2"}
                title={copy.gridTwo}
                aria-label={copy.gridTwo}
                onClick={() => applyGridMode("2")}
              >
                {/* 一行 2 个:两根立柱 */}
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <rect x="1.5" y="1.5" width="5" height="13" rx="1" />
                  <rect x="9.5" y="1.5" width="5" height="13" rx="1" />
                </svg>
              </button>
              <button
                type="button"
                className={gridMode === "1" ? "on" : ""}
                aria-pressed={gridMode === "1"}
                title={copy.gridOne}
                aria-label={copy.gridOne}
                onClick={() => applyGridMode("1")}
              >
                {/* 一行 1 个:单根立柱 */}
                <svg viewBox="0 0 16 16" aria-hidden="true">
                  <rect x="4.5" y="1.5" width="7" height="13" rx="1" />
                </svg>
              </button>
            </div>
          </div>
          {!rows.length ? (
            <div className="hub-empty" role="status">
              {busy ? (
                <>
                  <span className="hub-spin" aria-hidden />
                  <p>{copy.loading}</p>
                </>
              ) : (
                <>
                  <p>{copy.empty}</p>
                  <p className="muted">{copy.emptyHint}</p>
                </>
              )}
            </div>
          ) : (
            <div
              className={
                "hub-grid" +
                (gridMode ? ` cols-${gridMode}` : "") +
                (busy ? " busy" : "")
              }
            >
              {rows.map((m) => (
                <HubCard
                  key={m.id}
                  m={m}
                  copy={copy}
                  kindLabel={kindLabel}
                  tagLabelMap={tagLabelMap}
                  locale={locale}
                  pendingIds={pendingIds}
                  onOpen={openDetail}
                  onDelete={removeMaterial}
                />
              ))}
            </div>
          )}

          <div className="hub-pager">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              {copy.prev}
            </button>
            <span>
              {fillCopy(copy.pageOf, {
                p: String(page),
                n: String(pages),
                t: String(total),
              })}
            </span>
            <button
              type="button"
              disabled={page >= pages}
              onClick={() => setPage((p) => p + 1)}
            >
              {copy.next}
            </button>
          </div>
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
            <div
              className={
                "hub-prev" +
                (detail.kind === "images" || detail.kind === "anim" ? " checker" : "")
              }
            >
              {detail.missing ? (
                <span className="ph">⚠</span>
              ) : detail.kind === "images" || detail.kind === "anim" ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={hubFileUrl(detail.id)}
                  alt=""
                  className="zoomable"
                  title={copy.zoomTitle}
                  decoding="async"
                  onClick={() => setZoom(true)}
                />
              ) : detail.kind === "videos" || detail.kind === "silent" ? (
                <video
                  key={detail.id}
                  src={hubFileUrl(detail.id)}
                  controls
                  playsInline
                  preload="metadata"
                  muted={detail.kind === "silent"}
                  poster={detail.thumb ? hubThumbUrl(detail.id) : undefined}
                  onLoadedMetadata={(e) => {
                    if (detail.hit_t) e.currentTarget.currentTime = detail.hit_t;
                  }}
                />
              ) : detail.kind === "audio" ? (
                <audio src={hubFileUrl(detail.id)} controls />
              ) : (
                <span className="ph">{(detail.ext || "?").toUpperCase()}</span>
              )}
            </div>
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
            {/* 步骤 6:按需交付(计划 → 确认 → 下载) */}
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

      {zoom && detail && !detail.missing && (detail.kind === "images" || detail.kind === "anim") ? (
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
