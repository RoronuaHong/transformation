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
  hubUploadFile,
  type HubMaterial,
  type HubStats,
  type HubTag,
} from "@/lib/hub-api";

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
  const [detail, setDetail] = useState<HubMaterial | null>(null);
  const [editTags, setEditTags] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const kindLabel = useCallback(
    (k: string) => {
      const key = KIND_LABEL[k];
      return key ? copy[key] : copy.kindOther;
    },
    [copy],
  );

  const load = useCallback(async () => {
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
      const tTotal = count.total || 0;
      setTotal(tTotal);
      const pages = Math.max(1, Math.ceil(tTotal / pageSize));
      const p = Math.min(page, pages);
      if (p !== page) setPage(p);
      qs.set("offset", String((p - 1) * pageSize));
      qs.set("limit", String(pageSize));
      const list = await hubFetch<HubMaterial[]>(`/list?${qs}`);
      const st = await hubFetch<HubStats>("/stats");
      setRows(Array.isArray(list) ? list : []);
      setStats(st);
      const tg = await hubFetch<HubTag[]>(`/tags?ui=1&lang=${encodeURIComponent(locale.startsWith("zh") ? "zh" : "en")}`).catch(() => [] as HubTag[]);
      setTags(Array.isArray(tg) ? tg : []);
    } catch (e) {
      setRows([]);
      setStats(null);
      setErr(e instanceof Error ? e.message : copy.error);
    } finally {
      setBusy(false);
    }
  }, [q, kind, tag, mode, sort, page, copy.error, locale]);

  useEffect(() => {
    void load();
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

  async function onUpload(files: FileList | null) {
    if (!files?.length) return;
    try {
      for (const f of Array.from(files)) {
        await hubUploadFile(f);
      }
      setToast(fillCopy(copy.uploaded, { n: String(files.length) }));
      setPage(1);
      await load();
    } catch (e) {
      setToast(e instanceof Error ? e.message : copy.error);
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
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
        />
        <select value={mode} onChange={(e) => { setMode(e.target.value); setPage(1); }}>
          <option value="auto">{copy.modeAuto}</option>
          <option value="lexical">{copy.modeLex}</option>
          <option value="semantic">{copy.modeSem}</option>
        </select>
        <select value={sort} onChange={(e) => { setSort(e.target.value); setPage(1); }}>
          <option value="">{copy.sortNewest}</option>
          <option value="name">{copy.sortName}</option>
          <option value="size">{copy.sortSize}</option>
        </select>
        <button type="button" className="watch-btn" onClick={() => void load()} disabled={busy}>
          {copy.refresh}
        </button>
        <button type="button" className="watch-btn" onClick={() => fileRef.current?.click()}>
          {copy.upload}
        </button>
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
          <p className="hub-stats muted">
            {copy.statTotal} {stats?.total ?? "—"}
            {stats
              ? ` · ${Object.entries(stats.kinds || {})
                  .map(([k, v]) => `${kindLabel(k)}:${v}`)
                  .join("  ")}`
              : ""}
          </p>
          {!rows.length && !busy ? (
            <div className="hub-empty">
              <p>{copy.empty}</p>
              <p className="muted">{copy.emptyHint}</p>
            </div>
          ) : (
            <div className="hub-grid">
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
                    <span className="muted">
                      {kindLabel(m.kind)} · {fmtSize(m.size || 0)}
                    </span>
                    {m.description ? (
                      <p className="hub-desc" title={m.description}>
                        {m.description.replace(/\n+/g, " ").slice(0, 90)}
                      </p>
                    ) : null}
                    <div className="hub-chips">
                      {chipTagsVisible(m.tags)
                        .slice(0, 4)
                        .map((x) => (
                          <span key={x} className="chip" title={x}>
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
            <h2>{displayName(detail)}</h2>
            <div className="hub-prev">
              {detail.missing ? (
                <span className="ph">⚠</span>
              ) : detail.kind === "images" || detail.kind === "anim" ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={hubFileUrl(detail.id)} alt="" />
              ) : detail.kind === "videos" || detail.kind === "silent" ? (
                <video
                  key={detail.id}
                  src={hubFileUrl(detail.id)}
                  controls
                  playsInline
                  preload="metadata"
                  muted={detail.kind === "silent"}
                  poster={detail.thumb ? hubThumbUrl(detail.id) : undefined}
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
              <dd title={detail.external_path || detail.rel_path || ""}>
                {detail.external_path || detail.rel_path || "—"}
                {detail.missing ? ` (${copy.missing})` : ""}
              </dd>
            </dl>
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

      {toast ? <div className="hub-toast">{toast}</div> : null}
    </div>
  );
}
