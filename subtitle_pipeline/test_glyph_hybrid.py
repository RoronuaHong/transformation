"""glyph_detect / glyph_mask_hybrid / lama 降级 测试。"""
from __future__ import annotations

import numpy as np
import pytest


def test_extract_pts_both_structures() -> None:
    from glyph_detect import _extract_pts

    # use_rec=False: item 即 4×2 点集
    box = [[10.0, 20.0], [110.0, 20.0], [110.0, 50.0], [10.0, 50.0]]
    pts = _extract_pts(box)
    assert pts is not None and pts.shape == (4, 2)
    # use_rec=True: [box, text, score]
    pts2 = _extract_pts([box, "text", 0.9])
    assert pts2 is not None and pts2.shape == (4, 2)
    # 垃圾输入
    assert _extract_pts(None) is None
    assert _extract_pts([1, 2, 3]) is None


def test_glyph_mask_hybrid_uses_ocr_bbox() -> None:
    """有文字帧：hybrid mask 应该只覆盖 OCR bbox 附近的笔画，非整个框。"""
    import cv2

    from sttn_inpaint import glyph_mask_hybrid

    # 黑底白字
    f = np.full((120, 320, 3), 30, dtype=np.uint8)
    cv2.putText(f, "HELLO WORLD", (60, 60), cv2.FONT_HERSHEY_SIMPLEX,
                1.0, (255, 255, 255), 2, cv2.LINE_AA)
    box = {"x": 20, "y": 20, "w": 280, "h": 60}
    m = glyph_mask_hybrid(f, box)
    cov = float((m > 0).mean())
    assert cov > 0.01, f"mask 不应为空 cov={cov}"
    # mask 应集中在文字行附近（y 30..80），不该覆盖整个框（避免 bbox 大洞伪影）
    ys, xs = np.where(m > 0)
    assert ys.min() >= 20 and ys.max() <= 90
    # 空场景（无文字）→ 不挖洞
    f2 = np.full((120, 320, 3), 30, dtype=np.uint8)
    m2 = glyph_mask_hybrid(f2, box)
    assert int(m2.max()) == 0, "无文字帧不应产生洞"


def test_lama_engine_fallback_without_weights(tmp_path, monkeypatch) -> None:
    """缺 lama.onnx 时 engine=lama 应降级 STTN 且不崩。"""
    from pathlib import Path

    import inpaint_lama as il
    from visual_cleanup import run_multipass_cleanup
    import test_demosaic_instance as tdi

    monkeypatch.setattr(il, "LAMA_WEIGHTS", Path("/nonexistent/lama.onnx"))
    monkeypatch.setattr(il, "_SESSION", None)

    inst = tmp_path / "inst"
    inst.mkdir()
    src = inst / "s.mp4"
    tdi._make_sample(src)
    out = inst / "o.mp4"
    res = run_multipass_cleanup(
        src, out, work_dir=inst / "w", locate_mode="band",
        engine="lama", demosaic=False, dehardsub=True,
    )
    assert out.is_file()
    assert res.get("action") == "sttn_cleanup", res.get("action")


def test_lama_inpaint_missing_weights_raises(monkeypatch) -> None:
    from pathlib import Path

    import inpaint_lama as il

    monkeypatch.setattr(il, "LAMA_WEIGHTS", Path("/nonexistent/lama.onnx"))
    monkeypatch.setattr(il, "_SESSION", None)
    with pytest.raises(il.LamaUnavailable):
        il.lama_inpaint(np.zeros((64, 64, 3), np.uint8), np.zeros((64, 64), np.uint8))


def test_dehardsub_engine_lama_registered() -> None:
    from media_ops import DEHARDSUB_ENGINES, normalize_media_opts

    assert "lama" in DEHARDSUB_ENGINES
    assert normalize_media_opts({"dehardsub_engine": "lama"})["dehardsub_engine"] == "lama"
