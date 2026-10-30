"""回归测试：OCR 细字形洞 + TELEA 去字幕路线。

锁住核心不变量，防止以后重构（流式 / 羽化 / 时序平滑改动）无声退回：
  * 对「深色底 + 白字」合成视频跑一遍后 qc_dehardsub 漏删=0、误删=0；
  * 对无字视频跑一遍不误伤（无误删、无漏删）。
"""
import os
import shutil
import sys

import numpy as np
import pytest

sys_path = os.path.dirname(__file__)
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

# OCR 后端（glyph_detect._get_ocr，可能为 RapidOCR/PaddleOCR）缺失时跳过，
# 不阻塞其它测试。
try:
    from dehardsub_ocr_telea import run, qc_dehardsub, _ffmpeg  # noqa: E402
except Exception as e:  # pragma: no cover
    pytest.skip(f"dehardsub_ocr_telea unavailable: {e}", allow_module_level=True)

import cv2  # noqa: E402

# ffmpeg 来自 imageio_ffmpeg（管线自带），未必在 PATH；用模块自己的解析器校验。
_ff = _ffmpeg()
if not (os.path.exists(_ff) or shutil.which(_ff)):
    pytest.skip("ffmpeg not available", allow_module_level=True)


def _make_video(path, n=20, W=640, H=360, text="HELLO WORLD"):
    """深色背景 + 居中白字（目标字幕）。text=None 表示无字。"""
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 25, (W, H))
    for _ in range(n):
        img = np.full((H, W, 3), 28, np.uint8)
        if text:
            cv2.putText(img, text, (60, 210),
                        cv2.FONT_HERSHEY_SIMPLEX, 2.0, (255, 255, 255), 4)
        vw.write(img)
    vw.release()


def test_removes_band_text_without_damage(tmp_path):
    src = tmp_path / "src.avi"
    out = tmp_path / "out.mp4"
    _make_video(src)

    # 字幕带覆盖白字区域；无角标 → watermark=None 走自动探测（应探测为空）。
    band = {"x": 40, "y": 80, "w": 560, "h": 160}
    stats = run(str(src), str(out), band=band, watermark=None, max_frames=20)

    assert stats["frames"] == 20
    assert stats["out"] == str(out)

    r = qc_dehardsub(str(src), str(out), band=band, watermark=None)
    assert r["missed_count"] == 0, r["missed"]
    assert r["damaged_count"] == 0, r["damaged"]


def test_no_false_positive_on_clean_video(tmp_path):
    src = tmp_path / "src.avi"
    out = tmp_path / "out.mp4"
    _make_video(src, text=None)  # 无字视频

    band = {"x": 40, "y": 80, "w": 560, "h": 160}
    run(str(src), str(out), band=band, watermark=None, max_frames=20)

    r = qc_dehardsub(str(src), str(out), band=band, watermark=None)
    assert r["missed_count"] == 0
    assert r["damaged_count"] == 0


def test_pipeline_engine_routes_to_ocr_telea(tmp_path):
    # 最后一公里：验证 ocr_telea 已真正接入管线 run_multipass_cleanup，
    # 经引擎路由用自带 OCR 产出去字视频（漏删=0、误删=0）。
    try:
        from visual_cleanup import run_multipass_cleanup  # noqa: F401
    except Exception as e:  # pragma: no cover
        pytest.skip(f"visual_cleanup unavailable: {e}")

    src = tmp_path / "src.mp4"
    out = tmp_path / "out.mp4"
    _make_video(src)

    band = {"x": 40, "y": 80, "w": 560, "h": 160}
    res = run_multipass_cleanup(
        src, out,
        engine="ocr_telea",
        boxes_override=[{**band, "role": "dialogue"}],
        demosaic=False,
        locate_mode="band",
    )
    assert res["action"] == "ocr_telea_cleanup", res
    assert res["engine"] == "ocr_telea"
    assert out.exists(), "ocr_telea 引擎未产出视频"

    r = qc_dehardsub(str(src), str(out), band=band, watermark=None)
    assert r["missed_count"] == 0, r["missed"]
    assert r["damaged_count"] == 0, r["damaged"]


def test_bilibili_source_defaults_to_ocr_telea():
    # B站 任务默认走 ocr_telea；其余内容保持全局默认（sttn）。
    try:
        from media_ops import _default_dehardsub_engine_for
    except Exception as e:  # pragma: no cover
        pytest.skip(f"media_ops unavailable: {e}")

    assert _default_dehardsub_engine_for(
        "downloads/batch/bilibili_BV1oFJ16BEMT/media/source.mp4") == "ocr_telea"
    assert _default_dehardsub_engine_for("some_bv1abc_clip.mp4") == "ocr_telea"
    assert _default_dehardsub_engine_for(
        "downloads/batch/youtube_abc/media/source.mp4") == "sttn"


def test_propainter_engine_wired_and_graceful():
    # 引擎已接入；无 GPU 环境应优雅抛 PropainterUnavailable（由管线捕获降级到 sttn）。
    try:
        from propainter_inpaint import PropainterUnavailable, inpaint_video
    except Exception as e:  # pragma: no cover
        pytest.skip(f"propainter_inpaint unavailable: {e}")

    with pytest.raises(PropainterUnavailable):
        inpaint_video("nonexistent.mp4", "out.mp4", {"x": 0, "y": 0, "w": 10, "h": 10})
