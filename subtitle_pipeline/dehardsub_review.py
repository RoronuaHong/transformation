"""去字幕成片的帧准确审阅与风险排序工具。

最佳实践（2026-11-23 修正）：
- 并排审阅视频必须逐帧 hstack，不能用 ffmpeg hstack 直接拼不同 time_base
  的两路流（framesync 会在切点重复/丢帧，导致看到假的错帧）。
- 自动评分只能做“优先复核排名”，不是画质百分制；客观指标在平滑区域、
  动画和时间伪影上有已知盲点，最终以肉眼/主观验收为准。
- 评分不再用 clean/source 全局 std 比惩罚擦字成功后的平滑区，只惩罚
  **非字形区域**的纹理丢失，并把 outside/global diff 合并避免重复计分。
"""
from __future__ import annotations

import csv
import json
import multiprocessing as mp
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from dehardsub_postfix import _load_boxes, _ffmpeg

RISK_NOTE = (
    "relative risk ranking only (p50->0, p95->100 per metric); "
    "not an objective quality score; visual confirmation required"
)


def _gray(frame: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _roi(frame: np.ndarray, box: dict[str, Any]):
    x0 = max(0, int(box.get("x", 0)))
    y0 = max(0, int(box.get("y", 0)))
    x1 = min(frame.shape[1], x0 + int(box.get("w", 0)))
    y1 = min(frame.shape[0], y0 + int(box.get("h", 0)))
    if x1 <= x0 or y1 <= y0:
        return None
    return frame[y0:y1, x0:x1]


def _text_mask(gray: np.ndarray) -> np.ndarray:
    """暗底亮字/局部高对比字形先验（相对框内中值与 90 分位）。"""
    med = float(np.median(gray))
    p90 = float(np.percentile(gray, 90))
    return (gray > max(med + 22, p90)) & (gray > 85)


def build_review_pair(
    source: str | Path,
    clean: str | Path,
    out: str | Path,
    *,
    labels: bool = True,
) -> dict[str, Any]:
    """逐帧 hstack 生成左源右成片的审阅视频，保留成片音轨。"""
    source, clean, out = Path(source), Path(clean), Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cs, cc = cv2.VideoCapture(str(source)), cv2.VideoCapture(str(clean))
    fps = float(cc.get(cv2.CAP_PROP_FPS) or 30.0) or 30.0
    w = int(cc.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cc.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if w <= 0 or h <= 0:
        raise RuntimeError(f"cannot probe clean frame size: {clean}")
    tmp = out.with_name(out.stem + ".raw.avi")
    vw = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"MJPG"), fps, (w * 2, h))
    written = 0
    while True:
        ok1, fs = cs.read()
        ok2, fc = cc.read()
        if not (ok1 and ok2):
            break
        left, right = fs.copy(), fc.copy()
        if labels:
            cv2.putText(left, "SRC", (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                        (0, 0, 255), 2, cv2.LINE_AA)
            cv2.putText(right, "CLEAN", (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                        (0, 255, 0), 2, cv2.LINE_AA)
        vw.write(np.hstack([left, right]))
        written += 1
    vw.release()

    extra_s = extra_c = 0
    while cs.read()[0]:
        extra_s += 1
    while cc.read()[0]:
        extra_c += 1
    cs.release(); cc.release()

    ff = _ffmpeg()
    subprocess.run([
        ff, "-y", "-loglevel", "error",
        "-i", str(tmp), "-i", str(clean),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-frames:v", str(written),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(out),
    ], check=True)
    tmp.unlink(missing_ok=True)
    return {
        "source": str(source), "clean": str(clean), "out": str(out),
        "source_frames": written + extra_s, "clean_frames": written + extra_c,
        "written_frames": written,
        "aligned": extra_s == 0 and extra_c == 0,
    }


def _raw_metrics(fs: np.ndarray, fc: np.ndarray, boxes: list[dict[str, Any]],
                 width: int, height: int) -> dict[str, float]:
    gs_full, gc_full = _gray(fs), _gray(fc)
    diff = cv2.absdiff(gs_full, gc_full)
    frame_diff = float(diff.mean())

    sub_res = 0.0
    wm_res = 0.0
    texture_loss = 0.0
    w2b = 0.0
    for i, b in enumerate(boxes):
        sroi, croi = _roi(fs, b), _roi(fc, b)
        if sroi is None or croi is None:
            continue
        gs, gc = _gray(sroi), _gray(croi)
        ink = _text_mask(gs)
        role = str(b.get("role") or "").lower()
        if i == 0 or role == "dialogue":
            if int(ink.sum()) >= 12:
                sub_res = max(0.0, float(gc[ink].mean()) - float(np.median(gc)))
            w2b = float(((gs > 170) & (gc < 90)).mean())
        else:
            if int(ink.sum()) >= 15:
                wm_res = max(wm_res, max(0.0, float(gc[ink].mean()) - float(np.median(gc))))

        # 只惩罚非字形区域的纹理丢失：擦掉字后局部变平是成功，不是缺陷。
        glyph = cv2.dilate(ink.astype(np.uint8) * 255, np.ones((5, 5), np.uint8)) > 0
        keep = ~glyph
        if int(keep.sum()) > 80 and sroi.ndim == 3 and croi.ndim == 3:
            s_pix, c_pix = sroi[keep], croi[keep]
            sg = cv2.cvtColor(s_pix.reshape(-1, 1, 3), cv2.COLOR_BGR2GRAY)
            cg = cv2.cvtColor(c_pix.reshape(-1, 1, 3), cv2.COLOR_BGR2GRAY)
            sstd, cstd = float(sg.std()), float(cg.std())
            if sstd > 6:
                texture_loss = max(texture_loss, 1.0 - cstd / sstd)

    return {
        "sub_res": round(sub_res, 3),
        "wm_res": round(wm_res, 3),
        "texture_loss": round(max(0.0, texture_loss), 4),
        "w2b": round(w2b, 5),
        "frame_diff": round(frame_diff, 3),
    }


def _score_chunk(args: tuple[Any, Any, list[dict[str, Any]], float, int, int]) -> list[dict[str, Any]]:
    """模块级 worker，保证 Windows spawn 可 pickle。"""
    source, clean, boxes, fps, start, end = args
    s, c = cv2.VideoCapture(str(source)), cv2.VideoCapture(str(clean))
    for _ in range(start):
        if not (s.read()[0] and c.read()[0]):
            break
    rows: list[dict[str, Any]] = []
    for fi in range(start, end):
        ok1, fs = s.read()
        ok2, fc = c.read()
        if not (ok1 and ok2):
            break
        m = _raw_metrics(fs, fc, boxes, fc.shape[1], fc.shape[0])
        m["frame"] = fi
        m["time_sec"] = round(fi / fps, 3)
        rows.append(m)
    s.release(); c.release()
    return rows


def score_risk(
    source: str | Path,
    clean: str | Path,
    regions: str | Path,
    *,
    out_csv: str | Path,
    out_json: str | Path,
    out_sheet: str | Path | None = None,
    workers: int = 6,
) -> dict[str, Any]:
    """对全部帧输出相对风险排名（高分=更需要人工复核，不是画质分）。"""
    dialogue, watermarks = _load_boxes(regions)
    boxes = ([dialogue] if dialogue else []) + list(watermarks)
    cap = cv2.VideoCapture(str(source))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    fps = float(cv2.VideoCapture(str(clean)).get(cv2.CAP_PROP_FPS) or 30.0) or 30.0

    chunk_args = [
        (source, clean, boxes, fps, i, min(i + 192, total))
        for i in range(0, total, 192)
    ]
    if workers <= 1:
        parts = [_score_chunk(a) for a in chunk_args]
    else:
        with mp.Pool(min(workers, len(chunk_args) or 1)) as pool:
            parts = pool.map(_score_chunk, chunk_args)
    rows = [r for part in parts for r in part]
    rows.sort(key=lambda r: r["frame"])
    if not rows:
        raise RuntimeError("no frames scored")

    def _pen(key: str) -> list[float]:
        arr = np.asarray([r[key] for r in rows], dtype=np.float64)
        lo, hi = float(np.percentile(arr, 50)), float(np.percentile(arr, 95))
        if hi <= lo:
            hi = lo + 1e-6
        return [float(np.clip((v - lo) / (hi - lo) * 100.0, 0.0, 100.0)) for v in arr]

    pens = {k: _pen(k) for k in ("sub_res", "wm_res", "texture_loss", "w2b", "frame_diff")}
    weights = {"sub_res": 0.30, "wm_res": 0.30, "texture_loss": 0.15,
               "w2b": 0.10, "frame_diff": 0.15}
    for i, r in enumerate(rows):
        for k in pens:
            r[k + "_risk"] = round(pens[k][i], 2)
        r["risk_score"] = round(sum(weights[k] * pens[k][i] for k in weights), 2)
        r["human_review_priority"] = "high" if r["risk_score"] >= 70 else (
            "medium" if r["risk_score"] >= 40 else "low")

    fields = ["frame", "time_sec", "risk_score", "human_review_priority"] + [
        k + "_risk" for k in pens] + list(pens)
    out_csv = Path(out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)

    top = sorted(rows, key=lambda r: r["risk_score"], reverse=True)[:30]
    report = {"note": RISK_NOTE, "frames_scored": len(rows), "worst30": top}
    out_json = Path(out_json)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if out_sheet:
        picks = sorted(top[:12], key=lambda r: r["frame"])
        s, c = cv2.VideoCapture(str(source)), cv2.VideoCapture(str(clean))
        cells = []
        for r in picks:
            n = r["frame"]
            s.set(cv2.CAP_PROP_POS_FRAMES, n)
            c.set(cv2.CAP_PROP_POS_FRAMES, n)
            _, fs = s.read(); _, fc = c.read()
            if fs is None or fc is None:
                continue
            sf = cv2.resize(fs, (426, 240), interpolation=cv2.INTER_AREA)
            cf = cv2.resize(fc, (426, 240), interpolation=cv2.INTER_AREA)
            cv2.putText(sf, f"f{n} SRC", (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
            cv2.putText(cf, f"f{n} risk={r['risk_score']}", (6, 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            cells.append(np.vstack([sf, cf]))
        while len(cells) % 3:
            cells.append(np.zeros((480, 426, 3), np.uint8))
        grid = np.vstack([np.hstack(cells[i:i+3]) for i in range(0, len(cells), 3)])
        cv2.imwrite(str(out_sheet), grid)
    return {"frames_scored": len(rows), "csv": str(out_csv), "json": str(out_json),
            "sheet": str(out_sheet) if out_sheet else None}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="逐帧审阅视频与风险排序（非画质分）")
    ap.add_argument("--source", required=True)
    ap.add_argument("--clean", required=True)
    ap.add_argument("--regions", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--json", required=True)
    ap.add_argument("--sheet", default=None)
    ap.add_argument("--review-out", default=None)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    if a.review_out:
        print(json.dumps(build_review_pair(a.source, a.clean, a.review_out), ensure_ascii=False, indent=2))
    print(json.dumps(score_risk(a.source, a.clean, a.regions, out_csv=a.csv,
                                out_json=a.json, out_sheet=a.sheet,
                                workers=a.workers), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
