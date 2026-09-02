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


def remap_cues_to_spans(
    segs: list[dict],
    spans: list[tuple[float, float]],
    durations: list[float] | None = None,
) -> list[dict]:
    """Map full-timeline cues onto concatenated clip clock (t=0 = first span start).

    ``durations`` is the real length of each encoded clip when known; otherwise
    ``end - start`` is used. Cue overlap still uses the source-span window.
    """
    out: list[dict] = []
    cursor = 0.0
    for i, (start, end) in enumerate(spans):
        start_f = float(start)
        end_f = float(end)
        if end_f <= start_f:
            continue
        dur = end_f - start_f
        if durations is not None and i < len(durations):
            try:
                probed = float(durations[i])
            except (TypeError, ValueError):
                probed = 0.0
            if probed > 0.05:
                dur = probed
        for seg in segs:
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            s = float(seg["start"])
            e = float(seg["end"])
            if e <= start_f or s >= end_f:
                continue
            cs = max(s, start_f)
            ce = min(e, end_f)
            local_s = cursor + (cs - start_f)
            local_e = cursor + (ce - start_f)
            cap = cursor + dur
            local_s = min(max(cursor, local_s), cap)
            local_e = min(max(local_s, local_e), cap)
            if local_e > local_s:
                out.append({"start": local_s, "end": local_e, "text": text})
        cursor += dur
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

    seg_root = work_dir / "media" / "segments" / segment_id
    subs_dir = seg_root / "subs"
    subs_dir.mkdir(parents=True, exist_ok=True)
    lang_map: dict[str, str] = {}

    span = (float(start_sec), float(end_sec))
    for lang, path in srts.items():
        segs = parse_srt(path)
        if not segs:
            continue
        chunk = remap_cues_to_spans(segs, [span])
        if not chunk:
            continue
        rel = f"subs/{lang}.srt"
        _write_srt(subs_dir / f"{lang}.srt", chunk)
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
