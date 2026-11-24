"""通用逐帧精修模块的判据与防回退回归测试（纯合成图，不依赖具体视频）。"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dehardsub_frame_refine import (  # noqa: E402
    REFINE_ENV, glyph_prior, needs_rework, refine_enabled,
    refine_roi, residual_stats,
)


def _sample():
    H, W = 60, 240
    src = np.full((H, W, 3), 30, np.uint8)
    cv2.putText(src, "HELLO", (25, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                (235, 235, 235), 2, cv2.LINE_AA)
    box = {"x": 0, "y": 0, "w": W, "h": H}
    boxes = [{"x": 20, "y": 18, "w": 150, "h": 30}]
    return src, box, boxes


def test_refine_env():
    os.environ.pop(REFINE_ENV, None)
    assert refine_enabled() is False
    os.environ[REFINE_ENV] = "1"
    assert refine_enabled() is True
    os.environ.pop(REFINE_ENV, None)


def test_glyph_prior_and_residual_stats_with_reused_boxes():
    src, box, boxes = _sample()
    G, ring = glyph_prior(src, box, boxes)
    assert int((G > 0).sum()) > 200
    assert int(ring.sum()) > 100
    st = residual_stats(src, G, ring)
    assert st["dark_cc"] == 0
    assert st["bright_cc"] > 0
    assert needs_rework({}, st["dark_cc"]) is False


def test_clean_result_is_untouched():
    src, box, boxes = _sample()
    clean = np.full_like(src, 30)
    out, info = refine_roi(src, clean, box, boxes)
    assert info["changed"] is False
    assert np.array_equal(out, clean)


def test_soft_ink_dark_residual_is_reworked():
    """半透明亮字被清成黑字（f825 类）应触发小面积重做。"""
    H, W = 60, 240
    src = np.full((H, W, 3), 30, np.uint8)
    cv2.putText(src, "HELLO", (25, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                (95, 95, 95), 2, cv2.LINE_AA)
    box = {"x": 0, "y": 0, "w": W, "h": H}
    boxes = [{"x": 20, "y": 18, "w": 150, "h": 30}]
    G, ring = glyph_prior(src, box, boxes)
    bad = src.copy()
    bad[G > 0] = (20, 20, 20)
    st = residual_stats(bad, G, ring)
    assert st["dark_cc"] >= 2
    out, info = refine_roi(src, bad, box, boxes)
    assert info["changed"] is True
    assert residual_stats(out, G, ring)["dark_cc"] < st["dark_cc"]


def test_large_luma_drift_is_rejected():
    """整块字形被涂黑、候选会造成亮度大漂移时必须否决（防亮糊斑）。"""
    src, box, boxes = _sample()
    G, ring = glyph_prior(src, box, boxes)
    bad = src.copy()
    bad[G > 0] = (8, 8, 8)
    assert residual_stats(bad, G, ring)["dark_cc"] >= 2
    out, info = refine_roi(src, bad, box, boxes)
    assert info["changed"] is False
    assert np.array_equal(out, bad)


@pytest.mark.parametrize("st", [
    {"dark_cc": 0, "bright_cc": 20, "dlum": 100.0},
    {"dark_cc": 1, "bright_cc": 0, "dlum": 0.0},
])
def test_needs_rework_conservative(st):
    assert needs_rework({}, st["dark_cc"]) is False
