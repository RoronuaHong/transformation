"use client";

import type { KeyPoint } from "@/lib/note-points";

export type NotesDownloadPayload = {
  title?: string;
  one_liner?: string;
  summary?: string;
  focuses?: KeyPoint[];
  key_points?: KeyPoint[];
  hard_points?: KeyPoint[];
};

function stripMedia(points: KeyPoint[] | undefined): { title: string; detail: string }[] {
  return (points || [])
    .map((p) => ({
      title: (p.title || "").trim(),
      detail: (p.detail || "").trim(),
    }))
    .filter((p) => p.title);
}

export function DownloadNotes({
  label,
  filename,
  notes,
}: {
  label: string;
  filename: string;
  notes: NotesDownloadPayload;
}) {
  const focuses = stripMedia(notes.focuses);
  const keyPoints = stripMedia(notes.key_points);
  const hardPoints = stripMedia(notes.hard_points);
  const oneLiner = (notes.one_liner || "").trim();
  const summary = (notes.summary || "").trim();
  const hasBody =
    Boolean(oneLiner || summary) ||
    focuses.length > 0 ||
    keyPoints.length > 0 ||
    hardPoints.length > 0;
  if (!hasBody) return null;

  function onDownload() {
    const payload = {
      title: (notes.title || "").trim() || undefined,
      one_liner: oneLiner || undefined,
      summary: summary || undefined,
      focuses: focuses.length ? focuses : undefined,
      key_points: keyPoints.length ? keyPoints : undefined,
      hard_points: hardPoints.length ? hardPoints : undefined,
    };
    const body = `${JSON.stringify(payload, null, 2)}\n`;
    const blob = new Blob([body], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename.endsWith(".json") ? filename : `${filename}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <button type="button" className="watch-btn secondary captions-dl-btn" onClick={onDownload}>
      {label}
    </button>
  );
}
