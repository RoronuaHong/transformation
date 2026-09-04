"use client";

import { useEffect, useRef, useState } from "react";

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

function cueKey(cue: OverlayCue | null): string {
  if (!cue) return "";
  return `${cue.kind}:${cue.start}:${cue.text}`;
}

function kindFromTrackCue(cue: TextTrackCue): "title" | "caption" {
  const id = String(cue.id || "").toLowerCase();
  return id.includes("title") ? "title" : "caption";
}

function fromActiveCues(track: TextTrack): OverlayCue | null {
  const list = track.activeCues;
  if (!list || list.length === 0) return null;
  let chosen: OverlayCue | null = null;
  for (let i = 0; i < list.length; i += 1) {
    const row = list[i];
    const text = "text" in row ? String((row as VTTCue).text || "").trim() : "";
    if (!text) continue;
    const next: OverlayCue = {
      start: row.startTime,
      end: row.endTime,
      kind: kindFromTrackCue(row),
      text,
    };
    if (next.kind === "caption") return next;
    chosen = next;
  }
  return chosen;
}

export function RemixPlayer({
  src,
  cuesUrl,
  vttUrl,
  label,
}: {
  src: string;
  cuesUrl?: string;
  vttUrl?: string;
  label: string;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const cuesRef = useRef<OverlayCue[]>([]);
  const paintedRef = useRef("");
  const [cues, setCues] = useState<OverlayCue[]>([]);
  const [cue, setCue] = useState<OverlayCue | null>(null);

  useEffect(() => {
    cuesRef.current = cues;
    paintedRef.current = "";
  }, [cues]);

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

  useEffect(() => {
    const el = videoRef.current;
    if (!el) return;

    const paintCue = (next: OverlayCue | null) => {
      const key = cueKey(next);
      if (key === paintedRef.current) return;
      paintedRef.current = key;
      setCue(next);
    };

    const paintFromTrack = (track: TextTrack) => {
      paintCue(fromActiveCues(track));
    };

    const paintFromJson = () => {
      paintCue(activeCue(cuesRef.current, el.currentTime || 0));
    };

    const bindTrack = (track: TextTrack) => {
      track.mode = "hidden";
      const onCue = () => paintFromTrack(track);
      track.addEventListener("cuechange", onCue);
      onCue();
      return () => track.removeEventListener("cuechange", onCue);
    };

    let unbindTrack: (() => void) | undefined;
    const attach = () => {
      const track = el.textTracks[0];
      if (!track) return;
      unbindTrack?.();
      unbindTrack = bindTrack(track);
    };

    el.textTracks.addEventListener("addtrack", attach);
    attach();

    const onTime = () => {
      const track = el.textTracks[0];
      if (vttUrl && track && track.cues && track.cues.length > 0) {
        paintFromTrack(track);
        return;
      }
      paintFromJson();
    };

    let raf = 0;
    const tick = () => {
      if (!el.paused && !el.ended) onTime();
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    el.addEventListener("timeupdate", onTime);
    el.addEventListener("seeked", onTime);
    el.addEventListener("pause", onTime);
    el.addEventListener("play", onTime);
    onTime();

    return () => {
      if (raf) cancelAnimationFrame(raf);
      unbindTrack?.();
      el.textTracks.removeEventListener("addtrack", attach);
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("seeked", onTime);
      el.removeEventListener("pause", onTime);
      el.removeEventListener("play", onTime);
    };
  }, [src, cues, vttUrl]);

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
        >
          {vttUrl ? (
            <track kind="captions" src={vttUrl} srcLang="zh" default />
          ) : null}
        </video>
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
