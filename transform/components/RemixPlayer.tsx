"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type OverlayCue = {
  start: number;
  end: number;
  kind?: string;
  text: string;
};

function activeCue(cues: OverlayCue[], t: number): OverlayCue | null {
  for (const cue of cues) {
    if (t >= cue.start && t < cue.end && cue.text.trim()) return cue;
  }
  return null;
}

export function RemixPlayer({
  src,
  cuesUrl,
  label,
}: {
  src: string;
  cuesUrl?: string;
  label: string;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [cues, setCues] = useState<OverlayCue[]>([]);
  const [now, setNow] = useState(0);
  const cue = activeCue(cues, now);

  useEffect(() => {
    if (!cuesUrl) {
      setCues([]);
      return;
    }
    let gone = false;
    fetch(cuesUrl)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (gone || !data || !Array.isArray(data.cues)) return;
        setCues(
          data.cues
            .map((row: OverlayCue) => ({
              start: Number(row.start) || 0,
              end: Number(row.end) || 0,
              kind: String(row.kind || "caption"),
              text: String(row.text || "").trim(),
            }))
            .filter((row: OverlayCue) => row.end > row.start && row.text)
        );
      })
      .catch(() => {
        if (!gone) setCues([]);
      });
    return () => {
      gone = true;
    };
  }, [cuesUrl]);

  const syncTime = useCallback(() => {
    const el = videoRef.current;
    if (!el) return;
    setNow(el.currentTime || 0);
  }, []);

  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;
    let raf = 0;
    const tick = () => {
      if (!el.paused && !el.ended) setNow(el.currentTime || 0);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    el.addEventListener("timeupdate", syncTime);
    el.addEventListener("seeked", syncTime);
    return () => {
      cancelAnimationFrame(raf);
      el.removeEventListener("timeupdate", syncTime);
      el.removeEventListener("seeked", syncTime);
    };
  }, [syncTime, src]);

  return (
    <figure className="remix-player">
      <figcaption className="remix-player-label">{label}</figcaption>
      <div className="remix-stage">
        <video
          ref={videoRef}
          className="remix-stage-video"
          src={src}
          controls
          playsInline
          preload="metadata"
        />
        {cue ? (
          <div
            className={
              cue.kind === "title" ? "remix-overlay is-title" : "remix-overlay is-caption"
            }
            aria-live="polite"
          >
            {cue.text}
          </div>
        ) : null}
      </div>
    </figure>
  );
}
