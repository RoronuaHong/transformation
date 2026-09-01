import type { Locale } from "./locales";

export type UrlPlatform = "douyin" | "bilibili" | "youtube" | "hls";

export function detectPlatform(url: string): UrlPlatform | null {
  const u = url.trim().toLowerCase();
  if (u.includes(".m3u8")) {
    return "hls";
  }
  if (u.includes("douyin.com") || u.includes("iesdouyin.com") || u.includes("v.douyin.com")) {
    return "douyin";
  }
  if (u.includes("bilibili.com") || u.includes("b23.tv")) {
    return "bilibili";
  }
  if (u.includes("youtube.com") || u.includes("youtu.be")) {
    return "youtube";
  }
  return null;
}

/** Split textarea into unique non-empty lines (client-side preview). */
export function parseUrlLines(text: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const line of text.split(/\r?\n/)) {
    const u = line.trim();
    if (u.length < 8) continue;
    const key = u.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(u);
  }
  return out;
}

export type PlatformProbe = {
  count: number;
  required: boolean;
  soft?: boolean;
  present: boolean;
  ok: boolean;
  label: string;
};

export type UrlProbeResult = {
  ok?: boolean;
  valid_count?: number;
  invalid_count?: number;
  counts?: Record<string, number>;
  platforms?: Record<string, PlatformProbe>;
  block_submit?: boolean;
  message?: string;
  cookies_local?: boolean;
  duration_sec?: number;
  cached?: boolean;
  job_status?: string;
  langs?: string;
  frame_opts?: {
    frames?: string;
    clips?: unknown[];
    gif_ranges?: unknown[];
  };
};
