"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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

const dedupe = (arr: string[]): string[] => Array.from(new Set(arr));

const KINDS = [
  "images",
  "videos",
  "silent",
  "docs",
  "audio",
  "subs",
  "anim",
  "other",
] as const;

type KindKey = (typeof KINDS)[number] | "";

const KIND_LABEL: Record<string, keyof HubCopy> = {
  images: "kindImages",
  videos: "kindVideos",
  silent: "kindSilent",
  docs: "kindDocs",
  audio: "kindAudio",
  subs: "kindSubs",
  anim: "kindAnim",
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

export function MaterialsHub({ copy }: { copy: HubCopy }) {
  const params = useParams();
  const locale = typeof params?.locale === "string" ? params.locale : "zh";
  const [q, setQ] = useState("");
  const [kind, setKind] = useState<KindKey>("");
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
  const [maxUp, setMaxUp] = useState(HUB_UPLOAD_MAX_BYTES);
  const [detail, setDetail] = useState<HubMaterial | null>(null);
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
      return key ? copy[key] : copy.kindOther;
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
      const st = await hubFetch<HubStats>("/stats");
      setRows(Array.isArray(list) ? list : []);
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
  }, [q, kind, tag, mode, sort, page, copy.error, locale]);

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
      { val: "" as KindKey, label: copy.all, n: stats?.total || 0 },
      ...KINDS.map((k) => ({
        val: k as KindKey,
        label: kindLabel(k),
        n: kinds[k] || 0,
      })),
    ];
  }, [stats, copy.all, kindLabel]);

  // 标签人读文案以服务端 /tags?ui=1 为准(hub-api.ts 的 formatHubTag 仅作兜底)。
  const tagLabelMap = useMemo(() => {
    const map: Record<string, string> = {};
    for (const t of tags) {
      if (t.label) map[t.tag] = t.label;
    }
    return map;
  }, [tags]);

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
        const r = await hubUploadFile(f, (pct) =>
          setUpNote(fillCopy(copy.uploading, { name: f.name, pct: String(pct) })),
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

  async function removeMaterial(m: HubMaterial) {
    if (!window.confirm(`${copy.del}「${displayName(m)}」？`)) return;
    await hubFetch("/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: m.id }),
    });
    setToast(copy.deleted);
    if (detail?.id === m.id) setDetail(null);
    await load();
  }

  async function removeDetail() {
    if (!detail) return;
    await removeMaterial(detail);
  }

  function openDetail(m: HubMaterial) {
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
  }

  return (
    <div className="hub-page">
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
          <button type="button" className="watch-btn hub-upload" onClick={() => fileRef.current?.click()}>
            {copy.upload}
          </button>
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

      <div className="hub-layout">
        <aside className="hub-aside">
          <h3>{copy.statTotal}</h3>
          <div className="hub-facets" id="kindFacets">
            {facets.map((f) => (
              <button
                key={f.val || "all"}
                type="button"
                className={`hub-facet${kind === f.val ? " on" : ""}`}
                onClick={() => {
                  setKind(f.val);
                  setPage(1);
                }}
              >
                <span>{f.label}</span>
                <span className="n">{f.n}</span>
              </button>
            ))}
          </div>
          <h3>{copy.tags}</h3>
          <div className="hub-tags">
            {tags.slice(0, 28).map((tg) => (
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
          {(kind || tag || q) && (
            <button
              type="button"
              className="hub-clear"
              onClick={() => {
                setKind("");
                setTag("");
                setQ("");
                setSort("");
                setPage(1);
              }}
            >
              {copy.clear}
            </button>
          )}
        </aside>

        <div className="hub-main">
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
            <div className={"hub-grid" + (busy ? " busy" : "")}>
              {rows.map((m) => (
                <article key={m.id} className="hub-card">
                  <div className="hub-thumb">
                    <button
                      type="button"
                      className="hub-card-del"
                      title={copy.del}
                      aria-label={`${copy.del} ${displayName(m)}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        void removeMaterial(m);
                      }}
                    >
                      {copy.del}
                    </button>
                    {m.missing ? (
                      <span className="pill bad">{copy.missing}</span>
                    ) : m.kind === "images" || m.kind === "anim" ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img
                        src={hubFileUrl(m.id)}
                        alt=""
                        loading="lazy"
                        onClick={() => openDetail(m)}
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
                      <span className="ph" onClick={() => openDetail(m)} role="presentation">
                        ♪
                      </span>
                    ) : (
                      <span className="pill" onClick={() => openDetail(m)} role="presentation">
                        {(m.ext || "?").replace(/^\./, "").slice(0, 5).toUpperCase()}
                      </span>
                    )}
                  </div>
                  <button
                    type="button"
                    className="hub-card-body"
                    onClick={() => openDetail(m)}
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
