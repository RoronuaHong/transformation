"""Dehardsub self-check gate.

Streams SRC vs CLEAN over the subtitle ROI via ffmpeg raw output (fast — no
Python frame decode) and checks two invariants:

1. **No residual subtitles**: where SRC had a glyph mask, CLEAN must not still
   have one. A residual frame is a source-text frame whose CLEAN ROI still has a
   mask fraction above ``residual_th``.
2. **No unintended edits on subtitle-free frames**: on frames where SRC had no
   mask, |SRC-CLEAN| mean must stay within ``notouch_th`` (encoding noise only).

Exits non-zero if any frame fails the gate so it can be used as an automated
pass/fail check in the slice orchestrator.

Usage:
  .venv/Scripts/python.exe verify_dehardsub.py \
      --src downloads/batch/bilibili_BV1fRXxY1Eke/media/source.mp4 \
      --clean downloads/batch/bilibili_BV1fRXxY1Eke/media/dehardsub/clean.mp4 \
      --box 111,1382,861,65 --out verify_report.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np


def _ffmpeg_bin(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe:
            return exe
    except Exception:
        pass
    import shutil

    return shutil.which("ffmpeg") or "ffmpeg"


def build_mask(roi: np.ndarray) -> np.ndarray | None:
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    s = hsv[..., 1].astype(np.int16)
    v = hsv[..., 2].astype(np.int16)
    bgm = (s > 60) & (v > 200)
    if float(bgm.mean()) < 0.20:
        return None  # no dominant flat color → not our ROI, skip
    bg = np.median(roi[bgm].reshape(-1, 3), axis=0).astype(np.float32)
    d = np.linalg.norm(roi.astype(np.float32) - bg.reshape(1, 1, 3), axis=2)
    return d > 40


def _stream(ff: str, path: str, cx: int, cy: int, cw: int, ch: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            ff,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            path,
            "-vf",
            f"crop={cw}:{ch}:{cx}:{cy}",
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "-",
        ],
        stdout=subprocess.PIPE,
        bufsize=-1,
    )


def verify(
    src: str,
    clean: str,
    box: dict[str, int],
    *,
    pad: int = 25,
    residual_th: float = 0.03,
    notouch_th: float = 15.0,
) -> dict:
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    cx, cy, cw, ch = x, y - pad, w, h + 2 * pad
    fs = cw * ch * 3
    ff = _ffmpeg_bin()
    ps = _stream(ff, src, cx, cy, cw, ch)
    pc = _stream(ff, clean, cx, cy, cw, ch)
    assert ps.stdout and pc.stdout
    n = 0
    text_frames: list[int] = []
    bad: list[int] = []
    notouch_worst = 0.0
    notouch_n = 0
    try:
        while True:
            rs = ps.stdout.read(fs)
            rc = pc.stdout.read(fs)
            if len(rs) < fs or len(rc) < fs:
                break
            n += 1
            sroi = np.frombuffer(rs, np.uint8).reshape(ch, cw, 3)
            croi = np.frombuffer(rc, np.uint8).reshape(ch, cw, 3)
            ms = build_mask(sroi)
            if ms is None:
                continue
            frac_s = float(ms.mean())
            if frac_s > 0.005:
                text_frames.append(n)
                mc = build_mask(croi)
                if mc is not None and float(mc.mean()) > residual_th:
                    bad.append(n)
            else:
                d = float(np.abs(sroi.astype(int) - croi.astype(int)).mean())
                notouch_worst = max(notouch_worst, d)
                notouch_n += 1
            if n % 5000 == 0:
                print(f"[verify] scanned {n} frames...", flush=True)
    finally:
        ps.kill()
        pc.kill()
    report = {
        "frames_scanned": n,
        "source_text_frames": len(text_frames),
        "residual_frames": bad,
        "residual_count": len(bad),
        "no_text_frame_count": notouch_n,
        "no_text_max_absdiff": round(notouch_worst, 3),
        "notouch_threshold": notouch_th,
        "passed": len(bad) == 0 and notouch_worst <= notouch_th,
    }
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Dehardsub self-check gate")
    ap.add_argument("--src", required=True, help="source mp4")
    ap.add_argument("--clean", required=True, help="dehardsub output mp4")
    ap.add_argument("--box", required=True, help="x,y,w,h of subtitle box")
    ap.add_argument("--pad", type=int, default=25)
    ap.add_argument("--residual-th", type=float, default=0.03)
    ap.add_argument("--notouch-th", type=float, default=15.0)
    ap.add_argument("--ffmpeg")
    ap.add_argument("--out", help="write JSON report to this path")
    args = ap.parse_args()
    box = {k: int(v) for k, v in zip(("x", "y", "w", "h"), args.box.split(","))}
    rep = verify(
        args.src,
        args.clean,
        box,
        pad=args.pad,
        residual_th=args.residual_th,
        notouch_th=args.notouch_th,
    )
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
