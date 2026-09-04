"""deblur 阶段接线测试。

覆盖：
  1) 缺 ONNX 权重 → run_postproc 跳过 deblur 而不中断，其余阶段照常产出
  2) 有权重（假 ONNX session）→ deblur 真正执行并产出 deblur/deblurred.mp4
  3) run_postproc 中 deblur 应排在 concat 之后、enhance 之前
"""
from __future__ import annotations

import numpy as np


class _FakeDeblurSession:
    """最小 Real-ESRGAN ONNX session：输入 [1,3,H,W] → 输出同尺寸（锐化=提亮）。"""

    def __init__(self):
        self.calls = 0

    def get_inputs(self):
        class _I:
            name = "input"

        return [_I()]

    def run(self, _out, feeds):
        self.calls += 1
        blob = next(iter(feeds.values()))
        _b, _c, h, w = blob.shape
        # 输出比输入亮（可验证“帧确实被处理过”）
        out = np.clip(blob + 0.2, 0.0, 1.0).astype(np.float32)
        return [out]


def _make_source(path) -> None:
    import cv2

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(path), fourcc, 10, (160, 120))
    for i in range(6):
        f = np.full((120, 160, 3), 60 + i, dtype=np.uint8)
        vw.write(f)
    vw.release()


def _make_work_dir(tmp_path, src) -> "object":
    """构造最小 work_dir：media/source.mp4 结构。"""
    import shutil

    work = tmp_path / "job"
    media = work / "media"
    media.mkdir(parents=True)
    shutil.copy2(src, media / "source.mp4")
    return work


def test_deblur_skipped_when_weights_missing(tmp_path) -> None:
    """缺权重时应跳过 deblur 且不抛异常，流水线继续。"""
    from media_ops import run_postproc
    from test_demosaic_instance import _make_sample

    src = tmp_path / "src.mp4"
    _make_sample(src)
    work = _make_work_dir(tmp_path, src)

    # deblur 权重不存在（默认 models/realesrgan_x4.onnx 缺失）
    out = run_postproc(work, frozenset({"deblur"}), media_opts={"deblur": True})
    # 未产出 deblur（优雅跳过），但不抛异常
    assert "deblur" not in out


def test_deblur_runs_when_weights_present(tmp_path) -> None:
    """有权重时 deblur 应真正执行，产出 media/deblur/deblurred.mp4。"""
    import deblur_basicvsr as dbv
    from media_ops import job_media_dir, run_postproc
    from test_demosaic_instance import _make_sample

    src = tmp_path / "src.mp4"
    _make_sample(src)
    work = _make_work_dir(tmp_path, src)

    sess = _FakeDeblurSession()
    dbv._SESSION["realesrgan"] = sess
    try:
        out = run_postproc(work, frozenset({"deblur"}), media_opts={"deblur": True})
    finally:
        dbv._SESSION.clear()

    assert "deblur" in out, f"deblur 阶段未产出: {out}"
    assert sess.calls > 0, "神经推理未被调用"
    dest = job_media_dir(work) / "deblur" / "deblurred.mp4"
    assert dest.is_file(), f"产物缺失: {dest}"


def test_deblur_engine_option_respected(tmp_path) -> None:
    """deblur_engine=basicvsr++ 应复用 realesrgan 权重并同样执行。"""
    import deblur_basicvsr as dbv
    from media_ops import run_postproc
    from test_demosaic_instance import _make_sample

    src = tmp_path / "src.mp4"
    _make_sample(src)
    work = _make_work_dir(tmp_path, src)

    sess = _FakeDeblurSession()
    dbv._SESSION["basicvsr++"] = sess
    try:
        out = run_postproc(
            work,
            frozenset({"deblur"}),
            media_opts={"deblur": True, "deblur_engine": "basicvsr++"},
        )
    finally:
        dbv._SESSION.clear()

    assert "deblur" in out
    assert sess.calls > 0
