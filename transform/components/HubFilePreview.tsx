"use client";

import { useEffect, useRef, useState } from "react";
import { fillCopy, type HubCopy } from "@/lib/copy";
import {
  hubFileUrl,
  hubPreview,
  hubThumbUrl,
  type HubMaterial,
  type HubPreview as HubPreviewData,
} from "@/lib/hub-api";

/**
 * 素材详情的内容预览。格式判定只由后端 /api/preview/<id> 的 mode 决定,
 * 前端不重复维护扩展名表:文本/源码/表格/Office 提取文本/文件头十六进制
 * 都以 React text node 渲染(不执行不可信 HTML/SVG/JS);媒体走原生播放器,
 * 解码失败有明确降级说明;PDF 用浏览器内置查看器并额外给外链(MDN 建议)。
 */
type Props = {
  detail: HubMaterial;
  copy: HubCopy;
  onZoom?: () => void;
};

const REASON_KEYS: Record<string, keyof HubCopy> = {
  svg_static: "pvReasonSvg",
  browser_cannot_decode_image: "pvReasonImage",
  unsupported_container: "pvReasonMedia",
  unsupported_codec: "pvReasonMedia",
  office_text_only: "pvReasonOffice",
  legacy_office: "pvReasonLegacyOffice",
  rtf_approximate: "pvReasonRtf",
  active_source_only: "pvReasonActive",
  sniffed_text: "pvReasonSniffed",
  no_renderer: "pvReasonNoRenderer",
  parse_error: "pvReasonParseErr",
};

function reasonText(reason: string, copy: HubCopy): string {
  const key = REASON_KEYS[reason];
  return key ? String(copy[key]) : "";
}

function OpenLink({ id, copy }: { id: string; copy: HubCopy }) {
  return (
    <a className="hub-pv-open" href={hubFileUrl(id)} target="_blank" rel="noreferrer">
      {copy.openFile}
    </a>
  );
}

export default function HubFilePreview({ detail, copy, onZoom }: Props) {
  const [data, setData] = useState<HubPreviewData | null>(null);
  const [status, setStatus] = useState<"loading" | "ok" | "err">("loading");
  const [text, setText] = useState("");
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [mediaErr, setMediaErr] = useState(false);
  const [wrap, setWrap] = useState(false);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const copyTimer = useRef<number | null>(null);
  const reqGen = useRef(0); // 切换素材/分页在途请求作废,防止旧响应覆盖新预览

  useEffect(() => () => {
    if (copyTimer.current) window.clearTimeout(copyTimer.current);
  }, []);

  useEffect(() => {
    let cancel = false;
    reqGen.current += 1; // 作废旧的分页请求
    setBusy(false);
    setStatus("loading");
    setData(null);
    setText("");
    setNextOffset(null);
    setMediaErr(false);
    setWrap(false);
    setCopied(false);
    hubPreview(detail.id)
      .then((r) => {
        if (cancel) return;
        setData(r);
        setText(r.text || "");
        setNextOffset(r.next_offset ?? null);
        setStatus("ok");
      })
      .catch(() => { if (!cancel) setStatus("err"); });
    return () => { cancel = true; };
  }, [detail.id]);

  const copyText = () => {
    void navigator.clipboard?.writeText(text);
    setCopied(true);
    if (copyTimer.current) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
  };

  const loadMore = async () => {
    if (nextOffset == null || busy || !data) return;
    const gen = reqGen.current;
    const id = detail.id;
    setBusy(true);
    try {
      const r = await hubPreview(id, nextOffset);
      if (gen !== reqGen.current) return; // 期间切换了素材:丢弃旧响应
      setText((t) => (gen !== reqGen.current ? t : t + (r.text || "")));
      setNextOffset(r.next_offset ?? null);
      setData((d) => (d ? { ...d, offset: r.offset, truncated: r.truncated } : d));
    } catch {
      // 已显示的内容保留;不把整块预览替换成错误态
    } finally {
      if (gen === reqGen.current) setBusy(false);
    }
  };

  if (status === "loading") {
    return <div className="hub-pv hub-pv-msg">{copy.pvLoading}</div>;
  }
  if (status === "err" || !data) {
    return (
      <div className="hub-pv hub-pv-msg err">
        {copy.pvError} · <OpenLink id={detail.id} copy={copy} />
      </div>
    );
  }

  const url = hubFileUrl(detail.id);
  const ext = (data.ext || detail.ext || "?").replace(/^\./, "").toUpperCase();
  const reason = reasonText(data.reason, copy);
  const note = reason ? <div className="hub-pv-note">{reason}</div> : null;
  const fallback = (title: string) => (
    <div className="hub-pv-fallback">
      <span className="hub-pv-ext">{ext}</span>
      <strong>{title}</strong>
      {note}
      <p className="muted">{copy.pvOpenHint}</p>
      <OpenLink id={detail.id} copy={copy} />
    </div>
  );

  if (data.mode === "image") {
    if (!data.native || mediaErr) {
      return (
        <div className="hub-pv hub-pv-stack">
          {detail.thumb && !mediaErr ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img className="hub-pv-thumb" src={hubThumbUrl(detail.id)} alt="" />
          ) : null}
          {fallback(mediaErr ? copy.pvMediaErr : copy.pvTitle)}
        </div>
      );
    }
    return (
      <div className="hub-pv hub-pv-stack">
        <div className="hub-prev checker">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={url}
            alt=""
            className="zoomable"
            title={copy.zoomTitle}
            decoding="async"
            onClick={() => onZoom?.()}
            onError={() => setMediaErr(true)}
          />
        </div>
        {note}
      </div>
    );
  }

  if (data.mode === "video") {
    return (
      <div className="hub-pv hub-pv-stack">
        {mediaErr ? fallback(copy.pvMediaErr) : (
          <div className="hub-prev">
            <video
              key={detail.id}
              src={url}
              controls
              playsInline
              preload="metadata"
              muted={(detail.tags || "").includes("has_audio:0")}
              poster={detail.thumb ? hubThumbUrl(detail.id) : undefined}
              onError={() => setMediaErr(true)}
              onLoadedMetadata={(e) => {
                if (detail.hit_t) e.currentTarget.currentTime = detail.hit_t;
              }}
            />
          </div>
        )}
        {note}
      </div>
    );
  }

  if (data.mode === "audio") {
    return (
      <div className="hub-pv hub-pv-stack">
        {mediaErr ? fallback(copy.pvMediaErr) : (
          <div className="hub-prev">
            <audio src={url} controls preload="metadata" onError={() => setMediaErr(true)} />
          </div>
        )}
        {note}
      </div>
    );
  }

  if (data.mode === "pdf") {
    return (
      <div className="hub-pv hub-pv-stack">
        {/* 不给 iframe 加 sandbox:MDN 明确 sandbox 会挡住浏览器内置 PDF 查看器,
            该查看器本身已对可执行内容做沙箱;另给外链作为无查看器时的兜底。 */}
        <iframe className="hub-pv-pdf" src={url} title={copy.pvTitle} />
        <div className="hub-pv-note">{copy.pvPdfFallback} · <OpenLink id={detail.id} copy={copy} /></div>
      </div>
    );
  }

  if (data.mode === "font") {
    const fam = `hub-font-${detail.id.replace(/[^a-zA-Z0-9_-]/g, "")}`;
    const fontExt = (data.ext || "").toLowerCase();
    const fmt = fontExt === "woff2" ? "woff2" : fontExt === "woff" ? "woff"
      : fontExt === "otf" ? "opentype" : "truetype";
    return (
      <div className="hub-pv hub-pv-stack">
        <style>{`@font-face{font-family:"${fam}";src:url("${url}") format("${fmt}");font-display:swap;}`}</style>
        <div className="hub-pv-font" style={{ fontFamily: `"${fam}", serif` }}>
          <div className="hub-pv-font-sm">{copy.pvFontSample} · {fontExt.toUpperCase()}</div>
          <div className="hub-pv-font-lg">Aa 中文繁體 Español 1234567890</div>
        </div>
        {note}
      </div>
    );
  }

  const isText = data.mode === "text" || data.mode === "code" || data.mode === "subtitle" || data.mode === "hex";
  const isEmpty = isText && !text.trim();
  const lineCount = text ? text.split("\n").length : 0;

  return (
    <div className="hub-pv">
      <div className="hub-pv-bar">
        <span className="hub-pv-tag">{data.mode === "hex" ? copy.pvHexTitle : copy.pvTitle}</span>
        {data.encoding && data.mode !== "hex" ? <span className="muted">{copy.pvEncoding}: {data.encoding}</span> : null}
        {isText ? (
          <span className="muted">{text.length.toLocaleString()} {copy.pvChars} · {lineCount.toLocaleString()} {copy.pvLines}</span>
        ) : null}
        <span className="sp" />
        <button type="button" className={wrap ? "on" : ""} aria-pressed={wrap} onClick={() => setWrap((v) => !v)}>
          {copy.pvWrap}
        </button>
        {isText ? (
          <button type="button" onClick={copyText}>{copied ? `✓ ${copy.copied}` : copy.subsCopyText}</button>
        ) : null}
      </div>
      {note}
      {isEmpty ? <div className="hub-pv-msg">{copy.pvEmpty}</div> : null}
      {data.mode === "table" ? (
        <div className="hub-pv-body">
          <table className="hub-pv-table">
            {data.rows.length ? <thead><tr>{data.rows[0].map((cell, i) => <th key={i}>{cell}</th>)}</tr></thead> : null}
            <tbody>
              {data.rows.slice(1).map((row, i) => (
                <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>
              ))}
            </tbody>
          </table>
          {data.rows.length ? (
            <div className="hub-pv-note">
              {fillCopy(copy.pvTableHint, { r: data.rows.length, c: data.rows[0].length })}
            </div>
          ) : null}
        </div>
      ) : null}
      {data.mode === "sections" ? (
        <div className="hub-pv-body">
          {data.sections.map((s, i) => (
            <section className="hub-pv-section" key={`${s.title}-${i}`}>
              <h4>#{s.title}</h4>
              <pre className={wrap ? "wrap" : ""}>{s.text}</pre>
            </section>
          ))}
        </div>
      ) : null}
      {isText ? <pre className={`hub-pv-pre${wrap ? " wrap" : ""}`}>{text}</pre> : null}
      {data.mode === "hex" && !isEmpty ? (
        <div className="hub-pv-note">{copy.pvOpenHint} · <OpenLink id={detail.id} copy={copy} /></div>
      ) : null}
      {data.truncated && isText ? (
        <div className="hub-pv-foot">
          <span className="muted">{fillCopy(copy.pvTruncated, { n: text.length })}</span>
          {nextOffset != null ? (
            <button type="button" disabled={busy} onClick={() => void loadMore()}>
              {busy ? copy.pvLoading : copy.pvMore}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
