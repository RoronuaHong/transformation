#!/usr/bin/env python3
"""Attach still frames from source video to note key_points (visual how-tos)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fetch_media import find_ffmpeg
from job_layout import job_media_dir, locale_srt_path
from langs import coalesce_source_lang
from pipeline import parse_srt
from segment_subs import enrich_segment_cues, stamp_row_cues


VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov"}
DEFAULT_CLIP_WORKERS = 2

_print_lock = threading.Lock()


def locked_print(*args, **kwargs) -> None:
    with _print_lock:
        print(*args, **kwargs)


def clip_workers(explicit: int | None = None) -> int:
    """Parallel ffmpeg jobs for GIF/MP4 cuts (1–8)."""
    if explicit is not None:
        return max(1, min(8, int(explicit)))
    raw = (os.environ.get("VITUAL_CLIP_WORKERS") or str(DEFAULT_CLIP_WORKERS)).strip()
    try:
        return max(1, min(8, int(raw)))
    except ValueError:
        return DEFAULT_CLIP_WORKERS


def existing_source_video(work_dir: Path) -> Path | None:
    media = job_media_dir(work_dir)
    candidates: list[Path] = []
    for folder in (media, Path(work_dir)):
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES:
                if _has_video_stream(p):
                    candidates.append(p)
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def _has_video_stream(path: Path) -> bool:
    ffmpeg = find_ffmpeg()
    # ffprobe may sit next to ffmpeg; fall back to ffmpeg -i stderr
    probe = Path(ffmpeg).with_name("ffprobe.exe" if path.suffix else "ffprobe")
    if not probe.exists():
        probe = Path(ffmpeg).with_name("ffprobe")
    if probe.exists():
        try:
            r = subprocess.run(
                [
                    str(probe),
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            return "video" in (r.stdout or "").lower()
        except Exception:
            pass
    try:
        r = subprocess.run(
            [ffmpeg, "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        err = (r.stderr or "") + (r.stdout or "")
        return bool(re.search(r"Stream #.*Video:", err))
    except Exception:
        return False


def extract_frame(video: Path, t_sec: float, out_jpg: Path, *, width: int = 720) -> Path:
    ffmpeg = find_ffmpeg()
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    t = max(0.0, float(t_sec))
    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        f"{t:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        "3",
        str(out_jpg),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    if not out_jpg.is_file() or out_jpg.stat().st_size < 200:
        raise RuntimeError(f"frame extract failed at t={t}: {out_jpg}")
    return out_jpg


def extract_gif(
    video: Path,
    t_sec: float,
    out_gif: Path,
    *,
    duration: float = 4.0,
    start_sec: float | None = None,
    end_sec: float | None = None,
    width: int = 360,
    fps: int = 8,
) -> Path:
    """Looping GIF from cue head→tail (or centered window capped by duration)."""
    ffmpeg = find_ffmpeg()
    out_gif.parent.mkdir(parents=True, exist_ok=True)
    if start_sec is not None and end_sec is not None:
        start, end = clip_gif_span(float(start_sec), float(end_sec), max_dur=float(duration))
    else:
        dur = max(1.0, min(20.0, float(duration)))
        start = max(0.0, float(t_sec) - 0.4)
        end = start + dur
    dur = max(0.5, end - start)
    # palettegen keeps size reasonable for how-to clips
    vf = (
        f"fps={fps},scale={width}:-2:flags=lanczos,"
        f"split[s0][s1];[s0]palettegen=max_colors=64:stats_mode=diff[p];"
        f"[s1][p]paletteuse=dither=bayer:bayer_scale=3"
    )
    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{dur:.3f}",
        "-i",
        str(video),
        "-an",
        "-vf",
        vf,
        "-loop",
        "0",
        str(out_gif),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    if not out_gif.is_file() or out_gif.stat().st_size < 500:
        raise RuntimeError(f"gif extract failed at t={t_sec}: {out_gif}")
    return out_gif


def extract_video_clip(
    video: Path,
    start_sec: float,
    end_sec: float,
    out_mp4: Path,
    *,
    max_dur: float = 20.0,
    width: int = 720,
) -> Path:
    """Cut a short mp4 clip (re-encode; output-seek keeps A/V in sync)."""
    ffmpeg = find_ffmpeg()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    start = max(0.0, float(start_sec))
    end = max(start + 0.4, float(end_sec))
    cap = max(1.0, min(60.0, float(max_dur)))
    if end - start > cap:
        end = start + cap
    dur = max(0.4, end - start)
    # Output-seek (-ss after -i) + reset PTS so A/V both start at 0.
    # Input-seek alone often desyncs short clips around keyframes.
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{dur:.3f}",
        "-vf",
        f"scale={width}:-2,setpts=PTS-STARTPTS",
        "-af",
        "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "28",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-avoid_negative_ts",
        "make_zero",
        "-movflags",
        "+faststart",
        str(out_mp4),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    if not out_mp4.is_file() or out_mp4.stat().st_size < 800:
        raise RuntimeError(f"mp4 clip failed {start:.2f}-{end:.2f}s → {out_mp4}")
    return out_mp4


def public_clip_path(slug: str, index: int) -> str:
    return f"/clips/{slug}/kp_{index:02d}.mp4"


def clip_gif_span(
    seg_start: float,
    seg_end: float,
    *,
    max_dur: float = 4.0,
) -> tuple[float, float]:
    """Build a GIF window of *target* length around the subtitle cue.

    - Cue shorter than target → expand symmetrically to ``max_dur`` seconds.
    - Cue longer than target → keep the middle ``max_dur`` slice.
    """
    start = max(0.0, float(seg_start))
    end = max(start + 0.5, float(seg_end))
    target = max(1.0, min(20.0, float(max_dur)))
    mid = (start + end) / 2.0
    half = target / 2.0
    out_start = max(0.0, mid - half)
    # Keep exact target length after clamping against t=0.
    out_end = out_start + target
    return out_start, out_end


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    # CJK bigrams + latin/digit words
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    grams = {"".join(cjk[i : i + 2]) for i in range(max(0, len(cjk) - 1))}
    words = set(re.findall(r"[a-z0-9]{2,}", text))
    singles = set(cjk) if len(cjk) <= 4 else set()
    return grams | words | singles


def _score(note: dict, seg_text: str) -> float:
    needle = f"{note.get('title') or ''} {note.get('detail') or ''}"
    a, b = _tokens(needle), _tokens(seg_text)
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / (len(a) ** 0.5 * len(b) ** 0.5)


def align_notes_to_spans(
    notes: list[dict],
    segs: list[dict],
    *,
    min_score: float = 0.05,
) -> list[tuple[float, float] | None]:
    """Match each note to a cue (start, end) span from the source SRT."""
    spans: list[tuple[float, float] | None] = [None] * len(notes)
    if not notes or not segs:
        return spans

    # Distinctive anchors from titles (order matters for multi-word preference)
    anchors = (
        "肌肉卷",
        "羽绒服",
        "羽绒",
        "大衣",
        "毛衣",
        "卫衣",
        "长袖",
        "T恤",
        "t恤",
        "豆腐",
        "收纳箱",
        "尺寸",
        "帽子",
        "口袋",
        "悬挂",
        "衬衫",
    )

    used: set[int] = set()
    assigned: list[tuple[int, int]] = []  # note_i, seg_j

    def seg_span(j: int) -> tuple[float, float]:
        s = max(0.0, float(segs[j]["start"]))
        e = max(s + 0.5, float(segs[j]["end"]))
        return s, e

    def mid(j: int) -> float:
        s, e = seg_span(j)
        return (s + e) / 2.0

    # Pass 1: unique keyword from title (longer / more specific first)
    for i, note in enumerate(notes):
        title = str(note.get("title") or "")
        detail = str(note.get("detail") or "")
        blob = title + detail
        note_anchors = [
            ank
            for ank in anchors
            if ank in blob or ank.lower() in blob.lower()
        ]
        note_anchors.sort(key=lambda a: (-len(a), 0 if a in title else 1))
        if "帽" in title and "羽绒" not in title and "大衣" not in title:
            note_anchors.sort(
                key=lambda a: (0 if "帽" in a else 1, -len(a), 0 if a in title else 1)
            )
        # Prefer the garment noun when present (羽绒服 > 帽子)
        for preferred in ("羽绒服", "羽绒", "大衣", "毛衣", "卫衣", "肌肉卷", "长袖"):
            if preferred == "卫衣" and "帽" in title:
                continue
            if preferred in title and preferred in note_anchors:
                note_anchors.remove(preferred)
                note_anchors.insert(0, preferred)
                break
        hit_j = -1
        for ank in note_anchors:
            # For 羽绒服+帽子, skip early 帽子-only (hoodie) hits
            require_both = "羽绒" in title and "帽" in title and ank in ("帽子", "帽")
            for j, seg in enumerate(segs):
                if j in used:
                    continue
                text = str(seg.get("text") or "")
                if ank not in text and ank.lower() not in text.lower():
                    continue
                if require_both and "羽绒" not in text:
                    continue
                hit_j = j
                break
            if hit_j >= 0:
                break
        if hit_j >= 0:
            used.add(hit_j)
            assigned.append((i, hit_j))

    assigned_notes = {a[0] for a in assigned}

    # Pass 2: fuzzy fill remaining, prefer unused segments near neighbors
    for i, note in enumerate(notes):
        if i in assigned_notes:
            continue
        best_j, best_s = -1, 0.0
        for j, seg in enumerate(segs):
            if j in used:
                continue
            s = _score(note, str(seg.get("text") or ""))
            if s > best_s:
                best_s, best_j = s, j
        if best_j >= 0 and best_s >= min_score:
            used.add(best_j)
            assigned.append((i, best_j))
            assigned_notes.add(i)

    # Pass 3: interpolate missing by note index between neighbors
    known_mid = {i: mid(j) for i, j in assigned}
    known_span = {i: seg_span(j) for i, j in assigned}
    for i in range(len(notes)):
        if i in known_span:
            spans[i] = known_span[i]
            continue
        prev = next((known_mid[k] for k in range(i - 1, -1, -1) if k in known_mid), None)
        nxt = next((known_mid[k] for k in range(i + 1, len(notes)) if k in known_mid), None)
        if prev is not None and nxt is not None:
            t = (prev + nxt) / 2.0
        elif prev is not None:
            t = min(float(segs[-1]["end"]), prev + 8.0)
        elif nxt is not None:
            t = max(0.0, nxt - 8.0)
        else:
            frac = (i + 1) / (len(notes) + 1)
            t = float(segs[0]["start"]) + frac * (
                float(segs[-1]["end"]) - float(segs[0]["start"])
            )
        # Synthetic window ~default length around interpolated time
        spans[i] = (max(0.0, t - 1.5), t + 1.5)

    # Separate near-duplicate windows so GIFs are not almost identical
    seen: list[float] = []
    for i in range(len(spans)):
        sp = spans[i]
        if sp is None:
            continue
        s, e = sp
        mid_t = (s + e) / 2.0
        for prev in seen:
            if abs(mid_t - prev) < 1.2:
                shift = prev + 1.5 - mid_t
                s, e = s + shift, e + shift
                mid_t = (s + e) / 2.0
        spans[i] = (max(0.0, s), max(0.5, e))
        seen.append(mid_t)
    return spans


def align_notes_to_cues(
    notes: list[dict],
    segs: list[dict],
    *,
    min_score: float = 0.05,
) -> list[float | None]:
    """Match each note to a cue midpoint (compat wrapper)."""
    spans = align_notes_to_spans(notes, segs, min_score=min_score)
    return [None if sp is None else (sp[0] + sp[1]) / 2.0 for sp in spans]


def public_frame_path(slug: str, index: int, *, ext: str = "gif") -> str:
    return f"/frames/{slug}/kp_{index:02d}.{ext.lstrip('.')}"


def public_range_clip_path(slug: str, index: int) -> str:
    return f"/clips/{slug}/range_{index:02d}.mp4"


def _parse_clip_spans(clips: list[dict] | None) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    for item in clips or []:
        if not isinstance(item, dict):
            continue
        try:
            s = float(item["start"])
            e = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if e > s >= 0:
            spans.append((s, e))
    return spans


def _is_range_clip_row(kp: dict) -> bool:
    """Rows added only for custom video ranges (no step GIF)."""
    if kp.get("image"):
        return False
    title = str(kp.get("title") or "")
    clip = str(kp.get("clip") or "")
    if not clip:
        return False
    return (
        "/range_" in clip
        or title.startswith("片段")
        or title.lower().startswith("clip ")
    )


def _propagate_media_fields(work_dir: Path, src: str, stamped: list[dict]) -> None:
    notes_root = work_dir / "notes"
    if not notes_root.is_dir():
        return
    for child in notes_root.iterdir():
        js = child / "summary.json"
        if not child.is_dir() or not js.is_file() or child.name == src:
            continue
        loc = json.loads(js.read_text(encoding="utf-8-sig"))
        loc_kps = list(loc.get("key_points") or [])
        while len(loc_kps) < len(stamped):
            src_kp = stamped[len(loc_kps)]
            loc_kps.append(
                {
                    "title": src_kp.get("title"),
                    "detail": src_kp.get("detail"),
                }
            )
        while len(loc_kps) > len(stamped):
            last = loc_kps[-1]
            if _is_range_clip_row(last) or not last.get("image"):
                loc_kps.pop()
            else:
                break
        for i, kp in enumerate(loc_kps):
            if i >= len(stamped):
                break
            src_kp = stamped[i]
            for key in ("image", "clip", "start_sec", "end_sec", "cue_start", "cue_end"):
                if key in src_kp and src_kp[key] is not None:
                    kp[key] = src_kp[key]
                else:
                    kp.pop(key, None)
            if src_kp.get("title") and _is_range_clip_row(src_kp):
                kp["title"] = src_kp["title"]
                kp["detail"] = src_kp.get("detail") or ""
        loc["key_points"] = loc_kps
        js.write_text(json.dumps(loc, ensure_ascii=False, indent=2), encoding="utf-8")


def attach_keypoint_frames(
    work_dir: Path,
    *,
    slug: str,
    source_lang: str = "zh",
    video: Path | None = None,
    max_points: int = 12,
    media: str = "gif",
    gif_duration: float = 4.0,
) -> dict:
    """
    Match key_points to source SRT, extract GIF/JPG for step images.
    Does NOT cut custom-range MP4 — use attach_video_range_clips for that.
    """
    work_dir = Path(work_dir)
    video = video or existing_source_video(work_dir)
    if video is None:
        raise FileNotFoundError(f"no source video in {work_dir}")
    kind = (media or "gif").lower().strip()
    if kind not in {"gif", "jpg", "jpeg"}:
        kind = "gif"
    ext = "jpg" if kind in {"jpg", "jpeg"} else "gif"

    src = coalesce_source_lang(source_lang)
    notes_path = work_dir / "notes" / src / "summary.json"
    if not notes_path.is_file():
        raise FileNotFoundError(f"missing notes: {notes_path}")
    summary = json.loads(notes_path.read_text(encoding="utf-8-sig"))
    raw_points = list(summary.get("key_points") or [])
    # GIF/JPG pass must not drop custom-range MP4 rows (independent block).
    range_rows = [kp for kp in raw_points if _is_range_clip_row(kp)]
    key_points = [kp for kp in raw_points if not _is_range_clip_row(kp)]
    if not key_points:
        summary["key_points"] = key_points + range_rows
        notes_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary

    srt = locale_srt_path(work_dir, src)
    if not srt.is_file():
        raise FileNotFoundError(f"missing source srt: {srt}")
    segs = parse_srt(srt)
    slice_pts = key_points[:max_points]
    spans = align_notes_to_spans(slice_pts, segs)

    frames_dir = job_media_dir(work_dir) / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    stamped: list[dict] = []
    workers = clip_workers()

    def _cut_one(i: int, kp: dict, span: tuple[float, float] | None) -> dict:
        row = dict(kp)
        row.pop("clip", None)
        if span is None:
            return row
        seg_start, seg_end = span
        t = (seg_start + seg_end) / 2.0
        out = frames_dir / f"kp_{i:02d}.{ext}"
        try:
            if ext == "gif":
                start, end = clip_gif_span(
                    seg_start, seg_end, max_dur=float(gif_duration)
                )
                extract_gif(
                    video,
                    t,
                    out,
                    duration=max(end - start, 0.5),
                    start_sec=start,
                    end_sec=end,
                )
                row["start_sec"] = round(start, 3)
                row["end_sec"] = round(end, 3)
                locked_print(
                    f"[frames] kp[{i}] {start:.2f}-{end:.2f}s "
                    f"(cue {seg_start:.2f}-{seg_end:.2f}) -> {out.name}"
                )
            else:
                extract_frame(video, t, out)
                row["start_sec"] = round(t, 3)
                locked_print(f"[frames] kp[{i}] t={t:.2f}s -> {out.name}")
            row["image"] = public_frame_path(slug, i, ext=ext)
            size_kb = out.stat().st_size / 1024
            locked_print(f"[frames]   size={size_kb:.0f}KB")
        except Exception as e:
            locked_print(f"[frames] kp[{i}] skip ({type(e).__name__}: {e})")
        return row

    jobs = [
        (i, kp, spans[i] if i < len(spans) else None)
        for i, kp in enumerate(key_points)
    ]
    if workers <= 1 or len(jobs) <= 1:
        stamped = [_cut_one(i, kp, sp) for i, kp, sp in jobs]
    else:
        locked_print(f"[frames] cutting {len(jobs)} with workers={workers}")
        by_i: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_cut_one, i, kp, sp): i for i, kp, sp in jobs
            }
            for fut in as_completed(futs):
                i = futs[fut]
                by_i[i] = fut.result()
        stamped = [by_i[i] for i in range(len(jobs))]

    for i, row in enumerate(stamped):
        if row.get("start_sec") is not None and row.get("end_sec") is not None:
            meta = enrich_segment_cues(
                work_dir,
                segment_id=f"kp_{i:02d}",
                start_sec=float(row["start_sec"]),
                end_sec=float(row["end_sec"]),
            )
            stamped[i] = stamp_row_cues(dict(row), meta)

    stamped.extend(range_rows)
    summary["key_points"] = stamped
    notes_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _propagate_media_fields(work_dir, src, stamped)

    meta = {
        "slug": slug,
        "video": str(video),
        "media": ext,
        "frames": sum(1 for x in stamped if x.get("image")),
        "key_points": len(stamped),
    }
    (frames_dir / "frames_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[frames] attached {meta['frames']}/{meta['key_points']} ({ext}) → {frames_dir}")
    return summary


def public_gif_range_path(slug: str, index: int, *, ext: str = "gif") -> str:
    return f"/frames/{slug}/gif_range_{index:02d}.{ext.lstrip('.')}"


def attach_gif_ranges(
    work_dir: Path,
    *,
    slug: str,
    source_lang: str = "zh",
    video: Path | None = None,
    ranges: list[dict] | None = None,
    media: str = "gif",
    max_ranges: int = 24,
    max_dur: float = 20.0,
) -> dict:
    """
    Cut GIF/JPG from explicit start/end ranges (try-form block ①).
    Stamps images onto key_points by index; extra ranges append visual rows.
    Independent of MP4 range clips.
    """
    spans = _parse_clip_spans(ranges)[:max_ranges]
    work_dir = Path(work_dir)
    src = coalesce_source_lang(source_lang)
    notes_path = work_dir / "notes" / src / "summary.json"
    if not notes_path.is_file():
        raise FileNotFoundError(f"missing notes: {notes_path}")
    summary = json.loads(notes_path.read_text(encoding="utf-8-sig"))
    # Keep study notes + mp4 range rows; strip prior gif_range-only rows.
    base_points: list[dict] = []
    mp4_rows: list[dict] = []
    for kp in summary.get("key_points") or []:
        if not isinstance(kp, dict):
            continue
        if _is_range_clip_row(kp):
            mp4_rows.append(kp)
            continue
        title = str(kp.get("title") or "")
        if title.startswith("动图 ") or title.lower().startswith("gif "):
            continue
        row = dict(kp)
        row.pop("image", None)
        row.pop("start_sec", None)
        row.pop("end_sec", None)
        row.pop("clip", None)
        base_points.append(row)

    frames_dir = job_media_dir(work_dir) / "frames"
    if frames_dir.is_dir():
        for old in frames_dir.glob("gif_range_*.*"):
            old.unlink(missing_ok=True)
        for old in frames_dir.glob("kp_*.*"):
            old.unlink(missing_ok=True)

    if not spans:
        summary["key_points"] = base_points + mp4_rows
        notes_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        _propagate_media_fields(work_dir, src, summary["key_points"])
        print("[gif-ranges] empty — cleared custom gif cuts")
        return summary

    video = video or existing_source_video(work_dir)
    if video is None:
        raise FileNotFoundError(f"no source video in {work_dir}")
    frames_dir.mkdir(parents=True, exist_ok=True)

    kind = (media or "gif").lower().strip()
    if kind not in {"gif", "jpg", "jpeg"}:
        kind = "gif"
    ext = "jpg" if kind in {"jpg", "jpeg"} else "gif"
    cap = max(1.0, min(20.0, float(max_dur)))

    stamped = [dict(kp) for kp in base_points]
    extra_rows: list[dict] = []
    workers = clip_workers()

    def _cut_gif(i: int, start: float, end: float) -> tuple[int, dict | None]:
        end = min(end, start + cap)
        if end <= start:
            return i, None
        out = frames_dir / f"gif_range_{i:02d}.{ext}"
        try:
            if ext == "gif":
                extract_gif(
                    video,
                    (start + end) / 2.0,
                    out,
                    duration=end - start,
                    start_sec=start,
                    end_sec=end,
                )
            else:
                extract_frame(video, (start + end) / 2.0, out)
            payload = {
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "image": public_gif_range_path(slug, i, ext=ext),
            }
            locked_print(
                f"[gif-ranges] [{i}] {start:.2f}-{end:.2f}s -> {out.name} "
                f"({out.stat().st_size / 1024:.0f}KB)"
            )
            return i, payload
        except Exception as e:
            locked_print(f"[gif-ranges] [{i}] skip ({type(e).__name__}: {e})")
            return i, None

    results: list[tuple[int, dict | None]]
    if workers <= 1 or len(spans) <= 1:
        results = [_cut_gif(i, start, end) for i, (start, end) in enumerate(spans)]
    else:
        locked_print(f"[gif-ranges] cutting {len(spans)} with workers={workers}")
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(_cut_gif, i, start, end)
                for i, (start, end) in enumerate(spans)
            ]
            for fut in as_completed(futs):
                results.append(fut.result())
        results.sort(key=lambda x: x[0])

    for i, payload in results:
        if not payload:
            continue
        if i < len(stamped):
            stamped[i].update(payload)
        else:
            start = float(payload["start_sec"])
            end = float(payload["end_sec"])
            extra_rows.append(
                {
                    "title": f"动图 {i + 1}",
                    "detail": f"{start:.1f}s – {end:.1f}s",
                    **payload,
                }
            )

    final = stamped + extra_rows + mp4_rows
    for i, row in enumerate(final):
        if not isinstance(row, dict):
            continue
        sid = row.get("clip") and f"range_{i:02d}" if _is_range_clip_row(row) else None
        if not sid and row.get("image"):
            img = str(row.get("image") or "")
            if "gif_range_" in img:
                sid = Path(img).stem
            elif "kp_" in img:
                sid = Path(img).stem
        start = row.get("start_sec")
        end = row.get("end_sec")
        if sid and start is not None and end is not None:
            meta = enrich_segment_cues(
                work_dir,
                segment_id=sid,
                start_sec=float(start),
                end_sec=float(end),
            )
            final[i] = stamp_row_cues(dict(row), meta)
    summary["key_points"] = final
    notes_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _propagate_media_fields(work_dir, src, final)
    meta = {
        "slug": slug,
        "video": str(video),
        "media": ext,
        "gif_ranges": len(spans),
        "frames": sum(1 for x in final if x.get("image")),
    }
    (frames_dir / "frames_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[gif-ranges] attached {meta['frames']} images → {frames_dir}")
    return summary


def attach_video_range_clips(
    work_dir: Path,
    *,
    slug: str,
    source_lang: str = "zh",
    video: Path | None = None,
    clips: list[dict] | None = None,
    max_clips: int = 24,
    video_clip_max_sec: float = 60.0,
) -> dict:
    """
    Cut short MP4s from explicit start/end ranges (try-form video ranges).
    Independent of GIF/JPG step images.
    """
    spans = _parse_clip_spans(clips)[:max_clips]
    work_dir = Path(work_dir)
    src = coalesce_source_lang(source_lang)
    notes_path = work_dir / "notes" / src / "summary.json"
    if not notes_path.is_file():
        raise FileNotFoundError(f"missing notes: {notes_path}")
    summary = json.loads(notes_path.read_text(encoding="utf-8-sig"))
    base_points = [kp for kp in (summary.get("key_points") or []) if not _is_range_clip_row(kp)]

    clips_dir = job_media_dir(work_dir) / "clips"

    if not spans:
        # clips=None / omitted by callers → keep existing; only [] clears.
        if clips is None:
            print("[clips] no ranges passed — kept existing mp4 rows")
            return summary
        if clips_dir.is_dir():
            for old in clips_dir.glob("range_*.mp4"):
                old.unlink(missing_ok=True)
            (clips_dir / "clips_meta.json").unlink(missing_ok=True)
        summary["key_points"] = base_points
        notes_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        _propagate_media_fields(work_dir, src, base_points)
        print("[clips] empty ranges — cleared mp4 cut")
        return summary

    if clips_dir.is_dir():
        for old in clips_dir.glob("range_*.mp4"):
            old.unlink(missing_ok=True)

    video = video or existing_source_video(work_dir)
    if video is None:
        raise FileNotFoundError(f"no source video in {work_dir}")
    from media_ops import ensure_video_has_audio

    video = ensure_video_has_audio(work_dir, video)
    clips_dir.mkdir(parents=True, exist_ok=True)

    range_rows: list[dict] = []
    workers = clip_workers()

    def _cut_mp4(i: int, start: float, end: float) -> dict | None:
        out = clips_dir / f"range_{i:02d}.mp4"
        try:
            extract_video_clip(
                video,
                start,
                end,
                out,
                max_dur=float(video_clip_max_sec),
            )
            actual_end = min(end, start + float(video_clip_max_sec))
            locked_print(
                f"[clips] range[{i}] {start:.2f}-{actual_end:.2f}s "
                f"-> {out.name} ({out.stat().st_size / 1024:.0f}KB)"
            )
            return {
                "title": f"片段 {i + 1}",
                "detail": f"{start:.1f}s – {actual_end:.1f}s",
                "start_sec": round(start, 3),
                "end_sec": round(actual_end, 3),
                "clip": public_range_clip_path(slug, i),
            }
        except Exception as e:
            locked_print(f"[clips] range[{i}] skip ({type(e).__name__}: {e})")
            return None

    if workers <= 1 or len(spans) <= 1:
        for i, (start, end) in enumerate(spans):
            row = _cut_mp4(i, start, end)
            if row:
                range_rows.append(row)
    else:
        locked_print(f"[clips] cutting {len(spans)} with workers={workers}")
        by_i: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(_cut_mp4, i, start, end): i
                for i, (start, end) in enumerate(spans)
            }
            for fut in as_completed(futs):
                i = futs[fut]
                row = fut.result()
                if row:
                    by_i[i] = row
        range_rows = [by_i[i] for i in sorted(by_i)]

    if range_rows:
        from media_ops import write_clips_meta

        write_clips_meta(
            work_dir,
            [(float(row["start_sec"]), float(row["end_sec"])) for row in range_rows],
        )

    enriched_rows: list[dict] = []
    for i, row in enumerate(range_rows):
        meta = enrich_segment_cues(
            work_dir,
            segment_id=f"range_{i:02d}",
            start_sec=float(row["start_sec"]),
            end_sec=float(row["end_sec"]),
        )
        enriched_rows.append(stamp_row_cues(dict(row), meta))

    stamped = base_points + enriched_rows
    summary["key_points"] = stamped
    notes_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    _propagate_media_fields(work_dir, src, stamped)
    print(f"[clips] attached {len(range_rows)} mp4 ranges → {clips_dir}")
    return summary


def copy_frames_to_site(work_dir: Path, slug: str, site_public: Path) -> int:
    src = job_media_dir(work_dir) / "frames"
    if not src.is_dir():
        return 0
    dest = Path(site_public) / "frames" / slug
    dest.mkdir(parents=True, exist_ok=True)
    by_stem: dict[str, Path] = {}
    for p in sorted(list(src.glob("kp_*.*")) + list(src.glob("gif_range_*.*"))):
        if p.suffix.lower() not in {".gif", ".jpg", ".jpeg", ".webp"}:
            continue
        stem = p.stem
        prev = by_stem.get(stem)
        if prev is None or p.suffix.lower() == ".gif":
            by_stem[stem] = p
    n = 0
    for p in by_stem.values():
        target = dest / p.name
        target.write_bytes(p.read_bytes())
        n += 1
    keep = {p.name for p in by_stem.values()}
    for old in list(dest.glob("kp_*.*")) + list(dest.glob("gif_range_*.*")):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    return n


def copy_clips_to_site(work_dir: Path, slug: str, site_public: Path) -> int:
    src = job_media_dir(work_dir) / "clips"
    if not src.is_dir():
        return 0
    dest = Path(site_public) / "clips" / slug
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    keep: set[str] = set()
    for p in sorted(list(src.glob("kp_*.mp4")) + list(src.glob("range_*.mp4"))):
        target = dest / p.name
        target.write_bytes(p.read_bytes())
        keep.add(p.name)
        n += 1
    for old in dest.glob("*.mp4"):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    return n
