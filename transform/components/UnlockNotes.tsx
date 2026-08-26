"use client";

import { useEffect, useState } from "react";
import type { ArticleCopy } from "@/lib/copy";

const AD_MS = 5000;

export function UnlockNotes({
  storageKey,
  copy,
  children,
}: {
  storageKey: string;
  copy: Pick<ArticleCopy, "unlock" | "unlocking" | "lockedHint">;
  children: React.ReactNode;
}) {
  const [unlocked, setUnlocked] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [left, setLeft] = useState(Math.ceil(AD_MS / 1000));

  useEffect(() => {
    try {
      if (sessionStorage.getItem(storageKey) === "1") setUnlocked(true);
    } catch {
      /* private mode */
    }
  }, [storageKey]);

  useEffect(() => {
    if (!playing) return;
    const start = Date.now();
    const id = window.setInterval(() => {
      const remain = Math.max(0, Math.ceil((AD_MS - (Date.now() - start)) / 1000));
      setLeft(remain);
      if (remain <= 0) {
        window.clearInterval(id);
        try {
          sessionStorage.setItem(storageKey, "1");
        } catch {
          /* ignore */
        }
        setUnlocked(true);
        setPlaying(false);
      }
    }, 200);
    return () => window.clearInterval(id);
  }, [playing, storageKey]);

  if (unlocked) return <>{children}</>;

  return (
    <div className="gate">
      <div className="gate-preview" inert aria-hidden="true">
        {children}
      </div>
      <div className="gate-mask">
        {playing ? (
          <p className="gate-status" role="status">
            {copy.unlocking} · {left}s
          </p>
        ) : (
          <div className="gate-panel">
            <p>{copy.lockedHint}</p>
            <button type="button" className="unlock-btn" onClick={() => setPlaying(true)}>
              {copy.unlock}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
