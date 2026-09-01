"""WF-04 enhance / WF-05 compress / WF-06 concat — local ffmpeg only."""

from __future__ import annotations

import json
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
    return {
        "enhance_strength": strength,
        "enhance_max_height": max_h,
        "compress_height": height,
        "compress_crf": crf,
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
    for i, (start, end) in enumerate(spans):
        dest = clips_dir / f"range_{i:02d}.mp4"
        extract_video_clip(src, start, end, dest, max_dur=float(max_dur))
        out.append(dest)
    return out


def resolve_working_video(work_dir: Path) -> Path | None:
    """Prefer last postproc product, else source."""
    from note_frames import existing_source_video

    for rel in (
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
    """Run concat → enhance → compress on a job work dir. Missing steps are skipped."""
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
    }
    keep: set[str] = set()
    for name, src in mapping.items():
        if src.is_file() and src.stat().st_size > 800:
            (dest / name).write_bytes(src.read_bytes())
            keep.add(name)
            n += 1
    for old in dest.glob("*.mp4"):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    return n


def derived_public_urls(slug: str, site_public: Path) -> dict[str, str]:
    dest = Path(site_public) / "derived" / slug
    out: dict[str, str] = {}
    for name, key in (
        ("concat.mp4", "concat"),
        ("enhanced.mp4", "enhance"),
        ("compressed.mp4", "compress"),
    ):
        p = dest / name
        if p.is_file() and p.stat().st_size > 800:
            out[key] = f"/derived/{slug}/{name}"
    return out
