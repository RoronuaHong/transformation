/** Same-origin proxy to materials_hub Python API (:8000). */
export function hubApiBase(): string {
  const env = process.env.NEXT_PUBLIC_HUB_API_URL?.replace(/\/$/, "");
  if (env) return env;
  return "/hub-api";
}

/**
 * Uploads must hit hub :8000 directly — Next rewrites blow up on large
 * multipart bodies (e.g. ~360MB video → bare "Internal Server Error").
 */
export function hubUploadUrl(): string {
  const env = process.env.NEXT_PUBLIC_HUB_UPLOAD_URL?.replace(/\/$/, "");
  if (env) return env;
  const upstream = (
    process.env.NEXT_PUBLIC_HUB_UPSTREAM || "http://127.0.0.1:8000"
  ).replace(/\/$/, "");
  return `${upstream}/api/upload`;
}

export function hubFileUrl(id: string): string {
  return `${hubApiBase()}/file/${encodeURIComponent(id)}`;
}

export function hubThumbUrl(id: string): string {
  return `${hubApiBase()}/thumb/${encodeURIComponent(id)}`;
}

export type HubMaterial = {
  id: string;
  kind: string;
  ext?: string;
  name: string;
  size: number;
  tags?: string;
  description?: string;
  source?: string;
  location?: string;
  external_path?: string;
  rel_path?: string;
  created_at?: string;
  thumb?: boolean;
  missing?: boolean;
};

export type HubStats = {
  total: number;
  dupes: number;
  kinds: Record<string, number>;
  thumbs?: { ffmpeg?: boolean };
  external?: { external?: number; broken?: number };
};

export type HubTag = {
  tag: string;
  count: number;
  label?: string;
  group?: string;
  hint?: string;
};

/** 系统面标签人读文案(与 core.tag_ui_meta 对齐;卡片 chip 用)。 */
const TAG_EXACT_ZH: Record<string, string> = {
  sp: "来源·流水线",
  upload: "来源·上传",
  bilibili: "平台·B站",
  youtube: "平台·YouTube",
  demux: "拆条产物",
  asr: "ASR 音轨",
};
const TAG_EXACT_EN: Record<string, string> = {
  sp: "src·pipeline",
  upload: "src·upload",
  bilibili: "platform·Bilibili",
  youtube: "platform·YouTube",
  demux: "demuxed",
  asr: "ASR audio",
};
const TAG_TYPE_ZH: Record<string, string> = {
  media: "源片",
  clip: "物理切片",
  subs: "字幕",
  notes: "笔记",
  benchmark: "基准样例",
  render: "渲染预览",
  test: "测试样例",
  batch: "任务杂项",
  other: "其它",
};
const TAG_TYPE_EN: Record<string, string> = {
  media: "source media",
  clip: "export clip",
  subs: "captions",
  notes: "notes",
  benchmark: "benchmark",
  render: "render",
  test: "test",
  batch: "job misc",
  other: "other",
};
const TAG_ROLE_ZH: Record<string, string> = {
  master: "母版",
  clip: "物理切片",
  "silent-picture": "无声画面",
  "audio-stem": "音轨组件",
  picture: "画面轨",
};
const TAG_ROLE_EN: Record<string, string> = {
  master: "master",
  clip: "export clip",
  "silent-picture": "silent picture",
  "audio-stem": "audio stem",
  picture: "picture track",
};

const HIDE_CHIP = /^(from:|t_start:|t_end:|has_audio:|role:picture$)/;

export function formatHubTag(tag: string, locale = "zh"): string {
  const t = (tag || "").trim();
  if (!t) return "";
  const zh = locale.toLowerCase().startsWith("zh");
  const exact = zh ? TAG_EXACT_ZH : TAG_EXACT_EN;
  if (exact[t]) return exact[t];
  if (t.startsWith("type:")) {
    const v = t.slice(5);
    const map = zh ? TAG_TYPE_ZH : TAG_TYPE_EN;
    return `${zh ? "类型·" : "type·"}${map[v] || v}`;
  }
  if (t.startsWith("role:")) {
    const v = t.slice(5);
    const map = zh ? TAG_ROLE_ZH : TAG_ROLE_EN;
    return `${zh ? "角色·" : "role·"}${map[v] || v}`;
  }
  if (t.startsWith("job:")) {
    const v = t.slice(4);
    const short = v.length <= 10 ? v : `${v.slice(0, 8)}…`;
    return `${zh ? "任务·" : "job·"}${short}`;
  }
  if (t.startsWith("parent:")) {
    const v = t.slice(7);
    const short = v.length <= 10 ? v : `${v.slice(0, 8)}…`;
    return `${zh ? "父素材·" : "parent·"}${short}`;
  }
  if (t.startsWith("lang:")) {
    return `${zh ? "语言·" : "lang·"}${t.slice(5)}`;
  }
  return t;
}

export function chipTagsVisible(tagsCsv: string | undefined): string[] {
  return (tagsCsv || "")
    .split(",")
    .map((x) => x.trim())
    .filter((x) => x && !HIDE_CHIP.test(x));
}

export async function hubFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const base = hubApiBase();
  const url = path.startsWith("http") ? path : `${base}${path.startsWith("/") ? "" : "/"}${path}`;
  const r = await fetch(url, init);
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(text || `hub ${r.status}`);
  }
  if (r.status === 204) return undefined as T;
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("application/json")) return (await r.json()) as T;
  return undefined as T;
}

/** Multipart upload via direct hub (avoids Next /hub-api body limit). */
export async function hubUploadFile(
  file: File,
): Promise<{ status?: string; id?: string; path?: string; error?: string }> {
  const fd = new FormData();
  fd.append("file", file, file.name);
  const r = await fetch(hubUploadUrl(), {
    method: "POST",
    body: fd,
  });
  const text = await r.text().catch(() => "");
  let data: {
    status?: string;
    id?: string;
    path?: string;
    error?: string;
    detail?: string;
  } = {};
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { error: text || `upload ${r.status}` };
  }
  if (!r.ok) {
    throw new Error(data.detail || data.error || text || `upload ${r.status}`);
  }
  if (!data.id) {
    throw new Error(data.error || "upload: no id");
  }
  return data;
}
