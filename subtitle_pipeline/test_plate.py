"""CPU-only tests for the median-stack clean-plate (no CUDA model needed).

Covers ``build_dialogue_clean_plate`` and ``encode_composite_plate`` plus the
shared ``_encode_rawvideo_pipe`` helper used by both plate and LaMa encoders.
The LaMa path is exercised on a tiny synthetic clip so it stays fast.
"""
import subprocess
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent
CLIP = ROOT / "test_bili" / "clip15.mp4"
DLG = {"x": 63, "y": 1713, "w": 830, "h": 90}


def _have_clip() -> bool:
    return CLIP.is_file()


def _make_synthetic_mp4(path: Path, *, n: int = 30, fw: int = 64, fh: int = 64) -> tuple[dict, int, int, int]:
    """A static colored bar with a per-frame moving white caption (gets medianed out)."""
    from fetch_media import find_ffmpeg
    from sttn_inpaint import _finalize_writing_mp4

    bx, by, bw, bh = 0, 40, fw, 24
    ffmpeg = find_ffmpeg()
    writing = path.with_name(path.stem + "_writing.mp4")
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{fw}x{fh}", "-r", "10",
        "-i", "pipe:0", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-shortest", str(writing),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    rng = np.random.default_rng(0)
    bar = np.array([120, 80, 40], dtype=np.uint8)
    for _ in range(n):
        f = np.full((fh, fw, 3), 30, dtype=np.uint8)
        f[by:by + bh] = bar
        tx = int(rng.integers(4, fw - 20))
        f[by + 6:by + 18, tx:tx + 16] = 245  # moving white caption (>=185, low sat)
        proc.stdin.write(f.tobytes())
    proc.stdin.close()
    proc.communicate(timeout=60)
    assert writing.is_file() and writing.stat().st_size > 500
    if writing.resolve() != path.resolve():
        _finalize_writing_mp4(writing, path)
    return {"x": bx, "y": by, "w": bw, "h": bh}, fw, fh, n


@pytest.mark.skipif(not _have_clip(), reason="test_bili/clip15.mp4 not present")
def test_build_dialogue_clean_plate_real_clip():
    from visual_cleanup import build_dialogue_clean_plate

    bp = build_dialogue_clean_plate(CLIP, DLG)
    assert bp is not None
    plate, stats = bp
    assert isinstance(plate, np.ndarray)
    assert plate.shape[:2] == (DLG["h"], DLG["w"])
    assert 0.0 <= stats["temporal_l1"] <= 10.0
    assert 0.0 < stats["text_frac"] < 1.0
    assert plate.dtype == np.uint8


@pytest.mark.skipif(not _have_clip(), reason="test_bili/clip15.mp4 not present")
def test_encode_composite_plate_real_clip(tmp_path):
    import cv2

    from visual_cleanup import build_dialogue_clean_plate, encode_composite_plate

    bp = build_dialogue_clean_plate(CLIP, DLG)
    assert bp is not None
    out = tmp_path / "plate_out.mp4"
    stats = encode_composite_plate(CLIP, out, [(DLG, bp[0])])
    assert stats is not None
    assert stats["engine"] == "plate"
    assert stats["boxes"] == 1
    assert out.is_file() and out.stat().st_size > 1000

    cap = cv2.VideoCapture(str(CLIP))
    n_src = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    cap = cv2.VideoCapture(str(out))
    n_out = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    assert n_out == n_src


def test_clean_plate_removes_moving_caption(tmp_path):
    import cv2

    from visual_cleanup import build_dialogue_clean_plate, encode_composite_plate

    box, _fw, _fh, _n = _make_synthetic_mp4(tmp_path / "syn.mp4")
    src = tmp_path / "syn.mp4"
    bp = build_dialogue_clean_plate(src, box)
    assert bp is not None, "plate should accept a static colored bar"
    plate, _stats = bp
    # Bar preserved (colored); moving white caption removed by median stack.
    assert int(plate.max()) < 240, "white caption not removed by median stack"

    out = tmp_path / "syn_plate.mp4"
    used = encode_composite_plate(src, out, [(box, plate)])
    assert used is not None

    cap = cv2.VideoCapture(str(out))
    white = 0
    tot = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        roi = fr[box["y"]:box["y"] + box["h"], box["x"]:box["x"] + box["w"]]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        white += int((gray >= 240).sum())
        tot += int(gray.size)
    cap.release()
    frac = white / max(1, tot)
    assert frac < 0.05, f"residual white caption too high: {frac:.3f}"


def test_encode_lama_roundtrip_synthetic(tmp_path):
    """LaMa encoder (via shared helper) keeps frame count and produces a valid file."""
    import cv2

    from visual_cleanup import encode_lama_boxes

    box, _fw, _fh, n = _make_synthetic_mp4(tmp_path / "lama.mp4")
    src = tmp_path / "lama.mp4"
    out = tmp_path / "lama_rt.mp4"
    stats = encode_lama_boxes(src, out, [box], mask_mode="residual")
    assert stats is not None
    assert stats["engine"] == "lama"
    assert out.is_file() and out.stat().st_size > 1000

    cap = cv2.VideoCapture(str(src))
    n_src = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    cap = cv2.VideoCapture(str(out))
    n_out = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    assert n_out == n_src == n
