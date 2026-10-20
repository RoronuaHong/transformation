"""Repair the residual subtitle frames that the self-check flags.

Closing the loop on the self-check gate: scan the dehardsub output for frames
where the subtitle was NOT removed (clean still carries the source's text-edge
energy), then inpaint only those frames' still-text region. Everything else is
left untouched, so we never degrade frames the main pass already fixed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from verify_dehardsub import _edge_energy, _ffmpeg_bin, _stream, build_mask


def scan_residual_frames(
    src: Path,
    clean: Path,
    box: dict[str, int],
    *,
    pad: int = 6,
    residual_edge_frac: float = 0.4,
) -> list[int]:
    """Return 1-based frame indices where the subtitle was not removed.

    Same signal as the self-check gate: a source-text frame whose clean ROI still
    holds ``residual_edge_frac`` of the source's text-edge energy.
    """
    x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    cy = max(0, y - pad)
    ch = h + 2 * pad
    fs = w * ch * 3
    ff = _ffmpeg_bin()
    ps = _stream(ff, str(src), x, cy, w, ch)
    pc = _stream(ff, str(clean), x, cy, w, ch)
    idxs: list[int] = []
    n = 0
    try:
        assert ps.stdout is not None and pc.stdout is not None
        while True:
            rs = ps.stdout.read(fs)
            rc = pc.stdout.read(fs)
            if len(rs) < fs or len(rc) < fs:
                break
            n += 1
            sroi = np.frombuffer(rs, np.uint8).reshape(ch, w, 3)
            croi = np.frombuffer(rc, np.uint8).reshape(ch, w, 3)
            ms = build_mask(sroi)
            if ms is None:
                continue
            if float(ms.mean()) > 0.005:
                e_s = _edge_energy(sroi)
                e_c = _edge_energy(croi)
                if e_c > residual_edge_frac * max(e_s, 1e-6):
                    idxs.append(n)
    finally:
        ps.kill()
        pc.kill()
    return idxs


def repair_residual_frames(
    src: Path,
    clean: Path,
    box: dict[str, int],
    resid_idxs: list[int],
    *,
    pad: int = 4,
) -> Path:
    """Inpaint the still-text region on the flagged frames; pass others through."""
    from sttn_inpaint import _open_ffmpeg_writer
    from visual_cleanup import _enforce_frame_count

    src = Path(src)
    clean = Path(clean)
    x, y, w, h = int(box["x"]), int(box["y"]), int(box["w"]), int(box["h"])
    target = set(int(i) for i in resid_idxs)
    if not target:
        return clean

    cap = cv2.VideoCapture(str(clean))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {clean}")
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    cap.release()

    rx, ry = max(0, x - pad), max(0, y - pad)
    rw = min(w + 2 * pad, W - rx)
    rh = min(h + 2 * pad, H - ry)
    out = clean.with_name(clean.stem + "_residfix.mp4")

    cap = cv2.VideoCapture(str(clean))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {clean}")
    proc = _open_ffmpeg_writer(clean, out, W, H, fps)
    assert proc.stdin is not None
    i = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            i += 1
            if i in target and rw > 2 and rh > 2:
                roi = fr[ry : ry + rh, rx : rx + rw]
                m = build_mask(roi)
                if m is not None and float(m.mean()) > 0.002:
                    dm = cv2.dilate(m.astype(np.uint8), np.ones((7, 7), np.uint8), 1)
                    full = np.zeros(fr.shape[:2], dtype=np.uint8)
                    full[ry : ry + rh, rx : rx + rw] = (dm > 0).astype(np.uint8) * 255
                    fr = cv2.inpaint(fr, full, 5, cv2.INPAINT_TELEA)
            proc.stdin.write(np.ascontiguousarray(fr).tobytes())
    except Exception:
        proc.kill()
        raise
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    proc.communicate(timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(f"residual repair failed ({proc.returncode})")
    # Keep the result frame-exact vs the original source.
    _enforce_frame_count(src, out)
    return out


def scan_and_repair(
    src: Path,
    clean: Path,
    box: dict[str, int],
    *,
    pad: int = 6,
    residual_edge_frac: float = 0.4,
) -> tuple[Path, list[int]]:
    """Scan then repair; returns (final_path, repaired_frame_indices)."""
    idxs = scan_residual_frames(src, clean, box, pad=pad, residual_edge_frac=residual_edge_frac)
    if not idxs:
        return clean, []
    return repair_residual_frames(src, clean, box, idxs), idxs
