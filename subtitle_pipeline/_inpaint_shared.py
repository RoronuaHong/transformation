"""STTN / caption restore for burned-in text.

Routing (industry-aligned with video-subtitle-remover / VideoWipe):
  - Flat UI bars → Temporal Background Exposure (TBE): median of pixels that
    are *not* glyphs in other frames. Preserves grain; falls back to spatial
    lerp where a pixel never exposes. Matches VSR's "STTN mode" TBE idea
    without treating the whole box as a neural hole (which soft-smears bars).
  - Textured / live-action → native 1:1 STTN tiles (box hole).
  - Optional: demosaic via mosaic_boxes on the same tile pass, or CodeFormer.

Further quality (not default): LaMa ONNX for static never-changing text;
ProPainter for heavy motion (VRAM-heavy, often non-commercial weights).
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any

STTN_W = 432
STTN_H = 240
REF_LENGTH = 10
NEIGHBOR_STRIDE = 5
MAX_LOAD = max(6, int(os.environ.get("VITUAL_STTN_MAX_LOAD", "12") or 12))


def band_is_flat(bgr: Any, box: dict[str, int]) -> bool:
    """True when the caption bar is a low-variance UI strip (STTN soft-smears these).

    Real footage (衣服/皮肤/木纹) 必须返回 False → STTN tiles。
    仅动画/纯色 UI 底条才走 temporal_flat。
    """
    import cv2
    import numpy as np

    y = max(0, int(box["y"]))
    x = max(0, int(box["x"]))
    h = max(1, int(box["h"]))
    w = max(1, int(box["w"]))
    roi = bgr[y : y + h, x : x + w]
    if roi.size < 16:
        return False
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    bg_med = float(np.median(gray))
    glyphs = np.abs(gray.astype(np.float32) - bg_med) > 55.0
    remain = ~glyphs
    if float(remain.mean()) < 0.25:
        return False
    core_var = float(gray[remain].var())
    if core_var > 250.0:
        return False
    remain_med = float(np.median(gray[remain]))
    # Mid-tone scene (skin / suit / wood) — never a caption UI strip,
    # even when locally smooth (zero core_var).
    if 85.0 < remain_med < 195.0:
        return False
    # Edge energy gate only for ambiguous tones; putText AA on solid UI bars
    # already has moderate Laplacian (~8-14).
    lap = float(np.abs(cv2.Laplacian(gray, cv2.CV_32F)[remain]).mean())
    if 60.0 <= remain_med <= 220.0 and lap > 16.0:
        return False

    def _flat_on(mask: Any) -> bool:
        if float(mask.mean()) < 0.25:
            return False
        local_med = float(np.median(gray[mask]))
        bg = np.abs(gray.astype(np.float32) - local_med) < 28.0
        if float(bg.mean()) < 0.2:
            return False
        return float(gray[bg].var()) < 280.0

    if _flat_on(gray < 140):
        return True
    if _flat_on(gray > 200):
        return True
    return False


def find_black_caption_box(
    bgr: Any,
    seed: dict[str, int],
    *,
    min_h: int = 18,
    min_w: int = 80,
) -> dict[str, int] | None:
    """Tighten an oversized locate box onto the opaque black caption plate.

    Locate often returns a tall/wide dialogue band that mixes scene pixels with a
    smaller black subtitle plate. STTN on that mix smears into 'black paint';
    solid-fill should use the plate only.

    Detection uses midtone gaps: caption plates are mostly near-black fill plus
    bright glyph strokes, so mid-gray scene pixels are scarce inside the plate.
    After a candidate is found, height is re-cropped to the dark+stroke band so
    seed-tall empty bars do not win on area alone.
    """
    import cv2
    import numpy as np

    H, W = bgr.shape[:2]
    y0 = max(0, int(seed["y"]))
    x0 = max(0, int(seed["x"]))
    h0 = max(1, int(seed["h"]))
    w0 = max(1, int(seed["w"]))
    y1 = min(H, y0 + h0)
    x1 = min(W, x0 + w0)
    if y1 - y0 < min_h or x1 - x0 < min_w:
        return None
    gray = cv2.cvtColor(bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    rh, rw = gray.shape[:2]
    mid = (gray >= 48.0) & (gray <= 160.0)
    # Prefer rows that are plate-like (few midtones) AND mostly dark/bright ink.
    row_darkish = ((gray < 55.0) | (gray > 150.0)).mean(axis=1) > 0.55
    row_ok = (mid.mean(axis=1) < 0.40) & row_darkish

    def _runs(mask: Any, min_len: int) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        start: int | None = None
        for i, ok in enumerate(mask.tolist()):
            if ok and start is None:
                start = i
            elif not ok and start is not None:
                if i - start >= int(min_len):
                    out.append((start, i))
                start = None
        if start is not None and len(mask) - start >= int(min_len):
            out.append((start, len(mask)))
        return out

    row_runs = _runs(row_ok, min_h)
    if not row_runs:
        # Fallback: midtone-only (older heuristic) when darkish gate is too strict.
        row_runs = _runs(mid.mean(axis=1) < 0.40, min_h)
    if not row_runs:
        return None

    best: dict[str, int] | None = None
    best_score = -1.0
    seed_cx = 0.5 * float(rw)
    stroke_kernel = np.ones((3, 3), dtype=np.uint8)
    for ry0, ry1 in row_runs:
        band_mid = mid[ry0:ry1]
        col_ok = band_mid.mean(axis=0) < 0.40
        for cx0, cx1 in _runs(col_ok, min_w):
            roi = gray[ry0:ry1, cx0:cx1]
            glyphs = roi > 140.0
            remain = ~glyphs
            if float(remain.mean()) < 0.28:
                continue
            bg = roi[remain]
            bg_mean = float(bg.mean())
            dark_frac = float((bg < 48.0).mean())
            if bg_mean > 50.0 or dark_frac < 0.62:
                continue
            bright = roi > 160.0
            dark = roi < 60.0
            near_dark = cv2.dilate(dark.astype(np.uint8), stroke_kernel) > 0
            strokes = bright & near_dark
            bright_frac = float(bright.mean())
            stroke_frac = float(strokes.mean())
            stroke_ratio = float(strokes.sum()) / float(max(1, int(bright.sum())))
            # Solid white UI tiles (tier list) have lots of bright pixels but few
            # stroke edges against dark fill — reject those.
            if bright_frac > 0.12 and stroke_ratio < 0.05:
                continue
            h = int(ry1 - ry0)
            w = int(cx1 - cx0)
            if float(w) / float(max(1, h)) < 2.2:
                continue
            if stroke_frac < 0.002 and h < int(0.55 * rh):
                continue
            # Prefer compact plates: area alone re-selects the full seed height.
            height_pen = 1.0 / (1.0 + 0.012 * max(0, h - 72))
            area = float(h * w)
            cx = 0.5 * float(cx0 + cx1)
            center_bonus = 1.0 + 0.45 * (
                1.0 - abs(cx - seed_cx) / max(1.0, seed_cx)
            )
            ink_bonus = 1.0 + 60.0 * stroke_frac
            score = area * dark_frac * ink_bonus * center_bonus * height_pen
            if score > best_score:
                best_score = score
                best = {
                    "x": int(x0 + cx0),
                    "y": int(y0 + ry0),
                    "w": w,
                    "h": h,
                }
    if best is None:
        return None

    # Secondary crop: keep rows that are dark plate or glyph strokes.
    by0 = int(best["y"]) - y0
    bx0 = int(best["x"]) - x0
    by1 = by0 + int(best["h"])
    bx1 = bx0 + int(best["w"])
    plate = gray[by0:by1, bx0:bx1]
    if plate.size:
        dark_r = (plate < 55.0).mean(axis=1)
        bright_r = (plate > 140.0).mean(axis=1)
        keep_r = (dark_r > 0.45) | (bright_r > 0.01)
        idxs = np.where(keep_r)[0]
        if idxs.size >= min_h:
            # Drop sparse leading/trailing midtone scene rows.
            top = int(idxs[0])
            bot = int(idxs[-1]) + 1
            pad = 4
            top = max(0, top - pad)
            bot = min(plate.shape[0], bot + pad)
            if bot - top >= min_h:
                best = {
                    "x": int(best["x"]),
                    "y": int(y0 + by0 + top),
                    "w": int(best["w"]),
                    "h": int(bot - top),
                }
    return best


def caption_band_is_black_bar(
    bgr: Any,
    box: dict[str, int],
    *,
    max_bg_mean: float = 42.0,
    min_dark_frac: float = 0.55,
    max_bg_var: float = 380.0,
) -> bool:
    """True when caption box is mostly a solid dark letterbox (white glyphs on black).

    These bands should be solid-filled, not STTN-inpainted (STTN collapses to a
    darker smear and costs hours for an effect that looks like black paint).
    """
    import cv2
    import numpy as np

    if find_black_caption_box(bgr, box) is not None:
        return True

    y = max(0, int(box["y"]))
    x = max(0, int(box["x"]))
    h = max(1, int(box["h"]))
    w = max(1, int(box["w"]))
    roi = bgr[y : y + h, x : x + w]
    if roi.size < 64:
        return False
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
    # Treat bright strokes as glyphs; score the remaining "background".
    glyphs = gray > 140.0
    remain = ~glyphs
    if float(remain.mean()) < float(min_dark_frac):
        return False
    bg = gray[remain]
    if float(bg.mean()) > float(max_bg_mean):
        return False
    if float(bg.var()) > float(max_bg_var):
        return False
    dark = bg < 48.0
    return float(dark.mean()) >= 0.75


def classify_dialogue_route(
    video: Any,
    box: dict[str, int],
    *,
    samples: int = 7,
) -> dict[str, Any]:
    """General VSR-style dialogue routing (not clip-specific).

    1) Sample frames and optionally tighten onto an opaque caption plate.
    2) Classify:
       - ``glyph_black``: white strokes on opaque dark plate → paint strokes black
       - ``flat``: glyph-free flat UI strip → Temporal Background Exposure
       - ``tiles``: textured live-action, or flat bands carrying text glyphs
         (TBE+lerp ghosts on those; STTN tiles with glyph holes fill cleaner)
    """
    import cv2
    import numpy as np

    seed = {k: int(box[k]) for k in ("x", "y", "w", "h")}
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return {"ok": False, "route": "tiles", "box": seed, "hits": 0, "samples": 0}
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    idxs = (
        [0]
        if n <= 1
        else [int(round(i * (n - 1) / max(1, samples - 1))) for i in range(samples)]
    )
    black_hits = 0
    flat_hits = 0
    glyph_flat_hits = 0
    bg_flat_hits = 0
    bright_fracs: list[float] = []
    checked = 0
    found_boxes: list[dict[str, int]] = []
    band_medians: list[float] = []
    try:
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
            ok, fr = cap.read()
            if not ok:
                continue
            checked += 1
            # Band darkness gate: an opaque BLACK plate dominates its tight ROI, so
            # the ROI gray median stays low. Dark TEXT on bright footage also trips
            # find_black_caption_box, but keeps the ROI median bright — painting
            # those strokes black would leave black blobs (实测 gray_med=203 的亮底
            # 黑字被误判后整条涂黑). Bright band → never route to glyph_black.
            ry0 = max(0, seed["y"])
            ry1 = min(fr.shape[0], seed["y"] + seed["h"])
            rx0 = max(0, seed["x"])
            rx1 = min(fr.shape[1], seed["x"] + seed["w"])
            if ry1 > ry0 and rx1 > rx0:
                roi_gray = cv2.cvtColor(
                    fr[ry0:ry1, rx0:rx1], cv2.COLOR_BGR2GRAY
                )
                _fm = float(np.median(roi_gray))
                band_medians.append(_fm)
                # Fraction of the band brighter than a dark plate (footage bleeding
                # through a translucent bar, vs. thin white strokes). Aggregated
                # below with band_med to flag semi-transparent bars.
                bright_fracs.append(float((roi_gray > 130).mean()))
                # 平色背景检测（spatial_fill 路由）：字幕压在纯平色底上时，
                # STTN 时序取材会产生鬼影（同句字幕持续 ~3s，邻帧全有字），
                # 逐帧空间填充才是对的。判据：高饱和亮色占 ROI>25%（有主导
                # 平色底）且距该底色 <=40 的像素占比 >55%（背景确实平）。
                roi_bgr = fr[ry0:ry1, rx0:rx1]
                hsv_r = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
                ss = hsv_r[..., 1].astype(np.int16)
                vv = hsv_r[..., 2].astype(np.int16)
                bgm = (ss > 60) & (vv > 200)
                if float(bgm.mean()) > 0.25:
                    bgc = np.median(
                        roi_bgr[bgm].reshape(-1, 3), axis=0
                    ).astype(np.float32)
                    dr = np.linalg.norm(
                        roi_bgr.astype(np.float32) - bgc.reshape(1, 1, 3),
                        axis=2,
                    )
                    if float((dr <= 40).mean()) > 0.55:
                        bg_flat_hits += 1
            found = find_black_caption_box(fr, seed)
            if found is not None:
                black_hits += 1
                found_boxes.append(found)
                # Opaque dark plates are also "flat" for routing purposes.
                flat_hits += 1
                continue
            if caption_band_is_black_bar(fr, seed):
                black_hits += 1
                flat_hits += 1
                # Do NOT append the oversized seed — that re-inflates median height.
                continue
            if band_is_flat(fr, seed):
                flat_hits += 1
                # Flat band carrying text glyphs: TBE+lerp ghosts (实测 25.6 vs
                # tiles 7.7) → fall through to tiles routing below.
                if flat_band_glyph_px(fr, seed) > 0:
                    glyph_flat_hits += 1
    finally:
        cap.release()

    need = max(2, int(round(0.55 * max(1, checked))))
    tight = seed
    if found_boxes:
        # Keep locate WIDTH — median X from floating caption plates clips
        # left/right glyphs on other frames. Only tighten Y/H from plates.
        ys0 = sorted(int(b["y"]) for b in found_boxes)
        ys1 = sorted(int(b["y"]) + int(b["h"]) for b in found_boxes)
        mid = len(found_boxes) // 2
        y0 = int(ys0[mid])
        y1 = int(ys1[mid])
        # Small pad so glyph tops/bottoms are not clipped.
        pad = 8
        y0 = max(int(seed["y"]), y0 - pad)
        y1 = min(int(seed["y"]) + int(seed["h"]), y1 + pad)
        h = max(1, y1 - y0)
        if h >= 18:
            tight = {
                "x": int(seed["x"]),
                "y": int(y0),
                "w": int(seed["w"]),
                "h": int(h),
            }

    band_med = float(np.median(band_medians)) if band_medians else 255.0
    bright_frac = float(np.mean(bright_fracs)) if bright_fracs else 0.0
    # A semi-transparent bar = dark translucent bar (median <=120) with footage
    # bleeding through (a large bright share, well beyond thin white strokes). It
    # is neither an opaque black plate (glyph_black) nor a pure flat bar
    # (flat/TBE): its strokes sit over moving footage -> route to tiles (STTN).
    semi_transparent = band_med <= 120 and bright_frac > 0.25
    if semi_transparent:
        route = "tiles"
    elif black_hits >= need and band_med <= 120:
        route = "glyph_black"
    elif bg_flat_hits >= need:
        # 亮色平底上的烧录字幕：逐帧空间填充（STTN 时序取材会鬼影）。
        route = "spatial_fill"
    elif flat_hits >= need and glyph_flat_hits == 0:
        # Pure flat bars (no text glyphs) keep the fast TBE path.
        route = "flat"
    else:
        route = "tiles"
    return {
        "ok": True,
        "route": route,
        "box": tight,
        "seed_box": seed,
        "band_med_gray": band_med,
        "black_hits": int(black_hits),
        "flat_hits": int(flat_hits),
        "glyph_flat_hits": int(glyph_flat_hits),
        "bg_flat_hits": int(bg_flat_hits),
        "samples": int(checked),
    }


def _glyphfill_batch_size(height: int, width: int) -> int:
    env = (os.environ.get("VITUAL_GLYPHFILL_BATCH") or "").strip()
    if env.isdigit():
        return max(1, min(32, int(env)))
    px = max(1, int(height) * int(width))
    if px >= 1920 * 1080:
        return 8
    if px >= 1280 * 720:
        return 12
    return 16


def _resolve_glyphfill_plate(
    frame_bgr: Any,
    seed_box: dict[str, int],
    *,
    height: int,
    width: int,
) -> dict[str, int]:
    """ROI for glyphfill: keep seed width; optionally tighten Y from plate.

    ``find_black_caption_box`` often returns a *centered floating* plate that is
    narrower than the locate seed. Using that width drops left/right captions
    on other frames. Width stays on the seed; Y/H may tighten onto dark rows.
    """
    seed_x = max(0, int(seed_box["x"]))
    seed_y = max(0, int(seed_box["y"]))
    seed_w = max(1, int(seed_box["w"]))
    seed_h = max(1, int(seed_box["h"]))
    y0, x0 = seed_y, seed_x
    y1 = min(height, seed_y + seed_h)
    x1 = min(width, seed_x + seed_w)
    found = find_black_caption_box(frame_bgr, seed_box)
    if found is not None:
        fy0 = max(0, int(found["y"]))
        fy1 = min(height, fy0 + max(1, int(found["h"])))
        # Only accept height tighten when it still covers most of the seed band
        # or is a wide plate; never shrink X/W.
        if (fy1 - fy0) >= max(24, int(0.45 * seed_h)):
            pad = 8
            y0 = max(seed_y, fy0 - pad)
            y1 = min(seed_y + seed_h, fy1 + pad)
    return {
        "x": int(x0),
        "y": int(y0),
        "w": int(max(1, x1 - x0)),
        "h": int(max(1, y1 - y0)),
    }

def _glyph_black_kill_masks_torch(
    frames_bgr: Any,
    plate: dict[str, int],
    device: Any,
) -> tuple[Any, Any]:
    """Batched GPU ink+AA kill masks; thresholds match the CPU OpenCV path."""
    import torch
    import torch.nn.functional as F

    t = torch.from_numpy(frames_bgr).to(device=device, dtype=torch.float32)
    gray = 0.114 * t[..., 0] + 0.587 * t[..., 1] + 0.299 * t[..., 2]
    y0 = int(plate["y"])
    x0 = int(plate["x"])
    y1 = y0 + int(plate["h"])
    x1 = x0 + int(plate["w"])
    roi = gray[:, y0:y1, x0:x1]
    dark = (roi < 55.0).to(dtype=torch.float32)
    dark_f = F.avg_pool2d(
        dark.unsqueeze(1), kernel_size=(41, 17), stride=1, padding=(20, 8)
    ).squeeze(1)
    # Local-contrast strokes: pixels brighter than their LOCAL plate background,
    # as a SUPERSET of the global bright threshold (never loses current
    # detection). Catches dimmer strokes on a non-uniform opaque plate where a
    # single global level would miss them.
    local_bg = F.avg_pool2d(
        roi.unsqueeze(1), kernel_size=(21, 21), stride=1, padding=(10, 10)
    ).squeeze(1)
    bright = (roi > 110.0) | (roi > (local_bg + 45.0))
    aa = (roi > 45.0) & (roi <= 110.0)
    ink = bright & (dark_f > 0.40)
    fringe = aa & (dark_f > 0.55)
    kill = (ink | fringe).to(dtype=torch.float32)
    for _ in range(3):
        kill = F.max_pool2d(
            kill.unsqueeze(1), kernel_size=5, stride=1, padding=2
        ).squeeze(1)
    kill = kill * (dark_f > 0.35).to(dtype=torch.float32)
    # Opaque plate rows: any non-black pixel on a near-black row is glyph/AA.
    row_dark = dark.mean(dim=-1, keepdim=True)  # [N,H,1]
    plate_row = row_dark > 0.55
    on_plate = plate_row & (roi > 35.0) & (dark_f > 0.50)
    kill = torch.maximum(kill, on_plate.to(dtype=torch.float32))
    kill_b = kill > 0.5
    counts = kill_b.reshape(kill_b.shape[0], -1).sum(dim=1)
    full = torch.zeros(
        (frames_bgr.shape[0], frames_bgr.shape[1], frames_bgr.shape[2]),
        device=device,
        dtype=torch.bool,
    )
    full[:, y0:y1, x0:x1] = kill_b
    return full, counts


def encode_glyph_black_fill(
    src: Any,
    dest: Any,
    seed_box: dict[str, int],
) -> dict[str, Any]:
    """VSR-style black caption bar cleanup: paint glyph strokes solid black.

    Default: CUDA batched ink/AA masks (same thresholds as the former per-frame
    OpenCV loop). Falls back to CPU batch if CUDA is unavailable. RapidOCR is
    skipped on this route -- opaque black plates are covered by bright-on-dark ink.
    """
    from pathlib import Path

    import cv2
    import numpy as np

    src_p = Path(src)
    dest_p = Path(dest)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(src_p))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src_p}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    writing = dest_p.with_name(f"_{dest_p.stem}_glyphfill_writing.mp4")
    proc = _open_ffmpeg_writer(src_p, writing, width, height, fps)
    assert proc.stdin is not None

    use_cuda = False
    device = None
    try:
        import torch

        use_cuda = bool(torch.cuda.is_available())
        if use_cuda:
            device = torch.device("cuda:0")
            torch.cuda.empty_cache()
    except Exception:
        use_cuda = False
        device = None

    batch_n = _glyphfill_batch_size(height, width)
    if not use_cuda:
        batch_n = max(1, min(4, batch_n))
    frames = 0
    touched = 0
    painted_px = 0
    plate: dict[str, int] | None = None
    plate_refresh = max(
        30, int(os.environ.get("VITUAL_GLYPHFILL_PLATE_REFRESH", "60") or 60)
    )

    def _flush_batch(buf: list[Any]) -> None:
        nonlocal frames, touched, painted_px, plate
        if not buf:
            return
        arr = np.stack(buf, axis=0)
        if plate is None:
            plate = _resolve_glyphfill_plate(
                buf[0], seed_box, height=height, width=width
            )
        if use_cuda and device is not None:
            import torch

            with torch.no_grad():
                kill, counts = _glyph_black_kill_masks_torch(arr, plate, device)
                kill_cpu = kill.detach().cpu().numpy()
                counts_cpu = counts.detach().cpu().numpy()
                del kill, counts
        else:
            kill_cpu = np.zeros(arr.shape[:3], dtype=bool)
            counts_cpu = np.zeros((arr.shape[0],), dtype=np.int64)
            y0, x0 = int(plate["y"]), int(plate["x"])
            y1, x1 = y0 + int(plate["h"]), x0 + int(plate["w"])
            for i in range(arr.shape[0]):
                roi = arr[i, y0:y1, x0:x1]
                gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
                dark_f = cv2.blur((gray < 55.0).astype(np.float32), (41, 17))
                # Local-contrast strokes (superset of the global bright level):
                # brighter than the local plate background catches dimmer strokes
                # on a non-uniform opaque plate; never loses the global detection.
                local_bg = cv2.blur(gray, (21, 21))
                bright = (gray > 140.0) | (gray > (local_bg + 45.0))
                aa = (gray > 55.0) & (gray <= 140.0)
                kill = (bright & (dark_f > 0.45)) | (aa & (dark_f > 0.60))
                if bool(kill.any()):
                    kill_u8 = cv2.dilate(
                        kill.astype(np.uint8) * 255, np.ones((5, 5), np.uint8), 2
                    )
                    kill_u8 = cv2.bitwise_and(
                        kill_u8, (dark_f > 0.40).astype(np.uint8) * 255
                    )
                    km = kill_u8 > 0
                    kill_cpu[i, y0:y1, x0:x1] = km
                    counts_cpu[i] = int(km.sum())
        for i in range(arr.shape[0]):
            fr = arr[i]
            n_px = int(counts_cpu[i])
            if n_px > 0:
                fr = fr.copy()
                fr[kill_cpu[i]] = (0, 0, 0)
                painted_px += n_px
                touched += 1
            proc.stdin.write(np.ascontiguousarray(fr).tobytes())
            frames += 1
            if n and frames % 60 == 0:
                print(
                    f"[glyphfill] wrote {frames}/{n} touched={touched} px={painted_px} "
                    f"device={'cuda' if use_cuda else 'cpu'} batch={batch_n}",
                    flush=True,
                )
            if frames % plate_refresh == 0:
                plate = _resolve_glyphfill_plate(
                    buf[min(i, len(buf) - 1)],
                    seed_box,
                    height=height,
                    width=width,
                )

    print(
        f"[glyphfill] start device={'cuda' if use_cuda else 'cpu'} "
        f"batch={batch_n} box={seed_box}",
        flush=True,
    )
    try:
        buf: list[Any] = []
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            buf.append(fr)
            if len(buf) >= batch_n:
                _flush_batch(buf)
                buf = []
        _flush_batch(buf)
    except Exception:
        proc.kill()
        raise
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
        if use_cuda:
            try:
                import torch

                torch.cuda.empty_cache()
            except Exception:
                pass
    stderr = proc.communicate(timeout=300)[1]
    if proc.returncode != 0 or frames < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"glyph black fill failed ({proc.returncode}): {err}")
    _finalize_writing_mp4(writing, dest_p)
    return {
        "engine": "glyphfill",
        "mode": "glyph_solid_black_gpu" if use_cuda else "glyph_solid_black",
        "device": "cuda" if use_cuda else "cpu",
        "batch": int(batch_n),
        "frames": frames,
        "filled_frames": touched,
        "painted_px": painted_px,
        "bytes": dest_p.stat().st_size if dest_p.is_file() else 0,
        "seed_box": {k: int(seed_box[k]) for k in ("x", "y", "w", "h")},
        "plate_box": (
            {k: int(plate[k]) for k in ("x", "y", "w", "h")} if plate else None
        ),
    }


def encode_dialogue_spatial_fill(
    src: Any,
    dest: Any,
    seed_box: dict[str, int],
    *,
    pad: int = 25,
) -> dict[str, Any]:
    """Per-frame spatial fill for dialogue subtitles over a FLAT background.

    适用场景（2026-10-19 实测定稿）：字幕压在纯平色背景上（商品开箱粉底、白墙等）。
    STTN tiles 从时序邻帧取材，但同句字幕持续 ~3s、邻帧全有字 → 黑色鬼影；
    PS 中值堆栈要求背景静态（temporal_l1<=10），镜头缓动即被拒。
    本路线完全不做时序取材：逐帧用「与粉底参考色的颜色距离」出字形 mask
    （黑字核/白边/描边全部远离粉底色；medianBlur 局部背景在 65px 窄带内失效，
    字行占满带高时局部中值=字本身），mask 内直接填粉底色+固定位置微噪声
    （TELEA inpaint 会从 mask 边界 AA 过渡像素取色产生白晕，纯平背景直接填充更净）。
    """
    from pathlib import Path

    import cv2
    import numpy as np

    src_p = Path(src)
    dest_p = Path(dest)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(src_p))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src_p}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)  # 仅进度显示分母；cv2 帧数不可靠，不作逻辑依据
    cap.release()
    # 源用 ffmpeg 解码（与 verify_dehardsub 同解码器），保证帧数/逐帧对齐与自检一致；
    # cv2 读源帧数会与 ffmpeg 不一致（同文件 cv2=900 / ffmpeg=902），不可依赖 cv2 计数。
    from media_ops import find_ffmpeg

    ff = find_ffmpeg()
    rdr = subprocess.Popen(
        [ff, "-hide_banner", "-loglevel", "error", "-i", str(src_p),
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE, bufsize=-1,
    )
    assert rdr.stdout is not None
    _frame_bytes = width * height * 3

    x0 = max(0, int(seed_box["x"]))
    x1 = min(width, x0 + max(1, int(seed_box["w"])))
    y0 = max(0, int(seed_box["y"]) - pad)
    y1 = min(height, int(seed_box["y"]) + max(1, int(seed_box["h"])) + pad)
    bw, bh = x1 - x0, y1 - y0
    if bw < 8 or bh < 8:
        cap.release()
        raise RuntimeError(f"spatial fill box too small: {seed_box}")

    # 固定位置微噪声：帧间同位置噪声相同 → 无闪烁
    rng = np.random.RandomState(7)
    noise = rng.normal(0, 2.0, (bh, bw, 1)).astype(np.float32)

    writing = dest_p.with_name(f"_{dest_p.stem}_spatialfill_writing.mp4")
    proc = _open_ffmpeg_writer(src_p, writing, width, height, fps)
    assert proc.stdin is not None

    frames = 0
    touched = 0
    filled_px = 0
    skipped_flat = 0  # 无主导平色背景（bgm 采样不足）→ 不动
    skipped_gate = 0  # mask 占比超门卫 → 不动

    try:
        while True:
            buf = rdr.stdout.read(_frame_bytes)
            if len(buf) < _frame_bytes:
                break
            fr = np.frombuffer(buf, np.uint8).reshape(height, width, 3).copy()
            roi = fr[y0:y1, x0:x1]
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            s_ch = hsv[..., 1].astype(np.int16)
            v_ch = hsv[..., 2].astype(np.int16)
            bgm = (s_ch > 60) & (v_ch > 200)
            if float(bgm.mean()) < 0.20:
                skipped_flat += 1
                proc.stdin.write(np.ascontiguousarray(fr).tobytes())
                frames += 1
                continue
            bg = np.median(roi[bgm].reshape(-1, 3), axis=0).astype(np.float32)
            d = np.linalg.norm(roi.astype(np.float32) - bg.reshape(1, 1, 3), axis=2)
            m0 = d > 40
            m = cv2.dilate(m0.astype(np.uint8) * 255, np.ones((11, 11), np.uint8), 1)
            m = m > 0
            # 行级过滤：字幕行是稀疏笔画（密度 2–47%），内容行是实心块（密度 76–100%）。
            # 判据只靠【密度】区分：单行 masked 像素占比 >70% 判为实心内容块（黄衣/商品/
            # 彩条）整行剔除；<=70% 的稀疏行视为字幕笔画予以保留填充。
            # 不再用「白占比/彩占比」做行级门卫——彩色字幕（黄/蓝/红字）既无白核、彩占比
            # 又高，原 (row_white>=0.1)|(row_col<=0.3) 会整行误杀，导致字幕原样残留
            # （demo 实测 232/891 帧假残留，帧 300 仅 7/115 行过关）。白墙等无主导平色底
            # 已由上方 bgm_mean<0.20 的 flat-skip 拦掉，无需在此用颜色二次过滤。
            # 关键：密度判据作用于【未膨胀】的字形 mask(m0)，膨胀只用于实际填充。
            row_tot = m0.sum(axis=1)
            # 上限放宽到 0.98*bw：字幕常是「实心横幅 + 文字」整块（连排粗体/带底栏），单行
            # masked 占比可达 90%+，原 0.70 上限会把整行判为「实心内容块」整行剔除 → 字幕
            # 原样残留（帧 300 有 54 行 row_tot 高达 844/861）。真正需剔除的实心块由全局
            # frac>0.93 闸门兜底（整帧 >93% 被遮 → 跳过），无需在此用行密度误杀字幕。
            keep = (row_tot > 0.005 * bw) & (row_tot <= 0.98 * bw)
            m &= keep[:, None]
            frac = float(m.mean())
            if frac < 0.005 or frac > 0.93:
                skipped_gate += 1
                proc.stdin.write(np.ascontiguousarray(fr).tobytes())
                frames += 1
                continue
            fill = np.clip(bg.reshape(1, 1, 3) + noise, 0, 255).astype(np.uint8)
            out = roi.copy()
            out[m] = fill[m]
            fr = fr.copy()
            fr[y0:y1, x0:x1] = out
            filled_px += int(m.sum())
            touched += 1
            proc.stdin.write(np.ascontiguousarray(fr).tobytes())
            frames += 1
            if n and frames % 300 == 0:
                print(
                    f"[spatialfill] wrote {frames}/{n} touched={touched} "
                    f"px={filled_px} skip(flat={skipped_flat},gate={skipped_gate})",
                    flush=True,
                )
    except Exception:
        try:
            rdr.kill()
        except Exception:
            pass
        proc.kill()
        raise
    finally:
        try:
            rdr.kill()
        except Exception:
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass
    stderr = proc.communicate(timeout=300)[1]
    if proc.returncode != 0 or frames < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"dialogue spatial fill failed ({proc.returncode}): {err}")
    _finalize_writing_mp4(writing, dest_p)
    # 帧数严格对齐源：源用 ffmpeg 解码，frames 即真实帧数。writer 因音频时长扩展
    # 偶发多写 1–2 帧（copy 模式 -frames:v 无法裁掉，已验证失效），故用重新编码 +
    # select 取前 frames 帧，保证与 verify/合成逐帧不错位。
    try:
        _fixed = dest_p.with_name(dest_p.stem + "_framefix.mp4")
        _vf = f"select='lt(n\\,{frames})',setpts=N/FRAME_RATE/TB"
        _vcodec, _vargs = _video_encoder_for(ff, width, height)
        _r = subprocess.run(
            [
                ff,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(dest_p),
                "-vf",
                _vf,
                "-vsync",
                "0",
                "-c:v",
                _vcodec,
                *_vargs,
                "-c:a",
                "copy",
                str(_fixed),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if _fixed.is_file() and _fixed.stat().st_size > 800:
            _fixed.replace(dest_p)
        else:
            print(
                f"[spatialfill] frame-align warn: trim to {frames} failed: {(_r.stderr or '')[-200:]}",
                flush=True,
            )
    except Exception as _e:  # noqa: BLE001
        print(f"[spatialfill] frame-align skipped: {_e}", flush=True)
    return {
        "engine": "spatialfill",
        "mode": "flat_bg_spatial_fill",
        "frames": frames,
        "filled_frames": touched,
        "filled_px": filled_px,
        "skipped_flat": skipped_flat,
        "skipped_gate": skipped_gate,
        "bytes": dest_p.stat().st_size if dest_p.is_file() else 0,
        "seed_box": {k: int(seed_box[k]) for k in ("x", "y", "w", "h")},
        "roi": {"x": int(x0), "y": int(y0), "w": int(bw), "h": int(bh)},
    }


def glyph_hole_adaptive(bgr: Any, box: dict[str, int]) -> Any:
    """自适应字幕字形 mask（全帧坐标，含描边，用于 STTN 字形级 hole）。

    同时支持深底白字 / 白底黑字 / 彩色描边：
      - 主背景 = 框内灰度中位数（字幕框内背景占主导）
      - 字形 = 与背景对比 > 60 的高对比像素（亮字暗字都抓）
      - 形态学闭运算连接笔画 + 膨胀盖住描边
      - 占比门卫：高对比像素 > 55% 视为复杂场景（检测不可信）→ 返回空 mask，
        宁可漏字也不能把整框当洞重画（涂毁画面）。
    """
    import cv2
    import numpy as np

    y0 = max(0, int(box["y"]))
    x0 = max(0, int(box["x"]))
    y1 = min(bgr.shape[0], y0 + max(1, int(box["h"])))
    x1 = min(bgr.shape[1], x0 + max(1, int(box["w"])))
    if y1 <= y0 or x1 <= x0:
        return np.zeros(bgr.shape[:2], dtype=np.uint8)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    roi = gray[y0:y1, x0:x1]
    bg_med = float(np.median(roi))
    # 阈值 60→42：原阈值把白字笔画的 AA 柔边(gray~165-185)漏在洞外，
    # 导致 STTN 不重绘这些像素 → 残留白色鬼影(实测 clip15 去字幕后底部字幕区
    # 仍有 faint 白痕)。降到 42 把柔边纳入洞，配合下方膨胀盖描边 → 彻底清除。
    # 占比门卫 0.55 仍兜底：复杂场景宁可漏字也不误毁画面。
    contrast = np.abs(roi - bg_med) > 42.0
    # Yellow title paint (may sit near bg luminance after blur).
    bch = bgr[y0:y1, x0:x1, 0].astype(np.int16)
    gch = bgr[y0:y1, x0:x1, 1].astype(np.int16)
    rch = bgr[y0:y1, x0:x1, 2].astype(np.int16)
    yellow = (rch > 170) & (gch > 150) & (bch < 150) & ((rch + gch) > (2 * bch + 30))
    contrast |= yellow
    frac = float(contrast.mean())
    if frac > 0.55 or frac < 0.005:
        # 复杂场景（检测不可信）或框内几乎无字形 → 不动背景
        return np.zeros(bgr.shape[:2], dtype=np.uint8)
    m = (contrast.astype(np.uint8) * 255)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=3)
    full = np.zeros(bgr.shape[:2], dtype=np.uint8)
    full[y0:y1, x0:x1] = m
    return full


def flat_band_glyph_px(bgr: Any, box: dict[str, int]) -> int:
    """字形像素数（0=无文字）。flat 带路由用：有字形 → tiles 优于 TBE+lerp。

    TBE+逐字形 lerp 在 flat 带上残留幽灵笔画+水平涂抹（实测残差 25.6 vs
    tiles+字形 hole 7.7, instances/demosaic_test），故 flat 带一旦检出字形
    应回落 tiles；纯 flat 带保留 TBE 快路径。
    """
    import numpy as np

    try:
        hole = glyph_hole_adaptive(bgr, box)
        return int(np.count_nonzero(hole))
    except Exception:  # noqa: BLE001
        return 0


def glyph_mask_hybrid(
    bgr: Any,
    box: dict[str, int],
    *,
    pre_detected: list[dict[str, int]] | None = None,
) -> Any:
    """通用字形 mask（VSR 同款两级定位）：

    1) OCR 行 bbox 限定「哪里有字」（rapidocr，通用：不依赖底色/描边假设，
       且天然排除火焰/人物轮廓等高对比非文字内容）；
    2) bbox 内再做笔画细化（局部中位数 ±50 对比 + 闭运算 + 膨胀盖描边），
       洞贴合笔画而非整行矩形——矩形大洞会让 STTN 产生大块生成伪影
       （实测 bbox 直挖出现大团橙色涂抹）。
    OCR 不可用/无检出 → 回退 glyph_hole_adaptive（全框对比度）。
    """
    import cv2
    import numpy as np

    try:
        from glyph_detect import GlyphDetectUnavailable, detect_text_boxes
    except ImportError:
        return glyph_hole_adaptive(bgr, box)
    if pre_detected is not None:
        # 复用调用方已做的整帧 OCR：仅保留与 box 相交的行，并裁剪到交集
        # （与 detect_text_boxes 的 limit_box 行为一致）。
        boxes: list[dict[str, int]] = []
        for b in pre_detected:
            ix0 = max(b["x"], box["x"])
            iy0 = max(b["y"], box["y"])
            ix1 = min(b["x"] + b["w"], box["x"] + box["w"])
            iy1 = min(b["y"] + b["h"], box["y"] + box["h"])
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            boxes.append({"x": ix0, "y": iy0, "w": ix1 - ix0, "h": iy1 - iy0})
    else:
        try:
            boxes = detect_text_boxes(bgr, score_thr=0.5, limit_box=box)
        except GlyphDetectUnavailable:
            return glyph_hole_adaptive(bgr, box)
        except Exception:  # noqa: BLE001 — 单帧 OCR 失败不中断
            return glyph_hole_adaptive(bgr, box)
    if not boxes:
        # 框内确无文本行：不挖洞（宁可漏字也不误毁画面）
        return np.zeros(bgr.shape[:2], dtype=np.uint8)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    H, W = gray.shape
    mask = np.zeros((H, W), dtype=np.uint8)
    pad = 4
    for b in boxes:
        x0 = max(0, b["x"] - pad)
        y0 = max(0, b["y"] - pad)
        x1 = min(W, b["x"] + b["w"] + pad)
        y1 = min(H, b["y"] + b["h"] + pad)
        roi = gray[y0:y1, x0:x1]
        if roi.size == 0:
            continue
        med = float(np.median(roi))
        # 阈值 50→35：同 glyph_hole_adaptive，把白字笔画 AA 柔边纳入洞，
        # 否则 STTN 不重绘 → 残留白色鬼影。膨胀 iter 2→3 盖描边。
        strokes = (np.abs(roi - med) > 35.0).astype(np.uint8) * 255
        strokes = cv2.morphologyEx(strokes, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
        strokes = cv2.dilate(strokes, np.ones((3, 3), np.uint8), iterations=3)
        if int(strokes.max()) == 0:
            # 细化后为空（bbox 内全是背景）→ 保留矩形洞兜底，确保该行被处理
            strokes = np.full_like(strokes, 255)
        mask[y0:y1, x0:x1] = np.maximum(mask[y0:y1, x0:x1], strokes)
    return mask


# ---------------------------------------------------------------------------
# 视频编码器选择：优先 h264_nvenc（GPU 加速，缓解帧 I/O 瓶颈），不可用时回退 libx264
# ---------------------------------------------------------------------------
_VIDEO_ENC_CACHE: dict = {}
_VIDEO_ENC_LOCK = None  # 懒加载 threading，避免顶层开销


def _video_encoder_for(ffmpeg: str, w: int, h: int) -> tuple[str, list[str]]:
    """选取 H.264 编码器与码控参数（质量对标 libx264 -crf 18）。

    优先 ``h264_nvenc``（GPU 编码，省 CPU 帧 I/O 瓶颈）；若本机不可用
    （缺驱动/分辨率过小/会话超限）则自动回退 ``libx264``。可用环境变量
    ``VITUAL_VIDEO_ENCODER`` 强制：``nvenc`` / ``libx264`` / ``auto``（默认）。
    结果按 (ffmpeg, w, h) 缓存，避免逐段重复探测。
    """
    import os
    import threading

    global _VIDEO_ENC_LOCK
    if _VIDEO_ENC_LOCK is None:
        _VIDEO_ENC_LOCK = threading.Lock()
    key = (ffmpeg, int(w or 0), int(h or 0))
    with _VIDEO_ENC_LOCK:
        cached = _VIDEO_ENC_CACHE.get(key)
        if cached is not None:
            return cached
    libx264 = ("libx264", ["-preset", "veryfast", "-crf", "18"])
    override = (os.environ.get("VITUAL_VIDEO_ENCODER") or "auto").strip().lower()
    if override == "libx264":
        res = libx264
    else:
        res = _try_nvenc(ffmpeg, w, h) or libx264
        if override == "nvenc" and res[0] != "h264_nvenc":
            print("[encoder] VITUAL_VIDEO_ENCODER=nvenc 不可用，回退 libx264", flush=True)
    with _VIDEO_ENC_LOCK:
        _VIDEO_ENC_CACHE[key] = res
    return res


def _try_nvenc(ffmpeg: str, w: int, h: int):
    """自测 h264_nvenc 能否在本机真实编码该分辨率；成功返回 (codec, 码控参数)。"""
    import subprocess

    tw, th = max(2, (int(w or 2) // 2) * 2), max(2, (int(h or 2) // 2) * 2)
    probe = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{tw}x{th}",
        "-i", "pipe:0", "-c:v", "h264_nvenc", "-preset", "p1",
        "-rc", "vbr", "-cq", "18", "-pix_fmt", "yuv420p", "-f", "mp4", "-",
    ]
    try:
        payload = b"\x80\x80\x80" * (tw * th) + b"\x10\x10\x10" * (tw * th)  # 2 帧
        p = subprocess.run(probe, input=payload, capture_output=True, timeout=30)
        if p.returncode != 0 or not p.stdout:
            return None
        v = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "-", "-f", "null", "-"],
            input=p.stdout, capture_output=True, timeout=30,
        )
        if v.returncode != 0:
            return None
        return ("h264_nvenc", ["-preset", "p1", "-rc", "vbr", "-cq", "18"])
    except Exception:
        return None


def _open_ffmpeg_writer(
    src: Path,
    dest: Path,
    frame_w: int,
    frame_h: int,
    fps: float,
) -> Any:
    from media_ops import DEFAULT_AUDIO_K, find_ffmpeg, has_audio_stream

    ffmpeg = find_ffmpeg()
    # ~1s keyframes so the first media fragment lands quickly (fMP4 playability).
    gop = max(12, min(60, int(round(float(fps) or 25.0)) or 25))
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
        f"{frame_w}x{frame_h}",
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
    codec, enc_args = _video_encoder_for(ffmpeg, frame_w, frame_h)
    cmd.extend(["-c:v", codec, *enc_args, "-pix_fmt", "yuv420p"])
    cmd.extend(
        [
            "-g",
            str(gop),
            "-keyint_min",
            str(gop),
            "-movflags",
            _WRITE_MOVFLAGS,
            "-flush_packets",
            "1",
            "-fps_mode",
            "passthrough",
            "-shortest",
            str(dest),
        ]
    )
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def _finalize_writing_mp4(writing: Path, dest: Path) -> None:
    """Remux fragmented writing MP4 → normal +faststart dest (Windows-lock safe)."""
    import shutil
    import time

    from media_ops import find_ffmpeg

    if not writing.is_file() or writing.stat().st_size < 800:
        raise RuntimeError(f"encode produced empty file: {writing}")

    ffmpeg = find_ffmpeg()
    staged = dest.with_name(dest.stem + "_finalizing.mp4")
    if staged.is_file():
        staged.unlink(missing_ok=True)
    remux = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(writing),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(staged),
        ],
        capture_output=True,
        timeout=600,
    )
    if remux.returncode == 0 and staged.is_file() and staged.stat().st_size >= 800:
        try:
            writing.unlink(missing_ok=True)
        except OSError:
            pass
        move_from = staged
    else:
        err = (remux.stderr or b"").decode("utf-8", errors="replace")[:300]
        print(f"[sttn] remux skipped ({remux.returncode}): {err}", flush=True)
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        move_from = writing

    last_err: OSError | None = None
    for _ in range(8):
        try:
            if dest.is_file():
                dest.unlink()
            move_from.replace(dest)
            last_err = None
            break
        except OSError as exc:
            last_err = exc
            time.sleep(0.4)
    if last_err is not None:
        try:
            shutil.copy2(move_from, dest)
            if move_from.resolve() != writing.resolve():
                move_from.unlink(missing_ok=True)
            writing.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"wrote {writing} but could not replace {dest}: {exc}"
            ) from exc


