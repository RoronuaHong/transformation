"""通用型去字幕后处理：检测并修复三类渲染遗留瑕疵。

设计原则（通用型，不含任何视频特定帧号/坐标/亮度常量）：
- 输入只有 source.mp4、clean.mp4、regions.json（框来自 D0 检测结果）。
- 三个检测器都用"相对同帧/邻帧"的判据，自适应不同场景/亮度/分辨率：
    1. 切镜重复帧：clean 与 source 的全帧灰差在某点成对骤升（平时基线 K 倍），
       说明 clean 在切点重复了上一镜的 1~2 帧。用 source 帧重渲染去字后替换。
    2. 水印残留糊斑：固定水印框内 clean 出现 source 没有的亮块（相对框内环带
       显著偏亮的小连通块）。用 source 帧对该框做 NS 填充后替换。
    3. 字幕带压黑块：dialogue 框内 clean 相对 source 出现成片压暗（字形/环带
       的暗连通块）。用同帧环带中值回填该块。
- 修复只改检测到的帧/区域，其余帧原样透传；保留源音轨。
- opt-out 开关 VITUAL_DEHARDSUB_POSTFIX=0 可关闭（默认开）。

入口：
    run_postfix(source, clean, out, regions_path, *, stats_out=None) -> dict
"""
from __future__ import annotations

import json
import os
import subprocess
from collections import deque
from pathlib import Path
from typing import Any

import cv2
import numpy as np

POSTFIX_ENV = "VITUAL_DEHARDSUB_POSTFIX"
DARK_PATCH_ENV = "VITUAL_DEHARDSUB_POSTFIX_DARK"  # 压黑块修复默认关（见 detect_dark_patch 文档）

# 相对判据阈值（无量纲，非视频特定亮度）
_DIFF_SPIKE_FACTOR = 3.0     # 切点灰差 > 基线*3
_DIFF_SPIKE_MIN = 15.0       # 且绝对差 > 15（避免纯黑帧误报）
_BRIGHT_Z = 2.5              # 水印残留：相对环带亮 z
_DARK_Z = 1.8                # 压黑块：相对环带暗 z
_MIN_COMPONENT_AREA = 24     # 连通块最小面积（自适应分辨率时按比例缩放）


def postfix_enabled() -> bool:
    return os.environ.get(POSTFIX_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def _gray(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    return "ffmpeg"


def _load_boxes(regions_path: str | Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """从 regions.json 读 dialogue 框与所有 up_watermark 框。"""
    p = Path(regions_path)
    if not p.exists():
        return None, []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None, []
    boxes = data.get("boxes") if isinstance(data, dict) else None
    if not isinstance(boxes, list):
        return None, []
    dialogue = next((b for b in boxes if b.get("role") == "dialogue"), None)
    watermarks = [b for b in boxes if b.get("role") == "up_watermark"]
    return dialogue, watermarks


def _clamp_box(box: dict[str, Any], H: int, W: int) -> tuple[int, int, int, int]:
    x, y = max(0, int(box.get("x", 0))), max(0, int(box.get("y", 0)))
    x1 = min(W, x + int(box.get("w", 0)))
    y1 = min(H, y + int(box.get("h", 0)))
    return x, y, max(x, x1), max(y, y1)


# ---------------------------------------------------------------- 检测

_BRIGHT_TEXT_MIN_CONTRAST = 15.0   # 源框内亮字相对中值的 95 分位对比
_BRIGHT_TEXT_MAX_SRC_STD = 45.0    # 超出则视为复杂场景，不做整框 NS
_BRIGHT_TEXT_STD_RATIO = 0.62      # clean 框必须比源框字形明显更平


def _bright_watermark_residual_hit(gs: np.ndarray, gc: np.ndarray) -> bool:
    """亮字水印被擦后留下低对比糊斑（BV1P6 右下 @ 帧 733 类）。

    与已有 ``wm_hits``（深字→亮糊斑）互补：这里源框是暗底亮字，clean 框
    仍偏平但留有字形糊影。判据全部相对同帧两框，不含视频特定亮度/坐标。
    """
    if gs.size < 200:
        return False
    s_std = float(gs.std())
    c_std = float(gc.std())
    if s_std < 4 or s_std > _BRIGHT_TEXT_MAX_SRC_STD:
        return False
    if float(np.percentile(gs, 95) - np.median(gs)) < _BRIGHT_TEXT_MIN_CONTRAST:
        return False
    if float((gs > 140).mean()) > 0.10 or float((gc > 140).mean()) > 0.05:
        return False
    if c_std > s_std * _BRIGHT_TEXT_STD_RATIO:
        return False
    # 完全擦净的平背景 95 分位与中值几乎重合；糊斑会留下几级起伏。
    return float(np.percentile(gc, 95) - np.median(gc)) >= 4.0


def _scan_detections(
    source: str | Path,
    clean: str | Path,
    watermarks: list[dict[str, Any]],
    dialogue: dict[str, Any] | None,
    dark_enabled: bool,
) -> dict[str, Any]:
    """单遍顺序读 source/clean，同时算切点灰差、水印糊斑、压黑块与精确帧数。

    三类检测合成一遍解码，避免 3 次全片 I/O；帧数按两路各自顺序累计，
    一路先结束后仍把另一路剩余帧读完，得到可信的 source_frames/clean_frames。
    """
    cs, cc = cv2.VideoCapture(str(source)), cv2.VideoCapture(str(clean))
    diffs: list[float] = []
    wm_hits: list[tuple[int, int]] = []
    bright_hits: list[tuple[int, int]] = []
    dark_hits: list[int] = []
    src_n = 0
    clean_n = 0
    fi = 0
    while True:
        ok1, s = cs.read()
        ok2, c = cc.read()
        if ok1:
            src_n += 1
        if ok2:
            clean_n += 1
        if not ok1 and not ok2:
            break
        if not (ok1 and ok2):
            # 帧数校对：短流结束后继续顺序读长流，不能只看配对帧数。
            while ok1:
                ok1, _ = cs.read()
                if ok1:
                    src_n += 1
            while ok2:
                ok2, _ = cc.read()
                if ok2:
                    clean_n += 1
            break

        diffs.append(float(cv2.absdiff(_gray(s), _gray(c)).mean()))
        H, W = s.shape[:2]
        for bi, box in enumerate(watermarks):
            x0, y0, x1, y1 = _clamp_box(box, H, W)
            if x1 <= x0 or y1 <= y0:
                continue
            gs = _gray(s[y0:y1, x0:x1])
            gc = _gray(c[y0:y1, x0:x1])
            if _bright_watermark_residual_hit(gs, gc):
                bright_hits.append((fi, bi))
            if float(gs.std()) >= 32 or float((gs > 140).mean()) > 0.08:
                continue
            med = float(np.median(gc))
            if med >= 50:
                continue
            src_ink = gs < (float(np.median(gs)) - 12)
            smear = (gc > 120) & (gc > med + 35) & src_ink
            ys, xs = np.where(smear)
            if len(ys) < 3:
                continue
            if (np.median(xs) < 5 or np.median(xs) > gc.shape[1] - 5
                    or np.median(ys) < 6 or np.median(ys) > gc.shape[0] - 6):
                continue
            n, _lab, st, _cen = cv2.connectedComponentsWithStats(
                smear.astype(np.uint8) * 255, 8
            )
            max_blob = max([int(st[k, 4]) for k in range(1, n)] + [0])
            if max_blob > gc.size * 0.03:
                continue
            wm_hits.append((fi, bi))

        if dark_enabled and dialogue:
            x0, y0, x1, y1 = _clamp_box(dialogue, H, W)
            if x1 > x0 and y1 > y0:
                mask = _dark_blob_mask(_gray(s[y0:y1, x0:x1]), _gray(c[y0:y1, x0:x1]))
                if mask is not None and int((mask > 0).sum()) >= max(40, int(mask.size * 0.004)):
                    dark_hits.append(fi)
        fi += 1
    cs.release(); cc.release()
    return {
        "diffs": diffs,
        "watermark_hits": wm_hits,
        "bright_hits": bright_hits,
        "dark_hits": dark_hits,
        "source_frames": src_n,
        "clean_frames": clean_n,
    }


def _cut_frames_from_diffs(diffs: list[float]) -> list[int]:
    """切镜重复帧判据：灰差 > 基线*3 且 >15，连续 spike 成组替换。"""
    if len(diffs) < 8:
        return []
    base = float(np.median(diffs))
    thr = max(_DIFF_SPIKE_MIN, base * _DIFF_SPIKE_FACTOR)
    spikes = [i for i, d in enumerate(diffs) if d > thr]
    groups: list[list[int]] = []
    for i in spikes:
        if groups and i - groups[-1][-1] <= 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    return [i for g in groups for i in g]


def detect_cut_repeat(source: str | Path, clean: str | Path) -> list[int]:
    """检测 clean 相对 source 的切镜重复帧（返回 clean 中需替换的 0-based 帧号）。"""
    return _cut_frames_from_diffs(_scan_detections(source, clean, [], None, False)["diffs"])


def _count_frames_cv2(path: str | Path) -> int:
    """顺序解码统计帧数（帧数校对兜底，不用 CAP_PROP_FRAME_COUNT 元数据）。"""
    cap = cv2.VideoCapture(str(path))
    n = 0
    while True:
        ok, _frame = cap.read()
        if not ok:
            break
        n += 1
    cap.release()
    return n


def _limited_frames(cap: cv2.VideoCapture, target: int):
    """产出正好 target 帧；短流用末帧补齐（不依赖 ffmpeg tpad），长流截断。"""
    last: np.ndarray | None = None
    produced = 0
    while produced < target:
        ok, frame = cap.read()
        if ok:
            last = frame
        if last is None:
            raise RuntimeError("video has no decodable frames")
        yield last
        produced += 1


def detect_watermark_smear(source: str | Path, clean: str | Path,
                           watermarks: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """检测水印框内"深字被擦成白糊斑"，返回 (frame, box_index) 列表。

    判据（精确，避免全片误报）：源框平背景暗字 + clean 框内绝对亮小块 +
    落在源暗字位置。阈值 120 为 8-bit 通用经验值（白糊斑亮度），非视频特定。
    """
    if not watermarks:
        return []
    return _scan_detections(source, clean, watermarks, None, False)["watermark_hits"]


def detect_bright_watermark_residual(source: str | Path, clean: str | Path,
                                     watermarks: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """检测暗底亮字水印被擦后留下的低对比糊斑（返回 frame, box_index）。"""
    if not watermarks:
        return []
    return _scan_detections(source, clean, watermarks, None, False)["bright_hits"]


def _dark_blob_mask(gs: np.ndarray, gc: np.ndarray) -> np.ndarray | None:
    """dialogue 框内"亮字被擦成黑字"的残留块，返回二值 mask 或 None。

    只抓明确瑕疵：源帧亮字（>框内 90 分位且绝对>120）在 clean 同位置被压成
    显著暗块（暗超过 60 级）。动画下巴的深色线条/胡茬在 clean 里也偏暗，但
    源帧那里是字幕（同样被算作 src_ink），无法用弱阈值区分——故必须用强阈值
    （60 级）只抓"白字变黑字"这种一眼可见的残留，绝不碰原画。
    """
    if gs.size < 200:
        return None
    src_ink = (gs > float(np.percentile(gs, 90))) & (gs > 120)
    if int(src_ink.sum()) < 24:
        return None
    d = gc.astype(np.float32) - gs.astype(np.float32)
    dark = ((d < -60) & src_ink).astype(np.uint8) * 255
    if int(dark.max()) == 0:
        return None
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lab, st, _cen = cv2.connectedComponentsWithStats(dark, 8)
    keep = np.zeros_like(dark)
    area_thr = max(24, int(dark.size * 0.003))
    for k in range(1, n):
        if int(st[k, 4]) >= area_thr:
            keep[lab == k] = 255
    return keep if int(keep.max()) > 0 else None


def detect_dark_patch(source: str | Path, clean: str | Path,
                      dialogue: dict[str, Any] | None) -> list[int]:
    """检测 dialogue 框内压黑块帧。"""
    if not dialogue:
        return []
    return _scan_detections(source, clean, [], dialogue, True)["dark_hits"]


# ---------------------------------------------------------------- 修复

def _render_source_frame(frame: np.ndarray, box: dict[str, Any] | None) -> np.ndarray:
    """用源帧重渲染：dialogue 框内 OCR 细字形洞 + TELEA（切点帧通常无字，mask=0）。"""
    if not box:
        return frame
    try:
        from glyph_detect import detect_text_boxes
        from dehardsub_ocr_telea import thin_mask
        boxes = detect_text_boxes(frame) or []
    except Exception:
        return frame
    H, W = frame.shape[:2]
    x0, y0, x1, y1 = _clamp_box(box, H, W)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mask = np.zeros((H, W), np.uint8)
    for b in boxes:
        if (b.get("x", 0) + b.get("w", 0) < x0 or b.get("x", 0) > x1
                or b.get("y", 0) + b.get("h", 0) < y0 or b.get("y", 0) > y1):
            continue
        r = thin_mask(gray, b)
        if r:
            s, (mx, my) = r
            s8 = s if isinstance(s, np.ndarray) else np.asarray(s)
            if int(s8.max()) > 0:
                sy, sx = min(s8.shape[0], H - my), min(s8.shape[1], W - mx)
                if sy > 0 and sx > 0:
                    mask[my:my+sy, mx:mx+sx] = np.maximum(
                        mask[my:my+sy, mx:mx+sx], s8[:sy, :sx])
    if int(mask.max()) == 0:
        return frame
    return cv2.inpaint(frame, mask, 3, cv2.INPAINT_TELEA)


def _smear_mask_in_box(src_roi: np.ndarray, clean_roi: np.ndarray) -> np.ndarray | None:
    """水印框内糊斑的精确 mask（与 detect_watermark_smear 判据一致）。"""
    gs, gc = _gray(src_roi), _gray(clean_roi)
    if float(gs.std()) >= 32 or float((gs > 140).mean()) > 0.08:
        return None
    med = float(np.median(gc))
    if med >= 50:
        return None
    # 检测用 src_ink 确认是"字被擦成糊斑"，但修复 mask 用 clean 自身的亮像素
    # （糊斑与源字位置可能有几像素偏移，交集只盖到极小部分，擦不干净）
    src_ink = gs < (float(np.median(gs)) - 12)
    if int((src_ink & (gc > 120)).sum()) < 2:
        return None
    smear = ((gc > 120) & (gc > med + 35)).astype(np.uint8) * 255
    n, _lab, st, _cen = cv2.connectedComponentsWithStats(smear, 8)
    keep = np.zeros_like(smear)
    for k in range(1, n):
        if int(st[k, 4]) <= gc.size * 0.03:
            keep[_lab == k] = 255
    return keep if int(keep.max()) > 0 else None


def _ns_source_box(frame: np.ndarray, box: dict[str, Any],
                   smear_mask: np.ndarray | None = None) -> np.ndarray:
    """NS 整框填充 + 柔和羽化（f145 已验证的右@修复法）。

    必须整框：糊斑周围像素本身也是亮的，小 mask 的 NS 会把亮色插值回来擦不干净。
    整框 NS 从框外取干净暗背景样本，才能彻底去除。羽化消边界痕迹。
    """
    H, W = frame.shape[:2]
    x0, y0, x1, y1 = _clamp_box(box, H, W)
    if x1 <= x0 or y1 <= y0:
        return frame
    mask = np.zeros((H, W), np.uint8)
    mask[y0:y1, x0:x1] = 255
    ns = cv2.inpaint(frame, mask, 5, cv2.INPAINT_NS)
    # 大半径羽化：框中心全用 NS，边缘渐变回原帧，消除矩形边界
    k = max(9, (min(y1-y0, x1-x0) // 3) | 1)
    soft = cv2.GaussianBlur(mask, (k, k), 0).astype(np.float32) / 255.0
    return np.clip(frame.astype(np.float32) * (1 - soft[..., None])
                   + ns.astype(np.float32) * soft[..., None], 0, 255).astype(np.uint8)


def _ring_median_fill_roi(frame: np.ndarray, box: dict[str, Any],
                          mask_local: np.ndarray) -> np.ndarray:
    """dialogue 框内压黑块用同帧环带中值回填。"""
    H, W = frame.shape[:2]
    x0, y0, x1, y1 = _clamp_box(box, H, W)
    if x1 <= x0 or y1 <= y0:
        return frame
    roi = frame[y0:y1, x0:x1]
    m = mask_local > 0
    if not np.any(m):
        return frame
    dil = cv2.dilate(mask_local, np.ones((9, 9), np.uint8), iterations=1) > 0
    ring = dil & ~m
    if not np.any(ring):
        return frame
    med = np.median(roi[ring], axis=0).astype(np.uint8)
    out = roi.copy()
    out[m] = med
    # 轻微羽化边缘
    soft = cv2.GaussianBlur((dil.astype(np.float32) * 255), (5, 5), 0) / 255.0
    soft = np.maximum(soft, m.astype(np.float32))
    return np.clip(out.astype(np.float32) * soft[..., None]
                   + roi.astype(np.float32) * (1 - soft[..., None]), 0, 255).astype(np.uint8)


_CUT_DONOR_MAX_DIFF = 12.0


def _outside_boxes_diff(a: np.ndarray, b: np.ndarray, boxes: list[dict[str, Any]]) -> float:
    """比较两帧在指定框之外的灰差，用来判断供体帧是否来自同一镜头。"""
    ga, gb = _gray(a), _gray(b)
    if ga.shape != gb.shape:
        return float("inf")
    H, W = ga.shape
    mask = np.ones((H, W), np.uint8)
    for box in boxes:
        x0, y0, x1, y1 = _clamp_box(box, H, W)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = 0
    diff = cv2.absdiff(ga, gb)
    return float(diff[mask > 0].mean()) if int(mask.sum()) else float(diff.mean())


def _copy_boxes_with_feather(dst: np.ndarray, donor: np.ndarray,
                             boxes: list[dict[str, Any]]) -> np.ndarray:
    """从同镜头相邻供体帧复制水印框，内实外羽化避免接缝。"""
    out = dst.copy()
    H, W = out.shape[:2]
    for box in boxes:
        x0, y0, x1, y1 = _clamp_box(box, H, W)
        if x1 <= x0 or y1 <= y0:
            continue
        m = np.zeros((y1 - y0, x1 - x0), np.float32)
        fx = min(1, max(0, (x1 - x0) // 8))
        fy = min(1, max(0, (y1 - y0) // 8))
        if fx and fy:
            m[fy:-fy, fx:-fx] = 1.0
            m = cv2.GaussianBlur(m, (0, 0), 1.2)
        else:
            m[:] = 1.0
        a = m[..., None]
        d = dst[y0:y1, x0:x1].astype(np.float32)
        s = donor[y0:y1, x0:x1].astype(np.float32)
        out[y0:y1, x0:x1] = np.clip(d * (1.0 - a) + s * a, 0, 255).astype(np.uint8)
    return out


def _repair_cut_watermarks_from_donors(
    video_in: str | Path,
    video_out: str | Path,
    cut_frames: list[int],
    watermarks: list[dict[str, Any]],
) -> dict[str, Any]:
    """切镜重渲染帧的水印从同镜头相邻帧借像素，避免整框 NS 平色块。

    仅在相邻帧"框外灰差"小于阈值时复制（说明是同一镜头）；跨镜头则退化为
    整框 NS。顺序解码 + 3 帧滑窗，不依赖 cv2 seek。
    """
    cut_set = {int(i) for i in cut_frames}
    cap = cv2.VideoCapture(str(video_in))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0) or 30.0
    vw = cv2.VideoWriter(str(video_out), cv2.VideoWriter_fourcc(*"MJPG"), fps,
                         (int(cap.get(3)), int(cap.get(4))))
    buf: deque[tuple[int, np.ndarray]] = deque(maxlen=3)
    donor_fixed = 0
    ns_fixed = 0
    next_to_write = 0

    def _fix(cur_i: int, cur: np.ndarray,
             prev: tuple[int | None, np.ndarray | None],
             nxt: tuple[int | None, np.ndarray | None]) -> np.ndarray:
        nonlocal donor_fixed, ns_fixed
        if cur_i not in cut_set or not watermarks:
            return cur
        best: tuple[float, np.ndarray] | None = None
        for idx, frame in (prev, nxt):
            if idx is None or idx in cut_set or frame is None:
                continue
            d = _outside_boxes_diff(frame, cur, watermarks)
            if best is None or d < best[0]:
                best = (d, frame)
        if best is not None and best[0] <= _CUT_DONOR_MAX_DIFF:
            donor_fixed += 1
            return _copy_boxes_with_feather(cur, best[1], watermarks)
        ns_fixed += 1
        out = cur.copy()
        for box in watermarks:
            out = _ns_source_box(out, box)
        return out

    fi = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        buf.append((fi, frame))
        if len(buf) == 2:
            ci, cf = buf[0]
            ni, nf = buf[1]
            vw.write(_fix(ci, cf, (None, None), (ni, nf)))
            next_to_write = ci + 1
        elif len(buf) == 3:
            (pi, pf), (ci, cf), (ni, nf) = buf[0], buf[1], buf[2]
            vw.write(_fix(ci, cf, (pi, pf), (ni, nf)))
            next_to_write = ci + 1
        fi += 1
    for j, (ci, cf) in enumerate(list(buf)):
        if ci < next_to_write:
            continue
        prev = (list(buf)[j-1][0], list(buf)[j-1][1]) if j > 0 else (None, None)
        nxt = (list(buf)[j+1][0], list(buf)[j+1][1]) if j + 1 < len(buf) else (None, None)
        vw.write(_fix(ci, cf, prev, nxt))
    vw.release(); cap.release()
    return {"donor_fixed": donor_fixed, "ns_fixed": ns_fixed, "frames": fi}


def run_postfix(source: str | Path, clean: str | Path, out: str | Path,
                regions_path: str | Path, *, stats_out: str | Path | None = None) -> dict[str, Any]:
    """主入口：单遍检测三类瑕疵 → 帧数校对 → 修复 → 写出 out（保留音轨）。

    帧数以 source 为准：clean 短则复制末帧补齐，clean 长则截断；不再静默
    按两流较短者输出。返回统计 dict 含 source/clean/output 帧数与对齐动作。
    """
    source, clean, out = Path(source), Path(clean), Path(out)
    stats: dict[str, Any] = {
        "cut_repeat_frames": [], "watermark_smear": [],
        "bright_watermark_residual": [], "dark_patch_frames": [],
        "engine": "postfix", "source": str(source), "clean_in": str(clean), "out": str(out),
    }
    dialogue, watermarks = _load_boxes(regions_path)
    stats["dialogue_box"] = dialogue
    dark_enabled = os.environ.get(DARK_PATCH_ENV, "0").strip().lower() in ("1", "true", "yes", "on")

    # 1) 单遍检测 + 精确帧数（三类检测共用一次解码）
    try:
        scan = _scan_detections(source, clean, watermarks, dialogue, dark_enabled)
        cut_frames = _cut_frames_from_diffs(scan["diffs"])
        wm_hits = scan["watermark_hits"]
        bright_hits = scan["bright_hits"]
        dark_frames = scan["dark_hits"] if dark_enabled else []
        src_n, clean_n = scan["source_frames"], scan["clean_frames"]
    except Exception as exc:
        stats["scan_error"] = str(exc)
        cut_frames, wm_hits, bright_hits, dark_frames = [], [], [], []
        src_n = _count_frames_cv2(source)
        clean_n = _count_frames_cv2(clean)

    target = src_n or clean_n
    if not src_n or not clean_n:
        alignment = "undetermined"
    elif src_n == clean_n:
        alignment = "ok"
    elif src_n > clean_n:
        alignment = f"clean_short_{clean_n}/{src_n}_last_frame_duplicated"
    else:
        alignment = f"clean_long_{clean_n}/{src_n}_truncated"
    stats.update({
        "source_frames": src_n, "clean_frames": clean_n,
        "output_frames": target, "frame_alignment": alignment,
    })
    stats["cut_repeat_frames"] = cut_frames
    stats["watermark_smear"] = [[f, bi] for f, bi in wm_hits]
    stats["bright_watermark_residual"] = [[f, bi] for f, bi in bright_hits]
    stats["dark_patch_frames"] = dark_frames
    print(f"[postfix] detected: cut={cut_frames} wm={wm_hits[:20]}({len(wm_hits)}) "
          f"bright={bright_hits[:20]}({len(bright_hits)}) "
          f"dark={dark_frames[:20]}({len(dark_frames)}) "
          f"frames={src_n}/{clean_n} align={alignment}", flush=True)

    cut_set = set(cut_frames)
    wm_by_frame: dict[int, list[int]] = {}
    for f, bi in wm_hits:
        wm_by_frame.setdefault(f, []).append(bi)
    bright_by_frame: dict[int, list[int]] = {}
    for f, bi in bright_hits:
        bright_by_frame.setdefault(f, []).append(bi)
    dark_set = set(dark_frames)

    if not (cut_set or wm_by_frame or bright_by_frame or dark_set) and alignment == "ok":
        # 无瑕疵且帧数一致：直接复制（保留音轨）
        import shutil
        shutil.copy2(clean, out)
        if stats_out:
            Path(stats_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        return stats

    # 2) 按 source 帧数逐帧修复写出；短 clean 复制末帧、长 clean 截断
    cs, cc = cv2.VideoCapture(str(source)), cv2.VideoCapture(str(clean))
    fps = cc.get(cv2.CAP_PROP_FPS) or 30.0
    ff = _ffmpeg()
    tmp_raw = Path(str(out) + ".raw.avi")
    vw = cv2.VideoWriter(str(tmp_raw), cv2.VideoWriter_fourcc(*"MJPG"), fps,
                         (int(cc.get(3)), int(cc.get(4))))
    H = W = None
    fi = 0
    cut_done = 0; wm_done = 0; bright_done = 0; dark_done = 0
    for fi, (s, c) in enumerate(zip(_limited_frames(cs, target), _limited_frames(cc, target))):
        if H is None:
            H, W = c.shape[:2]
        cur = c
        # 切镜重复：整帧用 source 重渲染
        if fi in cut_set:
            cur = _render_source_frame(s, dialogue)
            cut_done += 1
        # 水印糊斑：仅填糊斑像素（精确 mask），避免整框 NS 的矩形痕迹
        dark_wm_boxes = list(wm_by_frame.get(fi, []))
        bright_wm_boxes = [bi for bi in bright_by_frame.get(fi, []) if bi not in dark_wm_boxes]
        if dark_wm_boxes or bright_wm_boxes:
            Hc, Wc = cur.shape[:2]
            for bi in dark_wm_boxes:
                if bi < len(watermarks):
                    x0, y0, x1, y1 = _clamp_box(watermarks[bi], Hc, Wc)
                    if x1 <= x0 or y1 <= y0:
                        continue
                    smear = _smear_mask_in_box(s[y0:y1, x0:x1], c[y0:y1, x0:x1])
                    cur = _ns_source_box(cur, watermarks[bi], smear)
                    wm_done += 1
            # 亮字水印残影：平暗底整框 NS，避免字形糊影残留
            for bi in bright_wm_boxes:
                if bi < len(watermarks):
                    x0, y0, x1, y1 = _clamp_box(watermarks[bi], Hc, Wc)
                    if x1 <= x0 or y1 <= y0:
                        continue
                    cur = _ns_source_box(cur, watermarks[bi])
                    bright_done += 1
        # 压黑块：环带中值回填
        if fi in dark_set and dialogue:
            x0, y0, x1, y1 = _clamp_box(dialogue, H, W)
            mask = _dark_blob_mask(_gray(s[y0:y1, x0:x1]), _gray(c[y0:y1, x0:x1]))
            if mask is not None:
                filled = _ring_median_fill_roi(cur, dialogue, mask)
                cur = cur.copy()
                cur[y0:y1, x0:x1] = filled
                dark_done += 1
        vw.write(cur)
    written = fi + 1 if target else 0
    vw.release(); cs.release(); cc.release()

    # 2b) 切镜重渲染帧的水印从同镜头相邻帧借像素，避免整框 NS 平色块
    donor_fixed = 0
    ns_fixed = 0
    video_for_mux = tmp_raw
    donor_raw = Path(str(tmp_raw) + ".donor.avi")
    if cut_set and watermarks:
        dstats = _repair_cut_watermarks_from_donors(tmp_raw, donor_raw, cut_frames, watermarks)
        donor_fixed = dstats["donor_fixed"]
        ns_fixed = dstats["ns_fixed"]
        video_for_mux = donor_raw
        print(f"[postfix] cut-watermark donor fix: donor={donor_fixed} ns_fallback={ns_fixed}", flush=True)

    # 3) remux 源音轨 → out
    cmd = [ff, "-y", "-i", str(video_for_mux), "-i", str(source),
           "-map", "0:v:0", "-map", "1:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp_raw.unlink(missing_ok=True)
    donor_raw.unlink(missing_ok=True)

    stats.update({"cut_fixed": cut_done, "wm_fixed": wm_done,
                  "bright_fixed": bright_done, "dark_fixed": dark_done,
                  "cut_watermark_donor_fixed": donor_fixed,
                  "cut_watermark_ns_fallback": ns_fixed,
                  "frames": written, "output_frames": written})
    print(f"[postfix] done: cut={cut_done} wm={wm_done} bright={bright_done} "
          f"dark={dark_done} frames={written} → {out}", flush=True)
    if stats_out:
        Path(stats_out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="通用去字幕后处理：切镜重复/水印糊斑/压黑块")
    ap.add_argument("--source", required=True)
    ap.add_argument("--clean", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--regions", required=True, help="dehardsub_regions.json 路径")
    ap.add_argument("--stats", default=None)
    a = ap.parse_args()
    run_postfix(a.source, a.clean, a.out, a.regions, stats_out=a.stats)


if __name__ == "__main__":
    main()
