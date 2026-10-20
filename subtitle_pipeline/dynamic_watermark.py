"""Dynamic (moving) burned-in watermark: detect the trajectory, then remove
per-frame at the watermark's current location.

A static box cannot cover a watermark that translates/floats across the frame,
so we (1) sample frames, (2) track the moving blob to build a per-frame box
trajectory, and (3) inpaint each frame at its own box. Frame count is aligned to
the source at the end so downstream concat/verify stay in sync.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _blob_candidates(frame: np.ndarray) -> list[dict[str, Any]]:
    """Small bright / highly-saturated connected components (watermark glyphs)."""
    h, w = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    v = hsv[:, :, 2]
    s = hsv[:, :, 1]
    mask = ((v > 205) | ((s > 130) & (v > 110))).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _lab, stats, cent = cv2.connectedComponentsWithStats(mask, 8)
    min_a = max(80, int(0.00005 * w * h))
    max_a = int(0.02 * w * h)
    out: list[dict[str, Any]] = []
    for k in range(1, n):
        x, y, ww, hh, area = (int(t) for t in stats[k])
        if area < min_a or area > max_a:
            continue
        if ww > 0.5 * w or hh > 0.3 * h:
            continue
        out.append(
            {
                "cx": float(cent[k][0]),
                "cy": float(cent[k][1]),
                "x": x,
                "y": y,
                "w": ww,
                "h": hh,
                "area": int(area),
            }
        )
    return out


def _interp_box(samp: list[tuple[int, dict[str, Any]]], i: int) -> dict[str, float]:
    """Linear interpolation of center + size at frame ``i`` from sorted samples."""
    if i <= samp[0][0]:
        b = samp[0][1]
        return {"cx": b["cx"], "cy": b["cy"], "w": float(b["w"]), "h": float(b["h"])}
    if i >= samp[-1][0]:
        b = samp[-1][1]
        return {"cx": b["cx"], "cy": b["cy"], "w": float(b["w"]), "h": float(b["h"])}
    for (f0, b0), (f1, b1) in zip(samp, samp[1:]):
        if f0 <= i <= f1:
            if f1 == f0:
                t = 0.0
            else:
                t = (i - f0) / (f1 - f0)
            return {
                "cx": b0["cx"] + (b1["cx"] - b0["cx"]) * t,
                "cy": b0["cy"] + (b1["cy"] - b0["cy"]) * t,
                "w": b0["w"] + (b1["w"] - b0["w"]) * t,
                "h": b0["h"] + (b1["h"] - b0["h"]) * t,
            }
    b = samp[-1][1]
    return {"cx": b["cx"], "cy": b["cy"], "w": float(b["w"]), "h": float(b["h"])}


def _in_any_box(cx: float, cy: float, boxes: list[dict[str, Any]]) -> bool:
    for b in boxes or []:
        if (
            float(b["x"]) <= cx <= float(b["x"]) + float(b["w"])
            and float(b["y"]) <= cy <= float(b["y"]) + float(b["h"])
        ):
            return True
    return False


def _verify_track(
    tr: list[tuple[int, dict[str, Any]]],
    grabbed: dict[int, np.ndarray],
    w: int,
    h: int,
    match_thresh: float,
    min_move_frac: float,
) -> tuple[list[tuple[int, dict[str, Any]]], float] | None:
    """Template-verify a candidate track; return (samples, motion) or None.

    A real moving watermark is ONE rigid object translating, so the first blob's
    crop re-matches strongly in *every* sampled frame (checked at the 25th
    percentile, stricter than the median, so one lucky frame can't carry it).
    Moving scene content / chained glyphs fail this. The crop is padded with
    surrounding context so a solid-colour blob still has variance — a constant
    template makes TM_CCOEFF_NORMED undefined (NaN). Trajectory is rebuilt from
    the match positions.
    """
    samp = sorted(tr, key=lambda t: t[0])
    _f0, b0 = samp[0]
    bx0 = max(0, int(b0["x"]))
    by0 = max(0, int(b0["y"]))
    bw = int(b0["w"])
    bh = int(b0["h"])
    px = max(2, bw // 3)
    py = max(2, bh // 3)
    cx0 = max(0, bx0 - px)
    cy0 = max(0, by0 - py)
    cx1 = min(w, bx0 + bw + px)
    cy1 = min(h, by0 + bh + py)
    tw2 = cx1 - cx0
    th2 = cy1 - cy0
    if tw2 < 6 or th2 < 6:
        return None
    # Background-invariant SHAPE matching. Binarise (bright / highly-saturated =
    # watermark ink) both the template crop and each frame, then cross-correlate
    # the binary silhouette with TM_CCORR_NORMED. Tracking the watermark's shape
    # (not its pixels) is robust to a changing background and to solid-colour or
    # semi-transparent marks — a constant template would break TM_CCOEFF_NORMED.
    def _bin(img: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        v = hsv[:, :, 2]
        s = hsv[:, :, 1]
        return (((v > 200) | ((s > 120) & (v > 100))).astype(np.float32))

    tmpl = _bin(grabbed[samp[0][0]][cy0:cy1, cx0:cx1])
    matched: list[tuple[int, int, int, float]] = []
    for fi, _b in samp:
        fr = grabbed[fi]
        if fr.shape[0] < th2 or fr.shape[1] < tw2:
            matched.append((fi, bx0, by0, 0.0))
            continue
        res = cv2.matchTemplate(_bin(fr), tmpl, cv2.TM_CCORR_NORMED)
        _mn, mx, _mnloc, mxloc = cv2.minMaxLoc(res)
        matched.append((fi, int(mxloc[0]), int(mxloc[1]), float(mx)))
    if float(np.percentile([m[3] for m in matched], 25)) < match_thresh:
        return None
    samp2: list[tuple[int, dict[str, Any]]] = [
        (
            fi,
            {
                "cx": mx + px + bw / 2.0,
                "cy": my + py + bh / 2.0,
                "x": mx + px,
                "y": my + py,
                "w": bw,
                "h": bh,
            },
        )
        for fi, mx, my, _sc in matched
    ]
    samp2.sort(key=lambda t: t[0])
    xs = [b["cx"] for _, b in samp2]
    ys = [b["cy"] for _, b in samp2]
    move = (max(xs) - min(xs)) + (max(ys) - min(ys))
    if move < min_move_frac * min(w, h):
        return None
    return samp2, move


def detect_moving_watermark(
    src: Path,
    *,
    samples: int = 20,
    min_present: float = 0.6,
    min_move_frac: float = 0.12,
    match_thresh: float = 0.5,
    ignore_boxes: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return a moving-watermark descriptor with a per-frame trajectory, or None.

    A track must persist across >= ``min_present`` sampled frames, move by
    >= ``min_move_frac * min(w,h)`` px, and pass template-match verification.
    ``ignore_boxes`` are already-classified static regions (dialogue/title/
    watermark); blobs inside them are skipped so a static subtitle is never
    mistaken for a moving watermark.
    """
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        return None
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if total <= 0 or w <= 0 or h <= 0:
            return None
        n = samples
        if total < n * 2:
            n = max(4, total // 2)
        idxs = sorted({int(round(i * (total - 1) / max(1, n - 1))) for i in range(n)})
        want = set(idxs)
        grabbed: dict[int, np.ndarray] = {}
        i = 0
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            if i in want:
                grabbed[i] = fr
            i += 1
    finally:
        cap.release()

    ignore = ignore_boxes or []
    per_frame: list[tuple[int, list[dict[str, Any]]]] = []
    for fi in idxs:
        if fi not in grabbed:
            continue
        blobs = [
            b
            for b in _blob_candidates(grabbed[fi])
            if not _in_any_box(b["cx"], b["cy"], ignore)
        ]
        per_frame.append((fi, blobs))
    if len(per_frame) < 3:
        return None

    max_jump = 0.25 * max(w, h)
    tracks: list[list[tuple[int, dict[str, Any]]]] = []
    active: list[list[tuple[int, dict[str, Any]]]] = []
    for fi, blobs in per_frame:
        carried: list[list[tuple[int, dict[str, Any]]]] = []
        used: set[int] = set()
        for tr in active:
            _lf, lb = tr[-1]
            best = None
            best_d = 1e9
            for bi, b in enumerate(blobs):
                d = float(np.hypot(b["cx"] - lb["cx"], b["cy"] - lb["cy"]))
                if d < best_d and d <= max_jump and bi not in used:
                    best_d = d
                    best = bi
            if best is not None:
                tr.append((fi, blobs[best]))
                used.add(best)
                carried.append(tr)
            else:
                if len(tr) >= min_present * len(per_frame):
                    tracks.append(tr)
        for bi, b in enumerate(blobs):
            if bi not in used:
                carried.append([(fi, b)])
        active = carried
    for tr in active:
        if len(tr) >= min_present * len(per_frame):
            tracks.append(tr)

    # Rank candidate tracks by centroid motion, then template-verify each in
    # turn; the first that passes is the moving watermark. Verifying several
    # (not just the single max-motion one) avoids missing a real watermark when
    # a higher-motion noise track fails verification first.
    def _move(tr: list[tuple[int, dict[str, Any]]]) -> float:
        xs = [b["cx"] for _, b in tr]
        ys = [b["cy"] for _, b in tr]
        return (max(xs) - min(xs)) + (max(ys) - min(ys))

    cands = sorted((t for t in tracks if len(t) >= 3), key=_move, reverse=True)[:10]
    # Loose sanity cap only: a single watermark rarely sweeps nearly the whole
    # frame. Shape verification above is the real discriminator (it rejects
    # chained scene content); this just guards against pathological full-frame
    # chaining that happens to pass shape matching.
    max_move = 0.9 * (w + h)
    samp: list[tuple[int, dict[str, Any]]] | None = None
    best_move = 0.0
    for tr in cands:
        res = _verify_track(tr, grabbed, w, h, match_thresh, min_move_frac)
        if res is None:
            continue
        cand_samp, cand_move = res
        if cand_move > max_move:
            continue
        samp, best_move = cand_samp, cand_move
        break
    if samp is None:
        return None

    trajectory: list[dict[str, int]] = []
    for i in range(total):
        bx = _interp_box(samp, i)
        trajectory.append(
            {
                "frame": i,
                "x": int(round(bx["cx"] - bx["w"] / 2)),
                "y": int(round(bx["cy"] - bx["h"] / 2)),
                "w": int(round(bx["w"])),
                "h": int(round(bx["h"])),
            }
        )
    first = samp[0][1]
    return {
        "role": "watermark_dynamic",
        "fps": fps,
        "width": w,
        "height": h,
        "motion_px": round(best_move, 1),
        "representative": {
            "x": int(first["x"]),
            "y": int(first["y"]),
            "w": int(first["w"]),
            "h": int(first["h"]),
        },
        "samples": [
            {
                "frame": fi,
                "x": int(b["x"]),
                "y": int(b["y"]),
                "w": int(b["w"]),
                "h": int(b["h"]),
            }
            for fi, b in samp
        ],
        "trajectory": trajectory,
    }


def strip_moving_watermark(
    src: Path,
    out: Path,
    trajectory: list[dict[str, int]],
    *,
    fps: float,
    width: int,
    height: int,
    pad: int = 4,
) -> Path:
    """Inpaint each frame at the watermark's box for that frame, then frame-align."""
    from sttn_inpaint import _open_ffmpeg_writer
    from visual_cleanup import _enforce_frame_count

    out = Path(out)
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src}")
    proc = _open_ffmpeg_writer(Path(src), out, width, height, fps)
    assert proc.stdin is not None
    i = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            box = trajectory[i] if i < len(trajectory) else None
            if box:
                x0 = max(0, int(box["x"]) - pad)
                y0 = max(0, int(box["y"]) - pad)
                x1 = min(width, int(box["x"]) + int(box["w"]) + pad)
                y1 = min(height, int(box["y"]) + int(box["h"]) + pad)
                if x1 - x0 > 2 and y1 - y0 > 2:
                    mask = np.zeros(fr.shape[:2], dtype=np.uint8)
                    mask[y0:y1, x0:x1] = 255
                    fr = cv2.inpaint(fr, mask, 5, cv2.INPAINT_TELEA)
            proc.stdin.write(np.ascontiguousarray(fr).tobytes())
            i += 1
    except Exception:
        proc.kill()
        raise
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    proc.communicate(timeout=900)
    if proc.returncode != 0:
        raise RuntimeError(f"dynamic watermark strip failed ({proc.returncode})")
    _enforce_frame_count(Path(src), out)
    return out
