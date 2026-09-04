"""WF-03b general visual cleanup: hardsubs + mosaic, multi-pass validate/repair.

Designed to be content-agnostic (not tied to one dark-UI layout):
  1) locate work regions (caption box + mosaic blobs)
  2) per-frame build masks (glyph / block-grid)
  3) repair (OpenCV inpaint + temporal blend)
  4) sample-validate residual → strengthen mask → repeat until clean or max passes
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_CLEAN_PASSES = max(1, int(os.environ.get("VITUAL_CLEAN_PASSES", "2") or 2))
DEFAULT_RESIDUAL_OK = float(os.environ.get("VITUAL_CLEAN_RESIDUAL_OK", "0.012") or 0.012)
MOSAIC_BLOCK_SIZES = (8, 12, 16, 24, 32)


def _media():
    """Lazy import to avoid circular dependency with media_ops."""
    import media_ops as m

    return m


def _as_bool(raw: Any, default: bool = True) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def hardsub_residual_score(gray: Any, mask: Any | None = None) -> float:
    """0..1 residual text-likeness: deviation from local median (ghosts + leftover glyphs)."""
    import cv2
    import numpy as np

    med = cv2.medianBlur(gray, 15)
    diff = np.abs(gray.astype(np.float32) - med.astype(np.float32))
    hit = diff > 14.0
    if mask is not None:
        hit &= mask > 0
    if not np.any(hit):
        return 0.0
    # Prefer stroke-like remnants over noise.
    u8 = hit.astype(np.uint8) * 255
    u8 = cv2.morphologyEx(u8, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return float((u8 > 0).mean())


def mosaic_block_score(gray: Any, block: int = 16) -> float:
    """Higher when an N×N mosaic grid dominates (low intra-block var, high block-edge)."""
    import cv2
    import numpy as np

    h, w = gray.shape[:2]
    if h < block * 2 or w < block * 2:
        return 0.0
    hh = (h // block) * block
    ww = (w // block) * block
    g = gray[:hh, :ww].astype(np.float32)
    tiles = g.reshape(hh // block, block, ww // block, block)
    # mean variance inside each block
    intra = float(tiles.var(axis=(1, 3)).mean())
    # variance of block means (should still exist if mosaic of different colors)
    means = tiles.mean(axis=(1, 3))
    inter = float(means.var())
    # edge energy on block boundaries
    gx = np.abs(np.diff(g, axis=1))
    gy = np.abs(np.diff(g, axis=0))
    bx = gx[:, block - 1 :: block]
    by = gy[block - 1 :: block, :]
    edge = float((bx.mean() + by.mean()) / 2.0)
    # Mosaic: low intra, non-trivial inter, strong grid edges.
    if inter < 8.0:
        return 0.0
    score = (edge / (intra + 1.0)) * min(1.0, inter / 40.0)
    return float(max(0.0, min(1.0, score / 8.0)))


def detect_mosaic_boxes(
    frame: Any,
    *,
    min_score: float = 0.22,
    min_side: int = 48,
) -> list[dict[str, int]]:
    """Find rectangular mosaic blobs via multi-scale block-grid scoring."""
    import cv2
    import numpy as np

    full_h, full_w = frame.shape[:2]
    # Work on a downscaled copy for speed; map boxes back.
    scale = 1.0
    work = frame
    if max(full_h, full_w) > 640:
        scale = 640.0 / max(full_h, full_w)
        work = cv2.resize(
            frame,
            (max(32, int(full_w * scale)), max(32, int(full_h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    heat = np.zeros((h, w), dtype=np.float32)
    for bs in MOSAIC_BLOCK_SIZES:
        step = max(bs, bs * 2)
        win = bs * 2
        for y0 in range(0, max(1, h - win), step):
            for x0 in range(0, max(1, w - win), step):
                patch = gray[y0 : y0 + win, x0 : x0 + win]
                if patch.shape[0] < bs * 2 or patch.shape[1] < bs * 2:
                    continue
                s = mosaic_block_score(patch, block=bs)
                if s >= min_score:
                    heat[y0 : y0 + win, x0 : x0 + win] += s
    if float(heat.max()) < min_score:
        return []
    mask = (heat >= min_score).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    boxes: list[dict[str, int]] = []
    mops = _media()
    inv = 1.0 / scale
    min_side_s = max(16, int(min_side * scale))
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if bw < min_side_s or bh < min_side_s or area < min_side_s * min_side_s:
            continue
        boxes.append(
            mops._clamp_delogo_box(
                {
                    "x": int(x * inv),
                    "y": int(y * inv),
                    "w": int(bw * inv),
                    "h": int(bh * inv),
                    "width": full_w,
                    "height": full_h,
                },
                full_w,
                full_h,
            )
        )
    # Prefer larger boxes first; cap count for speed.
    boxes.sort(key=lambda b: int(b["w"]) * int(b["h"]), reverse=True)
    return boxes[:6]


def build_hardsub_mask(roi: Any, *, dilate: int = 2) -> Any:
    """Tight white-glyph mask (first working path). ``dilate`` unused — keep strokes small."""
    import cv2

    _ = dilate
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return _media()._hardsub_glyph_mask(gray, bgr=roi)


def build_mosaic_mask(roi: Any, *, block_hint: int = 16, dilate: int = 1) -> Any:
    """Mask pixels that belong to a mosaic grid inside ROI."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    best = np.zeros((h, w), dtype=np.uint8)
    best_s = 0.0
    for bs in MOSAIC_BLOCK_SIZES:
        s = mosaic_block_score(gray, block=bs)
        if s > best_s:
            best_s = s
            block_hint = bs
    if best_s < 0.18:
        return best
    # Mark blocks with very low internal variance relative to neighbors.
    bs = block_hint
    for y0 in range(0, h - bs + 1, bs):
        for x0 in range(0, w - bs + 1, bs):
            tile = gray[y0 : y0 + bs, x0 : x0 + bs]
            if float(tile.var()) <= 18.0:
                best[y0 : y0 + bs, x0 : x0 + bs] = 255
    if int(best.max()) == 0:
        return best
    best = cv2.dilate(best, np.ones((3, 3), np.uint8), iterations=max(1, dilate))
    return best


def restore_region(
    roi: Any,
    mask_u8: Any,
    bg: Any | None,
    *,
    radius: int = 7,
) -> tuple[Any, Any]:
    """Glyph restore: same as the first working path (median on flat UI, else inpaint)."""
    _ = radius
    return _media()._restore_hardsub_roi(roi, mask_u8, bg)


def _merge_masks(*masks: Any) -> Any:
    import numpy as np

    out = None
    for m in masks:
        if m is None:
            continue
        if out is None:
            out = m.copy()
        else:
            out = np.maximum(out, m)
    if out is None:
        raise ValueError("no masks")
    return out


def validate_cleanup_sample(
    frame: Any,
    regions: list[dict[str, Any]],
    *,
    ref_frame: Any | None = None,
) -> dict[str, float]:
    """Score residuals inside known regions on one frame."""
    import cv2
    import numpy as np

    hard = 0.0
    mosa = 0.0
    n_h = 0
    n_m = 0
    for reg in regions:
        kind = str(reg.get("kind") or "hardsub")
        x, y, bw, bh = int(reg["x"]), int(reg["y"]), int(reg["w"]), int(reg["h"])
        roi = frame[y : y + bh, x : x + bw]
        if roi.size == 0:
            continue
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        if kind == "mosaic":
            mosa += mosaic_block_score(gray)
            n_m += 1
        else:
            if ref_frame is not None:
                ref = ref_frame[y : y + bh, x : x + bw]
                mask = build_hardsub_mask(ref, dilate=2)
            else:
                mask = build_hardsub_mask(roi, dilate=1)
            if int(mask.max()) > 0:
                med = cv2.medianBlur(gray, 21)
                bright_resid = float(
                    np.mean(
                        np.abs(gray.astype(np.float32) - med.astype(np.float32))[mask > 0]
                        > 10
                    )
                )
                # Dark glyph ghosts are flat vs medianBlur but still have stroke edges.
                edge_resid = float((cv2.Canny(gray, 30, 90) > 0)[mask > 0].mean())
                hard += max(bright_resid, edge_resid)
            else:
                hard += hardsub_residual_score(gray, mask)
            n_h += 1
    return {
        "hardsub": round(hard / max(1, n_h), 5),
        "mosaic": round(mosa / max(1, n_m), 5),
        "combined": round(
            (hard / max(1, n_h)) * 0.7 + (mosa / max(1, n_m)) * 0.3,
            5,
        ),
    }


def sample_validate_video(
    video: Path,
    regions: list[dict[str, Any]],
    *,
    samples: int = 7,
    ref_video: Path | None = None,
) -> dict[str, Any]:
    """Probe several timestamps; return mean residual + per-sample rows."""
    import cv2

    video = Path(video)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return {"ok": False, "combined": 1.0, "reason": "open_failed", "samples": []}
    ref_cap = None
    if ref_video is not None and Path(ref_video).is_file():
        ref_cap = cv2.VideoCapture(str(ref_video))
        if not ref_cap.isOpened():
            ref_cap.release()
            ref_cap = None
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    mops = _media()
    dur = (n / fps) if n > 0 else float(mops.probe_duration_sec(video) or 0.0)
    if dur <= 0.2:
        cap.release()
        if ref_cap is not None:
            ref_cap.release()
        return {"ok": False, "combined": 1.0, "reason": "short", "samples": []}
    fracs = [0.08 + 0.84 * i / max(1, samples - 1) for i in range(samples)]
    rows: list[dict[str, Any]] = []
    for frac in fracs:
        t = dur * frac
        idx = max(0, int(t * fps))
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        ref_frame = None
        if ref_cap is not None:
            ref_cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok_r, ref_frame = ref_cap.read()
            if not ok_r:
                ref_frame = None
        sc = validate_cleanup_sample(frame, regions, ref_frame=ref_frame)
        sc["t"] = round(t, 2)
        rows.append(sc)
    cap.release()
    if ref_cap is not None:
        ref_cap.release()
    if not rows:
        return {"ok": False, "combined": 1.0, "reason": "no_frames", "samples": []}
    comb = sum(float(r["combined"]) for r in rows) / len(rows)
    hard = sum(float(r["hardsub"]) for r in rows) / len(rows)
    mosa = sum(float(r["mosaic"]) for r in rows) / len(rows)
    return {
        "ok": True,
        "combined": round(comb, 5),
        "hardsub": round(hard, 5),
        "mosaic": round(mosa, 5),
        "samples": rows,
    }


def _collect_mosaic_regions(video: Path, *, samples: int = 5) -> list[dict[str, Any]]:
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    mops = _media()
    dur = (n / fps) if n > 0 else float(mops.probe_duration_sec(video) or 0.0)
    per_frame: list[list[dict[str, int]]] = []
    for i in range(samples):
        t = dur * (0.15 + 0.7 * i / max(1, samples - 1)) if dur > 0 else 0.0
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(t * fps)))
        ok, frame = cap.read()
        if not ok:
            continue
        # Stricter threshold to cut anime/UI false positives.
        per_frame.append(detect_mosaic_boxes(frame, min_score=0.38, min_side=64))
    cap.release()
    flat = [b for group in per_frame for b in group]
    if not flat:
        return []
    wh = mops.probe_video_wh(video)
    if not wh:
        return []

    def _iou(a: dict[str, int], b: dict[str, int]) -> float:
        x0 = max(a["x"], b["x"])
        y0 = max(a["y"], b["y"])
        x1 = min(a["x"] + a["w"], b["x"] + b["w"])
        y1 = min(a["y"] + a["h"], b["y"] + b["h"])
        if x1 <= x0 or y1 <= y0:
            return 0.0
        inter = (x1 - x0) * (y1 - y0)
        union = a["w"] * a["h"] + b["w"] * b["h"] - inter
        return inter / max(1, union)

    # Keep boxes confirmed in ≥2 sampled frames (stable mosaic, not one-frame noise).
    confirmed: list[dict[str, int]] = []
    for b in sorted(flat, key=lambda r: r["w"] * r["h"], reverse=True):
        hits = sum(1 for group in per_frame if any(_iou(b, o) >= 0.35 for o in group))
        if hits < 2:
            continue
        if any(_iou(b, c) >= 0.5 for c in confirmed):
            continue
        confirmed.append(dict(b))
        if len(confirmed) >= 4:
            break
    return [{"kind": "mosaic", **b} for b in confirmed]


def encode_cleanup_pass(
    src: Path,
    dest: Path,
    regions: list[dict[str, Any]],
    *,
    dilate: int = 2,
    inpaint_radius: int = 7,
    do_hardsub: bool = True,
    do_mosaic: bool = True,
    mask_from: Path | None = None,
) -> dict[str, Any]:
    """One full-video repair pass over ``regions``.

    ``mask_from``: optional original video used only to build masks (so later passes
    still target original glyph/mosaic pixels even after white text is gone).
    """
    import cv2
    import numpy as np

    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    mops = _media()
    from fetch_media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video for cleanup: {src}")
    mask_cap = None
    mask_path = Path(mask_from) if mask_from else None
    if mask_path is not None and mask_path.resolve() != src.resolve() and mask_path.is_file():
        mask_cap = cv2.VideoCapture(str(mask_path))
        if not mask_cap.isOpened():
            mask_cap.release()
            mask_cap = None
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if w < 2 or h < 2:
        cap.release()
        if mask_cap is not None:
            mask_cap.release()
        raise RuntimeError(f"bad video size: {src}")

    # Audio always from the working ``src`` timeline being repaired.
    audio_src = src
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
        str(audio_src),
        "-map",
        "0:v:0",
    ]
    if mops.has_audio_stream(audio_src):
        cmd.extend(
            ["-map", "1:a:0?", "-c:a", "aac", "-b:a", f"{mops.DEFAULT_AUDIO_K}k"]
        )
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
    bg_map: dict[int, Any] = {}
    frames = 0
    masked = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            mask_frame = None
            if mask_cap is not None:
                ok_m, mask_frame = mask_cap.read()
                if not ok_m:
                    mask_frame = None
            for i, reg in enumerate(regions):
                kind = str(reg.get("kind") or "hardsub")
                if kind == "hardsub" and not do_hardsub:
                    continue
                if kind == "mosaic" and not do_mosaic:
                    continue
                x, y, bw, bh = int(reg["x"]), int(reg["y"]), int(reg["w"]), int(reg["h"])
                x = max(0, min(w - 2, x))
                y = max(0, min(h - 2, y))
                bw = max(2, min(w - x, bw))
                bh = max(2, min(h - y, bh))
                roi = frame[y : y + bh, x : x + bw]
                ref = (
                    mask_frame[y : y + bh, x : x + bw]
                    if mask_frame is not None
                    else roi
                )
                if kind == "mosaic":
                    mask = build_mosaic_mask(ref, dilate=max(1, min(2, dilate)))
                else:
                    # Always take glyphs from the original frame when available.
                    mask = build_hardsub_mask(ref)
                if int(mask.max()) == 0:
                    continue
                restored, bg_map[i] = restore_region(
                    roi, mask, bg_map.get(i), radius=inpaint_radius
                )
                frame[y : y + bh, x : x + bw] = restored
                masked += 1
            proc.stdin.write(np.ascontiguousarray(frame).tobytes())
            frames += 1
    except Exception:
        proc.kill()
        raise
    finally:
        cap.release()
        if mask_cap is not None:
            mask_cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    stderr = proc.communicate(timeout=300)[1]
    if proc.returncode != 0 or frames < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"cleanup encode failed ({proc.returncode}): {err}")
    if not dest.is_file() or dest.stat().st_size < 800:
        raise RuntimeError(f"cleanup produced empty file: {dest}")
    return {"frames": frames, "masked_ops": masked, "bytes": dest.stat().st_size}


def run_multipass_cleanup(
    src: Path,
    dest: Path,
    *,
    work_dir: Path | None = None,
    locate_mode: str = "vlm",
    vlm_model: str | None = None,
    ratio: float = 0.14,
    max_passes: int = DEFAULT_CLEAN_PASSES,
    residual_ok: float = DEFAULT_RESIDUAL_OK,
    demosaic: bool = True,
    dehardsub: bool = True,
    engine: str = "sttn",
    demosaic_engine: str = "opencv",
) -> dict[str, Any]:
    """Locate caption box → STTN (default) or OpenCV fill; optional mosaic pass."""
    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    mops = _media()
    wh = mops.probe_video_wh(src)
    if wh is None:
        raise RuntimeError(f"cannot probe video size: {src}")
    width, height = wh
    model = (vlm_model or mops.HARDSUB_VLM_MODEL).strip() or mops.HARDSUB_VLM_MODEL
    regions: list[dict[str, Any]] = []

    engine_s = (engine or "sttn").strip().lower()
    locate_meta: dict[str, Any] = {}
    box: dict[str, Any] | None = None
    if dehardsub:
        located = mops.resolve_hardsub_box(
            src,
            width,
            height,
            mode=("vlm" if locate_mode == "auto" else locate_mode),
            ratio=ratio,
            vlm_model=model,
            work_dir=work_dir,
        )
        # STTN hole = located caption box (no glyph / strip mask).
        if engine_s == "sttn":
            box = located["box"]
        else:
            box = mops.refine_hardsub_box_glyphs(src, width, height, located["box"])
        regions.append({"kind": "hardsub", **box})
        locate_meta = {
            "box": box,
            "box_seed": located.get("box"),
            "box_source": located.get("source"),
            "locate": {
                k: located.get(k) for k in ("norm", "hits", "source", "model", "samples")
            },
            "engine": engine_s,
        }

    mosaic_regions: list[dict[str, Any]] = []
    if demosaic:
        mosaic_regions = _collect_mosaic_regions(src)
        regions.extend(mosaic_regions)

    if not regions:
        # Nothing to do — copy.
        import shutil

        shutil.copy2(src, dest)
        return {
            "action": "copy_no_regions",
            "passes": [],
            "regions": [],
            "demosaic": demosaic,
            "dehardsub": dehardsub,
            **locate_meta,
        }

    if engine_s == "sttn" and dehardsub and box is not None:
        from sttn_inpaint import encode_sttn

        # demosaic_engine=="sttn" 时，把马赛克块也作为 STTN 的洞一起修复，
        # 不再走 OpenCV inpaint；否则马赛克仍走原 OpenCV 分支。
        use_sttn_mosaic = str(demosaic_engine).lower() == "sttn" and bool(mosaic_regions)
        mosaic_boxes = (
            [{k: int(m[k]) for k in ("x", "y", "w", "h")} for m in mosaic_regions]
            if use_sttn_mosaic
            else None
        )
        sttn_dest = dest if not (mosaic_regions and not use_sttn_mosaic) else dest.with_name("_sttn_tmp.mp4")
        sttn_stats = encode_sttn(
            src,
            sttn_dest,
            {k: int(box[k]) for k in ("x", "y", "w", "h")},
            mosaic_boxes=mosaic_boxes,
        )
        if mosaic_regions and not use_sttn_mosaic:
            encode_cleanup_pass(
                sttn_dest,
                dest,
                mosaic_regions,
                dilate=1,
                inpaint_radius=7,
                do_hardsub=False,
                do_mosaic=True,
            )
            try:
                sttn_dest.unlink(missing_ok=True)
            except OSError:
                pass
        elif sttn_dest.resolve() != dest.resolve():
            sttn_dest.replace(dest)
        print(
            f"[cleanup] sttn frames={sttn_stats.get('frames')} "
            f"device={sttn_stats.get('device')}"
        )
        return {
            "action": "sttn_cleanup",
            "engine": "sttn",
            "passes": [{"pass": 1, "encode": sttn_stats}],
            "pass_count": 1,
            "regions": regions,
            "mosaic_regions": mosaic_regions,
            "demosaic": demosaic,
            "dehardsub": dehardsub,
            "src_size": f"{width}x{height}",
            **locate_meta,
            "encode": sttn_stats,
        }

    max_passes = max(1, min(6, int(max_passes)))
    residual_ok = max(0.002, float(residual_ok))
    current = src
    pass_rows: list[dict[str, Any]] = []
    tmp_paths: list[Path] = []

    for p in range(1, max_passes + 1):
        out_p = dest if p == max_passes else dest.with_name(f"_clean_pass{p}.mp4")
        if out_p.resolve() == current.resolve():
            out_p = dest.with_name(f"_clean_pass{p}_next.mp4")
        # Pass 1 = tight glyph fill. Later passes reuse the *same* source glyph
        # mask (no growing dilate) to kill leftover ghosts only.
        stats = encode_cleanup_pass(
            current,
            out_p,
            regions,
            dilate=1,
            inpaint_radius=7,
            do_hardsub=dehardsub,
            do_mosaic=demosaic and bool(mosaic_regions),
            mask_from=src,
        )
        if out_p != dest:
            tmp_paths.append(out_p)
        current = out_p
        val = sample_validate_video(current, regions, samples=7, ref_video=src)
        row = {
            "pass": p,
            "dilate": 1,
            "inpaint_radius": 7,
            "encode": stats,
            "validate": {
                k: val.get(k) for k in ("combined", "hardsub", "mosaic", "ok")
            },
        }
        pass_rows.append(row)
        print(
            f"[cleanup] pass {p}/{max_passes} "
            f"residual={val.get('combined')} "
            f"(hardsub={val.get('hardsub')} mosaic={val.get('mosaic')})"
        )
        if float(val.get("combined") or 1.0) <= residual_ok:
            break

    if current.resolve() != dest.resolve():
        current.replace(dest)

    for p in tmp_paths:
        try:
            if p.resolve() != dest.resolve() and p.is_file():
                p.unlink(missing_ok=True)
        except OSError:
            pass

    final_val = sample_validate_video(dest, regions, samples=7, ref_video=src)
    return {
        "action": "multipass_cleanup",
        "passes": pass_rows,
        "pass_count": len(pass_rows),
        "regions": regions,
        "mosaic_regions": mosaic_regions,
        "demosaic": demosaic,
        "dehardsub": dehardsub,
        "residual_ok": residual_ok,
        "final_validate": {
            k: final_val.get(k) for k in ("combined", "hardsub", "mosaic", "ok")
        },
        "src_size": f"{width}x{height}",
        **locate_meta,
    }
