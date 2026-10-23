import sys, pathlib, json
import cv2, numpy as np
sys.path.insert(0, r"D:\MineWeb\2026\Vitual\subtitle_pipeline")
from glyph_detect import _get_ocr

M = pathlib.Path(r"D:\MineWeb\2026\Vitual\subtitle_pipeline\downloads\batch\bilibili_BV1oFJ16BEMT\media")
BAND = {"x": 270, "y": 598, "w": 712, "h": 100}
WM = {"x": 985, "y": 638, "w": 290, "h": 72}
CAP_STEMS = ["无微不至", "感慨"]
STEP = 2
OCR = _get_ocr()

def in_box(b, box):
    bx1, by1 = b["x"] + b["w"], b["y"] + b["h"]
    x0, y0, x1, y1 = box["x"], box["y"], box["x"] + box["w"], box["y"] + box["h"]
    return not (bx1 < x0 or b["x"] > x1 or by1 < y0 or b["y"] > y1)

def diff_at(gs, go, b, pad=6):
    H, W = gs.shape[:2]
    x0 = max(0, b["x"] - pad); y0 = max(0, b["y"] - pad)
    x1 = min(W, b["x"] + b["w"] + pad); y1 = min(H, b["y"] + b["h"] + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    return float(np.abs(gs[y0:y1, x0:x1].astype(np.int16) - go[y0:y1, x0:x1].astype(np.int16)).mean())

cap_s = cv2.VideoCapture(str(M / "source.mp4"))
cap_o = cv2.VideoCapture(str(M / "dehardsub_full.mp4"))
fi = 0
miss = []; damage = []
while True:
    ok1, fs = cap_s.read(); ok2, fo = cap_o.read()
    if not ok1 or not ok2:
        break
    if fi % STEP == 0:
        gs = cv2.cvtColor(fs, cv2.COLOR_BGR2GRAY)
        go = cv2.cvtColor(fo, cv2.COLOR_BGR2GRAY)
        res, _ = OCR(fs, use_det=True, use_cls=False, use_rec=True)
        if res:
            for item in res:
                pts = np.asarray(item[0], dtype=np.float32)
                x0, y0 = pts[:, 0].min(), pts[:, 1].min()
                x1, y1 = pts[:, 0].max(), pts[:, 1].max()
                b = {"x": int(x0), "y": int(y0), "w": int(x1 - x0), "h": int(y1 - y0)}
                if b["w"] * b["h"] < 90 or b["w"] < 12 or b["h"] < 8:
                    continue
                txt = str(item[1]).strip()
                is_target = in_box(b, BAND) or in_box(b, WM) or txt.startswith("*") or any(s in txt for s in CAP_STEMS)
                d = diff_at(gs, go, b)
                if d is None:
                    continue
                if is_target:
                    if d < 12:
                        region = "band" if in_box(b, BAND) else ("wm" if in_box(b, WM) else "cap")
                        miss.append((fi, region, round(d, 1), txt[:8]))
                else:
                    if d > 12:   # 非目标文字被改动了 → 误删场景内容
                        damage.append((fi, txt[:8], round(d, 1)))
    fi += 1
    if fi % 400 == 0:
        print(f"  scanned {fi}", flush=True)
cap_s.release(); cap_o.release()
print(f"\nMISSED (residual): {len(miss)}  e.g. {miss[:20]}", flush=True)
print(f"DAMAGED (scene/menu removed): {len(damage)}  e.g. {damage[:20]}", flush=True)
json.dump({"miss": miss, "damage": damage}, open(M/"_detect"/"qc_v2.json", "w", encoding="utf-8"), ensure_ascii=False)
print("saved qc_v2.json", flush=True)
