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


def _best_block_score(
    gray: Any,
    block: int,
    *,
    pstep: int | None = None,
) -> tuple[float, tuple[int, int]]:
    """相位不敏感版 mosaic_block_score：返回 (最高分, 最佳相位 (dy,dx))。

    mosaic_block_score 假设马赛克网格从 (0,0) 起算；但 ROI / 滑窗起点通常与真实
    马赛克网格错开若干像素，此时 score 会从 1.0 塌到 0.0（实测错位 4px 即从
    1.0 掉到 0.0）。这里在 block 范围内采样相位偏移，取最高分及其相位。
    """
    best_s = 0.0
    best_ph = (0, 0)
    p = max(1, pstep or max(1, block // 4))
    for dy in range(0, block, p):
        for dx in range(0, block, p):
            sub = gray[dy:, dx:]
            if sub.shape[0] < block * 2 or sub.shape[1] < block * 2:
                continue
            s = mosaic_block_score(sub, block=block)
            if s > best_s:
                best_s = s
                best_ph = (dy, dx)
    return best_s, best_ph


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
        win = bs * 2
        # mosaic_block_score 对「网格相位」极敏感：马赛克块起点通常不是 win 的整数倍，
        # 从 (0,0) 固定步长滑窗会因相位错开而全部漏检（实测 32x32 窗错位 4px 时
        # score 从 1.0 掉到 0.0）。故窗口内采样相位偏移取最高分。
        # 注意保持 step==win：相邻窗口连续命中才能拼成足够大的连通域，
        # 否则单个 win×win 会被 min_side 过滤掉（实测 step=win+1 反而全部漏检）。
        step = max(bs, bs * 2)
        pstep = max(1, bs // 4)
        for y0 in range(0, max(1, h - win), step):
            for x0 in range(0, max(1, w - win), step):
                # 近乎纯色的窗口不可能是马赛克网格 → 跳过昂贵的相位搜索（大加速）
                base = gray[y0 : y0 + win, x0 : x0 + win]
                if base.shape[0] < bs * 2 or base.shape[1] < bs * 2:
                    continue
                if float(base.var()) < 4.0:
                    continue
                best_s = 0.0
                for dy in range(0, bs, pstep):
                    for dx in range(0, bs, pstep):
                        yy = y0 + dy
                        xx = x0 + dx
                        if yy + win > h or xx + win > w:
                            continue
                        patch = gray[yy : yy + win, xx : xx + win]
                        if patch.shape[0] < bs * 2 or patch.shape[1] < bs * 2:
                            continue
                        s = mosaic_block_score(patch, block=bs)
                        if s > best_s:
                            best_s = s
                            if best_s >= 0.9:  # 早停，省算力
                                break
                    if best_s >= 0.9:
                        break
                if best_s >= min_score:
                    heat[y0 : y0 + win, x0 : x0 + win] += best_s
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
    best_ph = (0, 0)
    # 相位不敏感：ROI 起点通常与马赛克网格错开，需先找最佳相位，
    # 否则评分归零 → mask 全空 → 马赛克区被整块跳过（表现为「没去马赛克」）。
    for bs in MOSAIC_BLOCK_SIZES:
        s, ph = _best_block_score(gray, bs)
        if s > best_s:
            best_s = s
            block_hint = bs
            best_ph = ph
    if best_s < 0.18:
        return best
    # Mark blocks with very low internal variance relative to neighbors.
    bs = block_hint
    dy0, dx0 = best_ph
    for y0 in range(dy0, h - bs + 1, bs):
        for x0 in range(dx0, w - bs + 1, bs):
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


def restore_edge_overlay(frame: Any, box: dict[str, int]) -> Any:
    """Edge overlay fallback for top/bottom/right static overlays.

    Copy a same-size patch from a nearby clean band and feather only on
    non-edge sides. This is crude, but it is often more stable than glyph-wise
    inpainting for large outlined titles / watermarks on blurry backgrounds.
    """
    import cv2
    import numpy as np

    out = frame.copy()
    h, w = out.shape[:2]
    x0 = max(0, min(w - 1, int(box["x"])))
    y0 = max(0, min(h - 1, int(box["y"])))
    x1 = max(x0 + 1, min(w, x0 + max(1, int(box["w"]))))
    y1 = max(y0 + 1, min(h, y0 + max(1, int(box["h"]))))
    bw = x1 - x0
    bh = y1 - y0
    if bw < 8 or bh < 8:
        return out

    touch_top = y0 <= 2
    touch_bottom = y1 >= h - 2
    touch_left = x0 <= 2
    touch_right = x1 >= w - 2

    # Prefer borrowing from the nearest clean interior band.
    if touch_top:
        sy0 = min(h - bh, y1 + 2)
        sy1 = min(h, sy0 + bh)
        sx0, sx1 = x0, x1
    elif touch_bottom and touch_right:
        sx1 = max(bw, x0 - 2)
        sx0 = max(0, sx1 - bw)
        sy1 = max(bh, y0 - 2)
        sy0 = max(0, sy1 - bh)
    elif touch_right:
        sx1 = max(bw, x0 - 2)
        sx0 = max(0, sx1 - bw)
        sy0, sy1 = y0, y1
    elif touch_bottom:
        sy1 = max(bh, y0 - 2)
        sy0 = max(0, sy1 - bh)
        sx0, sx1 = x0, x1
    else:
        sy1 = max(bh, y0 - 2)
        sy0 = max(0, sy1 - bh)
        sx0, sx1 = x0, x1
    sample = out[sy0:sy1, sx0:sx1]
    if sample.size == 0:
        return out
    if sample.shape[0] != bh or sample.shape[1] != bw:
        sample = cv2.resize(sample, (bw, bh), interpolation=cv2.INTER_LINEAR)

    yy, xx = np.mgrid[0:bh, 0:bw].astype(np.float32)
    dist_left = xx if not touch_left else np.full_like(xx, 9999.0)
    dist_top = yy if not touch_top else np.full_like(yy, 9999.0)
    dist_right = (bw - 1 - xx) if not touch_right else np.full_like(xx, 9999.0)
    dist_bottom = (bh - 1 - yy) if not touch_bottom else np.full_like(yy, 9999.0)
    edge = np.minimum.reduce([dist_left, dist_top, dist_right, dist_bottom])
    alpha = np.clip(edge / 10.0, 0.0, 1.0)[:, :, None]
    target = out[y0:y1, x0:x1].astype(np.float32)
    donor = sample.astype(np.float32)
    mixed = donor * alpha + target * (1.0 - alpha)
    out[y0:y1, x0:x1] = np.clip(mixed, 0, 255).astype(np.uint8)
    return out


def expand_static_overlay_box(
    box: dict[str, int],
    frame_w: int,
    frame_h: int,
    *,
    role: str,
) -> dict[str, int]:
    """Slightly enlarge static overlay boxes so edge text remnants are fully covered."""
    role_s = (role or "").strip().lower()
    if role_s == "title":
        pad_l, pad_t, pad_r, pad_b = 14, 0, 18, 14
    elif role_s == "watermark":
        pad_l, pad_t, pad_r, pad_b = 28, 16, 10, 14
    else:
        pad_l = pad_t = pad_r = pad_b = 0
    x0 = max(0, int(box["x"]) - pad_l)
    y0 = max(0, int(box["y"]) - pad_t)
    x1 = min(frame_w, int(box["x"]) + int(box["w"]) + pad_r)
    y1 = min(frame_h, int(box["y"]) + int(box["h"]) + pad_b)
    return {"x": x0, "y": y0, "w": max(2, x1 - x0), "h": max(2, y1 - y0)}


def restore_static_overlay_box(frame: Any, box: dict[str, int]) -> Any:
    """Generic static edge-overlay remover: full expanded box inpaint.

    This is more general than subtitle-shaped masks for top titles / corner
    watermarks because the overlay is static and edge-attached.
    """
    import cv2
    import numpy as np

    out = frame.copy()
    h, w = out.shape[:2]
    x0 = max(0, min(w - 1, int(box["x"])))
    y0 = max(0, min(h - 1, int(box["y"])))
    x1 = max(x0 + 1, min(w, x0 + max(1, int(box["w"]))))
    y1 = max(y0 + 1, min(h, y0 + max(1, int(box["h"]))))
    mask = np.zeros((h, w), dtype=np.uint8)
    mask[y0:y1, x0:x1] = 255
    filled = cv2.inpaint(out, mask, 5, cv2.INPAINT_TELEA)

    # Soften the rectangle boundary by blending a blurred version only near the
    # non-edge sides of the box; this reduces the "patched rectangle" look.
    touch_top = y0 <= 2
    touch_bottom = y1 >= h - 2
    touch_left = x0 <= 2
    touch_right = x1 >= w - 2
    roi = filled[y0:y1, x0:x1]
    blur = cv2.GaussianBlur(roi, (0, 0), 3.0)
    bh, bw = roi.shape[:2]
    yy, xx = np.mgrid[0:bh, 0:bw].astype(np.float32)
    dist_left = xx if not touch_left else np.full_like(xx, 9999.0)
    dist_top = yy if not touch_top else np.full_like(yy, 9999.0)
    dist_right = (bw - 1 - xx) if not touch_right else np.full_like(xx, 9999.0)
    dist_bottom = (bh - 1 - yy) if not touch_bottom else np.full_like(yy, 9999.0)
    edge = np.minimum.reduce([dist_left, dist_top, dist_right, dist_bottom])
    alpha = np.clip((10.0 - edge) / 10.0, 0.0, 1.0)[:, :, None]
    mixed = roi.astype(np.float32) * (1.0 - alpha) + blur.astype(np.float32) * alpha
    filled[y0:y1, x0:x1] = np.clip(mixed, 0, 255).astype(np.uint8)
    return filled


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
        boxes = detect_mosaic_boxes(frame, min_score=0.38, min_side=64)
        # 二次确认：候选区必须能生成非空马赛克 mask，否则是规则纹理误检。
        # （误检会导致对正常画面做 inpaint/模糊，破坏画质）
        kept: list[dict[str, int]] = []
        fh, fw = frame.shape[:2]
        for b in boxes:
            y0 = max(0, int(b["y"]))
            x0 = max(0, int(b["x"]))
            y1 = min(fh, y0 + int(b["h"]))
            x1 = min(fw, x0 + int(b["w"]))
            roi = frame[y0:y1, x0:x1]
            if roi.size and int(build_mosaic_mask(roi).max()) > 0:
                kept.append(b)
        per_frame.append(kept)
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
    mosaic_engine: str = "opencv",
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
                if kind == "mosaic":
                    eng = str(mosaic_engine).lower()
                    if eng == "lama":
                        try:
                            from inpaint_lama import LamaUnavailable, lama_inpaint

                            full = np.zeros(frame.shape[:2], dtype=np.uint8)
                            full[y : y + bh, x : x + bw] = mask
                            frame = lama_inpaint(frame, full)
                            masked += 1
                            continue
                        except Exception:
                            eng = "opencv"
                    from demosaic_codeformer import restore_mosaic_crop

                    restored, bg_map[i] = restore_mosaic_crop(
                        roi,
                        mask,
                        bg_map.get(i),
                        radius=inpaint_radius,
                        use_neural=(eng in ("codeformer", "lama", "auto")),
                        prefer=eng if eng in ("codeformer", "lama") else "auto",
                    )
                else:
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


def residual_ghost_mask(bgr: Any, box: dict[str, int], *, thr: float = 14.0) -> Any:
    """STTN/lerp 后残影 mask：框内相对局部中位数的笔画状偏差（OCR 已看不到字时仍可用）。"""
    import cv2
    import numpy as np

    h, w = bgr.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    x0 = max(0, min(w - 1, int(box["x"])))
    y0 = max(0, min(h - 1, int(box["y"])))
    x1 = max(x0 + 1, min(w, x0 + max(1, int(box["w"]))))
    y1 = max(y0 + 1, min(h, y0 + max(1, int(box["h"]))))
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    roi = gray[y0:y1, x0:x1]
    if roi.size < 64:
        return mask
    med = cv2.medianBlur(roi, 15)
    diff = np.abs(roi.astype(np.float32) - med.astype(np.float32))
    hit = (diff > float(thr)).astype(np.uint8) * 255
    hit = cv2.morphologyEx(hit, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    hit = cv2.morphologyEx(hit, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    n_cc, labels, stats, _ = cv2.connectedComponentsWithStats(hit, connectivity=8)
    clean = np.zeros_like(hit)
    for i in range(1, n_cc):
        area = int(stats[i, cv2.CC_STAT_AREA])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        if area < 8 or max(bw, bh) < 3:
            continue
        clean[labels == i] = 255
    if int(clean.max()) == 0:
        return mask
    frac = float((clean > 0).mean())
    if frac > 0.45:
        keep = diff > max(float(thr) + 8.0, float(np.percentile(diff, 85)))
        clean = (keep & (clean > 0)).astype(np.uint8) * 255
    mask[y0:y1, x0:x1] = clean
    return mask


def _lama_box_mask(bgr: Any, box: dict[str, int], mode: str) -> Any:
    """glyph = OCR 笔画；residual = 残影；auto = 有字用 glyph，否则 residual。"""
    from sttn_inpaint import glyph_mask_hybrid

    mode_s = (mode or "glyph").strip().lower()
    if mode_s == "residual":
        return residual_ghost_mask(bgr, box)
    glyph = glyph_mask_hybrid(bgr, box)
    if mode_s == "glyph":
        return glyph
    if int(glyph.max()) > 0:
        return glyph
    return residual_ghost_mask(bgr, box)


def encode_lama(
    src: Path,
    dest: Path,
    box: dict[str, Any],
    mosaic_regions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """LaMa 逐帧修复（VSR 双修复器之二）：静态字幕最优、无时序伪影。"""
    return encode_lama_boxes(src, dest, [box], mosaic_regions=mosaic_regions)


def encode_lama_boxes(
    src: Path,
    dest: Path,
    boxes: list[dict[str, Any]],
    mosaic_regions: list[dict[str, Any]] | None = None,
    *,
    dilate: int = 2,
    mask_mode: str = "glyph",
) -> dict[str, Any]:
    """LaMa 多区域字形填洞——纹理背景（衣服/木纹）比 STTN  smear 更自然。"""
    import cv2
    import numpy as np

    from inpaint_lama import lama_inpaint

    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    mops = _media()
    wh = mops.probe_video_wh(src)
    if wh is None:
        raise RuntimeError(f"cannot probe video: {src}")
    frame_w, frame_h = wh
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    n_hint = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    from fetch_media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    writing = dest.with_name(dest.stem + "_lama_writing.mp4")
    if writing.is_file():
        writing.unlink(missing_ok=True)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{frame_w}x{frame_h}",
        "-r", f"{fps:.4f}", "-i", "pipe:0", "-i", str(src), "-map", "0:v:0",
    ]
    if mops.has_audio_stream(src):
        cmd.extend(["-map", "1:a:0?", "-c:a", "aac", "-b:a", f"{mops.DEFAULT_AUDIO_K}k"])
    else:
        cmd.append("-an")
    cmd.extend([
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest", str(writing),
    ])
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    frames_done = 0
    lama_frames = 0
    kernel = np.ones((3, 3), np.uint8)
    boxes_n = [
        {k: int(b[k]) for k in ("x", "y", "w", "h")}
        for b in boxes
        if b and all(k in b for k in ("x", "y", "w", "h"))
    ]
    mode_s = (mask_mode or "glyph").strip().lower()
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            out = frame
            for mb in mosaic_regions or []:
                x = max(0, min(frame_w - 2, int(mb["x"])))
                y = max(0, min(frame_h - 2, int(mb["y"])))
                w = max(2, min(frame_w - x, int(mb["w"])))
                h = max(2, min(frame_h - y, int(mb["h"])))
                roi = out[y : y + h, x : x + w]
                mask = build_mosaic_mask(roi, dilate=1)
                if int(mask.max()) == 0:
                    continue
                full = np.zeros(out.shape[:2], dtype=np.uint8)
                full[y : y + h, x : x + w] = mask
                try:
                    out = lama_inpaint(out, full)
                except Exception:
                    from demosaic_codeformer import restore_mosaic_crop

                    restored, _bg = restore_mosaic_crop(
                        roi, mask, None, radius=7, use_neural=False
                    )
                    out = out.copy()
                    out[y : y + h, x : x + w] = restored
            hit = False
            for box in boxes_n:
                mask = _lama_box_mask(out, box, mode_s)
                if int(mask.max()) == 0:
                    continue
                if dilate > 0:
                    mask = cv2.dilate(mask, kernel, iterations=int(dilate))
                out = lama_inpaint(out, mask)
                hit = True
            if hit:
                lama_frames += 1
            proc.stdin.write(np.ascontiguousarray(out).tobytes())
            frames_done += 1
            if n_hint and frames_done % 60 == 0:
                print(
                    f"[lama] wrote {frames_done}/{n_hint} boxes={len(boxes_n)} mode={mode_s}",
                    flush=True,
                )
    except Exception:
        proc.kill()
        raise
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    stderr = proc.communicate(timeout=600)[1]
    if proc.returncode != 0 or frames_done < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        try:
            writing.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"lama encode failed ({proc.returncode}): {err}")
    if writing.resolve() != dest.resolve():
        if dest.is_file():
            dest.unlink(missing_ok=True)
        writing.replace(dest)
    print(
        f"[lama] frames={frames_done} lama_hits={lama_frames} boxes={len(boxes_n)} mode={mode_s}",
        flush=True,
    )
    return {
        "engine": "lama",
        "mode": mode_s,
        "frames": frames_done,
        "lama_runs": lama_frames,
        "boxes": len(boxes_n),
        "bytes": dest.stat().st_size,
    }


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
    mosaic_boxes: list[dict[str, int]] | None = None,
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
    hardsub_boxes: list[dict[str, int]] = []
    if dehardsub:
        located_list = mops.resolve_hardsub_boxes(
            src,
            width,
            height,
            mode=("vlm" if locate_mode == "auto" else locate_mode),
            ratio=ratio,
            vlm_model=model,
            work_dir=work_dir,
        )
        located = located_list[0]
        for loc in located_list:
            b = loc["box"]
            if engine_s != "sttn":
                b = mops.refine_hardsub_box_glyphs(src, width, height, b)
            hardsub_boxes.append({k: int(b[k]) for k in ("x", "y", "w", "h")})
            regions.append(
                {
                    "kind": "hardsub",
                    "role": loc.get("role") or "dialogue",
                    **{k: int(b[k]) for k in ("x", "y", "w", "h")},
                }
            )
        box = hardsub_boxes[0]
        locate_meta = {
            "box": box,
            "boxes": hardsub_boxes,
            "box_roles": [loc.get("role") for loc in located_list],
            "box_seed": located.get("box_seed") or located.get("box"),
            "box_source": located.get("source"),
            "tightened": bool(located.get("tightened")),
            "locate": {
                k: located.get(k)
                for k in ("norm", "hits", "source", "model", "samples", "tightened")
            },
            "locate_all": [
                {
                    "role": loc.get("role"),
                    "source": loc.get("source"),
                    "box": loc.get("box"),
                }
                for loc in located_list
            ],
            "engine": engine_s,
        }

    mosaic_regions: list[dict[str, Any]] = []
    if demosaic:
        if mosaic_boxes:
            # 显式马赛克框（绕过自动检测；真实场景可手动标注）
            mosaic_regions = [
                {
                    "kind": "mosaic",
                    **{k: int(b[k]) for k in ("x", "y", "w", "h")},
                }
                for b in mosaic_boxes
            ]
        else:
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

    if engine_s == "lama" and dehardsub and box is not None:
        try:
            from inpaint_lama import LamaUnavailable
        except ImportError:
            LamaUnavailable = None  # type: ignore[assignment]
        lama_ran = False
        if LamaUnavailable is not None:
            try:
                lama_stats = encode_lama(src, dest, box, mosaic_regions)
                lama_ran = True
            except LamaUnavailable as exc:
                print(f"[cleanup] lama unavailable ({exc}); falling back to sttn")
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanup] lama failed ({exc}); falling back to sttn")
        if lama_ran:
            return {
                "action": "lama_cleanup",
                "engine": "lama",
                "mosaic_engine": str(demosaic_engine).lower(),
                "passes": [{"pass": 1, "encode": lama_stats}],
                "pass_count": 1,
                "regions": regions,
                "mosaic_regions": mosaic_regions,
                "demosaic": demosaic,
                "dehardsub": dehardsub,
                "src_size": f"{width}x{height}",
                **locate_meta,
                "encode": lama_stats,
            }
        # 降级：继续走下方 STTN 分支（engine_s 保持 sttn 语义）
        engine_s = "sttn"

    if engine_s == "sttn" and dehardsub and box is not None:
        from sttn_inpaint import encode_sttn

        demosaic_engine_s = str(demosaic_engine).lower()
        sttn_in = src
        mosaic_tmp = None
        mosaic_boxes = None

        if demosaic and mosaic_regions and demosaic_engine_s == "sttn":
            # 马赛克与字幕一起作为 STTN 的洞（合一方案）。
            mosaic_boxes = [
                {k: int(m[k]) for k in ("x", "y", "w", "h")} for m in mosaic_regions
            ]
        elif demosaic and mosaic_regions and demosaic_engine_s == "lama":
            # 通用马赛克：先 LaMa 逐帧填洞，再 STTN 去字幕（out_real5 同款）。
            mosaic_tmp = dest.with_name("_mosaic_pre.mp4")
            try:
                encode_cleanup_pass(
                    src,
                    mosaic_tmp,
                    mosaic_regions,
                    dilate=1,
                    inpaint_radius=7,
                    do_hardsub=False,
                    do_mosaic=True,
                    mosaic_engine="lama",
                    mask_from=src,
                )
                sttn_in = mosaic_tmp
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanup] lama demosaic failed ({exc}); fall back sttn holes")
                mosaic_boxes = [
                    {k: int(m[k]) for k in ("x", "y", "w", "h")} for m in mosaic_regions
                ]
                mosaic_tmp = None
        elif demosaic and mosaic_regions:
            # codeformer / opencv：先在原帧修马赛克，再 STTN 去字幕。
            mosaic_tmp = dest.with_name("_mosaic_pre.mp4")
            encode_cleanup_pass(
                src,
                mosaic_tmp,
                mosaic_regions,
                dilate=1,
                inpaint_radius=7,
                do_hardsub=False,
                do_mosaic=True,
                mosaic_engine=demosaic_engine_s,
                mask_from=src,
            )
            sttn_in = mosaic_tmp

        # 去字幕：对话用 STTN 时序；标题/水印用 LaMa 融背景；最后 LaMa 收残影。
        sttn_out = dest if sttn_in.resolve() == src.resolve() else dest.with_name("_sttn.mp4")
        boxes_run = hardsub_boxes or [{k: int(box[k]) for k in ("x", "y", "w", "h")}]
        roles = list(locate_meta.get("box_roles") or [])
        current = sttn_in
        temps: list[Path] = []
        pass_stats: list[dict[str, Any]] = []
        try:
            from inpaint_lama import LamaUnavailable
        except ImportError:
            LamaUnavailable = None  # type: ignore[misc, assignment]

        def _try_lama(
            inp: Path,
            outp: Path,
            boxes_l: list[dict[str, int]],
            tag: str,
            *,
            mask_mode: str = "glyph",
        ) -> dict[str, Any] | None:
            if LamaUnavailable is None:
                return None
            try:
                stats = encode_lama_boxes(
                    inp, outp, boxes_l, dilate=2, mask_mode=mask_mode
                )
                stats = dict(stats)
                stats["role"] = tag
                return stats
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanup] lama {tag} skipped ({exc})", flush=True)
                return None

        try:
            for i, hb in enumerate(boxes_run):
                out_i = dest.with_name(f"_hs_r{i}.mp4")
                temps.append(out_i)
                role = roles[i] if i < len(roles) else "dialogue"
                box_i = {k: int(hb[k]) for k in ("x", "y", "w", "h")}
                box_run = (
                    expand_static_overlay_box(box_i, width, height, role=role)
                    if role in ("title", "watermark")
                    else box_i
                )
                if role in ("title", "watermark"):
                    import cv2
                    import numpy as np
                    from sttn_inpaint import _open_ffmpeg_writer

                    cap_fill = cv2.VideoCapture(str(current))
                    if not cap_fill.isOpened():
                        raise RuntimeError(f"cannot open {current} for edge overlay fill")
                    fps_fill = float(cap_fill.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
                    n_fill = int(cap_fill.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    proc_fill = _open_ffmpeg_writer(current, out_i, width, height, fps_fill)
                    assert proc_fill.stdin is not None
                    frames_fill = 0
                    try:
                        while True:
                            ok_fill, fr_fill = cap_fill.read()
                            if not ok_fill:
                                break
                            if role == "watermark":
                                out_fill = restore_edge_overlay(fr_fill, box_run)
                            else:
                                out_fill = restore_static_overlay_box(fr_fill, box_run)
                            proc_fill.stdin.write(np.ascontiguousarray(out_fill).tobytes())
                            frames_fill += 1
                            if n_fill and frames_fill % 60 == 0:
                                print(f"[edgefill:{role}] wrote {frames_fill}/{n_fill}", flush=True)
                    except Exception:
                        proc_fill.kill()
                        raise
                    finally:
                        cap_fill.release()
                        try:
                            proc_fill.stdin.close()
                        except Exception:
                            pass
                    stderr_fill = proc_fill.communicate(timeout=300)[1]
                    if proc_fill.returncode != 0 or frames_fill < 1:
                        err_fill = (stderr_fill or b"").decode("utf-8", errors="replace")[:400]
                        raise RuntimeError(
                            f"edge overlay fill failed ({proc_fill.returncode}): {err_fill}"
                        )
                    used = {
                        "engine": "copyfill" if role == "watermark" else "boxfill",
                        "mode": "edge_patch" if role == "watermark" else "box_inpaint",
                        "frames": frames_fill,
                        "bytes": out_i.stat().st_size if out_i.is_file() else 0,
                        "role": role,
                        "box": box_run,
                    }
                    pass_stats.append(used)
                    current = out_i
                    continue
                env_prev = None
                force_tiles = role in ("title", "watermark")
                if role == "dialogue":
                    try:
                        import cv2
                        from sttn_inpaint import band_is_flat as _bif

                        cap_chk = cv2.VideoCapture(str(current))
                        ok_chk, fr_chk = cap_chk.read()
                        cap_chk.release()
                        if ok_chk and not _bif(fr_chk, box_i):
                            force_tiles = True
                    except Exception:
                        force_tiles = True
                if force_tiles:
                    import os as _os

                    env_prev = _os.environ.get("VITUAL_STTN_FORCE")
                    if (env_prev or "auto").strip().lower() == "auto":
                        _os.environ["VITUAL_STTN_FORCE"] = "tiles"
                try:
                    used = encode_sttn(
                        current,
                        out_i,
                        box_run,
                        mosaic_boxes=(mosaic_boxes if i == 0 else None),
                    )
                    used = dict(used)
                    used["role"] = role
                    used["box"] = box_run
                finally:
                    if force_tiles:
                        import os as _os

                        if env_prev is None:
                            _os.environ.pop("VITUAL_STTN_FORCE", None)
                        else:
                            _os.environ["VITUAL_STTN_FORCE"] = env_prev
                pass_stats.append(used)
                current = out_i

            # Residual polish: only static overlays are safe for LaMa touch-up here.
            polish_roles = {"title", "watermark"}
            polish_boxes = [
                expand_static_overlay_box(
                    {k: int(hb[k]) for k in ("x", "y", "w", "h")},
                    width,
                    height,
                    role=(roles[i] if i < len(roles) else "dialogue"),
                )
                for i, hb in enumerate(boxes_run)
                if (roles[i] if i < len(roles) else "dialogue") in polish_roles
            ]
            polish = None
            if polish_boxes:
                polish = _try_lama(
                    current, sttn_out, polish_boxes, "polish", mask_mode="residual"
                )
            if polish is None:
                if current.resolve() != sttn_out.resolve():
                    import shutil

                    shutil.copy2(current, sttn_out)
            else:
                pass_stats.append(polish)

            sttn_stats = {
                "engine": "hybrid",
                "regions": len(boxes_run),
                "passes": pass_stats,
                "mode": "sttn+lama",
                "frames": pass_stats[-1].get("frames") if pass_stats else 0,
                "device": pass_stats[0].get("device") if pass_stats else "cpu",
                "bytes": sttn_out.stat().st_size if sttn_out.is_file() else 0,
            }
        finally:
            for t in temps:
                try:
                    if t.resolve() != sttn_out.resolve():
                        t.unlink(missing_ok=True)
                except OSError:
                    pass
        if mosaic_tmp is not None and mosaic_tmp.resolve() != dest.resolve():
            try:
                mosaic_tmp.unlink(missing_ok=True)
            except OSError:
                pass
        if sttn_out.resolve() != dest.resolve():
            sttn_out.replace(dest)
        print(
            f"[cleanup] hybrid frames={sttn_stats.get('frames')} "
            f"regions={len(boxes_run)} mosaic_pre={'on' if mosaic_tmp is not None else 'off'} "
            f"(demosaic={demosaic_engine_s})"
        )
        return {
            "action": "hybrid_cleanup",
            "engine": "sttn+lama",
            "mosaic_engine": demosaic_engine_s,
            "mosaic_pre": mosaic_tmp is not None,
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
            mosaic_engine=str(demosaic_engine).lower() if demosaic else "opencv",
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
