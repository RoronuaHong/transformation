"use client";

import { useEffect, useId, useRef, useState } from "react";

export function NoteFrame({
  src,
  viewLabel,
  closeLabel,
}: {
  src: string;
  viewLabel: string;
  closeLabel: string;
}) {
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
      <button
        type="button"
        className="note-frame-trigger"
        onClick={() => setOpen(true)}
        aria-label={viewLabel}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img className="note-frame" src={src} alt="" loading="lazy" />
        <span className="note-frame-cue">{viewLabel}</span>
      </button>
      {open ? (
        <div className="frame-modal-root" role="dialog" aria-modal="true" aria-labelledby={titleId}>
          <button
            type="button"
            className="notes-modal-backdrop"
            aria-label={closeLabel}
            onClick={() => setOpen(false)}
          />
          <div className="frame-modal-panel">
            <header className="notes-modal-head">
              <h2 id={titleId} className="section-title">
                {viewLabel}
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
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img className="frame-modal-img" src={src} alt="" />
          </div>
        </div>
      ) : null}
    </>
  );
}
