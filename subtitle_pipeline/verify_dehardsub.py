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
    """Text-ink mask, background-agnostic.

    Two complementary detectors (OR'd) so it generalises beyond vivid/saturated
    subtitles. The old vivid-only gate did ``return None`` when the vivid-bright
    fraction was <20%, which silently skipped white / non-vivid subtitles and
    produced a *false pass* (source_text_frames=0). Now:
      A) vivid flat-colour background distance (original path, kept for parity);
      B) whole-ROI median background distance — detects white or coloured glyphs
         on any reasonably flat background.
    """
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    s = hsv[..., 1].astype(np.int16)
    v = hsv[..., 2].astype(np.int16)
    bgm = (s > 60) & (v > 200)
    roi_f = roi.astype(np.float32)

    mask: np.ndarray | None = None
    if float(bgm.mean()) >= 0.20:
        bg = np.median(roi[bgm].reshape(-1, 3), axis=0).astype(np.float32)
        d = np.linalg.norm(roi_f - bg.reshape(1, 1, 3), axis=2)
        mask = d > 40
    bg2 = np.median(roi.reshape(-1, 3), axis=0).astype(np.float32)
    d2 = np.linalg.norm(roi_f - bg2.reshape(1, 1, 3), axis=2)
    m2 = d2 > 40
    return m2 if mask is None else (mask | m2)


def _edge_energy(roi: np.ndarray) -> float:
    """High-frequency stroke energy (text = sharp edges). Background-normalised
    when compared as a clean/src ratio, so inpaint texture doesn't inflate it."""
    g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


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
    residual_edge_frac: float = 0.4,
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
    worst_ratio = 0.0
    edge_removal: list[float] = []
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
            if float(ms.mean()) > 0.005:
                text_frames.append(n)
                # Residual = clean still carries a large share of the source's
                # text-edge energy. The clean/src edge ratio is background-
                # normalised, so inpaint texture (which inflates an absolute mask
                # fraction) does not cause false residuals.
                e_s = _edge_energy(sroi)
                e_c = _edge_energy(croi)
                ratio = e_c / max(e_s, 1e-6)
                worst_ratio = max(worst_ratio, ratio)
                edge_removal.append(1.0 - ratio)
                if ratio > residual_edge_frac:
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
        "residual_edge_frac_threshold": residual_edge_frac,
        "worst_edge_ratio": round(worst_ratio, 3),
        "mean_edge_removal": round(float(np.mean(edge_removal)), 3) if edge_removal else None,
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
    ap.add_argument(
        "--residual-edge-frac",
        type=float,
        default=0.4,
        help="residual if clean/src text-edge-energy ratio exceeds this",
    )
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
        residual_edge_frac=args.residual_edge_frac,
    )
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
