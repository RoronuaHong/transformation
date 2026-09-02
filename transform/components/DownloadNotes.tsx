"use client";

import type { ArticleCopy } from "@/lib/copy";
import type { KeyPoint } from "@/lib/note-points";

export type NotesDownloadPayload = {
  title?: string;
  one_liner?: string;
  summary?: string;
  focuses?: KeyPoint[];
  key_points?: KeyPoint[];
  hard_points?: KeyPoint[];
};

export type NotesDownloadHeadings = {
  overview: string;
  focuses: string;
  keyPoints: string;
  hardPoints: string;
};

const DEFAULT_HEADINGS: NotesDownloadHeadings = {
  overview: "Overview",
  focuses: "Core points",
  keyPoints: "Key points",
  hardPoints: "Watch-outs",
};

function stripMedia(points: KeyPoint[] | undefined): { title: string; detail: string }[] {
  return (points || [])
    .map((p) => ({
      title: (p.title || "").trim(),
      detail: (p.detail || "").trim(),
    }))
    .filter((p) => p.title);
}

function appendPointSection(
  lines: string[],
  heading: string,
  points: { title: string; detail: string }[]
) {
  if (!points.length) return;
  lines.push(`## ${heading}`, "");
  for (const p of points) {
    lines.push(p.detail ? `- **${p.title}** — ${p.detail}` : `- **${p.title}**`);
  }
  lines.push("");
}

export function formatNotesMarkdown(
  notes: NotesDownloadPayload,
  headings: NotesDownloadHeadings = DEFAULT_HEADINGS
): string {
  const lines: string[] = [];
  const title = (notes.title || "").trim();
  if (title) lines.push(`# ${title}`, "");

  const oneLiner = (notes.one_liner || "").trim();
  if (oneLiner) lines.push(`**${oneLiner}**`, "");

  const summary = (notes.summary || "").trim();
  if (summary) lines.push(`## ${headings.overview}`, "", summary, "");

  appendPointSection(lines, headings.focuses, stripMedia(notes.focuses));
  appendPointSection(lines, headings.keyPoints, stripMedia(notes.key_points));
  appendPointSection(lines, headings.hardPoints, stripMedia(notes.hard_points));

  return `${lines.join("\n").trim()}\n`;
}

export function hasNotesDownload(notes: NotesDownloadPayload): boolean {
  const oneLiner = (notes.one_liner || "").trim();
  const summary = (notes.summary || "").trim();
  return (
    Boolean(oneLiner || summary) ||
    stripMedia(notes.focuses).length > 0 ||
    stripMedia(notes.key_points).length > 0 ||
    stripMedia(notes.hard_points).length > 0
  );
}

function notesFilename(filename: string): string {
  if (filename.endsWith(".md")) return filename;
  if (filename.endsWith(".json")) return `${filename.slice(0, -5)}.md`;
  return `${filename}.md`;
}

export function downloadNotesFile(
  filename: string,
  notes: NotesDownloadPayload,
  headings?: NotesDownloadHeadings
): void {
  const body = formatNotesMarkdown(notes, headings);
  const blob = new Blob([body], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = notesFilename(filename);
  a.click();
  URL.revokeObjectURL(url);
}

export function DownloadNotes({
  label,
  filename,
  notes,
  headings,
  className = "watch-btn secondary captions-dl-btn",
}: {
  label: string;
  filename: string;
  notes: NotesDownloadPayload;
  headings?: NotesDownloadHeadings;
  className?: string;
}) {
  if (!hasNotesDownload(notes)) return null;

  return (
    <button
      type="button"
      className={className}
      onClick={() => downloadNotesFile(filename, notes, headings)}
    >
      {label}
    </button>
  );
}
