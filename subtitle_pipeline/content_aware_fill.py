"""Photoshop-style *content-aware* fill (Criminisi et al., 2004) for subtitle /
watermark hole removal.

Why this exists
---------------
OpenCV's built-in ``cv2.inpaint`` (TELEA / Navarre-Stokes) is *diffusion*-based:
on a textured / moving background it smears a soft blotch across the hole. Real
"Content-Aware Fill" (what Photoshop does) is *exemplar*-based -- it copies the
most similar real texture patch from elsewhere in the image, so the fill stays
sharp and natural with no smear.

This module implements that exemplar fill. The expensive nearest-patch search
is done with ``cv2.matchTemplate`` (masked SQDIFF), which is C-optimised.

Scope / speed
-------------
The caller passes a small ROI (the subtitle band + margin) so the search space
is tiny and the per-frame cost is a handful of matchTemplate calls. For thin
glyph holes this runs in well under a second per frame on CPU.
"""
from __future__ import annotations

from typing import Any

import cv2
import numpy as np


def criminisi_fill(
    roi_bgr: np.ndarray,
    mask_u8: np.ndarray,
    *,
    patch: int = 11,
    search: int = 110,
    max_iter: int | None = None,
) -> np.ndarray:
    """Fill ``mask_u8`` (255=hole) inside ``roi_bgr`` by copying real texture.

    Returns a new ROI (uint8 BGR) with the hole filled. Boundary-first
    (confidence) peeling; each step copies the best-matching clean patch found
    by masked SQDIFF template matching (excluding the target's own neighbourhood
    so it can't trivially copy itself).
    """
    img = roi_bgr.copy()
    H, W = img.shape[:2]
    hole0 = mask_u8 > 0
    if not hole0.any():
        return img

    half = patch // 2
    # conf: 1.0 where known, 0.0 where still to fill.
    conf = np.where(hole0, np.float32(0.0), np.float32(1.0))
    img_f = img.astype(np.float32)
    k3 = np.ones((3, 3), np.uint8)

    iters = 0
    while True:
        hole = conf < 1.0
        if not hole.any():
            break
        iters += 1
        if max_iter is not None and iters > max_iter:
            break

        # fill front = hole pixels touching a known pixel
        known = (~hole).astype(np.uint8)
        grown = cv2.dilate(known, k3) > 0
        front = grown & hole
        ys, xs = np.where(front)
        if len(ys) == 0:
            break

        # boundary-first: pick the patch with the most known pixels (onion peel)
        conf_sum = cv2.boxFilter(
            conf, -1, (patch, patch), normalize=False, borderType=cv2.BORDER_CONSTANT
        )
        best_c = -1.0
        by = bx = 0
        for y, x in zip(ys, xs):
            c = float(conf_sum[y, x])
            if c > best_c:
                best_c = c
                by, bx = int(y), int(x)

        y0 = max(0, by - half)
        x0 = max(0, bx - half)
        y1 = min(H, by + half + 1)
        x1 = min(W, bx + half + 1)
        tpl = img_f[y0:y1, x0:x1]                      # target patch (float32)
        tpl_known = (conf[y0:y1, x0:x1] >= 1.0).astype(np.uint8)  # known pixels
        if not tpl_known.any():
            # nothing known in this patch yet -> mark filled to avoid a stall
            conf[y0:y1, x0:x1] = 1.0
            continue

        # masked SQDIFF in a LOCAL window around the target: exemplar texture
        # lives near the hole, so a small search window is both correct and far
        # cheaper than scanning the whole ROI every iteration.
        th, tw = y1 - y0, x1 - x0
        sy0, sx0 = max(0, y0 - search), max(0, x0 - search)
        sy1, sx1 = min(H, y1 + search), min(W, x1 + search)
        win = img_f[sy0:sy1, sx0:sx1]
        if win.shape[0] < th or win.shape[1] < tw:
            win, oy, ox = img_f, 0, 0
        else:
            oy, ox = sy0, sx0
        resp = cv2.matchTemplate(
            win, tpl.astype(np.float32), cv2.TM_SQDIFF, mask=tpl_known
        )
        # forbid trivial self-match: blank the target's own neighbourhood
        ly0 = max(0, (y0 - oy) - half)
        lx0 = max(0, (x0 - ox) - half)
        ly1 = min(resp.shape[0], (y1 - oy) + half)
        lx1 = min(resp.shape[1], (x1 - ox) + half)
        resp[ly0:ly1, lx0:lx1] = np.inf

        min_val, _, min_loc, _ = cv2.minMaxLoc(resp)
        if not np.isfinite(min_val):
            conf[y0:y1, x0:x1] = 1.0
            continue

        # copy exemplar's pixels into the target's still-unknown pixels
        ey0, ex0 = oy + min_loc[1], ox + min_loc[0]
        src = img_f[ey0:ey0 + th, ex0:ex0 + tw]
        tgt_hole = tpl_known == 0
        img_f[y0:y1, x0:x1][tgt_hole] = src[tgt_hole]
        conf[y0:y1, x0:x1][tgt_hole] = 1.0

    return np.clip(img_f, 0, 255).astype(np.uint8)


def content_aware_hardsub(
    frame_bgr: np.ndarray,
    box: dict[str, int],
    hole_u8: np.ndarray,
    *,
    patch: int = 11,
    search: int = 110,
    margin: int = 40,
) -> np.ndarray:
    """Fill a glyph hole in ``frame_bgr`` using content-aware fill, scoped to the
    subtitle band (+ ``margin``) for speed. ``hole_u8`` is a full-frame glyph mask
    (255=hole); only its overlap with the band is filled.
    """
    H, W = frame_bgr.shape[:2]
    x = max(0, int(box["x"]) - margin)
    y = max(0, int(box["y"]) - margin)
    w = min(W - x, int(box["w"]) + 2 * margin)
    h = min(H - y, int(box["h"]) + 2 * margin)
    roi = frame_bgr[y:y + h, x:x + w]
    m = hole_u8[y:y + h, x:x + w]
    if not (m > 0).any():
        return frame_bgr
    filled = criminisi_fill(roi, m, patch=patch, search=search)
    out = frame_bgr.copy()
    out[y:y + h, x:x + w] = filled
    return out
