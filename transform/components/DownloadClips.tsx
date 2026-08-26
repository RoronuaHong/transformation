"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  buildSrt,
  rangeVideoClips,
  sliceSegmentCues,
  type ArticleCue,
  type KeyPoint,
} from "@/lib/note-points";
import type { Locale } from "@/lib/locales";

export function DownloadClips({
  label,
  itemLabel,
  emptyLabel,
  closeLabel,
  downloadLabel,
  previewLabel,
  segmentCaptionsLabel,
  downloadSegmentCaptionsLabel,
  locale,
  cues,
  points,
}: {
  label: string;
  itemLabel: string;
  emptyLabel: string;
  closeLabel: string;
  downloadLabel: string;
  previewLabel: string;
  segmentCaptionsLabel: string;
  downloadSegmentCaptionsLabel: string;
  locale: Locale;
  cues: ArticleCue[];
  points: KeyPoint[];
}) {
  const clips = useMemo(
    () =>
      rangeVideoClips(points).map((p, i) => ({
        title: p.title,
        href: p.clip as string,
        detail: p.detail,
        index: i + 1,
        cueStart: p.cue_start,
        cueEnd: p.cue_end,
      })),
    [points]
  );

  const [open, setOpen] = useState(false);
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    closeRef.current?.focus();
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open]);

  return (
    <>
      <button type="button" className="watch-btn secondary clips-dl-btn" onClick={() => setOpen(true)}>
        {label}
      </button>
      {open ? (
        <div className="clips-modal-root" role="dialog" aria-modal="true" aria-labelledby={titleId}>
          <button
            type="button"
            className="notes-modal-backdrop"
            aria-label={closeLabel}
            onClick={() => setOpen(false)}
          />
          <div className="clips-modal-panel">
            <header className="notes-modal-head">
              <h2 id={titleId} className="section-title">
                {label}
              </h2>
              <button
                ref={closeRef}
                type="button"
                className="notes-modal-close"
                onClick={() => setOpen(false)}
              >
                {closeLabel}
              </button>
            </header>
            {clips.length ? (
              <ul className="clips-list">
                {clips.map((c) => {
                  const segCues = sliceSegmentCues(cues, c.cueStart, c.cueEnd, locale);
                  const srtBody = buildSrt(segCues);
                  const srtName = `clip-${String(c.index).padStart(2, "0")}-${locale}.srt`;
                  return (
                    <li key={c.href} className="clips-list-item">
                      <div className="clips-list-meta">
                        <span className="clips-list-idx">
                          {itemLabel} {String(c.index).padStart(2, "0")}
                          {c.detail ? ` · ${c.detail}` : ""}
                        </span>
                        <span className="clips-list-title">{c.title}</span>
                      </div>
                      <video
                        className="clips-preview"
                        src={c.href}
                        controls
                        playsInline
                        preload="metadata"
                        aria-label={`${previewLabel} ${c.index}`}
                      />
                      {segCues.length ? (
                        <div className="clips-segment-captions">
                          <p className="clips-segment-captions-title">{segmentCaptionsLabel}</p>
                          <ol className="clips-segment-cue-list">
                            {segCues.map((line, i) => (
                              <li key={`${c.href}-cue-${i}`}>
                                <span className="clips-cue-time">
                                  {line.start.split(",")[0]}
                                </span>
                                <span>{line.text}</span>
                              </li>
                            ))}
                          </ol>
                        </div>
                      ) : null}
                      <div className="clips-list-actions">
                        <a className="clips-list-link" href={c.href} download>
                          {downloadLabel}
                        </a>
                        {srtBody ? (
                          <a
                            className="clips-list-link secondary"
                            href={`data:text/plain;charset=utf-8,${encodeURIComponent(srtBody)}`}
                            download={srtName}
                          >
                            {downloadSegmentCaptionsLabel}
                          </a>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="clips-empty">{emptyLabel}</p>
            )}
          </div>
        </div>
      ) : null}
    </>
  );
}
