"""BV 全流程：下载 → 生成 2 个视频
  A: 去字幕 + 去水印  -> <bvid>_dehardsub.mp4
  B: 不去字幕 + 去水印 -> <bvid>_keepsub_demark.mp4

用法:
  .venv/Scripts/python.exe run_dual_watermark.py inspect   # 下载 + 探测水印 + 预览帧
  .venv/Scripts/python.exe run_dual_watermark.py process   # 生成 A/B 两个成品
"""
from __future__ import annotations

import os
import sys
import json
import time
import shutil
from pathlib import Path

# Bilibili 下载必须走直连（代理会破坏其 TLS），先清掉代理环境变量。
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fetch_media import download_bilibili_api_source, detect_bvid
from media_ops import resolve_hardsub_boxes
from visual_cleanup import run_multipass_cleanup, _media
from dynamic_watermark import detect_moving_watermark

URL = "https://www.bilibili.com/video/BV1erPNzeEUE/?spm_id_from=333.337.search-card.all.click"
BVID = detect_bvid(URL)
assert BVID, "无法解析 BV 号"

MEDIA = ROOT / "downloads" / "batch" / f"bilibili_{BVID}" / "media"
MEDIA.mkdir(parents=True, exist_ok=True)
SRC = MEDIA / "source.mp4"
PREVIEW = MEDIA / "preview"
PREVIEW.mkdir(exist_ok=True)


def _download() -> None:
    if SRC.is_file() and SRC.stat().st_size > 1_000_000:
        print(f"[dual] 已存在源 {SRC} ({SRC.stat().st_size // 1024}KB)，跳过下载", flush=True)
        return
    print(f"[dual] 下载 {URL} -> {SRC}", flush=True)
    info = download_bilibili_api_source(URL, SRC)
    print("[dual] download:", json.dumps(
        {k: v for k, v in info.items() if k != "source_file"}, ensure_ascii=False, default=str), flush=True)


def _detect():
    mops = _media()
    wh = mops.probe_video_wh(SRC)
    assert wh, f"无法探测分辨率: {SRC}"
    W, H = wh
    boxes = resolve_hardsub_boxes(SRC, W, H, mode="band")
    summary = []
    for b in boxes:
        bb = b.get("box") or {}
        summary.append({"role": b.get("role"), "source": b.get("source"), "box": bb})
    wm_boxes = [b for b in boxes if b.get("role") in ("watermark", "up_watermark")]
    wm_override = []
    for b in wm_boxes:
        bb = b["box"]
        wm_override.append({"x": int(bb["x"]), "y": int(bb["y"]),
                            "w": int(bb["w"]), "h": int(bb["h"]), "role": b.get("role")})
    mw = detect_moving_watermark(SRC)
    return W, H, summary, wm_override, mw


def _save_previews(W, H, wm_override, mw):
    import cv2
    cap = cv2.VideoCapture(str(SRC))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fracs = [0.1, 0.5, 0.9]
    for fi, fr in enumerate(fracs):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fr * total))
        ok, frame = cap.read()
        if not ok:
            continue
        for b in wm_override:
            x, y, w, h = int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
            col = (0, 0, 255) if b.get("role") == "watermark" else (255, 0, 255)
            cv2.rectangle(frame, (x, y), (x + w, y + h), col, 2)
            cv2.putText(frame, b.get("role", ""), (x, max(0, y - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, col, 2)
        if mw:
            traj = mw.get("trajectory") or []
            idx = min(len(traj) - 1, int(fr * total))
            if 0 <= idx < len(traj):
                tb = traj[idx]
                x, y, w, h = int(tb["x"]), int(tb["y"]), int(tb["w"]), int(tb["h"])
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 255), 2)
                cv2.putText(frame, "moving", (x, max(0, y - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        out = PREVIEW / f"frame_{int(fr * 100):02d}.png"
        cv2.imwrite(str(out), frame)
        print(f"[dual] preview -> {out}", flush=True)
    cap.release()


def inspect():
    _download()
    W, H, summary, wm_override, mw = _detect()
    print(f"[dual] resolution={W}x{H}", flush=True)
    print("[dual] detected regions:", json.dumps(summary, ensure_ascii=False), flush=True)
    print(f"[dual] static watermark boxes: {len(wm_override)} -> {wm_override}", flush=True)
    if mw:
        print(f"[dual] MOVING watermark: motion_px={mw.get('motion_px')} "
              f"traj_frames={len(mw.get('trajectory') or [])}", flush=True)
    else:
        print("[dual] no moving watermark detected", flush=True)
    _save_previews(W, H, wm_override, mw)
    print("[dual] inspect done. preview frames in", PREVIEW, flush=True)


def process():
    _download()
    W, H, summary, _wm_detected, _mw = _detect()    # 检测器的右下 watermark / 右上 up_watermark / 动态水印轨迹经预览核实均为
    # 假阳性（真实水印是左上角 UP 名 + bilibili logo，检测器不扫左上）→ 全部弃用，
    # 改用人工核实的左上水印框（role=up_watermark 走 ocr_telea 字形洞，保留背景）。
    wm_box = {"x": 10, "y": 18, "w": 380, "h": 48, "role": "up_watermark"}
    dlg = next((b for b in summary if b.get("role") == "dialogue"), None)
    dlg_box = dlg["box"] if dlg else None
    print(f"[dual] resolution={W}x{H}  wm_box={wm_box}  dlg_box={dlg_box}", flush=True)
    assert dlg_box, "未检出字幕带，中止（避免盲跑）"

    # A: 去字幕 + 去水印
    outA = MEDIA / f"{BVID}_dehardsub.mp4"
    print(f"\n[dual] === A 去字幕+去水印 -> {outA.name} ===", flush=True)
    boxes_a = [
        {"x": int(dlg_box["x"]), "y": int(dlg_box["y"]),
         "w": int(dlg_box["w"]), "h": int(dlg_box["h"]), "role": "dialogue"},
        dict(wm_box),
    ]
    t0 = time.perf_counter()
    resA = run_multipass_cleanup(SRC, outA, dehardsub=True, demosaic=False,
                                 engine="ocr_telea", locate_mode="band",
                                 boxes_override=boxes_a)
    print(f"[dual] A action={resA.get('action')} elapsed={round(time.perf_counter() - t0, 1)}s", flush=True)
    print(f"[dual] A -> {outA} ({outA.stat().st_size // 1024}KB)", flush=True)

    # B: 不去字幕 + 去水印（只传水印框）
    outB = MEDIA / f"{BVID}_keepsub_demark.mp4"
    print(f"\n[dual] === B 不去字幕+去水印 -> {outB.name} ===", flush=True)
    t0 = time.perf_counter()
    resB = run_multipass_cleanup(SRC, outB, dehardsub=True, demosaic=False,
                                 engine="ocr_telea", locate_mode="band",
                                 boxes_override=[dict(wm_box)])
    print(f"[dual] B action={resB.get('action')} elapsed={round(time.perf_counter() - t0, 1)}s", flush=True)
    print(f"[dual] B -> {outB} ({outB.stat().st_size // 1024}KB)", flush=True)

    print("\n[dual] DONE", flush=True)
    print(json.dumps({
        "source": str(SRC),
        "A_dehardsub_demark": str(outA),
        "B_keepsub_demark": str(outB),
        "watermark_box": wm_box,
        "dialogue_box": dlg_box,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "inspect"
    if stage == "inspect":
        inspect()
    elif stage == "process":
        process()
    else:
        print(__doc__)
