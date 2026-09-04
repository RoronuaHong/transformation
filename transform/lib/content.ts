import { readFileSync, existsSync } from "fs";
import path from "path";
import type { Locale } from "./locales";
import { locales, recordAll } from "./locales";
import type { KeyPoint } from "./note-points";

export type { KeyPoint } from "./note-points";
export { isRangeVideoClip, rangeVideoClips } from "./note-points";

export type SampleArticle = {
  slug: string;
  topic: string;
  platform: "youtube" | "bilibili" | "douyin" | "upload";
  videoId: string;
  embedUrl: string;
  sourceUrl: string;
  titles: Record<Locale, string>;
  /** Short teaser (one_liner) shown under the title. */
  summaries: Record<Locale, string>;
  /** Longer 总体总结 shown inside the notes modal. */
  overviews: Record<Locale, string>;
  keyPointsIntro: Record<Locale, string>;
  focuses: Record<Locale, KeyPoint[]>;
  keyPoints: Record<Locale, KeyPoint[]>;
  hardPoints: Record<Locale, KeyPoint[]>;
  cues: { start: string; end: string; text: Record<Locale, string> }[];
};

/** Rewarded-ad gate for full notes. Off for the webpage preview. */
export const NOTES_AD_GATE = false;

export const FREE_POINT_COUNT = 3;

function emptyNoteMap(): Record<Locale, KeyPoint[]> {
  const out = {} as Record<Locale, KeyPoint[]>;
  for (const loc of locales) out[loc] = [];
  return out;
}

function notesFor(
  zh: KeyPoint[],
  en: KeyPoint[],
  hant: KeyPoint[] = zh
): Record<Locale, KeyPoint[]> {
  const out = emptyNoteMap();
  for (const loc of locales) {
    if (loc === "zh") out[loc] = zh;
    else if (loc === "zh-Hant") out[loc] = hant;
    else out[loc] = en;
  }
  return out;
}

/** Placeholder when content/articles.json is missing or empty. */
export const sampleArticle: SampleArticle = {
  slug: "sample-video-notes",
  topic: "general",
  platform: "youtube",
  videoId: "sample",
  embedUrl: "",
  sourceUrl: "https://www.youtube.com",
  titles: recordAll(
    {
      zh: "示例：视频笔记",
      "zh-Hant": "範例：影片筆記",
      en: "Sample: video notes",
    },
    "Sample: video notes"
  ),
  summaries: recordAll(
    {
      zh: "处理一条视频后，笔记会出现在列表页。",
      "zh-Hant": "處理一條影片後，筆記會出現在列表頁。",
      en: "After you process a video, its notes appear in the library.",
    },
    "After you process a video, its notes appear in the library."
  ),
  overviews: recordAll(
    {
      zh: "这是占位内容。在首页粘贴链接或上传文件，生成真实笔记。",
      en: "Placeholder content. Paste a link or upload a file on the home page to generate real notes.",
    },
    "Placeholder content. Paste a link or upload a file on the home page."
  ),
  keyPointsIntro: recordAll(
    {
      zh: "口播视频转结构化笔记。",
      en: "Spoken video → structured notes.",
    },
    "Spoken video → structured notes."
  ),
  focuses: notesFor(
    [{ title: "转写", detail: "语音转文字，保留时间轴。" }, { title: "翻译", detail: "多语言字幕与笔记。" }],
    [{ title: "Transcribe", detail: "Speech to text with timestamps." }, { title: "Translate", detail: "Multilingual captions and notes." }]
  ),
  keyPoints: notesFor(
    [
      { title: "粘贴链接或上传文件", detail: "支持 YouTube、Bilibili、抖音或本地 mp4/mov。" },
      { title: "选择目标语言", detail: "生成字幕与笔记。" },
      { title: "可选截取片段", detail: "GIF/静帧与 MP4 短片段。" },
    ],
    [
      { title: "Paste a link or upload", detail: "YouTube, Bilibili, Douyin, or local mp4/mov." },
      { title: "Pick target languages", detail: "Captions and notes in each language." },
      { title: "Optional clips", detail: "Step GIFs/stills and short MP4 ranges." },
    ]
  ),
  hardPoints: notesFor(
    [{ title: "处理耗时", detail: "本机跑批，长视频可能需要 30–90 分钟。" }],
    [{ title: "Processing time", detail: "Runs locally; long videos may take 30–90 minutes." }]
  ),
  cues: [],
};

type ExportedCue = {
  start?: string;
  end?: string;
  text?: Record<string, string>;
};

type ExportedKeyPoint = {
  title?: string;
  detail?: string;
  image?: string;
  clip?: string;
  start_sec?: number;
  end_sec?: number;
  cue_start?: number;
  cue_end?: number;
};

type ExportedArticle = {
  platform: string;
  video_id: string;
  topic: string;
  slug: string;
  canonical_url: string;
  embed_url: string;
  source_lang?: string;
  title_src?: string;
  locales: Record<
    string,
    {
      title?: string;
      one_liner?: string;
      summary?: string;
      keypoints_intro?: string;
      key_points?: ExportedKeyPoint[];
      focuses?: ExportedKeyPoint[];
      hard_points?: ExportedKeyPoint[];
    }
  >;
  cues?: ExportedCue[];
  /** Full-length / embed timeline. Distinct from ``remix`` (9:16 overlay clock). */
  cue_clock?: "source" | string;
  remix?: {
    clock?: "remix" | string;
    video?: string;
    vtt?: string;
    cues?: string;
    intro_sec?: number;
    source_clock?: string;
    audio_clock?: boolean;
  };
};

type ExportedLocale = ExportedArticle["locales"][string];

function fillLocales(
  localesMap: ExportedArticle["locales"],
  pick: (loc: ExportedLocale | undefined) => string,
  fallback: string
): Record<Locale, string> {
  const out = {} as Record<Locale, string>;
  const src = localesMap.en || localesMap.zh || Object.values(localesMap)[0];
  for (const loc of locales) {
    out[loc] = pick(localesMap[loc]) || pick(src) || fallback;
  }
  return out;
}

function fillCueText(text: Record<string, string> | undefined): Record<Locale, string> {
  const src = (text && (text.en || text.zh || Object.values(text)[0])) || "";
  const out = {} as Record<Locale, string>;
  for (const loc of locales) {
    out[loc] = (text && text[loc]) || src;
  }
  return out;
}

function cuesFromExport(a: ExportedArticle): SampleArticle["cues"] {
  const raw = a.cues || [];
  return raw
    .filter((c) => c.start && c.end)
    .map((c) => ({
      start: c.start as string,
      end: c.end as string,
      text: fillCueText(c.text),
    }));
}

function fillNoteItems(
  localesMap: ExportedArticle["locales"],
  key: "key_points" | "focuses" | "hard_points",
  fallback: KeyPoint[]
): Record<Locale, KeyPoint[]> {
  const out = {} as Record<Locale, KeyPoint[]>;
  const src =
    localesMap.en?.[key] ||
    localesMap.zh?.[key] ||
    Object.values(localesMap).find((x) => x?.[key]?.length)?.[key] ||
    fallback;
  for (const loc of locales) {
    const raw = localesMap[loc]?.[key] || src || [];
    const mediaSrc =
      localesMap.zh?.[key] ||
      localesMap.en?.[key] ||
      src ||
      [];
    out[loc] = raw
      .map((kp, i) => ({
        title: (kp.title || "").trim(),
        detail: (kp.detail || "").trim(),
        image:
          (kp.image || mediaSrc[i]?.image || "").trim() || undefined,
        clip: (kp.clip || mediaSrc[i]?.clip || "").trim() || undefined,
        start_sec: kp.start_sec ?? mediaSrc[i]?.start_sec,
        end_sec: kp.end_sec ?? mediaSrc[i]?.end_sec,
        cue_start: kp.cue_start ?? mediaSrc[i]?.cue_start,
        cue_end: kp.cue_end ?? mediaSrc[i]?.cue_end,
      }))
      .filter((kp) => kp.title);
  }
  return out;
}

function fillKeyPointsIntro(
  localesMap: ExportedArticle["locales"],
  fallback: string
): Record<Locale, string> {
  return fillLocales(localesMap, (x) => x?.keypoints_intro || "", fallback);
}

function fromExport(a: ExportedArticle): SampleArticle {
  return {
    slug: a.slug,
    topic: a.topic,
    platform:
      a.platform === "bilibili"
        ? "bilibili"
        : a.platform === "douyin"
          ? "douyin"
          : a.platform === "upload"
            ? "upload"
            : "youtube",
    videoId: a.video_id,
    embedUrl: a.embed_url,
    sourceUrl: a.canonical_url,
    titles: fillLocales(a.locales, (x) => x?.title || "", a.title_src || a.slug),
    summaries: fillLocales(
      a.locales,
      (x) => x?.one_liner || "",
      a.title_src || ""
    ),
    overviews: fillLocales(
      a.locales,
      (x) => {
        const overview = (x?.summary || "").trim();
        const teaser = (x?.one_liner || "").trim();
        if (overview && overview !== teaser) return overview;
        return overview || teaser;
      },
      a.title_src || ""
    ),
    keyPointsIntro: fillKeyPointsIntro(a.locales, ""),
    focuses: fillNoteItems(a.locales, "focuses", []),
    keyPoints: fillNoteItems(a.locales, "key_points", []),
    hardPoints: fillNoteItems(a.locales, "hard_points", []),
    cues: cuesFromExport(a),
  };
}

export function loadArticles(): SampleArticle[] {
  const file = path.join(process.cwd(), "content", "articles.json");
  if (!existsSync(file)) return [sampleArticle];
  try {
    const text = readFileSync(file, "utf-8").trim();
    if (!text) return [sampleArticle];
    const raw = JSON.parse(text) as {
      articles?: ExportedArticle[];
    };
    const list = (raw.articles || []).map(fromExport);
    return list.length ? list : [sampleArticle];
  } catch {
    return [sampleArticle];
  }
}

export function feedArticles(): SampleArticle[] {
  const feed = loadArticles();
  return feed.length ? feed : [sampleArticle];
}

export function getArticle(topic: string, slug: string): SampleArticle | undefined {
  return loadArticles().find((a) => a.topic === topic && a.slug === slug);
}

export function siteOrigin(): string {
  return process.env.NEXT_PUBLIC_SITE_URL || "http://localhost:3000";
}

export function previewPoints(article: SampleArticle, locale: Locale): KeyPoint[] {
  return article.keyPoints[locale].slice(0, FREE_POINT_COUNT);
}

export function splitNotes(article: SampleArticle, locale: Locale) {
  const points = article.keyPoints[locale];
  return {
    freePoints: points.slice(0, FREE_POINT_COUNT),
    lockedPoints: points.slice(FREE_POINT_COUNT),
    focuses: article.focuses[locale],
    hardPoints: article.hardPoints[locale],
    cues: article.cues,
  };
}

export function hasLockedNotes(article: SampleArticle, locale: Locale): boolean {
  const s = splitNotes(article, locale);
  return Boolean(
    s.lockedPoints.length || s.focuses.length || s.hardPoints.length || s.cues.length
  );
}
