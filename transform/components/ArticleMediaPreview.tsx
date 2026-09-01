"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { KeyPoint } from "@/lib/note-points";

type MediaKind = "gif" | "clip";

type MediaItem = {
  id: string;
  kind: MediaKind;
  title: string;
  detail: string;
  src: string;
};

function buildItems(
  gifs: KeyPoint[],
  clips: KeyPoint[],
  gifLabel: string,
  clipLabel: string
): { gifItems: MediaItem[]; clipItems: MediaItem[] } {
  const gifItems: MediaItem[] = [];
  const clipItems: MediaItem[] = [];

  gifs.forEach((item, i) => {
    if (!item.image) return;
    gifItems.push({
      id: `gif-${item.image}-${i}`,
      kind: "gif",
      title: item.title || gifLabel,
      detail: item.detail || "",
      src: item.image,
    });
  });

  clips.forEach((item, i) => {
    if (!item.clip) return;
    clipItems.push({
      id: `clip-${item.clip}-${i}`,
      kind: "clip",
      title: item.title || `${clipLabel} ${i + 1}`,
      detail: item.detail || "",
      src: item.clip,
    });
  });

  return { gifItems, clipItems };
}

export function ArticleMediaPreview({
  gifs,
  clips,
  title,
  gifKind,
  clipTabLabel,
  clipLabel,
  gifLabel,
  previewLabel,
  closeLabel,
}: {
  gifs: KeyPoint[];
  clips: KeyPoint[];
  title: string;
  gifKind: string;
  clipTabLabel: string;
  clipLabel: string;
  gifLabel: string;
  previewLabel: string;
  closeLabel: string;
}) {
  const { gifItems, clipItems } = useMemo(
    () => buildItems(gifs, clips, gifLabel, clipLabel),
    [gifs, clips, gifLabel, clipLabel]
  );

  const hasGifs = gifItems.length > 0;
  const hasClips = clipItems.length > 0;
  const showTabs = hasGifs && hasClips;

  const [tab, setTab] = useState<MediaKind>(() => (hasGifs ? "gif" : "clip"));
  const [open, setOpen] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [mounted, setMounted] = useState(false);
  const titleId = useId();
  const panelId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  const visibleItems = tab === "gif" ? gifItems : clipItems;
  const active = visibleItems.find((item) => item.id === activeId) ?? null;

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (hasGifs && !hasClips) setTab("gif");
    else if (!hasGifs && hasClips) setTab("clip");
  }, [hasGifs, hasClips]);

  useEffect(() => {
    if (!open) {
      setActiveId(null);
      return;
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
      videoRef.current?.pause();
    };
  }, [open]);

  if (!hasGifs && !hasClips) return null;

  const openItem = (id: string) => {
    setActiveId(id);
    setOpen(true);
  };

  const switchTab = (next: MediaKind) => {
    if (next === tab) return;
    setTab(next);
    setOpen(false);
    setActiveId(null);
  };

  const dialogTitle = active ? active.title : tab === "gif" ? gifKind : clipTabLabel;

  const dialog =
    open && mounted && active
      ? createPortal(
          <div
            className="notes-modal-root article-media-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
          >
            <button
              type="button"
              className="notes-modal-backdrop"
              aria-label={closeLabel}
              onClick={() => setOpen(false)}
            />
            <div className="notes-modal-panel">
              <div className="notes-modal-handle" aria-hidden="true" />
              <header className="notes-modal-head">
                <div className="article-media-head-lead">
                  <h2 id={titleId} className="section-title">
                    {dialogTitle}
                  </h2>
                </div>
                <button
                  ref={closeRef}
                  type="button"
                  className="notes-modal-close"
                  onClick={() => setOpen(false)}
                >
                  {closeLabel}
                </button>
              </header>
              <div className="notes-modal-body">
                <figure className="article-media-viewer">
                  {active.kind === "gif" ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      className="article-media-viewer-asset"
                      src={active.src}
                      alt={active.title}
                    />
                  ) : (
                    <video
                      ref={videoRef}
                      className="article-media-viewer-asset is-clip"
                      src={active.src}
                      controls
                      playsInline
                      autoPlay
                      preload="metadata"
                      aria-label={`${previewLabel} · ${active.title}`}
                    />
                  )}
                  {active.detail ? (
                    <figcaption className="article-media-viewer-cap">
                      {active.detail}
                    </figcaption>
                  ) : null}
                </figure>
              </div>
            </div>
          </div>,
          document.body
        )
      : null;

  return (
    <>
      <section className="article-media-preview" aria-label={title}>
        {showTabs ? (
          <div className="article-media-tabs" role="tablist" aria-label={title}>
            <button
              type="button"
              role="tab"
              id={`${panelId}-gif`}
              className={`article-media-tab${tab === "gif" ? " active" : ""}`}
              aria-selected={tab === "gif"}
              aria-controls={`${panelId}-panel`}
              onClick={() => switchTab("gif")}
            >
              {gifKind}
              <span className="article-media-tab-count">{gifItems.length}</span>
            </button>
            <button
              type="button"
              role="tab"
              id={`${panelId}-clip`}
              className={`article-media-tab${tab === "clip" ? " active" : ""}`}
              aria-selected={tab === "clip"}
              aria-controls={`${panelId}-panel`}
              onClick={() => switchTab("clip")}
            >
              {clipTabLabel}
              <span className="article-media-tab-count">{clipItems.length}</span>
            </button>
          </div>
        ) : null}

        <div
          id={`${panelId}-panel`}
          role={showTabs ? "tabpanel" : undefined}
          aria-label={showTabs ? undefined : tab === "gif" ? gifKind : clipTabLabel}
          aria-labelledby={
            showTabs ? (tab === "gif" ? `${panelId}-gif` : `${panelId}-clip`) : undefined
          }
          className="article-media-panel"
        >
          <ol className="article-media-list">
            {visibleItems.map((item, i) => (
              <li key={item.id}>
                <button
                  type="button"
                  className="article-media-row"
                  onClick={() => openItem(item.id)}
                  aria-label={`${gifLabel} · ${item.title}`}
                >
                  <span className="article-media-idx" aria-hidden="true">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className="article-media-thumb" aria-hidden="true">
                    {item.kind === "gif" ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={item.src} alt="" loading="lazy" />
                    ) : (
                      <span className="article-media-play">▶</span>
                    )}
                  </span>
                  <span className="article-media-row-copy">
                    <span className="article-media-kind">
                      {item.kind === "gif" ? gifKind : clipLabel}
                    </span>
                    <strong>{item.title}</strong>
                    {item.detail ? (
                      <span className="article-media-row-detail">{item.detail}</span>
                    ) : null}
                  </span>
                </button>
              </li>
            ))}
          </ol>
        </div>
      </section>
      {dialog}
    </>
  );
}
