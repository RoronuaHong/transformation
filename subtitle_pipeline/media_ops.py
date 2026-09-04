"""WF-04 enhance / WF-05 compress / WF-06 concat — local ffmpeg only."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
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
HARDSUB_VLM_MODEL = os.environ.get("VITUAL_DEHARDSUB_VLM", "gemma4:e2b")
DEHARDSUB_MODES = ("auto", "vlm", "band", "delogo", "fill", "crop")
DEHARDSUB_ENGINES = ("sttn", "opencv")
DEFAULT_DEHARDSUB_ENGINE = (
    str(os.environ.get("VITUAL_DEHARDSUB_ENGINE", "sttn") or "sttn").strip().lower()
)
DEFAULT_CLEAN_PASSES = max(1, int(os.environ.get("VITUAL_CLEAN_PASSES", "2") or 2))
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


def job_dehardsub_dir(work_dir: Path) -> Path:
    d = job_media_dir(work_dir) / "dehardsub"
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
    remix_lang = str(src.get("remix_lang") or "").strip() or None
    force_dehardsub = src.get("dehardsub_force", False)
    if isinstance(force_dehardsub, str):
        force_dehardsub = force_dehardsub.strip().lower() not in ("0", "false", "no", "off")
    try:
        dehardsub_ratio = float(src.get("dehardsub_ratio", HARDSUB_CROP_RATIO))
    except (TypeError, ValueError):
        dehardsub_ratio = HARDSUB_CROP_RATIO
    dehardsub_ratio = max(0.06, min(0.28, float(dehardsub_ratio)))
    dehardsub_mode = str(src.get("dehardsub_mode") or "auto").strip().lower()
    if dehardsub_mode not in DEHARDSUB_MODES:
        dehardsub_mode = "auto"
    dehardsub_vlm_model = str(
        src.get("dehardsub_vlm_model") or HARDSUB_VLM_MODEL
    ).strip() or HARDSUB_VLM_MODEL
    try:
        dehardsub_passes = int(src.get("dehardsub_passes", DEFAULT_CLEAN_PASSES))
    except (TypeError, ValueError):
        dehardsub_passes = DEFAULT_CLEAN_PASSES
    dehardsub_passes = max(1, min(6, dehardsub_passes))
    demosaic = src.get("dehardsub_demosaic", True)
    if isinstance(demosaic, str):
        demosaic = demosaic.strip().lower() not in ("0", "false", "no", "off")
    dehardsub_engine = str(
        src.get("dehardsub_engine") or DEFAULT_DEHARDSUB_ENGINE
    ).strip().lower()
    if dehardsub_engine not in DEHARDSUB_ENGINES:
        dehardsub_engine = "sttn"
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
        "remix_lang": remix_lang,
        "dehardsub_force": bool(force_dehardsub),
        "dehardsub_ratio": dehardsub_ratio,
        "dehardsub_mode": dehardsub_mode,
        "dehardsub_vlm_model": dehardsub_vlm_model,
        "dehardsub_passes": dehardsub_passes,
        "dehardsub_demosaic": bool(demosaic),
        "dehardsub_engine": dehardsub_engine,
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
            f"[{i}:a]aresample=44100:async=1,aformat=sample_fmts=fltp:channel_layouts=stereo,"
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
    """Legacy: drop the bottom band (shrinks frame). Prefer delogo on a tight box."""
    keep = max(0.5, min(0.95, 1.0 - float(ratio)))
    return f"crop=iw:trunc(ih*{keep}/2)*2:0:0"


def probe_video_wh(path: Path) -> tuple[int, int] | None:
    """Return (width, height) from ffmpeg -i probe."""
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
    err = (r.stderr or "") + (r.stdout or "")
    m = re.search(r"Stream #.*Video:.*?,\s*(\d{2,5})x(\d{2,5})", err)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def hardsub_band_box(
    width: int,
    height: int,
    ratio: float = HARDSUB_CROP_RATIO,
) -> dict[str, int]:
    """Legacy full-width bottom band (fallback when locate fails)."""
    w = max(2, int(width))
    h = max(2, int(height))
    ratio = max(0.06, min(0.28, float(ratio)))
    band_h = max(8, int(round(h * ratio)))
    if band_h % 2:
        band_h += 1
    band_h = min(band_h, h - 2)
    y = h - band_h
    x = 1
    box_w = max(2, w - 2)
    return {"x": x, "y": y, "w": box_w, "h": band_h, "width": w, "height": h}


def _clamp_delogo_box(box: dict[str, int], width: int, height: int) -> dict[str, int]:
    """ffmpeg delogo needs 1px margin from every edge."""
    w = max(2, int(width))
    h = max(2, int(height))
    x = max(1, min(w - 3, int(box.get("x", 1))))
    y = max(1, min(h - 3, int(box.get("y", 1))))
    bw = max(2, int(box.get("w", 2)))
    bh = max(2, int(box.get("h", 2)))
    if x + bw >= w:
        bw = w - x - 1
    if y + bh >= h:
        bh = h - y - 1
    bw = max(2, bw)
    bh = max(2, bh)
    return {"x": x, "y": y, "w": bw, "h": bh, "width": w, "height": h}


def box_from_norm(
    width: int,
    height: int,
    *,
    x: float,
    y: float,
    w: float,
    h: float,
    pad: float = 0.01,
) -> dict[str, int]:
    """Normalized fractions → pixel box with small padding."""
    x = max(0.0, min(0.98, float(x) - pad))
    y = max(0.0, min(0.98, float(y) - pad * 0.5))
    w = max(0.02, min(1.0 - x, float(w) + 2 * pad))
    h = max(0.02, min(1.0 - y, float(h) + pad))
    return _clamp_delogo_box(
        {
            "x": int(round(x * width)),
            "y": int(round(y * height)),
            "w": int(round(w * width)),
            "h": int(round(h * height)),
        },
        width,
        height,
    )


def hardsub_delogo_vf(width: int, height: int, ratio: float = HARDSUB_CROP_RATIO) -> str:
    """ffmpeg delogo on legacy full-width band."""
    box = hardsub_band_box(width, height, ratio)
    return hardsub_delogo_vf_box(box)


def hardsub_delogo_vf_box(box: dict[str, int]) -> str:
    return f"delogo=x={box['x']}:y={box['y']}:w={box['w']}:h={box['h']}:show=0"


def hardsub_fill_filter(
    width: int,
    height: int,
    ratio: float = HARDSUB_CROP_RATIO,
) -> str:
    """Cover caption band with blurred pixels cloned from just above (full frame)."""
    return hardsub_fill_filter_box(width, height, hardsub_band_box(width, height, ratio))


def hardsub_fill_filter_box(width: int, height: int, box: dict[str, int]) -> str:
    band_h = int(box["h"])
    band_y = int(box["y"])
    src_y = max(0, band_y - max(8, band_h))
    return (
        f"[0:v]split=2[base][ref];"
        f"[ref]crop={width}:{band_h}:0:{src_y},gblur=sigma=14:steps=2[fill];"
        f"[base][fill]overlay=0:{band_y}:format=auto,format=yuv420p[vout]"
    )


def _grab_gray_frame(video: Path, t_sec: float, *, width: int, height: int) -> bytes | None:
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return None
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, float(t_sec)):.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        f"format=gray,scale={width}:{height}",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout or len(r.stdout) != width * height:
        return None
    return r.stdout


def _grab_jpeg_frame(video: Path, t_sec: float, dest: Path) -> Path | None:
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, float(t_sec)):.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(dest),
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not dest.is_file() or dest.stat().st_size < 200:
        return None
    return dest


def locate_hardsub_box_band(
    video: Path,
    width: int,
    height: int,
) -> dict[str, Any] | None:
    """Locate a tight bottom-center caption box via bright-pixel scan (no LLM)."""
    video = Path(video)
    dur = probe_duration_sec(video) or 0.0
    if dur <= 0.4:
        return None
    aw, ah = 320, 180
    y0s: list[int] = []
    y1s: list[int] = []
    x0s: list[int] = []
    x1s: list[int] = []
    for t in [dur * x for x in (0.15, 0.3, 0.45, 0.6, 0.75, 0.9)]:
        raw = _grab_gray_frame(video, t, width=aw, height=ah)
        if raw is None:
            continue
        # Bottom 22%, center 80% — avoid left tier labels / logos / mid-frame UI.
        y_lo = int(ah * 0.78)
        x_lo, x_hi = int(aw * 0.12), int(aw * 0.88)
        region_w = x_hi - x_lo
        region_h = ah - y_lo
        region = memoryview(raw)
        # row scores
        row_score = [0.0] * region_h
        for ry in range(region_h):
            off = (y_lo + ry) * aw + x_lo
            row = region[off : off + region_w]
            bright = sum(1 for b in row if b >= 205) / max(1, region_w)
            row_score[ry] = bright
        peak = max(row_score) if row_score else 0.0
        if peak < 0.015:
            continue
        thr = max(0.015, peak * 0.4)
        rows = [i for i, s in enumerate(row_score) if s >= thr]
        if not rows:
            continue
        ry0, ry1 = rows[0], rows[-1] + 1
        pad = max(2, (ry1 - ry0) // 2 + 2)
        ry0 = max(0, ry0 - pad)
        ry1 = min(region_h, ry1 + pad)
        col_score = [0.0] * region_w
        for ry in range(ry0, ry1):
            off = (y_lo + ry) * aw + x_lo
            row = region[off : off + region_w]
            for cx, b in enumerate(row):
                if b >= 205:
                    col_score[cx] += 1.0
        cpeak = max(col_score) if col_score else 0.0
        if cpeak <= 0:
            cx0, cx1 = 0, region_w
        else:
            cthr = max(1.0, cpeak * 0.25)
            cols = [i for i, s in enumerate(col_score) if s >= cthr]
            if not cols:
                cx0, cx1 = 0, region_w
            else:
                cx0, cx1 = cols[0], cols[-1] + 1
                cpad = max(4, (cx1 - cx0) // 8)
                cx0 = max(0, cx0 - cpad)
                cx1 = min(region_w, cx1 + cpad)
        y0s.append(y_lo + ry0)
        y1s.append(y_lo + ry1)
        x0s.append(x_lo + cx0)
        x1s.append(x_lo + cx1)
    if not y0s:
        return None
    nx0, nx1 = min(x0s) / aw, max(x1s) / aw
    ny0, ny1 = min(y0s) / ah, max(y1s) / ah
    box = box_from_norm(
        width, height, x=nx0, y=ny0, w=nx1 - nx0, h=ny1 - ny0, pad=0.008
    )
    return {
        "box": box,
        "norm": {"x": nx0, "y": ny0, "w": nx1 - nx0, "h": ny1 - ny0},
        "hits": len(y0s),
        "source": "band",
    }


def _ollama_host() -> str:
    return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


def _ollama_vision_bbox(
    jpeg: Path,
    *,
    model: str,
    timeout: int = 180,
) -> dict[str, Any] | None:
    """Ask a local vision model for a normalized subtitle box."""
    b64 = base64.b64encode(jpeg.read_bytes()).decode("ascii")
    prompt = (
        "Locate the white dialogue subtitle near the BOTTOM of the frame "
        "(Chinese/English burned-in caption with outline). "
        "IGNORE logos, watermarks, usernames, and left-side UI labels. "
        'Reply JSON only: {"has_subs":true,"x":0.2,"y":0.85,"w":0.6,"h":0.08} '
        "x,y,w,h are fractions of image width/height; y is usually > 0.72. "
        "If none: {\"has_subs\":false,\"x\":0,\"y\":0,\"w\":0,\"h\":0}."
    )
    body = {
        "model": model,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0},
        "messages": [{"role": "user", "content": prompt, "images": [b64]}],
    }
    req = urllib.request.Request(
        f"{_ollama_host()}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    msg = data.get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        text = (msg.get("thinking") or "").strip()
    if not text:
        return None
    try:
        from pipeline import extract_json_object

        parsed = extract_json_object(text)
    except Exception:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return None
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    if not isinstance(parsed, dict) or not parsed.get("has_subs"):
        return None
    try:
        x, y, w, h = float(parsed["x"]), float(parsed["y"]), float(parsed["w"]), float(parsed["h"])
    except (KeyError, TypeError, ValueError):
        return None
    # Reject boxes that are clearly not bottom dialogue captions.
    if y < 0.72 or h > 0.22 or w < 0.08 or h < 0.02:
        return None
    if y + h > 1.05:
        return None
    return {"x": x, "y": y, "w": w, "h": h}


def locate_hardsub_box_vlm(
    video: Path,
    width: int,
    height: int,
    *,
    model: str = HARDSUB_VLM_MODEL,
    work_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Sample frames → Ollama vision → union of valid bottom caption boxes."""
    video = Path(video)
    dur = probe_duration_sec(video) or 0.0
    if dur <= 0.4:
        return None
    tmp = Path(work_dir) / "dehardsub" / "_vlm" if work_dir else video.parent / "_vlm_frames"
    tmp.mkdir(parents=True, exist_ok=True)
    norms: list[dict[str, float]] = []
    for i, frac in enumerate((0.25, 0.5, 0.75)):
        jpg = tmp / f"frame_{i}.jpg"
        if _grab_jpeg_frame(video, dur * frac, jpg) is None:
            continue
        hit = _ollama_vision_bbox(jpg, model=model)
        if hit:
            norms.append(hit)
    if not norms:
        return None
    x0 = min(n["x"] for n in norms)
    y0 = min(n["y"] for n in norms)
    x1 = max(n["x"] + n["w"] for n in norms)
    y1 = max(n["y"] + n["h"] for n in norms)
    box = box_from_norm(width, height, x=x0, y=y0, w=x1 - x0, h=y1 - y0, pad=0.012)
    return {
        "box": box,
        "norm": {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0},
        "hits": len(norms),
        "samples": 3,
        "model": model,
        "source": "vlm",
    }


def resolve_hardsub_box(
    video: Path,
    width: int,
    height: int,
    *,
    mode: str,
    ratio: float = HARDSUB_CROP_RATIO,
    vlm_model: str = HARDSUB_VLM_MODEL,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """Pick caption box: vlm → band scan → full-width ratio fallback."""
    mode_s = (mode or "vlm").strip().lower()
    locate: dict[str, Any] | None = None
    if mode_s == "vlm":
        locate = locate_hardsub_box_vlm(
            video, width, height, model=vlm_model, work_dir=work_dir
        )
        if locate is None:
            locate = locate_hardsub_box_band(video, width, height)
    elif mode_s == "band":
        locate = locate_hardsub_box_band(video, width, height)
    if locate and locate.get("box"):
        return locate
    box = hardsub_band_box(width, height, ratio)
    return {
        "box": box,
        "norm": {
            "x": box["x"] / width,
            "y": box["y"] / height,
            "w": box["w"] / width,
            "h": box["h"] / height,
        },
        "hits": 0,
        "source": "ratio",
    }


def _encode_dehardsub_fill(
    ffmpeg: str,
    src: Path,
    dest: Path,
    *,
    width: int,
    height: int,
    box: dict[str, int],
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fc = hardsub_fill_filter_box(width, height, box)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-filter_complex",
        fc,
        "-map",
        "[vout]",
    ]
    if has_audio_stream(src):
        cmd.extend(["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{DEFAULT_AUDIO_K}k"])
    else:
        cmd.append("-an")
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    _run_ffmpeg(cmd)
    if not dest.is_file() or dest.stat().st_size < 800:
        raise RuntimeError(f"dehardsub fill produced empty file: {dest}")


def _encode_dehardsub_delogo(
    ffmpeg: str,
    src: Path,
    dest: Path,
    *,
    box: dict[str, int],
) -> None:
    vf = hardsub_delogo_vf_box(box)
    _x264_out(ffmpeg, src, dest, vf=vf, crf=23)


def _hardsub_chroma_mask(bgr: Any) -> Any:
    """True where pixels look like colorful UI / avatar (must not be inpainted)."""
    import cv2
    import numpy as np

    b = bgr[:, :, 0].astype(np.int16)
    g = bgr[:, :, 1].astype(np.int16)
    r = bgr[:, :, 2].astype(np.int16)
    chroma = (np.abs(r - g) > 18) | (np.abs(g - b) > 18) | (np.abs(r - b) > 18)
    return cv2.dilate(chroma.astype(np.uint8), np.ones((5, 5), np.uint8), iterations=2) > 0


def _hardsub_glyph_mask(gray: Any, bgr: Any | None = None) -> Any:
    """Mask hardsub line as a padded strip (not glyph-shaped).

    Glyph-shaped fills leave dark gaps between letters that still read as
    character silhouettes. Expand the swallowed glyph islands to a rectangle
    (left avatars / chroma UI still protected).
    """
    import cv2
    import numpy as np

    # Adaptive to compressed 1080p: pure white is rare; bg is usually dark UI.
    bg_est = float(np.percentile(gray, 40))
    thr = max(170.0, min(210.0, bg_est + 90.0))
    bright = gray >= thr
    # Anti-aliased fringe next to bright cores.
    near_core = cv2.dilate(bright.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1) > 0
    bright |= (gray >= max(140.0, thr - 35.0)) & near_core
    h, w = gray.shape
    left = min(180, max(48, w // 5))
    edge = max(12, w // 14)
    if bgr is not None:
        protect = _hardsub_chroma_mask(bgr)
        bright[protect] = False
        # Captions sit in the center; always spare left avatars / tier icons.
        bright[:, :left] = False
        bright[:, w - edge :] = False
    if not np.any(bright):
        return np.zeros_like(gray, dtype=np.uint8)
    core = bright.astype(np.uint8) * 255
    core = cv2.morphologyEx(core, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    # Swallow black outline + compression ringing around letters.
    mask = cv2.dilate(core, np.ones((7, 7), np.uint8), iterations=2)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 5), np.uint8), iterations=1)
    # Merge into one strip so fill is not letter-shaped.
    ys = np.where(mask.max(axis=1) > 0)[0]
    xs = np.where(mask.max(axis=0) > 0)[0]
    if ys.size and xs.size:
        y0 = max(0, int(ys.min()) - 4)
        y1 = min(h, int(ys.max()) + 5)
        x0 = max(left, int(xs.min()) - 12)
        x1 = min(w - edge, int(xs.max()) + 12)
        strip = np.zeros_like(mask)
        strip[y0:y1, x0:x1] = 255
        mask = strip
    if bgr is not None:
        mask[_hardsub_chroma_mask(bgr)] = 0
        mask[:, :left] = 0
        mask[:, w - edge :] = 0
    return mask


def _restore_hardsub_roi(
    roi: Any,
    mask_u8: Any,
    bg: Any | None,
) -> tuple[Any, Any]:
    """Restore glyph pixels: solid median on flat UI bands, else inpaint + temporal."""
    import cv2
    import numpy as np

    out = roi.copy()
    m = mask_u8 > 0
    if bg is None:
        bg = roi.astype(np.float32)

    if not np.any(m):
        bg = bg * 0.9 + roi.astype(np.float32) * 0.1
        return out, bg

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    keep = ~m
    fill = None
    uniform = False
    # Sample true bg far from glyphs so black outlines don't poison the fill color.
    dist = cv2.distanceTransform(keep.astype(np.uint8), cv2.DIST_L2, 5)
    far = (dist >= 8) & (gray < 130) & (gray > 20)
    if float(far.mean()) < 0.02:
        far = keep & (gray < 130) & (gray > 20)
    if float(far.mean()) >= 0.02:
        var = float(np.var(gray[far]))
        uniform = var < 160.0
        fill = np.median(roi[far].reshape(-1, 3), axis=0).astype(np.uint8)

    if uniform and fill is not None:
        # Row-wise median from far pixels (or global fill) keeps horizontal grain
        # better than stamping one color, while staying silhouette-free.
        for ry in range(out.shape[0]):
            if not np.any(m[ry]):
                continue
            far_r = far[ry] if far is not None else None
            if far_r is not None and int(far_r.sum()) >= 8:
                row_fill = np.median(roi[ry][far_r].reshape(-1, 3), axis=0).astype(np.uint8)
            else:
                row_fill = fill
            out[ry, m[ry]] = row_fill
    else:
        spatial = cv2.inpaint(roi, mask_u8, 9, cv2.INPAINT_TELEA)
        out[m] = spatial[m]
        bg_u8 = np.clip(bg, 0, 255).astype(np.uint8)
        bg_gray = cv2.cvtColor(bg_u8, cv2.COLOR_BGR2GRAY)
        use_temporal = m & (bg_gray < 170)
        if np.any(use_temporal):
            blend = spatial.astype(np.float32) * 0.35 + bg_u8.astype(np.float32) * 0.65
            out[use_temporal] = np.clip(blend, 0, 255).astype(np.uint8)[use_temporal]

    # Kill leftover structure (dark ghosts) inside the strip mask.
    if fill is not None:
        g2 = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
        fill_g = float(np.median(fill))
        resid = m & (np.abs(g2.astype(np.float32) - fill_g) > 6)
        if np.any(resid):
            out[resid] = fill

    bg = bg.copy()
    clean = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY) < 170
    bg[clean] = bg[clean] * 0.7 + out[clean].astype(np.float32) * 0.3
    return out, bg


def refine_hardsub_box_glyphs(
    video: Path,
    width: int,
    height: int,
    seed: dict[str, int],
) -> dict[str, int]:
    """Small pad around seed only — do not enlarge into the picture above captions."""
    sx, sy, sw, sh = int(seed["x"]), int(seed["y"]), int(seed["w"]), int(seed["h"])
    pad_x, pad_y = 4, max(2, min(6, sh // 3))
    return _clamp_delogo_box(
        {
            "x": max(1, sx - pad_x),
            "y": max(1, sy - pad_y),
            "w": sw + 2 * pad_x,
            "h": sh + 2 * pad_y,
        },
        width,
        height,
    )


def _encode_dehardsub_inpaint(
    ffmpeg: str,
    src: Path,
    dest: Path,
    *,
    box: dict[str, int],
) -> None:
    """Remove hardsub glyphs inside box via temporal/spatial fill (full frame kept)."""
    import cv2
    import numpy as np

    dest.parent.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video for inpaint: {src}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0)
    if fps <= 1e-3:
        fps = 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or box.get("width") or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or box.get("height") or 0)
    if w < 2 or h < 2:
        cap.release()
        raise RuntimeError(f"bad video size for inpaint: {src}")
    x, y, bw, bh = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    x = max(0, min(w - 2, x))
    y = max(0, min(h - 2, y))
    bw = max(2, min(w - x, bw))
    bh = max(2, min(h - y, bh))
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{w}x{h}",
        "-r",
        f"{fps:.4f}",
        "-i",
        "pipe:0",
        "-i",
        str(src),
        "-map",
        "0:v:0",
    ]
    if has_audio_stream(src):
        cmd.extend(["-map", "1:a:0?", "-c:a", "aac", "-b:a", f"{DEFAULT_AUDIO_K}k"])
    else:
        cmd.append("-an")
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
            "-movflags",
            "+faststart",
            "-shortest",
            str(dest),
        ]
    )
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    bg: Any | None = None
    frames = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            roi = frame[y : y + bh, x : x + bw]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            mask_u8 = _hardsub_glyph_mask(gray, bgr=roi)
            if int(mask_u8.max()) > 0:
                restored, bg = _restore_hardsub_roi(roi, mask_u8, bg)
                frame[y : y + bh, x : x + bw] = restored
            elif bg is None:
                bg = roi.astype(np.float32)
            else:
                bg = bg * 0.9 + roi.astype(np.float32) * 0.1
            proc.stdin.write(np.ascontiguousarray(frame).tobytes())
            frames += 1
    except Exception:
        proc.kill()
        raise
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    stderr = proc.communicate(timeout=180)[1]
    if proc.returncode != 0 or frames < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"dehardsub inpaint ffmpeg failed ({proc.returncode}): {err}")
    if not dest.is_file() or dest.stat().st_size < 800:
        raise RuntimeError(f"dehardsub inpaint produced empty file: {dest}")


def _bottom_band_gray_bytes(
    video: Path,
    t_sec: float,
    *,
    band_ratio: float = 0.18,
    width: int = 160,
) -> bytes | None:
    """Grab one grayscale bottom-band frame as raw bytes (no Pillow)."""
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return None
    h = max(12, int(round(width * float(band_ratio) * 2.2)))
    vf = (
        f"crop=iw:trunc(ih*{band_ratio:.3f}/2)*2:0:ih-trunc(ih*{band_ratio:.3f}/2)*2,"
        f"format=gray,scale={width}:{h}"
    )
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, float(t_sec)):.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        vf,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout


def score_hardsub_band(raw: bytes) -> dict[str, float]:
    """Heuristic: burned captions → bright glyphs + high local contrast in bottom band."""
    if not raw:
        return {"score": 0.0, "mean": 0.0, "std": 0.0, "bright_frac": 0.0}
    n = len(raw)
    mean = sum(raw) / n
    var = sum((b - mean) ** 2 for b in raw) / n
    std = var**0.5
    bright = sum(1 for b in raw if b >= 200) / n
    score = min(1.0, bright * 5.0 + min(std, 70.0) / 140.0)
    return {
        "score": round(score, 4),
        "mean": round(mean, 2),
        "std": round(std, 2),
        "bright_frac": round(bright, 4),
    }


def detect_hardsubs(
    video: Path,
    *,
    samples: int = 5,
    band_ratio: float = 0.18,
) -> dict[str, Any]:
    """Judge whether the video has bottom-burned captions (no OCR model)."""
    video = Path(video)
    dur = probe_duration_sec(video) or 0.0
    if dur <= 0.4 or not video.is_file():
        return {
            "detected": False,
            "score": 0.0,
            "hits": 0,
            "samples": 0,
            "reason": "no_duration",
        }
    n = max(3, min(8, int(samples)))
    times = [dur * (i + 1) / (n + 1) for i in range(n)]
    frame_scores: list[float] = []
    details: list[dict[str, float]] = []
    for t in times:
        raw = _bottom_band_gray_bytes(video, t, band_ratio=band_ratio)
        if raw is None:
            continue
        row = score_hardsub_band(raw)
        frame_scores.append(float(row["score"]))
        details.append(row)
    if not frame_scores:
        return {
            "detected": False,
            "score": 0.0,
            "hits": 0,
            "samples": 0,
            "reason": "probe_failed",
        }
    avg = sum(frame_scores) / len(frame_scores)
    hits = sum(1 for s in frame_scores if s >= 0.28)
    need_hits = max(2, len(frame_scores) // 2)
    detected = avg >= 0.24 and hits >= need_hits
    return {
        "detected": detected,
        "score": round(avg, 4),
        "hits": hits,
        "samples": len(frame_scores),
        "need_hits": need_hits,
        "band_ratio": band_ratio,
        "frames": details,
        "reason": "hardsub_band" if detected else "below_threshold",
    }


def strip_hardsubs(
    work_dir: Path,
    *,
    video: Path | None = None,
    force: bool = False,
    ratio: float = HARDSUB_CROP_RATIO,
    mode: str = "auto",
    vlm_model: str | None = None,
    passes: int | None = None,
    demosaic: bool = True,
    engine: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Remove burned-in captions (+ optional mosaic) — keep full frame.

    ``mode``:
      - auto (default): STTN inpaint of the located caption box (no glyph mask)
      - vlm / band: same STTN path; locate via VLM or bright-band
      - delogo / fill / crop: legacy single-pass paths
    ``engine``: ``sttn`` (default) or ``opencv`` (legacy glyph fill, tests only).
    """
    from note_frames import existing_source_video

    work_dir = Path(work_dir)
    src = Path(video) if video else existing_source_video(work_dir)
    if src is None or not src.is_file():
        raise FileNotFoundError(f"dehardsub needs a source video in {work_dir}")
    dest_dir = job_dehardsub_dir(work_dir)
    dest = dest_dir / "clean.mp4"
    meta_path = dest_dir / "dehardsub_meta.json"
    mode_s = (mode or "auto").strip().lower()
    if mode_s not in DEHARDSUB_MODES:
        mode_s = "auto"
    model = (vlm_model or HARDSUB_VLM_MODEL).strip() or HARDSUB_VLM_MODEL
    max_passes = DEFAULT_CLEAN_PASSES if passes is None else max(1, min(6, int(passes)))
    engine_s = (engine or DEFAULT_DEHARDSUB_ENGINE).strip().lower()
    if engine_s not in DEHARDSUB_ENGINES:
        engine_s = "sttn"
    report = detect_hardsubs(src, band_ratio=max(0.12, float(ratio)))
    report.update(
        {
            "src": src.name,
            "force": bool(force),
            "ratio": round(float(ratio), 4),
            "mode": mode_s,
            "vlm_model": model,
            "passes": max_passes,
            "demosaic": bool(demosaic),
            "engine": engine_s,
        }
    )
    if not report.get("detected") and not force and mode_s not in ("auto",):
        # auto still runs mosaic scan even if hardsub heuristic misses
        report["action"] = "skip"
        _write_meta(meta_path, report)
        print(
            f"[dehardsub] skip — no burned captions "
            f"(score={report.get('score')} hits={report.get('hits')}/{report.get('samples')})"
        )
        return src, report

    # auto: always attempt multipass when forced OR hardsubs detected OR demosaic on
    if mode_s == "auto" and not report.get("detected") and not force and not demosaic:
        report["action"] = "skip"
        _write_meta(meta_path, report)
        print("[dehardsub] skip — nothing to clean")
        return src, report

    wh = probe_video_wh(src)
    if wh is None:
        raise RuntimeError(f"cannot probe video size: {src}")
    width, height = wh
    report["src_size"] = f"{width}x{height}"

    ffmpeg = find_ffmpeg()
    encode_dest = dest
    if src.resolve() == dest.resolve():
        encode_dest = dest_dir / "_clean_next.mp4"

    box: dict[str, int] = hardsub_band_box(width, height, ratio)
    action = "skip"

    if mode_s == "crop":
        box = hardsub_band_box(width, height, ratio)
        report["box"] = box
        report["box_source"] = "ratio"
        _x264_out(ffmpeg, src, encode_dest, vf=hardsub_crop_vf(ratio), crf=23)
        action = "crop"
    elif mode_s == "delogo":
        box = hardsub_band_box(width, height, ratio)
        report["box"] = box
        report["box_source"] = "ratio"
        _encode_dehardsub_delogo(ffmpeg, src, encode_dest, box=box)
        action = "delogo"
    elif mode_s == "fill":
        located = resolve_hardsub_box(
            src, width, height, mode="band", ratio=ratio, work_dir=work_dir
        )
        box = located["box"]
        report["box"] = box
        report["box_source"] = located.get("source")
        report["locate"] = {k: located.get(k) for k in ("norm", "hits", "source")}
        try:
            _encode_dehardsub_fill(
                ffmpeg, src, encode_dest, width=width, height=height, box=box
            )
            action = "fill"
        except RuntimeError:
            _encode_dehardsub_delogo(ffmpeg, src, encode_dest, box=box)
            action = "delogo_fallback"
    else:
        # auto / vlm / band → multipass hardsub (+ mosaic)
        from visual_cleanup import run_multipass_cleanup

        locate_mode = "vlm" if mode_s in ("auto", "vlm") else "band"
        try:
            cleanup = run_multipass_cleanup(
                src,
                encode_dest,
                work_dir=work_dir,
                locate_mode=locate_mode,
                vlm_model=model,
                ratio=ratio,
                max_passes=max_passes,
                demosaic=bool(demosaic),
                dehardsub=bool(report.get("detected") or force or mode_s != "auto"),
                engine=engine_s,
            )
            report.update(cleanup)
            box = cleanup.get("box") or box
            action = str(cleanup.get("action") or "multipass_cleanup")
        except Exception as exc:
            print(f"[dehardsub] multipass failed ({exc}); falling back to single inpaint")
            located = resolve_hardsub_box(
                src,
                width,
                height,
                mode=locate_mode,
                ratio=ratio,
                vlm_model=model,
                work_dir=work_dir,
            )
            box = refine_hardsub_box_glyphs(src, width, height, located["box"])
            report["box"] = box
            report["box_seed"] = located.get("box")
            report["box_source"] = located.get("source")
            try:
                _encode_dehardsub_inpaint(ffmpeg, src, encode_dest, box=box)
                action = f"inpaint_{located.get('source') or 'box'}"
            except Exception as exc2:
                print(f"[dehardsub] inpaint failed ({exc2}); falling back to delogo")
                _encode_dehardsub_delogo(ffmpeg, src, encode_dest, box=box)
                action = f"delogo_{located.get('source') or 'box'}"

    if encode_dest.resolve() != dest.resolve():
        encode_dest.replace(dest)

    out_wh = probe_video_wh(dest)
    report["action"] = action
    report["dest"] = dest.name
    report["bytes"] = dest.stat().st_size
    report["out_size"] = f"{out_wh[0]}x{out_wh[1]}" if out_wh else None
    report["clock"] = inherit_clock(src)
    if "box" not in report:
        report["box"] = box
    _write_meta(meta_path, report)
    print(
        f"[dehardsub] {action} passes={report.get('pass_count') or 1} "
        f"box={box.get('x')},{box.get('y')} {box.get('w')}x{box.get('h')} "
        f"{report.get('src_size')}→{report.get('out_size')} "
        f"→ {dest.name} (score={report.get('score')} hits={report.get('hits')})"
    )
    return dest, report


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
    clips_dir = job_media_dir(work_dir) / "clips"
    clean: list[dict[str, float | str]] = []
    for i, (start, end) in enumerate(spans):
        start_f = float(start)
        end_f = float(end)
        if end_f <= start_f:
            continue
        clip_path = clips_dir / f"range_{i:02d}.mp4"
        probed = probe_duration_sec(clip_path) if clip_path.is_file() else None
        dur = probed if probed and probed > 0.05 else max(0.05, end_f - start_f)
        clean.append(
            {
                "file": f"range_{i:02d}.mp4",
                "start": round(start_f, 3),
                "end": round(end_f, 3),
                "duration": round(dur, 3),
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
    if body.name == "_src_av.mp4":
        # No sidecar: remux of clip body still needs clips clock (name alone looks like source).
        if load_clip_spans(work_dir):
            return "clips"
        return "source"
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
        "_src_av.mp4": "_src_av_meta.json",
        "_clips.mp4": "concat_meta.json",
    }
    meta_name = names.get(video.name)
    if not meta_name:
        return None
    meta = video.parent / meta_name
    if video.name == "_clips.mp4" and not meta.is_file():
        meta = video.parent.parent / "concat" / "concat_meta.json"
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


def _ffprobe_bin(ffmpeg: str) -> str | None:
    exe = Path(ffmpeg)
    names = ("ffprobe.exe", "ffprobe") if exe.suffix.lower() == ".exe" else ("ffprobe", "ffprobe.exe")
    for name in names:
        cand = exe.with_name(name)
        if cand.is_file():
            return str(cand)
    which = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    return which


def _parse_duration_text(raw: str | None) -> float | None:
    text = (raw or "").strip().splitlines()
    if not text:
        return None
    token = text[0].strip().lower()
    if token in {"n/a", "nan", ""}:
        return None
    try:
        dur = float(token)
    except ValueError:
        return None
    return dur if dur > 0.05 else None


def probe_duration_sec(path: Path) -> float | None:
    """Video-clock duration of an encoded file. None if it cannot be probed.

    Prefers the video stream so AAC padding does not stretch the overlay shift.
    """
    if not path.is_file():
        return None
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return None
    probe = _ffprobe_bin(ffmpeg)
    if probe:
        for args in (
            [
                probe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
        ):
            r = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            dur = _parse_duration_text(r.stdout)
            if dur:
                return dur
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


def scale_span_durations(durs: list[float], body_dur: float | None) -> list[float]:
    """Stretch clip lengths so their sum matches the encoded body the player shows."""
    if not durs:
        return durs
    total = sum(durs)
    try:
        target = float(body_dur) if body_dur is not None else 0.0
    except (TypeError, ValueError):
        target = 0.0
    if total <= 0.05 or target <= 0.05:
        return durs
    scale = target / total
    if abs(scale - 1.0) < 0.001:
        return durs
    return [d * scale for d in durs]


def intro_duration_in_remix(remix: Path, body: Path, fallback: float) -> float:
    """Intro length on the concatenated remix clock (remix minus encoded body)."""
    remix_d = probe_duration_sec(remix)
    body_d = probe_duration_sec(body)
    if remix_d and body_d and remix_d > body_d + 0.04:
        return round(remix_d - body_d, 3)
    try:
        fb = float(fallback)
    except (TypeError, ValueError):
        fb = 0.0
    return fb if fb > 0 else 0.0


_SILENCE_START_RE = re.compile(r"silence_start:\s*([-0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")


def probe_stream_duration_sec(path: Path, stream: str = "a:0") -> float | None:
    """Duration of one stream (default audio). Overlay must follow the audio clock."""
    if not path.is_file():
        return None
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return None
    probe = _ffprobe_bin(ffmpeg)
    if probe:
        r = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-select_streams",
                stream,
                "-show_entries",
                "stream=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        dur = _parse_duration_text(r.stdout)
        if dur:
            return dur
        r = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        dur = _parse_duration_text(r.stdout)
        if dur:
            return dur
    # No ffprobe / N/A stream duration: fall back to container Duration from ffmpeg -i.
    return probe_duration_sec(path)


def has_audio_stream(path: Path) -> bool:
    """True when ffmpeg reports an audio stream (works without ffprobe)."""
    if not path.is_file():
        return False
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return False
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except Exception:
        return False
    blob = f"{r.stderr or ''}\n{r.stdout or ''}"
    return bool(re.search(r"Stream #.*Audio:", blob))


def clamp_overlay_cues(
    cues: list[dict],
    duration: float | None,
    *,
    tol: float = 0.12,
) -> list[dict]:
    """Drop/clamp cues that run past the playing file so mid/tail cannot hang off-end."""
    try:
        limit = float(duration) if duration is not None else 0.0
    except (TypeError, ValueError):
        limit = 0.0
    if limit <= 0.05:
        return cues
    out: list[dict] = []
    for row in cues:
        try:
            start = float(row["start"])
            end = float(row["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start >= limit - 0.01:
            continue
        end = min(end, limit + tol)
        if end <= start:
            continue
        item = dict(row)
        item["start"] = round(start, 3)
        item["end"] = round(end, 3)
        out.append(item)
    return out


def parse_leading_silence_sec(log: str) -> float | None:
    """First silence_end when the file starts silent (intro card). None otherwise."""
    starts = [float(x) for x in _SILENCE_START_RE.findall(log or "")]
    ends = [float(x) for x in _SILENCE_END_RE.findall(log or "")]
    if not starts or not ends:
        return None
    if starts[0] > 0.08:
        return None
    end = ends[0]
    return end if end > 0.04 else None


def probe_leading_silence_sec(path: Path) -> float | None:
    """When speech/audio begins after a silent intro, on the playing file's audio clock."""
    if not path.is_file():
        return None
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return None
    r = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "silencedetect=noise=-40dB:d=0.08",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return parse_leading_silence_sec(f"{r.stderr or ''}\n{r.stdout or ''}")


def overlay_intro_sec(
    remix: Path,
    body: Path,
    fallback: float,
    *,
    requested: float | None = None,
) -> float:
    """Intro length for overlay cues: leading silence of remix, else audio remix−body.

    Captions must start when the viewer hears the body, not when a sidecar file
    reports a container duration.
    """
    try:
        expect = float(requested if requested and requested > 0 else fallback or 0)
    except (TypeError, ValueError):
        expect = 0.0
    silence = probe_leading_silence_sec(remix)
    if silence is not None:
        if expect > 0 and abs(silence - expect) <= 0.55:
            return round(silence, 3)
        remix_d = probe_duration_sec(remix) or 0.0
        if expect <= 0 and 0.04 < silence < max(0.2, remix_d * 0.45):
            return round(silence, 3)
    ra = probe_stream_duration_sec(remix, "a:0")
    ba = probe_stream_duration_sec(body, "a:0")
    if ra and ba and ra > ba + 0.04:
        audio_diff = round(ra - ba, 3)
        if expect <= 0 or abs(audio_diff - expect) <= 0.8:
            return audio_diff
    return intro_duration_in_remix(remix, body, fallback)


def clip_span_durations(work_dir: Path, spans: list[tuple[float, float]]) -> list[float]:
    path = clips_meta_path(work_dir)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            data = {}
        rows = data.get("spans") if isinstance(data, dict) else None
        if isinstance(rows, list) and len(rows) == len(spans):
            stored: list[float] = []
            for row in rows:
                if not isinstance(row, dict):
                    stored = []
                    break
                try:
                    dur = float(row.get("duration"))
                except (TypeError, ValueError):
                    stored = []
                    break
                if dur <= 0.05:
                    stored = []
                    break
                stored.append(dur)
            if len(stored) == len(spans):
                return stored
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
        "audio_clock": True,
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
    timeline_dur: float | None = None,
    clock: str | None = None,
) -> list[tuple[float, float, str, str]]:
    from pipeline import parse_srt
    from segment_subs import remap_cues_to_spans

    segs = parse_srt(srt)
    clock_name = clock or caption_clock_for_body(work_dir, body)
    if clock_name == "clips":
        spans = load_clip_spans(work_dir)
        if spans:
            durs = scale_span_durations(clip_span_durations(work_dir, spans), timeline_dur)
            segs = remap_cues_to_spans(segs, spans, durations=durs)
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


def ensure_video_has_audio(
    work_dir: Path,
    video: Path,
    *,
    clock: str | None = None,
) -> Path:
    """Mux job WAV onto a video-only file so intro concat can bind [0:a]/[1:a]."""
    video = Path(video)
    if has_audio_stream(video):
        return video
    from job_layout import existing_wav

    wav = existing_wav(work_dir)
    if wav is None or not wav.is_file():
        return video
    clock_name = (clock or caption_clock_for_body(work_dir, video) or "source").strip().lower()
    if clock_name not in {"clips", "source"}:
        clock_name = "source"
    dest = job_remix_dir(work_dir) / "_src_av.mp4"
    ffmpeg = find_ffmpeg()
    # Prefer video length so a longer wav cannot stretch a short clip body.
    vid_dur = probe_duration_sec(video)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-i",
        str(wav),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        f"{DEFAULT_AUDIO_K}k",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-shortest",
        "-movflags",
        "+faststart",
    ]
    if vid_dur and vid_dur > 0.05:
        cmd.extend(["-t", f"{vid_dur:.3f}"])
    cmd.append(str(dest))
    _run_ffmpeg(cmd)
    _write_meta(
        dest.with_name("_src_av_meta.json"),
        {"clock": clock_name, "src": video.name, "bytes": dest.stat().st_size if dest.is_file() else 0},
    )
    return dest


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
        cmd.extend(["-t", f"{float(duration):.3f}", "-shortest"])
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
    # Clock must follow the body we resolved (e.g. _clips.mp4), not a remuxed
    # sidecar name like _src_av.mp4 — otherwise full-timeline SRT is hung on a short cut.
    clock = caption_clock_for_body(work_dir, src)
    src = ensure_video_has_audio(work_dir, src, clock=clock)

    hook = load_note_hook(work_dir, prefer=opts.get("remix_lang") or None)
    title = opts.get("remix_title") or hook.get("one_liner") or hook.get("title") or src.stem
    intro_sec = float(opts.get("remix_intro_sec") or 0)
    if not str(title).strip():
        intro_sec = 0.0

    ffmpeg = find_ffmpeg()
    intro_path = remix_dir / "_intro.mp4"
    body_path = remix_dir / "_body.mp4"
    crop_hardsubs = bool(opts.get("remix_crop_hardsubs", True))
    srt = find_remix_srt(work_dir, hook.get("lang") or opts.get("remix_lang"))

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
        intro_actual = 0.0
    else:
        concat_videos(pieces, dest, height=REMIX_H)
        intro_actual = overlay_intro_sec(
            dest, body_path, intro_actual, requested=intro_sec
        )

    remix_audio = probe_stream_duration_sec(dest, "a:0") or probe_duration_sec(dest)
    if remix_audio and remix_audio > intro_actual:
        body_dur = remix_audio - intro_actual
    else:
        body_dur = probe_duration_sec(body_path)
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
            work_dir,
            srt,
            body=src,
            shift=intro_actual,
            timeline_dur=body_dur,
            clock=clock,
        ):
            overlay_cues.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "kind": "caption",
                    "text": text,
                }
            )
    remix_limit = probe_duration_sec(dest) or remix_audio
    overlay_cues = clamp_overlay_cues(overlay_cues, remix_limit)
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
    clean = job_media_dir(work_dir) / "dehardsub" / "clean.mp4"
    if clean.is_file() and clean.stat().st_size > 800:
        src = clean
    src = ensure_video_has_audio(work_dir, src)
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
    """Prefer last postproc product, else dehardsub clean, else source."""
    from note_frames import existing_source_video

    for rel in (
        Path("remix") / "remix.mp4",
        Path("compress") / "compressed.mp4",
        Path("enhance") / "enhanced.mp4",
        Path("concat") / "concat.mp4",
        Path("dehardsub") / "clean.mp4",
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
    """Run dehardsub → concat → enhance → compress → remix. Missing steps are skipped."""
    from note_frames import existing_source_video

    opts = normalize_media_opts(media_opts)
    produced: dict[str, Path] = {}
    source = existing_source_video(work_dir)
    working = source

    if "dehardsub" in enabled:
        if working is None:
            raise FileNotFoundError(f"dehardsub needs a source video in {work_dir}")
        cleaned, report = strip_hardsubs(
            work_dir,
            video=working,
            force=bool(opts.get("dehardsub_force")),
            ratio=float(opts.get("dehardsub_ratio") or HARDSUB_CROP_RATIO),
            mode=str(opts.get("dehardsub_mode") or "auto"),
            vlm_model=str(opts.get("dehardsub_vlm_model") or HARDSUB_VLM_MODEL),
            passes=int(opts.get("dehardsub_passes") or DEFAULT_CLEAN_PASSES),
            demosaic=bool(opts.get("dehardsub_demosaic", True)),
            engine=str(opts.get("dehardsub_engine") or DEFAULT_DEHARDSUB_ENGINE),
        )
        if report.get("action") not in (None, "skip") and cleaned.is_file():
            produced["dehardsub"] = cleaned
            working = cleaned

    if "concat" in enabled:
        clips = list_clip_mp4s(work_dir)
        if len(clips) < MIN_CONCAT and clip_ranges:
            clips = cut_range_clips(work_dir, clip_ranges, video=working or source)
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
        remix_opts = dict(opts)
        # Avoid double-cropping when dehardsub already removed the bottom band.
        clean = job_media_dir(work_dir) / "dehardsub" / "clean.mp4"
        if clean.is_file() and body.resolve() == clean.resolve():
            remix_opts["remix_crop_hardsubs"] = False
        elif clean.is_file() and "dehardsub" in enabled:
            # Body may be compress/enhance of clean — still skip hardsub crop.
            meta = clean.with_name("dehardsub_meta.json")
            if meta.is_file():
                try:
                    data = json.loads(meta.read_text(encoding="utf-8"))
                    if data.get("action") not in (None, "skip"):
                        remix_opts["remix_crop_hardsubs"] = False
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    pass
        remix_vertical_notes(work_dir, dest, body=body, media_opts=remix_opts)
        produced["remix"] = dest

    return produced


def copy_derived_to_site(work_dir: Path, slug: str, site_public: Path) -> int:
    media = job_media_dir(work_dir)
    dest = Path(site_public) / "derived" / slug
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    mapping = {
        "clean.mp4": media / "dehardsub" / "clean.mp4",
        "dehardsub_meta.json": media / "dehardsub" / "dehardsub_meta.json",
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
