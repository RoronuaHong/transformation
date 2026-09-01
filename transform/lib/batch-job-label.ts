import type { Locale } from "./locales";
import { detectPlatform, type UrlPlatform } from "./try-urls";

type PlatformLabelRow = { en: string; zh?: string; "zh-Hant"?: string };

const PLATFORM_LABEL: Record<UrlPlatform, PlatformLabelRow> = {
  douyin: { en: "Douyin", zh: "抖音", "zh-Hant": "抖音" },
  bilibili: { en: "Bilibili", zh: "B 站", "zh-Hant": "B 站" },
  youtube: { en: "YouTube", zh: "YouTube", "zh-Hant": "YouTube" },
  hls: { en: "HLS", zh: "HLS", "zh-Hant": "HLS" },
};

function platformLabel(platform: UrlPlatform, locale: Locale): string {
  const row = PLATFORM_LABEL[platform];
  if (locale === "zh" && row.zh) return row.zh;
  if (locale === "zh-Hant" && row["zh-Hant"]) return row["zh-Hant"];
  return row.en;
}

function extractVideoId(url: string, platform: UrlPlatform): string | null {
  const u = url.trim();
  if (platform === "bilibili") {
    const bv = u.match(/BV[\w]+/i);
    if (bv) return bv[0].toUpperCase();
    const av = u.match(/av(\d+)/i);
    if (av) return `av${av[1]}`;
    return null;
  }
  if (platform === "youtube") {
    try {
      const parsed = new URL(u);
      if (parsed.hostname.includes("youtu.be")) {
        return parsed.pathname.replace(/^\//, "").split("/")[0] || null;
      }
      return parsed.searchParams.get("v");
    } catch {
      return null;
    }
  }
  if (platform === "douyin") {
    const m = u.match(/video\/(\d+)/) || u.match(/modal_id=(\d+)/);
    return m?.[1] ?? null;
  }
  if (platform === "hls") {
    try {
      const parsed = new URL(u);
      const leaf = parsed.pathname.split("/").filter(Boolean).pop();
      if (leaf?.includes(".m3u8")) return leaf;
      return parsed.hostname || null;
    } catch {
      return null;
    }
  }
  return null;
}

function truncateMiddle(text: string, max = 42): string {
  if (text.length <= max) return text;
  const head = Math.ceil((max - 1) / 2);
  const tail = Math.floor((max - 1) / 2);
  return `${text.slice(0, head)}…${text.slice(-tail)}`;
}

/** Compact label for batch job rows (URL, try:title, or filename). */
export function formatBatchJobLabel(raw: string, locale: Locale = "zh"): string {
  const text = raw.trim();
  if (!text) return "";

  const tryMatch = /^try:([^:]+):(.+)$/.exec(text);
  if (tryMatch) {
    const plat = tryMatch[1] as UrlPlatform;
    const id = tryMatch[2];
    if (PLATFORM_LABEL[plat]) {
      return `${platformLabel(plat, locale)} · ${id}`;
    }
    return truncateMiddle(text);
  }

  const platform = detectPlatform(text);
  if (platform) {
    const id = extractVideoId(text, platform);
    if (id) return `${platformLabel(platform, locale)} · ${id}`;
    return truncateMiddle(text);
  }

  if (text.startsWith("http://") || text.startsWith("https://")) {
    return truncateMiddle(text);
  }

  return text.length > 48 ? truncateMiddle(text, 48) : text;
}
