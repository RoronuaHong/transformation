"""Re-run optimized general 10s cleanup and score residual glyphs."""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2

from sttn_inpaint import classify_dialogue_route
from visual_cleanup import run_multipass_cleanup

ROOT = Path(__file__).resolve().parent / "downloads/mode-renders/BV1Sqgp6kEPN_gpu"
OUT = ROOT / "_compare10s"
CLIP_SRC = OUT / "src_10s.mp4"
CLIP_GEN = OUT / "fixed_general_10s.mp4"
BOX = {"x": 1, "y": 890, "w": 1593, "h": 189}


def main() -> None:
    route = classify_dialogue_route(CLIP_SRC, BOX)
    print(f"[opt] classify={route}", flush=True)
    t0 = time.time()
    meta = run_multipass_cleanup(
        CLIP_SRC,
        CLIP_GEN,
        work_dir=OUT / "_vlm_general2",
        locate_mode="band",
        ratio=0.14,
        demosaic=False,
        dehardsub=True,
        engine="sttn",
        polish_residual_floor=1.0,
        active_windows=[(0.0, 10.0)],
    )
    print(f"[opt] done {time.time() - t0:.1f}s action={meta.get('action')}", flush=True)
    (OUT / "general_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    cap = cv2.VideoCapture(str(CLIP_GEN))
    cap.set(cv2.CAP_PROP_POS_MSEC, 5000)
    ok, fr = cap.read()
    cap.release()
    assert ok and fr is not None
    crop = fr[820:1080, 350:1650]
    (OUT / "frames").mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(OUT / "frames/gen2_5_crop.jpg"), crop)
    band = fr[960:1060, 400:1500]
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    bright = float((gray > 120).mean())
    box_h = (route.get("box") or {}).get("h")
    print(f"[opt] residual_bright_frac={bright:.5f} classify_h={box_h}", flush=True)


if __name__ == "__main__":
    main()
