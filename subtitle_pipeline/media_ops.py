"""WF-04 enhance / WF-05 compress / WF-06 concat — local ffmpeg only."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fetch_media import find_ffmpeg
from job_layout import job_media_dir

STRENGTHS = ("light", "medium", "strong")
DEFAULT_STRENGTH = "medium"
DEFAULT_COMPRESS_HEIGHT = 720
DEFAULT_CRF = 28
DEFAULT_AUDIO_K = 96
MIN_CONCAT = 2
REMIX_W = 1080
REMIX_H = 1920
REMIX_TEMPLATE = "vertical_notes"
DEFAULT_INTRO_SEC = 2.5
HARDSUB_CROP_RATIO = 0.14
CLIPS_META_NAME = "clips_meta.json"


class MediaOpsError(ValueError):
    """Invalid media-workflow options or missing inputs."""


def job_concat_dir(work_dir: Path) -> Path:
    d = job_media_dir(work_dir) / "concat"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_enhance_dir(work_dir: Path) -> Path:
    d = job_media_dir(work_dir) / "enhance"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_compress_dir(work_dir: Path) -> Path:
    d = job_media_dir(work_dir) / "compress"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_remix_dir(work_dir: Path) -> Path:
    d = job_media_dir(work_dir) / "remix"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_clip_mp4s(work_dir: Path) -> list[Path]:
    clips = job_media_dir(work_dir) / "clips"
    if not clips.is_dir():
        return []
    return sorted(p for p in clips.glob("range_*.mp4") if p.is_file() and p.stat().st_size > 800)


def normalize_media_opts(raw: dict | None = None) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    strength = str(src.get("enhance_strength") or DEFAULT_STRENGTH).strip().lower()
    if strength not in STRENGTHS:
        strength = DEFAULT_STRENGTH
    try:
        height = int(src.get("compress_height", DEFAULT_COMPRESS_HEIGHT))
    except (TypeError, ValueError):
        height = DEFAULT_COMPRESS_HEIGHT
    if height < 0:
        height = 0
    if height not in (0, 480, 720, 1080, 1440):
        height = DEFAULT_COMPRESS_HEIGHT
    try:
        crf = int(src.get("compress_crf", DEFAULT_CRF))
    except (TypeError, ValueError):
        crf = DEFAULT_CRF
    crf = max(18, min(32, crf))
    try:
        max_h = int(src.get("enhance_max_height", 1080))
    except (TypeError, ValueError):
        max_h = 1080
    max_h = max(240, min(2160, max_h))
    burn = src.get("remix_burn_subs", True)
    if isinstance(burn, str):
        burn = burn.strip().lower() not in ("0", "false", "no", "off")
    try:
        intro = float(src.get("remix_intro_sec", DEFAULT_INTRO_SEC))
    except (TypeError, ValueError):
        intro = DEFAULT_INTRO_SEC
    intro = max(0.0, min(8.0, intro))
    crop_raw = src.get("remix_crop_hardsubs", True)
    if isinstance(crop_raw, str):
        crop_raw = crop_raw.strip().lower() not in ("0", "false", "no", "off")
    title = str(src.get("remix_title") or "").strip()
    plats_raw = src.get("publish_platforms") or []
    if isinstance(plats_raw, str):
        plats_raw = [p.strip() for p in plats_raw.replace(";", ",").split(",") if p.strip()]
    if not isinstance(plats_raw, list):
        plats_raw = []
    from publish_accounts import PUBLISH_PLATFORMS

    plats = []
    for item in plats_raw:
        p = str(item).strip().lower()
        if p in PUBLISH_PLATFORMS and p not in plats:
            plats.append(p)
    return {
        "enhance_strength": strength,
        "enhance_max_height": max_h,
        "compress_height": height,
        "compress_crf": crf,
        "remix_burn_subs": bool(burn),
        "remix_intro_sec": intro,
        "remix_crop_hardsubs": bool(crop_raw),
        "remix_title": title,
        "publish_platforms": plats,
    }


def even_height_vf(max_height: int) -> str:
    h = max(240, int(max_height))
    return f"scale=-2:trunc(min(ih\\,{h})/2)*2"


def enhance_vf(strength: str = DEFAULT_STRENGTH, max_height: int = 1080) -> str:
    cap = even_height_vf(max_height)
    s = (strength or DEFAULT_STRENGTH).strip().lower()
    if s == "light":
        return f"unsharp=5:5:0.5:5:5:0.0,{cap}"
    if s == "strong":
        return f"scale=iw*1.5:-2:flags=lanczos,unsharp=5:5:1.2:5:5:0.0,{cap}"
    return f"unsharp=5:5:1.0:5:5:0.0,{cap}"


def compress_vf(max_height: int) -> str | None:
    if int(max_height) <= 0:
        return None
    return even_height_vf(max_height)


def concat_filter(count: int, height: int = 720) -> str:
    n = int(count)
    if n < MIN_CONCAT:
        raise MediaOpsError(f"concat needs at least {MIN_CONCAT} clips")
    h = max(240, int(height) or DEFAULT_COMPRESS_HEIGHT)
    even = f"trunc({h}/2)*2"
    parts: list[str] = []
    for i in range(n):
        parts.append(
            f"[{i}:v]scale=-2:{even},setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS[v{i}]"
        )
        parts.append(
            f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS[a{i}]"
        )
    pairs = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{pairs}concat=n={n}:v=1:a=1[v][a]")
    return ";".join(parts)


def wrap_title(text: str, width: int = 16, max_lines: int = 4) -> list[str]:
    """Greedy wrap for 9:16 title cards (one CJK/latin char = one column)."""
    t = " ".join((text or "").split())
    if not t:
        return []
    chunks: list[str] = []
    rest = t
    while rest and len(chunks) < max_lines:
        if len(rest) <= width:
            chunks.append(rest)
            rest = ""
            break
        chunks.append(rest[:width])
        rest = rest[width:]
    if rest:
        last = chunks[-1] if chunks else ""
        chunks[-1] = last[: max(1, width - 1)] + "…"
    return chunks


def ass_escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def hardsub_crop_vf(ratio: float = HARDSUB_CROP_RATIO) -> str:
    """Drop the bottom band of source hardsubs before 9:16 pad."""
    keep = max(0.5, min(0.95, 1.0 - float(ratio)))
    return f"crop=iw:trunc(ih*{keep}/2)*2:0:0"


def remix_body_vf(*, crop_hardsubs: bool, ass_path: Path | None) -> str:
    parts: list[str] = []
    if crop_hardsubs:
        parts.append(hardsub_crop_vf())
    parts.append(vertical_pad_vf())
    if ass_path is not None:
        parts.append(_subtitles_vf(ass_path))
    return ",".join(parts)


def clips_meta_path(work_dir: Path) -> Path:
    return job_media_dir(work_dir) / "clips" / CLIPS_META_NAME


def write_clips_meta(work_dir: Path, spans: list[tuple[float, float]]) -> Path:
    clean: list[dict[str, float | str]] = []
    for i, (start, end) in enumerate(spans):
        start_f = float(start)
        end_f = float(end)
        if end_f <= start_f:
            continue
        clean.append(
            {
                "file": f"range_{i:02d}.mp4",
                "start": round(start_f, 3),
                "end": round(end_f, 3),
                "duration": round(end_f - start_f, 3),
            }
        )
    dest = clips_meta_path(work_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _write_meta(dest, {"clock": "clips", "spans": clean})
    return dest


def load_clip_spans(work_dir: Path) -> list[tuple[float, float]]:
    path = clips_meta_path(work_dir)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            data = {}
        rows = data.get("spans") if isinstance(data, dict) else None
        out: list[tuple[float, float]] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    start = float(row.get("start"))
                    end = float(row.get("end"))
                except (TypeError, ValueError):
                    continue
                if end > start >= 0:
                    out.append((start, end))
        if out:
            return out
    return _clip_spans_from_notes(work_dir)


def _clip_spans_from_notes(work_dir: Path) -> list[tuple[float, float]]:
    notes = Path(work_dir) / "notes"
    if not notes.is_dir():
        return []
    wanted = len(list_clip_mp4s(work_dir))
    if wanted <= 0:
        return []
    for lang in ("zh", "zh-Hant", "en"):
        path = notes / lang / "summary.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        points = data.get("key_points") if isinstance(data, dict) else None
        if not isinstance(points, list):
            continue
        spans: list[tuple[float, float]] = []
        for row in points:
            if not isinstance(row, dict):
                continue
            clip = str(row.get("clip") or "")
            if "range_" not in clip:
                continue
            try:
                start = float(row.get("start_sec", row.get("start")))
                end = float(row.get("end_sec", row.get("end")))
            except (TypeError, ValueError):
                continue
            if end > start >= 0:
                spans.append((start, end))
        if len(spans) == wanted:
            return spans
    return []


def caption_clock_for_body(work_dir: Path, body: Path | None) -> str:
    """Return 'clips' when burned cues must use concatenated-range clock."""
    from note_frames import existing_source_video

    if body is None:
        return "source"
    body = Path(body)
    source = existing_source_video(work_dir)
    try:
        if source is not None and body.resolve() == source.resolve():
            return "source"
    except OSError:
        pass
    if body.name.lower().startswith("range_"):
        return "clips"
    meta_clock = _clock_from_sidecar(body)
    if meta_clock:
        return meta_clock
    if body.name in {"concat.mp4", "_clips.mp4"}:
        return "clips"
    media = job_media_dir(work_dir)
    concat = media / "concat" / "concat.mp4"
    if concat.is_file() and body.name in {"enhanced.mp4", "compressed.mp4", "concat.mp4"}:
        return "clips"
    if not body.exists() and "concat" in body.as_posix().replace("\\", "/"):
        return "clips"
    return "source"


def _clock_from_sidecar(video: Path) -> str | None:
    names = {
        "concat.mp4": "concat_meta.json",
        "enhanced.mp4": "enhance_meta.json",
        "compressed.mp4": "compress_meta.json",
    }
    meta = video.parent / names.get(video.name, "")
    if not meta.is_file():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    clock = str(data.get("clock") or "").strip().lower()
    if clock in {"clips", "source"}:
        return clock
    return None


def inherit_clock(src: Path) -> str:
    if src.name.lower().startswith("range_") or src.name in {"concat.mp4", "_clips.mp4"}:
        return "clips"
    return _clock_from_sidecar(src) or "source"


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def probe_duration_sec(path: Path) -> float | None:
    """Read encoded duration from ffmpeg. None if the file cannot be probed."""
    if not path.is_file():
        return None
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return None
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    blob = f"{r.stderr or ''}\n{r.stdout or ''}"
    m = _DURATION_RE.search(blob)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    dur = h * 3600 + mi * 60 + s
    return dur if dur > 0.05 else None


def clip_span_durations(work_dir: Path, spans: list[tuple[float, float]]) -> list[float]:
    clips = job_media_dir(work_dir) / "clips"
    out: list[float] = []
    for i, (start, end) in enumerate(spans):
        probed = probe_duration_sec(clips / f"range_{i:02d}.mp4")
        if probed and probed > 0.05:
            out.append(probed)
        else:
            out.append(max(0.05, float(end) - float(start)))
    return out


def _vtt_timestamp(sec: float) -> str:
    t = max(0.0, float(sec))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def write_overlay_cues(
    dest_json: Path,
    dest_vtt: Path,
    cues: list[dict],
    *,
    intro_sec: float,
    clock: str,
) -> None:
    payload = {
        "clock": "remix",
        "source_clock": clock,
        "intro_sec": round(float(intro_sec), 3),
        "overlay": True,
        "cues": cues,
    }
    dest_json.parent.mkdir(parents=True, exist_ok=True)
    dest_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["WEBVTT", ""]
    for i, row in enumerate(cues, start=1):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start = float(row["start"])
        end = float(row["end"])
        if end <= start:
            continue
        ident = str(row.get("kind") or "cue")
        lines.append(f"{i}-{ident}")
        lines.append(f"{_vtt_timestamp(start)} --> {_vtt_timestamp(end)}")
        lines.append(text.replace("\n", " "))
        lines.append("")
    dest_vtt.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def caption_events_for_body(
    work_dir: Path,
    srt: Path,
    *,
    body: Path | None = None,
    shift: float = 0.0,
) -> list[tuple[float, float, str, str]]:
    from pipeline import parse_srt
    from segment_subs import remap_cues_to_spans

    segs = parse_srt(srt)
    clock = caption_clock_for_body(work_dir, body)
    if clock == "clips":
        spans = load_clip_spans(work_dir)
        if spans:
            segs = remap_cues_to_spans(
                segs, spans, durations=clip_span_durations(work_dir, spans)
            )
    out: list[tuple[float, float, str, str]] = []
    for seg in segs:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg["start"]) + float(shift)
        end = float(seg["end"]) + float(shift)
        if end > start:
            out.append((start, end, "Caption", text))
    return out


def vertical_pad_vf(width: int = REMIX_W, height: int = REMIX_H) -> str:
    w = max(2, int(width) // 2 * 2)
    h = max(2, int(height) // 2 * 2)
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=30,format=yuv420p"
    )


def _sec_to_ass(t: float) -> str:
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass_file(
    dest: Path,
    events: list[tuple[float, float, str, str]],
    *,
    width: int = REMIX_W,
    height: int = REMIX_H,
) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dialogues = []
    for start, end, style, text in events:
        if end <= start:
            continue
        body = ass_escape("\\N".join(wrap_title(text, width=18, max_lines=6) or [text]))
        if style == "Title":
            body = ass_escape("\\N".join(wrap_title(text, width=16, max_lines=4) or [text]))
        dialogues.append(
            f"Dialogue: 0,{_sec_to_ass(start)},{_sec_to_ass(end)},{style},,0,0,0,,{body}"
        )
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {int(width)}\n"
        f"PlayResY: {int(height)}\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Title,Microsoft YaHei,64,&H00FFFFFF,&H000000FF,&H64000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,3,0,5,70,70,0,1\n"
        "Style: Caption,Microsoft YaHei,42,&H00FFFFFF,&H000000FF,&H64000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,2,0,2,48,48,90,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    dest.write_text(header + "\n".join(dialogues) + "\n", encoding="utf-8")
    return dest


def _subtitles_vf(ass_path: Path) -> str:
    p = ass_path.resolve().as_posix().replace("\\", "/")
    p = p.replace(":", r"\:")
    return f"subtitles='{p}'"


def load_note_hook(work_dir: Path, prefer: str | None = None) -> dict[str, str]:
    notes = Path(work_dir) / "notes"
    order: list[str] = []
    if prefer:
        order.append(prefer)
    order.extend(["zh", "zh-Hant", "en"])
    if notes.is_dir():
        order.extend(p.name for p in notes.iterdir() if p.is_dir())
    seen: set[str] = set()
    for lang in order:
        if not lang or lang in seen:
            continue
        seen.add(lang)
        path = notes / lang / "summary.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        title = str(data.get("title") or "").strip()
        one = str(data.get("one_liner") or data.get("keypoints_intro") or "").strip()
        if title or one:
            return {"lang": lang, "title": title or one, "one_liner": one or title}
    return {"lang": prefer or "zh", "title": "", "one_liner": ""}


def find_remix_srt(work_dir: Path, lang: str | None = None) -> Path | None:
    from job_layout import list_locale_srts, locale_srt_path

    if lang:
        p = locale_srt_path(work_dir, lang)
        if p.is_file() and p.stat().st_size > 8:
            return p
    srts = list_locale_srts(work_dir)
    for key in (lang, "zh", "zh-Hant", "en"):
        if key and key in srts:
            return srts[key]
    if srts:
        return next(iter(srts.values()))
    return None


def _caption_events(srt: Path, shift: float) -> list[tuple[float, float, str, str]]:
    from pipeline import parse_srt

    out: list[tuple[float, float, str, str]] = []
    for seg in parse_srt(srt):
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg["start"]) + shift
        end = float(seg["end"]) + shift
        out.append((start, end, "Caption", text))
    return out


def _encode_clip(
    ffmpeg: str,
    dest: Path,
    *,
    inputs: list[str],
    vf: str | None,
    duration: float | None = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs]
    if vf:
        cmd.extend(["-vf", vf])
    if duration is not None:
        cmd.extend(["-t", f"{float(duration):.3f}"])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            f"{DEFAULT_AUDIO_K}k",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    _run_ffmpeg(cmd)
    if not dest.is_file() or dest.stat().st_size < 800:
        raise RuntimeError(f"ffmpeg produced empty file: {dest}")


def resolve_remix_body(work_dir: Path, *, working: Path | None = None) -> Path | None:
    from note_frames import existing_source_video

    source = existing_source_video(work_dir)
    clips = list_clip_mp4s(work_dir)
    if working is not None and working.is_file():
        if source is None or working.resolve() != source.resolve():
            return working
    if len(clips) >= MIN_CONCAT:
        return concat_videos(
            clips,
            job_remix_dir(work_dir) / "_clips.mp4",
            height=DEFAULT_COMPRESS_HEIGHT,
        )
    if len(clips) == 1:
        return clips[0]
    if working is not None and working.is_file():
        return working
    return source


def remix_vertical_notes(
    work_dir: Path,
    dest: Path | None = None,
    *,
    body: Path | None = None,
    media_opts: dict | None = None,
) -> Path:
    """WF-07 template vertical_notes: 9:16 canvas, title card, captions as overlay (not burned)."""
    work_dir = Path(work_dir)
    opts = normalize_media_opts(media_opts)
    remix_dir = job_remix_dir(work_dir)
    dest = Path(dest) if dest else remix_dir / "remix.mp4"
    src = Path(body) if body else resolve_remix_body(work_dir)
    if src is None or not src.is_file():
        raise FileNotFoundError(src or work_dir)

    hook = load_note_hook(work_dir)
    title = opts.get("remix_title") or hook.get("one_liner") or hook.get("title") or src.stem
    intro_sec = float(opts.get("remix_intro_sec") or 0)
    if not str(title).strip():
        intro_sec = 0.0

    ffmpeg = find_ffmpeg()
    intro_path = remix_dir / "_intro.mp4"
    body_path = remix_dir / "_body.mp4"
    crop_hardsubs = bool(opts.get("remix_crop_hardsubs", True))
    srt = find_remix_srt(work_dir, hook.get("lang"))
    clock = caption_clock_for_body(work_dir, src)

    body_vf = remix_body_vf(crop_hardsubs=crop_hardsubs, ass_path=None)
    pad_only = vertical_pad_vf()

    try:
        _encode_clip(ffmpeg, body_path, inputs=["-i", str(src)], vf=body_vf)
    except RuntimeError:
        if crop_hardsubs:
            try:
                _encode_clip(ffmpeg, body_path, inputs=["-i", str(src)], vf=pad_only)
                crop_hardsubs = False
            except RuntimeError:
                raise
        else:
            raise

    pieces: list[Path] = []
    intro_actual = 0.0
    if intro_sec > 0:
        intro_inputs = [
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x111111:s={REMIX_W}x{REMIX_H}:d={intro_sec}:r=30",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=44100:cl=stereo:d={intro_sec}",
        ]
        try:
            _encode_clip(
                ffmpeg,
                intro_path,
                inputs=intro_inputs,
                vf=None,
                duration=intro_sec,
            )
            pieces.append(intro_path)
            intro_actual = probe_duration_sec(intro_path) or intro_sec
        except RuntimeError:
            intro_sec = 0.0
            intro_actual = 0.0

    pieces.append(body_path)
    if len(pieces) == 1:
        if pieces[0].resolve() != dest.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pieces[0], dest)
    else:
        concat_videos(pieces, dest, height=REMIX_H)

    overlay_cues: list[dict] = []
    if intro_actual > 0 and str(title).strip():
        overlay_cues.append(
            {
                "start": 0.0,
                "end": round(intro_actual, 3),
                "kind": "title",
                "text": str(title).strip(),
            }
        )
    if srt is not None:
        for start, end, _style, text in caption_events_for_body(
            work_dir, srt, body=src, shift=intro_actual
        ):
            overlay_cues.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "kind": "caption",
                    "text": text,
                }
            )
    write_overlay_cues(
        remix_dir / "remix_cues.json",
        remix_dir / "remix.vtt",
        overlay_cues,
        intro_sec=intro_actual,
        clock=clock,
    )
    for stale in ("captions.ass", "intro.ass"):
        (remix_dir / stale).unlink(missing_ok=True)

    _write_meta(
        remix_dir / "remix_meta.json",
        {
            "template": REMIX_TEMPLATE,
            "src": src.name,
            "title": title,
            "one_liner": hook.get("one_liner") or title,
            "notes_lang": hook.get("lang"),
            "intro_sec": intro_actual or intro_sec,
            "burn_subs": False,
            "overlay": True,
            "crop_hardsubs": bool(crop_hardsubs),
            "caption_clock": clock,
            "srt": srt.name if srt else None,
            "clips": [src.name],
            "size": f"{REMIX_W}x{REMIX_H}",
            "bytes": dest.stat().st_size if dest.is_file() else 0,
        },
    )
    return dest


def _write_meta(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _run_ffmpeg(cmd: list[str]) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "ffmpeg failed").strip()
        raise RuntimeError(err[-2000:])


def _x264_out(
    ffmpeg: str,
    src: Path,
    dest: Path,
    *,
    vf: str | None,
    crf: int,
    audio_k: int = DEFAULT_AUDIO_K,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)]
    if vf:
        cmd.extend(["-vf", vf])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(int(crf)),
            "-c:a",
            "aac",
            "-b:a",
            f"{int(audio_k)}k",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    _run_ffmpeg(cmd)
    if not dest.is_file() or dest.stat().st_size < 800:
        raise RuntimeError(f"ffmpeg produced empty file: {dest}")


def enhance_video(
    src: Path,
    dest: Path,
    *,
    strength: str = DEFAULT_STRENGTH,
    max_height: int = 1080,
) -> Path:
    src = Path(src)
    dest = Path(dest)
    if not src.is_file():
        raise FileNotFoundError(src)
    opts = normalize_media_opts({"enhance_strength": strength, "enhance_max_height": max_height})
    vf = enhance_vf(opts["enhance_strength"], opts["enhance_max_height"])
    ffmpeg = find_ffmpeg()
    _x264_out(ffmpeg, src, dest, vf=vf, crf=23)
    _write_meta(
        dest.with_name("enhance_meta.json"),
        {
            "src": src.name,
            "strength": opts["enhance_strength"],
            "max_height": opts["enhance_max_height"],
            "vf": vf,
            "clock": inherit_clock(src),
            "bytes": dest.stat().st_size,
        },
    )
    return dest


def compress_video(
    src: Path,
    dest: Path,
    *,
    max_height: int = DEFAULT_COMPRESS_HEIGHT,
    crf: int = DEFAULT_CRF,
) -> Path:
    src = Path(src)
    dest = Path(dest)
    if not src.is_file():
        raise FileNotFoundError(src)
    opts = normalize_media_opts({"compress_height": max_height, "compress_crf": crf})
    vf = compress_vf(opts["compress_height"])
    ffmpeg = find_ffmpeg()
    _x264_out(ffmpeg, src, dest, vf=vf, crf=opts["compress_crf"])
    _write_meta(
        dest.with_name("compress_meta.json"),
        {
            "src": src.name,
            "max_height": opts["compress_height"],
            "crf": opts["compress_crf"],
            "vf": vf,
            "clock": inherit_clock(src),
            "bytes": dest.stat().st_size,
        },
    )
    return dest


def concat_videos(
    clips: list[Path],
    dest: Path,
    *,
    height: int = DEFAULT_COMPRESS_HEIGHT,
) -> Path:
    dest = Path(dest)
    paths = [Path(p) for p in clips if Path(p).is_file()]
    if len(paths) < MIN_CONCAT:
        raise MediaOpsError(f"concat needs at least {MIN_CONCAT} clips, got {len(paths)}")
    ffmpeg = find_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fc = concat_filter(len(paths), height)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for p in paths:
        cmd.extend(["-i", str(p)])
    cmd.extend(
        [
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            f"{DEFAULT_AUDIO_K}k",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    _run_ffmpeg(cmd)
    if not dest.is_file() or dest.stat().st_size < 800:
        raise RuntimeError(f"concat produced empty file: {dest}")
    _write_meta(
        dest.with_name("concat_meta.json"),
        {
            "clips": [p.name for p in paths],
            "height": height,
            "clock": "clips",
            "bytes": dest.stat().st_size,
        },
    )
    return dest


def cut_range_clips(
    work_dir: Path,
    clips: list[dict] | None,
    *,
    video: Path | None = None,
    max_dur: float = 60.0,
) -> list[Path]:
    """Cut range_XX.mp4 without requiring notes JSON."""
    from note_frames import existing_source_video, extract_video_clip

    spans: list[tuple[float, float]] = []
    for row in clips or []:
        if not isinstance(row, dict):
            continue
        try:
            start = float(row.get("start"))
            end = float(row.get("end"))
        except (TypeError, ValueError):
            continue
        if end > start >= 0:
            spans.append((start, end))
    if not spans:
        return []
    src = video or existing_source_video(work_dir)
    if src is None:
        raise FileNotFoundError(f"no source video in {work_dir}")
    clips_dir = job_media_dir(work_dir) / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    for old in clips_dir.glob("range_*.mp4"):
        old.unlink(missing_ok=True)
    out: list[Path] = []
    used: list[tuple[float, float]] = []
    cap = max(1.0, min(60.0, float(max_dur)))
    for i, (start, end) in enumerate(spans):
        actual_end = min(end, start + cap)
        dest = clips_dir / f"range_{i:02d}.mp4"
        extract_video_clip(src, start, actual_end, dest, max_dur=float(max_dur))
        out.append(dest)
        used.append((start, actual_end))
    if used:
        write_clips_meta(work_dir, used)
    return out


def resolve_working_video(work_dir: Path) -> Path | None:
    """Prefer last postproc product, else source."""
    from note_frames import existing_source_video

    for rel in (
        Path("remix") / "remix.mp4",
        Path("compress") / "compressed.mp4",
        Path("enhance") / "enhanced.mp4",
        Path("concat") / "concat.mp4",
    ):
        p = job_media_dir(work_dir) / rel
        if p.is_file() and p.stat().st_size > 800:
            return p
    return existing_source_video(work_dir)


def run_postproc(
    work_dir: Path,
    enabled: frozenset[str],
    *,
    media_opts: dict | None = None,
    clip_ranges: list[dict] | None = None,
) -> dict[str, Path]:
    """Run concat → enhance → compress → remix. Missing steps are skipped."""
    from note_frames import existing_source_video

    opts = normalize_media_opts(media_opts)
    produced: dict[str, Path] = {}
    source = existing_source_video(work_dir)
    working = source

    if "concat" in enabled:
        clips = list_clip_mp4s(work_dir)
        if len(clips) < MIN_CONCAT and clip_ranges:
            clips = cut_range_clips(work_dir, clip_ranges, video=source)
        if len(clips) < MIN_CONCAT:
            raise MediaOpsError(
                f"concat needs at least {MIN_CONCAT} MP4 ranges, got {len(clips)}"
            )
        dest = job_concat_dir(work_dir) / "concat.mp4"
        concat_videos(clips, dest, height=opts["compress_height"] or DEFAULT_COMPRESS_HEIGHT)
        produced["concat"] = dest
        working = dest

    if "enhance" in enabled:
        if working is None:
            raise FileNotFoundError(f"enhance needs a source video in {work_dir}")
        dest = job_enhance_dir(work_dir) / "enhanced.mp4"
        enhance_video(
            working,
            dest,
            strength=opts["enhance_strength"],
            max_height=opts["enhance_max_height"],
        )
        produced["enhance"] = dest
        working = dest

    if "compress" in enabled:
        if working is None:
            raise FileNotFoundError(f"compress needs a source video in {work_dir}")
        dest = job_compress_dir(work_dir) / "compressed.mp4"
        compress_video(
            working,
            dest,
            max_height=opts["compress_height"],
            crf=opts["compress_crf"],
        )
        produced["compress"] = dest
        working = dest

    if "remix" in enabled:
        body = resolve_remix_body(work_dir, working=working)
        if body is None:
            raise FileNotFoundError(f"remix needs a source video in {work_dir}")
        dest = job_remix_dir(work_dir) / "remix.mp4"
        remix_vertical_notes(work_dir, dest, body=body, media_opts=opts)
        produced["remix"] = dest

    return produced


def copy_derived_to_site(work_dir: Path, slug: str, site_public: Path) -> int:
    media = job_media_dir(work_dir)
    dest = Path(site_public) / "derived" / slug
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    mapping = {
        "concat.mp4": media / "concat" / "concat.mp4",
        "enhanced.mp4": media / "enhance" / "enhanced.mp4",
        "compressed.mp4": media / "compress" / "compressed.mp4",
        "remix.mp4": media / "remix" / "remix.mp4",
        "remix_cues.json": media / "remix" / "remix_cues.json",
        "remix.vtt": media / "remix" / "remix.vtt",
    }
    keep: set[str] = set()
    for name, src in mapping.items():
        min_n = 20 if name.endswith((".json", ".vtt")) else 800
        if src.is_file() and src.stat().st_size > min_n:
            (dest / name).write_bytes(src.read_bytes())
            keep.add(name)
            n += 1
    for old in dest.glob("*.mp4"):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    for extra in ("remix_cues.json", "remix.vtt"):
        if extra not in keep:
            (dest / extra).unlink(missing_ok=True)
    report = media / "publish" / "report.json"
    report_name = "publish-report.json"
    if report.is_file() and report.stat().st_size > 20:
        (dest / report_name).write_bytes(report.read_bytes())
        n += 1
    else:
        (dest / report_name).unlink(missing_ok=True)
    return n


def derived_public_urls(slug: str, site_public: Path) -> dict[str, str]:
    dest = Path(site_public) / "derived" / slug
    out: dict[str, str] = {}
    for name, key in (
        ("concat.mp4", "concat"),
        ("enhanced.mp4", "enhance"),
        ("compressed.mp4", "compress"),
        ("remix.mp4", "remix"),
        ("remix_cues.json", "remix_cues"),
        ("remix.vtt", "remix_vtt"),
        ("publish-report.json", "publish"),
    ):
        p = dest / name
        min_n = 20 if name.endswith((".json", ".vtt")) else 800
        if p.is_file() and p.stat().st_size > min_n:
            out[key] = f"/derived/{slug}/{name}"
    return out
