"""通用去字幕后处理测试：切镜重复/水印糊斑检测与修复（纯合成，不依赖具体视频）。"""
from __future__ import annotations

import json
import os
import sys

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dehardsub_postfix import (  # noqa: E402
    POSTFIX_ENV, detect_bright_watermark_residual, detect_cut_repeat,
    detect_watermark_smear, postfix_enabled, run_postfix,
    _repair_cut_watermarks_from_donors, _smear_mask_in_box,
)


def _write_video(path, frames, audio=False):
    h, w = frames[0].shape[:2]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 30, (w, h))
    for f in frames:
        vw.write(f)
    vw.release()


def test_postfix_env():
    old = os.environ.get(POSTFIX_ENV)
    os.environ[POSTFIX_ENV] = "0"
    assert postfix_enabled() is False
    os.environ[POSTFIX_ENV] = "1"
    assert postfix_enabled() is True
    if old is None:
        os.environ.pop(POSTFIX_ENV, None)
    else:
        os.environ[POSTFIX_ENV] = old


def test_detect_cut_repeat(tmp_path):
    """构造：10 帧，其中 f5/f6 的 clean 用了 f3 的画面（重复），应检出。"""
    W, H = 160, 90
    frames = []
    for i in range(10):
        img = np.full((H, W, 3), 40 + i * 10, np.uint8)
        cv2.rectangle(img, (10 + i * 5, 10), (60 + i * 5, 50), (200, 200, 200), -1)
        frames.append(img)
    clean = [f.copy() for f in frames]
    clean[5] = frames[3].copy()  # 重复：f5 用了 f3 画面
    clean[6] = frames[4].copy()
    src, cl = tmp_path / "src.avi", tmp_path / "clean.avi"
    _write_video(src, frames)
    _write_video(cl, clean)
    hits = detect_cut_repeat(src, cl)
    assert 5 in hits and 6 in hits, hits


def test_smear_mask_localization():
    """暗场景框内白糊斑应定位到；亮场景不定位。"""
    src = np.full((40, 100), 30, np.uint8)
    src[15:20, 40:60] = 10  # 暗字
    clean = src.copy()
    clean[15:20, 40:60] = 180  # 白糊斑
    m = _smear_mask_in_box(src, clean)
    assert m is not None and int((m > 0).sum()) >= 10
    # 亮场景：源亮占比高 → 不定位
    src_bright = np.full((40, 100), 200, np.uint8)
    assert _smear_mask_in_box(src_bright, clean) is None


def test_detect_watermark_smear(tmp_path):
    """暗水印框内白糊斑帧应检出（源帧框内须有暗字，模拟 @ 水印）。"""
    W, H = 160, 90
    frames = []
    for _ in range(5):
        img = np.full((H, W, 3), 30, np.uint8)
        img[20:26, 100:120] = 5  # 源帧暗字
        frames.append(img)
    clean = [f.copy() for f in frames]
    clean[2][21:23, 105:108] = 190  # 该帧白糊斑（小簇，模拟真实糊斑）
    src, cl = tmp_path / "src.avi", tmp_path / "clean.avi"
    _write_video(src, frames)
    _write_video(cl, clean)
    box = {"x": 95, "y": 15, "w": 35, "h": 20}
    hits = detect_watermark_smear(src, cl, [box])
    assert any(f == 2 for f, _bi in hits), hits


def test_run_postfix_no_hits_copies(tmp_path):
    """无瑕疵且帧数一致时直接复制（保留音轨），不改画面。"""
    W, H = 160, 90
    frames = [np.full((H, W, 3), 50 + i, np.uint8) for i in range(5)]
    src, cl, out = tmp_path / "src.avi", tmp_path / "clean.mp4", tmp_path / "out.mp4"
    _write_video(src, frames)
    _write_video(cl, frames)
    regions = tmp_path / "regions.json"
    regions.write_text(json.dumps({"boxes": [{"x": 0, "y": 0, "w": W, "h": H, "role": "dialogue"}]}), encoding="utf-8")
    stats = run_postfix(src, cl, out, regions)
    assert out.exists()
    assert stats["engine"] == "postfix"
    assert stats["frame_alignment"] == "ok"


def _frame_count(path):
    cap = cv2.VideoCapture(str(path))
    n = 0
    while True:
        ok, _frame = cap.read()
        if not ok:
            break
        n += 1
    cap.release()
    return n


def test_run_postfix_duplicates_short_clean_to_source_count(tmp_path):
    """clean 比 source 短时不得静默少帧：末帧补齐到 source 帧数。"""
    W, H = 160, 90
    frames = [np.full((H, W, 3), 50 + i, np.uint8) for i in range(6)]
    src, cl, out = tmp_path / "src.avi", tmp_path / "clean.avi", tmp_path / "out.mp4"
    _write_video(src, frames)
    _write_video(cl, frames[:4])
    regions = tmp_path / "regions.json"
    regions.write_text(json.dumps({"boxes": [{"x": 0, "y": 0, "w": W, "h": H, "role": "dialogue"}]}), encoding="utf-8")
    stats = run_postfix(src, cl, out, regions)
    assert _frame_count(out) == 6
    assert stats["source_frames"] == 6
    assert stats["clean_frames"] == 4
    assert stats["output_frames"] == 6
    assert stats["frame_alignment"].startswith("clean_short")


def test_run_postfix_truncates_long_clean_to_source_count(tmp_path):
    """clean 比 source 长时按 source 帧数截断，避免逐帧验证错位。"""
    W, H = 160, 90
    frames = [np.full((H, W, 3), 50 + i, np.uint8) for i in range(6)]
    src, cl, out = tmp_path / "src.avi", tmp_path / "clean.avi", tmp_path / "out.mp4"
    _write_video(src, frames[:4])
    _write_video(cl, frames)
    regions = tmp_path / "regions.json"
    regions.write_text(json.dumps({"boxes": [{"x": 0, "y": 0, "w": W, "h": H, "role": "dialogue"}]}), encoding="utf-8")
    stats = run_postfix(src, cl, out, regions)
    assert _frame_count(out) == 4
    assert stats["source_frames"] == 4
    assert stats["clean_frames"] == 6
    assert stats["output_frames"] == 4
    assert stats["frame_alignment"].startswith("clean_long")


def test_bright_watermark_residual_is_reworked(tmp_path):
    """暗底亮字水印被擦成低对比糊斑时应检出并整框 NS 修复。"""
    W, H = 160, 90
    box = {"x": 10, "y": 10, "w": 120, "h": 40, "role": "up_watermark"}
    src_frames = []
    clean_frames = []
    for _ in range(5):
        src = np.full((H, W, 3), 30, np.uint8)
        cv2.putText(src, "WM", (15, 36), cv2.FONT_HERSHEY_SIMPLEX, 0.9,
                    (200, 200, 200), 2, cv2.LINE_AA)
        ink = (cv2.cvtColor(src, cv2.COLOR_BGR2GRAY) > 120).astype(np.uint8) * 255
        smudge = cv2.GaussianBlur(ink, (11, 11), 0).astype(np.float32) * 0.45
        cl = np.full((H, W, 3), 30, np.uint8)
        for ch in range(3):
            cl[:, :, ch] = np.clip(cl[:, :, ch].astype(np.float32) + smudge, 0, 255).astype(np.uint8)
        src_frames.append(src)
        clean_frames.append(cl)
    src_path, cl_path, out = tmp_path / "src.avi", tmp_path / "clean.avi", tmp_path / "out.mp4"
    _write_video(src_path, src_frames)
    _write_video(cl_path, clean_frames)
    hits = detect_bright_watermark_residual(src_path, cl_path, [box])
    assert hits and all(bi == 0 for _fi, bi in hits), hits
    regions = tmp_path / "regions.json"
    regions.write_text(json.dumps({"boxes": [box]}), encoding="utf-8")
    stats = run_postfix(src_path, cl_path, out, regions)
    assert stats["bright_fixed"] >= 5
    assert stats["bright_watermark_residual"]


def test_cut_watermark_donor_repair(tmp_path):
    """切镜帧的水印从同镜头相邻帧借像素，不动其它帧。"""
    W, H = 160, 90
    box = {"x": 8, "y": 8, "w": 70, "h": 26, "role": "up_watermark"}
    frames = []
    for i in range(6):
        f = np.full((H, W, 3), 30 + i, np.uint8)
        if i in (2, 3):
            cv2.putText(f, "WM", (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                        (220, 220, 220), 2, cv2.LINE_AA)
        frames.append(f)
    vin, vout = tmp_path / "in.avi", tmp_path / "out.avi"
    _write_video(vin, frames)
    stats = _repair_cut_watermarks_from_donors(vin, vout, [2, 3], [box])
    assert stats["frames"] == 6
    assert stats["donor_fixed"] == 2
    cap = cv2.VideoCapture(str(vout))
    out_frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        out_frames.append(f)
    cap.release()
    assert len(out_frames) == 6
    for i in (2, 3):
        roi = cv2.cvtColor(out_frames[i][8:34, 8:78], cv2.COLOR_BGR2GRAY)
        assert float(roi.max()) < 100
    for i in (0, 1, 4, 5):
        assert float(cv2.absdiff(frames[i], out_frames[i]).mean()) < 3
