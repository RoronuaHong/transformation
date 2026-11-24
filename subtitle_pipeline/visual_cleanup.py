"""WF-03b general visual cleanup: hardsubs + mosaic, multi-pass validate/repair.

Designed to be content-agnostic (not tied to one dark-UI layout):
  1) locate work regions (caption box + mosaic blobs)
  2) per-frame build masks (glyph / block-grid)
  3) repair (OpenCV inpaint + temporal blend)
  4) sample-validate residual → strengthen mask → repeat until clean or max passes
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import warnings
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
    # Top-right logos: sample LEFT along the same top strip (not from below —
    # below often contains faces and hard-paste duplicates heads into the bar).
    # Top-left logos: sample RIGHT along the strip toward center — sampling
    # from below pastes forehead/eyes (v2/cur face-tear at demo t=1).
    if touch_top and (touch_right or x0 >= int(w * 0.45)):
        # Morphic 鳞片: tile a *narrow* left band. Pick gap so donor is not
        # still inside the glyph glow and not a darker plate (fi=69).
        near_w = max(24, min(48, bw // 8))
        zone_g = cv2.cvtColor(out[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
        z_bg = zone_g[zone_g < 150]
        zone_med = (
            float(np.median(z_bg)) if z_bg.size >= 32 else float(np.median(zone_g))
        )
        best: tuple[float, int, int] | None = None
        for gap in (2, 16, 32, 48, 72, 96, 128, 160):
            sx1_c = max(near_w, x0 - gap)
            sx0_c = max(0, sx1_c - near_w)
            if sx1_c - sx0_c < 8:
                continue
            sg = cv2.cvtColor(out[y0:y1, sx0_c:sx1_c], cv2.COLOR_BGR2GRAY)
            bright = float((sg > 170).mean())
            med = float(np.median(sg))
            score = bright * 3.0 + abs(med - zone_med) / 80.0
            if med + 15.0 < zone_med:
                score += 2.5  # dark-plate risk
            if best is None or score < best[0]:
                best = (score, sx0_c, sx1_c)
        if best is not None:
            _, sx0, sx1 = best
            sy0, sy1 = y0, y1
        else:
            sx1 = max(near_w, x0 - 2)
            sx0 = max(0, sx1 - near_w)
            sy0, sy1 = y0, y1
        if sx1 - sx0 < 8:
            sy0 = min(h - bh, y1 + 2)
            sy1 = min(h, sy0 + bh)
            sx0, sx1 = x0, x1
    elif touch_top and (touch_left or x0 <= int(w * 0.25)):
        # Prefer BELOW when that band is not forehead (locker t=7 → mae≪right).
        # Face close-ups (t=1): below pastes brow into the bar — keep RIGHT.
        sy0_b = min(h - bh, y1 + 2)
        sy1_b = min(h, sy0_b + bh)
        below = out[sy0_b:sy1_b, x0:x1]
        below_skin = 1.0
        if below.size >= 64:
            hsv_b = cv2.cvtColor(below, cv2.COLOR_BGR2HSV)
            below_skin = float(
                (
                    (hsv_b[..., 0] < 25)
                    & (hsv_b[..., 1] > 35)
                    & (hsv_b[..., 2] > 60)
                    & (hsv_b[..., 2] < 220)
                ).mean()
            )
        if below_skin < 0.10 and below.shape[0] == bh and below.shape[1] == bw:
            sy0, sy1, sx0, sx1 = sy0_b, sy1_b, x0, x1
        else:
            sx0 = min(w - bw, x1 + 2)
            sx1 = min(w, sx0 + bw)
            sy0, sy1 = y0, y1
            if sx1 - sx0 < bw:
                # Last resort: shallow below only (still better than empty).
                sy0, sy1, sx0, sx1 = sy0_b, sy1_b, x0, x1
    elif touch_top:
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
        # Morphic near-left is narrower than the bar — stretch (tile made a
        # vertical comb at t=7).
        sample = cv2.resize(sample, (bw, bh), interpolation=cv2.INTER_LINEAR)

    # Top/side logo bars: hard-replace with the donor. Soft feather left Morphic /
    # handle glyphs visible (demo10s topR bright% only halved).
    if touch_top or touch_right or touch_left or (touch_bottom and bh <= 64):
        out[y0:y1, x0:x1] = sample
        return out
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
    elif role_s == "up_watermark":
        # Height must stay a thin strip — pad_b=18..22 grew Morphic to h≈70 and
        # logo-TELEA smeared hangers/ceiling (demo v7 t=9s).
        pad_l, pad_t, pad_r, pad_b = 10, 2, 14, 6
        x0b = int(box.get("x") or 0)
        y0b = int(box.get("y") or 0)
        if y0b <= max(4, int(frame_h * 0.12)):
            if x0b >= int(frame_w * 0.45):
                # Morphic: thin strip; extra left pad so seam sits off the glyphs.
                # pad_b must clear "Made with Morphic" underbelly (~y52–55 on 480p).
                pad_l, pad_r, pad_b = 40, 28, 8
            elif x0b <= int(frame_w * 0.25):
                pad_l = max(pad_l, x0b)  # Poootaaaato to left edge
                pad_r, pad_b = 12, 2
        else:
            # Mid @handle: tiny pad only.
            pad_l, pad_t, pad_r, pad_b = 6, 4, 8, 6
    else:
        pad_l = pad_t = pad_r = pad_b = 0
    x0 = max(0, int(box["x"]) - pad_l)
    y0 = max(0, int(box["y"]) - pad_t)
    x1 = min(frame_w, int(box["x"]) + int(box["w"]) + pad_r)
    y1 = min(frame_h, int(box["y"]) + int(box["h"]) + pad_b)
    # Cap top strips. Left (Poootaaaato) stays ~40 to avoid dark-rect copyfill;
    # right Morphic glyphs peak ~row 44–52 on BV1P6 — old 0.085 cap (h=41)
    # left "Made with Morphic" entirely below the wipe box.
    if role_s == "up_watermark" and y0 <= max(4, int(frame_h * 0.12)):
        x0b = int(box.get("x") or 0)
        if x0b >= int(frame_w * 0.45):
            max_h = max(56, int(round(frame_h * 0.12)))
        else:
            max_h = max(36, int(round(frame_h * 0.085)))
        if (y1 - y0) > max_h:
            y1 = y0 + max_h
    return {"x": x0, "y": y0, "w": max(2, x1 - x0), "h": max(2, y1 - y0)}


def _logo_ink_mask_roi(roi_bgr: Any) -> Any:
    """Logo/handle ink mask (uint8 0/255).

    Hard bright ink (V>168) plus **D5 residual ink**: pixels brighter than the
    local median with low saturation — catches soft translucent ``@`` / Morphic
    that fail a fixed V threshold (BV1P6 left @ mean ink≈0.006 on V>168 alone).
    """
    import cv2
    import numpy as np

    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
    m = ((hsv[..., 2] > 168) & (hsv[..., 1] < 110)).astype(np.uint8) * 255
    m |= (gray > 175).astype(np.uint8) * 255
    # Soft residual vs local median (exclude very dark BG noise).
    med = float(np.median(gray))
    resid = (gray.astype(np.float32) - med) >= 14.0
    soft = resid & (hsv[..., 1] < 120) & (gray > max(40, int(med * 0.85)))
    m |= soft.astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return m


def _roi_half_wipe(src_roi: Any, out_roi: Any) -> bool:
    """True when fill is a worse half-wipe than leaving the source mark (D9).

    BV1P6: naive w2b≥0.04 rolled back 600–850/1149 frames and left Morphic /
    bilibili text intact. If bright ink is mostly gone, keep the wipe even when
    a dark fringe remains (readable mark only wins when wipe clearly failed).
    """
    import cv2
    import numpy as np

    if src_roi is None or out_roi is None or src_roi.size < 64 or out_roi.size < 64:
        return False
    if src_roi.shape != out_roi.shape:
        return False
    gs = cv2.cvtColor(src_roi, cv2.COLOR_BGR2GRAY)
    gc = cv2.cvtColor(out_roi, cv2.COLOR_BGR2GRAY)
    bright_s = float((gs > 180).mean())
    bright_c = float((gc > 180).mean())
    dark_d = float((gc < 30).mean()) - float((gs < 30).mean())
    w2b = float(((gs > 170) & (gc < 90)).mean())
    # Ink largely cleared → accept wipe (fringe/dark hole OK vs leaving logo).
    if bright_s >= 0.02 and bright_c <= max(0.012, bright_s * 0.35):
        return False
    # Failed wipe: still lots of bright ink, but painted a dark blotch.
    if w2b >= 0.04 and bright_c > max(0.02, bright_s * 0.35):
        return True
    if bright_s > 0.05 and bright_c < bright_s * 0.2 and dark_d > 0.08:
        return True
    return False


def top_strip_motion_score(
    video: Path,
    box: dict[str, int],
    *,
    samples: int = 10,
) -> float:
    """D12: mean abs-diff of successive ROI samples (higher = moving / face drift)."""
    import cv2
    import numpy as np

    video = Path(video)
    box = {k: int(box[k]) for k in ("x", "y", "w", "h")}
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return 0.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if n < 2:
        cap.release()
        return 0.0
    idxs = [
        min(n - 1, max(0, int(round(i * (n - 1) / max(1, samples - 1)))))
        for i in range(max(2, samples))
    ]
    prev = None
    diffs: list[float] = []
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    try:
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, fr = cap.read()
            if not ok or fr is None:
                continue
            roi = fr[y : y + h, x : x + w]
            if roi.size < 64:
                continue
            g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
            if prev is not None and prev.shape == g.shape:
                diffs.append(float(np.mean(np.abs(g - prev))))
            prev = g
    finally:
        cap.release()
    return float(np.mean(diffs)) if diffs else 0.0


def _fill_v2_enabled() -> bool:
    """2026-11-21 验证过的分区填充策略（顶栏 NS 扩散 + 字幕 LaMa 扩张 mask）。

    LaMa 是生成式模型：顶栏白字密集区它会把水印"生成回来"（第一帧实测
    bright 0.081→0.081）；顶栏必须用非生成的 NS 扩散。字幕压红领章等复杂
    纹理则用 LaMa，且 mask 要扩张含描边（LaMa-fp32 关键点）。
    """
    import os
    return os.environ.get("VITUAL_DEHARDSUB_FILL_V2", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _adaptive_watermark_enabled() -> bool:
    """2026-11-21 精修首帧策略：中景半透明 @ 走逐帧自适应残差 mask + LaMa。"""
    import os
    return os.environ.get("VITUAL_DEHARDSUB_WM_ADAPTIVE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


print(
    f"[visual_cleanup] WM env adaptive={os.environ.get('VITUAL_DEHARDSUB_WM_ADAPTIVE')} "
    f"fillv2={os.environ.get('VITUAL_DEHARDSUB_FILL_V2')}",
    flush=True,
)


def adaptive_lama_watermark_wipe(frame: Any, box: dict[str, int]) -> Any:
    """逐帧 residual>9~10 + gray + sat 检测中景 @，膨胀后 LaMa 重建。

    移植自首帧精修 fix_frame1_final2.py：固定 logo 共识 mask 抓不到半透明
    @（BV1P6 左 @ 在 V>168 下 ink≈0.006），必须逐帧按局部中值残差找字形，
    再把 mask 扩张到描边/抗锯齿后交给 LaMa。左右阈值不同（左暗墙、右肩章）。
    """
    import cv2
    import numpy as np

    try:
        from inpaint_lama import LAMA_WEIGHTS, lama_inpaint
    except Exception:
        return frame
    if not LAMA_WEIGHTS.is_file():
        return frame
    h, w = frame.shape[:2]
    x0, y0, bw, bh = _box_xywh(box)
    xa, ya = max(0, x0 - 6), max(0, y0 - 6)
    xb, yb = min(w, x0 + bw + 6), min(h, y0 + bh + 6)
    roi = frame[ya:yb, xa:xb]
    if roi.size < 64:
        return frame
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    sat = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[..., 1]
    local_med = cv2.medianBlur(gray, 15).astype(np.float32)
    residual = gray.astype(np.float32) - local_med
    if x0 < int(w * 0.45):
        sel = (residual > 10) & (gray > 45) & (sat < 110)
        k = 9
    else:
        sel = (residual > 9) & (gray > 50) & (sat < 105)
        k = 7
    m = cv2.dilate(sel.astype(np.uint8) * 255, np.ones((k, k), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    if int(m.max()) == 0:
        return frame
    full = np.zeros((h, w), dtype=np.uint8)
    full[ya:yb, xa:xb] = m
    with _lama_lock():
        return lama_inpaint(frame, full)


def ns_box_wipe(frame: Any, box: dict[str, int], *, feather: int = 6,
                radius: int = 5) -> Any:
    """整框 Navier-Stokes 扩散擦除 + 边缘羽化（非生成，不会重建字）。

    用于顶栏半透明/白字水印：局部中值残差在白字密集区失效，LaMa 会把字
    生成回来；NS 扩散沿边缘颜色填入暗渐变，第一帧实测 bright 0.081→0。
    羽化让框边界落在纹理连续处，避免水平亮带补丁感。
    """
    import cv2
    import numpy as np
    x0, y0, bw, bh = _box_xywh(box)
    h, w = frame.shape[:2]
    xa, ya = max(0, x0), max(0, y0)
    xb, yb = min(w, x0 + bw), min(h, y0 + bh)
    # 顶栏框常偏矮（regions h=48 但 Morphic 字到 y55）：若框下沿仍有高亮
    # 文字像素，向下扩展框盖住整字，否则 NS 只扩散到字的上半部→亮带残留。
    # 最多再扩 bh 的 60%，且只向下不向左（避免吃到人脸）。
    if y0 <= h * 0.15:
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # 字尾/光带常超出被 cap 的框高（regions h=48 → cap 41，字尾到 y55）。
        # 探测整段框高（不再只 0.6*bh），阈值放宽到 0.004（细字尾只占少量像素）。
        extra = min(bh, h - yb)
        probe = g[yb:min(h, yb + extra), xa:xb]
        if probe.size:
            # 只看框下沿紧邻的 12 行：字尾一定贴着框，远处光束不应触发扩框。
            row_frac = (probe > 150).mean(axis=1)[:12]
            rows = np.where(row_frac > 0.008)[0]
            if len(rows):
                yb = min(h, yb + int(rows[-1]) + 5)
    if xb <= xa or yb <= ya:
        return frame
    # 稍微外扩，让 NS 从框外干净像素取样，边界落在纹理连续处
    ex = min(feather, min(xa, w - xb, ya, h - yb))
    xa2, ya2 = max(0, xa - ex), max(0, ya - ex)
    xb2, yb2 = min(w, xb + ex), min(h, yb + ex)
    mask = np.zeros((h, w), dtype=np.uint8)
    mask[ya:yb, xa:xb] = 255
    ns = cv2.inpaint(frame, mask, radius, cv2.INPAINT_NS)
    # 羽化：只在框边一圈渐变混合，框内 100% 用 NS
    if feather > 0:
        soft = cv2.GaussianBlur(mask, (5, 5), 0).astype(np.float32) / 255.0
        a = soft[..., None]
        out = np.clip(frame.astype(np.float32) * (1 - a) + ns.astype(np.float32) * a,
                      0, 255).astype(np.uint8)
    else:
        out = ns
    # 只贴回扩展后的框区域
    result = frame.copy()
    result[ya2:yb2, xa2:xb2] = out[ya2:yb2, xa2:xb2]
    return result


def resolve_up_watermark_method(
    box: dict[str, int],
    frame_w: int,
    frame_h: int,
    *,
    motion: float | None = None,
) -> str:
    """Single gate for top/mid up_watermark routing (D7 + D12 motion).

    Replaces the old magic ``y0<=8 & h<=48`` auto path so expand heights
    (h=48..57) still classify as top strip copyfill/morphic.
    High motion: avoid blind neighbour copyfill (forehead paste); prefer
    morphic/logo + half-wipe rollback.
    """
    if up_watermark_prefers_boxfill(box, frame_h, frame_w):
        x0 = int(box.get("x") or 0)
        # Left dark logo (Poootaaaato): copyfill / optional NS.
        # Right Morphic bar: always morphic — FILL_V2 ns_box left a readable
        # blur ghost on BV1P6 (bright≈source). Never put Morphic on ns_box.
        if x0 < int(frame_w * 0.45):
            base = "ns_box" if _fill_v2_enabled() else "copyfill"
        else:
            base = "morphic"
    elif _adaptive_watermark_enabled():
        base = "adaptive_lama"
    else:
        base = "logo"
    if motion is None:
        return base
    m = float(motion)
    # Prefer v2 left-strip copyfill (clears Poootaaaato). Only abandon copyfill
    # on extreme motion — threshold 14 flipped BV1P6 left→morphic and left more
    # soft ink than v2's hard edge paste (face-paste tradeoff accepted like v2).
    if m >= 40.0 and base == "copyfill":
        return "morphic"
    # Morphic→logo was too eager (BV1P6 motion~30): logo+half-wipe left the
    # bar text. Keep morphic unless motion is extreme.
    if m >= 45.0 and base == "morphic":
        return "logo"
    return base


def audit_dialogue_safety_hits(
    src_video: Path,
    clean_video: Path,
    box: dict[str, int],
    *,
    max_darken: float = 0.15,
    w2b_thr: float = 0.04,
) -> dict[str, Any]:
    """Count frames that would still trip the safety gate on the final clean (D1)."""
    import cv2

    src_video, clean_video = Path(src_video), Path(clean_video)
    box = {k: int(box[k]) for k in ("x", "y", "w", "h")}
    caps = cv2.VideoCapture(str(src_video))
    capc = cv2.VideoCapture(str(clean_video))
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    frames = 0
    would = 0
    max_dlt = 0.0
    max_w2b = 0.0
    try:
        while True:
            ok_c, fr = capc.read()
            ok_s, sfr = caps.read()
            if not ok_c:
                break
            frames += 1
            if not ok_s or fr.shape != sfr.shape or w < 2 or h < 2:
                continue
            cg = cv2.cvtColor(fr[y : y + h, x : x + w], cv2.COLOR_BGR2GRAY)
            sg = cv2.cvtColor(sfr[y : y + h, x : x + w], cv2.COLOR_BGR2GRAY)
            dlt = float((cg < 30).mean()) - float((sg < 30).mean())
            w2b = float(((sg > 170) & (cg < 90)).mean())
            max_dlt = max(max_dlt, dlt)
            max_w2b = max(max_w2b, w2b)
            # Gold embroidery stays bright in both src/clean — use "cleared
            # former caption pixels" not raw bright_c (BV1P6 false restores).
            bright_s = float((sg > 170).mean())
            cleared = float(((sg > 170) & (cg < 120)).mean())
            # Mirror encode_dialogue_safety_gate exactly: the old hardcoded
            # bright_s>=0.03 undercounted would_restore on dark animated
            # captions where the live gate already uses min_bright=0.0.
            _strict = os.environ.get("VITUAL_DIALOGUE_SAFETY_STRICT", "") in (
                "1", "true", "yes",
            )
            min_bright = 0.03 if _strict else 0.0
            # Mirror encode gate: keep 0.65 (0.55 broke the half-wipe restore
            # guard). BV1P6 residual handled by VITUAL_DIALOGUE_SAFETY_OFF.
            glyphs_mostly_gone = bright_s >= min_bright and cleared >= max(
                0.02, bright_s * 0.65
            )
            hit_dark = dlt > float(max_darken)
            hit_w2b = w2b >= float(w2b_thr)
            # Match encode gate: w2b + readable leftovers only (not darkening).
            if hit_w2b and not glyphs_mostly_gone:
                would += 1
    finally:
        caps.release()
        capc.release()
    return {
        "frames": frames,
        "would_restore": would,
        "max_dlt": round(max_dlt, 4),
        "max_w2b": round(max_w2b, 4),
        "min_bright": float(min_bright),
        "box": box,
    }


def build_static_logo_mask(
    video: Path,
    box: dict[str, int],
    *,
    samples: int = 24,
    hit_frac: float = 0.55,
) -> Any | None:
    """Consensus logo mask: bright only where ink persists across frames.

    Per-frame bright masks also catch sweat / forehead highlights on close-ups
    (demo v10 t=0). Static logos vote high; moving skin highlights do not.
    """
    import cv2
    import numpy as np

    video = Path(video)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None
    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        if n < 2 or W < 2 or H < 2:
            return None
        x0 = max(0, min(W - 1, int(box["x"])))
        y0 = max(0, min(H - 1, int(box["y"])))
        x1 = max(x0 + 1, min(W, x0 + max(1, int(box["w"]))))
        y1 = max(y0 + 1, min(H, y0 + max(1, int(box["h"]))))
        bw, bh = x1 - x0, y1 - y0
        idxs = np.linspace(0, max(0, n - 1), int(min(samples, n))).astype(int)
        acc = np.zeros((bh, bw), dtype=np.float32)
        used = 0
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
            ok, fr = cap.read()
            if not ok:
                continue
            roi = fr[y0:y1, x0:x1]
            if roi.shape[0] != bh or roi.shape[1] != bw:
                continue
            acc += (_logo_ink_mask_roi(roi) > 0).astype(np.float32)
            used += 1
        if used < 3:
            return None
        hit = (acc >= float(hit_frac) * float(used)).astype(np.uint8) * 255
        # Grow a bit so soft anti-aliased Morphic edges are covered (ghosting
        # in demo v10 came from under-dilated glyph holes).
        hit = cv2.dilate(hit, np.ones((5, 5), np.uint8), iterations=2)
        # Reject masks that ate most of the ROI (false-positive on bright BG).
        frac = float((hit > 0).mean())
        if frac < 0.004 or frac > 0.45:
            return None
        full = np.zeros((H, W), dtype=np.uint8)
        full[y0:y1, x0:x1] = hit
        return full
    finally:
        cap.release()


def restore_logo_pixels(
    frame: Any,
    box: dict[str, int],
    mask: Any | None = None,
    *,
    residual_copyfill: bool = False,
) -> Any:
    """TELEA only inside a (preferably static) logo mask — never full-box paste.

    ``residual_copyfill`` (top strips): after TELEA, soft Morphic leftovers are
    replaced **only on residual ink pixels** from an edge donor. Full-strip
    copyfill pastes faces into the bar on close-ups; masked copyfill does not.
    """
    import cv2
    import numpy as np

    out = frame.copy()
    h, w = out.shape[:2]
    x0 = max(0, min(w - 1, int(box["x"])))
    y0 = max(0, min(h - 1, int(box["y"])))
    x1 = max(x0 + 1, min(w, x0 + max(1, int(box["w"]))))
    y1 = max(y0 + 1, min(h, y0 + max(1, int(box["h"]))))
    if mask is None:
        roi = out[y0:y1, x0:x1]
        if roi.size < 64:
            return out
        m = _logo_ink_mask_roi(roi)
        m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=1)
        if int(m.max()) == 0:
            return out
        full = np.zeros((h, w), dtype=np.uint8)
        full[y0:y1, x0:x1] = m
        mask = full
    if int(mask.max()) == 0:
        return out
    # Slightly larger radius reduces Morphic halo vs radius=3.
    filled = cv2.inpaint(out, mask, 5, cv2.INPAINT_TELEA)
    # Second TELEA pass: residual bright ink still inside the static hole.
    roi = filled[y0:y1, x0:x1]
    hole = mask[y0:y1, x0:x1]
    if roi.size >= 64 and int(hole.max()) > 0:
        resid = _logo_ink_mask_roi(roi)
        resid = cv2.bitwise_and(resid, hole)
        resid = cv2.dilate(resid, np.ones((3, 3), np.uint8), iterations=1)
        if int(resid.max()) > 0 and float((resid > 0).mean()) < 0.35:
            full2 = np.zeros((h, w), dtype=np.uint8)
            full2[y0:y1, x0:x1] = resid
            filled = cv2.inpaint(filled, full2, 3, cv2.INPAINT_TELEA)
    # Soft Morphic leftovers: re-TELEA only inside the existing hole (no hole
    # expand — that mushied left bilibili into a dark blob at t=7).
    roi = filled[y0:y1, x0:x1]
    hole = mask[y0:y1, x0:x1]
    if roi.size >= 64 and int(hole.max()) > 0:
        soft = _logo_ink_mask_roi(roi)
        soft = cv2.bitwise_and(soft, hole)
        soft = cv2.dilate(soft, np.ones((2, 2), np.uint8), iterations=1)
        if 0.004 <= float((soft > 0).mean()) < 0.35:
            full3 = np.zeros((h, w), dtype=np.uint8)
            full3[y0:y1, x0:x1] = soft
            filled = cv2.inpaint(filled, full3, 3, cv2.INPAINT_TELEA)
    if not residual_copyfill:
        return filled
    # Soft Morphic leftovers: small → paste donor ink-only; large → another
    # TELEA (wide paste became the dark Morphic rectangle at t=7).
    roi = filled[y0:y1, x0:x1]
    hole2 = mask[y0:y1, x0:x1]
    if roi.size < 64 or int(hole2.max()) == 0:
        return filled
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    soft = ((hsv[..., 2] > 125) & (hsv[..., 1] < 115)).astype(np.uint8) * 255
    soft |= _logo_ink_mask_roi(roi)
    soft = cv2.bitwise_and(soft, hole2)
    soft = cv2.dilate(soft, np.ones((2, 2), np.uint8), iterations=1)
    frac = float((soft > 0).mean())
    if frac < 0.008:
        return filled
    if frac > 0.14:
        full4 = np.zeros((h, w), dtype=np.uint8)
        full4[y0:y1, x0:x1] = soft
        return cv2.inpaint(filled, full4, 4, cv2.INPAINT_TELEA)
    donor = restore_edge_overlay(frame, box)
    d_roi = donor[y0:y1, x0:x1]
    if d_roi.shape != roi.shape:
        return filled
    m3 = (soft > 0)[:, :, None]
    mixed = np.where(m3, d_roi, roi)
    filled[y0:y1, x0:x1] = mixed
    return filled


def encode_dialogue_safety_gate(
    src_video: Path,
    clean_video: Path,
    dest_video: Path,
    box: dict[str, int],
    *,
    max_darken: float = 0.15,
    w2b_thr: float = 0.04,
) -> dict[str, Any]:
    """Per-frame: restore source ROI when TELEA left a dark bar or black glyphs.

    Aligns with ``qa_pair`` darkened (≥0.15) and white_to_black (≥0.04).
    Use the **confirm / route dialogue box** (not a heavily padded band) so the
    gate is not diluted below threshold (BV1P6 pad Δdark 0.419→0.376).
    """
    import cv2
    import numpy as np
    from _inpaint_shared import _open_ffmpeg_writer

    src_video, clean_video, dest_video = Path(src_video), Path(clean_video), Path(dest_video)
    box = {k: int(box[k]) for k in ("x", "y", "w", "h")}
    # 2026-11-22: three heuristics (w2b / dark / mush) kept pasting the ORIGINAL
    # captioned frames back over a successful OCR wipe — 526/1148 then 141/1148
    # frames, causing 20.8-42.4% clear-gate failures on BV1P6. Verified on f553 /
    # f925 / f1090: OCR direct output was clean, the gate restored captions.
    # The guard's net harm exceeds its protection (dark-smear is already caught
    # by LaMa dark-fill repair + the clear gate). Opt-out for affected jobs.
    if os.environ.get("VITUAL_DIALOGUE_SAFETY_OFF") == "1":
        import shutil
        shutil.copyfile(clean_video, dest_video)
        return {"restored": 0, "frames": 0, "disabled": True}
    caps = cv2.VideoCapture(str(src_video))
    capc = cv2.VideoCapture(str(clean_video))
    if not caps.isOpened() or not capc.isOpened():
        raise RuntimeError("dialogue safety gate cannot open videos")
    wh = _media().probe_video_wh(clean_video)
    if wh is None:
        caps.release()
        capc.release()
        raise RuntimeError(f"cannot probe {clean_video}")
    width, height = int(wh[0]), int(wh[1])
    fps = float(capc.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    proc = _open_ffmpeg_writer(clean_video, dest_video, width, height, fps)
    assert proc.stdin is not None
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    restored = 0
    restored_dark = 0
    restored_w2b = 0
    frames = 0
    max_dlt = 0.0
    max_w2b = 0.0
    try:
        while True:
            ok_c, fr = capc.read()
            ok_s, sfr = caps.read()
            if not ok_c:
                break
            frames += 1
            if ok_s and fr.shape == sfr.shape and w > 2 and h > 2:
                cg = cv2.cvtColor(fr[y : y + h, x : x + w], cv2.COLOR_BGR2GRAY)
                sg = cv2.cvtColor(sfr[y : y + h, x : x + w], cv2.COLOR_BGR2GRAY)
                dark_c = float((cg < 30).mean())
                dark_s = float((sg < 30).mean())
                dlt = dark_c - dark_s
                max_dlt = max(max_dlt, dlt)
                w2b = float(((sg > 170) & (cg < 90)).mean())
                max_w2b = max(max_w2b, w2b)
                hit_dark = dlt > float(max_darken)
                hit_w2b = w2b >= float(w2b_thr)
                # BV1P6: raw bright_c stayed high from gold embroidery after a
                # successful wipe → 317/1149 dark restores put captions back.
                # "Cleared" = source-bright pixels that are no longer bright.
                bright_s = float((sg > 170).mean())
                cleared = float(((sg > 170) & (cg < 120)).mean())
                # BV1P6 (animation, thin white glyphs on dark clothing): bright_s
                # is often <0.03 even when the glyphs WERE removed, so the old
                # absolute bright_s>=0.03 gate blocked the protection and let the
                # w2b branch restore 498 frames -> captions came back. The relative
                # condition cleared>=bright_s*0.65 already proves "most source
                # bright glyph pixels are gone"; the absolute 0.02 floor still
                # guards all-dark ROIs. True dark-smear frames are caught
                # independently by hit_mush below, so relaxing this is safe.
                # VITUAL_DIALOGUE_SAFETY_STRICT=1 restores the legacy behaviour.
                _strict = os.environ.get("VITUAL_DIALOGUE_SAFETY_STRICT", "") in (
                    "1", "true", "yes",
                )
                min_bright = 0.03 if _strict else 0.0
                # Restored to 0.65: the 0.55 experiment let half-wiped glyphs
                # (test_dialogue_safety_restores_w2b) escape restoration, breaking
                # the P0 anti-face-smear guard. f398-408's residual issue is
                # handled by turning the whole gate off for this job (see
                # VITUAL_DIALOGUE_SAFETY_OFF), not by weakening this threshold.
                _gmg_ratio = 0.65
                glyphs_mostly_gone = bright_s >= min_bright and cleared >= max(
                    0.02, bright_s * _gmg_ratio
                )
                # Symmetric polarity: dark glyphs on a bright plate are wiped
                # by turning bright. Without this, edge_collapse below sees a
                # successful wipe as scene damage and pastes the source back
                # (test_verify_scenes: 15/15 restored, caption never removed).
                src_dark = float((sg < 90).mean())
                dark_cleared = float(((sg < 90) & (cg > 150)).mean())
                dark_glyphs_mostly_gone = src_dark >= 0.01 and dark_cleared >= max(
                    0.01, src_dark * 0.65
                )
                wipe_ok = glyphs_mostly_gone or dark_glyphs_mostly_gone
                # Restore mid-wipe leftovers, OR mush fills that cleared glyphs
                # but destroyed the scene (BV1P6 t≈5 black chin bar: mae≫20,
                # edge collapsed, dark blotches). Leaving a caption beats a
                # crushed face — next pass / LaMa can retry the restored frame.
                mae = float(np.mean(np.abs(cg.astype(np.float32) - sg.astype(np.float32))))
                edge_s = float(cv2.Laplacian(sg, cv2.CV_64F).var())
                edge_c = float(cv2.Laplacian(cg, cv2.CV_64F).var())
                # BV1P6 (2026-11-22 root cause pinned): a SUCCESSFUL wipe of
                # bright glyphs on dark cloth necessarily (a) drops ROI high-frequency
                # energy, (b) raises mae>=22, (c) raises dark-pixel share because
                # the removed glyphs were bright. All three old "mush" signals
                # therefore fire on a CLEAN wipe. f553: gmg=True (0.149/0.160
                # source-bright glyphs gone) yet dark-share +0.08 fired mush →
                # pasted captioned source back (0.003→0.108), f925/f961 same.
                # When wipe_ok already proves glyphs were removed, edge/dark/m
                # mush are EXPECTED side effects, not damage — disable mush
                # entirely. True damage (face painted black) has w2b with
                # glyphs NOT gone and is still caught by the w2b branch + dlt.
                edge_collapse = edge_s >= 80.0 and edge_c < edge_s * 0.35
                dark_bloom = float((cg < 35).mean()) > float((sg < 35).mean()) + 0.06
                # 2026-11-22 second root cause: 141 frames (f1089/f1090 "Yes,
                # General.") had dlt=0.080 sitting exactly on the 0.08 edge
                # while mae=2.3 and edge 5020→4768 (scene unchanged) — natural
                # dark-cloth variation, NOT a mush. Require a large pixel change
                # alongside darkening: a real damaged wipe has mae>=15 (f553
                # damaged had 46). Without this guard dlt alone pasted captions
                # back on 141 frames.
                hit_mush = (not wipe_ok) and (
                    (dlt >= 0.08 and mae >= 15.0)
                    or (mae >= 22.0 and (edge_collapse or dark_bloom))
                )
                do_restore = (hit_w2b and not glyphs_mostly_gone) or hit_mush
                if do_restore and os.environ.get("VITUAL_SAFETY_DEBUG") == "1":
                    print(
                        f"[safety-debug] frame={frames} w2b={w2b:.4f} gmg={glyphs_mostly_gone} "
                        f"dgm={dark_glyphs_mostly_gone} mae={mae:.2f} dlt={dlt:.3f} "
                        f"edge={edge_s:.0f}->{edge_c:.0f} bloom={dark_bloom} "
                        f"hit_w2b={hit_w2b} mush={hit_mush}",
                        flush=True,
                    )
                if do_restore:
                    fr = fr.copy()
                    fr[y : y + h, x : x + w] = sfr[y : y + h, x : x + w]
                    restored += 1
                    if hit_dark:
                        restored_dark += 1
                    if hit_w2b:
                        restored_w2b += 1
            proc.stdin.write(np.ascontiguousarray(fr).tobytes())
    except Exception:
        proc.kill()
        raise
    finally:
        caps.release()
        capc.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    stderr = proc.communicate(timeout=600)[1]
    if proc.returncode != 0 or frames < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"dialogue safety gate failed: {err}")
    print(
        f"[cleanup] dialogue safety restored {restored}/{frames} "
        f"(dark={restored_dark} w2b={restored_w2b}) box={box}",
        flush=True,
    )
    return {
        "engine": "dialogue_safety",
        "frames": frames,
        "restored": restored,
        "restored_dark": restored_dark,
        "restored_w2b": restored_w2b,
        "max_darken": float(max_darken),
        "w2b_thr": float(w2b_thr),
        "max_dlt": round(max_dlt, 4),
        "max_w2b": round(max_w2b, 4),
        "box": box,
        "role": "dialogue",
    }


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


def _box_xywh(box: dict[str, int]) -> tuple[int, int, int, int]:
    return (
        int(box["x"]),
        int(box["y"]),
        int(box["w"]),
        int(box["h"]),
    )


def _donor_skin_frac(frame_bgr: Any, box: dict[str, int]) -> float:
    """Fraction of left-donor ROI that looks like forehead/face skin."""
    import cv2
    import numpy as np

    x0, y0, bw, bh = _box_xywh(box)
    donor = restore_edge_overlay(frame_bgr, box)
    roi = donor[y0 : y0 + bh, x0 : x0 + bw]
    if roi.size < 64:
        return 1.0
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    skin = (
        (hsv[..., 0] < 25)
        & (hsv[..., 1] > 35)
        & (hsv[..., 2] > 60)
        & (hsv[..., 2] < 220)
    )
    return float(skin.mean())


def _morphic_dark_bg(frame_bgr: Any, box: dict[str, int]) -> bool:
    """True when Morphic sits on locker/ceiling (not forehead close-up)."""
    import cv2
    import numpy as np

    x0, y0, bw, bh = _box_xywh(box)
    roi = frame_bgr[y0 : y0 + bh, x0 : x0 + bw]
    if roi.size < 64:
        return False
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    # Ignore bright logo ink when judging background.
    # t=7 locker median~109 — old thr 100 wrongly forced pure TELEA.
    bg = g[g < 150]
    if bg.size < 32:
        return float(np.median(g)) < 125.0
    return float(np.median(bg)) < 125.0


def morphic_use_copy(frame_bgr: Any, box: dict[str, int]) -> bool:
    """Full left-strip copy on locker BG; TELEA on forehead / luma-mismatch.

    Shared by ``encode_static_boxfill`` and ``encode_multi_static_boxfill`` —
    multi previously omitted the near-left luminance gate and pasted a dark
    rect on BV1P6 fi=69 (officer beam vs dark wall).
    """
    import cv2
    import numpy as np

    x0, y0, bw, bh = _box_xywh(box)
    skin = _donor_skin_frac(frame_bgr, box)
    dark = _morphic_dark_bg(frame_bgr, box)
    if skin >= 0.18:
        return False
    if not dark:
        return False
    # Near-left strip vs zone background (ignore bright logo ink).
    near_w = max(24, min(48, bw // 8))
    sx1 = max(near_w, x0 - 2)
    sx0 = max(0, sx1 - near_w)
    left = frame_bgr[y0 : y0 + bh, sx0:sx1]
    zone = frame_bgr[y0 : y0 + bh, x0 : x0 + bw]
    if left.size >= 64 and zone.size >= 64:
        lg = cv2.cvtColor(left, cv2.COLOR_BGR2GRAY)
        zg = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
        z_bg = zg[zg < 150]
        if z_bg.size >= 32:
            left_med = float(np.median(lg))
            zone_med = float(np.median(z_bg))
            # Only refuse when left is *darker* (fi=69 dark plate). Brighter
            # left (locker t=15 left~124 vs zone~88) is fine for 鳞片 tile —
            # abs() gate used to force TELEA fog there.
            if (zone_med - left_med) > 15.0:
                return False
    return True


def morphic_hard_seed(
    frame_bgr: Any, box: dict[str, int], static_mask: Any = None
) -> Any:
    """Hard white Morphic glyphs only — no soft residual (eats lockers)."""
    import cv2
    import numpy as np

    x0, y0, bw, bh = _box_xywh(box)
    roi_src = frame_bgr[y0 : y0 + bh, x0 : x0 + bw]
    if roi_src.size < 64:
        return static_mask
    hsv = cv2.cvtColor(roi_src, cv2.COLOR_BGR2HSV)
    g = cv2.cvtColor(roi_src, cv2.COLOR_BGR2GRAY)
    ink = ((hsv[..., 2] > 168) & (hsv[..., 1] < 110)).astype(np.uint8) * 255
    ink |= (g > 175).astype(np.uint8) * 255
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    ink = cv2.dilate(ink, np.ones((3, 3), np.uint8), iterations=1)
    if int(ink.max()) == 0:
        return static_mask
    src_hole = np.zeros(frame_bgr.shape[:2], dtype=np.uint8)
    src_hole[y0 : y0 + bh, x0 : x0 + bw] = ink
    if static_mask is not None:
        src_hole = cv2.bitwise_or(src_hole, static_mask)
    return src_hole


def _below_skin_frac(fr: Any, box: dict[str, int]) -> float:
    """Skin fraction of the band immediately below the box (forehead tear guard)."""
    import cv2
    import numpy as np

    x0, y0, bw, bh = _box_xywh(box)
    bel = fr[
        min(fr.shape[0] - 1, y0 + bh + 2) : min(fr.shape[0], y0 + 2 * bh + 2),
        x0 : x0 + bw,
    ]
    if bel.size < 64:
        return 1.0
    hsv = cv2.cvtColor(bel, cv2.COLOR_BGR2HSV)
    return float(
        (
            (hsv[..., 0] < 25)
            & (hsv[..., 1] > 35)
            & (hsv[..., 2] > 60)
            & (hsv[..., 2] < 220)
        ).mean()
    )


def apply_morphic_frame(
    fr: Any, box: dict[str, int], static_mask: Any = None
) -> tuple[Any, str]:
    """One-frame Morphic wipe. Returns ``(out, 'copy'|'telea')``.

    **鳞片 first** (narrow near-left tile via ``restore_edge_overlay``): keeps
    locker/ceiling grain. The old below→left_full→TELEA escalate flattened
    texture into fog (BV1P6 batch edge≈28 vs v2 tile edge≈230) even when OCR
    reported clean. Luma-mismatch / forehead still refuse copy and use hard-ink
    TELEA only — never a full-ROI soft inpaint plate.
    """
    import cv2
    import numpy as np

    x0, y0, bw, bh = _box_xywh(box)
    spill = min(20, max(0, fr.shape[0] - (y0 + bh)))
    if morphic_use_copy(fr, box):
        out = restore_edge_overlay(fr, box)
        bel = fr[
            min(fr.shape[0] - 1, y0 + bh + 2) : min(
                fr.shape[0], y0 + 2 * bh + 2
            ),
            x0 : x0 + bw,
        ]
        if bel.shape[0] == bh and bel.shape[1] == bw:
            grain = bel.astype(np.float32) - cv2.medianBlur(bel, 5).astype(
                np.float32
            )
            roi = out[y0 : y0 + bh, x0 : x0 + bw].astype(np.float32)
            out[y0 : y0 + bh, x0 : x0 + bw] = np.clip(
                roi + 0.85 * grain, 0, 255
            ).astype(np.uint8)
        seam = np.zeros(fr.shape[:2], dtype=np.uint8)
        seam[y0 : y0 + bh, x0 : min(x0 + 16, x0 + bw)] = 255
        out = cv2.inpaint(out, seam, 6, cv2.INPAINT_TELEA)
        # Reject bad 鳞片 (dark plate / leftover ink) → hard-ink TELEA + LaMa.
        src_roi = fr[y0 : y0 + bh, x0 : x0 + bw]
        met = watermark_roi_dirty(out[y0 : y0 + bh, x0 : x0 + bw], src_roi)
        if float(met.get("dark_rect") or 0) >= 8.0 or float(
            met.get("bright") or 0
        ) >= 0.04:
            seed = morphic_hard_seed(fr, box, static_mask)
            if seed is not None and int(np.asarray(seed).max()) > 0:
                out = cv2.inpaint(fr, seed, 5, cv2.INPAINT_TELEA)
            out = maybe_lama_watermark_finish(out, fr, box)
            return out, "telea"
        branch = "copy"
    else:
        # Forehead / luma-mismatch (fi=69 beam): hard-ink TELEA only.
        out = fr.copy()
        seed = morphic_hard_seed(fr, box, static_mask)
        if seed is not None and int(np.asarray(seed).max()) > 0:
            out = cv2.inpaint(fr, seed, 5, cv2.INPAINT_TELEA)
        out = maybe_lama_watermark_finish(out, fr, box)
        return out, "telea"
    if spill > 0:
        apron = out[y0 + bh : y0 + bh + spill, x0 : x0 + bw]
        if apron.size >= 32:
            hsv = cv2.cvtColor(apron, cv2.COLOR_BGR2HSV)
            ga = cv2.cvtColor(apron, cv2.COLOR_BGR2GRAY)
            hard = (
                ((hsv[..., 2] > 160) & (hsv[..., 1] < 110)) | (ga > 170)
            ).astype(np.uint8) * 255
            hard = cv2.dilate(hard, np.ones((3, 3), np.uint8), iterations=2)
            if float((hard > 0).mean()) >= 0.003:
                full = np.zeros(fr.shape[:2], dtype=np.uint8)
                full[y0 + bh : y0 + bh + spill, x0 : x0 + bw] = hard
                out = cv2.inpaint(out, full, 5, cv2.INPAINT_TELEA)
    return out, branch


def _roi_dark_flat(roi: Any) -> bool:
    """True when ROI is a dark, low-sat plate (safe for full strip neighbor wipe)."""
    import cv2
    import numpy as np

    if roi is None or roi.size < 64:
        return False
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    bg = g[g < 140]
    if bg.size < 32:
        return float(np.median(g)) < 70.0
    return float(np.median(bg)) < 75.0 and float(np.median(hsv[..., 1][g < 140])) < 80.0


def apply_neighbor_watermark_wipe(
    fr: Any,
    box: dict[str, int],
    *,
    direction: str = "right",
    spill: int = 0,
    full_box: bool | None = None,
) -> Any:
    """Wipe corner logo by pasting a clean neighbor strip onto the logo mask.

    Dark flat topL: ``full_box`` right-strip (hard) — LaMa/TELEA leave fog.
    Morphic on lit curtain: mask-only ``below`` paste — left-stretch makes dark rect.
    """
    import cv2
    import numpy as np

    x0, y0, bw, bh = _box_xywh(box)
    y1 = min(fr.shape[0], y0 + bh + max(0, int(spill)))
    x1 = min(fr.shape[1], x0 + bw)
    rh, rw = y1 - y0, x1 - x0
    if rh < 4 or rw < 4:
        return fr
    roi = fr[y0:y1, x0:x1]
    use_full = _roi_dark_flat(roi) if full_box is None else bool(full_box)
    if use_full:
        m = np.full((rh, rw), 255, dtype=np.uint8)
    else:
        m = full_up_watermark_mask(fr, {"x": x0, "y": y0, "w": rw, "h": rh})
        m = m[y0:y1, x0:x1]
        m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=2)
    if int(m.max()) == 0:
        return fr
    if direction == "right":
        sx0 = min(fr.shape[1] - rw, x1 + 4)
        donor = fr[y0:y1, sx0 : sx0 + rw]
        if donor.shape[1] != rw:
            donor = cv2.resize(donor, (rw, rh), interpolation=cv2.INTER_LINEAR)
    elif direction == "below":
        sy0 = min(fr.shape[0] - rh, y1 + 4)
        donor = fr[sy0 : sy0 + rh, x0:x1]
        if donor.shape[0] != rh:
            donor = cv2.resize(donor, (rw, rh), interpolation=cv2.INTER_LINEAR)
    elif direction == "left":
        sx1 = max(rw, x0 - 4)
        sx0 = max(0, sx1 - rw)
        donor = fr[y0:y1, sx0:sx1]
        if donor.shape[1] != rw:
            donor = cv2.resize(donor, (rw, rh), interpolation=cv2.INTER_LINEAR)
    else:
        raise ValueError(direction)
    hsv = cv2.cvtColor(donor, cv2.COLOR_BGR2HSV)
    dg = cv2.cvtColor(donor, cv2.COLOR_BGR2GRAY)
    bad = ((hsv[..., 2] > 135) & (hsv[..., 1] < 110)) | (dg > 155)
    if np.any(bad) and np.any(~bad):
        med = np.median(donor[~bad].reshape(-1, 3), axis=0)
        donor = donor.copy()
        donor[bad] = med.astype(np.uint8)
    if use_full:
        # Hard replace; feather only the interior edge away from frame border.
        out = fr.copy()
        out[y0:y1, x0:x1] = donor
        feather = 8
        if direction == "right" and x1 + 1 < fr.shape[1]:
            for i in range(feather):
                a = (i + 1) / (feather + 1)
                xx = x1 - feather + i
                if xx < x0:
                    continue
                out[y0:y1, xx] = (
                    donor[:, rw - feather + i].astype(np.float32) * (1 - a)
                    + fr[y0:y1, xx].astype(np.float32) * a
                ).astype(np.uint8)
        for i in range(min(feather, rh)):
            a = (i + 1) / (feather + 1)
            yy = y1 - feather + i
            if yy < y0 or yy >= fr.shape[0]:
                continue
            out[yy, x0:x1] = (
                out[yy, x0:x1].astype(np.float32) * (1 - a)
                + fr[yy, x0:x1].astype(np.float32) * a
            ).astype(np.uint8)
        # Scrub any leftover bright ink (bilibili antenna stubs).
        patch = out[y0:y1, x0:x1]
        hsv2 = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        g2 = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        bright = (
            ((hsv2[..., 2] > 120) & (hsv2[..., 1] < 120)) | (g2 > 130)
        ).astype(np.uint8) * 255
        bright = cv2.dilate(bright, np.ones((3, 3), np.uint8), iterations=2)
        if int(bright.max()) > 0 and float((bright > 0).mean()) < 0.25:
            dark = patch[(g2 < 90) & (hsv2[..., 1] < 100)]
            if dark.size >= 16:
                med = np.median(dark.reshape(-1, 3), axis=0)
                yy, xx = np.where(bright > 0)
                rng = np.random.default_rng((x0 * 131 + y0 * 17) & 0xFFFFFFFF)
                noise = rng.normal(0.0, 2.0, size=(len(yy), 3))
                patch = patch.copy()
                patch[yy, xx] = np.clip(med + noise, 0, 255).astype(np.uint8)
                out[y0:y1, x0:x1] = patch
        return out
    soft = cv2.GaussianBlur(m, (7, 7), 0).astype(np.float32) / 255.0
    core = cv2.erode(m, np.ones((3, 3), np.uint8), iterations=1)
    soft = np.maximum(soft, (core > 0).astype(np.float32))
    blend = (
        roi.astype(np.float32) * (1.0 - soft[..., None])
        + donor.astype(np.float32) * soft[..., None]
    )
    out = fr.copy()
    out[y0:y1, x0:x1] = np.clip(blend, 0, 255).astype(np.uint8)
    return out


_LAMA_LOCK = None


def _lama_lock():
    """Serialize LaMa across parallel watermark workers (GPU/session unsafe)."""
    global _LAMA_LOCK
    if _LAMA_LOCK is None:
        import threading

        _LAMA_LOCK = threading.Lock()
    return _LAMA_LOCK


def maybe_lama_watermark_finish(out: Any, src_fr: Any, box: dict[str, int]) -> Any:
    """If corner wipe still looks dirty, re-wipe from SOURCE with full-logo LaMa.

    Important: mask + inpaint must start from ``src_fr``. Running LaMa on an
    already-smeared copyfill frame cannot see original glyphs, so the fog bar
    stays (BV1P6 fi=69 topL).
    """
    x0, y0, bw, bh = _box_xywh(box)
    try:
        from inpaint_lama import LAMA_WEIGHTS, lama_inpaint

        met = watermark_roi_dirty(
            out[y0 : y0 + bh, x0 : x0 + bw],
            src_fr[y0 : y0 + bh, x0 : x0 + bw],
        )
        if not LAMA_WEIGHTS.is_file():
            return out
        if not (met.get("dirty") or float(met.get("ghost") or 0) >= 0.03):
            return out
        seed = full_up_watermark_mask(src_fr, box)
        if int(seed.max()) == 0:
            return out
        with _lama_lock():
            return lama_inpaint(src_fr, seed)
    except Exception:
        return out


def apply_copyfill_hard_residual(
    out: Any, box: dict[str, int], static_mask: Any = None
) -> Any:
    """Ink + hot-pixel residual after top-left copyfill (Poootaaaato/bilibili fog).

    Restrict to logo-ink / local-hot masks — the old ``gl > med+18`` alone carved
    letter-shaped skeletons on fi=69 instead of clearing fog.
    """
    import cv2
    import numpy as np

    x0, y0, bw, bh = _box_xywh(box)
    roi_l = out[y0 : y0 + bh, x0 : x0 + bw]
    if roi_l.size < 64:
        return out
    hsv_l = cv2.cvtColor(roi_l, cv2.COLOR_BGR2HSV)
    gl = cv2.cvtColor(roi_l, cv2.COLOR_BGR2GRAY)
    hard_l = (
        ((hsv_l[..., 2] > 160) & (hsv_l[..., 1] < 115)) | (gl > 165)
    ).astype(np.uint8) * 255
    med = cv2.medianBlur(gl, 15)
    hard_l |= ((gl > med + 25) & (gl > 120)).astype(np.uint8) * 255
    # Gate with soft ink so structure edges / shadows are not TELEA'd into ghosts.
    ink = _logo_ink_mask_roi(roi_l)
    if static_mask is not None:
        sm = static_mask[y0 : y0 + bh, x0 : x0 + bw]
        if sm.shape[:2] == hard_l.shape[:2]:
            ink = cv2.bitwise_or(ink, sm)
    # Hot pixels may sit outside soft ink after a partial edge paste — keep them.
    hard_l = cv2.bitwise_or(
        cv2.bitwise_and(hard_l, ink),
        ((gl > med + 30) & (gl > 140)).astype(np.uint8) * 255,
    )
    hard_l = cv2.morphologyEx(hard_l, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    hard_l = cv2.dilate(hard_l, np.ones((3, 3), np.uint8), iterations=1)
    if int(hard_l.max()) > 0 and float((hard_l > 0).mean()) < 0.35:
        full_l = np.zeros(out.shape[:2], dtype=np.uint8)
        full_l[y0 : y0 + bh, x0 : x0 + bw] = hard_l
        out = cv2.inpaint(out, full_l, 4, cv2.INPAINT_TELEA)
    return out


def encode_static_boxfill(
    src_video: Path,
    dest_video: Path,
    box: dict[str, int],
    *,
    role: str = "title",
    method: str = "auto",
) -> dict[str, Any]:
    """Encode a full-video pass that removes one static overlay box each frame.

    ``method``:
      - ``auto``: edge-attached up_watermark/title → neighbour copyfill (avoids
        TELEA grey smear / mosaic look); otherwise TELEA box inpaint.
      - ``copyfill`` / ``telea``: force one path.
    """
    import cv2
    import numpy as np
    from _inpaint_shared import _open_ffmpeg_writer

    src_video = Path(src_video)
    dest_video = Path(dest_video)
    box_run = {k: int(box[k]) for k in ("x", "y", "w", "h")}
    role_s = (role or "").strip().lower()
    method_s = (method or "auto").strip().lower()
    wh = _media().probe_video_wh(src_video)
    if wh is None:
        raise RuntimeError(f"cannot probe {src_video}")
    width, height = int(wh[0]), int(wh[1])
    if method_s == "auto":
        # D7+D12: same gate as hybrid (incl. motion for top strip).
        if role_s == "up_watermark":
            motion = top_strip_motion_score(src_video, box_run)
            method_s = resolve_up_watermark_method(
                box_run, width, height, motion=motion
            )
        elif role_s == "watermark" or (
            role_s == "title" and int(box_run.get("y") or 0) <= 4
        ):
            method_s = "copyfill"
        else:
            method_s = "telea"
    static_mask = None
    per_frame_logo = False
    if method_s in ("logo", "morphic"):
        static_mask = build_static_logo_mask(src_video, box_run)
        if static_mask is None and method_s == "logo":
            # Mid @handle: per-frame thin ink (v2 cleared these; skip = leave text).
            per_frame_logo = True
            print(
                f"[cleanup] logo per-frame ink (no static) box={box_run}",
                flush=True,
            )
    cap = cv2.VideoCapture(str(src_video))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src_video} for static boxfill")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    n_hint = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    proc = _open_ffmpeg_writer(src_video, dest_video, width, height, fps)
    assert proc.stdin is not None
    frames = 0
    restored = 0  # half-wipe rollback to source ROI (D9)
    morphic_copy = 0
    morphic_telea = 0
    tag = {
        "copyfill": "copyfill",
        "logo": "logo",
        "morphic": "morphic",
        "adaptive_lama": "adaptive_lama",
        "telea": "boxfill",
    }.get(method_s, "boxfill")
    x0, y0, bw, bh = _box_xywh(box_run)

    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            used_copyfill = False
            if method_s == "copyfill":
                # Edge copy only when below-band is clearly non-face (locker t=7).
                # Face close-ups (t=1): edge paste tears forehead into the bar.
                below = fr[
                    min(fr.shape[0] - 1, y0 + bh + 2) : min(
                        fr.shape[0], y0 + 2 * bh + 2
                    ),
                    x0 : x0 + bw,
                ]
                below_skin = 1.0
                if below.size >= 64:
                    hsv_b = cv2.cvtColor(below, cv2.COLOR_BGR2HSV)
                    below_skin = float(
                        (
                            (hsv_b[..., 0] < 25)
                            & (hsv_b[..., 1] > 35)
                            & (hsv_b[..., 2] > 60)
                            & (hsv_b[..., 2] < 220)
                        ).mean()
                    )
                # Dark flat topL (fi=69 Poootaaaato): hard RIGHT strip wipe.
                # Edge TELEA/LaMa leave fog bars; neighbor hard paste is clean.
                roi0 = fr[y0 : y0 + bh, x0 : x0 + bw]
                if _roi_dark_flat(roi0) and x0 <= int(fr.shape[1] * 0.35):
                    # Keep h≈logo strip only — bh+20 ate hairline (fi=69 blob).
                    tall = {
                        "x": max(0, x0 - 4),
                        "y": y0,
                        "w": min(fr.shape[1] - max(0, x0 - 4), max(bw + 40, 360)),
                        "h": min(fr.shape[0] - y0, bh + 6),
                    }
                    out = apply_neighbor_watermark_wipe(
                        fr, tall, direction="right", spill=0, full_box=True
                    )
                elif below_skin < 0.12:
                    out = restore_edge_overlay(fr, box_run)
                    seed = _logo_ink_mask_roi(out[y0 : y0 + bh, x0 : x0 + bw])
                    if float((seed > 0).mean()) >= 0.008:
                        seed = cv2.dilate(
                            seed, np.ones((3, 3), np.uint8), iterations=1
                        )
                        full = np.zeros(fr.shape[:2], dtype=np.uint8)
                        full[y0 : y0 + bh, x0 : x0 + bw] = seed
                        out = restore_logo_pixels(
                            out, box_run, mask=full, residual_copyfill=False
                        )
                    out = apply_copyfill_hard_residual(out, box_run, static_mask)
                    out = maybe_lama_watermark_finish(out, fr, box_run)
                else:
                    out = restore_logo_pixels(
                        fr, box_run, mask=static_mask, residual_copyfill=False
                    )
                    out = apply_copyfill_hard_residual(out, box_run, static_mask)
                    out = maybe_lama_watermark_finish(out, fr, box_run)
                morphic_telea += 1
                used_copyfill = True
            elif method_s == "morphic":
                out, branch = apply_morphic_frame(fr, box_run, static_mask)
                if branch == "copy":
                    morphic_copy += 1
                else:
                    morphic_telea += 1
                used_copyfill = True
            elif method_s == "adaptive_lama":
                out = adaptive_lama_watermark_wipe(fr, box_run)
            elif method_s == "logo":
                # Static consensus when available; else per-frame thin ink.
                # Skip a frame only if ink covers too much ROI (face highlight).
                use_mask = None if per_frame_logo else static_mask
                if per_frame_logo:
                    roi = fr[y0 : y0 + bh, x0 : x0 + bw]
                    if roi.size >= 64:
                        ink = _logo_ink_mask_roi(roi)
                        if float((ink > 0).mean()) > 0.32:
                            out = fr
                        else:
                            out = restore_logo_pixels(
                                fr, box_run, mask=None, residual_copyfill=False
                            )
                    else:
                        out = fr
                else:
                    out = restore_logo_pixels(
                        fr, box_run, mask=use_mask, residual_copyfill=True
                    )
            else:
                out = restore_static_overlay_box(fr, box_run)
            # D9: soft-ink half-wipe → leave source (readable mark > grey smear).
            # Skip after strip copyfill — darkening white Morphic is intentional;
            # rollback was re-leaving Morphic on BV1P6 (~588/1149 frames).
            if (
                method_s in ("logo", "morphic", "telea")
                and not used_copyfill
                and bw > 2
                and bh > 2
            ):
                src_roi = fr[y0 : y0 + bh, x0 : x0 + bw]
                out_roi = out[y0 : y0 + bh, x0 : x0 + bw]
                if _roi_half_wipe(src_roi, out_roi):
                    out = fr.copy()
                    restored += 1
            proc.stdin.write(np.ascontiguousarray(out).tobytes())
            frames += 1
            if n_hint and frames % 60 == 0:
                print(f"[{tag}:{role}] wrote {frames}/{n_hint}", flush=True)
    except Exception:
        proc.kill()
        raise
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    stderr = proc.communicate(timeout=300)[1]
    if proc.returncode != 0 or frames < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"static boxfill failed ({proc.returncode}): {err}")
    if restored:
        print(
            f"[cleanup] {method_s} half-wipe rollback {restored}/{frames} "
            f"frames box={box_run}",
            flush=True,
        )
    if method_s == "morphic":
        print(
            f"[cleanup] morphic adaptive copy={morphic_copy} telea={morphic_telea} "
            f"box={box_run}",
            flush=True,
        )
    engine = {
        "copyfill": "copyfill",
        "logo": "logo_telea",
        "morphic": "morphic_adaptive",
        "telea": "boxfill",
    }.get(method_s, "boxfill")
    if method_s == "logo" and per_frame_logo:
        mode = "per_frame_ink"
    elif method_s == "morphic":
        mode = "ink_telea"
    elif method_s == "copyfill":
        mode = "ink_telea"
    else:
        mode = {
            "logo": "static_logo_mask",
            "telea": "box_inpaint",
        }.get(method_s, "box_inpaint")
    return {
        "engine": engine,
        "mode": mode,
        "frames": frames,
        "restored": restored,
        "morphic_copy": morphic_copy if method_s == "morphic" else None,
        "morphic_telea": morphic_telea if method_s == "morphic" else None,
        "bytes": dest_video.stat().st_size if dest_video.is_file() else 0,
        "role": role,
        "box": box_run,
    }


def rework_up_watermark_roi(
    out: Any,
    src_fr: Any,
    box: dict[str, int],
    *,
    method: str = "auto",
    static_mask: Any = None,
) -> tuple[Any, str]:
    """Stronger wipe from SOURCE when first-pass ROI still looks dirty.

    Order: alternate neighbor → full-mask TELEA → LaMa finish. Always seed
    from ``src_fr`` so smeared first-pass fog does not poison the mask.
    """
    import cv2
    import numpy as np

    x0, y0, bw, bh = _box_xywh(box)
    method_s = (method or "auto").strip().lower()
    roi0 = src_fr[y0 : y0 + bh, x0 : x0 + bw]
    # FILL_V2 顶栏返工：同样用 NS 扩散，别让 neighbor/TELEA 把 NS 结果覆盖
    if method_s == "ns_box":
        return ns_box_wipe(src_fr, box), "ns_box"
    if method_s == "adaptive_lama":
        return adaptive_lama_watermark_wipe(src_fr, box), "adaptive_lama"
    # 1) Prefer hard neighbor paste when BG is flat / left-edge logo.
    if _roi_dark_flat(roi0) or method_s == "copyfill":
        tall = {
            "x": max(0, x0 - 4),
            "y": y0,
            "w": min(src_fr.shape[1] - max(0, x0 - 4), max(bw + 40, 360)),
            "h": min(src_fr.shape[0] - y0, bh + 6),
        }
        cand = apply_neighbor_watermark_wipe(
            src_fr, tall, direction="right", spill=0, full_box=True
        )
        met = watermark_roi_dirty(
            cand[y0 : y0 + bh, x0 : x0 + bw], roi0
        )
        if watermark_roi_accept(met):
            return cand, "neighbor_right"
    if _below_skin_frac(src_fr, box) < 0.22:
        cover_h = min(src_fr.shape[0] - y0, bh + 16)
        for full_box, tag in ((False, "neighbor_below"), (True, "neighbor_below_full")):
            cand = apply_neighbor_watermark_wipe(
                src_fr,
                {"x": x0, "y": y0, "w": bw, "h": cover_h},
                direction="below",
                spill=0,
                full_box=full_box,
            )
            met = watermark_roi_dirty(
                cand[y0 : y0 + bh, x0 : x0 + bw], roi0
            )
            if watermark_roi_accept(met):
                return cand, tag
    # Right-corner Morphic: 鳞片 tile (narrow left stretch) before full-left
    # paste / TELEA fog. Matches v2 perceptual quality on locker tops.
    if x0 >= int(src_fr.shape[1] * 0.45) or method_s == "morphic":
        if morphic_use_copy(src_fr, box) or method_s == "morphic":
            cand, _br = apply_morphic_frame(src_fr, box, static_mask)
            met = watermark_roi_dirty(
                cand[y0 : y0 + bh, x0 : x0 + bw], roi0
            )
            if watermark_roi_accept(met) or float(met.get("leftover") or 1) < 0.25:
                return cand, "morphic_scale"
        cand = apply_neighbor_watermark_wipe(
            src_fr, box, direction="left", spill=0, full_box=True
        )
        met = watermark_roi_dirty(
            cand[y0 : y0 + bh, x0 : x0 + bw], roi0
        )
        if watermark_roi_accept(met):
            return cand, "neighbor_left_full"
    # 2) Full-logo TELEA from source (clears soft AA fringe metrics miss).
    seed = full_up_watermark_mask(src_fr, box)
    if static_mask is not None:
        seed = cv2.bitwise_or(seed, static_mask)
    if int(seed.max()) > 0:
        cand = cv2.inpaint(src_fr, seed, 5, cv2.INPAINT_TELEA)
        cand = apply_copyfill_hard_residual(cand, box, static_mask)
        met = watermark_roi_dirty(
            cand[y0 : y0 + bh, x0 : x0 + bw], roi0
        )
        if watermark_roi_accept(met):
            return cand, "telea_full"
        # 3) LaMa from source as last resort.
        finished = maybe_lama_watermark_finish(cand, src_fr, box)
        met2 = watermark_roi_dirty(
            finished[y0 : y0 + bh, x0 : x0 + bw], roi0
        )
        if watermark_roi_accept(met2) or float(met2.get("leftover") or 1) < float(
            met.get("leftover") or 1
        ):
            return finished, "lama_finish"
        # Keep TELEA over a dirtier LaMa.
        return cand, "telea_keep"
    # Fallback: keep first-pass out but try LaMa on it from source.
    finished = maybe_lama_watermark_finish(out, src_fr, box)
    return finished, "lama_only"


def _paste_box_roi(
    base: Any, wiped: Any, box: dict[str, int], *, pad: int = 2, pad_b: int = 0
) -> Any:
    """Paste only the watermark ROI from ``wiped`` onto ``base``."""
    x0, y0, bw, bh = _box_xywh(box)
    xa = max(0, x0 - pad)
    ya = max(0, y0 - pad)
    xb = min(base.shape[1], x0 + bw + pad)
    yb = min(base.shape[0], y0 + bh + max(pad, pad_b))
    out = base.copy()
    out[ya:yb, xa:xb] = wiped[ya:yb, xa:xb]
    return out


def apply_up_watermark_job_frame(
    base: Any,
    src_fr: Any,
    job: dict[str, Any],
) -> tuple[Any, dict[str, int]]:
    """Apply one watermark job onto ``base``; seed masks from ``src_fr``.

    Never assign the whole frame to ``src_fr`` — a later mid-@ skip used to
    resurrect Morphic after the corner wipe (BV1P6).
    """
    import cv2
    import numpy as np

    method_s = job["method"]
    box_run = job["box"]
    static_mask = job.get("mask")
    x0, y0, bw, bh = _box_xywh(box_run)
    counters = {"morphic_copy": 0, "morphic_telea": 0, "restored": 0}
    used_copyfill = False
    pad_b = 0
    fr = src_fr
    if method_s == "ns_box":
        # FILL_V2 顶栏：整框 NS 扩散 + 羽化，非生成不重建字
        wiped = ns_box_wipe(fr, box_run)
        return _paste_box_roi(base, wiped, box_run, pad=8), counters
    if method_s == "adaptive_lama":
        # 中景半透明 @：逐帧残差 mask + LaMa（首帧精修策略）
        wiped = adaptive_lama_watermark_wipe(fr, box_run)
        return _paste_box_roi(base, wiped, box_run, pad=6), counters
    if method_s == "copyfill":
        below = fr[
            min(fr.shape[0] - 1, y0 + bh + 2) : min(
                fr.shape[0], y0 + 2 * bh + 2
            ),
            x0 : x0 + bw,
        ]
        below_skin = 1.0
        if below.size >= 64:
            hsv_b = cv2.cvtColor(below, cv2.COLOR_BGR2HSV)
            below_skin = float(
                (
                    (hsv_b[..., 0] < 25)
                    & (hsv_b[..., 1] > 35)
                    & (hsv_b[..., 2] > 60)
                    & (hsv_b[..., 2] < 220)
                ).mean()
            )
        roi0 = fr[y0 : y0 + bh, x0 : x0 + bw]
        if _roi_dark_flat(roi0) and x0 <= int(fr.shape[1] * 0.35):
            tall = {
                "x": max(0, x0 - 4),
                "y": y0,
                "w": min(fr.shape[1] - max(0, x0 - 4), max(bw + 40, 360)),
                "h": min(fr.shape[0] - y0, bh + 6),
            }
            wiped = apply_neighbor_watermark_wipe(
                fr, tall, direction="right", spill=0, full_box=True
            )
            return _paste_box_roi(base, wiped, tall, pad=2), counters
        if below_skin < 0.12:
            wiped = restore_edge_overlay(fr, box_run)
            seed = _logo_ink_mask_roi(wiped[y0 : y0 + bh, x0 : x0 + bw])
            if float((seed > 0).mean()) >= 0.008:
                seed = cv2.dilate(
                    seed, np.ones((3, 3), np.uint8), iterations=1
                )
                full = np.zeros(fr.shape[:2], dtype=np.uint8)
                full[y0 : y0 + bh, x0 : x0 + bw] = seed
                wiped = restore_logo_pixels(
                    wiped, box_run, mask=full, residual_copyfill=False
                )
            wiped = apply_copyfill_hard_residual(wiped, box_run, static_mask)
            wiped = maybe_lama_watermark_finish(wiped, fr, box_run)
        else:
            wiped = restore_logo_pixels(
                fr, box_run, mask=static_mask, residual_copyfill=False
            )
            wiped = apply_copyfill_hard_residual(wiped, box_run, static_mask)
            wiped = maybe_lama_watermark_finish(wiped, fr, box_run)
        used_copyfill = True
    elif method_s == "morphic":
        wiped, branch = apply_morphic_frame(fr, box_run, static_mask)
        if branch == "copy":
            counters["morphic_copy"] = 1
        else:
            counters["morphic_telea"] = 1
        pad_b = 16 if str(branch).startswith("below") else 0
        used_copyfill = True
    elif method_s == "logo":
        use_mask = None if job.get("per_frame_logo") else static_mask
        if job.get("per_frame_logo"):
            roi = fr[y0 : y0 + bh, x0 : x0 + bw]
            if roi.size < 64:
                return base, counters
            ink = _logo_ink_mask_roi(roi)
            # Dense bright ROI (medals) ≠ logo — skip this box only.
            if float((ink > 0).mean()) > 0.32:
                return base, counters
            wiped = restore_logo_pixels(
                fr, box_run, mask=None, residual_copyfill=False
            )
        else:
            wiped = restore_logo_pixels(
                fr, box_run, mask=use_mask, residual_copyfill=True
            )
    else:
        wiped = restore_static_overlay_box(fr, box_run)
    if (
        method_s in ("logo", "morphic", "telea")
        and not used_copyfill
        and bw > 2
        and bh > 2
    ):
        src_roi = fr[y0 : y0 + bh, x0 : x0 + bw]
        out_roi = wiped[y0 : y0 + bh, x0 : x0 + bw]
        if _roi_half_wipe(src_roi, out_roi):
            counters["restored"] = 1
            return _paste_box_roi(base, fr, box_run, pad=0), counters
    return _paste_box_roi(base, wiped, box_run, pad=2, pad_b=pad_b), counters


def process_up_watermark_frame_qa(
    src_fr: Any,
    prepared: list[dict[str, Any]],
    *,
    qa_rework: bool = True,
) -> tuple[Any, dict[str, Any]]:
    """Wipe all watermark jobs on one frame, then metric-verify + rework.

    Each job pastes only its ROI onto the composite so a later mid-handle
    skip cannot resurrect Morphic.
    """
    out = src_fr.copy()
    stats = {
        "rework": 0,
        "still_dirty": 0,
        "morphic_copy": 0,
        "morphic_telea": 0,
        "restored": 0,
        "rework_paths": [],
    }
    jobs = sorted(
        prepared, key=lambda j: int((j.get("box") or {}).get("x") or 0)
    )
    for job in jobs:
        out, ctr = apply_up_watermark_job_frame(out, src_fr, job)
        stats["morphic_copy"] += int(ctr.get("morphic_copy") or 0)
        stats["morphic_telea"] += int(ctr.get("morphic_telea") or 0)
        stats["restored"] += int(ctr.get("restored") or 0)
        if not qa_rework:
            continue
        box = job["box"]
        x0, y0, bw, bh = _box_xywh(box)
        met = watermark_roi_dirty(
            out[y0 : y0 + bh, x0 : x0 + bw],
            src_fr[y0 : y0 + bh, x0 : x0 + bw],
        )
        if watermark_roi_accept(met):
            continue
        method_j = str(job.get("method") or "")
        # 鳞片/copyfill changes texture vs src → leftover trips dirty; do not
        # rework into TELEA fog when bright ink is already gone (v2 look).
        if method_j in ("morphic", "copyfill"):
            if float(met.get("bright") or 0) < 0.012 and float(
                met.get("ghost") or 0
            ) < 0.12:
                continue
        # Mid @handle: body highlights trip bright-only gates — rework only
        # when ghost/dark-rect say ink or smear remains.
        if method_j in ("logo", "adaptive_lama"):
            if float(met.get("ghost") or 0) < 0.04 and float(
                met.get("dark_rect") or 0
            ) < 8.0:
                continue
        # ns_box / adaptive_lama same-seed rework is a no-op — escalate to
        # morphic/neighbor when bright ink or ghost remains (BV1P6 Morphic).
        rework_method = method_j or "auto"
        if method_j == "ns_box":
            if float(met.get("bright") or 0) < 0.008 and float(
                met.get("leftover") or 0
            ) < 0.20:
                continue
            rework_method = "morphic"
        elif method_j == "adaptive_lama":
            if float(met.get("bright") or 0) < 0.008 and float(
                met.get("ghost") or 0
            ) < 0.06:
                continue
            rework_method = "morphic"
        reworked, path = rework_up_watermark_roi(
            out,
            src_fr,
            box,
            method=rework_method,
            static_mask=job.get("mask"),
        )
        pad = 8 if str(path).startswith("neighbor") else 2
        pad_b = (
            16
            if path in ("neighbor_below", "neighbor_below_full")
            else pad
        )
        out = _paste_box_roi(out, reworked, box, pad=pad, pad_b=pad_b)
        stats["rework"] += 1
        stats["rework_paths"].append(path)
        met2 = watermark_roi_dirty(
            out[y0 : y0 + bh, x0 : x0 + bw],
            src_fr[y0 : y0 + bh, x0 : x0 + bw],
        )
        if not watermark_roi_accept(met2):
            stats["still_dirty"] += 1
    return out, stats


def encode_multi_static_boxfill(
    src_video: Path,
    dest_video: Path,
    jobs: list[dict[str, Any]],
    *,
    qa_rework: bool | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """One encode pass applying several up_watermark boxfills (left→right).

    Cuts re-encode mush vs calling ``encode_static_boxfill`` once per box.
    Each job: ``{box, method, role?}``.

    Every frame is metric-verified after wipe; dirty ROIs are reworked from
    source. Frame batches run in a thread pool (``workers``).
    """
    import cv2
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor
    from _inpaint_shared import _open_ffmpeg_writer

    if not jobs:
        raise ValueError("encode_multi_static_boxfill: empty jobs")

    if qa_rework is None:
        qa_rework = _as_bool(os.environ.get("VITUAL_DEHARDSUB_WM_FRAME_QA"), True)
    if workers is None:
        workers = max(
            1, int(os.environ.get("VITUAL_DEHARDSUB_WM_QA_WORKERS") or 4)
        )
    batch_n = max(workers * 2, 8)

    src_video = Path(src_video)
    dest_video = Path(dest_video)
    wh = _media().probe_video_wh(src_video)
    if wh is None:
        raise RuntimeError(f"cannot probe {src_video}")
    width, height = int(wh[0]), int(wh[1])

    prepared: list[dict[str, Any]] = []
    for j in jobs:
        box_run = {k: int(j["box"][k]) for k in ("x", "y", "w", "h")}
        role_s = str(j.get("role") or "up_watermark").strip().lower()
        method_s = str(j.get("method") or "auto").strip().lower()
        if method_s == "auto":
            motion = top_strip_motion_score(src_video, box_run)
            method_s = resolve_up_watermark_method(
                box_run, width, height, motion=motion
            )
        static_mask = None
        per_frame_logo = False
        if method_s in ("logo", "morphic"):
            static_mask = build_static_logo_mask(src_video, box_run)
            if static_mask is None and method_s == "logo":
                per_frame_logo = True
        prepared.append(
            {
                "box": box_run,
                "role": role_s,
                "method": method_s,
                "mask": static_mask,
                "per_frame_logo": per_frame_logo,
                "morphic_copy": 0,
                "morphic_telea": 0,
                "restored": 0,
                "rework": 0,
                "still_dirty": 0,
            }
        )

    cap = cv2.VideoCapture(str(src_video))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src_video} for multi boxfill")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    n_hint = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    proc = _open_ffmpeg_writer(src_video, dest_video, width, height, fps)
    assert proc.stdin is not None
    frames = 0
    total_rework = 0
    total_still = 0
    total_morphic_copy = 0
    total_morphic_telea = 0
    total_restored = 0
    print(
        f"[multiboxfill] frame_qa={qa_rework} workers={workers} "
        f"batch={batch_n} jobs={len(prepared)}",
        flush=True,
    )

    def _one(fr: Any) -> tuple[Any, dict[str, Any]]:
        return process_up_watermark_frame_qa(
            fr, prepared, qa_rework=bool(qa_rework)
        )

    try:
        while True:
            batch: list[Any] = []
            for _ in range(batch_n):
                ok, fr = cap.read()
                if not ok:
                    break
                batch.append(fr)
            if not batch:
                break
            if workers <= 1 or len(batch) == 1:
                results = [_one(fr) for fr in batch]
            else:
                with ThreadPoolExecutor(
                    max_workers=min(workers, len(batch))
                ) as pool:
                    results = list(pool.map(_one, batch))
            for out, st in results:
                total_rework += int(st.get("rework") or 0)
                total_still += int(st.get("still_dirty") or 0)
                total_morphic_copy += int(st.get("morphic_copy") or 0)
                total_morphic_telea += int(st.get("morphic_telea") or 0)
                total_restored += int(st.get("restored") or 0)
                proc.stdin.write(np.ascontiguousarray(out).tobytes())
                frames += 1
                if n_hint and frames % 60 == 0:
                    print(
                        f"[multiboxfill] wrote {frames}/{n_hint} "
                        f"rework={total_rework} still_dirty={total_still}",
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
    if proc.returncode != 0 or frames < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"multi boxfill failed ({proc.returncode}): {err}")
    methods = [p["method"] for p in prepared]
    print(
        f"[cleanup] multi up_watermark n={len(prepared)} methods={methods} "
        f"frames={frames} rework={total_rework} still_dirty={total_still} "
        f"qa={qa_rework} workers={workers}",
        flush=True,
    )
    return {
        "engine": "multi_boxfill",
        "mode": "+".join(methods),
        "frames": frames,
        "rework": total_rework,
        "still_dirty": total_still,
        "qa_rework": bool(qa_rework),
        "workers": int(workers),
        "jobs": [
            {
                "method": p["method"],
                "box": p["box"],
                "morphic_copy": total_morphic_copy if p["method"] == "morphic" else None,
                "morphic_telea": total_morphic_telea if p["method"] == "morphic" else None,
                "restored": total_restored,
            }
            for p in prepared
        ],
        "bytes": dest_video.stat().st_size if dest_video.is_file() else 0,
        "role": "up_watermark",
    }


def up_watermark_prefers_boxfill(
    box: dict[str, int], frame_h: int, frame_w: int
) -> bool:
    """Top-edge thin strips are usually static logos — OCR often misses them."""
    y0 = int(box.get("y") or 0)
    h = int(box.get("h") or 0)
    w = int(box.get("w") or 0)
    if frame_h <= 0 or frame_w <= 0 or h <= 0 or w <= 0:
        return False
    # Top strip logos — allow post-expand height up to ~18% (pad can push
    # Morphic from h=38 → ~64, which used to miss this gate and fall into OCR).
    if y0 <= max(4, int(round(frame_h * 0.12))) and h <= max(40, int(round(frame_h * 0.18))):
        return True
    # Mid-frame @handles: do NOT prefer logo_telea — bright-pixel fill eats
    # medals/highlights on anime bodies. Leave them to OCR thin-mask.
    return False


def dialogue_plate_is_safe(
    video: Path,
    box: dict[str, int],
    plate: Any,
    *,
    max_darker: float = 28.0,
    samples: int = 7,
) -> tuple[bool, dict[str, float]]:
    """Reject a median plate that would stamp a dark bar over a cleaned band.

    Compares plate mean luma to the post-TELEA (or current) video ROI. A plate
    much darker than the live band is typical of hardsub-on-scene median fail
    (non-UI caption), not a Douyin-style translucent input bar.
    """
    import cv2
    import numpy as np

    video = Path(video)
    x0 = int(box["x"])
    y0 = int(box["y"])
    w = int(box["w"])
    h = int(box["h"])
    if plate is None or w <= 2 or h <= 2:
        return False, {"reason": 0.0}
    pg = cv2.cvtColor(
        np.clip(plate, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY
    ).astype(np.float32)
    plate_mean = float(pg.mean())
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return False, {"plate_mean": plate_mean, "video_mean": -1.0}
    try:
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if n < 2:
            return False, {"plate_mean": plate_mean, "video_mean": -1.0}
        idxs = np.linspace(0, max(0, n - 1), int(min(samples, n))).astype(int)
        vals: list[float] = []
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
            ok, fr = cap.read()
            if not ok:
                continue
            roi = fr[y0 : y0 + h, x0 : x0 + w]
            if roi.size == 0:
                continue
            vals.append(float(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).mean()))
        if not vals:
            return False, {"plate_mean": plate_mean, "video_mean": -1.0}
        video_mean = float(np.mean(vals))
        darker = video_mean - plate_mean
        ok = darker <= float(max_darker)
        return ok, {
            "plate_mean": round(plate_mean, 2),
            "video_mean": round(video_mean, 2),
            "darker": round(darker, 2),
            "max_darker": float(max_darker),
        }
    finally:
        cap.release()


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


def _window_coverage(windows: list[tuple[float, float]]) -> float:
    return sum(max(0.0, float(end) - float(start)) for start, end in windows)



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
    # D13: temp/`_*` near-lossless; final deliverable crf18.
    _crf = "12" if dest.name.startswith("_") else "18"
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            _crf,
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


def residual_ghost_mask(
    bgr: Any,
    box: dict[str, int],
    *,
    thr: float = 14.0,
) -> Any:
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


def build_dialogue_clean_plate(
    video: Path,
    box: dict[str, int],
    *,
    k: int = 25,
    white_thr: float = 185.0,
    sat_thr: float = 70.0,
    max_temporal_l1: float = 10.0,
) -> tuple[Any, dict[str, float]] | None:
    """PS「图层 > 智能对象 > 堆栈模式 > 中间值」的等价实现。

    对话区是【静态半透明输入栏 + 持久 UI（爱心/表情/占位符/发送箭头）】，
    字幕白字是瞬态遮挡且位置逐帧变化 → 逐像素时序中值即可还原无字干净底板，
    并 1:1 保留所有持久 UI（实测 clip15 底板中 占位符/笑脸/爱心/礼物/箭头 全部
    清晰完好）。返回 (plate_bgr, stats)；背景运动过大（非文字帧 temporal L1 超
    max_temporal_l1）或采样不足时返回 None（调用方回退 STTN 结果）。
    """
    import cv2
    import numpy as np

    video = Path(video)
    x0 = int(box["x"])
    y0 = int(box["y"])
    w = int(box["w"])
    h = int(box["h"])
    if w <= 4 or h <= 4:
        return None
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return None
    try:
        n_hint = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if n_hint < 8:
            return None
        idxs = np.linspace(0, n_hint - 1, int(min(k, n_hint))).astype(int)
        stack: list[Any] = []
        tmasks: list[Any] = []
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
            ok, frame = cap.read()
            if not ok:
                continue
            roi = frame[y0 : y0 + h, x0 : x0 + w]
            if roi.shape[0] != h or roi.shape[1] != w:
                continue
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            # 字幕白字笔画：亮且低饱和 → 掩掉，不参与逐像素中值。
            tmask = (gray >= white_thr) & (hsv[..., 1] <= sat_thr)
            stack.append(roi.astype(np.float32))
            tmasks.append(tmask.astype(bool))
        if len(stack) < 8:
            return None
        st = np.stack(stack)
        tm = np.stack(tmasks)
        # Polarity guard: the "white text" mask (bright + low-sat) only catches
        # LIGHT strokes. If it blankets the band it is really masking the
        # BACKGROUND (e.g. dark text on a white plate) -> the median plate would
        # keep the dark text and overwrite STTN's clean result. Reject so the
        # caller falls back to STTN, which detects glyphs of either polarity.
        if float(tm.mean()) > 0.5:
            return None
        med_plain = np.median(st, axis=0)
        vals = np.where(tm[..., None], np.nan, st)
        all_text = np.all(tm, axis=0)
        # 每帧都是字幕的像素(all_text)做 nanmedian 会得到全 NaN 并触发告警，
        # 但其结果在下方会被 med_plain 覆盖，故用 0 占位避免告警（不影响最终 plate）。
        vals[:, all_text, :] = 0.0
        with np.errstate(all="ignore"):
            med_masked = np.nanmedian(vals, axis=0)
        plate = np.where(all_text[..., None], med_plain, med_masked)
        plate = np.nan_to_num(plate, nan=0.0)
        # Direct residual check on the built plate: the text region must read as
        # background, i.e. match the non-text region. A large gap means the
        # temporal median KEPT the caption (a persistent / mostly-present line) --
        # compositing such a plate would re-stamp the very text STTN just removed
        # (leaks e.g. a long line's tail). Reject -> caller falls back to STTN.
        # Polarity-agnostic (light or dark text on a flat band): a clean plate
        # makes text-region == non-text region.
        _tp = tm.any(axis=0)
        if _tp.any() and bool((~_tp).any()):
            _pg = cv2.cvtColor(
                np.clip(plate, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY
            ).astype(np.float32)
            _gap = abs(float(_pg[_tp].mean()) - float(_pg[~_tp].mean()))
            if _gap > 25.0:
                return None
        # 背景稳定性：相邻采样帧在【非文字】像素上的平均 L1（越低越静态）。
        diffs: list[float] = []
        valid = ~tm
        for j in range(len(st) - 1):
            m = valid[j] & valid[j + 1]
            if int(m.sum()) > 50:
                diffs.append(float(np.abs(st[j][m] - st[j + 1][m]).mean()))
        tl1 = float(np.mean(diffs)) if diffs else 255.0
        if tl1 > max_temporal_l1:
            return None
        return (
            np.clip(plate, 0, 255).astype(np.uint8),
            {
                "temporal_l1": round(tl1, 2),
                "text_frac": round(float(tm.mean()), 4),
                "samples": int(len(st)),
            },
        )
    finally:
        cap.release()


def _encode_rawvideo_pipe(
    src_video: Path,
    dest_video: Path,
    *,
    tag: str,
    per_frame: "Callable[[Any], Any | None]",
    progress_every: int = 120,
    frame_wh: "tuple[int, int] | None" = None,
) -> dict[str, Any]:
    """共享的 ffmpeg rawvideo 管道编码器（plate 与 lama 共用，去除重复样板）。

    逐帧读取 src_video，对每帧调用 per_frame(frame) -> BGR(np.uint8) 写盘；
    返回 None 表示跳过该帧。音频从 src mux，输出 fMP4。失败时抛 RuntimeError。
    """
    from typing import Callable

    import cv2
    import numpy as np
    import subprocess

    src_video = Path(src_video)
    dest_video = Path(dest_video)
    dest_video.parent.mkdir(parents=True, exist_ok=True)
    mops = _media()
    if frame_wh is None:
        frame_wh = mops.probe_video_wh(src_video)
        if frame_wh is None:
            raise RuntimeError(f"cannot probe video: {src_video}")
    frame_w, frame_h = frame_wh
    cap = cv2.VideoCapture(str(src_video))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src_video}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    n_hint = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    from fetch_media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    writing = dest_video.with_name(dest_video.stem + f"_{tag}_writing.mp4")
    if writing.is_file():
        writing.unlink(missing_ok=True)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{frame_w}x{frame_h}",
        "-r", f"{fps:.4f}", "-i", "pipe:0", "-i", str(src_video), "-map", "0:v:0",
    ]
    if mops.has_audio_stream(src_video):
        cmd.extend(["-map", "1:a:0?", "-c:a", "aac", "-b:a", f"{mops.DEFAULT_AUDIO_K}k"])
    else:
        cmd.append("-an")
    cmd.extend([
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-g", str(max(12, min(60, int(round(fps)) or 25))),
        "-keyint_min", str(max(12, min(60, int(round(fps)) or 25))),
        "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
        "-flush_packets", "1",
        "-shortest", str(writing),
    ])
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    frames_done = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            out = per_frame(frame)
            if out is None:
                continue
            proc.stdin.write(np.ascontiguousarray(out).astype(np.uint8).tobytes())
            frames_done += 1
            if n_hint and frames_done % progress_every == 0:
                print(f"[{tag}] wrote {frames_done}/{n_hint}", flush=True)
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
        raise RuntimeError(f"{tag} encode failed ({proc.returncode}): {err}")
    if writing.resolve() != dest_video.resolve():
        from _inpaint_shared import _finalize_writing_mp4

        _finalize_writing_mp4(writing, dest_video)
    return {"frames": frames_done}


def encode_composite_plate(
    src_video: Path,
    dest_video: Path,
    plates: list[tuple[dict[str, int], Any]],
    *,
    feather: float = 8.0,
) -> dict[str, Any] | None:
    """把干净底板合成回视频：框内整体用底板，边缘高斯羽化避免接缝。

    用于对话区（静态半透明栏）：底板同时完成「去字幕 + 保留持久 UI」，
    优于 STTN 时序填充（后者会把爱心/表情涂毁并留下灰白 wash）。
    """
    import cv2
    import numpy as np

    if not plates:
        return None
    src_video = Path(src_video)
    dest_video = Path(dest_video)
    wh = _media().probe_video_wh(src_video)
    if wh is None:
        return None
    frame_w, frame_h = wh
    alphas: list[tuple[dict[str, int], Any, Any]] = []
    for b, plate in plates:
        bx = max(0, int(b["x"]))
        by = max(0, int(b["y"]))
        bw = max(2, min(frame_w - bx, int(b["w"])))
        bh = max(2, min(frame_h - by, int(b["h"])))
        m = np.zeros((frame_h, frame_w), dtype=np.float32)
        m[by : by + bh, bx : bx + bw] = 1.0
        if feather and feather > 0:
            m = cv2.GaussianBlur(m, (0, 0), float(feather))
        alphas.append(({"x": bx, "y": by, "w": bw, "h": bh}, plate.astype(np.float32), m[..., None]))

    def _frame(frame: Any) -> Any:
        out = frame.astype(np.float32)
        for b, plate, a in alphas:
            bx, by, bw, bh = b["x"], b["y"], b["w"], b["h"]
            # Weight the plate down wherever IT carries high-frequency content
            # (text residue the temporal median failed to drop, e.g. a persistent
            # caption on a semi-transparent bar). There we keep the incoming frame
            # (the clean STTN result) instead of stamping the plate's text back.
            # A genuinely clean plate is smooth -> composited at full weight, so
            # the plate still fills the gaps STTN left.
            _pu = np.clip(plate, 0, 255).astype(np.uint8)
            _pg = cv2.cvtColor(_pu, cv2.COLOR_BGR2GRAY).astype(np.float32)
            _gx = cv2.Sobel(_pg, cv2.CV_32F, 1, 0)
            _gy = cv2.Sobel(_pg, cv2.CV_32F, 0, 1)
            _grad = np.sqrt(_gx * _gx + _gy * _gy)
            _smooth = np.clip(1.0 - (_grad - 20.0) / 30.0, 0.0, 1.0)
            _smooth = cv2.GaussianBlur(_smooth, (0, 0), 2.0)
            aeff = a[..., 0].copy()
            aeff[by : by + bh, bx : bx + bw] *= _smooth
            comp = out.copy()
            comp[by : by + bh, bx : bx + bw] = plate
            out = out * (1.0 - aeff[..., None]) + comp * aeff[..., None]
        return np.clip(out, 0, 255).astype(np.uint8)

    stats = _encode_rawvideo_pipe(
        src_video, dest_video, tag="plate", progress_every=120, frame_wh=wh,
        per_frame=_frame,
    )
    stats.update({"engine": "plate", "mode": "median_stack", "boxes": len(alphas)})
    print(f"[plate] frames={stats['frames']} boxes={len(alphas)}", flush=True)
    return stats


def full_up_watermark_mask(bgr: Any, box: dict[str, int]) -> Any:
    """Full corner-logo mask: bright ink + AA fringe (covers Poootaaaato / Morphic).

    Classical hard-ink seeds often miss the upper half of glyphs → TELEA/LaMa
    leave readable ghosts. Relative-to-median + abs bright + close/dilate.
    """
    import cv2
    import numpy as np

    x0, y0, bw, bh = _box_xywh(box)
    roi = bgr[y0 : y0 + bh, x0 : x0 + bw]
    full = np.zeros(bgr.shape[:2], dtype=np.uint8)
    if roi.size < 64:
        return full
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    med = cv2.medianBlur(g.astype(np.uint8), 21).astype(np.float32)
    rel = (g > med + 8.0) & (g > 55)
    absb = ((hsv[..., 2] > 100) & (hsv[..., 1] < 130)) | (g > 120)
    m = (rel | absb).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2)
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=5)
    # Drop giant soft blobs (light beams) that cover most of the ROI.
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    keep = np.zeros_like(m)
    lim = int(0.45 * m.size)
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < 8 or area > lim:
            continue
        keep[lab == i] = 255
    if int(keep.max()) == 0:
        keep = m
    full[y0 : y0 + bh, x0 : x0 + bw] = keep
    return full


def watermark_roi_dirty(out_roi: Any, src_roi: Any | None = None) -> dict[str, Any]:
    """Metric gate for Morphic dark-rect / topL blob / leftover bright ink."""
    import cv2
    import numpy as np

    if out_roi is None or out_roi.size < 64:
        return {"dirty": False, "bright": 0.0, "dark_rect": 0.0, "ghost": 0.0}
    g = cv2.cvtColor(out_roi, cv2.COLOR_BGR2GRAY)
    bright = float((g > 155).mean())
    # Dark-rect: center band much darker than left/right margins.
    h, w = g.shape
    mid = g[:, w // 4 : 3 * w // 4]
    side = np.concatenate([g[:, : max(4, w // 8)], g[:, -max(4, w // 8) :]], axis=1)
    dark_rect = max(0.0, float(np.median(side)) - float(np.median(mid)))
    # Ghost: high edge density where source had bright ink.
    ghost = 0.0
    leftover = 0.0
    if src_roi is not None and src_roi.shape[:2] == out_roi.shape[:2]:
        sg = cv2.cvtColor(src_roi, cv2.COLOR_BGR2GRAY)
        ink = sg > 140
        if np.any(ink):
            edges = cv2.Canny(g, 40, 100) > 0
            ghost = float(edges[ink].mean())
        src_bright = float((sg > 155).mean())
        # Intact Morphic: clean still carries ≥35% of source bright mass.
        if src_bright >= 0.015:
            leftover = bright / max(src_bright, 1e-6)
    # Tight gates: fi=69 topL fog sat at ghost≈0.046 / morph dark_rect≈5–10.
    # Ghost alone after ink is gone is 鳞片 texture (locker grain in former
    # glyph pixels) — do not flag dirty or post-QA rework will TELEA it to fog.
    dirty = bool(
        bright >= 0.003
        or dark_rect >= 8.0
        or leftover >= 0.35
        or (ghost >= 0.04 and bright >= 0.0015)
    )
    return {
        "dirty": dirty,
        "bright": round(bright, 5),
        "dark_rect": round(dark_rect, 2),
        "ghost": round(ghost, 4),
        "leftover": round(leftover, 3),
    }


def watermark_roi_accept(met: dict[str, Any] | None) -> bool:
    """True when ROI is clean enough to keep (don't escalate/rework further).

    Hard below on Morphic can leave mild Canny ghost (~0.05) after ink is gone;
    treating that as dirty made rework fall into worse TELEA/LaMa (BV1P6 fi=75).
    """
    if not met:
        return False
    if not met.get("dirty"):
        return True
    return (
        float(met.get("leftover") or 0) < 0.12
        and float(met.get("bright") or 0) < 0.003
        and float(met.get("ghost") or 0) < 0.08
        and float(met.get("dark_rect") or 0) < 10.0
    )


def sample_up_watermark_qa(
    video: Path,
    boxes: list[dict[str, int]],
    *,
    ref_video: Path | None = None,
    samples: int = 5,
    use_vlm: bool | None = None,
    vlm_model: str | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """Metric (+ optional parallel VLM) QA for corner watermarks.

    Default ``samples=5`` = sparse timestamps (safe for confirm workflow).
    ``samples<=0`` = every frame (heavy; use with VITUAL_DEHARDSUB_WM_FULL_QA).

    Returns dirty box list + per-frame dirty indices for rework. VLM catches
    mush / dark-rect / ghosts that metrics miss; run in a thread pool.
    """
    import cv2
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor, as_completed

    video = Path(video)
    if use_vlm is None:
        use_vlm = _as_bool(os.environ.get("VITUAL_DEHARDSUB_WM_VLM"), True)
    if workers is None:
        workers = max(
            1, int(os.environ.get("VITUAL_DEHARDSUB_WM_QA_WORKERS") or 4)
        )
    mops = _media()
    model = (vlm_model or getattr(mops, "HARDSUB_VLM_MODEL", "gemma4:e2b")).strip()
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return {"ok": False, "dirty_boxes": [], "reason": "open_failed"}
    ref_cap = None
    if ref_video is not None and Path(ref_video).is_file():
        ref_cap = cv2.VideoCapture(str(ref_video))
        if not ref_cap.isOpened():
            ref_cap.release()
            ref_cap = None
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    dur = (n / fps) if n > 0 else float(mops.probe_duration_sec(video) or 0.0)
    if dur <= 0.2 or not boxes:
        cap.release()
        if ref_cap is not None:
            ref_cap.release()
        return {"ok": True, "dirty_boxes": [], "samples": [], "dirty_frames": []}

    if int(samples) > 0:
        fracs = [0.05, 0.15, 0.35, 0.55, 0.75, 0.90][: max(1, int(samples))]
        frame_indices = [max(0, int(dur * frac * fps)) for frac in fracs]
        if n > 0:
            frame_indices = [min(idx, n - 1) for idx in frame_indices]
    else:
        frame_indices = list(range(n if n > 0 else 0))
        if not frame_indices:
            # Unknown length — read sequentially until EOF later.
            frame_indices = []

    dirty_flags = [False] * len(boxes)
    dirty_frame_set: set[int] = set()
    rows: list[dict[str, Any]] = []
    work = video.parent / "_wm_qa"
    work.mkdir(parents=True, exist_ok=True)
    # Cap VLM calls. Every-frame metric QA is uncapped; VLM defaults to 64
    # parallel dirty-ROI looks (set VITUAL_DEHARDSUB_WM_VLM_MAX=0 for no cap).
    max_vlm_raw = os.environ.get("VITUAL_DEHARDSUB_WM_VLM_MAX")
    if max_vlm_raw is None or str(max_vlm_raw).strip() == "":
        max_vlm = 64 if int(samples) <= 0 else 8
    else:
        max_vlm = int(max_vlm_raw)
    vlm_budget = {"n": 0}

    print(
        f"[cleanup] up_watermark QA start frames="
        f"{'ALL' if int(samples) <= 0 else len(frame_indices)} "
        f"boxes={len(boxes)} vlm={bool(use_vlm)} workers={workers} "
        f"max_vlm={max_vlm or 'none'}",
        flush=True,
    )

    def _metric_rois(
        idx: int, fr: Any, ref: Any | None
    ) -> list[dict[str, Any]]:
        """Phase 1: metric only (fast, every frame)."""
        local: list[dict[str, Any]] = []
        for bi, box in enumerate(boxes):
            x0, y0, bw, bh = _box_xywh(box)
            out_roi = fr[y0 : y0 + bh, x0 : x0 + bw]
            src_roi = None if ref is None else ref[y0 : y0 + bh, x0 : x0 + bw]
            met = watermark_roi_dirty(out_roi, src_roi)
            local.append(
                {
                    "frame": idx,
                    "box_i": bi,
                    "metric": met,
                    "vlm_dirty": False,
                    "vlm_issues": [],
                    "dirty": bool(met.get("dirty")),
                    "_roi": out_roi if met.get("dirty") else None,
                }
            )
        return local

    def _vlm_one(row: dict[str, Any]) -> dict[str, Any]:
        """Phase 2: one VLM look on a metric-dirty crop."""
        roi = row.pop("_roi", None)
        if roi is None or getattr(roi, "size", 0) < 64:
            return row
        crop = work / f"fi{row['frame']}_b{row['box_i']}.jpg"
        cv2.imwrite(str(crop), roi)
        try:
            use_multi = _as_bool(
                os.environ.get("VITUAL_DEHARDSUB_WM_VLM_MULTI"), True
            )
            if use_multi and hasattr(mops, "vlm_qa_watermark_crop_multi"):
                mods = None
                if model and "," in str(model):
                    mods = [
                        m.strip()
                        for m in str(model).split(",")
                        if m.strip()
                    ]
                vlm = mops.vlm_qa_watermark_crop_multi(
                    crop, models=mods, timeout=45
                )
            else:
                vlm = mops.vlm_qa_watermark_crop(
                    crop, model=model, timeout=45
                )
        except Exception as exc:  # noqa: BLE001
            vlm = {"clean": True, "error": str(exc)[:120]}
        vlm_hit = bool(vlm.get("clean") is False)
        issues = vlm.get("issues") or []
        row["vlm_dirty"] = vlm_hit
        row["vlm_issues"] = (
            [str(x)[:40] for x in issues[:4]] if isinstance(issues, list) else []
        )
        row["dirty"] = bool(row.get("dirty") or vlm_hit)
        return row

    # PeelSub/VideoWipe playback acceptance: wipe-ROI flicker between
    # consecutive cleaned frames (smear/ghost that still-frame OCR-zero misses).
    flicker_thr = float(
        os.environ.get("VITUAL_DEHARDSUB_WM_FLICKER_THR") or 12.0
    )
    prev_gray: list[Any | None] = [None] * len(boxes)

    def _with_flicker(
        idx: int, fr: Any, ref: Any | None
    ) -> list[dict[str, Any]]:
        local = _metric_rois(idx, fr, ref)
        for row in local:
            bi = int(row["box_i"])
            x0, y0, bw, bh = _box_xywh(boxes[bi])
            g = cv2.cvtColor(
                fr[y0 : y0 + bh, x0 : x0 + bw], cv2.COLOR_BGR2GRAY
            ).astype(np.float32)
            flick = 0.0
            if prev_gray[bi] is not None and prev_gray[bi].shape == g.shape:
                flick = float(np.mean(np.abs(g - prev_gray[bi])))
            prev_gray[bi] = g
            row["flicker"] = round(flick, 2)
            if flick >= flicker_thr:
                row["dirty"] = True
                row["flicker_dirty"] = True
            else:
                row["flicker_dirty"] = False
        return local

    # Stream metrics — never hold all frames (BV1P6 OOM: 1148×full BGR).
    loaded_n = 0
    if int(samples) > 0:
        for idx in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, fr = cap.read()
            if not ok:
                continue
            ref = None
            if ref_cap is not None:
                ref_cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok_r, ref = ref_cap.read()
                if not ok_r:
                    ref = None
            # Sparse samples: skip flicker (non-adjacent frames).
            rows.extend(_metric_rois(idx, fr, ref))
            loaded_n += 1
            if loaded_n % 120 == 0:
                print(f"  [wm_qa] metric {loaded_n}", flush=True)
    else:
        idx = 0
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            ref = None
            if ref_cap is not None:
                ok_r, ref = ref_cap.read()
                if not ok_r:
                    ref = None
            rows.extend(_with_flicker(idx, fr, ref))
            idx += 1
            loaded_n = idx
            if idx % 120 == 0:
                print(f"  [wm_qa] metric {idx}", flush=True)

    cap.release()
    if ref_cap is not None:
        ref_cap.release()
    print(
        f"  [wm_qa] metric done rows={len(rows)} "
        f"metric_dirty={sum(1 for r in rows if r.get('dirty'))}",
        flush=True,
    )

    # Phase 2: parallel VLM on metric-dirty (capped). Optional every-frame VLM
    # via VITUAL_DEHARDSUB_WM_VLM_EVERY_FRAME still goes through this queue.
    vlm_every = _as_bool(
        os.environ.get("VITUAL_DEHARDSUB_WM_VLM_EVERY_FRAME"), False
    )
    vlm_targets = [
        r
        for r in rows
        if r.get("_roi") is not None
        and (vlm_every or r.get("dirty"))
        and use_vlm
    ]
    if max_vlm > 0:
        vlm_targets = vlm_targets[:max_vlm]
    # Drop unused ROI buffers to free RAM before VLM.
    vlm_ids = {id(r) for r in vlm_targets}
    for r in rows:
        if id(r) not in vlm_ids:
            r.pop("_roi", None)
    if vlm_targets:
        print(
            f"  [wm_qa] VLM parallel n={len(vlm_targets)} workers={workers}",
            flush=True,
        )
        if workers <= 1 or len(vlm_targets) == 1:
            for r in vlm_targets:
                _vlm_one(r)
                vlm_budget["n"] += 1
        else:
            with ThreadPoolExecutor(
                max_workers=min(workers, len(vlm_targets))
            ) as pool:
                futs = {pool.submit(_vlm_one, r): r for r in vlm_targets}
                for fut in as_completed(futs):
                    fut.result()
                    vlm_budget["n"] += 1
                    if vlm_budget["n"] % 8 == 0:
                        print(
                            f"  [wm_qa] vlm {vlm_budget['n']}/{len(vlm_targets)}",
                            flush=True,
                        )
        for r in vlm_targets:
            r.pop("_roi", None)

    for row in rows:
        row.pop("_roi", None)
        if row.get("dirty"):
            dirty_flags[int(row["box_i"])] = True
            dirty_frame_set.add(int(row["frame"]))

    dirty_boxes = [boxes[i] for i, d in enumerate(dirty_flags) if d]
    dirty_frames = sorted(dirty_frame_set)
    print(
        f"[cleanup] up_watermark QA done dirty_boxes={len(dirty_boxes)}/"
        f"{len(boxes)} dirty_frames={len(dirty_frames)}/{loaded_n} "
        f"vlm_calls={vlm_budget['n']}",
        flush=True,
    )
    return {
        "ok": True,
        "dirty_boxes": dirty_boxes,
        "dirty_n": len(dirty_boxes),
        "dirty_frames": dirty_frames,
        "frames_checked": loaded_n,
        "samples": rows,
        "use_vlm": bool(use_vlm),
        "workers": int(workers),
        "vlm_calls": int(vlm_budget["n"]),
    }


def ocr_scan_up_watermark_frames(
    video: Path,
    *,
    every: int = 1,
    y1_ratio: float = 0.15,
) -> dict[str, Any]:
    """Frame-by-frame OCR gate on the top strip (not sparse sampling).

    Returns frames where Morphic / Poootaaaato / bilibili / Made with still
    OCR-readable. Sparse metric QA previously let intact logos through.
    """
    import re

    import cv2

    try:
        from glyph_detect import _get_ocr
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "hits": [], "error": str(exc)[:120]}
    try:
        ocr = _get_ocr()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "hits": [], "error": str(exc)[:120]}

    video = Path(video)
    pat = re.compile(r"morphic|pooot|bilibili|made\s*with|potato", re.I)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return {"ok": False, "hits": [], "error": "open_failed"}
    hits: list[dict[str, Any]] = []
    fi = 0
    checked = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if every > 1 and (fi % every) != 0:
            fi += 1
            continue
        y1 = max(28, min(fr.shape[0], int(round(fr.shape[0] * y1_ratio))))
        top = fr[0:y1, :]
        big = cv2.resize(top, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
        try:
            res, _ = ocr(big, use_det=True, use_cls=False, use_rec=True)
        except Exception:
            fi += 1
            checked += 1
            continue
        texts = [
            str(it[1]).strip()
            for it in (res or [])
            if isinstance(it, (list, tuple)) and len(it) >= 2 and str(it[1] or "").strip()
        ]
        joined = " | ".join(texts)
        if pat.search(joined):
            hits.append({"frame": fi, "text": joined[:160]})
        checked += 1
        if checked % 200 == 0:
            print(
                f"  [wm_ocr] scanned {checked} hits={len(hits)}",
                flush=True,
            )
        fi += 1
    cap.release()
    print(
        f"[cleanup] up_watermark OCR gate frames={checked} hits={len(hits)}",
        flush=True,
    )
    return {
        "ok": True,
        "hits": hits,
        "hit_n": len(hits),
        "frames_checked": checked,
        "dirty_frames": [int(h["frame"]) for h in hits],
    }


def encode_up_watermark_frame_rework(
    cleaned_video: Path,
    dest_video: Path,
    boxes: list[dict[str, int]],
    *,
    ref_video: Path,
    dirty_frames: list[int] | None = None,
    methods: list[str] | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """Re-wipe dirty frames only (parallel batches), copy clean frames through.

    Used after full-frame QA: only frames flagged dirty get ``rework_up_watermark_roi``
    from ``ref_video`` (source / pre-boxfill).
    """
    import cv2
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor
    from _inpaint_shared import _open_ffmpeg_writer

    cleaned_video = Path(cleaned_video)
    dest_video = Path(dest_video)
    ref_video = Path(ref_video)
    if workers is None:
        workers = max(
            1, int(os.environ.get("VITUAL_DEHARDSUB_WM_QA_WORKERS") or 4)
        )
    batch_n = max(workers * 2, 8)
    wh = _media().probe_video_wh(cleaned_video)
    if wh is None:
        raise RuntimeError(f"cannot probe {cleaned_video}")
    width, height = int(wh[0]), int(wh[1])
    boxes_run = [{k: int(b[k]) for k in ("x", "y", "w", "h")} for b in boxes]
    method_list = list(methods or ["auto"] * len(boxes_run))
    while len(method_list) < len(boxes_run):
        method_list.append("auto")
    dirty_set = set(int(i) for i in (dirty_frames or []))

    cap = cv2.VideoCapture(str(cleaned_video))
    ref_cap = cv2.VideoCapture(str(ref_video))
    if not cap.isOpened() or not ref_cap.isOpened():
        cap.release()
        ref_cap.release()
        raise RuntimeError("cannot open cleaned/ref for watermark rework")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    n_hint = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    proc = _open_ffmpeg_writer(cleaned_video, dest_video, width, height, fps)
    assert proc.stdin is not None
    frames = 0
    reworked = 0
    print(
        f"[wm_rework] dirty_frames={len(dirty_set) or 'ALL'} "
        f"boxes={len(boxes_run)} workers={workers}",
        flush=True,
    )

    def _rework_pair(
        fr: Any, src_fr: Any, do_rework: bool
    ) -> tuple[Any, int]:
        if not do_rework:
            return fr, 0
        out = fr.copy()
        n_rw = 0
        for bi, box in enumerate(boxes_run):
            x0, y0, bw, bh = _box_xywh(box)
            met = watermark_roi_dirty(
                out[y0 : y0 + bh, x0 : x0 + bw],
                src_fr[y0 : y0 + bh, x0 : x0 + bw],
            )
            if not met.get("dirty"):
                continue
            method_bi = str(method_list[bi] or "")
            # Keep 鳞片/copyfill when bright ink is gone — leftover texture is OK.
            if method_bi in ("morphic", "copyfill"):
                if float(met.get("bright") or 0) < 0.012 and float(
                    met.get("ghost") or 0
                ) < 0.15:
                    continue
            if method_bi in ("logo", "adaptive_lama"):
                if float(met.get("ghost") or 0) < 0.04 and float(
                    met.get("dark_rect") or 0
                ) < 8.0:
                    continue
            rework_method = method_bi
            if method_bi == "ns_box":
                if float(met.get("bright") or 0) < 0.008 and float(
                    met.get("leftover") or 0
                ) < 0.20:
                    continue
                rework_method = "morphic"
            elif method_bi == "adaptive_lama":
                if float(met.get("bright") or 0) < 0.008 and float(
                    met.get("ghost") or 0
                ) < 0.06:
                    continue
                rework_method = "morphic"
            fixed, _path = rework_up_watermark_roi(
                out, src_fr, box, method=rework_method
            )
            out[y0 : y0 + bh, x0 : x0 + bw] = fixed[y0 : y0 + bh, x0 : x0 + bw]
            n_rw += 1
        return out, n_rw

    try:
        idx = 0
        while True:
            batch_fr: list[Any] = []
            batch_src: list[Any] = []
            batch_idx: list[int] = []
            for _ in range(batch_n):
                ok, fr = cap.read()
                ok_r, src_fr = ref_cap.read()
                if not ok:
                    break
                if not ok_r:
                    src_fr = fr
                batch_fr.append(fr)
                batch_src.append(src_fr)
                batch_idx.append(idx)
                idx += 1
            if not batch_fr:
                break
            flags = [
                (not dirty_set) or (i in dirty_set) for i in batch_idx
            ]
            if workers <= 1 or len(batch_fr) == 1:
                results = [
                    _rework_pair(fr, src, flag)
                    for fr, src, flag in zip(batch_fr, batch_src, flags)
                ]
            else:
                with ThreadPoolExecutor(
                    max_workers=min(workers, len(batch_fr))
                ) as pool:
                    results = list(
                        pool.map(
                            lambda t: _rework_pair(t[0], t[1], t[2]),
                            zip(batch_fr, batch_src, flags),
                        )
                    )
            for out, n_rw in results:
                reworked += int(n_rw)
                proc.stdin.write(np.ascontiguousarray(out).tobytes())
                frames += 1
                if n_hint and frames % 60 == 0:
                    print(
                        f"[wm_rework] wrote {frames}/{n_hint} "
                        f"roi_reworks={reworked}",
                        flush=True,
                    )
    except Exception:
        proc.kill()
        raise
    finally:
        cap.release()
        ref_cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    stderr = proc.communicate(timeout=600)[1]
    if proc.returncode != 0 or frames < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"wm frame rework failed ({proc.returncode}): {err}")
    print(
        f"[cleanup] up_watermark frame rework frames={frames} "
        f"roi_reworks={reworked}",
        flush=True,
    )
    return {
        "engine": "wm_frame_rework",
        "frames": frames,
        "roi_reworks": reworked,
        "dirty_frames": len(dirty_set) if dirty_set else frames,
        "bytes": dest_video.stat().st_size if dest_video.is_file() else 0,
        "role": "up_watermark",
    }


def _lama_box_mask(
    bgr: Any,
    box: dict[str, int],
    mode: str,
    *,
    thr: float = 14.0,
    pre_detected: list[dict[str, int]] | None = None,
) -> Any:
    """glyph = OCR 笔画；residual = 残影；watermark = 角标全覆盖；auto = glyph→residual。"""
    from _inpaint_shared import glyph_mask_hybrid

    mode_s = (mode or "glyph").strip().lower()
    if mode_s in ("watermark", "up_watermark", "logo_full"):
        return full_up_watermark_mask(bgr, box)
    if mode_s == "residual":
        return residual_ghost_mask(bgr, box, thr=thr)
    glyph = glyph_mask_hybrid(bgr, box, pre_detected=pre_detected)
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

    from glyph_detect import detect_text_boxes
    from inpaint_lama import lama_inpaint

    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    mops = _media()
    wh = mops.probe_video_wh(src)
    if wh is None:
        raise RuntimeError(f"cannot probe video: {src}")
    frame_w, frame_h = wh
    kernel = np.ones((3, 3), np.uint8)
    boxes_n = [
        {k: int(b[k]) for k in ("x", "y", "w", "h")}
        for b in boxes
        if b and all(k in b for k in ("x", "y", "w", "h"))
    ]
    mode_s = (mask_mode or "glyph").strip().lower()
    counters = {"lama_frames": 0}

    def _frame(frame: Any) -> Any:
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

                restored, _bg = restore_mosaic_crop(roi, mask, None, radius=7, use_neural=False)
                out = out.copy()
                out[y : y + h, x : x + w] = restored
        # 整帧只做一次 OCR，所有字幕 box 复用同一份检测结果
        # （原实现每 box 各跑一次全帧 RapidOCR，O(帧×框)→O(帧)）。
        try:
            frame_boxes = detect_text_boxes(out, score_thr=0.5)
        except Exception:
            frame_boxes = None
        hit = False
        for box in boxes_n:
            mask = _lama_box_mask(out, box, mode_s, pre_detected=frame_boxes)
            if int(mask.max()) == 0:
                continue
            if dilate > 0:
                mask = cv2.dilate(mask, kernel, iterations=int(dilate))
            out = lama_inpaint(out, mask)
            hit = True
        if hit:
            counters["lama_frames"] += 1
        return out

    stats = _encode_rawvideo_pipe(
        src, dest, tag="lama", progress_every=60, frame_wh=wh, per_frame=_frame
    )
    stats.update(
        {
            "engine": "lama",
            "mode": mode_s,
            "lama_runs": counters["lama_frames"],
            "boxes": len(boxes_n),
            "bytes": dest.stat().st_size,
        }
    )
    print(
        f"[lama] frames={stats['frames']} lama_hits={stats['lama_runs']} "
        f"boxes={len(boxes_n)} mode={mode_s}",
        flush=True,
    )
    return stats


def _count_frames_ffmpeg(ff: str, p: "Path", wh: tuple[int, int]) -> int:
    """Reliable frame count via ffmpeg raw decode (cv2 CAP_PROP_FRAME_COUNT lies)."""
    import subprocess

    w, h = wh
    r = subprocess.run(
        [ff, "-hide_banner", "-loglevel", "error", "-i", str(p),
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, bufsize=-1,
    )
    n = len(r.stdout)
    return n // (w * h * 3) if n else 0


def _enforce_frame_count(src: "Path", dest: "Path") -> None:
    """Ensure dest has exactly the same frame count as src.

    The shared ffmpeg writer can emit 1–2 extra frames (audio-duration
    extension) *or* drop a few; ``-frames:v`` in copy mode fixes neither, and
    ffmpeg's ``tpad`` filter proved unreliable on our build (it silently added
    no frames). So this does it deterministically in Python, streaming frame by
    frame (constant memory, safe for long videos):
      * over-produced → stop after ``n_src`` frames;
      * under-produced → repeat the last frame to reach ``n_src``.
    Video is re-encoded to a temp, then the *original* audio is muxed back so the
    soundtrack is preserved. Frame-exact output keeps verify / slice concat
    frame-aligned.
    """
    import subprocess

    import cv2

    from media_ops import find_ffmpeg, _video_pix_fmt

    try:
        ff = find_ffmpeg()
        wh = _media().probe_video_wh(src)
        if not wh:
            return
        n_src = _count_frames_ffmpeg(ff, src, wh)
        n_dst = _count_frames_ffmpeg(ff, dest, wh)
        if n_dst == 0 or n_src == 0:
            return
        # BV1P6 (2026-11-19): intermediates are now yuv444p (luma-preserving),
        # so the final deliverable can land as yuv444p via sttn_out.replace().
        # yuv444p is not broadly playable — always re-encode the final to yuv420p
        # even when frame counts match. The streaming pass below is the same one
        # used for frame alignment, so doing it here also guarantees exact count.
        pix = (_video_pix_fmt(ff, dest) or "").strip().lower()
        if n_dst == n_src and pix.startswith("yuv420"):
            return
        w, h = int(wh[0]), int(wh[1])
        cap = cv2.VideoCapture(str(dest))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0) or 25.0
        cap.release()
        fs = w * h * 3

        tmp_v = dest.with_name(dest.stem + "_vfix.mp4")
        rd = subprocess.Popen(
            [ff, "-hide_banner", "-loglevel", "error", "-i", str(dest),
             "-pix_fmt", "bgr24", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, bufsize=-1,
        )
        wr = subprocess.Popen(
            [ff, "-y", "-hide_banner", "-loglevel", "error",
             "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
             "-r", f"{fps}", "-i", "-", "-an",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-pix_fmt", "yuv420p", str(tmp_v)],
            stdin=subprocess.PIPE,
        )
        written = 0
        last: bytes | None = None
        try:
            assert rd.stdout is not None and wr.stdin is not None
            while written < n_src:
                b = rd.stdout.read(fs)
                if len(b) < fs:
                    break
                wr.stdin.write(b)
                last = b
                written += 1
            # under-produced → hold the last frame to reach n_src exactly
            if last is not None:
                while written < n_src:
                    wr.stdin.write(last)
                    written += 1
            wr.stdin.close()
        except Exception:
            rd.kill()
            wr.kill()
            raise
        finally:
            rd.kill()
        wr.wait()
        if not (tmp_v.is_file() and tmp_v.stat().st_size > 800):
            print(f"[cleanup] frame-align warn: video fix failed {n_dst}->{n_src}", flush=True)
            tmp_v.unlink(missing_ok=True)
            return

        # Mux the original (untouched) audio back onto the frame-exact video.
        fixed = dest.with_name(dest.stem + "_framefix.mp4")
        r = subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(tmp_v), "-i", str(dest),
             "-map", "0:v:0", "-map", "1:a:0?",
             "-c:v", "copy", "-c:a", "copy", str(fixed)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if fixed.is_file() and fixed.stat().st_size > 800:
            from _inpaint_shared import replace_file_robust
            replace_file_robust(fixed, dest)
            tmp_v.unlink(missing_ok=True)
        else:
            print(f"[cleanup] frame-align warn: mux failed: {(r.stderr or '')[-200:]}", flush=True)
            tmp_v.unlink(missing_ok=True)
    except Exception as _e:  # noqa: BLE001
        print(f"[cleanup] frame-align skipped: {_e}", flush=True)


def _box_text_energy(src: Path, box: dict[str, int], samples: int = 6) -> float:
    """Median Laplacian (stroke) energy inside ``box`` over a few sampled frames.

    Used to sanity-check a tightened dialogue box: if narrowing locked onto an
    empty sub-band, this collapses vs the original box and the caller falls back.
    """
    import cv2
    import numpy as np

    x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    if w <= 0 or h <= 0:
        return 0.0
    cap = cv2.VideoCapture(str(src))
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            return 0.0
        idxs = {int(round(i * (total - 1) / max(1, samples - 1))) for i in range(samples)}
        vals: list[float] = []
        i = 0
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            if i in idxs:
                roi = fr[y : y + h, x : x + w]
                if roi.size:
                    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                    vals.append(float(cv2.Laplacian(g, cv2.CV_64F).var()))
            i += 1
    finally:
        cap.release()
    return float(np.median(vals)) if vals else 0.0


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
    engine: str = "ocr_telea",
    demosaic_engine: str = "opencv",
    mosaic_boxes: list[dict[str, int]] | None = None,
    polish_residual_floor: float = DEFAULT_RESIDUAL_OK,
    active_windows: list[tuple[float, float]] | None = None,
    dialogue_short_route: str = "auto",
    progress_path: Path | None = None,
    boxes_override: list[dict] | None = None,
    ocr_use_brightness: bool = True,
) -> dict[str, Any]:
    """Locate caption box → 逐框路由（字形洞 + 中值板 clean plate + 纹理对话走 OCR+TELEA）；optional mosaic pass."""
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

    engine_s = (engine or "ocr_telea").strip().lower()
    locate_meta: dict[str, Any] = {}
    box: dict[str, Any] | None = None
    hardsub_boxes: list[dict[str, int]] = []
    if dehardsub:
        # ``boxes_override is not None`` (incl. []) means confirm/wipe_plan owns
        # the track list — never fall back to re-detect (all-keep used to).
        if boxes_override is not None:
            located_list = [
                {
                    "box": {
                        "x": int(b["x"]), "y": int(b["y"]),
                        "w": int(b["w"]), "h": int(b["h"]),
                    },
                    "role": b.get("role", "dialogue"),
                    "source": "override",
                }
                for b in boxes_override
                if str(b.get("action") or "remove").strip().lower() != "keep"
            ]
            if not located_list:
                print(
                    "[cleanup] wipe_plan/override empty after keep — skip hardsub",
                    flush=True,
                )
                dehardsub = False
                locate_meta = {
                    "box": None,
                    "boxes": [],
                    "box_roles": [],
                    "box_source": "wipe_plan_all_keep",
                    "engine": engine_s,
                }
        else:
            located_list = mops.resolve_hardsub_boxes(
                src,
                width,
                height,
                mode=("vlm" if locate_mode == "auto" else locate_mode),
                ratio=ratio,
                vlm_model=model,
                work_dir=work_dir,
            )
    if dehardsub:
        located = located_list[0]
        for loc in located_list:
            b = loc["box"]
            # 对话框始终用字形精修（STTN 已移除，不再走 clamp 旧分支）。
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

        # ---- 黄金标杆工作流（2026-11-24 用户验收：顶栏 NS + 中景@ + 字幕一体）----
        # 替代后续 hybrid 多遍 / wm_qa / residual / safety，避免把完美链路改坏。
        try:
            from dehardsub_gold_workflow import gold_workflow_enabled, run_gold_workflow
        except Exception:
            gold_workflow_enabled = lambda: False  # type: ignore[assignment]
            run_gold_workflow = None  # type: ignore[assignment]
        if gold_workflow_enabled() and run_gold_workflow is not None:
            gold_boxes = [
                {
                    "role": (locate_meta.get("box_roles") or ["dialogue"])[i]
                    if i < len(locate_meta.get("box_roles") or [])
                    else "dialogue",
                    **{k: int(hb[k]) for k in ("x", "y", "w", "h")},
                }
                for i, hb in enumerate(hardsub_boxes)
            ]
            print(
                f"[cleanup] GOLD_WORKFLOW boxes={len(gold_boxes)} "
                f"roles={[b['role'] for b in gold_boxes]}",
                flush=True,
            )
            gold_rep = run_gold_workflow(
                src,
                dest,
                boxes=gold_boxes,
                progress_path=Path(progress_path) if progress_path else None,
            )
            # Persist regions for clear-gate / review.
            if work_dir is not None:
                try:
                    from media_ops import job_dehardsub_dir

                    reg_path = job_dehardsub_dir(Path(work_dir)) / "dehardsub_regions.json"
                    reg_path.write_text(
                        json.dumps(
                            {
                                "boxes": [
                                    {**b, "kind": "hardsub"} for b in gold_boxes
                                ]
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[cleanup] gold regions write skipped ({exc})", flush=True)
            report = {
                **locate_meta,
                **gold_rep,
                "passes": [{"pass": 1, "encode": gold_rep}],
                "dehardsub": True,
            }
            return report

    mosaic_regions: list[dict[str, Any]] = []
    # Per-dialogue VSR routing: tighten + classify (glyph_black | flat | tiles).
    dialogue_routes: list[dict[str, Any] | None] = []
    plate_boxes: list[dict[str, int]] = []
    dialogue_boxes_all: list[dict[str, int]] = []
    any_glyph_black = False
    if dehardsub and hardsub_boxes:
        try:
            from _inpaint_shared import classify_dialogue_route

            roles0 = list(locate_meta.get("box_roles") or [])
            for i, hb in enumerate(hardsub_boxes):
                role_i = roles0[i] if i < len(roles0) else "dialogue"
                if role_i != "dialogue":
                    dialogue_routes.append(None)
                    continue
                route_i = classify_dialogue_route(src, hb)
                dialogue_routes.append(route_i)
                rb = route_i.get("box")
                box_use = (
                    {k: int(rb[k]) for k in ("x", "y", "w", "h")}
                    if isinstance(rb, dict)
                    else {k: int(hb[k]) for k in ("x", "y", "w", "h")}
                )
                # Guard: a tightened box must still carry the dialogue text. If
                # narrowing locked onto an empty sub-band (stroke energy collapses
                # vs the original box), the real subtitle is left behind — fall
                # back to the full detected box.
                if isinstance(rb, dict):
                    e_full = _box_text_energy(src, hb)
                    e_tight = _box_text_energy(src, box_use)
                    if e_tight < 0.5 * e_full:
                        print(
                            f"[cleanup] dialogue[{i}] tighten dropped text "
                            f"(edge {e_tight:.0f} < 0.5*{e_full:.0f}); keep full box",
                            flush=True,
                        )
                        box_use = {k: int(hb[k]) for k in ("x", "y", "w", "h")}
                # Propagate the guarded box back into the route record: the
                # per-box loop later reads route_info["box"] for safety/QA, and
                # otherwise re-uses the discarded h=27 sub-band (P0 h=62→27).
                route_i["box"] = box_use
                dialogue_boxes_all.append(box_use)
                hardsub_boxes[i] = box_use
                if route_i.get("route") in ("glyph_black", "flat"):
                    plate_boxes.append(box_use)
                if route_i.get("route") == "glyph_black":
                    any_glyph_black = True
                print(
                    f"[cleanup] dialogue[{i}] route={route_i.get('route')} "
                    f"black={route_i.get('black_hits')}/{route_i.get('samples')} "
                    f"flat={route_i.get('flat_hits')}/{route_i.get('samples')} "
                    f"box={route_i.get('box')}",
                    flush=True,
                )
            if hardsub_boxes:
                box = hardsub_boxes[0]
                roles_sync = list(locate_meta.get("box_roles") or [])
                for j, hb in enumerate(hardsub_boxes):
                    role_j = roles_sync[j] if j < len(roles_sync) else "dialogue"
                    if role_j != "dialogue":
                        continue
                    for reg in regions:
                        if (
                            reg.get("kind") == "hardsub"
                            and (reg.get("role") or "dialogue") == "dialogue"
                        ):
                            for k in ("x", "y", "w", "h"):
                                reg[k] = int(hb[k])
                            break
        except Exception as exc:  # noqa: BLE001
            print(f"[cleanup] dialogue route classify skipped ({exc})", flush=True)
            dialogue_routes = []

    if demosaic and any_glyph_black and not mosaic_boxes:
        # Opaque black caption plates: auto mosaic scan is almost all false
        # positives (glyph edges). Skip demosaic pre; glyph_black path is enough.
        print(
            "[cleanup] skip demosaic auto-scan (dialogue glyph_black plate)",
            flush=True,
        )
        demosaic = False

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
        # Caption plates / dialogue bands fool mosaic detectors (glyph edges).
        reject_boxes = plate_boxes or dialogue_boxes_all
        if reject_boxes and mosaic_regions:

            def _overlap_frac(a: dict[str, int], b: dict[str, int]) -> float:
                x0 = max(int(a["x"]), int(b["x"]))
                y0 = max(int(a["y"]), int(b["y"]))
                x1 = min(int(a["x"]) + int(a["w"]), int(b["x"]) + int(b["w"]))
                y1 = min(int(a["y"]) + int(a["h"]), int(b["y"]) + int(b["h"]))
                inter = max(0, x1 - x0) * max(0, y1 - y0)
                area = max(1, int(a["w"]) * int(a["h"]))
                return float(inter) / float(area)

            def _center_in_band(m: dict[str, int], band: dict[str, int]) -> bool:
                cy = int(m["y"]) + int(m["h"]) // 2
                cx = int(m["x"]) + int(m["w"]) // 2
                return (
                    int(band["y"]) <= cy <= int(band["y"]) + int(band["h"])
                    and int(band["x"]) <= cx <= int(band["x"]) + int(band["w"])
                )

            before = len(mosaic_regions)
            mosaic_regions = [
                m
                for m in mosaic_regions
                if not any(
                    _center_in_band({k: int(m[k]) for k in ("x", "y", "w", "h")}, bx)
                    or _overlap_frac(
                        {k: int(m[k]) for k in ("x", "y", "w", "h")}, bx
                    )
                    >= 0.25
                    for bx in reject_boxes
                )
            ]
            dropped = before - len(mosaic_regions)
            if dropped:
                print(
                    f"[cleanup] drop {dropped} mosaic false-positives on caption plate",
                    flush=True,
                )
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

    # 默认主路径 = 下方 hybrid 逐框路由（字形洞 + 中值板 clean plate + 纹理对话走 OCR+TELEA）。
    # STTN 与 ProPainter 已彻底移除，去字幕只保留 ocr_telea/LaMa 这一条路线。

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
                print(f"[cleanup] lama unavailable ({exc}); falling back to hybrid route")
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanup] lama failed ({exc}); falling back to hybrid route")
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
        # 降级：继续走下方 hybrid 逐框路由（已解门控）

    if engine_s == "ocr_telea" and dehardsub and box is not None:
        # 默认主路径 = 逐框路由（字形洞 + 中值板 clean plate + 纹理对话走 OCR+TELEA）。
        # STTN 已彻底移除，不再有独立的 STTN 时序分支。
        demosaic_engine_s = str(demosaic_engine).lower()
        sttn_in = src
        mosaic_tmp = None
        mosaic_boxes = None

        if demosaic and mosaic_regions and demosaic_engine_s == "lama":
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
        while len(roles) < len(boxes_run):
            roles.append("dialogue")
        # Left→right up_watermark before Morphic so top-strip donors are clean.
        _dr = list(dialogue_routes) if dialogue_routes else []
        while len(_dr) < len(boxes_run):
            _dr.append(None)
        _order = sorted(
            range(len(boxes_run)),
            key=lambda i: (
                0
                if roles[i] == "dialogue"
                else (1 if roles[i] == "up_watermark" else 2),
                int(boxes_run[i].get("x") or 0) if roles[i] == "up_watermark" else i,
            ),
        )
        boxes_run = [boxes_run[i] for i in _order]
        roles = [roles[i] for i in _order]
        dialogue_routes = [_dr[i] for i in _order]
        current = sttn_in
        temps: list[Path] = []
        pass_stats: list[dict[str, Any]] = []
        dlg_plate_boxes: list[dict[str, int]] = []
        dlg_tiles_boxes: list[dict[str, int]] = []
        # Confirm/route boxes for safety (not process-pad — avoids Δdark dilution).
        dlg_safety_boxes: list[dict[str, int]] = []
        # Boxes that already got solid boxfill — LaMa residual polish re-blurs them.
        boxfill_skip_polish: list[dict[str, int]] = []
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
            # Dialogue inpaint may be TELEA, but watermark/title residual polish
            # still needs LaMa. Only skip non-polish LaMa when TELEA is forced.
            inpaint = (os.environ.get("VITUAL_DEHARDSUB_INPAINT") or "").strip().lower()
            if tag != "polish" and inpaint == "telea":
                return None
            polish_mode = (os.environ.get("VITUAL_DEHARDSUB_POLISH") or "auto").strip().lower()
            if tag == "polish" and polish_mode in ("0", "false", "no", "off", "skip"):
                print("[cleanup] residual polish disabled (VITUAL_DEHARDSUB_POLISH)", flush=True)
                return None
            if LamaUnavailable is None:
                return None
            # Prefer CPU EP for polish to avoid CUDA/cuDNN process aborts that
            # cannot be caught in-process (dialogue path stays on TELEA).
            prev_require = os.environ.get("VITUAL_REQUIRE_GPU")
            if tag == "polish" and polish_mode in ("auto", "cpu", "lama", ""):
                os.environ["VITUAL_REQUIRE_GPU"] = "0"
            try:
                stats = encode_lama_boxes(
                    inp, outp, boxes_l, dilate=2, mask_mode=mask_mode,
                )
                stats = dict(stats)
                stats["role"] = tag
                return stats
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanup] lama {tag} skipped ({exc})", flush=True)
                return None
            finally:
                if tag == "polish":
                    if prev_require is None:
                        os.environ.pop("VITUAL_REQUIRE_GPU", None)
                    else:
                        os.environ["VITUAL_REQUIRE_GPU"] = prev_require

        try:
            _wm_skip_until = -1
            for i, hb in enumerate(boxes_run):
                if i < _wm_skip_until:
                    continue
                out_i = dest.with_name(f"_hs_r{i}.mp4")
                temps.append(out_i)
                role = roles[i] if i < len(roles) else "dialogue"
                box_i = {k: int(hb[k]) for k in ("x", "y", "w", "h")}
                route_info: dict[str, Any] | None = None
                route_kind = ""
                if role == "dialogue":
                    if i < len(dialogue_routes) and dialogue_routes[i]:
                        route_info = dialogue_routes[i]
                    else:
                        try:
                            from _inpaint_shared import classify_dialogue_route

                            route_info = classify_dialogue_route(current, box_i)
                        except Exception as exc:  # noqa: BLE001
                            print(
                                f"[cleanup] dialogue[{i}] classify failed ({exc})",
                                flush=True,
                            )
                            route_info = {
                                "ok": False,
                                "route": "tiles",
                                "box": box_i,
                            }
                    route_kind = str((route_info or {}).get("route") or "tiles")
                    rb = (route_info or {}).get("box")
                    if isinstance(rb, dict):
                        box_i = {k: int(rb[k]) for k in ("x", "y", "w", "h")}
                    # Safety / QA use the route box before process pad (D1).
                    dlg_confirm_box = {k: int(box_i[k]) for k in ("x", "y", "w", "h")}
                    # Tiny pad — taller pads pulled chin/sweat into TELEA and
                    # left brown chin blocks vs clean_demo_v2.
                    _px = max(2, int(round(box_i["w"] * 0.02)))
                    _py = max(2, int(round(box_i["h"] * 0.04)))
                    _nx = max(0, int(box_i["x"]) - _px)
                    _ny = max(0, int(box_i["y"]) - _py)
                    box_i = {
                        "x": _nx,
                        "y": _ny,
                        "w": min(int(width) - _nx, int(box_i["w"]) + 2 * _px),
                        "h": min(int(height) - _ny, int(box_i["h"]) + 2 * _py),
                    }
                else:
                    dlg_confirm_box = None
                # D4: confirmed regions may still be h=28 — grow top strips before expand
                # (detect-only ensure left override boxes short → spill).
                if role == "up_watermark":
                    box_i = _media().ensure_top_up_watermark_min_h(
                        box_i, width, height
                    )
                # title / watermark / up_watermark 扩框；dialogue 用已 pad 的 box_i。
                box_run = (
                    expand_static_overlay_box(box_i, width, height, role=role)
                    if role in ("title", "watermark", "up_watermark")
                    else box_i
                )
                # cleanplate 只服务「静态半透明输入栏」路线（flat）。硬字幕压在
                # 运动画面上（tiles）时中值板易压成黑条，禁止入队；tiles 走残影修补。
                if role == "dialogue" and route_kind == "flat":
                    dlg_plate_boxes.append(
                        {k: int(box_run[k]) for k in ("x", "y", "w", "h")}
                    )
                if role == "dialogue" and route_kind == "tiles":
                    dlg_tiles_boxes.append(
                        {k: int(box_run[k]) for k in ("x", "y", "w", "h")}
                    )
                    if dlg_confirm_box is not None:
                        dlg_safety_boxes.append(dlg_confirm_box)
                # Skip edgefill only when overlay heavily overlaps the caption plate
                # (decorative chrome beside the plate still gets cleaned).
                if any_glyph_black and role in ("title", "watermark") and plate_boxes:

                    def _iou(a: dict[str, int], b: dict[str, int]) -> float:
                        x0 = max(int(a["x"]), int(b["x"]))
                        y0 = max(int(a["y"]), int(b["y"]))
                        x1 = min(int(a["x"]) + int(a["w"]), int(b["x"]) + int(b["w"]))
                        y1 = min(int(a["y"]) + int(a["h"]), int(b["y"]) + int(b["h"]))
                        inter = max(0, x1 - x0) * max(0, y1 - y0)
                        if inter <= 0:
                            return 0.0
                        union = int(a["w"]) * int(a["h"]) + int(b["w"]) * int(b["h"]) - inter
                        return float(inter) / float(max(1, union))

                    if any(_iou(box_run, pb) >= 0.35 for pb in plate_boxes):
                        print(
                            f"[cleanup] skip {role} edgefill (overlaps glyph_black plate)",
                            flush=True,
                        )
                        continue
                # up_watermark: batch consecutive boxes into one encode (D13).
                if role == "up_watermark":
                    wm_jobs: list[dict[str, Any]] = []
                    j = i
                    while j < len(boxes_run) and roles[j] == "up_watermark":
                        bj = {k: int(boxes_run[j][k]) for k in ("x", "y", "w", "h")}
                        bj = _media().ensure_top_up_watermark_min_h(bj, width, height)
                        bj = expand_static_overlay_box(
                            bj, width, height, role="up_watermark"
                        )
                        motion_j = top_strip_motion_score(current, bj)
                        method_j = resolve_up_watermark_method(
                            bj, width, height, motion=motion_j
                        )
                        why_j = {
                            "copyfill": "top/logo strip",
                            "morphic": "top/morphic",
                            "logo": "mid handle",
                        }.get(method_j, method_j)
                        print(
                            f"[cleanup] up_watermark {method_j} ({why_j}) "
                            f"motion={motion_j:.1f} box={bj}",
                            flush=True,
                        )
                        wm_jobs.append(
                            {
                                "box": bj,
                                "method": method_j,
                                "role": "up_watermark",
                            }
                        )
                        j += 1
                    wm_src = current  # pre-boxfill (dialogue-cleaned), not raw src
                    used = encode_multi_static_boxfill(wm_src, out_i, wm_jobs)
                    used = dict(used)
                    used["fallback"] = "multi_" + "+".join(
                        str(j0.get("method") or "") for j0 in wm_jobs
                    )
                    pass_stats.append(used)
                    current = out_i
                    # Default workflow: sparse metric/VLM QA + LaMa polish.
                    # Full-frame QA / multi-round rework / OCR gate is opt-in
                    # (VITUAL_DEHARDSUB_WM_FULL_QA=1) — it was hanging confirm.
                    wm_boxes = [
                        {k: int(j0["box"][k]) for k in ("x", "y", "w", "h")}
                        for j0 in wm_jobs
                    ]
                    wm_methods = [
                        str(j0.get("method") or "auto") for j0 in wm_jobs
                    ]
                    full_qa = _as_bool(
                        os.environ.get("VITUAL_DEHARDSUB_WM_FULL_QA"), False
                    )
                    qa_samples = 0 if full_qa else 5
                    qa = sample_up_watermark_qa(
                        current,
                        wm_boxes,
                        ref_video=wm_src,
                        samples=qa_samples,
                        vlm_model=model,
                    )
                    dirty = list(qa.get("dirty_boxes") or [])
                    dirty_frames = list(qa.get("dirty_frames") or [])
                    print(
                        f"[cleanup] up_watermark QA dirty_boxes={len(dirty)}/"
                        f"{len(wm_boxes)} dirty_frames={len(dirty_frames)}/"
                        f"{qa.get('frames_checked')} vlm={qa.get('use_vlm')} "
                        f"vlm_calls={qa.get('vlm_calls')} full_qa={full_qa}",
                        flush=True,
                    )
                    pass_stats.append(
                        {
                            "engine": "wm_qa",
                            "role": "up_watermark",
                            "dirty_n": len(dirty),
                            "dirty_frames": len(dirty_frames),
                            "frames_checked": qa.get("frames_checked"),
                            "boxes": len(wm_boxes),
                            "use_vlm": qa.get("use_vlm"),
                            "vlm_calls": qa.get("vlm_calls"),
                            "full_qa": full_qa,
                            "sample_dirty": sum(
                                1 for r in (qa.get("samples") or []) if r.get("dirty")
                            ),
                        }
                    )
                    if full_qa and (dirty_frames or dirty):
                        # Opt-in heavy path: per-frame rework (one round).
                        rework_out = dest.with_name("_wm_frame_rework.mp4")
                        temps.append(rework_out)
                        try:
                            rw = encode_up_watermark_frame_rework(
                                current,
                                rework_out,
                                wm_boxes,
                                ref_video=wm_src,
                                dirty_frames=dirty_frames or None,
                                methods=wm_methods,
                            )
                            rw = dict(rw)
                            rw["fallback"] = "wm_frame_rework_after_qa"
                            pass_stats.append(rw)
                            current = rework_out
                        except Exception as exc:  # noqa: BLE001
                            print(
                                f"[cleanup] up_watermark frame rework failed "
                                f"({exc}); try LaMa polish",
                                flush=True,
                            )
                            if dirty:
                                polish_out = dest.with_name("_wm_lama_polish.mp4")
                                temps.append(polish_out)
                                polished = _try_lama(
                                    current,
                                    polish_out,
                                    dirty,
                                    "wm_polish",
                                    mask_mode="watermark",
                                )
                                if polished is not None:
                                    polished = dict(polished)
                                    polished["role"] = "up_watermark"
                                    polished["fallback"] = "wm_lama_after_qa"
                                    pass_stats.append(polished)
                                    current = polish_out
                    elif dirty:
                        polish_out = dest.with_name("_wm_lama_polish.mp4")
                        temps.append(polish_out)
                        polished = _try_lama(
                            current,
                            polish_out,
                            dirty,
                            "wm_polish",
                            mask_mode="watermark",
                        )
                        if polished is not None:
                            polished = dict(polished)
                            polished["role"] = "up_watermark"
                            polished["fallback"] = "wm_lama_after_qa"
                            pass_stats.append(polished)
                            current = polish_out
                        else:
                            print(
                                "[cleanup] up_watermark LaMa polish unavailable",
                                flush=True,
                            )
                    # Watermark path owns these boxes (boxfill ± polish/rework).
                    for b in wm_boxes:
                        boxfill_skip_polish.append(
                            {k: int(b[k]) for k in ("x", "y", "w", "h")}
                        )
                    _wm_skip_until = j
                    continue
                if role in ("title", "watermark"):
                    import cv2
                    import numpy as np
                    from _inpaint_shared import _open_ffmpeg_writer

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
                # Preflight safety net: even if route classification is wrong,
                # never paint a mostly-dark textured band solid black.
                if role == "dialogue" and route_kind == "glyph_black":
                    from _inpaint_shared import glyph_black_would_flood

                    if glyph_black_would_flood(current, box_run):
                        print(
                            "[cleanup] glyph_black flood guard: route -> tiles "
                            "(OCR+TELEA), box=" + str(box_run)
                        )
                        route_kind = "tiles"
                        if route_info:
                            route_info = dict(route_info)
                            route_info["route"] = "tiles"
                            route_info["flood_guard"] = True
                        dlg_tiles_boxes.append(
                            {k: int(box_run[k]) for k in ("x", "y", "w", "h")}
                        )
                        if dlg_confirm_box is not None:
                            dlg_safety_boxes.append(dlg_confirm_box)
                if role == "dialogue" and route_kind == "glyph_black":
                    from _inpaint_shared import encode_glyph_black_fill

                    if progress_path is not None:
                        try:
                            mops = _media()
                            mops._write_meta(
                                progress_path,
                                {
                                    "kind": "dehardsub_glyph_black",
                                    "status": "running",
                                    "role": role,
                                    "box": box_run,
                                    "route": "glyph_black",
                                },
                            )
                        except Exception:
                            pass
                    used = encode_glyph_black_fill(current, out_i, box_run)
                    used = dict(used)
                    used["role"] = role
                    used["box"] = box_run
                    used["route"] = "glyph_black"
                    if route_info:
                        used["route_info"] = {
                            k: route_info.get(k)
                            for k in (
                                "ok",
                                "route",
                                "black_hits",
                                "flat_hits",
                                "samples",
                                "box",
                                "seed_box",
                            )
                        }
                    if progress_path is not None:
                        try:
                            mops = _media()
                            mops._write_meta(
                                progress_path,
                                {
                                    "kind": "dehardsub_glyph_black",
                                    "status": "done",
                                    "role": role,
                                    "frames": used.get("frames"),
                                    "painted_px": used.get("painted_px"),
                                    "route": "glyph_black",
                                },
                            )
                        except Exception:
                            pass
                    pass_stats.append(used)
                    current = out_i
                    continue
                if role == "dialogue" and route_kind == "spatial_fill":
                    from _inpaint_shared import encode_dialogue_spatial_fill

                    if progress_path is not None:
                        try:
                            mops = _media()
                            mops._write_meta(
                                progress_path,
                                {
                                    "kind": "dehardsub_spatial_fill",
                                    "status": "running",
                                    "role": role,
                                    "box": box_run,
                                    "route": "spatial_fill",
                                },
                            )
                        except Exception:
                            pass
                    used = encode_dialogue_spatial_fill(current, out_i, box_run)
                    used = dict(used)
                    used["role"] = role
                    used["box"] = box_run
                    used["route"] = "spatial_fill"
                    if route_info:
                        used["route_info"] = {
                            k: route_info.get(k)
                            for k in (
                                "ok",
                                "route",
                                "black_hits",
                                "flat_hits",
                                "bg_flat_hits",
                                "band_med_gray",
                                "plate_std_gray",
                                "samples",
                                "box",
                                "seed_box",
                            )
                        }
                    if progress_path is not None:
                        try:
                            mops = _media()
                            mops._write_meta(
                                progress_path,
                                {
                                    "kind": "dehardsub_spatial_fill",
                                    "status": "done",
                                    "role": role,
                                    "frames": used.get("frames"),
                                    "filled_px": used.get("filled_px"),
                                    "skipped_flat": used.get("skipped_flat"),
                                    "skipped_gate": used.get("skipped_gate"),
                                    "route": "spatial_fill",
                                },
                            )
                        except Exception:
                            pass
                    pass_stats.append(used)
                    current = out_i
                    continue
                env_prev = None
                force_mode = ""
                if role == "up_watermark":
                    force_mode = "tiles"
                elif role == "dialogue":
                    # Classify after tighten — never force tiles from a single
                    # oversized mixed seed frame (that was the quality failure mode).
                    if route_kind == "flat":
                        force_mode = "flat"
                    elif route_kind == "tiles":
                        force_mode = "tiles"
                if force_mode:
                    import os as _os

                    env_prev = _os.environ.get("VITUAL_STTN_FORCE")
                    if (env_prev or "auto").strip().lower() == "auto":
                        _os.environ["VITUAL_STTN_FORCE"] = force_mode
                try:
                    # 文字/纹理类对话（非 glyph_black / spatial_fill）走 OCR+TELEA 逐框去字；
                    # 不再使用 STTN 时序补洞（避免平灰块/鬼影）。
                    from dehardsub_ocr_telea import run as _ocr_telea_run

                    # Glyph-hole TELEA by default. Thin-mask LaMa is opt-in via
                    # env — except dialogue on textured cloth: TELEA+milk-clamp
                    # left readable dark letter fog (BV1P6 fi=69 uniform).
                    _inp_env = (
                        os.environ.get("VITUAL_DEHARDSUB_INPAINT") or ""
                    ).strip().lower()
                    if _inp_env in ("telea", "lama"):
                        _inp = _inp_env
                    elif role == "dialogue":
                        try:
                            from inpaint_lama import LAMA_WEIGHTS as _lw

                            _inp = "lama" if _lw.is_file() else "telea"
                        except Exception:
                            _inp = "telea"
                    else:
                        _inp = "telea"
                    used = _ocr_telea_run(
                        current,
                        out_i,
                        band=box_run,
                        # Hybrid owns ALL overlay boxes via roles — never let
                        # dialogue OCR auto-detect a corner logo (that soft-
                        # smears Morphic before up_watermark boxfill runs).
                        watermark=False,
                        # Never apply one-video caption stem defaults globally.
                        caption_stems=(),
                        # Brightness band fallback over-masks dark anime dialogue.
                        # Never band-brightness on dialogue: skin highlights get
                        # wiped into white/brown chin blocks (worse than v2).
                        use_brightness=(
                            False
                            if role == "dialogue"
                            else ocr_use_brightness
                        ),
                        # Dialogue: no feather — soft ring re-blended source
                        # white outlines into grey caption ghosts vs v2.
                        feather_px=0 if role == "dialogue" else 5,
                        feather_sigma=1.0 if role == "dialogue" else 1.5,
                        inpaint_method=_inp,
                        # D8: only inpaint caption-active windows for dialogue.
                        active_windows=(
                            active_windows if role == "dialogue" else None
                        ),
                        reject_dark_fill=(role == "dialogue"),
                    )
                    used = dict(used)
                    used["role"] = role
                    used["box"] = box_run
                    if route_kind:
                        used["route"] = route_kind
                    if route_info:
                        used["route_info"] = {
                            k: route_info.get(k)
                            for k in (
                                "ok",
                                "route",
                                "black_hits",
                                "flat_hits",
                                "samples",
                                "box",
                                "seed_box",
                            )
                        }
                    # Logo / grey @handle often OCR to 0 text boxes — fall back
                    # to full-box TELEA. Ignore watermark_mask_pixels (auto
                    # corner mask used to hide this empty-band case).
                    masked = int(used.get("text_boxes_masked") or 0)
                    if role == "up_watermark" and masked <= 0:
                        print(
                            f"[cleanup] up_watermark OCR empty → boxfill "
                            f"box={box_run}",
                            flush=True,
                        )
                        fb = dest.with_name(f"_hs_r{i}_boxfill.mp4")
                        temps.append(fb)
                        used = encode_static_boxfill(
                            current, fb, box_run, role="up_watermark"
                        )
                        used = dict(used)
                        used["fallback"] = "boxfill_after_ocr0"
                        boxfill_skip_polish.append(
                            {k: int(box_run[k]) for k in ("x", "y", "w", "h")}
                        )
                        out_i = fb
                finally:
                    if force_mode:
                        import os as _os

                        if env_prev is None:
                            _os.environ.pop("VITUAL_STTN_FORCE", None)
                        else:
                            _os.environ["VITUAL_STTN_FORCE"] = env_prev
                pass_stats.append(used)
                current = out_i

            # 对话区干净底板：仅 flat（静态半透明输入栏）入队。硬字幕 tiles 禁止。
            # 额外门禁：板比当前带更暗 → 拒绝（防黑条盖回 OCR+TELEA 结果）。
            if dlg_plate_boxes:
                plates: list[tuple[dict[str, int], Any]] = []
                for b in dlg_plate_boxes:
                    bp = build_dialogue_clean_plate(src, b)
                    if bp is None:
                        print(
                            f"[cleanup] dialogue plate skipped (背景运动/采样不足) box={b}",
                            flush=True,
                        )
                        continue
                    ok_plate, gate = dialogue_plate_is_safe(current, b, bp[0])
                    if not ok_plate:
                        print(
                            f"[cleanup] dialogue plate rejected "
                            f"(dark/worse) box={b} gate={gate}",
                            flush=True,
                        )
                        pass_stats.append(
                            {
                                "engine": "plate",
                                "mode": "rejected",
                                "role": "dialogue",
                                "box": b,
                                "gate": gate,
                            }
                        )
                        continue
                    plates.append((b, bp[0]))
                    print(
                        f"[cleanup] dialogue plate ok box={b} stats={bp[1]} gate={gate}",
                        flush=True,
                    )
                if plates:
                    tmp = dest.with_name("_dlg_plate.mp4")
                    used = encode_composite_plate(current, tmp, plates)
                    if used is not None:
                        used["role"] = "dialogue"
                        pass_stats.append(used)
                        current = tmp
                        temps.append(tmp)

            # Residual re-encode mushied the whole plate vs clean_demo_v2.
            # Primary OCR+TELEA (+ adjacent leftover pass) owns clear; skip
            # extra video rewrite unless explicitly enabled.
            _resid_on = (
                os.environ.get("VITUAL_DEHARDSUB_RESIDUAL") or ""
            ).strip().lower() in ("1", "true", "on", "yes")
            if dlg_tiles_boxes and _resid_on:
                from residual_repair import scan_and_repair

                for bi, b in enumerate(dlg_tiles_boxes):
                    try:
                        repaired, idxs, rmeta = scan_and_repair(src, current, b)
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"[cleanup] dialogue residual repair failed ({exc})",
                            flush=True,
                        )
                        pass_stats.append(
                            {
                                "engine": "residual_repair",
                                "role": "dialogue",
                                "box": b,
                                "mode": "error",
                                "error": str(exc)[:200],
                            }
                        )
                        continue
                    mode = str((rmeta or {}).get("mode") or "ok")
                    print(
                        f"[cleanup] dialogue residual mode={mode} "
                        f"hits={rmeta.get('hits')} selected={len(idxs)} box={b}",
                        flush=True,
                    )
                    pass_stats.append(
                        {
                            "engine": "residual_repair",
                            "role": "dialogue",
                            "box": b,
                            "mode": mode,
                            "scanned": rmeta.get("scanned"),
                            "hits": rmeta.get("hits"),
                            "frames_repaired": len(idxs),
                            "sample_idxs": idxs[:12],
                        }
                    )
                    if idxs and Path(repaired).resolve() != Path(current).resolve():
                        current = Path(repaired)
                        temps.append(current)
            elif dlg_tiles_boxes:
                for b in dlg_tiles_boxes:
                    pass_stats.append(
                        {
                            "engine": "residual_repair",
                            "role": "dialogue",
                            "box": b,
                            "mode": "skipped_v2_strength",
                            "frames_repaired": 0,
                        }
                    )
                    print(
                        "[cleanup] dialogue residual skipped "
                        "(set VITUAL_DEHARDSUB_RESIDUAL=1 to enable)",
                        flush=True,
                    )

            # Safety: restore confirm-box ROI on dark bar / w2b (D1).
            safety_boxes = dlg_safety_boxes or dlg_tiles_boxes
            if safety_boxes:
                for bi, b in enumerate(safety_boxes):
                    gate_out = dest.with_name(f"_dlg_safe{bi}.mp4")
                    temps.append(gate_out)
                    try:
                        used = encode_dialogue_safety_gate(src, current, gate_out, b)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[cleanup] dialogue safety skipped ({exc})", flush=True)
                        continue
                    pass_stats.append(used)
                    current = gate_out

            # Residual polish: only static overlays are safe for LaMa touch-up here.
            # 对话区【不】走 LaMa 残影精修：字幕条是半透明 UI 栏覆盖在运动画面上，
            # LaMa 没有干净空间上下文可分，会对残影 hallucinate 出亮色涂抹
            # （实测 run5：对话区 亮白 0.2%→3.7%、闪烁 1.46→1.82 全面变差）。
            # 对话残影由字形洞（glyph_mask_hybrid）已能压到 亮白~0.2%，保持不动为最佳。
            # up_watermark is finished in-pass (logo pixels / copyfill). LaMa
            # residual polish on those boxes re-smeared Morphic / top bars.
            polish_roles = {"title", "watermark"}

            def _same_box(a: dict[str, int], b: dict[str, int], tol: int = 8) -> bool:
                return all(
                    abs(int(a[k]) - int(b[k])) <= tol for k in ("x", "y", "w", "h")
                )

            polish_boxes = []
            for i, hb in enumerate(boxes_run):
                role_p = roles[i] if i < len(roles) else "dialogue"
                if role_p not in polish_roles:
                    continue
                pb = expand_static_overlay_box(
                    {k: int(hb[k]) for k in ("x", "y", "w", "h")},
                    width,
                    height,
                    role=role_p,
                )
                # Boxfill already wiped logos; LaMa residual leaves Morphic-like blur.
                if any(_same_box(pb, sk) for sk in boxfill_skip_polish):
                    print(
                        f"[cleanup] skip LaMa polish (already boxfill) box={pb}",
                        flush=True,
                    )
                    continue
                if role_p == "up_watermark" and up_watermark_prefers_boxfill(
                    pb, height, width
                ):
                    print(
                        f"[cleanup] skip LaMa polish (top/logo strip) box={pb}",
                        flush=True,
                    )
                    continue
                polish_boxes.append(pb)
            polish = None
            polish_probe = None
            if float(polish_residual_floor) >= 0.99:
                print(
                    f"[cleanup] polish=off (floor={float(polish_residual_floor):.3f})",
                    flush=True,
                )
            if polish_boxes:
                polish_probe = sample_validate_video(current, polish_boxes, samples=5, ref_video=src)
                polish_score = float(polish_probe.get("hardsub") or polish_probe.get("combined") or 0.0)
                if polish_score >= max(0.0, float(polish_residual_floor)):
                    polish = _try_lama(
                        current, sttn_out, polish_boxes, "polish", mask_mode="residual"
                    )
            if polish is None:
                if current.resolve() != sttn_out.resolve():
                    import shutil

                    shutil.copy2(current, sttn_out)
            else:
                pass_stats.append(polish)

            polish_score_v = (
                float(polish_probe.get("hardsub") or polish_probe.get("combined") or 0.0)
                if polish_probe
                else 0.0
            )
            # Only "needed" when probe residual clears the floor — empty polish_boxes
            # (all logos already boxfilled) or low residual must not mark degraded.
            polish_needed = bool(polish_boxes) and polish_score_v >= max(
                0.0, float(polish_residual_floor)
            )
            polish_applied = bool(polish is not None)
            quality = "ok"
            quality_note = ""
            if polish_needed and not polish_applied:
                quality = "degraded"
                quality_note = (
                    "residual LaMa polish failed for watermark|title; "
                    "dialogue TELEA path kept"
                )
                print(f"[cleanup] QUALITY degraded: {quality_note}", flush=True)

            # D3: spot QA on process boxes → quality=degraded when flags fire.
            qa_flags: list[str] = []
            qa_spans: list[dict[str, Any]] = []
            truthful_ok = True
            try:
                from dehardsub_frame_qa import qa_pair

                qa_boxes: list[dict[str, Any]] = []
                for sb in dlg_safety_boxes:
                    qb = {k: int(sb[k]) for k in ("x", "y", "w", "h")}
                    qb["role"] = "dialogue"
                    qa_boxes.append(qb)
                if not dlg_safety_boxes:
                    for i, hb in enumerate(boxes_run):
                        role_q = roles[i] if i < len(roles) else "dialogue"
                        if role_q != "dialogue":
                            continue
                        qb = {k: int(hb[k]) for k in ("x", "y", "w", "h")}
                        qb["role"] = "dialogue"
                        qa_boxes.append(qb)
                for i, hb in enumerate(boxes_run):
                    role_q = roles[i] if i < len(roles) else "dialogue"
                    if role_q == "dialogue":
                        continue
                    qb = expand_static_overlay_box(
                        {k: int(hb[k]) for k in ("x", "y", "w", "h")},
                        width,
                        height,
                        role=role_q,
                    )
                    qb["role"] = role_q
                    qa_boxes.append(qb)
                if qa_boxes and sttn_out.is_file():
                    dur = float(_media().probe_duration_sec(src) or 0.0)
                    if dur <= 0.5:
                        times = [0.2]
                    else:
                        times = [
                            round(min(dur * 0.95, max(0.1, dur * f)), 2)
                            for f in (0.05, 0.25, 0.45, 0.65, 0.85)
                        ]
                    qa_rep = qa_pair(
                        src,
                        sttn_out,
                        qa_boxes,
                        times_sec=times,
                        out_dir=None,
                        write_frames=False,
                    )
                    qa_flags = list(qa_rep.get("flags") or [])
                    qa_spans = list(qa_rep.get("spans") or [])
                    truthful_ok = bool(qa_rep.get("truthful_ok"))
                    # D14 truthful: byte-identical clean must not be quality=ok.
                    if qa_rep.get("byte_identical") and "byte_identical_clean" not in qa_flags:
                        qa_flags = list(qa_flags) + ["byte_identical_clean"]
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanup] inline QA skipped ({exc})", flush=True)
                qa_spans = []
                truthful_ok = True
            if qa_flags:
                quality = "degraded"
                note = f"qa_flags={len(qa_flags)} e.g. {qa_flags[0]}"
                quality_note = (
                    f"{quality_note}; {note}" if quality_note else note
                )
                print(f"[cleanup] QUALITY degraded: {note}", flush=True)

            # D1: safety restored many frames → still mark degraded if QA silent
            # but gate fired heavily (leave-text is safer than black bar, not "ok").
            safety_restored = sum(
                int(p.get("restored") or 0)
                for p in pass_stats
                if p.get("engine") == "dialogue_safety"
            )
            if safety_restored >= 12 and quality == "ok":
                quality = "degraded"
                quality_note = (
                    f"dialogue_safety restored {safety_restored} frames "
                    "(captions left rather than black smear)"
                )
                print(f"[cleanup] QUALITY degraded: {quality_note}", flush=True)

            sttn_stats = {
                "engine": "hybrid",
                "regions": len(boxes_run),
                "passes": pass_stats,
                "mode": "cleanplate+lama",
                "polish_probe": polish_probe,
                "polish_residual_floor": float(polish_residual_floor),
                "polish_applied": polish_applied,
                "polish_needed": polish_needed,
                "quality": quality,
                "quality_note": quality_note,
                "qa_flags": qa_flags[:24],
                "qa_spans": qa_spans[:24],
                "truthful_ok": bool(truthful_ok) and not qa_flags,
                "active_windows": (
                    [{"start": float(s), "end": float(e)} for s, e in active_windows]
                    if active_windows
                    else None
                ),
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
            from _inpaint_shared import replace_file_robust
            replace_file_robust(sttn_out, dest)
        # Global frame-align: the last pass re-encodes with the shared writer,
        # which can overproduce 1-2 frames (audio-duration extension). Trim the
        # final output to the source's true frame count so verify/concat stay aligned.
        _enforce_frame_count(src, dest)
        # D1: replay safety on final clean — would_restore>0 means gate missed.
        safety_audit: list[dict[str, Any]] = []
        for b in dlg_safety_boxes or dlg_tiles_boxes:
            try:
                hit = audit_dialogue_safety_hits(src, dest, b)
                safety_audit.append(hit)
                if int(hit.get("would_restore") or 0) > 0:
                    sttn_stats["quality"] = "degraded"
                    note = (
                        f"safety_audit would_restore={hit['would_restore']} "
                        f"on final clean"
                    )
                    prev = str(sttn_stats.get("quality_note") or "")
                    sttn_stats["quality_note"] = (
                        f"{prev}; {note}" if prev else note
                    )
                    print(f"[cleanup] QUALITY degraded: {note}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[cleanup] safety_audit skipped ({exc})", flush=True)
        sttn_stats["safety_audit"] = safety_audit
        print(
            f"[cleanup] hybrid frames={sttn_stats.get('frames')} "
            f"regions={len(boxes_run)} mosaic_pre={'on' if mosaic_tmp is not None else 'off'} "
            f"(demosaic={demosaic_engine_s})"
        )
        return {
            "action": "hybrid_cleanup",
            "engine": "cleanplate+lama",
            "mosaic_engine": demosaic_engine_s,
            "mosaic_pre": mosaic_tmp is not None,
            "passes": [{"pass": 1, "encode": sttn_stats}],
            "pass_count": 1,
            "regions": regions,
            "mosaic_regions": mosaic_regions,
            "demosaic": demosaic,
            "dehardsub": dehardsub,
            "src_size": f"{width}x{height}",
            "quality": sttn_stats.get("quality") or "ok",
            "quality_note": sttn_stats.get("quality_note") or "",
            "polish_applied": bool(sttn_stats.get("polish_applied")),
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
        from _inpaint_shared import replace_file_robust
        replace_file_robust(current, dest)
    _enforce_frame_count(src, dest)

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
