"""OCR 细字形洞 + TELEA 逐帧去硬字幕（B站硬字幕首选路线）。

路线（用户 2026-10-22 明令"以后首选"）：
    逐帧 RapidOCR 定位字幕 → 在框内取细字形洞（贴合笔画，非整框）
    → cv2.TELEA 局部填充。洞细、四周全是真实画面纹理，填充从周边延续，
    不会出现 STTN tiles 在持久/白字字幕上的大块平灰补丁。

适用：白字/描边字压在实拍背景、人物皮肤/衣服上（对话带、UP主水印、浮动旁白）。
不适用：纯平色 UI 条带（走 flat-fill）、需强时序一致的大块持久遮挡（仍考虑 STTN temporal）。

三类目标的去除策略：
    1. 底部对白带：固定位置 → OCR 命中 + 亮度白字兜底（不依赖 OCR，兜住漏检）。
    2. 右下角水印（静态 logo）：预扫前 N 帧取字形并集做成固定遮罩，每帧强制套用，
       解决 OCR 逐帧漏检导致的长段水印残留。
    3. 浮动 `* ` 旁白：逐帧 OCR，按文本 `*` 前缀或特有词过滤，绝不靠纯位置框
       （同区域不同镜头含义不同，位置过滤会误删菜单等场景文字）。

为避免误删：除固定位置目标外，仅当文本以 `*` 开头或命中 `caption_stems` 才去除
（菜单、墙上价格等场景文字因此完好）。

对齐 `DEHARDSUB.md`「完美还原六要点」：
    - 第2条 边缘羽化 3–5px：文字本体权重恒为 1（保证去除彻底、不留鬼影），
      仅在外扩环带内按高斯权重与原始背景融合 → 消硬接缝，同时不削弱去除效果。
    - 第3条 时序平滑消闪烁：逐帧 OCR 收集目标框 → 抑制孤立单帧检测（邻帧无支持则丢弃），
      避免遮罩跳变导致闪烁。刻意不向邻帧扩散，防止在无字幕帧里白白抹掉背景纹理。
    - 第1条 无损导出：同分辨率 + crf18 高码率重编码（文档认可的视觉无损区间）；
      因水印每帧都在、无干净段，故不适用流拷贝路径。

入口：
    run(source, out, **opts)              处理并写出 out
    qc_dehardsub(source, out, **opts)     残差(漏删)+误删 验收，返回 {missed, damaged}
    make_compare(source, out, compare)    生成左右对比视频
命令行：
    python dehardsub_ocr_telea.py --source media/source.mp4 --out media/dehardsub_full.mp4 [--qc] [--compare media/compare_full.mp4]
    （不传 --source/--out 时，默认在 media/ 目录下按 source.mp4→dehardsub_full.mp4 工作）
"""
from __future__ import annotations

import os
import subprocess
import sys
import json
from pathlib import Path

import cv2
import numpy as np

from glyph_detect import _get_ocr

# ---- 默认目标区域（按 BV1oFJ16BEMT 验证值；其它视频用 --band/--watermark 或自动探测覆盖）----
DEFAULT_BAND = {"x": 131, "y": 597, "w": 853, "h": 122}  # BV1oFJ16BEMT 全片实测对白框并集+外扩(2026-11-02)
DEFAULT_WATERMARK = {"x": 985, "y": 638, "w": 290, "h": 72}
DEFAULT_CAPTION_STEMS = ("无微不至", "感慨")
DEFAULT_SCORE_THR = 0.25
DEFAULT_INPAINT_RADIUS = 3
DEFAULT_DILATE = 2          # thin_mask 膨胀迭代次数（盖描边即可，重膨胀会变大洞）
DEFAULT_PRE_SCAN = 200      # 水印固定遮罩预扫帧数
DEFAULT_MIN_AREA = 90


def _ffmpeg() -> str:
    """复用管线自带的 imageio_ffmpeg 二进制，缺失则回退 PATH 上的 ffmpeg。"""
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    return "ffmpeg"


# ----------------------------- 字形洞工具 -----------------------------
def _in_box(b, box):
    bx1, by1 = b["x"] + b["w"], b["y"] + b["h"]
    x0, y0, x1, y1 = box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]
    return not (bx1 < x0 or b["x"] > x1 or by1 < y0 or b["y"] > y1)


def thin_mask(gray, b, pad=6):
    """自适应细字形洞：取框内偏离本地的极端像素。

    暗景（字幕压在衣服/长椅上）取**亮笔画 ∪ 邻接暗描边**（白芯+黑边同洞），
    再小膨胀。只取暗端会把腿/衣物抹黑（BV1P6 v4/v5）；只取亮芯则 TELEA 把
    描边当边界色吸入洞内变黑字。亮底白字仍用双向极端。
    """
    H, W = gray.shape
    x0 = max(0, b["x"] - pad); y0 = max(0, b["y"] - pad)
    x1 = min(W, b["x"] + b["w"] + pad); y1 = min(H, b["y"] + b["h"] + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    roi = gray[y0:y1, x0:x1].astype(np.float32)
    lo = np.percentile(roi, 12); hi = np.percentile(roi, 88)
    med = float(np.median(roi))
    if med < 110.0:
        thr = max(float(hi), 165.0)
        core = (roi >= thr).astype(np.uint8)
        # Expand bright cores to cover black outlines. Marking dark outline
        # pixels directly made TELEA paint glyph-shaped dark blotches on
        # shorts (demo t=7). Core-grow stays anchored to white ink.
        s = (core > 0).astype(np.uint8) * 255
        s = cv2.dilate(s, np.ones((3, 3), np.uint8), iterations=2)
    else:
        s = ((roi < lo) | (roi > hi)).astype(np.uint8) * 255
        s = cv2.morphologyEx(s, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
        s = cv2.dilate(s, np.ones((3, 3), np.uint8), iterations=DEFAULT_DILATE)
    if int(s.max()) == 0:
        return None
    return s, (x0, y0)


def gold_threshold_mask(frame_bgr, band, *, gray_thr=90, sat_thr=95) -> np.ndarray:
    """黄金参照（src_0001_final2.jpg）的字幕 mask 逻辑，1:1 对齐。

    来自 fix_frame1_final2.py：``gray>90 & sat<95`` + 椭圆 11x11 膨胀 + CLOSE 5x5。
    与 OCR thin_mask（局部差分，笔画连成实心块）不同，固定阈值 mask 会在字间距
    保留缝隙，让 LaMa 能借周围纹理，擦除后不留大块平色补丁——这正是用户认可的
    "完美"首帧的做法。用 band 动态定位字幕带，不写死坐标。

    返回与 frame 同尺寸的 uint8 mask（0/255）。
    """
    import os as _os
    if _os.environ.get("VITUAL_DEHARDSUB_GOLD_MASK", "").strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        return np.zeros(frame_bgr.shape[:2], np.uint8)
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    sat = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)[..., 1]
    H, W = gray.shape[:2]
    if isinstance(band, dict):
        x, y, w, h = band["x"], band["y"], band["w"], band["h"]
    else:
        x, y, w, h = band
    # 精确对齐黄金脚本字幕带：中心沿用 confirm box，尺寸用黄金的 370x83。
    # confirm box (216,399,428,62) 中心=(430,430)；黄金带 (255,375,370,83)
    # 中心=(440,416.5)。按中心对齐可把 x 收到 255-625、y 收到 375-458，
    # goldpx≈19439（与黄金脚本一致），避免 x 过宽抓到两侧非字幕亮区。
    cx, cy = x + w / 2, y + h / 2
    gw, gh = 370, 83
    gx0 = int(round(cx - gw / 2)); gx1 = min(W, gx0 + gw)
    gy0 = int(round(cy - gh / 2 - 13)); gy1 = min(H, gy0 + gh)  # y中心上移13对齐416.5
    gx0 = max(0, gx0); gy0 = max(0, gy0)
    sel = ((gray[gy0:gy1, gx0:gx1] > gray_thr) & (sat[gy0:gy1, gx0:gx1] < sat_thr))
    m = np.zeros(gray.shape, np.uint8)
    if sel.any():
        m[gy0:gy1, gx0:gx1] = cv2.dilate(
            sel.astype(np.uint8) * 255,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)),
        )
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return m


def _reject_dark_inpaint(frame_bgr, filled_bgr, mask_u8, *, margin: float = 12.0) -> bool:
    """True when TELEA/LaMa filled the hole much darker than the surrounding ring.

    Ring donors exclude near-black outline and near-white glyph leftovers so a
    correct dark-cloth fill is not rejected against bright caption remnants.
    """
    m = mask_u8 > 0
    if not np.any(m):
        return False
    dil = cv2.dilate(mask_u8, np.ones((7, 7), np.uint8), iterations=1)
    gray_f = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    ring = (dil > 0) & ~m & (gray_f > 55) & (gray_f < 200)
    if not np.any(ring):
        ring = (dil > 0) & ~m
    if not np.any(ring):
        return False
    gf = cv2.cvtColor(filled_bgr, cv2.COLOR_BGR2GRAY)
    hole = float(gf[m].mean())
    ring_m = float(np.median(gf[ring]))
    return hole < ring_m - float(margin)


def _surgical_dark_repair(frame_bgr, filled_bgr, mask_u8, *, delta: float = 28.0,
                          abs_floor: float = 38.0):
    """Replace only pathologically dark hole pixels; keep LaMa texture elsewhere.

    Full ``_ring_median_fill`` under GOLD_MASK flattened 800+ BV1P6 frames into
    flat smudge bars (gate PASS, look FAIL). Only pixels that are much darker
    than the ring median (or near-black) get the ring color; the rest of the
    gold/LaMa fill is preserved.
    """
    m = mask_u8 > 0
    if not np.any(m):
        return filled_bgr
    ring_fill = _ring_median_fill(frame_bgr, mask_u8)
    gf = cv2.cvtColor(filled_bgr, cv2.COLOR_BGR2GRAY)
    gr = cv2.cvtColor(ring_fill, cv2.COLOR_BGR2GRAY)
    # Prefer ring sample under the same pixel; fall back to global ring median.
    ring_ref = gr.copy()
    bad = m & ((gf.astype(np.float32) < ring_ref.astype(np.float32) - float(delta)) | (gf < abs_floor))
    if not np.any(bad):
        return filled_bgr
    out = filled_bgr.copy()
    out[bad] = ring_fill[bad]
    return out


def _ring_median_fill(frame_bgr, mask_u8):
    """Fill mask with BGR median of the surrounding ring (anti black-TELEA).

    BV1P6: TELEA often painted near-black into bilingual dialogue holes; using
    the ring median keeps cloth/skin tone instead of a black smear or leaving
    source captions. Ring donors exclude near-black outline and bright glyph
    pixels so the median is not pulled by stroke/halo.
    """
    m = mask_u8 > 0
    if not np.any(m):
        return frame_bgr
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    dil = cv2.dilate(mask_u8, np.ones((11, 11), np.uint8), iterations=2)
    # Prefer mid-tone cloth/skin; skip outline (≤55) and caption ink (≥200).
    ring = (dil > 0) & ~m & (gray > 55) & (gray < 200)
    out = frame_bgr.copy()
    if not np.any(ring):
        # Widen search: mid-tone pixels in a padded bbox around the mask.
        ys, xs = np.where(m)
        y0, y1 = max(0, int(ys.min()) - 24), min(out.shape[0], int(ys.max()) + 25)
        x0, x1 = max(0, int(xs.min()) - 24), min(out.shape[1], int(xs.max()) + 25)
        pad = np.zeros_like(m)
        pad[y0:y1, x0:x1] = True
        ring = pad & ~m & (gray > 55) & (gray < 200)
    if not np.any(ring):
        # Last resort: any non-mask pixel in pad (still better than source glyphs).
        ys, xs = np.where(m)
        y0, y1 = max(0, int(ys.min()) - 24), min(out.shape[0], int(ys.max()) + 25)
        x0, x1 = max(0, int(xs.min()) - 24), min(out.shape[1], int(xs.max()) + 25)
        pad = np.zeros_like(m)
        pad[y0:y1, x0:x1] = True
        ring = pad & ~m & (gray > 40)
    if not np.any(ring):
        return out
    for c in range(out.shape[2] if out.ndim == 3 else 1):
        ch = out[:, :, c]
        # Slightly above median biases away from residual dark outline donors.
        med = int(np.percentile(ch[ring], 55))
        ch[m] = med
        out[:, :, c] = ch
    # Light blur only on mask edge to hide hard paint.
    blur = cv2.GaussianBlur(out, (0, 0), 1.2)
    edge = cv2.dilate(mask_u8, np.ones((3, 3), np.uint8), iterations=1)
    edge = (edge > 0) & ~m
    if np.any(edge):
        out[edge] = blur[edge]
    return out


def band_white_mask(gray, band):
    """底部对白带内白字字形（不依赖 OCR，兜住漏检）。仅限字幕行中心区 + 文本尺寸，保守补充。

    关键: 先 OPEN(3x3,2) 断开白字与白米饭/白盘间的细薄桥接, 再对连通域加面积/宽度上限,
    只保留文本尺寸的白色块——避免把整片白色食物连成大块一起遮掉(否则 TELEA 填出大色块)。
    """
    x0, y0, w, h = band["x"], band["y"], band["w"], band["h"]
    crop = gray[y0:y0 + h, x0:x0 + w]
    white = (crop > 200).astype(np.uint8)
    white = cv2.morphologyEx(white, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=2)
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
    num, labels, stats, cent = cv2.connectedComponentsWithStats(white, 8)
    m = np.zeros_like(white)
    for i in range(1, num):
        a = stats[i]; bw, bh, area = a[2], a[3], a[4]
        cy = cent[i][1] + y0
        # 只保留文本尺寸: 太小/太大(=白色食物大块)/过高/过宽 都丢
        if area < 25 or area > 4000 or bh < 12 or bh > 80 or bw < 8 or bw > 900:
            continue
        if not (band["y"] + 17 <= cy <= band["y"] + band["h"] - 17):
            continue
        m[labels == i] = 255
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=1)
    return m, (x0, y0)


# ----------------------------- 流式 IO（长片不爆内存）-----------------------------
def _iter_frames(path, max_frames=None):
    """惰性逐帧读取。绝不把整段视频读进内存——长片安全（原实现 3253 帧≈9GB）。"""
    cap = cv2.VideoCapture(str(path))
    n = 0
    try:
        while True:
            ok, f = cap.read()
            if not ok:
                break
            yield f
            n += 1
            if max_frames and n >= max_frames:
                break
    finally:
        cap.release()


def _probe(path):
    """返回 (fps, W, H, frame_count)。"""
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    return fps, W, H, n


def build_watermark_mask(frames_iter, ocr, wm_box, pre_scan=DEFAULT_PRE_SCAN, *, size):
    """预扫前 pre_scan 帧，取水印字形并集做成固定遮罩（水印是静态 logo，逐帧 OCR 会漏检）。

    ``frames_iter`` 为帧迭代器（流式）；``size``=(H, W) 必须给出。
    注意：不接受 wm_box=None 自动探测（会消耗迭代器），自动探测请在调用前用独立迭代器完成。
    """
    import itertools
    H, W = size
    mask = np.zeros((H, W), np.uint8)
    for f in itertools.islice(frames_iter, pre_scan):
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        res, _ = ocr(f, use_det=True, use_cls=False, use_rec=True)
        if not res:
            continue
        for item in res:
            pts = np.asarray(item[0], dtype=np.float32)
            x0, y0 = pts[:, 0].min(), pts[:, 1].min()
            x1, y1 = pts[:, 0].max(), pts[:, 1].max()
            b = {"x": int(x0), "y": int(y0), "w": int(x1 - x0), "h": int(y1 - y0)}
            if _in_box(b, wm_box):
                r = thin_mask(gray, b)
                if r:
                    s, (mx, my) = r
                    mask[my:my + s.shape[0], mx:mx + s.shape[1]] = np.maximum(
                        mask[my:my + s.shape[0], mx:mx + s.shape[1]], s)
    return cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)


def _auto_detect_region(frames_iter, ocr, pre_scan, prefer="corner", *, size):
    """兜底：跨预扫帧找最常出现、位置稳定的文本块（水印=角部常驻）。返回 box 或 None。"""
    import itertools
    from collections import defaultdict
    H, W = size
    buckets = defaultdict(int)
    for f in itertools.islice(frames_iter, pre_scan):
        res, _ = ocr(f, use_det=True, use_cls=False, use_rec=True)
        if not res:
            continue
        for item in res:
            pts = np.asarray(item[0], dtype=np.float32)
            x0, y0 = pts[:, 0].min(), pts[:, 1].min()
            x1, y1 = pts[:, 0].max(), pts[:, 1].max()
            cx, cy = int((x0 + x1) / 2) // 24, int((y0 + y1) / 2) // 24
            if prefer == "corner" and cx < W // 24 * 0.6 and cy < H // 24 * 0.6:
                continue
            buckets[(cx, cy)] += 1
    if not buckets:
        return None
    (cx, cy), cnt = max(buckets.items(), key=lambda kv: kv[1])
    if cnt < max(3, pre_scan * 0.4):
        return None
    return {"x": cx * 24 - 40, "y": cy * 24 - 30, "w": 120, "h": 60}


# ----------------------------- 时序平滑 + 边缘羽化 -----------------------------
def _iou(a, b):
    ax1, ay1 = a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1 = b["x"] + b["w"], b["y"] + b["h"]
    ix = min(ax1, bx1) - max(a["x"], b["x"])
    iy = min(ay1, by1) - max(a["y"], b["y"])
    if ix <= 0 or iy <= 0:
        return 0.0
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def collect_target_boxes(frame, ocr, band, caption_stems, score_thr):
    """单帧 OCR，取出该帧目标字幕框（对白带命中 / `*` 前缀 / 旁白关键词）。"""
    out = []
    res, _ = ocr(frame, use_det=True, use_cls=False, use_rec=True)
    if not res:
        return out
    for item in res:
        pts = np.asarray(item[0], dtype=np.float32)
        x0, y0 = pts[:, 0].min(), pts[:, 1].min()
        x1, y1 = pts[:, 0].max(), pts[:, 1].max()
        b = {"x": int(x0), "y": int(y0), "w": int(x1 - x0), "h": int(y1 - y0)}
        if b["w"] * b["h"] < DEFAULT_MIN_AREA or b["w"] < 12 or b["h"] < 8:
            continue
        txt = str(item[1]).strip()
        if float(item[2] if len(item) > 2 else 0) < score_thr:
            continue
        if (band is not None and _in_box(b, band)) or \
                txt.startswith("*") or any(s in txt for s in caption_stems):
            out.append(b)
    return out


def temporal_smooth(boxes_per_frame, min_support=2):
    """抑制孤立单帧检测（OCR 抖动）→ 缓解逐帧遮罩跳变导致的闪烁。

    只在邻帧(±1)存在近似框时才保留。刻意**不向邻帧扩散**：真实字幕持续多帧，
    扩散反而会在无字幕帧里白白填背景、把纹理抹掉。
    """
    n = len(boxes_per_frame)
    kept = []
    for k in range(n):
        lst = []
        for b in boxes_per_frame[k]:
            support = 1
            for j in (k - 1, k + 1):
                if 0 <= j < n and any(_iou(b, ob) >= 0.3 for ob in boxes_per_frame[j]):
                    support += 1
                    break
            if support >= min_support:
                lst.append(b)
        kept.append(lst)
    return kept


def feather_blend(frame, inpainted, mask, feather_px=5, feather_sigma=1.5):
    """边缘羽化（最佳实践：3–5px 软边）。

    文字本体权重恒为 1（保证彻底去除、不留鬼影），仅在外扩环带内按高斯权重
    与原始背景融合 → 消除填充硬接缝，同时不削弱去除效果。
    """
    k = 3 if feather_px <= 4 else 5
    ring = cv2.dilate(mask, np.ones((k, k), np.uint8), iterations=1)
    w = cv2.GaussianBlur(ring.astype(np.float32) / 255.0, (0, 0), feather_sigma)
    w = np.maximum(w, mask.astype(np.float32) / 255.0)  # 文字本体必须全替换
    w3 = w[..., None]
    return np.clip(
        inpainted.astype(np.float32) * w3 + frame.astype(np.float32) * (1 - w3),
        0, 255,
    ).astype(np.uint8)


# ----------------------------- 主处理 -----------------------------
def _collect_all(path, ocr, band, caption_stems, score_thr, max_frames=None, label="ocr"):
    """流式：逐帧 OCR 收集目标框（只存框，不存帧）。"""
    boxes = []
    for k, f in enumerate(_iter_frames(path, max_frames)):
        boxes.append(collect_target_boxes(f, ocr, band, caption_stems, score_thr))
        if (k + 1) % 200 == 0:
            print(f"  [ocr_telea] {label} {k+1}", flush=True)
    return boxes


def _sec_in_windows(t_sec: float, windows) -> bool:
    """True if t is inside any [start, end] window; empty/None → always True."""
    if not windows:
        return True
    for win in windows:
        if win is None or len(win) < 2:
            continue
        s, e = float(win[0]), float(win[1])
        if s <= t_sec <= e:
            return True
    return False


def _render_pass(src_path, vw, boxes, wm_mask, band, inpaint_radius,
                 feather_px, feather_sigma, max_frames=None, label="render",
                 use_brightness=True, inpaint_method="telea",
                 fps=30.0, active_windows=None, reject_dark_fill=True):
    """流式：逐帧建遮罩 → 修补 → 羽化融合，写入 vw。返回本遍遮罩的框数。

    ``use_brightness``：亮度兜底只在第 1 遍（原片）开。迭代遍里画面已被填充过，
    再按亮度抓会误抓带内非文字的亮像素并抹掉（实测会误删场景文字），必须关。
    ``inpaint_method``：'telea'(默认, 扩散填充, 快但繁忙纹理会晕成糊斑) /
    'lama'(神经内容识别填充, 纹理背景/食物最自然, 适合做饭·实拍视频, 需 torch+inpaint_lama)。
    ``active_windows``：仅在有字时段 inpaint；窗外帧原样拷贝（D8）。
    ``reject_dark_fill``：洞内均值相对环带过暗则拒收该帧填充（D6）。
    """
    removed = 0
    skipped_win = 0
    window_rescue = 0
    rejected_dark = 0
    fps_f = float(fps) or 30.0
    import os as _os
    # FILL_V2：LaMa 的 mask 要扩张含描边(+5~10%)，且羽化只交给内置的一次，
    # 避免紧贴笔画 mask 造成残影 / 二次 feather 把边缘字保留下来。
    fill_v2 = _os.environ.get("VITUAL_DEHARDSUB_FILL_V2", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    # 通用逐帧精修（opt-in）：只对检测到的字形化暗残留做小面积重做，默认关闭。
    try:
        from dehardsub_frame_refine import refine_enabled, refine_roi as _refine_roi
        do_refine = refine_enabled()
    except Exception:
        do_refine = False
        _refine_roi = None
    refine_count = 0
    for k, f in enumerate(_iter_frames(src_path, max_frames)):
        t_sec = k / fps_f
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        frame_boxes = boxes[k] if k < len(boxes) else []
        # D8 windows save compute — but never copy-skip a frame where OCR
        # already found caption glyphs (BV1P6 t≈8 "We're good" sat in a
        # 7.65–9.85 scan gap and was left fully readable).
        if not _sec_in_windows(t_sec, active_windows):
            if frame_boxes:
                window_rescue += 1
            else:
                vw.write(f)
                skipped_win += 1
                if (k + 1) % 200 == 0:
                    print(f"  [ocr_telea] {label} {k+1}", flush=True)
                continue
        mask = wm_mask.copy()
        # 2026-11-22 黄金参照对齐：字幕带用固定阈值 mask（gray>90&sat<95 +
        # 椭圆膨胀 + CLOSE），保留字间缝隙让 LaMa 借周围纹理，避免平色补丁。
        # 这是用户认可的 src_0001_final2.jpg 的做法，已离线 A/B 验证。
        # 完美帧参照 fix_frame1_final2.py / src_0001_final2.jpg：
        # 字幕带只用 gray>90&sat<95 + 椭圆膨胀 + CLOSE → LaMa，禁止 OCR 叠实心、
        # 禁止亮度兜底、禁止 ring_median/surgical（那些会毁掉军装纹理）。
        gold_mask = None
        if band is not None and _os.environ.get("VITUAL_DEHARDSUB_GOLD_MASK", "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            _gm = gold_threshold_mask(f, band)
            if int(_gm.max()) > 0:
                gold_mask = _gm
                # 水印 mask 保留；字幕带以黄金 mask 为准，不与 OCR 叠加。
                mask = np.maximum(wm_mask.copy(), gold_mask)
                if frame_boxes:
                    removed += len(frame_boxes)
        # 亮度兜底：底部对白带白字（黄金 mask 开启时跳过，避免叠加成实心块）
        if band is not None and use_brightness and gold_mask is None:
            bm, (bx, by) = band_white_mask(gray, band)
            if int(bm.max()) > 0:
                mask[by:by + bm.shape[0], bx:bx + bm.shape[1]] = np.maximum(
                    mask[by:by + bm.shape[0], bx:bx + bm.shape[1]], bm)
        # 非黄金路径才走 OCR thin_mask；黄金路径已在上面定稿 mask。
        if gold_mask is None:
            for b in frame_boxes:
                r = thin_mask(gray, b)
                if r:
                    s, (mx, my) = r
                    s8 = s if isinstance(s, np.ndarray) else np.asarray(s)
                    mask[my:my + s8.shape[0], mx:mx + s8.shape[1]] = np.maximum(
                        mask[my:my + s8.shape[0], mx:mx + s8.shape[1]], s8)
                    removed += 1
        if int(mask.max()) > 0:
            if inpaint_method == "lama":
                # FILL_V2: LaMa 要 mask 比字大一圈(含描边/抗锯齿)，边缘羽化交给
                # lama_inpaint 内置 GaussianBlur；跳过二次 feather（会留边缘残字）。
                # 黄金 mask 自己已膨胀+CLOSE，无需再膨胀（否则叠成实心平色块）。
                dilate_k = 1 if gold_mask is not None else (11 if fill_v2 else 3)
                if dilate_k > 1:
                    mask = cv2.dilate(mask, np.ones((dilate_k, dilate_k), np.uint8), iterations=1)
                try:
                    from inpaint_lama import lama_inpaint
                    out_f = lama_inpaint(f, mask)
                    if not fill_v2:
                        out_f = feather_blend(f, out_f, mask, feather_px, feather_sigma)
                    # 非黄金路径才做暗填回退。黄金路径与 fix_frame1_final2.py 一致：
                    # LaMa 直出，不做 ring_median / surgical（会抹平军装纹理）。
                    if (
                        gold_mask is None
                        and reject_dark_fill
                        and _reject_dark_inpaint(f, out_f, mask)
                    ):
                        out_f = _ring_median_fill(f, mask)
                        rejected_dark += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[ocr_telea] lama unavailable ({exc}); fallback telea",
                          file=sys.stderr)
                    out_f = cv2.inpaint(f, mask, inpaint_radius, cv2.INPAINT_TELEA)
                    out_f = feather_blend(f, out_f, mask, feather_px, feather_sigma)
            else:
                # Stroke TELEA, no close/smooth/feather (fog bar at t=7).
                # After wipe: pull milky TELEA pixels toward ring median so the
                # fill reads closer to v2's dark smear than a grey cloud.
                sg = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                mask = cv2.dilate(mask, np.ones((3, 3), np.uint8), iterations=1)
                radius = max(3, int(inpaint_radius))
                out_f = cv2.inpaint(f, mask, radius, cv2.INPAINT_TELEA)
                if feather_px and feather_px > 0:
                    out_f = feather_blend(f, out_f, mask, feather_px, feather_sigma)
                cg = cv2.cvtColor(out_f, cv2.COLOR_BGR2GRAY)
                near = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
                leftover = (
                    ((sg > 170) & (cg > 155) & (near > 0)).astype(np.uint8) * 255
                )
                if float((leftover > 0).mean()) >= 0.002:
                    leftover = cv2.dilate(
                        leftover, np.ones((3, 3), np.uint8), iterations=1
                    )
                    mask2 = np.maximum(mask, leftover)
                    out_f = cv2.inpaint(f, mask2, radius, cv2.INPAINT_TELEA)
                    if feather_px and feather_px > 0:
                        out_f = feather_blend(
                            f, out_f, mask2, feather_px, feather_sigma
                        )
                    rejected_dark += 1
                    mask = mask2
                    cg = cv2.cvtColor(out_f, cv2.COLOR_BGR2GRAY)
                elif reject_dark_fill and _reject_dark_inpaint(f, out_f, mask):
                    # D6: a too-dark hole must be repaired, not left black and
                    # not pasted back as source captions (the 50% no-op root
                    # cause). Ring median keeps cloth/skin tone and never
                    # restores the glyph ink.
                    out_f = _ring_median_fill(f, mask)
                    rejected_dark += 1
                # Dark-scene milk clamp: TELEA fills mid-grey over pants; pull
                # wiped pixels toward the *source* ring median (cg ring is often
                # already milky). Skip on high-texture rings (uniform fabric) —
                # dark-ring paste redraws letter-shaped fog (BV1P6 fi=69).
                # Skip on skin/face rings: dark-shadow samples paint a black
                # chin bar (BV1P6 t≈5 close-up).
                m = mask > 0
                if np.any(m):
                    dil = cv2.dilate(mask, np.ones((9, 9), np.uint8), iterations=1)
                    ring = (dil > 0) & ~m & (sg > 25) & (sg < 180)
                    skin_skip = False
                    if np.any(ring):
                        hsv_r = cv2.cvtColor(f, cv2.COLOR_BGR2HSV)
                        skin_skip = float(
                            (
                                ring
                                & (hsv_r[..., 0] < 25)
                                & (hsv_r[..., 1] > 35)
                                & (hsv_r[..., 2] > 60)
                                & (hsv_r[..., 2] < 220)
                            ).mean()
                        ) > 0.12
                    if np.any(ring) and not skin_skip:
                        ring_med = float(np.median(sg[ring]))
                        band_med = float(np.median(sg[m]))
                        ring_std = float(np.std(sg[ring]))
                        if band_med < 120.0 and ring_std < 28.0:
                            # Dark ring only — mid-bright cloth samples re-milked.
                            tgt_lum = float(np.percentile(sg[ring], 20))
                            dark_ring = ring & (sg <= tgt_lum + 12.0)
                            milky = m & (cg > tgt_lum + 3.0)
                            if np.any(milky) and np.any(dark_ring):
                                out_f = out_f.copy()
                                ring_yx = np.column_stack(np.where(dark_ring))
                                my, mx = np.where(milky)
                                pick = (my * 131 + mx * 17) % ring_yx.shape[0]
                                sampled = f[ring_yx[pick, 0], ring_yx[pick, 1]]
                                alpha = 0.90
                                out_f[milky] = (
                                    out_f[milky].astype(np.float32) * (1.0 - alpha)
                                    + sampled.astype(np.float32) * alpha
                                ).astype(np.uint8)
        else:
            out_f = f
        # 黄金路径不做 refine：完美帧是 LaMa 直出，二次精修会改掉纹理。
        if do_refine and band is not None and gold_mask is None:
            if isinstance(band, dict):
                bx, by = int(band["x"]), int(band["y"])
                bw, bh = int(band["w"]), int(band["h"])
            else:
                bx, by, bw, bh = (int(v) for v in band)
            Hf, Wf = out_f.shape[:2]
            xa, ya = max(0, bx), max(0, by)
            xb, yb = min(Wf, bx + bw), min(Hf, by + bh)
            if xb > xa and yb > ya:
                local_boxes = []
                for _b in frame_boxes:
                    _lx, _ly = int(_b.get("x", 0)) - xa, int(_b.get("y", 0)) - ya
                    _lw, _lh = int(_b.get("w", 0)), int(_b.get("h", 0))
                    if _lx + _lw > 0 and _ly + _lh > 0 and _lx < xb - xa and _ly < yb - ya:
                        local_boxes.append({"x": _lx, "y": _ly, "w": _lw, "h": _lh})
                fixed, _info = _refine_roi(
                    f[ya:yb, xa:xb], out_f[ya:yb, xa:xb],
                    {"x": xa, "y": ya, "w": xb - xa, "h": yb - ya},
                    local_boxes,
                )
                if _info.get("changed"):
                    out_f = out_f.copy()
                    out_f[ya:yb, xa:xb] = fixed
                    refine_count += 1
        vw.write(out_f)
        if (k + 1) % 200 == 0:
            print(f"  [ocr_telea] {label} {k+1}", flush=True)
    if skipped_win or rejected_dark or window_rescue or refine_count:
        print(
            f"[ocr_telea] {label}: window_skip={skipped_win} "
            f"window_rescue={window_rescue} reject_dark={rejected_dark} "
            f"refined={refine_count}",
            flush=True,
        )
    return removed


def run(source, out, *,
        band=DEFAULT_BAND, watermark=DEFAULT_WATERMARK,
        caption_stems=DEFAULT_CAPTION_STEMS, score_thr=DEFAULT_SCORE_THR,
        inpaint_radius=DEFAULT_INPAINT_RADIUS, pre_scan=DEFAULT_PRE_SCAN,
        feather_px=5, feather_sigma=1.5, temporal_smoothing=True, min_support=2,
        passes=1, max_frames=None, use_brightness=True, inpaint_method="telea",
        active_windows=None, reject_dark_fill=True):
    """对 source 跑 OCR 细字形洞 + 逐帧修补去字幕，写出 out（mp4, crf18, yuv420p）。

    - 全程流式：逐帧读取、不把整段视频读进内存（长片安全）。
    - 默认 ``inpaint_method=telea``（细洞局部插值）；``lama`` 仅 opt-in
      （``VITUAL_DEHARDSUB_INPAINT=lama``），整框 LaMa 易把轮廓涂黑。
    - 多遍迭代 `passes`：第 2 遍起在上一遍产物上重新 OCR，只补仍残留的目标文字；
      检测不到残留则提前停止（不白跑）。**实测默认关闭**：迭代遍会多遮大量框
      （60 帧样本 57→116），遮罩+羽化外扩波及带外场景文字，误删 damaged 0→2。
      故默认 passes=1；确有明显残留时才 `--passes 2` 并人工复核。
    - watermark=None 时用独立迭代器做角部常驻 logo 自动探测兜底。
    """
    source, out = Path(source), Path(out)
    fps, W, H, total = _probe(source)
    if not W or not H:
        raise RuntimeError(f"cannot probe frame size from {source}")
    ocr = _get_ocr()

    # watermark=False → caller owns overlays (hybrid up_watermark); skip auto.
    # watermark=None → legacy auto corner detect. Else use the given box.
    if watermark is False:
        wm_fixed = np.zeros((H, W), np.uint8)
        wm_box = None
    else:
        wm_box = watermark
        if wm_box is None:
            wm_box = _auto_detect_region(
                _iter_frames(source), ocr, pre_scan, prefer="corner", size=(H, W)
            )
        if wm_box is None:
            print(
                "[ocr_telea] warning: watermark auto-detect found nothing; skip watermark",
                file=sys.stderr,
            )
            wm_fixed = np.zeros((H, W), np.uint8)
        else:
            wm_fixed = build_watermark_mask(
                _iter_frames(source), ocr, wm_box, pre_scan, size=(H, W)
            )

    ff = _ffmpeg()
    tmps = []

    # Pass 1：OCR 收集 → 时序平滑 → 渲染
    boxes = _collect_all(source, ocr, band, caption_stems, score_thr, max_frames, label="ocr p1")
    if temporal_smoothing:
        boxes = temporal_smooth(boxes, min_support)
    if active_windows:
        print(
            f"[ocr_telea] active_windows={len(active_windows)} "
            f"(frames outside windows copied)",
            flush=True,
        )
    tmp1 = out.with_name(f"{out.stem}_p1.avi")
    vw = cv2.VideoWriter(str(tmp1), cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
    removed = _render_pass(
        source, vw, boxes, wm_fixed, band, inpaint_radius,
        feather_px, feather_sigma, max_frames, label="render p1",
        use_brightness=use_brightness, inpaint_method=inpaint_method,
        fps=fps, active_windows=active_windows,
        reject_dark_fill=reject_dark_fill,
    )
    vw.release()
    cur = tmp1
    tmps.append(cur)

    # Pass 2..N：多遍迭代补残留（水印已在第1遍去除，后续遍不再套水印遮罩）
    for p in range(2, max(1, int(passes)) + 1):
        # 迭代遍只补"确信的残留"：抬高置信阈值，避免把填充纹理误当文字再抹一遍
        score_p = min(0.95, score_thr + 0.25)
        boxes_p = _collect_all(cur, ocr, band, caption_stems, score_p, max_frames,
                               label=f"ocr p{p}")
        if temporal_smoothing:
            boxes_p = temporal_smooth(boxes_p, min_support)
        if sum(len(b) for b in boxes_p) == 0:
            print(f"[ocr_telea] pass {p}: no residual text detected, stop", flush=True)
            break
        tmp_p = out.with_name(f"{out.stem}_p{p}.avi")
        vw = cv2.VideoWriter(str(tmp_p), cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
        removed += _render_pass(
            cur, vw, boxes_p, np.zeros((H, W), np.uint8), band,
            inpaint_radius, feather_px, feather_sigma,
            max_frames, label=f"render p{p}", use_brightness=False,
            inpaint_method=inpaint_method,
            fps=fps, active_windows=active_windows,
            reject_dark_fill=reject_dark_fill,
        )
        vw.release()
        cur = tmp_p
        tmps.append(cur)

    # 回填源音轨（此前 -an 会丢声音；-map 1:a:0? 在源无音轨时自动跳过）
    # Hybrid intermediates `_hs_*.mp4` keep near-lossless; final deliverable 18.
    _crf = "12" if out.name.startswith("_") else "18"
    subprocess.run([ff, "-y", "-i", str(cur), "-i", str(source),
                    "-map", "0:v:0", "-map", "1:a:0?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", _crf,
                    "-pix_fmt", "yuv420p", str(out)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for t in tmps:
        Path(t).unlink(missing_ok=True)
    stats = {"engine": "ocr_telea", "frames": len(boxes),
             "text_boxes_masked": removed, "passes_used": len(tmps),
             "watermark_mask_pixels": int(wm_fixed.sum() // 255), "out": str(out)}
    print(f"[ocr_telea] done: {stats}", flush=True)
    return stats


# ----------------------------- 验收（残差 + 误删）-----------------------------
def _diff_at(gs, go, b, pad=6):
    H, W = gs.shape[:2]
    x0 = max(0, b["x"] - pad); y0 = max(0, b["y"] - pad)
    x1 = min(W, b["x"] + b["w"] + pad); y1 = min(H, b["y"] + b["h"] + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    return float(np.abs(gs[y0:y1, x0:x1].astype(np.int16) - go[y0:y1, x0:x1].astype(np.int16)).mean())


def qc_dehardsub(source, out, *,
                 band=DEFAULT_BAND, watermark=DEFAULT_WATERMARK,
                 caption_stems=DEFAULT_CAPTION_STEMS, step=2, miss_thr=12, dmg_thr=12):
    """SOURCE→OUTPUT 真实 diff 验收：目标框 output 几乎没变=漏删；非目标文字框变了=误删。"""
    source, out = Path(source), Path(out)
    cap_s = cv2.VideoCapture(str(source))
    cap_o = cv2.VideoCapture(str(out))
    fi = 0
    missed, damaged = [], []
    while True:
        ok1, fs = cap_s.read(); ok2, fo = cap_o.read()
        if not ok1 or not ok2:
            break
        if fi % step == 0:
            gs = cv2.cvtColor(fs, cv2.COLOR_BGR2GRAY)
            go = cv2.cvtColor(fo, cv2.COLOR_BGR2GRAY)
            res, _ = _get_ocr()(fs, use_det=True, use_cls=False, use_rec=True)
            if res:
                for item in res:
                    pts = np.asarray(item[0], dtype=np.float32)
                    x0, y0 = pts[:, 0].min(), pts[:, 1].min()
                    x1, y1 = pts[:, 0].max(), pts[:, 1].max()
                    b = {"x": int(x0), "y": int(y0), "w": int(x1 - x0), "h": int(y1 - y0)}
                    if b["w"] * b["h"] < DEFAULT_MIN_AREA or b["w"] < 12 or b["h"] < 8:
                        continue
                    txt = str(item[1]).strip()
                    is_target = (band is not None and _in_box(b, band)) or \
                                (watermark is not None and _in_box(b, watermark)) or \
                                txt.startswith("*") or any(s in txt for s in caption_stems)
                    d = _diff_at(gs, go, b)
                    if d is None:
                        continue
                    if is_target:
                        if d < miss_thr:
                            region = "band" if (band and _in_box(b, band)) else \
                                ("wm" if (watermark and _in_box(b, watermark)) else "cap")
                            missed.append((fi, region, round(d, 1), txt[:8]))
                    else:
                        if d > dmg_thr:
                            damaged.append((fi, txt[:8], round(d, 1)))
        fi += 1
        if fi % 400 == 0:
            print(f"  [ocr_telea] qc scanned {fi}", flush=True)
    cap_s.release(); cap_o.release()
    result = {"missed": missed, "damaged": damaged,
              "missed_count": len(missed), "damaged_count": len(damaged)}
    print(f"[ocr_telea] qc: missed(residual)={len(missed)}  damaged(scene removed)={len(damaged)}",
          flush=True)
    return result


# ----------------------------- 对比视频 -----------------------------
def make_compare(source, out, compare, fps=30):
    """左右对比视频（流式：逐帧读取拼接，不把两个视频都读进内存）。"""
    source, out, compare = Path(source), Path(out), Path(compare)
    ff = _ffmpeg()
    tmp = compare.with_suffix(".avi")
    cap_s = cv2.VideoCapture(str(source))
    cap_o = cv2.VideoCapture(str(out))
    vw = None
    n = 0
    try:
        while True:
            ok1, fs = cap_s.read()
            ok2, fo = cap_o.read()
            if not ok1 or not ok2:
                break
            if vw is None:
                gap = np.full((fs.shape[0], 8, 3), 60, np.uint8)
                vw = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"MJPG"), fps,
                                     (fs.shape[1] * 2 + 8, fs.shape[0]))
            vw.write(np.hstack([fs, gap, fo]))
            n += 1
            if n % 200 == 0:
                print(f"  [ocr_telea] compare {n}", flush=True)
    finally:
        cap_s.release(); cap_o.release()
        if vw is not None:
            vw.release()
    subprocess.run([ff, "-y", "-i", str(tmp), "-an", "-c:v", "libx264", "-crf", "20",
                   "-pix_fmt", "yuv420p", str(compare)], check=True,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp.unlink()
    print(f"[ocr_telea] compare saved: {compare} ({n} frames)", flush=True)
    return str(compare)


# ----------------------------- CLI -----------------------------
def _parse_box(s):
    if not s:
        return None
    x, y, w, h = (int(v) for v in s.split(","))
    return {"x": x, "y": y, "w": w, "h": h}


def main(argv=None):
    import argparse
    here = Path.cwd()
    media = here / "media"
    ap = argparse.ArgumentParser(description="OCR 细字形洞 + TELEA 逐帧去硬字幕（B站硬字幕首选路线）")
    ap.add_argument("--source", default=str(media / "source.mp4"))
    ap.add_argument("--out", default=str(media / "dehardsub_full.mp4"))
    ap.add_argument("--compare", default=None, help="生成左右对比视频路径（不传则不生成）")
    ap.add_argument("--qc", action="store_true", help="跑残差/误删验收")
    ap.add_argument("--band", default=None, help="字幕带 x,y,w,h（默认 BV1oFJ16BEMT）")
    ap.add_argument("--watermark", default=None, help="水印 x,y,w,h（默认 BV1oFJ16BEMT；传 'auto' 自动探测）")
    ap.add_argument("--score-thr", type=float, default=DEFAULT_SCORE_THR)
    ap.add_argument("--inpaint-radius", type=int, default=DEFAULT_INPAINT_RADIUS)
    ap.add_argument("--max-frames", type=int, default=None, help="仅处理前 N 帧（冒烟测试）")
    ap.add_argument("--pre-scan", type=int, default=DEFAULT_PRE_SCAN)
    ap.add_argument("--feather-px", type=int, default=5, help="边缘羽化宽度（最佳实践 3-5px）")
    ap.add_argument("--feather-sigma", type=float, default=1.5, help="边缘羽化高斯 sigma")
    ap.add_argument("--no-temporal-smooth", action="store_true", help="关闭时序平滑（抑制 OCR 抖动）")
    ap.add_argument("--min-support", type=int, default=2, help="时序平滑：邻帧支持数下限")
    ap.add_argument("--passes", type=int, default=1,
                    help="多遍迭代遍数；默认1（实测2会误删场景文字，仅在确有明显残留时开）")
    args = ap.parse_args(argv)

    band = _parse_box(args.band) or DEFAULT_BAND
    watermark = None if args.watermark == "auto" else (_parse_box(args.watermark) or DEFAULT_WATERMARK)

    run(args.source, args.out, band=band, watermark=watermark,
        score_thr=args.score_thr, inpaint_radius=args.inpaint_radius,
        pre_scan=args.pre_scan, max_frames=args.max_frames,
        feather_px=args.feather_px, feather_sigma=args.feather_sigma,
        temporal_smoothing=not args.no_temporal_smooth,
        min_support=args.min_support, passes=args.passes)

    if args.qc:
        qc_dehardsub(args.source, args.out, band=band, watermark=watermark)
    if args.compare:
        make_compare(args.source, args.out, args.compare)


if __name__ == "__main__":
    main()
