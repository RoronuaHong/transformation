"use client";

type CueLine = { start: string; end: string; text: string };

export function DownloadCaptions({
  label,
  filename,
  cues,
}: {
  label: string;
  filename: string;
  cues: CueLine[];
}) {
  if (!cues.length) return null;

  function onDownload() {
    const body = cues
      .map((c, i) => `${i + 1}\n${c.start} --> ${c.end}\n${c.text}\n`)
      .join("\n");
    const blob = new Blob([body], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename.endsWith(".srt") ? filename : `${filename}.srt`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <button type="button" className="watch-btn secondary captions-dl-btn" onClick={onDownload}>
      {label}
    </button>
  );
}
