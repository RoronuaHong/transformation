"""Per-segment multilingual subtitle bundles (work_dir/media/segments/)."""

from __future__ import annotations

import json
from pathlib import Path

from job_layout import list_locale_srts, locale_srt_path
from pipeline import parse_srt


def _srt_timestamp(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    ms_total = int(round(float(sec) * 1000.0))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def find_cue_range(segs: list[dict], start_sec: float, end_sec: float) -> tuple[int, int] | None:
    """Inclusive cue indices overlapping [start_sec, end_sec]."""
    if not segs:
        return None
    first: int | None = None
    last: int | None = None
    for i, seg in enumerate(segs):
        s = float(seg["start"])
        e = float(seg["end"])
        if e < start_sec or s > end_sec:
            continue
        if first is None:
            first = i
        last = i
    if first is None or last is None:
        return None
    return first, last


def _shift_segments(segs: list[dict], offset: float) -> list[dict]:
    out: list[dict] = []
    for seg in segs:
        s = max(0.0, float(seg["start"]) - offset)
        e = max(s + 0.01, float(seg["end"]) - offset)
        out.append({"start": s, "end": e, "text": seg.get("text") or ""})
    return out


def _write_srt(path: Path, segs: list[dict]) -> None:
    lines: list[str] = []
    for i, seg in enumerate(segs, start=1):
        lines.append(str(i))
        lines.append(
            f"{_srt_timestamp(float(seg['start']))} --> {_srt_timestamp(float(seg['end']))}"
        )
        lines.append(str(seg.get("text") or "").strip())
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def enrich_segment_cues(
    work_dir: Path,
    *,
    segment_id: str,
    start_sec: float,
    end_sec: float,
) -> dict | None:
    """
    Write media/segments/{segment_id}/subs/{lang}.srt for all locale SRTs.
    Returns {cue_start, cue_end, segment_id} or None when no overlap.
    """
    work_dir = Path(work_dir)
    srts = list_locale_srts(work_dir)
    if not srts:
        return None

    primary_path: Path | None = None
    primary_segs: list[dict] | None = None
    for key in ("zh", "en", "ja", "pt", "de", "ru", "ko"):
        if key in srts:
            primary_path = srts[key]
            primary_segs = parse_srt(primary_path)
            break
    if primary_segs is None:
        primary_path = next(iter(srts.values()))
        primary_segs = parse_srt(primary_path)

    cue_range = find_cue_range(primary_segs, start_sec, end_sec)
    if cue_range is None:
        return None
    i0, i1 = cue_range
    offset = float(primary_segs[i0]["start"])

    seg_root = work_dir / "media" / "segments" / segment_id
    subs_dir = seg_root / "subs"
    subs_dir.mkdir(parents=True, exist_ok=True)
    lang_map: dict[str, str] = {}

    for lang, path in srts.items():
        segs = parse_srt(path)
        if not segs:
            continue
        slice_end = min(i1, len(segs) - 1)
        slice_start = min(i0, slice_end)
        chunk = segs[slice_start : slice_end + 1]
        if not chunk:
            continue
        rel = f"subs/{lang}.srt"
        _write_srt(subs_dir / f"{lang}.srt", _shift_segments(chunk, offset))
        lang_map[lang] = rel

    meta = {
        "segment_id": segment_id,
        "start_sec": round(float(start_sec), 3),
        "end_sec": round(float(end_sec), 3),
        "cue_start": i0,
        "cue_end": i1,
        "subs": lang_map,
    }
    seg_root.mkdir(parents=True, exist_ok=True)
    (seg_root / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return meta


def stamp_row_cues(row: dict, meta: dict | None) -> dict:
    if not meta:
        row.pop("cue_start", None)
        row.pop("cue_end", None)
        return row
    row["cue_start"] = meta["cue_start"]
    row["cue_end"] = meta["cue_end"]
    row["start_sec"] = meta.get("start_sec", row.get("start_sec"))
    row["end_sec"] = meta.get("end_sec", row.get("end_sec"))
    return row
