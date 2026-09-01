/** Client-safe note point helpers (no Node fs). */

import type { Locale } from "./locales";

export type KeyPoint = {
  title: string;
  detail: string;
  image?: string;
  clip?: string;
  start_sec?: number;
  end_sec?: number;
  cue_start?: number;
  cue_end?: number;
};

export type ArticleCue = {
  start: string;
  end: string;
  text: Record<string, string>;
};

/** Custom try-form GIF ranges (`/frames/.../gif_range_XX.gif`). */
export function gifRangeFrames(points: KeyPoint[]): KeyPoint[] {
  return points.filter((kp) => (kp.image || "").includes("/gif_range_"));
}

/** Custom try-form ranges only (`/clips/.../range_XX.mp4`), not subtitle-aligned GIFs. */
export function isRangeVideoClip(kp: KeyPoint): boolean {
  const clip = (kp.clip || "").trim();
  return clip.includes("/range_");
}

export function rangeVideoClips(points: KeyPoint[]): KeyPoint[] {
  return points.filter(isRangeVideoClip);
}

export function sliceSegmentCues(
  cues: ArticleCue[],
  cueStart: number | undefined,
  cueEnd: number | undefined,
  locale: Locale
): { start: string; end: string; text: string }[] {
  if (cueStart == null || cueEnd == null || !cues.length) return [];
  const i0 = Math.max(0, Math.min(cueStart, cues.length - 1));
  const i1 = Math.max(i0, Math.min(cueEnd, cues.length - 1));
  const out: { start: string; end: string; text: string }[] = [];
  for (let i = i0; i <= i1; i++) {
    const c = cues[i];
    const text = (c.text[locale] || c.text.zh || Object.values(c.text)[0] || "").trim();
    if (!text) continue;
    out.push({ start: c.start, end: c.end, text });
  }
  return out;
}

export function buildSrt(lines: { start: string; end: string; text: string }[]): string {
  const body: string[] = [];
  lines.forEach((line, i) => {
    body.push(String(i + 1));
    body.push(`${line.start} --> ${line.end}`);
    body.push(line.text);
    body.push("");
  });
  return body.join("\n").trim() + (body.length ? "\n" : "");
}
