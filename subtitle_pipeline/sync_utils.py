"""Subtitle timeline helpers: align checks, slice offset, global shift."""

from __future__ import annotations

import json
from pathlib import Path


def apply_slice_offset(segments: list[dict], slice_start_sec: float) -> list[dict]:
    """Map clip-local Whisper times onto full-video clock."""
    if not slice_start_sec:
        return [dict(s) for s in segments]
    off = float(slice_start_sec)
    out: list[dict] = []
    for s in segments:
        item = dict(s)
        item["start"] = round(float(s["start"]) + off, 3)
        item["end"] = round(float(s["end"]) + off, 3)
        out.append(item)
    return out


def merge_offset_segments(
    parts: list[tuple[list[dict], float]],
    *,
    fix_overlaps: bool = True,
) -> list[dict]:
    """Merge clip-local segment lists onto one timeline via per-part offsets.

    Each part is ``(segments, slice_start_sec)`` where times inside ``segments``
    are relative to the clip (Whisper output starting near 0). Never ``cat``
    raw SRTs that all start at 0.
    """
    merged: list[dict] = []
    for segs, offset in parts:
        if not segs:
            continue
        merged.extend(apply_slice_offset(list(segs), float(offset)))
    merged = [s for s in merged if str(s.get("text") or "").strip()]
    merged.sort(key=lambda s: (float(s["start"]), float(s["end"])))
    if not fix_overlaps or len(merged) <= 1:
        return merged
    out: list[dict] = [dict(merged[0])]
    for seg in merged[1:]:
        cur = dict(seg)
        prev = out[-1]
        if float(cur["start"]) < float(prev["end"]):
            if float(cur["start"]) > float(prev["start"]) + 1e-9:
                # Later cue opens inside prev — trim prev to abut.
                prev["end"] = round(float(cur["start"]), 3)
                if float(prev["end"]) <= float(prev["start"]) + 1e-9:
                    out.pop()
                    if not out:
                        out.append(cur)
                        continue
                    prev = out[-1]
                    if float(cur["start"]) < float(prev["end"]):
                        cur["start"] = round(float(prev["end"]), 3)
            else:
                # Same (or earlier) start — typically a bad zero-based cat; delay cur.
                cur["start"] = round(float(prev["end"]), 3)
            if float(cur["end"]) <= float(cur["start"]) + 1e-9:
                continue
        out.append(cur)
    return out


def shift_segments(segments: list[dict], shift_ms: int) -> list[dict]:
    """Global shift in milliseconds (can be negative). Clamps start >= 0."""
    delta = float(shift_ms) / 1000.0
    out: list[dict] = []
    for s in segments:
        start = max(0.0, float(s["start"]) + delta)
        end = max(start + 0.01, float(s["end"]) + delta)
        item = dict(s)
        item["start"] = round(start, 3)
        item["end"] = round(end, 3)
        out.append(item)
    return out


def assert_timeline_aligned(
    source: list[dict],
    target: list[dict],
    *,
    tol: float = 1e-3,
    label: str = "target",
) -> None:
    """Fail hard if cue count or start/end diverge (S7 guard)."""
    if len(source) != len(target):
        raise ValueError(
            f"[sync] {label} cue count mismatch: source={len(source)} {label}={len(target)}"
        )
    for i, (a, b) in enumerate(zip(source, target), 1):
        if abs(float(a["start"]) - float(b["start"])) > tol or abs(
            float(a["end"]) - float(b["end"])
        ) > tol:
            raise ValueError(
                f"[sync] {label} cue #{i} timeline drift: "
                f"src={a['start']:.3f}-{a['end']:.3f} "
                f"tgt={b['start']:.3f}-{b['end']:.3f}"
            )


def validate_srt_monotonic(segments: list[dict], *, max_overlap: float = 0.5) -> list[str]:
    """Return soft warning strings; empty means OK."""
    warnings: list[str] = []
    prev_end: float | None = None
    for i, s in enumerate(segments, 1):
        start, end = float(s["start"]), float(s["end"])
        if end <= start:
            warnings.append(f"cue#{i} end<=start ({start}-{end})")
        if prev_end is not None and start + 1e-6 < prev_end - max_overlap:
            warnings.append(
                f"cue#{i} overlaps previous by >{max_overlap}s "
                f"(prev_end={prev_end:.3f} start={start:.3f})"
            )
        prev_end = end
    return warnings


def write_sync_meta(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
