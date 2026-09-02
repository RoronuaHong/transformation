"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { DownloadNotes, hasNotesDownload, type NotesDownloadPayload } from "@/components/DownloadNotes";
import { NoteList } from "@/components/NoteList";
import type { ArticleCopy } from "@/lib/copy";
import type { KeyPoint } from "@/lib/note-points";

export function NotesModal({
  chrome,
  overview,
  focuses,
  keyPoints,
  hardPoints,
  clipLabel,
  downloadFilename,
  downloadNotes,
}: {
  chrome: ArticleCopy;
  overview: string;
  focuses: KeyPoint[];
  keyPoints: KeyPoint[];
  hardPoints: KeyPoint[];
  clipLabel?: string;
  downloadFilename?: string;
  downloadNotes?: NotesDownloadPayload;
}) {
  const [open, setOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const hasBody =
    Boolean(overview.trim()) ||
    focuses.length > 0 ||
    keyPoints.length > 0 ||
    hardPoints.length > 0;

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!open) return;
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
    };
  }, [open]);

  if (!hasBody) return null;

  const canDownload = Boolean(downloadFilename && downloadNotes && hasNotesDownload(downloadNotes));

  const dialog =
    open && mounted
      ? createPortal(
          <div
            className="notes-modal-root"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
          >
            <button
              type="button"
              className="notes-modal-backdrop"
              aria-label={chrome.closeNotes}
              onClick={() => setOpen(false)}
            />
            <div className="notes-modal-panel">
              <div className="notes-modal-handle" aria-hidden="true" />
              <header className="notes-modal-head">
                <h2 id={titleId} className="section-title">
                  {chrome.notes}
                </h2>
                <button
                  ref={closeRef}
                  type="button"
                  className="notes-modal-close"
                  onClick={() => setOpen(false)}
                >
                  {chrome.closeNotes}
                </button>
              </header>
              <div className="notes-modal-body">
                {overview.trim() ? (
                  <section className="notes-overview" aria-labelledby={`${titleId}-ov`}>
                    <h3 id={`${titleId}-ov`} className="section-title">
                      {chrome.overview}
                    </h3>
                    <p className="notes-overview-text">{overview}</p>
                  </section>
                ) : null}
                <NoteList
                  heading={chrome.focuses}
                  items={focuses}
                  clipLabel={clipLabel}
                  viewFrameLabel={chrome.viewFrame}
                  closeLabel={chrome.closeNotes}
                />
                <NoteList
                  heading={chrome.keyPoints}
                  items={keyPoints}
                  clipLabel={clipLabel}
                  viewFrameLabel={chrome.viewFrame}
                  closeLabel={chrome.closeNotes}
                />
                <NoteList
                  heading={chrome.hardPoints}
                  items={hardPoints}
                  clipLabel={clipLabel}
                  viewFrameLabel={chrome.viewFrame}
                  closeLabel={chrome.closeNotes}
                />
              </div>
              {canDownload && downloadFilename && downloadNotes ? (
                <div className="notes-modal-foot">
                  <DownloadNotes
                    label={chrome.downloadNotes}
                    filename={downloadFilename}
                    notes={downloadNotes}
                    headings={{
                      overview: chrome.overview,
                      focuses: chrome.focuses,
                      keyPoints: chrome.keyPoints,
                      hardPoints: chrome.hardPoints,
                    }}
                    className="watch-btn secondary notes-modal-download"
                  />
                </div>
              ) : null}
            </div>
          </div>,
          document.body,
        )
      : null;

  return (
    <>
      <button type="button" className="watch-btn notes-cta" onClick={() => setOpen(true)}>
        {chrome.openNotes}
      </button>
      {dialog}
    </>
  );
}
