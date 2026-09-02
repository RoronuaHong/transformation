import { locales } from "@/lib/locales";

/** Mirrors subtitle_pipeline/api/try_service.resolve_try_intent (keep in sync). */

export type TryIntentKind =
  | "full"
  | "post"
  | "clips"
  | "media"
  | "frames"
  | "noop";

export type FrameOptsLike = {
  frames?: string;
  gif_sec?: number;
  clips?: unknown[];
  gif_ranges?: unknown[];
};

function normalizeTryLangs(raw: string | readonly string[] | null | undefined): string {
  const codes: string[] = [];
  if (Array.isArray(raw)) {
    for (const item of raw) {
      const tag = String(item || "")
        .trim()
        .toLowerCase();
      if (tag && !codes.includes(tag)) codes.push(tag);
    }
  } else {
    const text = String(raw || "").trim();
    if (!text || text === "site" || text === "all" || text === "*") return "site";
    for (const part of text.replace(/;/g, ",").split(",")) {
      const tag = part.trim().toLowerCase();
      if (tag && !codes.includes(tag)) codes.push(tag);
    }
  }
  if (!codes.length || codes.length >= locales.length) return "site";
  return codes.join(",");
}

function langsEqual(
  a: string | readonly string[] | null | undefined,
  b: string | readonly string[] | null | undefined
): boolean {
  const left = normalizeTryLangs(a);
  const right = normalizeTryLangs(b);
  if (left === "site" && right === "site") return true;
  if (left === "site" || right === "site") return false;
  if (left === right) return true;
  const A = new Set(left.split(",").filter(Boolean));
  const B = new Set(right.split(",").filter(Boolean));
  if (A.size !== B.size) return false;
  for (const x of A) if (!B.has(x)) return false;
  return true;
}

function frameOptsEqual(a?: FrameOptsLike | null, b?: FrameOptsLike | null): boolean {
  if (!a || !b) return false;
  const secA = Number(a.gif_sec ?? 4);
  const secB = Number(b.gif_sec ?? 4);
  if (!Number.isFinite(secA) || !Number.isFinite(secB) || Math.abs(secA - secB) >= 0.05) {
    return false;
  }
  const clipsA = Array.isArray(a.clips) ? a.clips : [];
  const clipsB = Array.isArray(b.clips) ? b.clips : [];
  const gifA = Array.isArray(a.gif_ranges) ? a.gif_ranges : [];
  const gifB = Array.isArray(b.gif_ranges) ? b.gif_ranges : [];
  return (
    String(a.frames || "auto") === String(b.frames || "auto") &&
    JSON.stringify(clipsA) === JSON.stringify(clipsB) &&
    JSON.stringify(gifA) === JSON.stringify(gifB)
  );
}

export function resolveTryIntent(opts: {
  jobStatus?: string | null;
  stages?: string | null;
  frames?: string;
  hasClips?: boolean;
  hasGifRanges?: boolean;
  newLangs?: string | readonly string[] | null;
  prevLangs?: string | readonly string[] | null;
  newFrameOpts?: FrameOptsLike | null;
  prevFrameOpts?: FrameOptsLike | null;
}): { intent: TryIntentKind; stages: string; reason: string } {
  const newPack = normalizeTryLangs(opts.newLangs);
  const prevPack =
    opts.prevLangs === undefined || opts.prevLangs === null
      ? null
      : normalizeTryLangs(opts.prevLangs);
  const mode = String(opts.frames || "auto")
    .trim()
    .toLowerCase();
  let explicit = String(opts.stages || "")
    .trim()
    .toLowerCase();

  if (["clips", "media", "frames", "gif", "mp4", "jpg"].includes(explicit)) {
    if (explicit === "gif" || explicit === "jpg") explicit = "frames";
    if (explicit === "mp4") explicit = "clips";
    return {
      intent: explicit as TryIntentKind,
      stages: explicit,
      reason: "explicit_stages",
    };
  }

  const st = String(opts.jobStatus || "")
    .trim()
    .toLowerCase();
  if (st !== "done" && st !== "published") {
    return { intent: "full", stages: "all", reason: "new_or_retry" };
  }

  const postproc = new Set(
    explicit
      .replace(/;/g, ",")
      .split(",")
      .map((p) => p.trim())
      .filter(
        (p) =>
          p === "enhance" ||
          p === "compress" ||
          p === "concat" ||
          p === "remix" ||
          p === "publish"
      )
  );
  if (postproc.size) {
    const parts: string[] = [];
    if (postproc.has("concat")) parts.push("clips");
    for (const name of ["concat", "enhance", "compress", "remix", "publish"] as const) {
      if (postproc.has(name)) parts.push(name);
    }
    return { intent: "media", stages: parts.join(","), reason: "postproc" };
  }

  if (prevPack === null || !langsEqual(prevPack, newPack)) {
    return { intent: "post", stages: "post", reason: "langs_changed" };
  }

  if (frameOptsEqual(opts.prevFrameOpts, opts.newFrameOpts)) {
    return { intent: "noop", stages: "noop", reason: "unchanged" };
  }

  const hasClips = Boolean(opts.hasClips);
  const hasGif = Boolean(opts.hasGifRanges);

  if (mode === "none" && hasClips) {
    return { intent: "clips", stages: "clips", reason: "clips_only" };
  }

  if (hasClips || hasGif || mode === "gif" || mode === "jpg") {
    let mediaStages: TryIntentKind = "media";
    if (hasClips && !hasGif && mode === "none") mediaStages = "clips";
    else if ((hasGif || mode === "gif" || mode === "jpg") && !hasClips) {
      mediaStages = "frames";
    }
    return {
      intent: mediaStages,
      stages: mediaStages,
      reason: "media_refresh",
    };
  }

  return { intent: "media", stages: "media", reason: "frame_opts_changed" };
}

/** Build try pipeline stages from module toggles (new jobs). */
export function composeTryStages(opts: {
  wantTranslate: boolean;
  wantNotes: boolean;
  hasMedia: boolean;
  wantEnhance?: boolean;
  wantCompress?: boolean;
  wantConcat?: boolean;
  wantRemix?: boolean;
  wantPublish?: boolean;
}): string {
  const {
    wantTranslate,
    wantNotes,
    hasMedia,
    wantEnhance = false,
    wantCompress = false,
    wantConcat = false,
    wantRemix = false,
    wantPublish = false,
  } = opts;
  const post: string[] = [];
  if (wantConcat) post.push("concat");
  if (wantEnhance) post.push("enhance");
  if (wantCompress) post.push("compress");
  if (wantRemix) post.push("remix");
  if (wantPublish) post.push("publish");
  if (wantTranslate && wantNotes) {
    return post.length ? ["all", ...post].join(",") : "all";
  }
  const parts: string[] = [];
  const text = wantTranslate || wantNotes;
  const media = hasMedia || wantConcat;
  if (text || media || post.length) parts.push("fetch");
  if (text) parts.push("asr");
  if (wantTranslate) parts.push("translate");
  if (wantNotes) parts.push("notes", "localize");
  if (hasMedia) parts.push("frames", "clips");
  else if (wantConcat) parts.push("clips");
  if (!wantTranslate && !wantNotes && !hasMedia && !post.length) {
    parts.push("fetch", "asr", "notes", "localize");
  }
  parts.push(...post);
  return [...new Set(parts)].join(",");
}
