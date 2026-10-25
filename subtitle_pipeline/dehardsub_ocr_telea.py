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
DEFAULT_BAND = {"x": 270, "y": 598, "w": 712, "h": 100}
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
    """自适应细字形洞：取框内偏离本地的极端像素（亮底白字/暗底白字都行）。"""
    H, W = gray.shape
    x0 = max(0, b["x"] - pad); y0 = max(0, b["y"] - pad)
    x1 = min(W, b["x"] + b["w"] + pad); y1 = min(H, b["y"] + b["h"] + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    roi = gray[y0:y1, x0:x1].astype(np.float32)
    lo = np.percentile(roi, 12); hi = np.percentile(roi, 88)
    s = ((roi < lo) | (roi > hi)).astype(np.uint8) * 255
    s = cv2.morphologyEx(s, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
    s = cv2.dilate(s, np.ones((3, 3), np.uint8), iterations=DEFAULT_DILATE)
    if int(s.max()) == 0:
        s = np.full_like(s, 255)
    return s, (x0, y0)


def band_white_mask(gray, band):
    """底部对白带内白字字形（不依赖 OCR，兜住漏检）。仅限字幕行中心区 + 文本尺寸，保守补充。"""
    x0, y0, w, h = band["x"], band["y"], band["w"], band["h"]
    crop = gray[y0:y0 + h, x0:x0 + w]
    white = (crop > 200).astype(np.uint8)
    white = cv2.morphologyEx(white, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
    num, labels, stats, cent = cv2.connectedComponentsWithStats(white, 8)
    m = np.zeros_like(white)
    for i in range(1, num):
        a = stats[i]; bw, bh, area = a[2], a[3], a[4]
        cy = cent[i][1] + y0
        if area < 25 or bh < 14 or bh > 72 or bw < 10:
            continue
        if not (band["y"] + 17 <= cy <= band["y"] + band["h"] - 17):
            continue
        m[labels == i] = 255
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=1)
    return m, (x0, y0)


def build_watermark_mask(frames, ocr, wm_box, pre_scan=DEFAULT_PRE_SCAN):
    """预扫前 pre_scan 帧，取水印字形并集做成固定遮罩（水印是静态 logo，逐帧 OCR 会漏检）。"""
    if wm_box is None:
        wm_box = _auto_detect_region(frames, ocr, pre_scan, prefer="corner")
    if wm_box is None:
        return np.zeros((frames[0].shape[0], frames[0].shape[1]), np.uint8)
    H, W = frames[0].shape[:2]
    mask = np.zeros((H, W), np.uint8)
    for k in range(min(pre_scan, len(frames))):
        gray = cv2.cvtColor(frames[k], cv2.COLOR_BGR2GRAY).astype(np.float32)
        res, _ = ocr(frames[k], use_det=True, use_cls=False, use_rec=True)
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


def _auto_detect_region(frames, ocr, pre_scan, prefer="corner"):
    """兜底：跨预扫帧找最常出现、位置稳定的文本块（水印=角部常驻；字幕带=下部常现）。返回 box 或 None。"""
    H, W = frames[0].shape[:2]
    from collections import defaultdict
    buckets = defaultdict(int)
    for k in range(min(pre_scan, len(frames))):
        res, _ = ocr(frames[k], use_det=True, use_cls=False, use_rec=True)
        if not res:
            continue
        for item in res:
            pts = np.asarray(item[0], dtype=np.float32)
            x0, y0 = pts[:, 0].min(), pts[:, 1].min()
            x1, y1 = pts[:, 0].max(), pts[:, 1].max()
            cx, cy = int((x0 + x1) / 2) // 24, int((y0 + y1) / 2) // 24
            if prefer == "corner" and not (cx > W // 24 * 0.6 or cy > H // 24 * 0.6):
                # 偏好右下/左下角
                if cx < W // 24 * 0.6 and cy < H // 24 * 0.6:
                    continue
            buckets[(cx, cy)] += 1
    if not buckets:
        return None
    (cx, cy), cnt = max(buckets.items(), key=lambda kv: kv[1])
    if cnt < max(3, pre_scan * 0.4):
        return None
    return {"x": cx * 24 - 40, "y": cy * 24 - 30, "w": 120, "h": 60}


# ----------------------------- 主处理 -----------------------------
def run(source, out, *,
        band=DEFAULT_BAND, watermark=DEFAULT_WATERMARK,
        caption_stems=DEFAULT_CAPTION_STEMS, score_thr=DEFAULT_SCORE_THR,
        inpaint_radius=DEFAULT_INPAINT_RADIUS, pre_scan=DEFAULT_PRE_SCAN,
        max_frames=None):
    """对 source 跑 OCR 细字形洞 + TELEA 去字幕，写出 out（mp4, crf18, yuv420p）。"""
    source, out = Path(source), Path(out)
    cap = cv2.VideoCapture(str(source))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
        if max_frames and len(frames) >= max_frames:
            break
    cap.release()
    if not frames:
        raise RuntimeError(f"no frames read from {source}")
    H, W = frames[0].shape[:2]
    ocr = _get_ocr()

    wm_fixed = build_watermark_mask(frames, ocr, watermark, pre_scan)
    if int(wm_fixed.max()) == 0:
        print("[ocr_telea] warning: no watermark mask built (watermark=None and auto-detect found nothing)",
              file=sys.stderr)

    ff = _ffmpeg()
    tmp = out.with_suffix(".avi")
    vw = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
    removed = 0
    for k, f in enumerate(frames):
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        mask = wm_fixed.copy()
        # 亮度兜底：底部对白带白字
        if band is not None:
            bm, (bx, by) = band_white_mask(gray, band)
            if int(bm.max()) > 0:
                mask[by:by + bm.shape[0], bx:bx + bm.shape[1]] = np.maximum(
                    mask[by:by + bm.shape[0], bx:bx + bm.shape[1]], bm)
        # OCR：对白带 + 浮动旁白
        res, _ = ocr(f, use_det=True, use_cls=False, use_rec=True)
        if res:
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
                is_target = (band is not None and _in_box(b, band)) or \
                            txt.startswith("*") or any(s in txt for s in caption_stems)
                if not is_target:
                    continue
                r = thin_mask(gray, b)
                if r:
                    s, (mx, my) = r
                    mask[my:my + s.shape[0], mx:mx + s.shape[1]] = np.maximum(
                        mask[my:my + s.shape[0], mx:mx + s.shape[1]], s)
                    removed += 1
        if int(mask.max()) > 0:
            out_f = cv2.inpaint(f, mask, inpaint_radius, cv2.INPAINT_TELEA)
        else:
            out_f = f
        vw.write(out_f)
    vw.release()
    subprocess.run([ff, "-y", "-i", str(tmp), "-an", "-c:v", "libx264", "-crf", "18",
                   "-pix_fmt", "yuv420p", str(out)], check=True,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tmp.unlink()
    stats = {"engine": "ocr_telea", "frames": len(frames), "text_boxes_masked": removed,
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
    cap_s.release(); cap_o.release()
    result = {"missed": missed, "damaged": damaged,
              "missed_count": len(missed), "damaged_count": len(damaged)}
    print(f"[ocr_telea] qc: missed(residual)={len(missed)}  damaged(scene removed)={len(damaged)}",
          flush=True)
    return result


# ----------------------------- 对比视频 -----------------------------
def make_compare(source, out, compare, fps=30):
    source, out, compare = Path(source), Path(out), Path(compare)
    cap = cv2.VideoCapture(str(source)); s = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        s.append(f)
    cap.release()
    cap = cv2.VideoCapture(str(out)); d = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        d.append(f)
    cap.release()
    n = min(len(s), len(d))
    gap = np.full((s[0].shape[0], 8, 3), 60, np.uint8)
    ff = _ffmpeg()
    tmp = compare.with_suffix(".avi")
    vw = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"MJPG"), fps, (s[0].shape[1] * 2 + 8, s[0].shape[0]))
    for k in range(n):
        vw.write(np.hstack([s[k], gap, d[k]]))
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
    args = ap.parse_args(argv)

    band = _parse_box(args.band) or DEFAULT_BAND
    watermark = None if args.watermark == "auto" else (_parse_box(args.watermark) or DEFAULT_WATERMARK)

    run(args.source, args.out, band=band, watermark=watermark,
        score_thr=args.score_thr, inpaint_radius=args.inpaint_radius,
        pre_scan=args.pre_scan, max_frames=args.max_frames)

    if args.qc:
        qc_dehardsub(args.source, args.out, band=band, watermark=watermark)
    if args.compare:
        make_compare(args.source, args.out, args.compare)


if __name__ == "__main__":
    main()
