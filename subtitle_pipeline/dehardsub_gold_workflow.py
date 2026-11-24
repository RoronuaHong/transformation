# -*- coding: utf-8 -*-
"""黄金标杆去字幕工作流（通用型）。

用户验收标杆（2026-11-24）
--------------------------
``fix_frame1_final2.py`` / ``src_0001_final2.jpg`` 的整帧一体流程，
在 BV1P6 前 50 帧（含顶栏水印 + 中景 @ + 底部字幕）被认定为「非常完美」。
本模块把该流程做成**与视频无关的通用实现**：框来自 regions / confirm，
不写死 BV 号坐标；算法参数与黄金脚本 1:1。

算法（对齐首帧黄金 + 可选关键帧传播）
------------------------------------
1. **顶栏水印（强制对齐首帧 / TOP GOLD LOCK）**
   - 输入：**regions/confirm 原框**（``x,y,w,h`` 原样）。
   - 算法：整框 ``ns_box_wipe(feather=6, radius=5)``；字尾由 ``ns_box_wipe`` **内部**
     向下探测覆盖（勿在调用前 ``expand_static_overlay_box``）。
   - **禁止**（2026-11-24 实测偏离 gold50 / ``src_0001_final2``）：
     ``expand_static_overlay_box`` 大扩框（Morphic MAD ~3.8→~9）、
     Morphic 贴片、自制 TELEA、ink 复合、关键帧 donor 贴顶栏。
   - 回归门槛（BV1P6 f0 vs ``src_0001_final2``）：右顶 MAD<5、左顶 MAD<2、bright<0.01。
2. **中景 / 对白**：自适应 mask →（可选）最近黄金关键帧 donor 填洞 → 残差 ``lama_inpaint``。
3. **Pass A/B**：关键帧库**只**服务中景/对白；顶栏每帧仍走上述锁死路径。

开关
----
``VITUAL_DEHARDSUB_GOLD_WORKFLOW=1``（默认开）走本模块；``=0`` 回退旧 hybrid。
``VITUAL_DEHARDSUB_GOLD_WORKERS``：并行准备 mask 的线程数（LaMa 仍串行吃 GPU）。
``VITUAL_DEHARDSUB_GOLD_KF=1``（默认开）启用关键帧黄金库；``=0`` 退回纯逐帧。
``VITUAL_DEHARDSUB_GOLD_KF_STRIDE``：关键帧间隔秒（默认 ``0.5``）。

公共 API
--------
- ``process_frame(frame, boxes) -> BGR``
- ``run_gold_workflow(source, out, boxes, ...) -> dict``
- ``boxes_from_regions(regions_path | dict | list) -> list[dict]``
- ``build_gold_keyframe_bank(source, boxes, ...) -> list[(idx, frame)]``
- ``top_gold_contract() -> dict`` 顶栏锁契约（测试/报告用）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np

GOLD_WORKFLOW_ENV = "VITUAL_DEHARDSUB_GOLD_WORKFLOW"
GOLD_WORKERS_ENV = "VITUAL_DEHARDSUB_GOLD_WORKERS"
GOLD_KF_ENV = "VITUAL_DEHARDSUB_GOLD_KF"
GOLD_KF_STRIDE_ENV = "VITUAL_DEHARDSUB_GOLD_KF_STRIDE"

# 顶栏黄金锁参数（与 src_0001_final2 / gold50 对齐；勿改除非重跑对齐验收）。
GOLD_TOP_FEATHER = 6
GOLD_TOP_RADIUS = 5
# BV1P6 f0 wipe vs src_0001_final2 门槛（扩框回归会破右顶）。
# wipe 阶段左顶通常 <1.2；最终 H.264 remux 可多 ~1 MAD，故左顶门限 2.5。
GOLD_TOP_MAD_RIGHT_MAX = 5.0
GOLD_TOP_MAD_LEFT_MAX = 2.5
GOLD_TOP_BRIGHT_MAX = 0.01

# fix_frame1_final2.py 对话带几何（仅作「无 dialogue 框」时的回退比例，不绑死像素）。
_DIALOGUE_BAND_FRAC = (0.30, 0.78, 0.43, 0.95)  # x0,y0,x1,y1 as fractions of W,H


def top_gold_contract() -> dict[str, Any]:
    """Machine-readable top-bar gold lock (for tests / meta / agents)."""
    return {
        "name": "top_ns_box_raw",
        "box_source": "regions_raw",
        "method": "ns_box_wipe",
        "feather": GOLD_TOP_FEATHER,
        "radius": GOLD_TOP_RADIUS,
        "expand_static_overlay_box": False,
        "donor_paste": False,
        "keyframe_ns_median_plate": False,  # opt-in VITUAL_DEHARDSUB_GOLD_TOP_PLATE=1
        "ink_composite": False,
        "morphic_tile": False,
        "telea": False,
        "mad_right_max": GOLD_TOP_MAD_RIGHT_MAX,
        "mad_left_max": GOLD_TOP_MAD_LEFT_MAX,
        "bright_max": GOLD_TOP_BRIGHT_MAX,
        "reference": "eval/gold/dehardsub_gold_workflow/src_0001_final2.jpg",
        "note": (
            "Default: ns_box_wipe on confirm/raw xywh (gold50 / src_0001_final2). "
            "Optional VITUAL_DEHARDSUB_GOLD_TOP_PLATE=1 for KF median plates. "
            "Never expand_static_overlay_box."
        ),
    }


def gold_workflow_enabled() -> bool:
    raw = (os.environ.get(GOLD_WORKFLOW_ENV) or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def gold_keyframe_enabled() -> bool:
    raw = (os.environ.get(GOLD_KF_ENV) or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def gold_keyframe_stride_sec() -> float:
    raw = (os.environ.get(GOLD_KF_STRIDE_ENV) or "0.5").strip()
    try:
        v = float(raw)
    except ValueError:
        v = 0.5
    return max(0.15, min(v, 5.0))


def keyframe_indices(total: int, fps: float, *, stride_sec: float | None = None) -> list[int]:
    """Evenly spaced frame indices (always include 0 and last)."""
    if total <= 0:
        return []
    if total == 1:
        return [0]
    step = max(1, int(round(float(fps) * (stride_sec if stride_sec is not None else gold_keyframe_stride_sec()))))
    idxs = list(range(0, total, step))
    if idxs[-1] != total - 1:
        idxs.append(total - 1)
    return idxs


def nearest_keyframe(
    bank: list[tuple[int, np.ndarray]], idx: int
) -> tuple[int, np.ndarray] | None:
    if not bank:
        return None
    best = min(bank, key=lambda t: abs(int(t[0]) - int(idx)))
    return best


def _box_ring_mad(
    a: np.ndarray,
    b: np.ndarray,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    *,
    ring: int = 8,
) -> float:
    """Mean abs-diff on a ring just outside the box (background similarity)."""
    H, W = a.shape[:2]
    if a.shape != b.shape or x1 <= x0 or y1 <= y0:
        return 1e9
    rx0, ry0 = max(0, x0 - ring), max(0, y0 - ring)
    rx1, ry1 = min(W, x1 + ring), min(H, y1 + ring)
    if rx1 <= rx0 or ry1 <= ry0:
        return 1e9
    ga = cv2.cvtColor(a[ry0:ry1, rx0:rx1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(b[ry0:ry1, rx0:rx1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    mask = np.ones(ga.shape, np.uint8)
    ix0, iy0 = x0 - rx0, y0 - ry0
    ix1, iy1 = x1 - rx0, y1 - ry0
    mask[max(0, iy0) : max(0, iy1), max(0, ix0) : max(0, ix1)] = 0
    if int(mask.sum()) < 32:
        return 1e9
    return float(np.mean(np.abs(ga[mask > 0] - gb[mask > 0])))


def _paste_roi_feather(
    frame: np.ndarray,
    donor: np.ndarray,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    *,
    feather: int = 8,
) -> np.ndarray:
    """Feather-blend donor ROI into frame over [x0:x1,y0:y1]."""
    H, W = frame.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 <= x0 or y1 <= y0 or donor.shape[:2] != frame.shape[:2]:
        return frame
    out = frame.copy()
    bw, bh = x1 - x0, y1 - y0
    alpha = np.ones((bh, bw), np.float32)
    f = max(1, min(feather, bw // 3, bh // 3))
    for i in range(f):
        a = (i + 1) / (f + 1)
        alpha[i, :] = np.minimum(alpha[i, :], a)
        alpha[bh - 1 - i, :] = np.minimum(alpha[bh - 1 - i, :], a)
        alpha[:, i] = np.minimum(alpha[:, i], a)
        alpha[:, bw - 1 - i] = np.minimum(alpha[:, bw - 1 - i], a)
    a3 = alpha[..., None]
    src = out[y0:y1, x0:x1].astype(np.float32)
    don = donor[y0:y1, x0:x1].astype(np.float32)
    out[y0:y1, x0:x1] = np.clip(don * a3 + src * (1.0 - a3), 0, 255).astype(np.uint8)
    return out


def fill_mask_from_donor(
    frame: np.ndarray,
    donor: np.ndarray,
    mask: np.ndarray,
    *,
    max_patch_mad: float = 22.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Copy donor into mask where local neighborhood matches; return (out, residual_mask)."""
    if donor is None or int(mask.max()) == 0 or donor.shape != frame.shape:
        return frame, mask
    ys, xs = np.where(mask > 0)
    if len(ys) == 0:
        return frame, mask
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    # Expand slightly for local context check.
    H, W = frame.shape[:2]
    pad = 6
    cy0, cy1 = max(0, y0 - pad), min(H, y1 + pad)
    cx0, cx1 = max(0, x0 - pad), min(W, x1 + pad)
    ga = cv2.cvtColor(frame[cy0:cy1, cx0:cx1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(donor[cy0:cy1, cx0:cx1], cv2.COLOR_BGR2GRAY).astype(np.float32)
    local = mask[cy0:cy1, cx0:cx1] == 0
    if int(local.sum()) < 24:
        return frame, mask
    mad = float(np.mean(np.abs(ga[local] - gb[local])))
    if mad > max_patch_mad:
        return frame, mask
    out = frame.copy()
    m = (mask > 0).astype(np.float32)
    m = cv2.GaussianBlur(m, (5, 5), 0)[..., None]
    out_f = out.astype(np.float32)
    don_f = donor.astype(np.float32)
    out_f = don_f * m + out_f * (1.0 - m)
    out = np.clip(out_f, 0, 255).astype(np.uint8)
    # Residual = where donor still looks like ink vs cleaned donor (keep small).
    residual = np.zeros_like(mask)
    # If donor cleared the region, residual empty; else keep weak leftovers for LaMa.
    dg = cv2.cvtColor(donor, cv2.COLOR_BGR2GRAY)
    bright_left = (mask > 0) & (dg > 200)
    if int(bright_left.sum()) > 40:
        residual[bright_left] = 255
        residual = cv2.dilate(residual, np.ones((3, 3), np.uint8), iterations=1)
    return out, residual


def _xyxy(box: dict[str, Any], W: int, H: int) -> tuple[int, int, int, int]:
    if all(k in box for k in ("x0", "y0", "x1", "y1")):
        x0, y0, x1, y1 = (int(box[k]) for k in ("x0", "y0", "x1", "y1"))
    else:
        x0 = int(box.get("x", 0))
        y0 = int(box.get("y", 0))
        x1 = x0 + int(box.get("w", 0))
        y1 = y0 + int(box.get("h", 0))
    return max(0, x0), max(0, y0), min(W, x1), min(H, y1)


def boxes_from_regions(regions: Any) -> list[dict[str, Any]]:
    """Normalize regions.json / locate list / override list → [{role,x,y,w,h}]."""
    raw: Iterable[Any]
    if regions is None:
        return []
    if isinstance(regions, (str, Path)):
        p = Path(regions)
        if not p.is_file():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        raw = data.get("boxes") or data.get("regions") or []
    elif isinstance(regions, dict):
        raw = regions.get("boxes") or regions.get("regions") or []
    else:
        raw = regions
    out: list[dict[str, Any]] = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        if str(b.get("action") or "remove").strip().lower() == "keep":
            continue
        role = str(b.get("role") or "dialogue").strip().lower() or "dialogue"
        if all(k in b for k in ("x", "y", "w", "h")):
            out.append(
                {
                    "role": role,
                    "x": int(b["x"]),
                    "y": int(b["y"]),
                    "w": int(b["w"]),
                    "h": int(b["h"]),
                }
            )
        elif all(k in b for k in ("x0", "y0", "x1", "y1")):
            x0, y0, x1, y1 = (int(b[k]) for k in ("x0", "y0", "x1", "y1"))
            out.append({"role": role, "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0})
    return out


def classify_boxes(
    boxes: list[dict[str, Any]], *, frame_h: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into (top_ns, mid_lama, dialogue) — generic, no video-specific ids."""
    top_ns: list[dict[str, Any]] = []
    mid_lama: list[dict[str, Any]] = []
    dialogue: list[dict[str, Any]] = []
    top_cut = max(48, int(frame_h * 0.18))
    for b in boxes:
        role = str(b.get("role") or "dialogue").lower()
        y = int(b.get("y", 0))
        if role == "dialogue":
            dialogue.append(b)
        elif role in ("title",) or y < top_cut:
            top_ns.append(b)
        elif role in ("up_watermark", "watermark", "logo"):
            if y < top_cut:
                top_ns.append(b)
            else:
                mid_lama.append(b)
        else:
            if y < top_cut:
                top_ns.append(b)
            else:
                mid_lama.append(b)
    return top_ns, mid_lama, dialogue


def _dialogue_band_xyxy(box: dict[str, Any] | None, W: int, H: int) -> tuple[int, int, int, int]:
    """Dialogue search band: cover detect box (+pad), at least final2 370×83.

    Long lines need the full confirm box — fixed-only 370-wide clipped left/right.
    Do **not** expand to full frame width here; call ``_widen_band_to_glyphs`` only
    after ``dialogue_glyphs_present`` is true.
    """
    pad_x, pad_y = 36, 14
    min_w, min_h = 370, 83
    if box is None:
        x0 = int(W * _DIALOGUE_BAND_FRAC[0])
        y0 = int(H * _DIALOGUE_BAND_FRAC[1])
        x1 = int(W * _DIALOGUE_BAND_FRAC[2])
        y1 = int(H * _DIALOGUE_BAND_FRAC[3])
        return x0, y0, x1, y1
    x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    cx, cy = x + w / 2.0, y + h / 2.0
    cy_lift = cy - 13  # final2 lift
    gw = max(min_w, w + 2 * pad_x)
    gh = max(min_h, h + 2 * pad_y)
    gx0 = int(round(cx - gw / 2))
    gy0 = int(round(cy_lift - gh / 2))
    gx1 = gx0 + gw
    gy1 = gy0 + gh
    bx0, by0 = x - pad_x, y - pad_y - 13
    bx1, by1 = x + w + pad_x, y + h + pad_y
    x0 = max(0, min(gx0, bx0))
    y0 = max(0, min(gy0, by0))
    x1 = min(W, max(gx1, bx1))
    y1 = min(H, max(gy1, by1))
    return x0, y0, x1, y1


def dialogue_glyphs_present(
    gray: np.ndarray,
    sat: np.ndarray,
    residual: np.ndarray,
    band: tuple[int, int, int, int],
) -> bool:
    """True only when the band looks like a subtitle line — skip no-sub close-ups.

    Teeth / fabric highlights are bright+low-sat but lack a wide multi-blob line.
    """
    x0, y0, x1, y1 = band
    if x1 <= x0 or y1 <= y0:
        return False
    sel = (
        (gray[y0:y1, x0:x1] > 100)
        & (sat[y0:y1, x0:x1] < 90)
        & (residual[y0:y1, x0:x1] > 12)
    )
    px = int(sel.sum())
    if px < 90:
        return False
    cols = np.where(sel.any(axis=0))[0]
    if len(cols) == 0:
        return False
    span = int(cols[-1] - cols[0] + 1)
    if span < 100:
        return False
    m = (sel.astype(np.uint8) * 255)
    nlab, _, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    blobs = sum(
        1
        for i in range(1, nlab)
        if stats[i, cv2.CC_STAT_AREA] >= 14
        and stats[i, cv2.CC_STAT_WIDTH] >= 3
        and stats[i, cv2.CC_STAT_HEIGHT] >= 4
    )
    return blobs >= 3


def _walk_glyph_edge(
    col_frac: np.ndarray, start: int, *, direction: int, thr: float, max_gap: int
) -> int:
    """Walk left (-1) or right (+1) from start across glyph columns; allow small gaps."""
    W = int(col_frac.shape[0])
    x = int(start)
    gap = 0
    last_hit = x
    while True:
        nxt = x + direction
        if nxt < 0 or nxt >= W:
            break
        if col_frac[nxt] > thr:
            x = nxt
            last_hit = x
            gap = 0
        else:
            gap += 1
            if gap > max_gap:
                break
            x = nxt
    return last_hit


def _widen_band_to_glyphs(
    gray: np.ndarray,
    sat: np.ndarray,
    residual: np.ndarray,
    band: tuple[int, int, int, int],
    *,
    W: int,
    pad: int = 40,
) -> tuple[int, int, int, int]:
    """Expand band horizontally to cover full subtitle line (call only if present).

    Long bilingual lines (e.g. BV1P6 f724) spill past the detect box; a tight
    +120 cap clipped 「今/Yo」…「吧/it.」. Walk continuous bright columns from
    the band, allowing small inter-glyph gaps, then pad.
    """
    x0, y0, x1, y1 = band
    if x1 <= x0 or y1 <= y0:
        return band
    sel = (
        (gray[y0:y1] > 90)
        & (sat[y0:y1] < 95)
        & (residual[y0:y1] > 8)
    )
    col_frac = sel.mean(axis=0) if sel.size else np.zeros(W, np.float32)
    thr = 0.035
    left = _walk_glyph_edge(col_frac, x0, direction=-1, thr=thr, max_gap=18)
    right = _walk_glyph_edge(col_frac, x1 - 1, direction=1, thr=thr, max_gap=18)
    gx0 = max(0, left - pad)
    gx1 = min(W, right + 1 + pad)
    return min(x0, gx0), y0, max(x1, gx1), y1


def _expand_top_box(
    box: dict[str, Any], W: int, H: int, gray: np.ndarray
) -> tuple[int, int, int, int]:
    """Expand top watermark box to cover glyph tails (Morphic underbelly)."""
    x0, y0, x1, y1 = _xyxy(box, W, H)
    if y0 <= int(H * 0.15) and x1 > x0:
        # Morphic "Made with" often sits below regions h=48.
        y1 = min(H, y1 + 10)
        extra = min(int(box.get("h", 40)), H - y1)
        if extra > 0:
            probe = gray[y1 : min(H, y1 + extra), x0:x1].astype(np.uint8)
            if probe.size:
                row_frac = (probe > 150).mean(axis=1)[:12]
                rows = np.where(row_frac > 0.008)[0]
                if len(rows):
                    y1 = min(H, y1 + int(rows[-1]) + 5)
        if x0 >= int(W * 0.45):
            x0 = max(0, x0 - 24)
            x1 = min(W, x1 + 20)
        elif x0 <= int(W * 0.25):
            x0 = 0
            x1 = min(W, x1 + 12)
    return x0, y0, x1, y1


def _top_box_expanded(
    frame_bgr: np.ndarray, box: dict[str, Any]
) -> dict[str, int]:
    """Expand top watermark box (generic; optional visual_cleanup helper)."""
    H, W = frame_bgr.shape[:2]
    try:
        from visual_cleanup import expand_static_overlay_box

        return expand_static_overlay_box(
            {
                "x": int(box["x"]),
                "y": int(box["y"]),
                "w": int(box["w"]),
                "h": int(box["h"]),
            },
            W,
            H,
            role="up_watermark",
        )
    except Exception:
        gray0 = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        x0, y0, x1, y1 = _expand_top_box(box, W, H, gray0)
        return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def top_ink_mask(
    frame_bgr: np.ndarray, box: dict[str, Any]
) -> tuple[dict[str, int], np.ndarray]:
    """Generic top-logo mask via local residual — not raw brightness.

    Flat bright windows/beams make gray>170 catch the whole plate; glyphs stick
    out vs local median. Component filter rejects near-full-box plates.
    Cap mask area so a bad frame cannot fog the entire bar.
    """
    H, W = frame_bgr.shape[:2]
    eb = _top_box_expanded(frame_bgr, box)
    x0, y0, bw, bh = int(eb["x"]), int(eb["y"]), int(eb["w"]), int(eb["h"])
    mask = np.zeros((H, W), np.uint8)
    roi = frame_bgr[y0 : y0 + bh, x0 : x0 + bw]
    if roi.size < 64 or bw < 4 or bh < 4:
        return eb, mask
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sat = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)[..., 1].astype(np.float32)
    med = cv2.medianBlur(g.astype(np.uint8), 15).astype(np.float32)
    residual = g - med
    sel = (residual > 22) & (sat < 110) & (g > 120)
    ink = (sel.astype(np.uint8) * 255)
    ink = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    nlab, lab, stats, _ = cv2.connectedComponentsWithStats(ink, 8)
    kept = np.zeros_like(ink)
    max_area = max(80, int(bw * bh * 0.22))
    for i in range(1, nlab):
        area = int(stats[i, cv2.CC_STAT_AREA])
        ww = int(stats[i, cv2.CC_STAT_WIDTH])
        hh = int(stats[i, cv2.CC_STAT_HEIGHT])
        if area < 8 or area > max_area:
            continue
        if hh < 3 or ww < 2:
            continue
        if ww > bw * 0.85 and hh > bh * 0.55:
            continue
        kept[lab == i] = 255
    if int(kept.max()) == 0:
        # Dark-BG logos: residual weak — fall back to hard white, still CC-filtered.
        raw = ((g > 175) & (sat < 110)).astype(np.uint8) * 255
        raw = cv2.morphologyEx(raw, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        nlab, lab, stats, _ = cv2.connectedComponentsWithStats(raw, 8)
        for i in range(1, nlab):
            area = int(stats[i, cv2.CC_STAT_AREA])
            ww = int(stats[i, cv2.CC_STAT_WIDTH])
            hh = int(stats[i, cv2.CC_STAT_HEIGHT])
            if 8 <= area <= max_area and hh >= 3 and ww >= 2:
                if not (ww > bw * 0.85 and hh > bh * 0.55):
                    kept[lab == i] = 255
    kept = cv2.dilate(kept, np.ones((3, 3), np.uint8), iterations=1)
    soft = ((residual > 12) & (sat < 100) & (g > 110)).astype(np.uint8) * 255
    near = cv2.dilate(kept, np.ones((7, 7), np.uint8), iterations=1)
    soft = cv2.bitwise_and(soft, near)
    hole = np.maximum(kept, soft)
    # Hard cap: never mask >35% of the top box (anti fog-plate).
    cap = int(bw * bh * 0.35)
    if int((hole > 0).sum()) > cap > 0:
        # Keep only strongest residual pixels.
        score = residual.copy()
        score[hole == 0] = -1e9
        flat = score.reshape(-1)
        idx = np.argpartition(flat, -cap)[-cap:]
        tight = np.zeros_like(hole)
        ys, xs = np.unravel_index(idx, score.shape)
        tight[ys, xs] = 255
        tight = cv2.dilate(tight, np.ones((3, 3), np.uint8), iterations=1)
        hole = tight
    mask[y0 : y0 + bh, x0 : x0 + bw] = hole
    return eb, mask


# Back-compat alias
_morphic_ink_mask = top_ink_mask


def _ink_tile_from_neighbor(
    frame: np.ndarray, eb: dict[str, int], mask: np.ndarray, *, side: str
) -> np.ndarray:
    """Paste a narrow clean strip onto ink mask only (never full-box replace)."""
    H, W = frame.shape[:2]
    x0, y0, bw, bh = int(eb["x"]), int(eb["y"]), int(eb["w"]), int(eb["h"])
    if bw < 8 or bh < 8 or int(mask[y0 : y0 + bh, x0 : x0 + bw].max()) == 0:
        return frame
    near_w = max(24, min(48, bw // 8))
    zone = frame[y0 : y0 + bh, x0 : x0 + bw]
    zg = cv2.cvtColor(zone, cv2.COLOR_BGR2GRAY)
    z_bg = zg[zg < 150]
    zone_med = float(np.median(z_bg)) if z_bg.size >= 32 else float(np.median(zg))
    best: tuple[float, np.ndarray] | None = None

    def _score_sample(sample: np.ndarray) -> float:
        sg = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
        bright = float((sg > 170).mean())
        med = float(np.median(sg))
        score = bright * 3.0 + abs(med - zone_med) / 80.0
        if med + 15.0 < zone_med:
            score += 2.5
        return score

    if side == "left":
        for gap in (2, 16, 32, 48, 72, 96, 128, 160):
            sx1 = max(near_w, x0 - gap)
            sx0 = max(0, sx1 - near_w)
            if sx1 - sx0 < 8:
                continue
            sample = frame[y0 : y0 + bh, sx0:sx1]
            if sample.shape[1] != bw:
                sample = cv2.resize(sample, (bw, bh), interpolation=cv2.INTER_LINEAR)
            sc = _score_sample(sample)
            if best is None or sc < best[0]:
                best = (sc, sample)
    else:
        # Right or below for left-top logos.
        for gap in (2, 16, 32, 48, 72, 96):
            sx0 = min(W - near_w, x0 + bw + gap)
            sx1 = min(W, sx0 + near_w)
            if sx1 - sx0 < 8:
                continue
            sample = frame[y0 : y0 + bh, sx0:sx1]
            if sample.shape[1] != bw:
                sample = cv2.resize(sample, (bw, bh), interpolation=cv2.INTER_LINEAR)
            sc = _score_sample(sample)
            if best is None or sc < best[0]:
                best = (sc, sample)
        sy0 = min(H - bh, y0 + bh + 2)
        sy1 = min(H, sy0 + bh)
        below = frame[sy0:sy1, x0 : x0 + bw]
        if below.shape[0] == bh and below.shape[1] == bw:
            hsv = cv2.cvtColor(below, cv2.COLOR_BGR2HSV)
            skin = float(
                (
                    (hsv[..., 0] < 25)
                    & (hsv[..., 1] > 35)
                    & (hsv[..., 2] > 60)
                    & (hsv[..., 2] < 220)
                ).mean()
            )
            if skin < 0.10:
                sc = _score_sample(below)
                if best is None or sc < best[0]:
                    best = (sc, below)

    if best is None:
        return frame
    sample = best[1]
    dg = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
    if abs(float(np.median(dg)) - zone_med) > 16.0:
        return frame
    out = frame.copy()
    a = mask[y0 : y0 + bh, x0 : x0 + bw].astype(np.float32) / 255.0
    a = cv2.GaussianBlur(a, (5, 5), 0)[..., None]
    roi = out[y0 : y0 + bh, x0 : x0 + bw].astype(np.float32)
    out[y0 : y0 + bh, x0 : x0 + bw] = np.clip(
        sample.astype(np.float32) * a + roi * (1.0 - a), 0, 255
    ).astype(np.uint8)
    return out


def _box_key(b: dict[str, Any]) -> tuple[int, int, int, int]:
    return (int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"]))


def _ns_wipe_one_box(frame: np.ndarray, b: dict[str, Any]) -> np.ndarray:
    eb = {
        "x": int(b["x"]),
        "y": int(b["y"]),
        "w": int(b["w"]),
        "h": int(b["h"]),
    }
    try:
        from visual_cleanup import ns_box_wipe

        return ns_box_wipe(
            frame, eb, feather=GOLD_TOP_FEATHER, radius=GOLD_TOP_RADIUS
        )
    except Exception:
        H, W = frame.shape[:2]
        x0, y0, x1, y1 = _xyxy(b, W, H)
        mask = np.zeros((H, W), np.uint8)
        mask[y0:y1, x0:x1] = 255
        return cv2.inpaint(frame, mask, GOLD_TOP_RADIUS, cv2.INPAINT_NS)


def build_top_plate_for_window(
    source: str | Path,
    box: dict[str, Any],
    center_idx: int,
    *,
    radius: int = 24,
    max_samples: int = 17,
    max_temporal_l1: float = 12.0,
) -> np.ndarray | None:
    """Keyframe-local top cleanplate: median of NS-wiped ROIs in a temporal window.

    Static shots (lockers/walls) get a sharp plate without single-frame NS fog.
    High motion windows return None → caller falls back to per-frame NS.
    """
    source = Path(source)
    x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    if w < 8 or h < 8:
        return None
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        return None
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total < 4:
            return None
        lo = max(0, int(center_idx) - int(radius))
        hi = min(total - 1, int(center_idx) + int(radius))
        span = max(1, hi - lo)
        n = min(max_samples, span + 1)
        idxs = sorted(
            {int(round(lo + i * span / max(n - 1, 1))) for i in range(n)}
        )
        stack: list[np.ndarray] = []
        for i in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, i)
            ok, fr = cap.read()
            if not ok or fr is None:
                continue
            wiped = _ns_wipe_one_box(fr, box)
            roi = wiped[y : y + h, x : x + w]
            if roi.shape[0] != h or roi.shape[1] != w:
                continue
            stack.append(roi.astype(np.float32))
        if len(stack) < 5:
            return None
        st = np.stack(stack, axis=0)
        # Temporal stability on wiped stack (logo already gone).
        diffs: list[float] = []
        for j in range(len(st) - 1):
            diffs.append(float(np.mean(np.abs(st[j] - st[j + 1]))))
        tl1 = float(np.mean(diffs)) if diffs else 255.0
        if tl1 > max_temporal_l1:
            return None
        plate = np.median(st, axis=0)
        return np.clip(plate, 0, 255).astype(np.uint8)
    finally:
        cap.release()


def build_top_plate_bank(
    source: str | Path,
    boxes: list[dict[str, Any]],
    *,
    indices: list[int] | None = None,
    stride_sec: float | None = None,
    max_frames: int | None = None,
) -> dict[int, dict[tuple[int, int, int, int], np.ndarray]]:
    """Pass-A top plates keyed by keyframe index → {box_xywh: plate_roi}."""
    source = Path(source)
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {source}")
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if max_frames is not None:
            total = min(total, int(max_frames))
        H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    finally:
        cap.release()
    top_ns, _, _ = classify_boxes(boxes, frame_h=H or 480)
    if not top_ns:
        return {}
    idxs = (
        list(indices)
        if indices is not None
        else keyframe_indices(total, fps, stride_sec=stride_sec)
    )
    idxs = [i for i in idxs if 0 <= i < total]
    bank: dict[int, dict[tuple[int, int, int, int], np.ndarray]] = {}
    for n, i in enumerate(idxs, 1):
        plates: dict[tuple[int, int, int, int], np.ndarray] = {}
        for b in top_ns:
            plate = build_top_plate_for_window(source, b, i)
            if plate is not None:
                plates[_box_key(b)] = plate
        if plates:
            bank[i] = plates
        if n % 10 == 0 or n == len(idxs):
            print(
                f"[gold_workflow] top_plates {n}/{len(idxs)} "
                f"(last_idx={i} n_boxes={len(plates)})",
                flush=True,
            )
    return bank


def nearest_top_plates(
    bank: dict[int, dict[tuple[int, int, int, int], np.ndarray]],
    idx: int,
) -> dict[tuple[int, int, int, int], np.ndarray] | None:
    if not bank:
        return None
    best_i = min(bank.keys(), key=lambda k: abs(int(k) - int(idx)))
    # Refuse plates from too far away (cross-shot paste).
    if abs(int(best_i) - int(idx)) > 45:
        return None
    return bank[best_i]


def _paste_plate_roi(
    frame: np.ndarray,
    plate: np.ndarray,
    box: dict[str, Any],
    *,
    feather: int = 8,
    match_thr: float = 16.0,
) -> np.ndarray | None:
    """Paste plate into box if non-ink background matches; else None."""
    x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    H, W = frame.shape[:2]
    if x < 0 or y < 0 or x + w > W or y + h > H:
        return None
    if plate.shape[0] != h or plate.shape[1] != w:
        return None
    roi = frame[y : y + h, x : x + w]
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    pg = cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY)
    # Compare on darker (non-logo) pixels of the *source* ROI.
    bg = g < 150
    if int(bg.sum()) < 32:
        bg = np.ones_like(g, dtype=bool)
    mad = float(np.mean(np.abs(g[bg].astype(np.float32) - pg[bg].astype(np.float32))))
    if mad > match_thr:
        return None
    out = frame.copy()
    alpha = np.ones((h, w), np.float32)
    f = max(1, min(feather, w // 3, h // 3))
    for i in range(f):
        a = (i + 1) / (f + 1)
        alpha[i, :] = np.minimum(alpha[i, :], a)
        alpha[h - 1 - i, :] = np.minimum(alpha[h - 1 - i, :], a)
        alpha[:, i] = np.minimum(alpha[:, i], a)
        alpha[:, w - 1 - i] = np.minimum(alpha[:, w - 1 - i], a)
    a3 = alpha[..., None]
    mixed = plate.astype(np.float32) * a3 + roi.astype(np.float32) * (1.0 - a3)
    out[y : y + h, x : x + w] = np.clip(mixed, 0, 255).astype(np.uint8)
    return out


def wipe_top_watermarks(
    frame_bgr: np.ndarray,
    boxes: list[dict[str, Any]],
    *,
    donor: np.ndarray | None = None,  # mid/dialogue donor; unused for top
    plates: dict[tuple[int, int, int, int], np.ndarray] | None = None,
    **_kwargs: Any,
) -> np.ndarray:
    """Top wipe: keyframe NS-median plate when stable, else raw-box ``ns_box_wipe``.

    Still uses regions **raw** boxes (no ``expand_static_overlay_box``). Plates are
    built per keyframe window (median of NS wipes) so static shots keep texture
    instead of single-frame NS fog.
    """
    del donor, _kwargs
    H, W = frame_bgr.shape[:2]
    top_ns, _, _ = classify_boxes(boxes, frame_h=H)
    if not top_ns:
        return frame_bgr

    out = frame_bgr.copy()
    for b in top_ns:
        used = False
        if plates:
            plate = plates.get(_box_key(b))
            if plate is not None:
                pasted = _paste_plate_roi(out, plate, b, feather=8, match_thr=16.0)
                if pasted is not None:
                    out = pasted
                    used = True
        if not used:
            out = _ns_wipe_one_box(out, b)
    return out


def build_gold_masks(
    frame_bgr: np.ndarray,
    boxes: list[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray]:
    """Return (ns_mask, lama_mask). ns_mask is unused (kept for API compat)."""
    H, W = frame_bgr.shape[:2]
    _top_ns, mid_lama, dialogue = classify_boxes(boxes, frame_h=H)
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sat = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)[..., 1].astype(np.float32)
    local_med = cv2.medianBlur(gray.astype(np.uint8), 15).astype(np.float32)
    residual = gray - local_med

    # Top bars are wiped in ``wipe_top_watermarks`` (gold ns_box_wipe).
    ns_mask = np.zeros((H, W), np.uint8)

    lama_mask = np.zeros((H, W), np.uint8)
    for b in mid_lama:
        x0, y0, x1, y1 = _xyxy(b, W, H)
        if x1 <= x0 or y1 <= y0:
            continue
        if x0 < int(W * 0.45):
            thr_res, thr_gray, thr_sat, k = 10.0, 45.0, 110.0, 9
        else:
            thr_res, thr_gray, thr_sat, k = 9.0, 50.0, 105.0, 7
        sel = (
            (residual[y0:y1, x0:x1] > thr_res)
            & (gray[y0:y1, x0:x1] > thr_gray)
            & (sat[y0:y1, x0:x1] < thr_sat)
        )
        if sel.any():
            lama_mask[y0:y1, x0:x1] = np.maximum(
                lama_mask[y0:y1, x0:x1],
                cv2.dilate(sel.astype(np.uint8) * 255, np.ones((k, k), np.uint8)),
            )

    dlg = dialogue[0] if dialogue else None
    band = _dialogue_band_xyxy(dlg, W, H)
    # No-sub frames (close-ups, teeth, etc.): do not touch dialogue band at all.
    if dialogue_glyphs_present(gray, sat, residual, band):
        x0, y0, x1, y1 = _widen_band_to_glyphs(
            gray, sat, residual, band, W=W
        )
        if x1 > x0 and y1 > y0:
            # Gold criterion (final2): gray>90 & sat<95
            sel = (gray[y0:y1, x0:x1] > 90) & (sat[y0:y1, x0:x1] < 95)
            if sel.any():
                m = cv2.dilate(
                    sel.astype(np.uint8) * 255,
                    cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)),
                )
                # Extra end-cap dilate so first/last glyphs (今 / it.) are not clipped.
                end = max(48, (x1 - x0) // 8)
                left = m[:, :end]
                right = m[:, -end:]
                k15 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
                m[:, :end] = cv2.dilate(left, k15)
                m[:, -end:] = cv2.dilate(right, k15)
                lama_mask[y0:y1, x0:x1] = np.maximum(lama_mask[y0:y1, x0:x1], m)
    if int(lama_mask.max()) > 0:
        lama_mask = cv2.morphologyEx(
            lama_mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)
        )
    return ns_mask, lama_mask


_LAMA_LOCK = threading.Lock()
_LAMA_READY = False


def _ensure_lama() -> None:
    """Warm ONNX/LaMa once so first-frame latency is not paid mid-batch."""
    global _LAMA_READY
    if _LAMA_READY:
        return
    from inpaint_lama import lama_inpaint  # noqa: F401 — import loads session

    try:
        from inpaint_lama import _load_session

        _load_session()
    except Exception:
        pass
    _LAMA_READY = True


def prepare_frame(
    frame_bgr: np.ndarray,
    boxes: list[dict[str, Any]],
    *,
    donor: np.ndarray | None = None,
    top_plates: dict[tuple[int, int, int, int], np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """CPU stage: top wipe (plate or NS) + LaMa mask.

    Safe to run in parallel workers. ``top_plates`` from keyframe NS-median bank;
    ``donor`` only fills mid/dialogue residual before LaMa.
    """
    fixed = wipe_top_watermarks(frame_bgr, boxes, plates=top_plates)
    _ns, lama_mask = build_gold_masks(fixed, boxes)
    if donor is not None and int(lama_mask.max()) > 0:
        fixed, lama_mask = fill_mask_from_donor(fixed, donor, lama_mask)
    return fixed, lama_mask


def apply_lama(frame_bgr: np.ndarray, lama_mask: np.ndarray) -> np.ndarray:
    """GPU stage: single LaMa call (serialized)."""
    if int(lama_mask.max()) == 0:
        return frame_bgr
    from inpaint_lama import lama_inpaint

    with _LAMA_LOCK:
        return lama_inpaint(frame_bgr, lama_mask)


def process_frame(
    frame_bgr: np.ndarray,
    boxes: list[dict[str, Any]],
    *,
    donor: np.ndarray | None = None,
    top_plates: dict[tuple[int, int, int, int], np.ndarray] | None = None,
) -> np.ndarray:
    """Apply gold workflow to one BGR frame. Pure CPU+LaMa; thread-safe via lock."""
    fixed, lama_mask = prepare_frame(
        frame_bgr, boxes, donor=donor, top_plates=top_plates
    )
    return apply_lama(fixed, lama_mask)


def process_frame_seed(frame_bgr: np.ndarray, boxes: list[dict[str, Any]]) -> np.ndarray:
    """Pass-A seed: pure per-frame gold (no donor) — builds the keyframe bank."""
    return process_frame(frame_bgr, boxes, donor=None)


def build_gold_keyframe_bank(
    source: str | Path,
    boxes: list[dict[str, Any]],
    *,
    indices: list[int] | None = None,
    stride_sec: float | None = None,
    max_frames: int | None = None,
    workers: int | None = None,
) -> list[tuple[int, np.ndarray]]:
    """Pass A: sample keyframes → cleaned gold frames ``[(idx, bgr), ...]``."""
    source = Path(source)
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {source}")
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 30) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if max_frames is not None:
            total = min(total, int(max_frames))
        idxs = list(indices) if indices is not None else keyframe_indices(
            total, fps, stride_sec=stride_sec
        )
        idxs = [i for i in idxs if 0 <= i < total]
        if not idxs:
            return []
        _ensure_lama()
        n_workers = workers if workers is not None else _worker_count()

        def _load(i: int) -> tuple[int, np.ndarray] | None:
            # Each worker needs its own capture for seek safety on Windows.
            c = cv2.VideoCapture(str(source))
            try:
                c.set(cv2.CAP_PROP_POS_FRAMES, int(i))
                ok, fr = c.read()
                if not ok or fr is None:
                    return None
                return int(i), fr
            finally:
                c.release()

        raw: list[tuple[int, np.ndarray]] = []
        if n_workers <= 1 or len(idxs) == 1:
            for i in idxs:
                got = _load(i)
                if got is not None:
                    raw.append(got)
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                for got in pool.map(_load, idxs):
                    if got is not None:
                        raw.append(got)
        raw.sort(key=lambda t: t[0])

        bank: list[tuple[int, np.ndarray]] = []
        # Seed frames: pure gold, no donor (bootstrap). Serial LaMa.
        for i, fr in raw:
            bank.append((i, process_frame_seed(fr, boxes)))
            if len(bank) % 10 == 0 or len(bank) == len(raw):
                print(
                    f"[gold_workflow] keyframes {len(bank)}/{len(raw)} "
                    f"(last_idx={i})",
                    flush=True,
                )
        return bank
    finally:
        cap.release()


def _worker_count() -> int:
    raw = (os.environ.get(GOLD_WORKERS_ENV) or "2").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 2
    # Mask+NS is CPU-bound; more than ~4 rarely helps on typical 4–8 core boxes.
    return max(1, min(n, 8))


def _ffmpeg_remux(video_no_audio: Path, source: Path, dest: Path) -> bool:
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    cmd = [
        ff,
        "-y",
        "-i",
        str(video_no_audio),
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "veryfast",
        "-c:a",
        "aac",
        "-shortest",
        str(dest),
    ]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(cmd, capture_output=True, creationflags=flags)
        return r.returncode == 0 and dest.is_file() and dest.stat().st_size > 1000
    except OSError:
        return False


def run_gold_workflow(
    source: str | Path,
    out: str | Path,
    boxes: list[dict[str, Any]] | None = None,
    *,
    regions: Any = None,
    max_frames: int | None = None,
    progress_path: Path | None = None,
    workers: int | None = None,
    use_keyframes: bool | None = None,
    keyframe_stride_sec: float | None = None,
) -> dict[str, Any]:
    """Encode full (or capped) video with gold workflow. Returns report dict.

    Default: Pass A builds a gold keyframe bank, Pass B propagates from nearest
    cleaned keyframe and only LaMa-fills residual holes.
    """
    source, out = Path(source), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    box_list = list(boxes or [])
    if not box_list and regions is not None:
        box_list = boxes_from_regions(regions)
    if not box_list:
        raise RuntimeError("gold workflow needs at least one region/box")

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {source}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30) or 30.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    limit = total if max_frames is None else min(total, int(max_frames))
    n_workers = workers if workers is not None else _worker_count()
    do_kf = gold_keyframe_enabled() if use_keyframes is None else bool(use_keyframes)
    stride = (
        float(keyframe_stride_sec)
        if keyframe_stride_sec is not None
        else gold_keyframe_stride_sec()
    )

    t0 = time.time()
    _ensure_lama()

    bank: list[tuple[int, np.ndarray]] = []
    plate_bank: dict[int, dict[tuple[int, int, int, int], np.ndarray]] = {}
    # Top plates (NS-median / src-median) were A/B'd on BV1P6 hard KFs and
    # lost to raw-box ns_box_wipe vs gold50 — keep code but default OFF.
    use_top_plates = (os.environ.get("VITUAL_DEHARDSUB_GOLD_TOP_PLATE") or "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if limit > 1 and use_top_plates:
        print(
            f"[gold_workflow] Pass A: top plates stride={stride}s "
            f"limit={limit}",
            flush=True,
        )
        plate_bank = build_top_plate_bank(
            source,
            box_list,
            stride_sec=stride,
            max_frames=limit,
        )
        print(
            f"[gold_workflow] Pass A top_plates: {len(plate_bank)} keyframes "
            f"elapsed={time.time() - t0:.0f}s",
            flush=True,
        )
    if do_kf and limit > 1:
        print(
            f"[gold_workflow] Pass A: mid/dialogue keyframes "
            f"stride={stride}s",
            flush=True,
        )
        if progress_path is not None:
            try:
                progress_path.write_text(
                    json.dumps(
                        {
                            "stage": "gold_keyframes",
                            "done": 0,
                            "total": limit,
                            "percent": 0,
                        }
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
        bank = build_gold_keyframe_bank(
            source,
            box_list,
            stride_sec=stride,
            max_frames=limit,
            workers=n_workers,
        )
        print(
            f"[gold_workflow] Pass A donor bank: {len(bank)} keyframes "
            f"elapsed={time.time() - t0:.0f}s",
            flush=True,
        )

    # Re-open for sequential Pass B read (bank build used its own captures).
    cap.release()
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"cannot reopen {source}")

    tmp = out.with_name(out.stem + "._gold_tmp.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(tmp), fourcc, fps, (W, H))
    done = 0

    # Pipeline: read → parallel CPU (mask+NS+donor) → serial GPU LaMa → write.
    batch_size = max(8, n_workers * 4)

    def _prep(item: tuple[int, np.ndarray]) -> tuple[int, np.ndarray, np.ndarray]:
        idx, fr = item
        don = None
        hit = nearest_keyframe(bank, idx)
        if hit is not None:
            don = hit[1]
        plates = nearest_top_plates(plate_bank, idx)
        fixed, mask = prepare_frame(
            fr, box_list, donor=don, top_plates=plates
        )
        return idx, fixed, mask

    def _read_batch(start_idx: int) -> list[tuple[int, np.ndarray]]:
        batch: list[tuple[int, np.ndarray]] = []
        while len(batch) < batch_size and start_idx + len(batch) < limit:
            ok, fr = cap.read()
            if not ok:
                break
            batch.append((start_idx + len(batch), fr))
        return batch

    def _prep_batch(
        batch: list[tuple[int, np.ndarray]],
        pool: ThreadPoolExecutor | None,
    ) -> list[tuple[int, np.ndarray, np.ndarray]]:
        if not batch:
            return []
        if pool is None or len(batch) == 1:
            out_b = [_prep(it) for it in batch]
        else:
            out_b = list(pool.map(_prep, batch))
        out_b.sort(key=lambda t: t[0])
        return out_b

    def _write_progress(n: int) -> None:
        if progress_path is None or not (n % 20 == 0 or n == limit):
            return
        try:
            progress_path.write_text(
                json.dumps(
                    {
                        "stage": "gold_workflow",
                        "done": n,
                        "total": limit,
                        "percent": int(100 * n / max(limit, 1)),
                        "keyframes": len(bank),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    print(
        f"[gold_workflow] Pass B: full render keyframes={len(bank)} "
        f"workers={n_workers}",
        flush=True,
    )
    pool: ThreadPoolExecutor | None = (
        ThreadPoolExecutor(max_workers=n_workers) if n_workers > 1 else None
    )
    try:
        cur = _prep_batch(_read_batch(0), pool)
        while cur:
            next_raw = _read_batch(done + len(cur))
            futs = (
                [pool.submit(_prep, it) for it in next_raw]
                if (next_raw and pool is not None and len(next_raw) > 1)
                else []
            )

            for _, fixed, mask in cur:
                vw.write(apply_lama(fixed, mask))
                done += 1
                _write_progress(done)
                if done % 100 == 0 or done == limit:
                    print(
                        f"[gold_workflow] {done}/{limit} "
                        f"elapsed={time.time() - t0:.0f}s workers={n_workers}",
                        flush=True,
                    )

            if futs:
                cur = [f.result() for f in futs]
                cur.sort(key=lambda t: t[0])
            elif next_raw:
                cur = _prep_batch(next_raw, pool)
            else:
                cur = []
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
        cap.release()
        vw.release()

    # Prefer ffmpeg remux with audio + H.264; else keep mp4v.
    if not _ffmpeg_remux(tmp, source, out):
        if out.exists():
            out.unlink()
        tmp.replace(out)
    else:
        tmp.unlink(missing_ok=True)

    elapsed = time.time() - t0
    top_ns, mid_lama, dialogue = classify_boxes(box_list, frame_h=H)
    report = {
        "ok": True,
        "action": "gold_workflow",
        "engine": "gold_workflow",
        "frames": done,
        "elapsed_sec": round(elapsed, 2),
        "fps_eff": round(done / elapsed, 3) if elapsed > 0 else None,
        "workers": n_workers,
        "boxes": box_list,
        "box_roles": [b.get("role") for b in box_list],
        "top_ns": len(top_ns),
        "mid_lama": len(mid_lama),
        "dialogue": len(dialogue),
        "keyframes": len(bank),
        "keyframe_stride_sec": stride if do_kf else None,
        "top_gold": top_gold_contract(),
        "dest": str(out),
        "quality": "gold_benchmark",
        "quality_note": (
            "top=regions_raw+ns_box_wipe(feather6,radius5); "
            "mid/dialogue=optional KF donor + residual LaMa; "
            "locked to src_0001_final2 / gold50"
        ),
    }
    print(
        f"[gold_workflow] done frames={done} keyframes={len(bank)} "
        f"elapsed={elapsed:.1f}s eff={report['fps_eff']} → {out}",
        flush=True,
    )
    return report


def persist_benchmark_clip(
    src_clip: Path,
    dest_dir: Path,
    *,
    meta: dict[str, Any] | None = None,
) -> Path:
    """Copy accepted gold clip + meta into eval/gold for regression."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src_clip.name
    shutil.copy2(src_clip, dest)
    (dest_dir / "benchmark.json").write_text(
        json.dumps(
            {
                "name": "dehardsub_gold_workflow",
                "accepted": "2026-11-24",
                "reference": "fix_frame1_final2.py / src_0001_final2.jpg",
                "clip": dest.name,
                "meta": meta or {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return dest
