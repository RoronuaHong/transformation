"""CodeFormer 神经路径测试（用假 ONNX session 覆盖"有权重"分支）。

无真实 codeformer.onnx 时，restore_mosaic_crop 只能走降级分支；
这里 monkeypatch _load_session 为假 session，验证「权重就位」时的神经路径：
  - 整块马赛克（覆盖率≥0.5）→ 整块修复，不留接缝
  - 局部马赛克（覆盖率<0.5）→ mask 混合，非 mask 区保持原样
  - session 抛异常 → 优雅降级 OpenCV，不中断
  - 缺权重抛 DemosaicUnavailable → 降级不中断
  - 端到端：run_multipass_cleanup(demosaic_engine="codeformer") 走神经路径
"""
from __future__ import annotations

import numpy as np


class _FakeSession:
    """最小 ONNX session：输入 [1,3,512,512] → 输出同尺寸全 200 灰。"""

    def __init__(self, value: int = 200, fail: bool = False):
        self._value = value
        self._fail = fail

    def get_inputs(self):
        class _I:
            name = "input"

        return [_I()]

    def run(self, _out, feeds):
        if self._fail:
            raise RuntimeError("fake onnx failure")
        blob = next(iter(feeds.values()))
        out = np.full(
            (1, 3, 512, 512), self._value / 255.0, dtype=np.float32
        )
        return [out]


def test_restore_whole_block_uses_neural_output() -> None:
    import cv2
    import demosaic_codeformer as dcf

    roi = np.full((32, 32, 3), 40, dtype=np.uint8)
    mask = np.full((32, 32), 255, dtype=np.uint8)  # 覆盖率 1.0
    dcf._SESSION = _FakeSession(value=200)
    try:
        out, _bg = dcf.restore_mosaic_crop(
            roi, mask, None, radius=7, prefer="codeformer"
        )
    finally:
        dcf._SESSION = None
    # 整块修复：所有像素都应是神经网络输出（≈200），而非原图 40
    assert int(out.mean()) == 200, out.mean()


def test_restore_partial_mask_preserves_unmasked() -> None:
    import demosaic_codeformer as dcf

    roi = np.full((32, 32, 3), 40, dtype=np.uint8)
    mask = np.zeros((32, 32), dtype=np.uint8)
    mask[:8, :8] = 255  # 覆盖率 (8*8)/(32*32)=0.0625 < 0.5
    dcf._SESSION = _FakeSession(value=200)
    try:
        out, _bg = dcf.restore_mosaic_crop(
            roi, mask, None, radius=7, prefer="codeformer"
        )
    finally:
        dcf._SESSION = None
    # mask 区被神经修复（≈200），非 mask 区保持原样（40）
    assert int(out[0, 0, 0]) == 200
    assert int(out[30, 30, 0]) == 40


def test_neural_failure_falls_back_without_raising() -> None:
    import demosaic_codeformer as dcf

    roi = np.full((32, 32, 3), 40, dtype=np.uint8)
    mask = np.full((32, 32), 255, dtype=np.uint8)
    dcf._SESSION = _FakeSession(fail=True)
    try:
        out, _bg = dcf.restore_mosaic_crop(
            roi, mask, None, radius=7, prefer="codeformer"
        )
    finally:
        dcf._SESSION = None
    assert out.shape == roi.shape, "降级后仍应返回同尺寸图像"


def test_missing_weights_falls_back_without_raising() -> None:
    import demosaic_codeformer as dcf

    roi = np.full((32, 32, 3), 40, dtype=np.uint8)
    mask = np.full((32, 32), 255, dtype=np.uint8)
    saved = dcf._SESSION
    dcf._SESSION = None
    try:
        # 确保权重缺失时抛 DemosaicUnavailable 且调用方降级
        try:
            dcf._load_session()
        except dcf.DemosaicUnavailable:
            pass
        out, _bg = dcf.restore_mosaic_crop(
            roi, mask, None, radius=7, prefer="codeformer"
        )
    finally:
        dcf._SESSION = saved
    assert out.shape == roi.shape


def test_cleanup_uses_codeformer_engine_when_weights_present(tmp_path) -> None:
    """端到端：有权重时 run_multipass_cleanup("codeformer") 应真正调用神经路径。"""
    import cv2
    import demosaic_codeformer as dcf
    from visual_cleanup import run_multipass_cleanup

    from test_demosaic_instance import _make_sample, BX, BY, BW, BH

    inst = tmp_path / "inst"
    inst.mkdir()
    src = inst / "source.mp4"
    _make_sample(src)

    mosaic_box = {"x": BX, "y": BY, "w": BW, "h": BH}
    out = inst / "out_cf_neural.mp4"

    calls = {"n": 0}

    class _Spy(_FakeSession):
        def run(self, o, feeds):
            calls["n"] += 1
            return super().run(o, feeds)

    dcf._SESSION = _Spy(value=90)
    try:
        res = run_multipass_cleanup(
            src,
            out,
            work_dir=inst / "work",
            locate_mode="band",
            engine="sttn",
            demosaic=True,
            dehardsub=True,
            demosaic_engine="codeformer",
            mosaic_boxes=[mosaic_box],
        )
    finally:
        dcf._SESSION = None

    assert out.is_file()
    assert res.get("mosaic_engine") == "codeformer"
    assert res.get("mosaic_pre") is True, "应先修马赛克再 STTN 去字幕"
    assert calls["n"] > 0, "神经路径未被调用（权重就位时应走 CodeFormer）"

    # 马赛克区应被神经输出（≈90）覆盖
    cap = cv2.VideoCapture(str(out))
    ok, f = cap.read()
    cap.release()
    assert ok
    assert int(f[BY : BY + BH, BX : BX + BW].mean()) > 60, "马赛克区未被神经修复"
