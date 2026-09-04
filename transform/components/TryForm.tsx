"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cloneSourceMedia,
  defaultSourceMedia,
  TrySourceMediaCard,
  type ClipRow,
  type SourceMediaState,
} from "@/components/TrySourceMedia";
import { RemixPlayer } from "@/components/RemixPlayer";
import { fillCopy, type TryCopy } from "@/lib/copy";
import { DEFAULT_TOPIC } from "@/lib/site-config";
import {
  composeTryStages,
  resolveTryIntent,
  type FrameOptsLike,
  type TryIntentKind,
} from "@/lib/try-intent";
import { formatBatchJobLabel } from "@/lib/batch-job-label";
import { parseUrlLines, type UrlProbeResult } from "@/lib/try-urls";
import { tryApiBase } from "@/lib/api-base";
import type { Locale } from "@/lib/locales";
import { localeNames, localePath, locales } from "@/lib/locales";

/** Same-origin proxy — avoids browser extensions blocking cross-origin :8901 fetch. */
const API = tryApiBase();

function isFetchNetworkError(e: unknown): boolean {
  if (!(e instanceof Error)) return false;
  const m = e.message.toLowerCase();
  return (
    e.name === "TypeError" ||
    m.includes("failed to fetch") ||
    m.includes("networkerror") ||
    m.includes("load failed")
  );
}

type Progress = {
  percent: number;
  stage: string;
  detail?: string | null;
  active?: boolean;
  stale?: boolean;
  updated_sec_ago?: number | null;
};

type Poll = {
  job_id: number;
  status: string;
  error?: string | null;
  path?: string;
  title?: string;
  busy?: boolean;
  paused?: boolean;
  active_job_id?: number | null;
  progress?: Progress;
  queue_position?: number;
  queue_ahead?: {
    job_id: number;
    title?: string;
    progress?: Progress;
  };
  cached?: boolean;
  duration_sec?: number | null;
  derived?: {
    enhance?: string;
    compress?: string;
    concat?: string;
    remix?: string;
    remix_cues?: string;
    remix_vtt?: string;
    publish?: string;
  };
  frame_opts?: {
    frames?: string;
    gif_sec?: number;
    gif_ranges?: { start: number; end: number }[];
    clips?: { start: number; end: number }[];
  };
};

const GIF_SEGMENT_MAX = 20;
/** Fallback max when video length is unknown (prevents absurd inputs). */
const CLIP_FALLBACK_MAX = 6 * 60 * 60;
/** Must stay aligned with normalize_frame_opts / attach_video_range_clips. */
const CLIP_SEGMENT_MAX = 60;

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function sourceKeyUrl(url: string) {
  return `url:${url}`;
}

function sourceKeyFile(file: File) {
  return `file:${file.name}:${file.size}:${file.lastModified}`;
}

function readFileDuration(file: File): Promise<number | null> {
  return new Promise((resolve) => {
    const obj = URL.createObjectURL(file);
    const el = document.createElement("video");
    el.preload = "metadata";
    el.onloadedmetadata = () => {
      const d = el.duration;
      URL.revokeObjectURL(obj);
      resolve(Number.isFinite(d) && d > 0 ? d : null);
    };
    el.onerror = () => {
      URL.revokeObjectURL(obj);
      resolve(null);
    };
    el.src = obj;
  });
}

function clipLimit(videoDur: number | null) {
  return videoDur != null && videoDur > 0 ? videoDur : CLIP_FALLBACK_MAX;
}

function sanitizeSecInput(raw: string, max: number): string {
  if (raw.trim() === "") return "";
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n > max) return String(Math.round(max * 10) / 10);
  // Keep reasonable precision; drop scientific / overlong digit strings.
  return String(Math.round(n * 10) / 10);
}

function validateClipRows(
  rows: ClipRow[],
  videoDur: number | null,
  copy: TryCopy,
  opts: { segmentMax: number; tooLongMsg: string }
): string | null {
  const max = clipLimit(videoDur);
  const { segmentMax, tooLongMsg } = opts;
  for (const row of rows) {
    const sRaw = row.start.trim();
    const eRaw = row.end.trim();
    if (!sRaw && !eRaw) continue;
    if (!sRaw || !eRaw) return copy.clipsNeedBoth;
    const start = Number(sRaw);
    const end = Number(eRaw);
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end < 0) {
      return copy.clipsInvalidNumber;
    }
    if (end <= start) return copy.clipsEndAfterStart;
    if (end - start > segmentMax) {
      return tooLongMsg.replace("{n}", String(segmentMax));
    }
    if (start > max || end > max) {
      return copy.clipsOverDuration.replace("{n}", String(Math.round(max * 10) / 10));
    }
  }
  return null;
}

const STAGE_KEYS: Record<string, keyof TryCopy> = {
  queued: "stageQueued",
  download: "stageDownload",
  transcribe: "stageTranscribe",
  translate: "stageTranslate",
  notes: "stageNotes",
  localize: "stageLocalize",
  gif: "stageGif",
  enhance: "stageEnhance",
  compress: "stageCompress",
  concat: "stageConcat",
  remix: "stageRemix",
  publish: "stagePublish",
  export: "stageExport",
  done: "stageDone",
  failed: "stageFailed",
  cancelled: "stageCancelled",
};

function isTerminalStatus(status?: string) {
  return (
    status === "done" ||
    status === "failed" ||
    status === "dead" ||
    status === "cancelled"
  );
}

function badgeClass(status?: string) {
  if (status === "processing") return "is-processing";
  if (status === "done") return "is-done";
  if (status === "failed" || status === "dead") return "is-failed";
  if (status === "cancelled") return "is-cancelled";
  return "is-pending";
}

function stageLabel(copy: TryCopy, stage?: string) {
  if (!stage) return copy.statusProcessing;
  const key = STAGE_KEYS[stage];
  return key ? String(copy[key]) : copy.statusProcessing;
}

function jobStatusLabel(copy: TryCopy, status?: string) {
  if (status === "done") return copy.statusDone;
  if (status === "cancelled") return copy.statusCancelled;
  if (status === "failed" || status === "dead") return copy.statusFailed;
  if (status === "processing") return copy.statusProcessing;
  return copy.statusPending;
}

/** Client-side: quick hint before server probe. */
function urlNeedsCookies(raw: string): boolean {
  const u = raw.trim().toLowerCase();
  return u.includes("douyin.com") || u.includes("iesdouyin.com") || u.includes("v.douyin.com");
}

function platformDisplay(label: string, locale: Locale): string {
  if (locale === "zh" || locale === "zh-Hant") {
    if (label === "Douyin") return "抖音";
    if (label === "Bilibili") return "B 站";
  }
  return label;
}

function progressDetail(copy: TryCopy, progress?: Progress) {
  if (!progress?.detail) return null;
  if (progress.stage === "translate" || progress.stage === "localize") {
    const [n, total] = progress.detail.split("/");
    return copy.progressLangs.replace("{n}", n).replace("{total}", total);
  }
  return progress.detail;
}

function parseRanges(rows: ClipRow[], segmentMax: number, max: number) {
  return rows
    .map((c) => ({ start: Number(c.start), end: Number(c.end) }))
    .filter(
      (c) =>
        Number.isFinite(c.start) &&
        Number.isFinite(c.end) &&
        c.end > c.start &&
        c.start >= 0
    )
    .map((c) => ({
      start: Math.min(c.start, max),
      end: Math.min(c.end, max),
    }))
    .map((c) =>
      c.end - c.start > segmentMax ? { start: c.start, end: c.start + segmentMax } : c
    )
    .filter((c) => c.end > c.start);
}

function intentLabel(
  copy: TryCopy,
  intent: TryIntentKind,
  langCount: number,
  postprocOn = false
) {
  switch (intent) {
    case "post":
      return copy.intentPost.replace("{n}", String(langCount));
    case "clips":
      return copy.intentClips;
    case "media":
    case "frames":
      return postprocOn ? copy.intentPostproc : copy.intentMedia;
    case "noop":
      return copy.intentNoop;
    default:
      return copy.intentFull;
  }
}

export function TryForm({ locale, copy }: { locale: Locale; copy: TryCopy }) {
  const [tab, setTab] = useState<"url" | "upload">("url");
  const [urlsText, setUrlsText] = useState("");
  const [sessionid, setSessionid] = useState("");
  const [urlProbe, setUrlProbe] = useState<UrlProbeResult | null>(null);
  const [batchQueued, setBatchQueued] = useState(0);
  const [sourceMedia, setSourceMedia] = useState<Record<string, SourceMediaState>>({});
  const [sourceDurations, setSourceDurations] = useState<Record<string, number>>({});
  const [files, setFiles] = useState<File[]>([]);
  const [fileDragOver, setFileDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [targetLangs, setTargetLangs] = useState<Locale[]>(() => [...locales]);
  const [wantTranslate, setWantTranslate] = useState(true);
  const [wantNotes, setWantNotes] = useState(true);
  const [wantClips, setWantClips] = useState(true);
  const [wantEnhance, setWantEnhance] = useState(false);
  const [wantDehardsub, setWantDehardsub] = useState(false);
  const [wantCompress, setWantCompress] = useState(false);
  const [wantConcat, setWantConcat] = useState(false);
  const [wantRemix, setWantRemix] = useState(false);
  const [wantPublish, setWantPublish] = useState(false);
  const [publishPlatforms, setPublishPlatforms] = useState<string[]>([]);
  const [accountSlots, setAccountSlots] = useState<
    Array<{
      platform: string;
      label: string;
      account_id: string;
      status: "valid" | "invalid" | "unbound";
      bound: boolean;
    }>
  >([]);
  const [accountDrafts, setAccountDrafts] = useState<
    Record<string, { secret: string; label: string; account_id: string }>
  >({});
  const [accountBusy, setAccountBusy] = useState<string | null>(null);
  const [enhanceStrength, setEnhanceStrength] = useState<"light" | "medium" | "strong">(
    "medium"
  );
  const [compressHeight, setCompressHeight] = useState(720);
  const [compressCrf, setCompressCrf] = useState(28);
  const [videoDur, setVideoDur] = useState<number | null>(null);
  const [cachedDone, setCachedDone] = useState(false);
  const [prevLangs, setPrevLangs] = useState<string | null>(null);
  const [prevFrameOpts, setPrevFrameOpts] = useState<FrameOptsLike | null>(null);
  const [langsOpen, setLangsOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [poll, setPoll] = useState<Poll | null>(null);
  const [batchJobs, setBatchJobs] = useState<
    Array<{
      job_id: number;
      label: string;
      status: string;
      path?: string;
      title?: string;
      error?: string | null;
      progress?: Progress;
    }>
  >([]);
  const [err, setErr] = useState<string | null>(null);
  const [queuePaused, setQueuePaused] = useState(false);
  const [jobActionBusy, setJobActionBusy] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const probeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollFailRef = useRef(0);
  const langsDdRef = useRef<HTMLDivElement | null>(null);
  const batchJobsRef = useRef(batchJobs);
  batchJobsRef.current = batchJobs;

  const loadAccounts = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/try/accounts`);
      if (!r.ok) return;
      const data = (await r.json()) as {
        accounts?: Array<{
          platform: string;
          label: string;
          account_id: string;
          status: "valid" | "invalid" | "unbound";
          bound: boolean;
        }>;
      };
      const slots = data.accounts || [];
      setAccountSlots(slots);
      const valid = slots.filter((s) => s.status === "valid").map((s) => s.platform);
      setPublishPlatforms((prev) => {
        const keep = prev.filter((p) => valid.includes(p));
        return keep.length ? keep : valid;
      });
    } catch {
      /* backend may be down */
    }
  }, []);

  useEffect(() => {
    void loadAccounts();
  }, [loadAccounts]);

  const parsedUrls = useMemo(() => parseUrlLines(urlsText), [urlsText]);

  const sourceItems = useMemo(() => {
    if (tab === "url") {
      return parsedUrls.map((url) => ({
        key: sourceKeyUrl(url),
        label: url,
      }));
    }
    return files.map((f) => ({
      key: sourceKeyFile(f),
      label: f.name,
    }));
  }, [tab, parsedUrls, files]);

  const sourceKeysSig = sourceItems.map((s) => s.key).join("\n");

  useEffect(() => {
    const keys = sourceKeysSig ? sourceKeysSig.split("\n").filter(Boolean) : [];
    setSourceMedia((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const key of keys) {
        if (!next[key]) {
          // New item: copy first existing card if any, else auto defaults.
          const seed = Object.values(prev)[0];
          next[key] = seed
            ? { ...cloneSourceMedia(seed.frames, seed.gifRanges, seed.clips), open: true }
            : defaultSourceMedia("auto");
          changed = true;
        }
      }
      for (const key of Object.keys(next)) {
        if (!keys.includes(key)) {
          delete next[key];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [sourceKeysSig]);

  const cookiesBlockSubmit = Boolean(urlProbe?.block_submit);
  const cookiesReady = !cookiesBlockSubmit && (parsedUrls.length > 0 || Boolean(urlProbe?.ok));
  const linkRangeError = sourceItems.reduce<string | null>((found, { key }) => {
    if (found) return found;
    const sm = sourceMedia[key];
    if (!sm) return null;
    const dur = sourceDurations[key] ?? videoDur;
    const gErr =
      sm.frames === "none"
        ? null
        : validateClipRows(sm.gifRanges, dur, copy, {
            segmentMax: GIF_SEGMENT_MAX,
            tooLongMsg: copy.gifTooLong,
          });
    if (gErr) return gErr;
    return validateClipRows(sm.clips, dur, copy, {
      segmentMax: CLIP_SEGMENT_MAX,
      tooLongMsg: copy.clipsTooLong,
    });
  }, null);
  const allLangsSelected = targetLangs.length === locales.length;

  const batchHasClips = sourceItems.some(({ key }) => {
    const sm = sourceMedia[key];
    return sm?.clips.some((r) => r.start.trim() && r.end.trim());
  });
  const batchHasGif = sourceItems.some(({ key }) => {
    const sm = sourceMedia[key];
    if (!sm || sm.frames === "none") return false;
    if (sm.frames === "gif" || sm.frames === "jpg") return true;
    return sm.gifRanges.some((r) => r.start.trim() && r.end.trim());
  });
  const batchFrames =
    sourceMedia[sourceItems[0]?.key || ""]?.frames ||
    (batchHasGif ? "gif" : batchHasClips ? "none" : "auto");

  const firstSourceKey =
    sourceItems.find(({ key }) => sourceMedia[key]?.open !== undefined)?.key ||
    sourceItems[0]?.key;
  const firstSm = firstSourceKey
    ? sourceMedia[firstSourceKey] || defaultSourceMedia("auto")
    : null;
  const firstDur = firstSourceKey
    ? (sourceDurations[firstSourceKey] ?? videoDur)
    : videoDur;
  const firstMax = clipLimit(firstDur);
  const newFrameOpts: FrameOptsLike | null = firstSm
    ? (() => {
        const clips = parseRanges(firstSm.clips, CLIP_SEGMENT_MAX, firstMax);
        const gifRanges =
          firstSm.frames === "none"
            ? []
            : parseRanges(firstSm.gifRanges, GIF_SEGMENT_MAX, firstMax);
        const gifSec =
          gifRanges.length > 0
            ? Math.min(
                GIF_SEGMENT_MAX,
                Math.max(...gifRanges.map((r) => r.end - r.start), 1)
              )
            : 4;
        return {
          frames: firstSm.frames,
          gif_sec: gifSec,
          clips,
          gif_ranges: gifRanges,
        };
      })()
    : {
        frames: batchFrames,
        gif_sec: 4,
        clips: [],
        gif_ranges: [],
      };

  const filledClipCount = (key: string) =>
    (sourceMedia[key]?.clips || []).filter((r) => r.start.trim() && r.end.trim()).length;
  const concatMissingRanges =
    wantConcat &&
    (sourceItems.length === 0 || sourceItems.some(({ key }) => filledClipCount(key) < 2));
  const validPublish = accountSlots.filter((s) => s.status === "valid");
  const publishBlocked =
    wantPublish && (validPublish.length === 0 || publishPlatforms.length === 0);
  const hasAnyModule =
    wantTranslate ||
    wantNotes ||
    wantDehardsub ||
    wantEnhance ||
    wantCompress ||
    wantConcat ||
    wantRemix ||
    wantPublish ||
    (wantClips && (batchHasClips || batchHasGif));
  const stagesPayload = composeTryStages({
    wantTranslate,
    wantNotes,
    hasMedia: wantClips && (batchHasClips || batchHasGif || batchFrames !== "none"),
    wantDehardsub,
    wantEnhance,
    wantCompress,
    wantConcat,
    wantRemix,
    wantPublish,
  });
  const plannedIntent: TryIntentKind =
    sourceItems.length > 1 && !cachedDone
      ? "full"
      : resolveTryIntent({
          jobStatus: cachedDone ? urlProbe?.job_status || "done" : null,
          stages: stagesPayload,
          frames: batchFrames,
          hasClips: batchHasClips,
          hasGifRanges: batchHasGif,
          newLangs: targetLangs,
          prevLangs,
          newFrameOpts,
          prevFrameOpts,
        }).intent;
  const plannedIntentText = intentLabel(
    copy,
    plannedIntent,
    targetLangs.length,
    wantDehardsub || wantEnhance || wantCompress || wantConcat || wantRemix || wantPublish
  );
  const langsSummary = allLangsSelected
    ? copy.langsAllLabel
    : targetLangs.length === 0
      ? copy.langsPick
      : targetLangs
          .slice(0, 3)
          .map((c) => localeNames[c])
          .join(locale.startsWith("zh") ? "、" : ", ") +
        (targetLangs.length > 3 ? ` +${targetLangs.length - 3}` : "");

  useEffect(() => {
    if (!langsOpen) return;
    const root = document.documentElement;
    root.classList.add("langs-sheet-open");
    const onPointer = (e: MouseEvent | TouchEvent) => {
      const el = langsDdRef.current;
      if (!el) return;
      if (e.target instanceof Node && !el.contains(e.target)) setLangsOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setLangsOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("touchstart", onPointer, { passive: true });
    document.addEventListener("keydown", onKey);
    return () => {
      root.classList.remove("langs-sheet-open");
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("touchstart", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [langsOpen]);

  function patchSourceMedia(key: string, patch: Partial<SourceMediaState>) {
    setSourceMedia((prev) => ({
      ...prev,
      [key]: { ...(prev[key] || defaultSourceMedia("auto")), ...patch },
    }));
  }

  function syncFirstToAll() {
    const firstKey = sourceItems[0]?.key;
    const first = firstKey ? sourceMedia[firstKey] : null;
    if (!first) return;
    setSourceMedia((prev) => {
      const next = { ...prev };
      for (const { key } of sourceItems) {
        next[key] = {
          ...cloneSourceMedia(first.frames, first.gifRanges, first.clips),
          open: prev[key]?.open ?? true,
        };
      }
      return next;
    });
  }

  function setSourceRangeField(
    key: string,
    which: "gif" | "mp4",
    index: number,
    field: "start" | "end",
    raw: string
  ) {
    const sm = sourceMedia[key] || defaultSourceMedia("auto");
    const dur = sourceDurations[key] ?? videoDur;
    const max = clipLimit(dur);
    const v = sanitizeSecInput(raw, max);
    if (which === "gif") {
      const next = [...(sm.gifRanges.length ? sm.gifRanges : [{ start: "", end: "" }])];
      next[index] = { ...next[index], [field]: v };
      patchSourceMedia(key, { gifRanges: next });
    } else {
      const next = [...(sm.clips.length ? sm.clips : [{ start: "", end: "" }])];
      next[index] = { ...next[index], [field]: v };
      patchSourceMedia(key, { clips: next });
    }
  }

  function addSourceRange(key: string, which: "gif" | "mp4") {
    const sm = sourceMedia[key] || defaultSourceMedia("auto");
    if (which === "gif") {
      patchSourceMedia(key, {
        gifRanges: [
          ...(sm.gifRanges.length ? sm.gifRanges : [{ start: "", end: "" }]),
          { start: "", end: "" },
        ],
      });
    } else {
      patchSourceMedia(key, {
        clips: [...(sm.clips.length ? sm.clips : [{ start: "", end: "" }]), { start: "", end: "" }],
      });
    }
  }

  function removeSourceRange(key: string, which: "gif" | "mp4", index: number) {
    const sm = sourceMedia[key] || defaultSourceMedia("auto");
    if (which === "gif") {
      const base = sm.gifRanges.length ? sm.gifRanges : [{ start: "", end: "" }];
      const next = base.filter((_, j) => j !== index);
      patchSourceMedia(key, { gifRanges: next.length ? next : [{ start: "", end: "" }] });
    } else {
      const base = sm.clips.length ? sm.clips : [{ start: "", end: "" }];
      const next = base.filter((_, j) => j !== index);
      patchSourceMedia(key, { clips: next.length ? next : [{ start: "", end: "" }] });
    }
  }

  function sourceMediaErrors(key: string): { gif: string | null; clip: string | null } {
    const sm = sourceMedia[key];
    if (!sm) return { gif: null, clip: null };
    const dur = sourceDurations[key] ?? videoDur;
    return {
      gif:
        sm.frames === "none"
          ? null
          : validateClipRows(sm.gifRanges, dur, copy, {
              segmentMax: GIF_SEGMENT_MAX,
              tooLongMsg: copy.gifTooLong,
            }),
      clip: validateClipRows(sm.clips, dur, copy, {
        segmentMax: CLIP_SEGMENT_MAX,
        tooLongMsg: copy.clipsTooLong,
      }),
    };
  }

  function buildSourceEntry(key: string, idField: "url" | "filename", idValue: string) {
    const sm = sourceMedia[key] || defaultSourceMedia("auto");
    const dur = sourceDurations[key] ?? videoDur;
    const max = clipLimit(dur);
    const entryClips = parseRanges(sm.clips, CLIP_SEGMENT_MAX, max);
    const entryGif = sm.frames === "none" ? [] : parseRanges(sm.gifRanges, GIF_SEGMENT_MAX, max);
    const gifSec =
      entryGif.length > 0
        ? Math.min(GIF_SEGMENT_MAX, Math.max(...entryGif.map((r) => r.end - r.start), 1))
        : 4;
    return {
      [idField]: idValue,
      override: true,
      frames: sm.frames,
      gif_sec: gifSec,
      gif_ranges: entryGif,
      clips: entryClips,
      ...(dur != null ? { duration_sec: dur } : {}),
    };
  }

  async function ingestFiles(picked: File[]) {
    setFiles(picked);
    const durations: Record<string, number> = {};
    for (const f of picked) {
      const key = sourceKeyFile(f);
      const dur = await readFileDuration(f);
      if (dur != null && dur > 0) durations[key] = dur;
    }
    setSourceDurations((prev) => {
      const next = { ...prev };
      for (const key of Object.keys(next)) {
        if (!key.startsWith("file:")) continue;
        if (!picked.some((f) => sourceKeyFile(f) === key)) delete next[key];
      }
      return { ...next, ...durations };
    });
    if (picked.length === 1) {
      const d = durations[sourceKeyFile(picked[0])];
      applyVideoDuration(d ?? null);
    } else if (!picked.length) {
      applyVideoDuration(null);
    }
  }

  function mergePickedFiles(incoming: FileList | File[]) {
    const list = Array.from(incoming).filter((f) => {
      const name = f.name.toLowerCase();
      return (
        f.type.startsWith("video/") ||
        f.type.startsWith("audio/") ||
        /\.(mp4|mov|mkv|webm|m4a|wav)$/i.test(name)
      );
    });
    if (!list.length) return;
    const byKey = new Map<string, File>();
    for (const f of files) byKey.set(sourceKeyFile(f), f);
    for (const f of list) byKey.set(sourceKeyFile(f), f);
    void ingestFiles([...byKey.values()]);
  }

  function removeFileAt(index: number) {
    const next = files.filter((_, i) => i !== index);
    void ingestFiles(next);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function clearFiles() {
    void ingestFiles([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function toggleLang(code: Locale) {
    setTargetLangs((prev) =>
      prev.includes(code) ? prev.filter((x) => x !== code) : [...prev, code]
    );
  }

  function applyVideoDuration(sec: number | null) {
    if (sec == null || !(sec > 0)) {
      setVideoDur(null);
      return;
    }
    const d = Math.round(sec * 10) / 10;
    setVideoDur(d);
    setSourceMedia((prev) => {
      let changed = false;
      const next: Record<string, SourceMediaState> = {};
      for (const [key, sm] of Object.entries(prev)) {
        const clampRows = (rows: ClipRow[]) =>
          rows.map((row) => ({
            start: sanitizeSecInput(row.start, d),
            end: sanitizeSecInput(row.end, d),
          }));
        const gifRanges = clampRows(sm.gifRanges);
        const clips = clampRows(sm.clips);
        if (
          gifRanges.some((r, i) => r.start !== sm.gifRanges[i]?.start || r.end !== sm.gifRanges[i]?.end) ||
          clips.some((r, i) => r.start !== sm.clips[i]?.start || r.end !== sm.clips[i]?.end)
        ) {
          changed = true;
          next[key] = { ...sm, gifRanges, clips };
        } else {
          next[key] = sm;
        }
      }
      return changed ? next : prev;
    });
  }

  const stopPoll = useCallback(() => {
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
  }, []);

  const resetForm = useCallback(() => {
    stopPoll();
    if (probeTimer.current) {
      clearTimeout(probeTimer.current);
      probeTimer.current = null;
    }
    pollFailRef.current = 0;
    batchJobsRef.current = [];
    if (fileInputRef.current) fileInputRef.current.value = "";
    setUrlsText("");
    setSessionid("");
    setUrlProbe(null);
    setBatchQueued(0);
    setSourceMedia({});
    setSourceDurations({});
    setFiles([]);
    setFileDragOver(false);
    setVideoDur(null);
    setCachedDone(false);
    setPrevLangs(null);
    setPrevFrameOpts(null);
    setTargetLangs([...locales]);
    setWantTranslate(true);
    setWantNotes(true);
    setWantClips(true);
    setWantDehardsub(false);
    setWantEnhance(false);
    setWantCompress(false);
    setWantConcat(false);
    setEnhanceStrength("medium");
    setCompressHeight(720);
    setCompressCrf(28);
    setLangsOpen(false);
    setSubmitting(false);
    setPoll(null);
    setBatchJobs([]);
    setErr(null);
  }, [stopPoll]);

  const refreshJobs = useCallback(async () => {
    const current = batchJobsRef.current;
    const toPoll = current.filter((j) => j.job_id && !isTerminalStatus(j.status));
    if (!toPoll.length) {
      stopPoll();
      return;
    }

    let activePoll: Poll | null = null;
    let processingPoll: Poll | null = null;
    let lastUpdated: Poll | null = null;
    const byId = new Map<number, Partial<Poll> & { status: string }>();

    try {
      for (const j of toPoll) {
        const r = await fetch(`${API}/api/try/${j.job_id}`);
        if (!r.ok) throw new Error(await r.text());
        const data = (await r.json()) as Poll;
        byId.set(j.job_id, {
          status: data.status,
          path: data.path,
          title: data.title,
          error: data.error,
          progress: data.progress,
          queue_ahead: data.queue_ahead,
          queue_position: data.queue_position,
          busy: data.busy,
        });
        lastUpdated = data;
        if (data.paused != null) setQueuePaused(Boolean(data.paused));
        if (data.status === "processing") {
          processingPoll = data;
        } else if (data.status === "pending") {
          activePoll = data;
        }
        if (data.duration_sec != null && data.duration_sec > 0) {
          applyVideoDuration(data.duration_sec);
        }
      }
    } catch (e) {
      pollFailRef.current += 1;
      if (pollFailRef.current < 3) {
        return;
      }
      setErr(isFetchNetworkError(e) ? copy.apiDown : e instanceof Error ? e.message : String(e));
      stopPoll();
      return;
    }

    pollFailRef.current = 0;
    setErr((prev) => (prev === copy.apiDown ? null : prev));

    setBatchJobs((prev) =>
      prev.map((j) => {
        const u = byId.get(j.job_id);
        return u ? { ...j, ...u } : j;
      })
    );

    const nextPoll = processingPoll || activePoll || lastUpdated;
    if (nextPoll) setPoll(nextPoll);

    const stillActive = toPoll.some((j) => {
      const u = byId.get(j.job_id);
      return Boolean(u && !isTerminalStatus(u.status));
    });
    if (!stillActive) {
      stopPoll();
    }
  }, [stopPoll]);

  const toggleQueuePause = useCallback(async () => {
    const path = queuePaused ? "/api/try/queue/resume" : "/api/try/queue/pause";
    try {
      const r = await fetch(`${API}${path}`, { method: "POST" });
      if (!r.ok) throw new Error(await r.text());
      const data = (await r.json()) as { paused?: boolean };
      setQueuePaused(Boolean(data.paused));
      if (!data.paused) void refreshJobs();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [queuePaused, refreshJobs]);

  const runJobAction = useCallback(
    async (jobId: number, action: "cancel" | "retry") => {
      setJobActionBusy(jobId);
      try {
        const r = await fetch(`${API}/api/try/${jobId}/${action}`, { method: "POST" });
        if (!r.ok) {
          const body = (await r.json().catch(() => ({}))) as { detail?: string };
          throw new Error(body.detail || r.statusText);
        }
        const data = (await r.json()) as Poll;
        if (data.paused != null) setQueuePaused(Boolean(data.paused));
        setBatchJobs((prev) =>
          prev.map((j) =>
            j.job_id === jobId
              ? {
                  ...j,
                  status: data.status || j.status,
                  path: data.path ?? j.path,
                  title: data.title ?? j.title,
                  error: data.error ?? null,
                  progress: data.progress ?? j.progress,
                }
              : j
          )
        );
        setPoll((prev) => (prev?.job_id === jobId ? { ...prev, ...data } : prev));
        if (action === "retry" && !timer.current) {
          timer.current = setInterval(() => {
            void refreshJobs();
          }, 3000);
        }
        void refreshJobs();
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        setJobActionBusy(null);
      }
    },
    [refreshJobs]
  );

  const resumedRef = useRef(false);
  useEffect(() => {
    if (resumedRef.current) return;
    resumedRef.current = true;
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`${API}/api/try/active`);
        if (!r.ok || cancelled) return;
        const data = (await r.json()) as {
          jobs?: Array<Poll & { job_id: number; title?: string }>;
          paused?: boolean;
        };
        if (data.paused != null) setQueuePaused(Boolean(data.paused));
        const active = (data.jobs || []).filter(
          (j) => j.status === "pending" || j.status === "processing"
        );
        if (!active.length || cancelled) return;
        const mapped = active.map((j) => ({
          job_id: j.job_id,
          label: j.title || String(j.job_id),
          status: j.status,
          path: j.path,
          title: j.title,
          error: j.error ?? null,
          progress: j.progress,
        }));
        setBatchJobs(mapped);
        batchJobsRef.current = mapped;
        setPoll(active[0]);
        if (!timer.current) {
          timer.current = setInterval(() => {
            void refreshJobs();
          }, 3000);
        }
      } catch {
        /* ignore — probe/submit will surface API errors */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshJobs]);

  useEffect(() => () => stopPoll(), [stopPoll]);

  useEffect(() => {
    if (tab !== "url") return;
    const urls = parseUrlLines(urlsText);
    if (urls.length === 0) {
      if (!files.length) applyVideoDuration(null);
      setUrlProbe(null);
      setCachedDone(false);
      setPrevLangs(null);
      setPrevFrameOpts(null);
      return;
    }
    if (probeTimer.current) clearTimeout(probeTimer.current);
    probeTimer.current = setTimeout(async () => {
      try {
        const r = await fetch(`${API}/api/try/probe`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            urls,
            ...(sessionid.trim() ? { sessionid: sessionid.trim() } : {}),
          }),
        });
        if (!r.ok) throw new Error(`probe ${r.status}`);
        const data = (await r.json()) as UrlProbeResult;
        setUrlProbe(data);
        setErr((prev) => (prev === copy.apiDown ? null : prev));
        const done =
          !!data.cached || data.job_status === "done" || data.job_status === "published";
        setCachedDone(done);
        setPrevLangs(typeof data.langs === "string" ? data.langs : done ? "site" : null);
        setPrevFrameOpts(data.frame_opts || null);
        if (data.duration_sec != null) applyVideoDuration(data.duration_sec);
        else if (!files.length) applyVideoDuration(null);
      } catch (e) {
        if (!files.length) applyVideoDuration(null);
        setUrlProbe(null);
        if (isFetchNetworkError(e) || (e instanceof Error && /probe\s+[45]\d\d/.test(e.message))) {
          setErr(copy.apiDown);
        }
      }
    }, 450);
    return () => {
      if (probeTimer.current) clearTimeout(probeTimer.current);
    };
  }, [tab, urlsText, sessionid, files.length]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setPoll(null);
    setBatchJobs([]);

    const clipCheck = linkRangeError;
    if (clipCheck) {
      setErr(clipCheck);
      return;
    }
    if (sourceItems.length === 0) {
      setErr(tab === "url" ? copy.urlHint : copy.filesNeedOne);
      return;
    }
    if (concatMissingRanges) {
      setErr(copy.concatNeedTwo);
      return;
    }
    if (!hasAnyModule) {
      setErr(
        locale.startsWith("zh")
          ? "请至少开启一个模块，或添加 GIF/MP4 片段。"
          : "Enable at least one module, or add GIF/MP4 clips."
      );
      return;
    }
    if ((wantTranslate || wantNotes) && targetLangs.length === 0) {
      setErr(copy.langsNeedOne);
      return;
    }

    setSubmitting(true);
    stopPoll();

    try {
      let res: Response;
      const entriesPayload =
        tab === "url"
          ? parseUrlLines(urlsText).map((url) =>
              buildSourceEntry(sourceKeyUrl(url), "url", url)
            )
          : files.map((f) => buildSourceEntry(sourceKeyFile(f), "filename", f.name));
      if (tab === "url") {
        const urls = parseUrlLines(urlsText);
        if (!urls.length) {
          setSubmitting(false);
          return;
        }
        if (cookiesBlockSubmit && !sessionid.trim()) {
          setErr(urlProbe?.message || copy.cookiesBlockBatch.replace("{platforms}", ""));
          setSubmitting(false);
          return;
        }
        res = await fetch(`${API}/api/try/urls`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            urls,
            entries: entriesPayload,
            topic: DEFAULT_TOPIC,
            frames: "auto",
            gif_sec: 4,
            gif_ranges: [],
            clips: [],
            langs: targetLangs,
            want_translate: wantTranslate,
            want_notes: wantNotes,
            want_dehardsub: wantDehardsub,
            want_enhance: wantEnhance,
            want_compress: wantCompress,
            want_concat: wantConcat,
            want_remix: wantRemix,
            want_publish: wantPublish,
            media_opts: {
              enhance_strength: enhanceStrength,
              compress_height: compressHeight,
              compress_crf: compressCrf,
              publish_platforms: publishPlatforms,
            },
            stages: stagesPayload,
            ...(sessionid.trim() ? { sessionid: sessionid.trim() } : {}),
          }),
        });
      } else {
        if (!files.length) {
          setSubmitting(false);
          return;
        }
        const fd = new FormData();
        for (const f of files) {
          fd.append("files", f);
        }
        fd.append("entries", JSON.stringify(entriesPayload));
        fd.append("topic", DEFAULT_TOPIC);
        fd.append("frames", "auto");
        fd.append("gif_sec", "4");
        fd.append("gif_ranges", "[]");
        fd.append("clips", "[]");
        fd.append("langs", JSON.stringify(targetLangs));
        fd.append("want_translate", wantTranslate ? "true" : "false");
        fd.append("want_notes", wantNotes ? "true" : "false");
        fd.append("want_dehardsub", wantDehardsub ? "true" : "false");
        fd.append("want_enhance", wantEnhance ? "true" : "false");
        fd.append("want_compress", wantCompress ? "true" : "false");
        fd.append("want_concat", wantConcat ? "true" : "false");
        fd.append("want_remix", wantRemix ? "true" : "false");
        fd.append("want_publish", wantPublish ? "true" : "false");
        fd.append(
          "media_opts",
          JSON.stringify({
            enhance_strength: enhanceStrength,
            compress_height: compressHeight,
            compress_crf: compressCrf,
            publish_platforms: publishPlatforms,
          })
        );
        fd.append("stages", stagesPayload);
        res = await fetch(`${API}/api/try/uploads`, { method: "POST", body: fd });
      }
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error((body as { detail?: string }).detail || res.statusText);
      }
      const data = (await res.json()) as Poll & {
        job_id: number;
        frame_opts?: Poll["frame_opts"];
        noop?: boolean;
        intent?: { intent?: string; reason?: string };
        langs?: string;
        batch?: boolean;
        queued?: number;
        jobs?: Array<{
          job_id?: number;
          url?: string;
          filename?: string;
          status?: string;
          path?: string;
          title?: string;
          error?: string | null;
          progress?: Progress;
          frame_opts?: FrameOptsLike;
          ok?: boolean;
        }>;
      };
      if (data.batch && typeof data.queued === "number") {
        setBatchQueued(data.queued);
      } else {
        setBatchQueued(0);
      }
      if (typeof data.langs === "string") setPrevLangs(data.langs);
      if (data.frame_opts) setPrevFrameOpts(data.frame_opts);
      else if (data.jobs?.[0]?.frame_opts) setPrevFrameOpts(data.jobs[0].frame_opts);

      const jobsRaw = Array.isArray(data.jobs) ? data.jobs : [];
      const mapped =
        jobsRaw.length > 0
          ? jobsRaw
              .filter(
                (j): j is typeof j & { job_id: number } => typeof j.job_id === "number"
              )
              .map((j) => ({
                job_id: j.job_id,
                label: j.url || j.filename || j.title || String(j.job_id),
                status: j.status || (j.ok === false ? "failed" : "pending"),
                path: j.path,
                title: j.title,
                error: j.error ?? null,
                progress: j.progress,
              }))
          : typeof data.job_id === "number"
            ? [
                {
                  job_id: data.job_id,
                  label: sourceItems[0]?.label || String(data.job_id),
                  status: data.status || "pending",
                  path: data.path,
                  title: data.title,
                  error: data.error ?? null,
                  progress: data.progress,
                },
              ]
            : [];
      setBatchJobs(mapped);
      batchJobsRef.current = mapped;

      if (data.noop) {
        setCachedDone(true);
        setSubmitting(false);
        setPoll({ ...data, status: data.status || "done" });
        return;
      }
      if (data.status === "done" || data.status === "published") {
        setCachedDone(true);
      }
      setPoll(data);
      const allTerminal =
        mapped.length > 0 && mapped.every((j) => isTerminalStatus(j.status));
      const terminal =
        allTerminal ||
        isTerminalStatus(data.status);
      if (terminal || mapped.length === 0) {
        setSubmitting(false);
      } else {
        timer.current = setInterval(() => {
          void refreshJobs();
        }, 3000);
        await refreshJobs();
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setErr(isFetchNetworkError(e) || msg.toLowerCase().includes("fetch") ? copy.apiDown : msg);
      setSubmitting(false);
    }
  }

  const inFlight =
    poll &&
    (poll.status === "pending" || poll.status === "processing");

  const submitLabel = inFlight
    ? poll.status === "processing"
      ? copy.processing
      : copy.submitting
    : copy.submit;

  const statusLabel =
    poll?.status === "done"
      ? copy.statusDone
      : poll?.status === "cancelled"
        ? copy.statusCancelled
        : poll?.status === "failed" || poll?.status === "dead"
          ? copy.statusFailed
          : poll?.status === "processing"
            ? copy.statusProcessing
            : poll
              ? copy.statusPending
              : null;

  const batchCounts = useMemo(() => {
    const total = batchJobs.length;
    const running = batchJobs.filter((j) => j.status === "processing").length;
    const queued = batchJobs.filter((j) => j.status === "pending").length;
    return { total, running, queued };
  }, [batchJobs]);

  const hasActiveBatch = batchJobs.some((j) => !isTerminalStatus(j.status));

  const progress = poll?.progress;
  const stage = stageLabel(copy, progress?.stage);
  const detail = progressDetail(copy, progress);
  const pollSourceRaw = poll?.title || batchJobs[0]?.label;
  const pollSourceLabel = pollSourceRaw
    ? formatBatchJobLabel(pollSourceRaw, locale)
    : null;
  const queueAhead = poll?.queue_ahead;
  const queueAheadProgress = queueAhead?.progress;
  const queueAheadStage = stageLabel(copy, queueAheadProgress?.stage);
  const queueAheadLabel = queueAhead?.title
    ? formatBatchJobLabel(queueAhead.title, locale)
    : null;
  const queueAheadPct = queueAheadProgress?.percent ?? 0;

  return (
    <div className="try-panel">
      <div className="try-workflow-rail-wrap">
      <ol className="try-workflow-rail" aria-label={copy.modulesSection}>
        <li
          className={sourceItems.length ? "is-done" : "is-active"}
          aria-current={!sourceItems.length ? "step" : undefined}
        >
          <span className="try-workflow-rail-n" aria-hidden="true">
            1
          </span>
          <span className="try-workflow-rail-label">{copy.workflowRailSource}</span>
        </li>
        {(
          [
            {
              id: "translate",
              on: wantTranslate,
              set: setWantTranslate,
              label: copy.workflowRailTranslate,
            },
            {
              id: "notes",
              on: wantNotes,
              set: setWantNotes,
              label: copy.workflowRailNotes,
            },
            {
              id: "clips",
              on: wantClips,
              set: setWantClips,
              label: copy.workflowRailClips,
            },
            {
              id: "dehardsub",
              on: wantDehardsub,
              set: setWantDehardsub,
              label: copy.workflowRailDehardsub,
            },
            {
              id: "enhance",
              on: wantEnhance,
              set: setWantEnhance,
              label: copy.workflowRailEnhance,
            },
            {
              id: "compress",
              on: wantCompress,
              set: setWantCompress,
              label: copy.workflowRailCompress,
            },
            {
              id: "concat",
              on: wantConcat,
              set: setWantConcat,
              label: copy.workflowRailConcat,
            },
            {
              id: "remix",
              on: wantRemix,
              set: setWantRemix,
              label: copy.workflowRailRemix,
            },
            {
              id: "publish",
              on: wantPublish,
              set: setWantPublish,
              label: copy.workflowRailPublish,
            },
          ] as const
        ).map((wf) => (
          <li key={wf.id} className={wf.on ? "is-on" : "is-off"}>
            <button
              type="button"
              className="try-workflow-rail-toggle"
              aria-pressed={wf.on}
              disabled={submitting}
              onClick={() => wf.set((v) => !v)}
            >
              <span className="try-workflow-rail-n" aria-hidden="true">
                {wf.on ? "✓" : "–"}
              </span>
              <span className="try-workflow-rail-label">{wf.label}</span>
            </button>
          </li>
        ))}
      </ol>
      <p className="try-workflow-rail-hint">{copy.workflowRailHint}</p>
      </div>
      <div className="try-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          className={tab === "url" ? "try-tab active" : "try-tab"}
          aria-selected={tab === "url"}
          onClick={() => setTab("url")}
        >
          {copy.tabUrl}
        </button>
        <button
          type="button"
          role="tab"
          className={tab === "upload" ? "try-tab active" : "try-tab"}
          aria-selected={tab === "upload"}
          onClick={() => setTab("upload")}
        >
          {copy.tabUpload}
        </button>
      </div>

      <form className="try-form" onSubmit={onSubmit}>
        <section className="try-section try-source">
          <header className="try-section-head">
            <h2 className="try-section-title">{copy.sourceSection}</h2>
          </header>

          {tab === "url" ? (
            <div className="try-source-grid">
              <label className="try-field try-field-url">
                <span>{copy.urlLabel}</span>
                <textarea
                  name="urls"
                  rows={4}
                  value={urlsText}
                  onChange={(e) => setUrlsText(e.target.value)}
                  placeholder={copy.urlPlaceholder}
                  disabled={submitting}
                  required
                  spellCheck={false}
                />
                <p className="try-hint">{copy.urlHint}</p>
                {parsedUrls.length > 0 ? (
                  <p className="try-url-summary" role="status">
                    {fillCopy(copy.urlsParsed, { n: parsedUrls.length })}
                    {(urlProbe?.invalid_count ?? 0) > 0
                      ? ` · ${fillCopy(copy.urlsInvalid, { n: urlProbe?.invalid_count ?? 0 })}`
                      : null}
                  </p>
                ) : null}
              </label>
              <div className="try-cookies">
                {urlProbe?.platforms && Object.keys(urlProbe.platforms).length > 0 ? (
                  <div className="try-cookie-platforms" role="status">
                    <p className="try-cookie-platforms-title">{copy.cookiesPlatformTitle}</p>
                    <ul className="try-cookie-platform-list">
                      {Object.entries(urlProbe.platforms).map(([key, p]) => {
                        const label = platformDisplay(p.label || key, locale);
                        const line =
                          p.required && !p.ok
                            ? copy.cookiesPlatformRequired
                            : p.required && p.ok
                              ? copy.cookiesPlatformReady
                              : p.soft
                                ? copy.cookiesPlatformSoft
                                : copy.cookiesPlatformOptional;
                        const tone =
                          p.required && !p.ok
                            ? "is-error"
                            : p.ok
                              ? "is-ok"
                              : "is-neutral";
                        return (
                          <li key={key} className={`try-cookie-platform ${tone}`}>
                            {fillCopy(line, { platform: label, n: p.count })}
                          </li>
                        );
                      })}
                    </ul>
                  </div>
                ) : null}
                {cookiesBlockSubmit ? (
                  <p className="try-error" role="alert">
                    {urlProbe?.message ||
                      fillCopy(copy.cookiesBlockBatch, {
                        platforms: Object.values(urlProbe?.platforms || {})
                          .filter((p) => p.required && !p.ok)
                          .map((p) => platformDisplay(p.label, locale))
                          .join(locale.startsWith("zh") ? "、" : ", "),
                      })}
                  </p>
                ) : cookiesReady && parsedUrls.some((u) => urlNeedsCookies(u)) ? (
                  <p className="try-hint" role="status">
                    {copy.cookiesOk}
                  </p>
                ) : null}
                {urlProbe?.cookies_local && !sessionid.trim() ? (
                  <p className="try-hint">{copy.cookiesLocalDetected}</p>
                ) : null}
                <label className="try-field">
                  <span>{copy.sessionidLabel}</span>
                  <textarea
                    name="sessionid"
                    rows={3}
                    autoComplete="off"
                    spellCheck={false}
                    value={sessionid}
                    onChange={(e) => setSessionid(e.target.value)}
                    placeholder={copy.sessionidPlaceholder}
                    disabled={submitting}
                    required={cookiesBlockSubmit}
                  />
                </label>
                <p className="try-hint">{copy.cookiesHint}</p>
              </div>
            </div>
          ) : (
            <div className="try-field try-upload-field">
              <span className="try-upload-label">{copy.fileLabel}</span>
              <input
                ref={fileInputRef}
                className="try-dropzone-input"
                type="file"
                accept="video/*,audio/*,.mp4,.mov,.mkv,.webm,.m4a,.wav"
                multiple
                disabled={submitting}
                required={files.length === 0}
                tabIndex={-1}
                aria-hidden="true"
                onChange={(e) => {
                  mergePickedFiles(e.target.files ?? []);
                  // Allow picking the same file again later.
                  e.target.value = "";
                }}
              />
              <button
                type="button"
                className={
                  fileDragOver
                    ? "try-dropzone is-drag"
                    : files.length
                      ? "try-dropzone has-files"
                      : "try-dropzone"
                }
                disabled={submitting}
                aria-label={copy.fileLabel}
                onClick={() => {
                  if (submitting) return;
                  fileInputRef.current?.click();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    if (!submitting) fileInputRef.current?.click();
                  }
                }}
                onDragEnter={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  if (!submitting) setFileDragOver(true);
                }}
                onDragOver={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  if (!submitting) setFileDragOver(true);
                }}
                onDragLeave={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  const next = e.relatedTarget as Node | null;
                  if (next && e.currentTarget.contains(next)) return;
                  setFileDragOver(false);
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  e.stopPropagation();
                  setFileDragOver(false);
                  if (submitting) return;
                  mergePickedFiles(e.dataTransfer.files);
                }}
              >
                <span className="try-dropzone-face">
                  <span className="try-dropzone-icon" aria-hidden="true">
                    <svg viewBox="0 0 48 48" width="40" height="40" fill="none">
                      <rect
                        x="8"
                        y="12"
                        width="32"
                        height="24"
                        rx="4"
                        stroke="currentColor"
                        strokeWidth="2"
                      />
                      <path
                        d="M16 28l6-7 5 5 3-3 6 7"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                      <circle cx="19" cy="20" r="2.2" fill="currentColor" />
                      <path
                        d="M24 6v10M20 10l4-4 4 4"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </span>
                  <span className="try-dropzone-title">{copy.filesDropTitle}</span>
                  <span className="try-dropzone-browse">{copy.filesDropBrowse}</span>
                  <span className="try-dropzone-types">{copy.filesDropTypes}</span>
                </span>
              </button>
              {files.length > 0 ? (
                <ul className="try-file-list" aria-label={copy.fileLabel}>
                  {files.map((f, i) => (
                    <li key={sourceKeyFile(f)} className="try-file-chip">
                      <span className="try-file-chip-icon" aria-hidden="true">
                        ▶
                      </span>
                      <span className="try-file-chip-meta">
                        <span className="try-file-chip-name" title={f.name}>
                          {f.name}
                        </span>
                        <span className="try-file-chip-size">{formatFileSize(f.size)}</span>
                      </span>
                      <button
                        type="button"
                        className="try-file-chip-remove"
                        disabled={submitting}
                        onClick={() => removeFileAt(i)}
                        aria-label={copy.clipsRemove}
                      >
                        ×
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
              <div className="try-upload-meta">
                <p className="try-hint">{copy.filesHint}</p>
                {files.length > 0 ? (
                  <div className="try-upload-meta-actions">
                    <p className="try-url-summary" role="status">
                      {fillCopy(copy.filesParsed, { n: files.length })}
                    </p>
                    <button
                      type="button"
                      className="try-file-clear"
                      disabled={submitting}
                      onClick={clearFiles}
                    >
                      {copy.filesClear}
                    </button>
                  </div>
                ) : null}
              </div>
            </div>
          )}
        </section>

        {wantClips || wantConcat ? (
          <section className="try-section try-outputs">
            <header className="try-section-head">
              <h2 className="try-section-title">{copy.clipsSection}</h2>
              {sourceItems.length > 1 ? (
                <button
                  type="button"
                  className="try-sync-first"
                  disabled={submitting}
                  onClick={syncFirstToAll}
                >
                  {copy.applyDefaultsAll}
                </button>
              ) : null}
            </header>

            {sourceItems.length === 0 ? (
              <div className="try-workflow-empty" role="status">
                <p className="try-hint">{copy.workflowMediaEmpty}</p>
              </div>
            ) : (
              <div className="try-link-media">
                {sourceItems.length > 1 ? (
                  <p className="try-media-sub">
                    {copy.workflowMediaHint}
                    <span className="try-link-media-count">{sourceItems.length}</span>
                  </p>
                ) : null}
                {sourceItems.map(({ key, label }) => {
                  const sm = sourceMedia[key] || defaultSourceMedia("auto");
                  const dur = sourceDurations[key] ?? videoDur;
                  const itemMax = clipLimit(dur);
                  const errs = sourceMediaErrors(key);
                  return (
                    <TrySourceMediaCard
                      key={key}
                      sourceKey={key}
                      label={label}
                      copy={copy}
                      media={sm}
                      clipMax={itemMax}
                      loading={submitting}
                      gifError={errs.gif}
                      clipError={errs.clip}
                      onToggle={() => patchSourceMedia(key, { open: !sm.open })}
                      onFrames={(value) => patchSourceMedia(key, { frames: value })}
                      onRange={(which, index, field, raw) =>
                        setSourceRangeField(key, which, index, field, raw)
                      }
                      onAddRange={(which) => addSourceRange(key, which)}
                      onRemoveRange={(which, index) =>
                        removeSourceRange(key, which, index)
                      }
                    />
                  );
                })}
              </div>
            )}
          </section>
        ) : null}

        {wantEnhance || wantCompress || wantConcat || wantRemix || wantPublish ? (
          <section className="try-section try-postproc">
            <header className="try-section-head">
              <h2 className="try-section-title">
                {[
                  wantEnhance ? copy.enhanceSection : null,
                  wantCompress ? copy.compressSection : null,
                  wantConcat ? copy.workflowRailConcat : null,
                  wantRemix ? copy.workflowRailRemix : null,
                  wantPublish ? copy.workflowRailPublish : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </h2>
            </header>
            {wantConcat ? <p className="try-hint">{copy.concatHint}</p> : null}
            {wantConcat && concatMissingRanges ? (
              <p className="try-error">{copy.concatNeedTwo}</p>
            ) : null}
            {wantEnhance ? (
              <div className="try-field">
                <span>{copy.enhanceStrength}</span>
                <p className="try-hint">{copy.enhanceHint}</p>
                <div className="try-frames-options try-postproc-options">
                  {(
                    [
                      ["light", copy.enhanceLight],
                      ["medium", copy.enhanceMedium],
                      ["strong", copy.enhanceStrong],
                    ] as const
                  ).map(([id, label]) => (
                    <label key={id} className={enhanceStrength === id ? "is-on" : undefined}>
                      <input
                        type="radio"
                        name="enhance-strength"
                        checked={enhanceStrength === id}
                        disabled={submitting}
                        onChange={() => setEnhanceStrength(id)}
                      />
                      <span>{label}</span>
                    </label>
                  ))}
                </div>
              </div>
            ) : null}
            {wantCompress ? (
              <div className="try-field">
                <span>{copy.compressSection}</span>
                <p className="try-hint">{copy.compressHint}</p>
                <label className="try-select-row">
                  <span>{copy.compressHeight}</span>
                  <select
                    value={compressHeight}
                    disabled={submitting}
                    onChange={(e) => setCompressHeight(Number(e.target.value))}
                  >
                    <option value={0}>{copy.compressKeep}</option>
                    <option value={720}>720p</option>
                    <option value={1080}>1080p</option>
                  </select>
                </label>
                <label className="try-select-row">
                  <span>{copy.compressCrf}</span>
                  <input
                    type="number"
                    min={18}
                    max={32}
                    step={1}
                    value={compressCrf}
                    disabled={submitting}
                    onChange={(e) => setCompressCrf(Number(e.target.value) || 28)}
                  />
                </label>
              </div>
            ) : null}
            {wantRemix ? (
              <div className="try-field">
                <span>{copy.remixSection}</span>
                <p className="try-hint">{copy.remixHint}</p>
              </div>
            ) : null}
            {wantPublish ? (
              <div className="try-field">
                <span>{copy.publishSection}</span>
                <p className="try-hint">{copy.publishHint}</p>
                {validPublish.length === 0 ? (
                  <p className="try-error">{copy.accountNeedBind}</p>
                ) : (
                  <div className="try-frames-options try-postproc-options">
                    {validPublish.map((slot) => (
                      <label
                        key={slot.platform}
                        className={publishPlatforms.includes(slot.platform) ? "is-on" : undefined}
                      >
                        <input
                          type="checkbox"
                          checked={publishPlatforms.includes(slot.platform)}
                          disabled={submitting}
                          onChange={() =>
                            setPublishPlatforms((prev) =>
                              prev.includes(slot.platform)
                                ? prev.filter((p) => p !== slot.platform)
                                : [...prev, slot.platform]
                            )
                          }
                        />
                        <span>
                          {slot.platform === "douyin"
                            ? copy.accountDouyin
                            : slot.platform === "kuaishou"
                              ? copy.accountKuaishou
                              : slot.platform}
                        </span>
                      </label>
                    ))}
                  </div>
                )}
              </div>
            ) : null}
          </section>
        ) : null}

        {(wantTranslate || wantNotes) ? (
          <section className="try-section try-langs">
            <header className="try-section-head">
              <h2 className="try-section-title">{copy.langsSection}</h2>
            </header>
            <p className="try-media-sub">{copy.langsHint}</p>
            <div className="try-langs-dd" ref={langsDdRef}>
              <button
                type="button"
                className={langsOpen ? "try-langs-trigger is-open" : "try-langs-trigger"}
                aria-expanded={langsOpen}
                aria-haspopup="dialog"
                disabled={submitting}
                onClick={() => setLangsOpen((v) => !v)}
              >
                <span className="try-langs-trigger-main">
                  <span className="try-langs-trigger-label">{copy.langsPick}</span>
                  <span className="try-langs-trigger-value">{langsSummary}</span>
                </span>
                <span className="try-langs-trigger-meta">
                  <span className="try-langs-count">
                    {targetLangs.length}/{locales.length}
                  </span>
                  <span className="try-langs-chevron" aria-hidden="true">
                    ▾
                  </span>
                </span>
              </button>
              {langsOpen ? (
                <>
                  <button
                    type="button"
                    className="try-langs-scrim"
                    aria-label={copy.langsDone}
                    onClick={() => setLangsOpen(false)}
                  />
                  <div
                    className="try-langs-bubble"
                    role="dialog"
                    aria-label={copy.langsSection}
                  >
                    <span className="try-langs-bubble-handle" aria-hidden="true" />
                    <div className="try-langs-bubble-bar">
                      <button
                        type="button"
                        className="try-lang-action"
                        disabled={submitting || allLangsSelected}
                        onClick={() => setTargetLangs([...locales])}
                      >
                        {copy.langsSelectAll}
                      </button>
                      <button
                        type="button"
                        className="try-lang-action"
                        disabled={submitting || targetLangs.length === 0}
                        onClick={() => setTargetLangs([])}
                      >
                        {copy.langsClear}
                      </button>
                      <button
                        type="button"
                        className="try-lang-action try-lang-done"
                        onClick={() => setLangsOpen(false)}
                      >
                        {copy.langsDone}
                      </button>
                    </div>
                    <div
                      className="try-langs-grid"
                      role="group"
                      aria-label={copy.langsSection}
                    >
                      {locales.map((code) => {
                        const on = targetLangs.includes(code);
                        return (
                          <label key={code} className={on ? "is-on" : undefined}>
                            <input
                              type="checkbox"
                              checked={on}
                              disabled={submitting}
                              onChange={() => toggleLang(code)}
                            />
                            <span>
                              <em>{localeNames[code]}</em>
                              <small>{code}</small>
                            </span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                </>
              ) : null}
            </div>
            {targetLangs.length === 0 ? (
              <p className="try-error">{copy.langsNeedOne}</p>
            ) : cachedDone ? (
              <p className="try-frames-hint" role="status">
                {copy.intentCached}
              </p>
            ) : null}
          </section>
        ) : null}

        <section className="try-section try-accounts">
          <header className="try-section-head">
            <h2 className="try-section-title">{copy.accountsSection}</h2>
          </header>
          <p className="try-hint">{copy.accountsHint}</p>
          <div className="try-account-grid">
            {(accountSlots.length
              ? accountSlots
              : [
                  { platform: "douyin", label: "", account_id: "", status: "unbound" as const, bound: false },
                  { platform: "kuaishou", label: "", account_id: "", status: "unbound" as const, bound: false },
                ]
            ).map((slot) => {
              const draft = accountDrafts[slot.platform] || {
                secret: "",
                label: slot.label,
                account_id: slot.account_id,
              };
              const name =
                slot.platform === "douyin"
                  ? copy.accountDouyin
                  : slot.platform === "kuaishou"
                    ? copy.accountKuaishou
                    : slot.platform;
              const statusText =
                slot.status === "valid"
                  ? copy.accountValid
                  : slot.status === "invalid"
                    ? copy.accountInvalid
                    : copy.accountUnbound;
              return (
                <div key={slot.platform} className="try-account-card">
                  <div className="try-select-row">
                    <strong>{name}</strong>
                    <span className={`try-account-status is-${slot.status}`}>{statusText}</span>
                  </div>
                  <label className="try-field">
                    <span>{copy.accountLabel}</span>
                    <input
                      value={draft.label}
                      disabled={submitting || accountBusy === slot.platform}
                      onChange={(e) =>
                        setAccountDrafts((prev) => ({
                          ...prev,
                          [slot.platform]: { ...draft, label: e.target.value },
                        }))
                      }
                    />
                  </label>
                  <label className="try-field">
                    <span>{copy.accountId}</span>
                    <input
                      value={draft.account_id}
                      disabled={submitting || accountBusy === slot.platform}
                      onChange={(e) =>
                        setAccountDrafts((prev) => ({
                          ...prev,
                          [slot.platform]: { ...draft, account_id: e.target.value },
                        }))
                      }
                    />
                  </label>
                  <label className="try-field">
                    <span>{copy.accountSecret}</span>
                    <textarea
                      rows={3}
                      value={draft.secret}
                      placeholder={slot.bound ? "••••" : ""}
                      disabled={submitting || accountBusy === slot.platform}
                      onChange={(e) =>
                        setAccountDrafts((prev) => ({
                          ...prev,
                          [slot.platform]: { ...draft, secret: e.target.value },
                        }))
                      }
                    />
                  </label>
                  <div className="try-actions-buttons">
                    <button
                      type="button"
                      className="watch-btn"
                      disabled={submitting || accountBusy === slot.platform || !draft.secret.trim()}
                      onClick={async () => {
                        setAccountBusy(slot.platform);
                        setErr(null);
                        try {
                          const r = await fetch(`${API}/api/try/accounts`, {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({
                              platform: slot.platform,
                              secret: draft.secret,
                              label: draft.label,
                              account_id: draft.account_id,
                            }),
                          });
                          if (!r.ok) {
                            const body = (await r.json().catch(() => ({}))) as { detail?: string };
                            throw new Error(body.detail || r.statusText);
                          }
                          setAccountDrafts((prev) => ({
                            ...prev,
                            [slot.platform]: { ...draft, secret: "" },
                          }));
                          await loadAccounts();
                        } catch (e) {
                          setErr(e instanceof Error ? e.message : String(e));
                        } finally {
                          setAccountBusy(null);
                        }
                      }}
                    >
                      {copy.accountBind}
                    </button>
                    {slot.bound ? (
                      <button
                        type="button"
                        className="watch-btn secondary"
                        disabled={submitting || accountBusy === slot.platform}
                        onClick={async () => {
                          setAccountBusy(slot.platform);
                          setErr(null);
                          try {
                            const r = await fetch(
                              `${API}/api/try/accounts/${encodeURIComponent(slot.platform)}`,
                              { method: "DELETE" }
                            );
                            if (!r.ok) {
                              const body = (await r.json().catch(() => ({}))) as {
                                detail?: string;
                              };
                              throw new Error(body.detail || r.statusText);
                            }
                            await loadAccounts();
                          } catch (e) {
                            setErr(e instanceof Error ? e.message : String(e));
                          } finally {
                            setAccountBusy(null);
                          }
                        }}
                      >
                        {copy.accountUnbind}
                      </button>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        <div className="try-actions">
          <p className="try-intent" role="status">
            {plannedIntentText}
          </p>
          <div className="try-actions-buttons">
            <button
              type="button"
              className="watch-btn secondary try-reset"
              onClick={resetForm}
              disabled={submitting}
            >
              {copy.reset}
            </button>
            <button
              type="submit"
              className="watch-btn try-submit"
              disabled={
                submitting ||
                Boolean(linkRangeError) ||
                sourceItems.length === 0 ||
                !hasAnyModule ||
                ((wantTranslate || wantNotes) && targetLangs.length === 0) ||
                concatMissingRanges ||
                publishBlocked ||
                (plannedIntent === "noop" && sourceItems.length <= 1) ||
                (tab === "url" && cookiesBlockSubmit)
              }
            >
              {submitLabel}
            </button>
          </div>
          {batchQueued > 1 ? (
            <p className="try-hint try-batch-queued" role="status">
              {fillCopy(copy.batchQueued, { n: batchQueued })}
            </p>
          ) : null}
        </div>
      </form>

      {err ? <p className="try-error">{err}</p> : null}

      {poll ? (
        <div className="try-status" aria-live="polite">
          <p className="try-status-head">
            <strong>{statusLabel}</strong>
            {pollSourceLabel ? (
              <span className="try-status-source" title={pollSourceRaw}>
                {" "}
                · {pollSourceLabel}
              </span>
            ) : null}
          </p>

          {progress &&
          (poll.status === "pending" || poll.status === "processing") ? (
            <div className="try-progress">
              <div
                className="try-progress-bar"
                role="progressbar"
                aria-valuenow={progress.percent}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-label={stage}
              >
                <div
                  className="try-progress-fill"
                  style={{ width: `${Math.max(progress.percent, 2)}%` }}
                />
              </div>
              <p className="try-progress-meta">
                <span>{fillCopy(copy.progressNow, { stage })}</span>
                {detail ? <span>{detail}</span> : null}
                <span>{progress.percent}%</span>
              </p>
              {progress.active ? (
                <p className="try-progress-active">{copy.progressActive}</p>
              ) : progress.stale ? (
                <p className="try-progress-stale">{copy.progressStale}</p>
              ) : poll.status === "pending" ? (
                <p className="try-progress-wait">{copy.stageQueued}</p>
              ) : progress.stage === "transcribe" && progress.percent <= 20 ? (
                <p className="try-progress-wait">{copy.stageTranscribe}</p>
              ) : null}
            </div>
          ) : null}

          {poll.busy && poll.status === "pending" ? (
            <>
              {poll.queue_position && poll.queue_position > 1 ? (
                <p className="try-hint">
                  {fillCopy(copy.queuePosition, { n: poll.queue_position })}
                </p>
              ) : (
                <p className="try-hint">{copy.busy}</p>
              )}
              {queueAhead && queueAheadProgress ? (
                <div className="try-queue-ahead">
                  <p className="try-queue-ahead-label">
                    {fillCopy(copy.queueAhead, {
                      title: queueAheadLabel || String(queueAhead.job_id),
                      stage: queueAheadStage,
                      percent: queueAheadPct,
                    })}
                  </p>
                  <div
                    className="try-progress-bar try-queue-ahead-bar"
                    role="progressbar"
                    aria-valuenow={queueAheadPct}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-label={queueAheadStage}
                  >
                    <div
                      className="try-progress-fill"
                      style={{ width: `${Math.max(queueAheadPct, 2)}%` }}
                    />
                  </div>
                </div>
              ) : null}
            </>
          ) : null}

          {poll.error &&
          (poll.status === "failed" ||
            poll.status === "dead" ||
            poll.status === "cancelled") ? (
            <p className="try-error">{poll.error}</p>
          ) : null}
          {poll.status === "done" && poll.derived ? (
            <div className="try-derived">
              <p className="try-derived-title">{copy.derivedTitle}</p>
              {poll.derived.remix ? (
                <RemixPlayer
                  src={poll.derived.remix}
                  cuesUrl={poll.derived.remix_cues}
                  vttUrl={poll.derived.remix_vtt}
                  label={copy.remixSection}
                />
              ) : null}
              <ul>
                {poll.derived.concat ? (
                  <li>
                    <a href={poll.derived.concat} download>
                      {copy.derivedConcat}
                    </a>
                  </li>
                ) : null}
                {poll.derived.enhance ? (
                  <li>
                    <a href={poll.derived.enhance} download>
                      {copy.derivedEnhance}
                    </a>
                  </li>
                ) : null}
                {poll.derived.compress ? (
                  <li>
                    <a href={poll.derived.compress} download>
                      {copy.derivedCompress}
                    </a>
                  </li>
                ) : null}
                {poll.derived.remix ? (
                  <li>
                    <a href={poll.derived.remix} download>
                      {copy.derivedRemix}
                    </a>
                  </li>
                ) : null}
                {poll.derived.remix_vtt ? (
                  <li>
                    <a href={poll.derived.remix_vtt} download>
                      {copy.derivedRemixVtt}
                    </a>
                  </li>
                ) : null}
                {poll.derived.publish ? (
                  <li>
                    <a href={poll.derived.publish} download>
                      {copy.derivedPublish}
                    </a>
                  </li>
                ) : null}
              </ul>
            </div>
          ) : null}
          {poll.status === "done" && poll.path && batchJobs.length <= 1 ? (
            <p>
              {poll.cached ? (
                <span className="try-hint">{copy.alreadyDone} </span>
              ) : null}
              <Link className="watch-btn" href={localePath(locale, poll.path)}>
                {copy.openNotes}
              </Link>
            </p>
          ) : null}
        </div>
      ) : null}

      {batchJobs.length > 0 ? (
        <div className="try-batch-jobs-wrap">
          <div className="try-batch-jobs-head">
            <span>{copy.batchJobsTitle}</span>
            <span className="try-batch-jobs-count">{batchJobs.length}</span>
            {batchCounts.total > 1 ? (
              <span className="try-batch-jobs-summary">
                {fillCopy(copy.batchSummary, batchCounts)}
              </span>
            ) : null}
            {hasActiveBatch ? (
              <button
                type="button"
                className="watch-btn secondary try-batch-queue-btn"
                onClick={() => void toggleQueuePause()}
              >
                {queuePaused ? copy.queueResume : copy.queuePause}
              </button>
            ) : null}
          </div>
          {queuePaused && hasActiveBatch ? (
            <p className="try-hint try-queue-paused">{copy.queuePaused}</p>
          ) : null}
          <ul className="try-batch-jobs" aria-label={copy.batchJobsTitle}>
            {batchJobs.map((j) => {
              const label = formatBatchJobLabel(j.title || j.label, locale);
              const stage = stageLabel(copy, j.progress?.stage);
              const detail = progressDetail(copy, j.progress);
              const pct = j.progress?.percent ?? 0;
              const showProgress =
                Boolean(j.progress) &&
                (j.status === "pending" || j.status === "processing");
              const canCancel = j.status === "pending" || j.status === "processing";
              const canRetry =
                j.status === "failed" ||
                j.status === "dead" ||
                j.status === "cancelled";
              return (
                <li key={j.job_id} className="try-batch-job">
                  <div className="try-batch-job-head">
                    <span className="try-batch-jobs-label" title={j.label}>
                      {label}
                    </span>
                    <span className={`try-batch-jobs-badge ${badgeClass(j.status)}`}>
                      {jobStatusLabel(copy, j.status)}
                    </span>
                    <div className="try-batch-job-actions">
                      {canCancel ? (
                        <button
                          type="button"
                          className="watch-btn secondary try-batch-job-btn"
                          disabled={jobActionBusy === j.job_id}
                          onClick={() => void runJobAction(j.job_id, "cancel")}
                        >
                          {copy.jobCancel}
                        </button>
                      ) : null}
                      {canRetry ? (
                        <button
                          type="button"
                          className="watch-btn secondary try-batch-job-btn"
                          disabled={jobActionBusy === j.job_id}
                          onClick={() => void runJobAction(j.job_id, "retry")}
                        >
                          {copy.jobRetry}
                        </button>
                      ) : null}
                    </div>
                  </div>
                  {showProgress ? (
                    <div className="try-batch-job-progress">
                      <div
                        className="try-progress-bar"
                        role="progressbar"
                        aria-valuenow={pct}
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-label={stage}
                      >
                        <div
                          className="try-progress-fill"
                          style={{ width: `${Math.max(pct, 2)}%` }}
                        />
                      </div>
                      <p className="try-batch-job-progress-meta">
                        <span>{fillCopy(copy.progressNow, { stage })}</span>
                        {detail ? <span>{detail}</span> : null}
                        <span>{pct}%</span>
                      </p>
                    </div>
                  ) : null}
                  {j.path && j.status === "done" ? (
                    <p className="try-batch-job-done">
                      <Link href={localePath(locale, j.path)}>{copy.openNotes}</Link>
                    </p>
                  ) : null}
                  {j.error && (j.status === "failed" || j.status === "dead" || j.status === "cancelled") ? (
                    <p className="try-error try-batch-job-error">{j.error}</p>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
