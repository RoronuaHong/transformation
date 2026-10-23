import sys, pathlib, subprocess
import cv2, numpy as np
sys.path.insert(0, r"D:\MineWeb\2026\Vitual\subtitle_pipeline")
from glyph_detect import _get_ocr

FF = r"D:\MineWeb\2026\Vitual\subtitle_pipeline\.venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win-x86_64-v7.1.exe"
SRC = pathlib.Path(r"D:\MineWeb\2026\Vitual\subtitle_pipeline\downloads\batch\bilibili_BV1oFJ16BEMT\media\source.mp4")
OUT = pathlib.Path(r"D:\MineWeb\2026\Vitual\subtitle_pipeline\downloads\batch\bilibili_BV1oFJ16BEMT\media\dehardsub_full.mp4")

BAND = {"x": 270, "y": 598, "w": 712, "h": 100}
WM = {"x": 985, "y": 638, "w": 290, "h": 72}
CAP_STEMS = ["无微不至", "感慨"]
MIN_AREA = 90
SCORE = 0.25

def in_box(b, box):
    bx1, by1 = b["x"] + b["w"], b["y"] + b["h"]
    x0, y0, x1, y1 = box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]
    return not (bx1 < x0 or b["x"] > x1 or by1 < y0 or b["y"] > y1)

def thin_mask(gray, b, pad=6):
    H, W = gray.shape
    x0 = max(0, b["x"] - pad); y0 = max(0, b["y"] - pad)
    x1 = min(W, b["x"] + b["w"] + pad); y1 = min(H, b["y"] + b["h"] + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    roi = gray[y0:y1, x0:x1].astype(np.float32)
    lo = np.percentile(roi, 12); hi = np.percentile(roi, 88)
    s = ((roi < lo) | (roi > hi)).astype(np.uint8) * 255
    s = cv2.morphologyEx(s, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
    s = cv2.dilate(s, np.ones((3, 3), np.uint8), iterations=2)
    if int(s.max()) == 0:
        s = np.full_like(s, 255)
    return s, (x0, y0)

def band_white_mask(gray):
    """底部对白带内的白字字形（不依赖 OCR，兜住漏检）。仅限字幕行中心区 + 文本尺寸，保守补充。"""
    x0, y0, w, h = BAND["x"], BAND["y"], BAND["w"], BAND["h"]
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
        if not (615 <= cy <= 685):
            continue
        m[labels == i] = 255
    m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=1)
    return m, (x0, y0)

cap = cv2.VideoCapture(str(SRC))
fps = cap.get(cv2.CAP_PROP_FPS) or 30
frames = []
while True:
    ok, f = cap.read()
    if not ok:
        break
    frames.append(f)
cap.release()
H, W = frames[0].shape[:2]
print("frames:", len(frames), flush=True)

ocr = _get_ocr()

# 预扫：水印固定字形并集
wm_fixed = np.zeros((H, W), np.uint8)
pre = min(200, len(frames))
for k in range(pre):
    gray = cv2.cvtColor(frames[k], cv2.COLOR_BGR2GRAY).astype(np.float32)
    res, _ = ocr(frames[k], use_det=True, use_cls=False, use_rec=True)
    if not res:
        continue
    for item in res:
        pts = np.asarray(item[0], dtype=np.float32)
        x0, y0 = pts[:, 0].min(), pts[:, 1].min()
        x1, y1 = pts[:, 0].max(), pts[:, 1].max()
        b = {"x": int(x0), "y": int(y0), "w": int(x1 - x0), "h": int(y1 - y0)}
        if in_box(b, WM):
            r = thin_mask(gray, b)
            if r:
                s, (mx, my) = r
                wm_fixed[my:my + s.shape[0], mx:mx + s.shape[1]] = np.maximum(
                    wm_fixed[my:my + s.shape[0], mx:mx + s.shape[1]], s)
wm_fixed = cv2.dilate(wm_fixed, np.ones((3, 3), np.uint8), iterations=1)
print(f"wm_fixed pixels: {int(wm_fixed.sum()//255)}", flush=True)

tmp = OUT.with_suffix(".avi")
vw = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
for k, f in enumerate(frames):
    gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
    mask = wm_fixed.copy()
    # 亮度兜底：底部对白带白字
    bm, (bx, by) = band_white_mask(gray)
    if int(bm.max()) > 0:
        mask[by:by + bm.shape[0], bx:bx + bm.shape[1]] = np.maximum(
            mask[by:by + bm.shape[0], bx:bx + bm.shape[1]], bm)
    # OCR：浮动旁白 + 容错
    res, _ = ocr(f, use_det=True, use_cls=False, use_rec=True)
    if res:
        for item in res:
            pts = np.asarray(item[0], dtype=np.float32)
            x0, y0 = pts[:, 0].min(), pts[:, 1].min()
            x1, y1 = pts[:, 0].max(), pts[:, 1].max()
            b = {"x": int(x0), "y": int(y0), "w": int(x1 - x0), "h": int(y1 - y0)}
            if b["w"] * b["h"] < MIN_AREA or b["w"] < 12 or b["h"] < 8:
                continue
            txt = str(item[1]).strip()
            if float(item[2] if len(item) > 2 else 0) < SCORE:
                continue
            # 对白带 + 浮动旁白（水印已由固定遮罩兜底；亮度兜底补充漏检）
            if in_box(b, BAND) or txt.startswith("*") or any(s in txt for s in CAP_STEMS):
                r = thin_mask(gray, b)
                if r:
                    s, (mx, my) = r
                    mask[my:my + s.shape[0], mx:mx + s.shape[1]] = np.maximum(
                        mask[my:my + s.shape[0], mx:mx + s.shape[1]], s)
    if int(mask.max()) > 0:
        out = cv2.inpaint(f, mask, 3, cv2.INPAINT_TELEA)
    else:
        out = f
    vw.write(out)
    if (k + 1) % 200 == 0:
        print(f"  {k+1}/{len(frames)}", flush=True)
vw.release()
subprocess.run([FF, "-y", "-i", str(tmp), "-an", "-c:v", "libx264", "-crf", "18",
                "-pix_fmt", "yuv420p", str(OUT)], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
tmp.unlink()
print("DONE", OUT, flush=True)
