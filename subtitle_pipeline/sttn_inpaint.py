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


def default_ckpt() -> Path:
    env = (os.environ.get("VITUAL_STTN_CKPT") or "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "models" / "sttn.pth"


def sttn_tiles(
    box: dict[str, int],
    frame_w: int,
    frame_h: int,
    *,
    overlap: int = 64,
) -> list[dict[str, int]]:
    """1:1 432×240 tiles covering the caption box (no downscale)."""
    bx = max(0, int(box["x"]))
    by = max(0, int(box["y"]))
    bw = max(2, int(box["w"]))
    bh = max(2, int(box["h"]))
    y1 = min(frame_h, by + bh + 8)
    y0 = max(0, y1 - STTN_H)
    th = min(STTN_H, frame_h - y0)
    x_lo = max(0, bx - 8)
    x_hi = min(frame_w, bx + bw + 8)
    span = max(1, STTN_W - overlap)
    tiles: list[dict[str, int]] = []
    x = x_lo
    while True:
        tx = 0 if frame_w < STTN_W else min(x, frame_w - STTN_W)
        tw = min(STTN_W, frame_w - tx)
        rec = {"x": int(tx), "y": int(y0), "w": int(tw), "h": int(th)}
        if not tiles or rec != tiles[-1]:
            tiles.append(rec)
        if tx + tw >= x_hi or tx + tw >= frame_w:
            break
        nxt = tx + span
        if nxt <= tx:
            break
        x = nxt
    return tiles


def sttn_view(box: dict[str, int], frame_w: int, frame_h: int) -> tuple[dict[str, int], dict[str, int]]:
    """First native tile + caption box in tile coords (tests / debug)."""
    tiles = sttn_tiles(box, frame_w, frame_h)
    crop = tiles[0]
    hole = {
        "x": max(0, int(box["x"]) - crop["x"]),
        "y": max(0, int(box["y"]) - crop["y"]),
        "w": min(int(box["w"]), crop["w"]),
        "h": min(int(box["h"]), crop["h"]),
    }
    return crop, hole


def extract_tile(frame: Any, tile: dict[str, int]) -> Any:
    """Crop tile and pad to STTN_W×STTN_H."""
    import numpy as np

    x, y, w, h = int(tile["x"]), int(tile["y"]), int(tile["w"]), int(tile["h"])
    patch = frame[y : y + h, x : x + w]
    if patch.shape[0] == STTN_H and patch.shape[1] == STTN_W:
        return patch
    canvas = np.zeros((STTN_H, STTN_W, patch.shape[2]), dtype=patch.dtype)
    canvas[: patch.shape[0], : patch.shape[1]] = patch
    return canvas


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
    row_ok = mid.mean(axis=1) < 0.40

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
            area = float(h * w)
            cx = 0.5 * float(cx0 + cx1)
            center_bonus = 1.0 + 0.45 * (
                1.0 - abs(cx - seed_cx) / max(1.0, seed_cx)
            )
            ink_bonus = 1.0 + 60.0 * stroke_frac
            score = area * dark_frac * ink_bonus * center_bonus
            if score > best_score:
                best_score = score
                best = {
                    "x": int(x0 + cx0),
                    "y": int(y0 + ry0),
                    "w": w,
                    "h": h,
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


def fill_caption_black_bar(
    bgr: Any,
    box: dict[str, int],
    *,
    color: tuple[int, int, int] = (0, 0, 0),
) -> Any:
    """Paint the caption box solid (default black) — correct for black letterbox bars."""
    import numpy as np

    out = bgr.copy()
    y = max(0, int(box["y"]))
    x = max(0, int(box["x"]))
    h = max(1, int(box["h"]))
    w = max(1, int(box["w"]))
    H, W = out.shape[:2]
    y1 = min(H, y + h)
    x1 = min(W, x + w)
    if y1 <= y or x1 <= x:
        return out
    out[y:y1, x:x1] = np.array(color, dtype=np.uint8)
    return out


def erase_glyphs_on_black_bar(
    bgr: Any,
    seed: dict[str, int],
    *,
    color: tuple[int, int, int] = (0, 0, 0),
) -> tuple[Any, int]:
    """Clear black caption plates that carry bright glyph strokes.

    When a tightened wide plate box is found, solid-fill it. Otherwise only
    remove high-confidence ink strokes (no flood into dark food/sauce).
    """
    import cv2
    import numpy as np

    out = bgr.copy()
    H, W = out.shape[:2]
    y0 = max(0, int(seed["y"]))
    x0 = max(0, int(seed["x"]))
    y1 = min(H, y0 + max(1, int(seed["h"])))
    x1 = min(W, x0 + max(1, int(seed["w"])))
    if y1 <= y0 or x1 <= x0:
        return out, 0

    found = find_black_caption_box(out, seed)
    if found is not None:
        aspect = float(found["w"]) / float(max(1, found["h"]))
        if aspect >= 2.0 and int(found["w"]) >= 120:
            out2 = fill_caption_black_bar(out, found, color=color)
            return out2, int(found["w"]) * int(found["h"])

    roi = out[y0:y1, x0:x1]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
    bright = gray > 170.0
    dark_soft = gray < 50.0
    dark_f = cv2.blur(dark_soft.astype(np.float32), (41, 17))
    ink = bright & (dark_f > 0.55)
    if not bool(ink.any()):
        return out, 0
    glyph_kill = cv2.dilate(ink.astype(np.uint8) * 255, np.ones((3, 3), np.uint8), 2)
    glyph_kill = cv2.bitwise_and(glyph_kill, (dark_f > 0.55).astype(np.uint8) * 255)
    plate = glyph_kill > 0
    n = int(plate.sum())
    if n <= 0:
        return out, 0
    roi[plate] = np.array(color, dtype=np.uint8)
    out[y0:y1, x0:x1] = roi
    return out, n


def probe_dialogue_black_bar(
    video: Any,
    box: dict[str, int],
    *,
    samples: int = 7,
) -> dict[str, Any]:
    """Sample frames; decide whether dialogue should use solid black-bar fill."""
    import cv2

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return {"ok": False, "hits": 0, "samples": 0, "box": None}
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    hits = 0
    found_boxes: list[dict[str, int]] = []
    idxs = (
        [0]
        if n <= 1
        else [int(round(i * (n - 1) / max(1, samples - 1))) for i in range(samples)]
    )
    try:
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
            ok, fr = cap.read()
            if not ok:
                continue
            found = find_black_caption_box(fr, box)
            if found is not None:
                hits += 1
                found_boxes.append(found)
    finally:
        cap.release()
    checked = len(idxs)
    ok = hits >= max(2, int(round(0.55 * max(1, checked))))
    rep = None
    if found_boxes:
        # Median box is stabler than min/max union (avoids one oversized hit).
        xs = sorted(b["x"] for b in found_boxes)
        ys = sorted(b["y"] for b in found_boxes)
        x2s = sorted(b["x"] + b["w"] for b in found_boxes)
        y2s = sorted(b["y"] + b["h"] for b in found_boxes)
        mid = len(found_boxes) // 2
        x0, y0, x1, y1 = xs[mid], ys[mid], x2s[mid], y2s[mid]
        rep = {
            "x": int(x0),
            "y": int(y0),
            "w": int(max(1, x1 - x0)),
            "h": int(max(1, y1 - y0)),
        }
    return {
        "ok": bool(ok),
        "hits": int(hits),
        "samples": int(checked),
        "box": rep,
        "fps": fps,
        "frames": n,
    }


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
       - ``flat``: flat UI strip → Temporal Background Exposure
       - ``tiles``: textured live-action → STTN tiles with glyph holes
    """
    import cv2

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
    checked = 0
    found_boxes: list[dict[str, int]] = []
    try:
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
            ok, fr = cap.read()
            if not ok:
                continue
            checked += 1
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
                found_boxes.append(seed)
                continue
            if band_is_flat(fr, seed):
                flat_hits += 1
    finally:
        cap.release()

    need = max(2, int(round(0.55 * max(1, checked))))
    tight = seed
    if found_boxes:
        xs = sorted(b["x"] for b in found_boxes)
        ys = sorted(b["y"] for b in found_boxes)
        x2s = sorted(b["x"] + b["w"] for b in found_boxes)
        y2s = sorted(b["y"] + b["h"] for b in found_boxes)
        mid = len(found_boxes) // 2
        x0, y0, x1, y1 = xs[mid], ys[mid], x2s[mid], y2s[mid]
        cand = {
            "x": int(x0),
            "y": int(y0),
            "w": int(max(1, x1 - x0)),
            "h": int(max(1, y1 - y0)),
        }
        if float(cand["w"]) / float(max(1, cand["h"])) >= 2.0:
            tight = cand

    if black_hits >= need:
        route = "glyph_black"
    elif flat_hits >= need:
        route = "flat"
    else:
        route = "tiles"
    return {
        "ok": True,
        "route": route,
        "box": tight,
        "seed_box": seed,
        "black_hits": int(black_hits),
        "flat_hits": int(flat_hits),
        "samples": int(checked),
    }


def encode_glyph_black_fill(
    src: Any,
    dest: Any,
    seed_box: dict[str, int],
) -> dict[str, Any]:
    """VSR-style black caption bar cleanup: paint *glyph strokes* solid black.

    For opaque black letterbox/caption plates the recoverable background *is*
    black. Whole-box STTN/crop/fill look like paint; Temporal TBE often leaves
    glyph ghosts when text is dense. Glyph-only solid fill matches the plate.
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
    frames = 0
    touched = 0
    painted_px = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            mask = glyph_mask_hybrid(fr, seed_box)
            # Tighten to opaque plate when possible — seed locate boxes often
            # include a strip of food above the bar.
            found = find_black_caption_box(fr, seed_box)
            work = found if found is not None else seed_box
            aspect = float(work["w"]) / float(max(1, work["h"]))
            if found is None or aspect < 2.0:
                work = seed_box
            y0 = max(0, int(work["y"]))
            x0 = max(0, int(work["x"]))
            y1 = min(height, y0 + max(1, int(work["h"])))
            x1 = min(width, x0 + max(1, int(work["w"])))
            # Restrict OCR mask to the plate; add bright-on-dark ink fallback.
            plate = np.zeros(fr.shape[:2], dtype=np.uint8)
            plate[y0:y1, x0:x1] = 255
            mask = cv2.bitwise_and(mask, plate)
            roi = fr[y0:y1, x0:x1]
            if roi.size:
                gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
                bright = gray > 155.0
                dark_f = cv2.blur((gray < 55.0).astype(np.float32), (41, 17))
                ink = bright & (dark_f > 0.50)
                if bool(ink.any()):
                    ink_u8 = cv2.dilate(
                        ink.astype(np.uint8) * 255, np.ones((5, 5), np.uint8), 2
                    )
                    # Never paint midtone food: keep only high dark support.
                    ink_u8 = cv2.bitwise_and(
                        ink_u8, (dark_f > 0.45).astype(np.uint8) * 255
                    )
                    mask[y0:y1, x0:x1] = np.maximum(mask[y0:y1, x0:x1], ink_u8)
            n_px = int((mask > 0).sum())
            if n_px > 0:
                fr = fr.copy()
                fr[mask > 0] = (0, 0, 0)
                painted_px += n_px
                touched += 1
            proc.stdin.write(np.ascontiguousarray(fr).tobytes())
            frames += 1
            if n and frames % 60 == 0:
                print(
                    f"[glyphfill] wrote {frames}/{n} touched={touched} px={painted_px}",
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
    stderr = proc.communicate(timeout=300)[1]
    if proc.returncode != 0 or frames < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"glyph black fill failed ({proc.returncode}): {err}")
    _finalize_writing_mp4(writing, dest_p)
    return {
        "engine": "glyphfill",
        "mode": "glyph_solid_black",
        "frames": frames,
        "filled_frames": touched,
        "painted_px": painted_px,
        "bytes": dest_p.stat().st_size if dest_p.is_file() else 0,
        "seed_box": {k: int(seed_box[k]) for k in ("x", "y", "w", "h")},
    }


def encode_black_caption_fill(
    src: Any,
    dest: Any,
    seed_box: dict[str, int],
    *,
    fallback_box: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Per-frame: fill black caption plates under white glyphs (no STTN)."""
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
    writing = dest_p.with_name(f"_{dest_p.stem}_writing.mp4")
    proc = _open_ffmpeg_writer(src_p, writing, width, height, fps)
    assert proc.stdin is not None
    frames = 0
    touched = 0
    painted_px = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            fr2, n_px = erase_glyphs_on_black_bar(fr, seed_box)
            if n_px <= 0 and fallback_box is not None:
                fr2, n_px = erase_glyphs_on_black_bar(fr, fallback_box)
            if n_px > 0:
                painted_px += int(n_px)
            if n_px > 80:
                touched += 1
            proc.stdin.write(np.ascontiguousarray(fr2).tobytes())
            frames += 1
            if n and frames % 60 == 0:
                print(
                    f"[blackfill] wrote {frames}/{n} touched={touched} px={painted_px}",
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
    stderr = proc.communicate(timeout=300)[1]
    if proc.returncode != 0 or frames < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"black caption fill failed ({proc.returncode}): {err}")
    _finalize_writing_mp4(writing, dest_p)
    return {
        "engine": "blackfill",
        "mode": "erase_glyphs_on_black",
        "frames": frames,
        "filled_frames": touched,
        "painted_px": painted_px,
        "bytes": dest_p.stat().st_size if dest_p.is_file() else 0,
        "seed_box": {k: int(seed_box[k]) for k in ("x", "y", "w", "h")},
        "fallback_box": (
            {k: int(fallback_box[k]) for k in ("x", "y", "w", "h")}
            if fallback_box is not None
            else None
        ),
    }


def encode_black_bar_crop(
    src: Any,
    dest: Any,
    bar_box: dict[str, int],
    *,
    out_w: int | None = None,
    out_h: int | None = None,
) -> dict[str, Any]:
    """Crop away an opaque bottom caption bar and scale back to original size.

    Black letterbox captions have no recoverable background — fill/STTN both look
    like black paint. Cropping the bar and zooming the remaining frame is the
    clean visual for this layout.
    """
    from pathlib import Path

    import cv2

    from media_ops import find_ffmpeg

    src_p = Path(src)
    dest_p = Path(dest)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(src_p))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src_p}")
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    if width < 16 or height < 16:
        raise RuntimeError(f"bad video size {width}x{height}")

    y0 = max(0, min(height - 2, int(bar_box.get("y", height))))
    # Keep a couple pixels of margin above the bar so the cut is not mid-glyph.
    keep_h = max(16, min(height - 2, y0 - 2))
    if keep_h % 2:
        keep_h -= 1
    keep_h = max(16, keep_h)
    ow = int(out_w or width)
    oh = int(out_h or height)
    if ow % 2:
        ow -= 1
    if oh % 2:
        oh -= 1
    # Crop top keep_h, then stretch back to original canvas (avoids letterbox pad).
    vf = f"crop=iw:{keep_h}:0:0,scale={ow}:{oh}"
    ffmpeg = find_ffmpeg()
    tmp = dest_p.with_name(f"_{dest_p.stem}_crop_tmp.mp4")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src_p),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(tmp),
    ]
    r = __import__("subprocess").run(cmd, capture_output=True)
    if r.returncode != 0 or not tmp.is_file():
        err = (r.stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"black-bar crop failed: {err}")
    tmp.replace(dest_p)
    return {
        "engine": "crop",
        "mode": "black_bar_crop_scale",
        "frames": n,
        "keep_h": keep_h,
        "out_size": f"{ow}x{oh}",
        "bar_box": {k: int(bar_box[k]) for k in ("x", "y", "w", "h") if k in bar_box},
        "bytes": dest_p.stat().st_size if dest_p.is_file() else 0,
        "vf": vf,
    }


def horiz_lerp_fill(bgr: Any, hole_u8: Any) -> Any:
    """Fill hole runs by lerping nearest clean left/right pixels (keeps horizontal grain)."""
    import numpy as np

    out = bgr.astype(np.float32).copy()
    hmask = hole_u8 > 0
    for y in np.where(hmask.any(axis=1))[0]:
        row_h = hmask[y]
        width = int(row_h.size)
        x = 0
        while x < width:
            if not row_h[x]:
                x += 1
                continue
            x0 = x
            while x < width and row_h[x]:
                x += 1
            x1 = x
            left = out[y, x0 - 1] if x0 > 0 else None
            right = out[y, x1] if x1 < width else None
            if left is None and right is None:
                continue
            if left is None:
                left = right
            if right is None:
                right = left
            nseg = x1 - x0
            for i, xi in enumerate(range(x0, x1)):
                t = (i + 1) / (nseg + 1.0)
                out[y, xi] = left * (1.0 - t) + right * t
    return np.clip(out, 0, 255).astype(np.uint8)


def text_hole_parts(bgr: Any, box: dict[str, int]) -> list[Any]:
    """Per-glyph holes (core + dark outline). Avoid one sausage that lerps into a smear."""
    import cv2
    import numpy as np

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    y0 = max(0, int(box["y"]))
    x0 = max(0, int(box["x"]))
    y1 = min(gray.shape[0], y0 + max(1, int(box["h"])))
    x1 = min(gray.shape[1], x0 + max(1, int(box["w"])))
    if y1 <= y0 or x1 <= x0:
        return []
    bg_est = float(np.percentile(gray[y0:y1, x0:x1], 40))
    thr = max(170.0, min(210.0, bg_est + 90.0))
    bright = gray >= thr
    near = cv2.dilate(bright.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1) > 0
    bright |= (gray >= max(140.0, thr - 35.0)) & near
    # Strong chroma only (avatars / icons). Keep yellow/white caption paint —
    # yellow outline titles were wiped by the old |R-G|/|R-B| gate.
    b = bgr[:, :, 0].astype(np.int16)
    g = bgr[:, :, 1].astype(np.int16)
    r = bgr[:, :, 2].astype(np.int16)
    yellow = (
        (r > 170)
        & (g > 150)
        & (b < 150)
        & ((r + g) > (2 * b + 30))
    )
    bright |= yellow
    strong = (np.abs(r - g) > 40) | (np.abs(g - b) > 40) | (np.abs(r - b) > 40)
    caption_paint = yellow | (gray >= thr)
    strong_ui = strong & (~caption_paint)
    near_ui = cv2.dilate(strong_ui.astype(np.uint8), np.ones((5, 5), np.uint8), iterations=1) > 0
    bright[near_ui] = False
    clip = np.zeros(gray.shape, dtype=np.uint8)
    clip[y0:y1, x0:x1] = 255
    core = cv2.bitwise_and((bright.astype(np.uint8) * 255), clip)
    n_cc, labels, stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    darker = gray < (bg_est - 8.0)
    parts: list[Any] = []
    for i in range(1, n_cc):
        if int(stats[i, cv2.CC_STAT_AREA]) < 8:
            continue
        comp = (labels == i).astype(np.uint8) * 255
        dil = cv2.dilate(comp, np.ones((3, 3), np.uint8), iterations=3)
        local = np.zeros_like(core)
        local[(comp > 0) | ((dil > 0) & darker)] = 255
        local = cv2.bitwise_and(local, clip)
        local[near_ui] = 0
        if int(local.max()) > 0:
            parts.append(local)
    return parts


def text_hole_for_tile(tile_bgr: Any, box: dict[str, int], tile: dict[str, int]) -> Any:
    """Caption-box hole inside this tile — no per-glyph mask."""
    import numpy as np

    h, w = tile_bgr.shape[:2]
    hole = np.zeros((h, w), dtype=np.uint8)
    bx0 = int(box["x"])
    by0 = int(box["y"])
    bx1 = bx0 + max(1, int(box["w"]))
    by1 = by0 + max(1, int(box["h"]))
    tx0 = int(tile["x"])
    ty0 = int(tile["y"])
    tx1 = tx0 + int(tile["w"])
    ty1 = ty0 + int(tile["h"])
    ix0 = max(bx0, tx0)
    iy0 = max(by0, ty0)
    ix1 = min(bx1, tx1)
    iy1 = min(by1, ty1)
    if ix1 > ix0 and iy1 > iy0:
        hole[iy0 - ty0 : iy1 - ty0, ix0 - tx0 : ix1 - tx0] = 255
    return hole


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
    contrast = np.abs(roi - bg_med) > 60.0
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
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=2)
    full = np.zeros(bgr.shape[:2], dtype=np.uint8)
    full[y0:y1, x0:x1] = m
    return full


def glyph_hole_for_tile(glyph_union: Any, tile: dict[str, int]) -> Any:
    """把全帧字形 mask 裁剪/填充到 STTN tile 坐标。"""
    import numpy as np

    x, y, w, h = int(tile["x"]), int(tile["y"]), int(tile["w"]), int(tile["h"])
    sub = glyph_union[y : y + h, x : x + w]
    if sub.shape[0] == STTN_H and sub.shape[1] == STTN_W:
        return sub
    pad = np.zeros((STTN_H, STTN_W), dtype=np.uint8)
    pad[: sub.shape[0], : sub.shape[1]] = sub
    return pad


def glyph_mask_hybrid(bgr: Any, box: dict[str, int]) -> Any:
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
        strokes = (np.abs(roi - med) > 50.0).astype(np.uint8) * 255
        strokes = cv2.morphologyEx(strokes, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
        strokes = cv2.dilate(strokes, np.ones((3, 3), np.uint8), iterations=2)
        if int(strokes.max()) == 0:
            # 细化后为空（bbox 内全是背景）→ 保留矩形洞兜底，确保该行被处理
            strokes = np.full_like(strokes, 255)
        mask[y0:y1, x0:x1] = np.maximum(mask[y0:y1, x0:x1], strokes)
    return mask


def fill_caption_spatial(bgr: Any, box: dict[str, int]) -> Any:
    """Native-res caption restore for flat UI bars (per-glyph horizontal lerp)."""
    import cv2
    import numpy as np

    out = bgr.copy()
    parts = text_hole_parts(out, box)
    if not parts:
        return out
    protect = _caption_protect_mask(out.shape[:2], box)
    union = np.zeros(out.shape[:2], dtype=np.uint8)
    for part in parts:
        part = part.copy()
        part[protect] = 0
        if int(part.max()) == 0:
            continue
        out = horiz_lerp_fill(out, part)
        union = np.maximum(union, part)
    y0 = max(0, int(box["y"]))
    x0 = max(0, int(box["x"]))
    y1 = min(out.shape[0], y0 + max(1, int(box["h"])))
    x1 = min(out.shape[1], x0 + max(1, int(box["w"])))
    clip = np.zeros(out.shape[:2], dtype=np.uint8)
    clip[y0:y1, x0:x1] = 255
    near_ui = cv2.dilate(
        (
            (np.abs(bgr[:, :, 2].astype(np.int16) - bgr[:, :, 1].astype(np.int16)) > 40)
            | (np.abs(bgr[:, :, 1].astype(np.int16) - bgr[:, :, 0].astype(np.int16)) > 40)
            | (np.abs(bgr[:, :, 2].astype(np.int16) - bgr[:, :, 0].astype(np.int16)) > 40)
        ).astype(np.uint8),
        np.ones((5, 5), np.uint8),
        iterations=1,
    ) > 0
    bg_est = float(
        np.percentile(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)[y0:y1, x0:x1], 40)
    )
    for _ in range(2):
        gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
        prox = cv2.dilate(union, np.ones((5, 5), np.uint8), iterations=1) > 0
        # 残留双向检测：暗 ghost（白底黑字场景）与亮 ghost（深底白字场景）都要抓，
        # 否则深灰板上白字 lerp 后的亮残影（实测 glyph ratio 残留 0.05）漏检。
        resid = (
            (
                (gray < (bg_est - 10.0)) | (gray > (bg_est + 22.0))
            )
            & prox
            & (clip > 0)
            & (~near_ui)
        ).astype(np.uint8) * 255
        if int(resid.max()) == 0:
            break
        n_cc, labels, stats, _ = cv2.connectedComponentsWithStats(resid, connectivity=8)
        for i in range(1, n_cc):
            if int(stats[i, cv2.CC_STAT_AREA]) < 3:
                continue
            local = (labels == i).astype(np.uint8) * 255
            out = horiz_lerp_fill(out, local)
        union = np.maximum(union, resid)
    return reinject_band_grain(out, bgr, box)


def _caption_protect_mask(shape_hw: tuple[int, int], box: dict[str, int]) -> Any:
    """Left tier icons / avatars — never fill (captions sit mid-bar)."""
    import numpy as np

    h, w = shape_hw
    protect = np.zeros((h, w), dtype=bool)
    left = min(180, max(48, w // 5))
    protect[:, :left] = True
    _ = box
    return protect


def glyph_union_mask(bgr: Any, box: dict[str, int]) -> Any:
    """Union of per-glyph holes inside ``box`` (uint8 0/255), or empty."""
    import numpy as np

    h, w = bgr.shape[:2]
    parts = text_hole_parts(bgr, box)
    if not parts:
        return np.zeros((h, w), dtype=np.uint8)
    union = parts[0].copy()
    for part in parts[1:]:
        union = np.maximum(union, part)
    union[_caption_protect_mask((h, w), box)] = 0
    return union


def build_temporal_plate(
    frames_bgr: list[Any],
    box: dict[str, int],
    *,
    min_hits: int = 3,
) -> tuple[Any, Any]:
    """TBE plate: per-pixel median of non-glyph samples (robust to mask leaks)."""
    import numpy as np

    if not frames_bgr:
        raise ValueError("empty frames for temporal plate")
    h, w = frames_bgr[0].shape[:2]
    y0 = max(0, int(box["y"]))
    x0 = max(0, int(box["x"]))
    y1 = min(h, y0 + max(1, int(box["h"])))
    x1 = min(w, x0 + max(1, int(box["w"])))
    rh, rw = y1 - y0, x1 - x0
    stack = np.full((len(frames_bgr), rh, rw, 3), np.nan, dtype=np.float32)
    hits = np.zeros((h, w), dtype=np.float32)
    protect = _caption_protect_mask((h, w), box)
    for i, fr in enumerate(frames_bgr):
        mask = glyph_union_mask(fr, box)
        clean = (mask[y0:y1, x0:x1] == 0) & (~protect[y0:y1, x0:x1])
        if not np.any(clean):
            continue
        roi = fr[y0:y1, x0:x1].astype(np.float32)
        stack[i][clean] = roi[clean]
        hits[y0:y1, x0:x1][clean] += 1.0
    plate = frames_bgr[0].copy()
    with np.errstate(all="ignore"):
        med = np.nanmedian(stack, axis=0)
    ok = hits >= float(max(1, min_hits))
    valid = ok[y0:y1, x0:x1] & np.isfinite(med[:, :, 0])
    if np.any(valid):
        plate[y0:y1, x0:x1][valid] = np.clip(med[valid], 0, 255).astype(np.uint8)
    return plate, hits


def fill_caption_tbe(
    bgr: Any,
    box: dict[str, int],
    plate: Any,
    hits: Any,
    *,
    min_hits: int = 3,
    spatial_residual: bool = True,
) -> Any:
    """Replace glyph pixels from TBE plate; uncovered holes → spatial lerp."""
    import cv2
    import numpy as np

    out = bgr.copy()
    mask = glyph_union_mask(out, box)
    if int(mask.max()) == 0:
        return out
    protect = _caption_protect_mask(out.shape[:2], box)
    cover = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=2)
    cover[protect] = 0
    trusted = hits >= float(max(1, min_hits))
    covered = (cover > 0) & trusted
    if np.any(covered):
        alpha = _soft_alpha(cover)
        a = np.where(covered, alpha, 0.0).astype(np.float32)
        base = out.astype(np.float32)
        pl = plate.astype(np.float32)
        out = np.clip(base * (1.0 - a[:, :, None]) + pl * a[:, :, None], 0, 255).astype(
            np.uint8
        )
    need = (cover > 0) & (~trusted)
    if np.any(need):
        out = horiz_lerp_fill(out, need.astype(np.uint8) * 255)
    if spatial_residual:
        out = fill_caption_spatial(out, box)
    return reinject_band_grain(out, bgr, box, cover)


def reinject_band_grain(
    out: Any,
    src: Any,
    box: dict[str, int],
    filled_u8: Any | None = None,
) -> Any:
    """Restore local film grain on filled pixels so the patch doesn't look plastic.

    Industry gap after spatial lerp / neural fill on flat UI: low-frequency OK,
    high-frequency missing. Match high-pass σ from clean pixels in the same band.
    """
    import cv2
    import numpy as np

    y0 = max(0, int(box["y"]))
    x0 = max(0, int(box["x"]))
    y1 = min(out.shape[0], y0 + max(1, int(box["h"])))
    x1 = min(out.shape[1], x0 + max(1, int(box["w"])))
    if y1 <= y0 or x1 <= x0:
        return out
    protect = _caption_protect_mask(out.shape[:2], box)
    if filled_u8 is None:
        # Approximate: pixels that changed vs source inside box.
        diff = np.abs(out.astype(np.int16) - src.astype(np.int16)).max(axis=2) > 4
        filled = diff & (~protect)
        filled[:y0, :] = False
        filled[y1:, :] = False
        filled[:, :x0] = False
        filled[:, x1:] = False
    else:
        filled = (filled_u8 > 0) & (~protect)
    if not np.any(filled):
        return out
    roi_src = src[y0:y1, x0:x1].astype(np.float32)
    blur = cv2.GaussianBlur(roi_src, (5, 5), 0)
    hp = roi_src - blur
    clean = ~filled[y0:y1, x0:x1]
    if float(clean.mean()) < 0.05:
        return out
    # Per-channel σ from clean high-pass.
    sig = []
    for c in range(3):
        vals = hp[:, :, c][clean]
        sig.append(float(np.std(vals)) if vals.size else 0.0)
    sig_a = np.array(sig, dtype=np.float32)
    if float(sig_a.max()) < 0.8:
        return out
    rng = np.random.default_rng(
        int(out[y0, x0, 0]) * 1000 + int(out[y0, x0, 1]) * 10 + int(out[y0, x0, 2])
    )
    noise = rng.normal(0.0, 1.0, size=out[y0:y1, x0:x1].shape).astype(np.float32)
    noise *= sig_a.reshape(1, 1, 3)
    patched = out.copy()
    local = patched[y0:y1, x0:x1].astype(np.float32)
    m = filled[y0:y1, x0:x1]
    local[m] = np.clip(local[m] + noise[m], 0, 255)
    patched[y0:y1, x0:x1] = local.astype(np.uint8)
    return patched


def _soft_alpha(hole_u8: Any) -> Any:
    import cv2
    import numpy as np

    return np.clip(cv2.GaussianBlur(hole_u8, (5, 5), 0).astype(np.float32) / 255.0, 0.0, 1.0)


def _load_generator(ckpt: Path, device: Any) -> Any:
    import torch

    root = Path(__file__).resolve().parent / "sttn"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from model.sttn import InpaintGenerator  # type: ignore

    model = InpaintGenerator().to(device)
    data = torch.load(str(ckpt), map_location=device, weights_only=False)
    state = data["netG"] if isinstance(data, dict) and "netG" in data else data
    if isinstance(state, dict) and state and next(iter(state)).startswith("module."):
        state = {k.replace("module.", "", 1): v for k, v in state.items()}
    model.load_state_dict(state)
    model.eval()
    if device.type == "cuda":
        model = model.half()
    return model


def _get_ref_index(neighbor_ids: list[int], length: int) -> list[int]:
    refs: list[int] = []
    for i in range(0, length, REF_LENGTH):
        if i not in neighbor_ids:
            refs.append(i)
        if len(refs) >= 2:
            break
    return refs


def _hole_frames(hole_u8: Any, length: int) -> list[Any]:
    if isinstance(hole_u8, list):
        if len(hole_u8) != length:
            raise ValueError(f"hole list length {len(hole_u8)} != clip {length}")
        return hole_u8
    return [hole_u8] * length


def _complete_clip(model: Any, frames_rgb: list[Any], hole_u8: Any, device: Any) -> list[Any]:
    """Run STTN on a short RGB clip already sized 240×432."""
    import numpy as np
    import torch

    length = len(frames_rgb)
    holes = _hole_frames(hole_u8, length)
    arr = np.stack(frames_rgb, axis=0).astype(np.float32) / 255.0
    feats = torch.from_numpy(arr).permute(0, 3, 1, 2).unsqueeze(0) * 2.0 - 1.0
    mstack = np.stack([(h > 0).astype(np.float32) for h in holes], axis=0)
    masks = torch.from_numpy(mstack).unsqueeze(1).unsqueeze(0)
    binaries = [np.expand_dims((h > 0).astype(np.float32), 2) for h in holes]
    feats = feats.to(device)
    masks = masks.to(device)
    if device.type == "cuda":
        feats = feats.half()
        masks = masks.half()
    comp = [None] * length
    with torch.no_grad():
        masked = (feats * (1.0 - masks)).view(length, 3, STTN_H, STTN_W)
        del feats
        encoded = model.encoder(masked)
        del masked
        _c = encoded.size(1)
        encoded = encoded.view(1, length, _c, encoded.size(2), encoded.size(3))
        for f in range(0, length, NEIGHBOR_STRIDE):
            neighbor_ids = list(
                range(max(0, f - NEIGHBOR_STRIDE), min(length, f + NEIGHBOR_STRIDE + 1))
            )
            ref_ids = _get_ref_index(neighbor_ids, length)
            ids = neighbor_ids + ref_ids
            feat_in = encoded[0, ids].contiguous()
            mask_in = masks[0, ids].contiguous()
            pred_feat = model.infer(feat_in, mask_in)
            pred_img = torch.tanh(model.decoder(pred_feat[: len(neighbor_ids)])).detach()
            pred_np = (
                ((pred_img.float() + 1.0) / 2.0).cpu().permute(0, 2, 3, 1).numpy() * 255.0
            )
            pred_np = np.nan_to_num(pred_np, nan=0.0, posinf=255.0, neginf=0.0)
            del pred_feat, pred_img, feat_in, mask_in
            for i, idx in enumerate(neighbor_ids):
                b = binaries[idx]
                orig = frames_rgb[idx].astype(np.float32)
                img = pred_np[i].astype(np.float32) * b + orig * (1.0 - b)
                bad = ~np.isfinite(img)
                if np.any(bad):
                    img = np.where(bad, orig, img)
                if comp[idx] is None:
                    comp[idx] = img
                else:
                    comp[idx] = comp[idx] * 0.5 + img * 0.5
        del encoded, masks
        if device.type == "cuda":
            import gc

            gc.collect()
            torch.cuda.empty_cache()
    out = []
    for i in range(length):
        b = binaries[i]
        merged = np.array(comp[i]).astype(np.float32) * b + frames_rgb[i].astype(
            np.float32
        ) * (1.0 - b)
        merged = np.nan_to_num(merged, nan=0.0, posinf=255.0, neginf=0.0)
        out.append(np.clip(merged, 0, 255).astype(np.uint8))
    return out


def _tile_x_ramps(tiles: list[dict[str, int]], overlap: int = 64) -> list[Any]:
    import numpy as np

    ramps: list[Any] = []
    n = len(tiles)
    for i, tile in enumerate(tiles):
        w = int(tile["w"])
        ramp = np.ones(w, dtype=np.float32)
        ov = min(overlap, max(1, w // 4))
        if i > 0:
            ramp[:ov] = np.linspace(0.0, 1.0, ov, dtype=np.float32)
        if i < n - 1:
            ramp[-ov:] = np.linspace(1.0, 0.0, ov, dtype=np.float32)
        ramps.append(ramp)
    return ramps


# Mid-encode temp files must stay playable: classic +faststart only writes moov
# after ffmpeg exits, so `_clean_*_writing.mp4` shows "moov atom not found".
# frag+empty_moov needs -flush_packets 1 or the OS file stays an empty stub.
_WRITE_MOVFLAGS = "+frag_keyframe+empty_moov+default_base_moof"


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
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-g",
            str(gop),
            "-keyint_min",
            str(gop),
            "-movflags",
            _WRITE_MOVFLAGS,
            "-flush_packets",
            "1",
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


def _fill_mosaic_native(frame: Any, mosaic_boxes: list[dict[str, int]]) -> Any:
    """在当前帧上直接弱化/修复马赛克块（spatial 路径用）。

    与 encode_cleanup_pass 的马赛克分支同一套逻辑：build_mosaic_mask 取 mask，
    restore_mosaic_crop(use_neural=False) 走 OpenCV 兜底（中值模糊弱化网格）。
    """
    import cv2

    from demosaic_codeformer import restore_mosaic_crop
    from visual_cleanup import build_mosaic_mask

    out = frame
    H, W = frame.shape[:2]
    for mb in mosaic_boxes:
        x = max(0, min(W - 2, int(mb["x"])))
        y = max(0, min(H - 2, int(mb["y"])))
        w = max(2, min(W - x, int(mb["w"])))
        h = max(2, min(H - y, int(mb["h"])))
        roi = out[y : y + h, x : x + w]
        if roi.size == 0:
            continue
        mask = build_mosaic_mask(roi, dilate=1)
        if int(mask.max()) == 0:
            continue
        restored, _bg = restore_mosaic_crop(roi, mask, None, radius=7, use_neural=False)
        out = out.copy()
        out[y : y + h, x : x + w] = restored
    return out


def spatial_sampling_policy(
    *,
    n_hint: int,
    frame_w: int,
    box_w: int,
    has_mosaic: bool,
) -> dict[str, int]:
    """Tune TBE sampling for short dialogue windows vs long full videos."""
    import math

    min_hits = max(1, int(os.environ.get("VITUAL_TBE_MIN_HITS", "3") or 3))
    sample_stride = max(1, int(os.environ.get("VITUAL_TBE_SAMPLE_STRIDE", "2") or 2))
    max_samples = max(16, int(os.environ.get("VITUAL_TBE_MAX_SAMPLES", "96") or 96))
    if n_hint > 0 and n_hint > max_samples:
        sample_stride = max(sample_stride, int(math.ceil(n_hint / max_samples)))
    short = 0 < n_hint <= 90
    very_short = 0 < n_hint <= 45
    wide_box = frame_w >= 1280 and box_w >= int(frame_w * 0.55)
    if short and not has_mosaic:
        sample_stride = max(sample_stride, 3)
        min_hits = min(min_hits, 2)
    if wide_box and not has_mosaic:
        sample_stride = max(sample_stride, 4 if short else 3)
    if very_short and not has_mosaic:
        sample_stride = max(sample_stride, 4)
    return {
        "min_hits": int(max(1, min_hits)),
        "sample_stride": int(max(1, sample_stride)),
        "max_samples": int(max_samples),
    }


def _encode_spatial(
    src: Path,
    dest: Path,
    box: dict[str, int],
    *,
    first: Any,
    cap: Any,
    frame_w: int,
    frame_h: int,
    fps: float,
    n_hint: int,
    mosaic_boxes: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Flat-bar path: TBE plate (pass1) then per-frame paste + spatial residual (pass2)."""
    import cv2
    import numpy as np

    policy = spatial_sampling_policy(
        n_hint=n_hint,
        frame_w=frame_w,
        box_w=int(box["w"]),
        has_mosaic=bool(mosaic_boxes),
    )
    min_hits = int(policy["min_hits"])
    sample_stride = int(policy["sample_stride"])
    max_samples = int(policy["max_samples"])
    # Pass 1 — subsampled median TBE plate (robust vs mean ghosting).
    print(
        f"[sttn] mode=temporal_flat TBE plate box={box} min_hits={min_hits} "
        f"stride={sample_stride} max_samples={max_samples} "
        f"mosaic_boxes={len(mosaic_boxes or [])}",
        flush=True,
    )
    cap.release()
    cap1 = cv2.VideoCapture(str(src))
    if not cap1.isOpened():
        raise RuntimeError(f"cannot reopen {src} for TBE plate")
    h, w = frame_h, frame_w
    y0 = max(0, int(box["y"]))
    x0 = max(0, int(box["x"]))
    y1 = min(h, y0 + max(1, int(box["h"])))
    x1 = min(w, x0 + max(1, int(box["w"])))
    rh, rw = y1 - y0, x1 - x0
    protect = _caption_protect_mask((h, w), box)
    samples: list[Any] = []
    hits = np.zeros((h, w), dtype=np.float32)
    scanned = 0
    while True:
        ok, fr = cap1.read()
        if not ok:
            break
        scanned += 1
        if (scanned - 1) % sample_stride != 0:
            continue
        mask = glyph_union_mask(fr, box)
        clean = (mask[y0:y1, x0:x1] == 0) & (~protect[y0:y1, x0:x1])
        slot = np.full((rh, rw, 3), np.nan, dtype=np.float32)
        if np.any(clean):
            roi = fr[y0:y1, x0:x1].astype(np.float32)
            slot[clean] = roi[clean]
            hits[y0:y1, x0:x1][clean] += 1.0
        samples.append(slot)
        if n_hint and scanned % 120 == 0:
            print(f"[sttn] TBE scan {scanned}/{n_hint} samples={len(samples)}", flush=True)
    cap1.release()
    plate = first.copy()
    if samples:
        stack = np.stack(samples, axis=0)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            med = np.nanmedian(stack, axis=0)
        del stack
        ok_hits = hits >= float(min_hits)
        valid = ok_hits[y0:y1, x0:x1] & np.isfinite(med[:, :, 0])
        if np.any(valid):
            plate[y0:y1, x0:x1][valid] = np.clip(med[valid], 0, 255).astype(np.uint8)
    else:
        ok_hits = hits >= float(min_hits)
    coverage = float(ok_hits[y0:y1, x0:x1].mean()) if y1 > y0 and x1 > x0 else 0.0
    # High TBE coverage: skip spatial residual (it re-lerps and kills grain / smears UI).
    use_spatial = coverage < 0.85
    print(
        f"[sttn] TBE plate coverage={coverage:.3f} scanned={scanned} "
        f"samples={len(samples)} spatial_residual={use_spatial}",
        flush=True,
    )

    # Pass 2 — apply plate under glyphs, spatial residual for never-exposed.
    writing = dest.with_name("_clean_tbe_writing.mp4")
    if writing.is_file():
        writing.unlink(missing_ok=True)
    proc = _open_ffmpeg_writer(src, writing, frame_w, frame_h, fps)
    assert proc.stdin is not None
    cap2 = cv2.VideoCapture(str(src))
    if not cap2.isOpened():
        proc.kill()
        raise RuntimeError(f"cannot reopen {src} for TBE encode")
    frames_done = 0
    try:
        while True:
            ok, frame = cap2.read()
            if not ok:
                break
            out = frame
            if mosaic_boxes:
                out = _fill_mosaic_native(out, mosaic_boxes)
            out = fill_caption_tbe(
                out,
                box,
                plate,
                hits,
                min_hits=min_hits,
                spatial_residual=use_spatial,
            )
            proc.stdin.write(np.ascontiguousarray(out).tobytes())
            frames_done += 1
            if n_hint and frames_done % 60 == 0:
                print(f"[sttn] wrote {frames_done}/{n_hint}", flush=True)
        if n_hint:
            print(f"[sttn] wrote {frames_done}/{n_hint}", flush=True)
    except Exception:
        proc.kill()
        raise
    finally:
        cap2.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    stderr = proc.communicate(timeout=300)[1]
    if proc.returncode != 0 or frames_done < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        try:
            writing.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"TBE encode failed ({proc.returncode}): {err}")
    _finalize_writing_mp4(writing, dest)
    return {
        "engine": "sttn",
        "mode": "temporal_flat",
        "tbe_coverage": round(coverage, 4),
        "tbe_min_hits": min_hits,
        "tbe_spatial_residual": use_spatial,
        "frames": frames_done,
        "device": "cpu",
        "bytes": dest.stat().st_size,
    }


def encode_sttn(
    src: Path,
    dest: Path,
    box: dict[str, int],
    *,
    ckpt: Path | None = None,
    mosaic_boxes: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Restore caption region.

    Routing (``VITUAL_STTN_FORCE``):
      - ``auto`` (default): flat UI bars → temporal_flat (TBE); textured → STTN tiles
        with glyph-level holes. Flat bars + whole-box/neural fill leave glyph ghosts.
      - ``flat`` / ``0`` / ``false``: force temporal_flat
      - ``tiles`` / ``sttn`` / ``1`` / ``true``: force STTN tiles
    """
    import cv2
    import numpy as np
    import torch

    from media_ops import probe_video_wh

    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    wh = probe_video_wh(src)
    if wh is None:
        raise RuntimeError(f"cannot probe video: {src}")
    frame_w, frame_h = wh

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    n_hint = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    ok, first = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError(f"empty video: {src}")

    _force = (os.environ.get("VITUAL_STTN_FORCE") or "auto").strip().lower()
    force_tiles = _force in ("tiles", "sttn", "1", "true", "yes", "on")
    force_flat = _force in ("flat", "0", "false", "no", "off", "spatial")
    # Multi-frame flat vote: static UI bars stay flat; one noisy frame shouldn't force STTN.
    flat_votes = 1 if band_is_flat(first, box) else 0
    flat_checks = 1
    if n_hint > 8 and not force_tiles and not force_flat:
        for frac in (0.35, 0.65):
            idx = max(1, min(n_hint - 2, int(n_hint * frac)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, float(idx))
            ok_f, fr = cap.read()
            if not ok_f:
                continue
            flat_checks += 1
            if band_is_flat(fr, box):
                flat_votes += 1
        cap.set(cv2.CAP_PROP_POS_FRAMES, 1.0)
        ok_r, first2 = cap.read()
        if ok_r:
            first = first2
    use_flat = force_flat or (
        not force_tiles and flat_votes * 2 >= flat_checks  # majority / tie → flat
    )
    if use_flat:
        print(
            f"[sttn] route=temporal_flat votes={flat_votes}/{flat_checks} "
            f"force={_force or 'auto'} box={box}",
            flush=True,
        )
        try:
            return _encode_spatial(
                src,
                dest,
                box,
                first=first,
                cap=cap,
                frame_w=frame_w,
                frame_h=frame_h,
                fps=fps,
                n_hint=n_hint,
                mosaic_boxes=list(mosaic_boxes or []),
            )
        finally:
            try:
                cap.release()
            except Exception:
                pass

    print(
        f"[sttn] route=tiles votes={flat_votes}/{flat_checks} "
        f"force={_force or 'auto'} box={box}",
        flush=True,
    )

    weights = Path(ckpt) if ckpt else default_ckpt()
    if not weights.is_file() or weights.stat().st_size < 1_000_000:
        cap.release()
        raise FileNotFoundError(
            f"STTN checkpoint missing: {weights}. "
            "Set VITUAL_STTN_CKPT or place models/sttn.pth"
        )
    def _build_region(b: dict[str, int]) -> dict[str, Any] | None:
        ts = sttn_tiles(b, frame_w, frame_h)
        if not ts:
            return None
        return {
            "box": b,
            "tiles": ts,
            "ramps": _tile_x_ramps(ts),
            "band_y": int(ts[0]["y"]),
            "band_h": int(ts[0]["h"]),
        }

    regions: list[dict[str, Any]] = []
    cap_region = _build_region(box)
    if cap_region:
        cap_region["kind"] = "hardsub"
        regions.append(cap_region)
    # 马赛克块也作为 STTN 的「洞」接入同一套 tiles（人脸/大块由时序补内容）。
    for mb in mosaic_boxes or []:
        r = _build_region(mb)
        if r:
            r["kind"] = "mosaic"
            regions.append(r)
    if not regions:
        cap.release()
        raise RuntimeError("STTN produced no tiles for regions")

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    if device.type == "cuda":
        torch.cuda.empty_cache()
    total_tiles = sum(len(r["tiles"]) for r in regions)
    print(
        f"[sttn] mode=tiles device={device} ckpt={weights.name} regions={len(regions)} "
        f"tiles={total_tiles} native={STTN_W}x{STTN_H} box={box}",
        flush=True,
    )
    model = _load_generator(weights, device)
    # Write to a temp file so a crash never leaves a truncated clean.mp4.
    writing = dest.with_name("_clean_sttn_writing.mp4")
    if writing.is_file():
        writing.unlink(missing_ok=True)
    proc = _open_ffmpeg_writer(src, writing, frame_w, frame_h, fps)
    assert proc.stdin is not None

    chunk = max(6, min(MAX_LOAD, 16))
    step = max(1, chunk - 2)
    pending: list[Any] = []
    frames_done = 0

    def _composite_frame(orig: Any, regions_data: list[Any], idx: int) -> Any:
        out = orig.copy()
        for r in regions_data:
            tile_preds = r["tile_preds"]
            tile_holes = r["tile_holes"]
            tiles = r["tiles"]
            ramps = r["ramps"]
            band_y = r["band_y"]
            band_h = r["band_h"]
            num = np.zeros((band_h, frame_w, 3), dtype=np.float32)
            den = np.zeros((band_h, frame_w), dtype=np.float32)
            for ti, tile in enumerate(tiles):
                preds = tile_preds[ti]
                if preds is None:
                    continue
                pred = cv2.cvtColor(preds[idx], cv2.COLOR_RGB2BGR).astype(np.float32)
                alpha = _soft_alpha(tile_holes[ti][idx])
                tw = int(tile["w"])
                th = int(tile["h"])
                a = alpha[:th, :tw] * ramps[ti][None, :tw]
                x = int(tile["x"])
                ly = int(tile["y"]) - band_y
                num[ly : ly + th, x : x + tw] += pred[:th, :tw] * a[:, :, None]
                den[ly : ly + th, x : x + tw] += a
            if float(den.max()) <= 1e-4:
                continue
            den3 = np.maximum(den[:, :, None], 1e-6)
            blend = np.clip(den, 0.0, 1.0)[:, :, None]
            band = out[band_y : band_y + band_h].astype(np.float32)
            avg = num / den3
            band = band * (1.0 - blend) + avg * blend
            out[band_y : band_y + band_h] = np.clip(band, 0, 255).astype(np.uint8)
        return out

    def flush_clip(bgr_frames: list[Any], regions: list[dict], *, last: bool) -> None:
        nonlocal frames_done
        n = len(bgr_frames)
        write_n = n if last else min(n, step)
        regions_data: list[Any] = []
        for r in regions:
            tiles = r["tiles"]
            box = r["box"]
            kind = str(r.get("kind") or "hardsub")
            # 字幕：通用混合字形 hole（OCR 行 bbox 限定 + 笔画细化，回退自适应对比度）。
            # 马赛克：块状区域，整框 hole 才正确。
            glyph_per_frame: list[Any] = []
            if kind == "hardsub":
                glyph_per_frame = [glyph_mask_hybrid(fr, box) for fr in bgr_frames]
            tile_preds: list[Any] = []
            tile_holes: list[list[Any]] = []
            for tile in tiles:
                rgbs: list[Any] = []
                holes: list[Any] = []
                any_hole = False
                for fi, fr in enumerate(bgr_frames):
                    patch = extract_tile(fr, tile)
                    if kind == "hardsub":
                        hole = glyph_hole_for_tile(glyph_per_frame[fi], tile)
                    else:
                        hole = text_hole_for_tile(patch, box, tile)
                    if hole.shape[0] != STTN_H or hole.shape[1] != STTN_W:
                        padded = np.zeros((STTN_H, STTN_W), dtype=np.uint8)
                        padded[: hole.shape[0], : hole.shape[1]] = hole
                        hole = padded
                    if int(hole.max()) > 0:
                        any_hole = True
                    rgbs.append(cv2.cvtColor(patch, cv2.COLOR_BGR2RGB))
                    holes.append(hole)
                tile_holes.append(holes)
                if any_hole:
                    tile_preds.append(_complete_clip(model, rgbs, holes, device))
                else:
                    tile_preds.append(None)
            regions_data.append(
                {
                    "tile_preds": tile_preds,
                    "tile_holes": tile_holes,
                    "tiles": tiles,
                    "ramps": r["ramps"],
                    "band_y": r["band_y"],
                    "band_h": r["band_h"],
                }
            )
        for i in range(write_n):
            out = _composite_frame(bgr_frames[i], regions_data, i)
            # Close the last plastic-patch gap on flat UI bars.
            out = reinject_band_grain(out, bgr_frames[i], box)
            proc.stdin.write(np.ascontiguousarray(out).tobytes())
            frames_done += 1
        if n_hint:
            print(f"[sttn] wrote {frames_done}/{n_hint}", flush=True)
        keep = n - write_n
        if keep > 0:
            pending[:] = bgr_frames[-keep:]
        else:
            pending.clear()

    try:
        bgr_buf: list[Any] = [first]
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            bgr_buf.append(frame)
            if len(bgr_buf) >= chunk:
                flush_clip(bgr_buf, regions, last=False)
                bgr_buf = list(pending)
        if bgr_buf:
            flush_clip(bgr_buf, regions, last=True)
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
    if proc.returncode != 0 or frames_done < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        try:
            writing.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"STTN encode failed ({proc.returncode}): {err}")
    _finalize_writing_mp4(writing, dest)
    return {
        "engine": "sttn",
        "mode": "tiles",
        "frames": frames_done,
        "tiles": total_tiles,
        "ckpt": weights.name,
        "device": str(device),
        "bytes": dest.stat().st_size,
    }
