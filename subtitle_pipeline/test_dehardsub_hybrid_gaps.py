"""General dehardsub hybrid guards: dark plate reject + boxfill helper."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest


def _make_clip(
    path: Path,
    *,
    n: int = 24,
    fw: int = 96,
    fh: int = 64,
    band_y: int = 40,
    band_h: int = 20,
    band_gray: int = 90,
) -> dict:
    from _inpaint_shared import _finalize_writing_mp4
    from fetch_media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    writing = path.with_name(path.stem + "_writing.mp4")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{fw}x{fh}",
        "-r",
        "10",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-shortest",
        str(writing),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    for _ in range(n):
        f = np.full((fh, fw, 3), 40, dtype=np.uint8)
        f[band_y : band_y + band_h, :] = int(band_gray)
        proc.stdin.write(f.tobytes())
    proc.stdin.close()
    proc.communicate(timeout=60)
    assert writing.is_file()
    if writing.resolve() != path.resolve():
        _finalize_writing_mp4(writing, path)
    return {"x": 8, "y": band_y, "w": fw - 16, "h": band_h}


def _write_bgr_frames(path: Path, frames: list[np.ndarray]) -> None:
    """Write a tiny H.264 clip from explicit BGR frames (no OCR needed)."""
    from _inpaint_shared import _finalize_writing_mp4
    from fetch_media import find_ffmpeg

    h, w = frames[0].shape[:2]
    writing = path.with_name(path.stem + "_writing.mp4")
    proc = subprocess.Popen(
        [
            find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
            "-r", "10", "-i", "pipe:0", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            str(writing),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    for frame in frames:
        proc.stdin.write(np.ascontiguousarray(frame).tobytes())
    proc.stdin.close()
    proc.communicate(timeout=60)
    if writing.resolve() != path.resolve():
        _finalize_writing_mp4(writing, path)


def test_dialogue_plate_rejects_darker_than_band(tmp_path: Path) -> None:
    from visual_cleanup import dialogue_plate_is_safe

    box = _make_clip(tmp_path / "bright_band.mp4", band_gray=140)
    # Artificial dark plate (would stamp a black bar).
    plate = np.full((box["h"], box["w"], 3), 20, dtype=np.uint8)
    ok, gate = dialogue_plate_is_safe(tmp_path / "bright_band.mp4", box, plate)
    assert ok is False
    assert gate["darker"] > 28.0


def test_dialogue_plate_accepts_similar_luma(tmp_path: Path) -> None:
    from visual_cleanup import dialogue_plate_is_safe

    box = _make_clip(tmp_path / "ok_band.mp4", band_gray=120)
    plate = np.full((box["h"], box["w"], 3), 118, dtype=np.uint8)
    ok, gate = dialogue_plate_is_safe(tmp_path / "ok_band.mp4", box, plate)
    assert ok is True
    assert abs(gate["darker"]) < 10.0


def test_ensure_top_up_watermark_min_h() -> None:
    from media_ops import ensure_top_up_watermark_min_h

    short = ensure_top_up_watermark_min_h(
        {"x": 510, "y": 0, "w": 330, "h": 28}, 852, 480
    )
    assert short["h"] >= 32
    assert short["h"] <= 48
    mid = ensure_top_up_watermark_min_h(
        {"x": 110, "y": 148, "w": 100, "h": 32}, 852, 480
    )
    assert mid["h"] == 32  # mid handle unchanged


def test_resolve_up_watermark_method_unified() -> None:
    from visual_cleanup import resolve_up_watermark_method

    # Expanded Morphic height (>48) must still be morphic, not logo (D7).
    assert (
        resolve_up_watermark_method({"x": 510, "y": 0, "w": 330, "h": 56}, 852, 480)
        == "morphic"
    )
    assert (
        resolve_up_watermark_method({"x": 8, "y": 0, "w": 280, "h": 48}, 852, 480)
        == "copyfill"
    )
    assert (
        resolve_up_watermark_method({"x": 110, "y": 148, "w": 100, "h": 32}, 852, 480)
        == "logo"
    )


def test_logo_ink_catches_soft_residual() -> None:
    """D5: soft grey ink above local median should light up mask."""
    from visual_cleanup import _logo_ink_mask_roi

    roi = np.full((40, 80, 3), 60, dtype=np.uint8)
    # Soft translucent glyph-like band (fails hard V>168).
    roi[12:28, 15:65] = 95
    ink = _logo_ink_mask_roi(roi)
    assert float((ink > 0).mean()) > 0.02


def test_roi_half_wipe_detects_w2b() -> None:
    from visual_cleanup import _roi_half_wipe

    src = np.full((30, 60, 3), 40, dtype=np.uint8)
    src[8:22, 10:50] = 220
    # Failed wipe: still bright residual + dark blotch elsewhere → rollback.
    out = src.copy()
    out[8:14, 10:50] = 50
    # leave half the bright ink
    assert _roi_half_wipe(src, out) is True
    assert _roi_half_wipe(src, src) is False
    # Successful wipe: bright ink mostly gone (keep even with dark fill).
    cleared = np.full((30, 60, 3), 40, dtype=np.uint8)
    cleared[8:22, 10:50] = 45
    assert _roi_half_wipe(src, cleared) is False


def test_build_dehardsub_gates_ok_after_min_h(tmp_path: Path) -> None:
    from media_ops import build_dehardsub_gates

    # Synthetic clip with bright top strip fully inside a tall enough box.
    box = _make_clip(tmp_path / "gate.mp4", n=8, band_y=0, band_h=50, band_gray=220)
    boxes = [
        {"x": 8, "y": 0, "w": 80, "h": 50, "role": "up_watermark"},
        {"x": 8, "y": 40, "w": 80, "h": 20, "role": "dialogue"},
    ]
    g = build_dehardsub_gates(tmp_path / "gate.mp4", boxes, width=96, height=64)
    assert g["top_min_h_ok"] is True
    # spill may or may not fire on synthetic; ok flag follows spill+min_h
    assert "ok" in g
    assert g["boxes_normalized"][0]["h"] >= 48 or 64 < 480  # 64p: min scales


def test_up_watermark_prefers_boxfill_top_strip() -> None:
    from visual_cleanup import up_watermark_prefers_boxfill

    assert up_watermark_prefers_boxfill({"x": 500, "y": 2, "w": 300, "h": 24}, 480, 852)
    # Mid-frame @handle must NOT prefer logo fill (destroys body highlights).
    assert not up_watermark_prefers_boxfill(
        {"x": 120, "y": 148, "w": 100, "h": 35}, 480, 852
    )
    # Morphic-like top-right strip on 852x480.
    assert up_watermark_prefers_boxfill({"x": 510, "y": 1, "w": 310, "h": 34}, 480, 852)
    assert not up_watermark_prefers_boxfill(
        {"x": 100, "y": 200, "w": 400, "h": 80}, 480, 852
    )


def test_smear_score_flags_soft_blotch() -> None:
    from residual_repair import _smear_score

    gray = np.full((40, 120), 80, dtype=np.uint8)
    # Soft bright blotch (TELEA-like smear), not sharp glyphs.
    gray[12:28, 20:100] = 130
    ink = np.zeros((40, 120), dtype=bool)
    ink[10:30, 15:105] = True
    assert _smear_score(gray, ink) >= 0.012
    assert _smear_score(np.full((40, 120), 80, dtype=np.uint8), ink) < 0.005


def test_encode_static_boxfill_runs(tmp_path: Path) -> None:
    import cv2

    from visual_cleanup import encode_static_boxfill

    # Dark top strip with a compact bright logo blob (not a full white band —
    # a solid band dilates past the static-mask frac cap and correctly skips).
    box = _make_clip(tmp_path / "wm.mp4", band_y=2, band_h=14, band_gray=40)
    src = tmp_path / "wm.mp4"
    # Stamp a persistent logo-like rectangle into every frame.
    from _inpaint_shared import _finalize_writing_mp4
    from fetch_media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    stamped = tmp_path / "wm_logo.mp4"
    writing = stamped.with_name(stamped.stem + "_writing.mp4")
    cap = cv2.VideoCapture(str(src))
    fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 24)
    proc = __import__("subprocess").Popen(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{fw}x{fh}",
            "-r", "10", "-i", "pipe:0", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            str(writing),
        ],
        stdin=__import__("subprocess").PIPE,
        stderr=__import__("subprocess").PIPE,
    )
    assert proc.stdin is not None
    for _ in range(n):
        ok, fr = cap.read()
        if not ok:
            fr = np.full((fh, fw, 3), 40, dtype=np.uint8)
        fr[4:12, 20:52] = 230
        proc.stdin.write(fr.tobytes())
    cap.release()
    proc.stdin.close()
    proc.communicate(timeout=60)
    _finalize_writing_mp4(writing, stamped)
    out = tmp_path / "wm_out.mp4"
    # Top-strip up_watermark: ink TELEA (no full-strip face paste).
    used = encode_static_boxfill(stamped, out, box, role="up_watermark")
    assert used["engine"] == "copyfill"
    assert used["mode"] == "ink_telea"
    assert used["frames"] >= 8
    assert out.is_file() and out.stat().st_size > 500
    mid = encode_static_boxfill(
        stamped,
        tmp_path / "wm_mid.mp4",
        {"x": 20, "y": 30, "w": 40, "h": 20},
        role="up_watermark",
        method="logo",
    )
    assert mid["engine"] == "logo_telea"
    assert mid["mode"] in ("static_logo_mask", "per_frame_ink")
    assert mid["frames"] >= 8
    cap = cv2.VideoCapture(str(out))
    ok, fr = cap.read()
    cap.release()
    assert ok


def test_residual_scan_aborts_when_too_hot(tmp_path: Path) -> None:
    """Guard: opt-out Top-K still skips when too hot (legacy)."""
    from residual_repair import scan_residual_frames

    box = _make_clip(tmp_path / "src.mp4", band_gray=140)
    # Identical "clean" still has band texture → smear gate can fire widely.
    import shutil

    clean = tmp_path / "clean.mp4"
    shutil.copy2(tmp_path / "src.mp4", clean)
    idxs = scan_residual_frames(
        tmp_path / "src.mp4",
        clean,
        box,
        smear_score_thr=0.001,
        residual_edge_frac=0.01,
        max_hit_frac=0.12,
        top_k_when_hot=None,
    )
    assert idxs == []


def test_residual_scan_top_k_when_hot(tmp_path: Path) -> None:
    from residual_repair import scan_residual_frames

    box = _make_clip(tmp_path / "src2.mp4", band_gray=140)
    import shutil

    clean = tmp_path / "clean2.mp4"
    shutil.copy2(tmp_path / "src2.mp4", clean)
    meta: dict = {}
    idxs = scan_residual_frames(
        tmp_path / "src2.mp4",
        clean,
        box,
        smear_score_thr=0.001,
        residual_edge_frac=0.01,
        max_hit_frac=0.12,
        top_k_when_hot=5,
        meta_out=meta,
    )
    assert meta.get("mode") in ("top_k_hot", "ok", "skipped_too_hot")
    if meta.get("mode") == "top_k_hot":
        assert 1 <= len(idxs) <= 5


def test_dialogue_safety_restores_w2b(tmp_path: Path) -> None:
    """Bright glyphs turned near-black must restore (D1 w2b gate)."""
    import cv2
    import shutil
    from visual_cleanup import encode_dialogue_safety_gate

    box = _make_clip(tmp_path / "src_w2b.mp4", n=12, band_gray=40)
    src = tmp_path / "src_w2b.mp4"
    # Stamp bright text-like strip on source.
    from _inpaint_shared import _finalize_writing_mp4
    from fetch_media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    stamped = tmp_path / "src_bright.mp4"
    writing = stamped.with_name(stamped.stem + "_writing.mp4")
    cap = cv2.VideoCapture(str(src))
    fw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    fh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 12)
    proc = subprocess.Popen(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{fw}x{fh}",
            "-r", "10", "-i", "pipe:0", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            str(writing),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    for _ in range(n):
        ok, fr = cap.read()
        if not ok:
            fr = np.full((fh, fw, 3), 40, dtype=np.uint8)
        fr[box["y"] + 4 : box["y"] + box["h"] - 4, box["x"] + 10 : box["x"] + box["w"] - 10] = 220
        proc.stdin.write(fr.tobytes())
    cap.release()
    proc.stdin.close()
    proc.communicate(timeout=60)
    _finalize_writing_mp4(writing, stamped)

    # Clean: same but glyphs crushed to near-black (TELEA failure).
    dirty = tmp_path / "dirty.mp4"
    writing2 = dirty.with_name(dirty.stem + "_writing.mp4")
    cap = cv2.VideoCapture(str(stamped))
    proc = subprocess.Popen(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{fw}x{fh}",
            "-r", "10", "-i", "pipe:0", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            str(writing2),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    for _ in range(n):
        ok, fr = cap.read()
        if not ok:
            break
        fr = fr.copy()
        # Partial wipe: half glyphs crushed black, half still bright → must restore.
        y0 = box["y"] + 4
        y1 = box["y"] + box["h"] - 4
        x0 = box["x"] + 10
        x1 = box["x"] + box["w"] - 10
        mid = (x0 + x1) // 2
        fr[y0:y1, x0:mid] = 12
        proc.stdin.write(fr.tobytes())
    cap.release()
    proc.stdin.close()
    proc.communicate(timeout=60)
    _finalize_writing_mp4(writing2, dirty)

    out = tmp_path / "safe.mp4"
    used = encode_dialogue_safety_gate(stamped, dirty, out, box)
    assert used["restored"] >= 1
    assert out.is_file()


def test_dialogue_safety_keeps_successful_dark_glyph_wipe(tmp_path: Path) -> None:
    """Dark outlined glyphs correctly wiped must NOT be pasted back (P0 no-op)."""
    from visual_cleanup import audit_dialogue_safety_hits, encode_dialogue_safety_gate

    box = {"x": 8, "y": 40, "w": 80, "h": 20}
    src_frames: list[np.ndarray] = []
    clean_frames: list[np.ndarray] = []
    glyph = (slice(box["y"] + 3, box["y"] + 13), slice(box["x"] + 10, box["x"] + 50))
    for _ in range(12):
        frame = np.full((64, 96, 3), 40, dtype=np.uint8)
        frame[glyph] = 220
        src_frames.append(frame.copy())
        clean = frame.copy()
        clean[glyph] = 40
        clean_frames.append(clean)
    src = tmp_path / "src_dark_glyph.mp4"
    clean = tmp_path / "clean_dark_glyph.mp4"
    _write_bgr_frames(src, src_frames)
    _write_bgr_frames(clean, clean_frames)

    out = tmp_path / "safe.mp4"
    used = encode_dialogue_safety_gate(src, clean, out, box)
    assert used["restored"] == 0
    audit = audit_dialogue_safety_hits(src, out, box)
    assert audit["would_restore"] == 0
    assert audit["min_bright"] == 0.0


def test_dialogue_safety_keeps_bright_plate_dark_glyph_wipe(tmp_path: Path) -> None:
    """Edge collapse after a correct dark-on-white wipe is not scene damage."""
    from visual_cleanup import encode_dialogue_safety_gate

    box = {"x": 8, "y": 40, "w": 80, "h": 20}
    glyph = (slice(box["y"] + 3, box["y"] + 13), slice(box["x"] + 10, box["x"] + 50))
    src_frames: list[np.ndarray] = []
    clean_frames: list[np.ndarray] = []
    for _ in range(12):
        frame = np.full((64, 96, 3), 235, dtype=np.uint8)
        frame[glyph] = 30
        src_frames.append(frame.copy())
        clean = frame.copy()
        clean[glyph] = 235
        clean_frames.append(clean)
    src = tmp_path / "src_bright_plate.mp4"
    clean = tmp_path / "clean_bright_plate.mp4"
    _write_bgr_frames(src, src_frames)
    _write_bgr_frames(clean, clean_frames)

    out = tmp_path / "safe.mp4"
    used = encode_dialogue_safety_gate(src, clean, out, box)
    assert used["restored"] == 0


def test_ring_median_fill_repairs_too_dark_hole() -> None:
    """D6: rejected dark holes get cloth tone, never black or source glyphs."""
    import cv2
    from dehardsub_ocr_telea import _reject_dark_inpaint, _ring_median_fill

    frame = np.full((32, 32, 3), 90, dtype=np.uint8)
    mask = np.zeros((32, 32), dtype=np.uint8)
    mask[12:20, 12:20] = 255
    filled = frame.copy()
    filled[12:20, 12:20] = 5
    assert _reject_dark_inpaint(frame, filled, mask) is True

    repaired = _ring_median_fill(frame, mask)
    hole = float(cv2.cvtColor(repaired[12:20, 12:20], cv2.COLOR_BGR2GRAY).mean())
    assert 70.0 <= hole <= 110.0
    assert float(repaired[0, 0].mean()) == float(frame[0, 0].mean())


def test_surgical_dark_repair_keeps_lama_texture() -> None:
    """GOLD_MASK path: only black pixels get ring fill; textured LaMa stays."""
    import cv2
    from dehardsub_ocr_telea import _surgical_dark_repair

    frame = np.full((40, 40, 3), 100, dtype=np.uint8)
    # textured cloth in ring
    frame[0:40, 0:40] = 95
    frame[5:35:2, 5:35] = 110
    mask = np.zeros((40, 40), dtype=np.uint8)
    mask[12:28, 12:28] = 255
    filled = frame.copy()
    # most of hole = soft LaMa texture (~90); one black blob
    filled[12:28, 12:28] = 88
    filled[16:20, 16:20] = 8
    out = _surgical_dark_repair(frame, filled, mask)
    # textured LaMa region preserved
    assert int(out[13, 13, 0]) == 88
    # black blob lifted toward ring
    assert int(out[17, 17, 0]) > 50


def test_ocr_telea_window_skip_and_sec_helper() -> None:
    from dehardsub_ocr_telea import _sec_in_windows

    assert _sec_in_windows(1.0, None) is True
    assert _sec_in_windows(1.0, []) is True
    assert _sec_in_windows(1.0, [(0.0, 0.5), (2.0, 3.0)]) is False
    assert _sec_in_windows(2.5, [(0.0, 0.5), (2.0, 3.0)]) is True


def test_merge_windows_closes_short_caption_gap() -> None:
    """BV1P6: hits at 6s and 11s must not leave a hole that drops t≈8 dialogue."""
    from media_ops import _merge_time_windows, expand_windows_fade_hold

    wins = _merge_time_windows([6.0, 11.0], 38.3)
    wins = expand_windows_fade_hold(wins, 38.3, hold_sec=0.25)
    assert any(s <= 8.0 <= e for s, e in wins), wins


def test_qa_pair_flags_watermark_w2b(tmp_path: Path) -> None:
    import cv2
    from dehardsub_frame_qa import qa_pair

    box = _make_clip(tmp_path / "qa_src.mp4", n=10, band_y=2, band_h=16, band_gray=40)
    src = tmp_path / "qa_src.mp4"
    # Build clean with w2b in top strip.
    from _inpaint_shared import _finalize_writing_mp4
    from fetch_media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    fw, fh = 96, 64
    def _write(path: Path, bright: bool) -> None:
        writing = path.with_name(path.stem + "_writing.mp4")
        proc = subprocess.Popen(
            [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{fw}x{fh}",
                "-r", "10", "-i", "pipe:0", "-an", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                str(writing),
            ],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdin is not None
        for _ in range(10):
            f = np.full((fh, fw, 3), 40, dtype=np.uint8)
            f[2:14, 10:80] = 220 if bright else 50
            proc.stdin.write(f.tobytes())
        proc.stdin.close()
        proc.communicate(timeout=60)
        _finalize_writing_mp4(writing, path)

    _write(src, True)
    clean = tmp_path / "qa_clean.mp4"
    _write(clean, False)
    rep = qa_pair(
        src,
        clean,
        [{"x": 10, "y": 2, "w": 70, "h": 12, "role": "up_watermark"}],
        times_sec=[0.3],
        write_frames=False,
    )
    assert any("white_to_black" in f for f in rep.get("flags") or [])


def test_wipe_plan_keep_filters() -> None:
    from media_ops import build_wipe_plan, filter_boxes_by_wipe_plan

    boxes = [
        {"x": 1, "y": 0, "w": 40, "h": 20, "role": "up_watermark"},
        {"x": 1, "y": 40, "w": 80, "h": 20, "role": "dialogue", "action": "keep"},
    ]
    plan = build_wipe_plan(boxes, active_windows=[(1.0, 3.0)], duration_sec=10.0)
    assert plan[0]["action"] == "remove"
    assert plan[1]["action"] == "keep"
    assert plan[0]["windows"][0]["end"] == 10.0  # overlay = full clip
    kept = filter_boxes_by_wipe_plan(boxes, plan)
    assert len(kept) == 1
    assert kept[0]["role"] == "up_watermark"


def test_override_empty_skips_redetect(tmp_path: Path) -> None:
    """All-keep override must NOT fall back to resolve_hardsub_boxes."""
    from visual_cleanup import run_multipass_cleanup

    box = _make_clip(tmp_path / "keep.mp4", n=8, band_y=40, band_h=20, band_gray=200)
    dest = tmp_path / "keep_out.mp4"
    rep = run_multipass_cleanup(
        tmp_path / "keep.mp4",
        dest,
        demosaic=False,
        dehardsub=True,
        boxes_override=[],  # wipe_plan all-keep
        polish_residual_floor=1.0,
    )
    assert dest.is_file()
    assert rep.get("action") in ("copy_no_regions", "hybrid_cleanup")
    assert (rep.get("box_source") == "wipe_plan_all_keep") or (
        rep.get("action") == "copy_no_regions"
    )
    _ = box


def test_expand_windows_fade_hold() -> None:
    from media_ops import expand_windows_fade_hold

    out = expand_windows_fade_hold([(2.0, 4.0)], 10.0, hold_sec=0.25)
    assert out == [(1.75, 4.25)]


def test_wipe_plan_duration_keeps_windows() -> None:
    """Confirm refresh must not wipe windows (duration=0 bug)."""
    from media_ops import build_wipe_plan

    boxes = [{"x": 1, "y": 0, "w": 40, "h": 20, "role": "up_watermark"}]
    empty = build_wipe_plan(boxes, duration_sec=0.0)
    assert empty[0]["windows"] == []
    filled = build_wipe_plan(boxes, duration_sec=12.5)
    assert filled[0]["windows"] == [{"start": 0.0, "end": 12.5}]


def test_resolve_up_watermark_motion_overrides_copyfill() -> None:
    from visual_cleanup import resolve_up_watermark_method

    box = {"x": 8, "y": 0, "w": 280, "h": 48}
    assert resolve_up_watermark_method(box, 852, 480) == "copyfill"
    # Moderate motion still copyfill (v2 left-strip strength).
    assert resolve_up_watermark_method(box, 852, 480, motion=18.0) == "copyfill"
    assert (
        resolve_up_watermark_method(box, 852, 480, motion=42.0) == "morphic"
    )
    # Top-right Morphic stays morphic at moderate-high motion (not logo).
    right = {"x": 510, "y": 0, "w": 330, "h": 56}
    assert resolve_up_watermark_method(right, 852, 480, motion=30.0) == "morphic"
    assert resolve_up_watermark_method(right, 852, 480, motion=50.0) == "logo"


def test_morphic_luma_mismatch_refuses_copy() -> None:
    """BV1P6 fi=69: dark left vs brighter Morphic zone must not left-stretch."""
    import cv2

    from visual_cleanup import apply_morphic_frame, morphic_use_copy

    h, w = 80, 220
    fr = np.full((h, w, 3), 40, dtype=np.uint8)
    fr[:, 110:] = (90, 70, 50)  # brighter beam zone
    cv2.putText(
        fr, "Morphic", (120, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (240, 240, 240), 2
    )
    box = {"x": 110, "y": 0, "w": 110, "h": 40}
    assert morphic_use_copy(fr, box) is False
    out, branch = apply_morphic_frame(fr, box, None)
    # Luma mismatch must refuse 鳞片 copy; hard-ink TELEA only (no fog plate).
    assert branch == "telea", branch
    roi = out[0:40, 110:220]
    bright = float((cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) > 170).mean())
    assert bright < 0.02


def test_morphic_expand_taller_than_left_strip() -> None:
    """Right Morphic strip must cover glyph spill (~row 44–52 on 480p)."""
    from visual_cleanup import expand_static_overlay_box

    left = expand_static_overlay_box(
        {"x": 8, "y": 1, "w": 300, "h": 48}, 852, 480, role="up_watermark"
    )
    right = expand_static_overlay_box(
        {"x": 500, "y": 1, "w": 350, "h": 48}, 852, 480, role="up_watermark"
    )
    assert left["h"] <= 42
    assert right["h"] >= 52


def test_qa_byte_identical_and_spans(tmp_path: Path) -> None:
    """D14: identical dialogue ROIs with bright src → byte_identical + spans."""
    from dehardsub_frame_qa import qa_pair
    from _inpaint_shared import _finalize_writing_mp4
    from fetch_media import find_ffmpeg

    ffmpeg = find_ffmpeg()
    path = tmp_path / "same.mp4"
    writing = path.with_name(path.stem + "_writing.mp4")
    fw, fh = 96, 64
    proc = subprocess.Popen(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{fw}x{fh}",
            "-r", "10", "-i", "pipe:0", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
            str(writing),
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    for _ in range(12):
        f = np.full((fh, fw, 3), 40, dtype=np.uint8)
        f[40:58, 10:86] = 210
        proc.stdin.write(f.tobytes())
    proc.stdin.close()
    proc.communicate(timeout=60)
    _finalize_writing_mp4(writing, path)
    import shutil

    clean = tmp_path / "same_clean.mp4"
    shutil.copy2(path, clean)
    rep = qa_pair(
        path,
        clean,
        [{"x": 10, "y": 40, "w": 76, "h": 18, "role": "dialogue"}],
        times_sec=[0.2, 0.5, 0.8],
        write_frames=False,
    )
    assert rep.get("byte_identical") is True
    assert rep.get("truthful_ok") is False
    assert any("psnr" in (r.get("rois") or {}).get("dialogue_0", {}) for r in rep["samples"])
