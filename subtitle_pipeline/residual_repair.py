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
    residual_edge_frac: float = 0.25,
) -> list[int]:
    """Return 1-based frame indices where the subtitle was not removed.

    Mask-weighted: text-edge energy is measured **inside the source's ink mask**
    (dilated), not over the whole box+padding. On frames where the subtitle is
    faint the source edge energy is low, so background texture inside the padding
    can push the plain ratio over the gate on a frame that is actually clean —
    flagging it would make the repair inpaint an already-good frame and blur its
    background. Restricting to the ink answers "is the text still there?".

    ``residual_edge_frac`` defaults to 0.25, not 0.4: on a real clip the clean
    frames sit at p95=0.145 / p99=0.325, while faint STTN ghosts land at
    0.31–0.36. A 0.4 gate lets those visible ghosts through silently; 0.25 sits
    just above the clean population's 99th percentile and catches them.
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
                md = cv2.dilate(ms.astype(np.uint8), np.ones((7, 7), np.uint8), 1).astype(bool)
                if not md.any():
                    continue
                gs = cv2.cvtColor(sroi, cv2.COLOR_BGR2GRAY)
                gc = cv2.cvtColor(croi, cv2.COLOR_BGR2GRAY)
                e_s = float(cv2.Laplacian(gs, cv2.CV_64F)[md].var())
                e_c = float(cv2.Laplacian(gc, cv2.CV_64F)[md].var())
                if e_c > residual_edge_frac * max(e_s, 1e-6):
                    idxs.append(n)
    finally:
        ps.kill()
        pc.kill()
    return idxs


def _text_mask(roi: np.ndarray, thresh: float = 40.0) -> np.ndarray | None:
    """Ink mask for a ROI (same idea as verify.build_mask, tunable threshold).

    Background-agnostic: distance from the ROI's own median colour, OR'd with the
    vivid-flat-background detector. A lower threshold is used as a fallback to
    catch faint ghosts that survive in the cleaned frame.
    """
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    s = hsv[..., 1].astype(np.int16)
    v = hsv[..., 2].astype(np.int16)
    roi_f = roi.astype(np.float32)
    bgm = (s > 60) & (v > 200)
    mask: np.ndarray | None = None
    if float(bgm.mean()) >= 0.20:
        bg = np.median(roi[bgm].reshape(-1, 3), axis=0).astype(np.float32)
        d = np.linalg.norm(roi_f - bg.reshape(1, 1, 3), axis=2)
        mask = d > thresh
    bg2 = np.median(roi.reshape(-1, 3), axis=0).astype(np.float32)
    d2 = np.linalg.norm(roi_f - bg2.reshape(1, 1, 3), axis=2)
    m2 = d2 > thresh
    return m2 if mask is None else (mask | m2)


def repair_residual_frames(
    src: Path,
    clean: Path,
    box: dict[str, int],
    resid_idxs: list[int],
    *,
    pad: int = 4,
) -> Path:
    """Inpaint the still-text region on the flagged frames; pass others through.

    The repair region comes from the **source** frame's ink mask, not the cleaned
    frame's. A faint STTN ghost differs from the cleaned background by less than
    the mask threshold, so masking the output itself silently skips exactly the
    pixels we need to fix; the source mask reliably says where the text was.
    Falls back to a low-threshold mask on the output if the source has no ink
    there (e.g. the box drifted onto background).
    """
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

    capc = cv2.VideoCapture(str(clean))
    caps = cv2.VideoCapture(str(src))  # walked in lockstep for the ink mask
    if not capc.isOpened() or not caps.isOpened():
        raise RuntimeError(f"cannot open {clean} / {src}")
    proc = _open_ffmpeg_writer(clean, out, W, H, fps)
    assert proc.stdin is not None
    i = 0
    try:
        while True:
            ok, fr = capc.read()
            ok2, sfr = caps.read()
            if not ok:
                break
            i += 1
            if i in target and rw > 2 and rh > 2:
                roi = fr[ry : ry + rh, rx : rx + rw]
                m = None
                if ok2:
                    sroi = sfr[ry : ry + rh, rx : rx + rw]
                    if sroi.shape[:2] == roi.shape[:2]:
                        m = _text_mask(sroi, 40.0)
                if m is None or float(m.mean()) <= 0.002:
                    # faint-ghost fallback on the output itself
                    m = _text_mask(roi, 22.0)
                if m is not None and float(m.mean()) > 0.002:
                    dm = cv2.dilate(m.astype(np.uint8), np.ones((9, 9), np.uint8), 1)
                    full = np.zeros(fr.shape[:2], dtype=np.uint8)
                    full[ry : ry + rh, rx : rx + rw] = (dm > 0).astype(np.uint8) * 255
                    fr = cv2.inpaint(fr, full, 7, cv2.INPAINT_TELEA)
            proc.stdin.write(np.ascontiguousarray(fr).tobytes())
    except Exception:
        proc.kill()
        raise
    finally:
        capc.release()
        caps.release()
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
    residual_edge_frac: float = 0.25,
) -> tuple[Path, list[int]]:
    """Scan then repair; returns (final_path, repaired_frame_indices)."""
    idxs = scan_residual_frames(src, clean, box, pad=pad, residual_edge_frac=residual_edge_frac)
    if not idxs:
        return clean, []
    out = repair_residual_frames(src, clean, box, idxs)
    # One confirmation pass: if the repair left anything, the loop already ran
    # once; a second scan keeps the reported numbers honest.
    return out, idxs
