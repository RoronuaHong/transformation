"""全场景回归校对（精简版）：锁定「去字幕+去马赛克」各引擎路径的核心行为。

覆盖（快速合成样片，GPU/CPU 均可跑）：
  A) hybrid 字形 mask：黑底白字 mask 贴合笔画、无字帧不挖洞（防大洞伪影回归）
  B) OCR 封装两种返回结构（use_rec=False/True）
  C) band_is_flat 主背景簇判定（白底黑字/深底白字都必须 True）
  D) engine=sttn 端到端：字幕残留 < 阈值、分辨率/帧数不变
  E) demosaic_engine=sttn 合一路 + mosaic_pre 顺序
  F) engine=lama 缺权重降级 STTN
"""
from __future__ import annotations

import cv2
import numpy as np


def _frame_white_sub() -> np.ndarray:
    """白底黑字帧（合成）。"""
    f = np.full((240, 320, 3), 40, dtype=np.uint8)
    f[197:, :] = (235, 235, 235)
    cv2.putText(f, "HELLO SUB", (60, 225), cv2.FONT_HERSHEY_SIMPLEX,
                0.7, (20, 20, 20), 2, cv2.LINE_AA)
    return f


def _frame_dark_sub() -> np.ndarray:
    """深底白字帧（合成）。"""
    f = np.full((240, 320, 3), 30, dtype=np.uint8)
    cv2.circle(f, (160, 100), 30, (90, 60, 50), -1)
    cv2.putText(f, "HELLO", (110, 200), cv2.FONT_HERSHEY_SIMPLEX,
                0.9, (255, 255, 255), 2, cv2.LINE_AA)
    return f


def test_band_is_flat_both_polarity() -> None:
    """旧 var 判定被字形 AA 边缘拉爆（510>400）的回归锁。"""
    from sttn_inpaint import band_is_flat

    f = _frame_white_sub()
    assert band_is_flat(f, {"x": 35, "y": 186, "w": 248, "h": 53}) is True
    f2 = _frame_dark_sub()
    assert band_is_flat(f2, {"x": 60, "y": 170, "w": 200, "h": 50}) is True


def test_extract_pts_structures() -> None:
    from glyph_detect import _extract_pts

    box = [[10.0, 20.0], [110.0, 20.0], [110.0, 50.0], [10.0, 50.0]]
    assert _extract_pts(box).shape == (4, 2)          # use_rec=False
    assert _extract_pts([box, "text", 0.9]).shape == (4, 2)  # use_rec=True
    assert _extract_pts(None) is None
    assert _extract_pts([1, 2, 3]) is None


def test_glyph_mask_hybrid_stroke_level() -> None:
    """hybrid mask：OCR bbox 限定 + 笔画细化，洞不覆盖整框（防大洞伪影回归）。"""
    import cv2

    from sttn_inpaint import glyph_mask_hybrid

    f = _frame_dark_sub()
    box = {"x": 40, "y": 160, "w": 240, "h": 60}
    m = glyph_mask_hybrid(f, box)
    assert int(m.max()) > 0, "有字帧 mask 不应为空"
    ys, _xs = np.where(m > 0)
    assert ys.min() >= 150 and ys.max() <= 225, "mask 应集中在文字行附近"
    cov = float((m > 0).mean())
    assert cov < 0.30, f"笔画级 mask 覆盖率应远小于整框 cov={cov:.3f}"
    # 无字帧不挖洞
    f2 = np.full((240, 320, 3), 30, dtype=np.uint8)
    assert int(glyph_mask_hybrid(f2, box).max()) == 0


def _make_sample(path, kind: str = "both") -> None:
    import cv2

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(path), fourcc, 10, (320, 240))
    grid = np.zeros((80, 80, 3), np.uint8)
    for gy in range(0, 80, 16):
        for gx in range(0, 80, 16):
            c = 80 + ((gx // 16 + gy // 16) % 2) * 70
            grid[gy : gy + 16, gx : gx + 16] = (c, c, c)
    for i in range(15):
        f = np.full((240, 320, 3), 40, np.uint8)
        cv2.circle(f, (60 + i * 2, 40), 12, (90, 60, 50), -1)
        if kind in ("sub", "both"):
            f[197:, :] = (235, 235, 235)
            cv2.putText(f, "HELLO SUB", (60, 225), cv2.FONT_HERSHEY_SIMPLEX,
                        0.7, (20, 20, 20), 2, cv2.LINE_AA)
        if kind in ("mosaic", "both"):
            j = ((i * 7) % 2) * 6
            f[60:140, 120:200] = np.clip(grid.astype(int) + j, 0, 255).astype(np.uint8)
        vw.write(f)
    vw.release()


def test_sttn_e2e_sub_removed_size_stable(tmp_path) -> None:
    """engine=sttn 端到端：字幕去除 + 分辨率/帧数不变（hybrid mask 默认路径）。"""
    import cv2

    from visual_cleanup import run_multipass_cleanup

    src = tmp_path / "s.mp4"
    _make_sample(src, "sub")
    out = tmp_path / "o.mp4"
    res = run_multipass_cleanup(src, out, work_dir=tmp_path / "w",
                                locate_mode="band", engine="sttn",
                                demosaic=False, dehardsub=True)
    assert out.is_file()

    def _cap(p):
        cap = cv2.VideoCapture(str(p))
        vals = {"w": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "h": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "n": int(cap.get(cv2.CAP_PROP_FRAME_COUNT))}
        cap.release()
        return vals

    s, o = _cap(src), _cap(out)
    assert (o["w"], o["h"]) == (s["w"], s["h"]), f"分辨率被改变 {s} -> {o}"
    assert o["n"] == s["n"], f"帧数被改变 {s['n']} -> {o['n']}"
    # 白底暗字残留（源≈18，处理后应显著下降）
    def _resid(p):
        cap = cv2.VideoCapture(str(p))
        vals = []
        while True:
            ok, f = cap.read()
            if not ok:
                break
            roi = f[197:239, 35:283]
            g = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            vals.append(float(np.mean(np.abs(g.astype(np.float32) - 235.0))))
        cap.release()
        return float(np.mean(vals))

    assert _resid(out) < _resid(src) * 0.6, f"字幕残留未充分下降"


def test_demosaic_combined_and_lama_fallback(tmp_path, monkeypatch) -> None:
    """合一路（demosaic_engine=sttn）与 lama 缺权重降级两分支回归锁。"""
    from pathlib import Path

    import inpaint_lama as il
    from visual_cleanup import run_multipass_cleanup

    src = tmp_path / "s.mp4"
    _make_sample(src, "both")
    out1 = tmp_path / "o1.mp4"
    r1 = run_multipass_cleanup(src, out1, work_dir=tmp_path / "w1",
                               locate_mode="band", engine="sttn",
                               demosaic=True, dehardsub=True,
                               demosaic_engine="sttn")
    assert out1.is_file()
    assert r1.get("mosaic_engine") == "sttn"
    assert len(r1.get("mosaic_regions") or []) >= 1

    monkeypatch.setattr(il, "LAMA_WEIGHTS", Path("/nonexistent/lama.onnx"))
    monkeypatch.setattr(il, "_SESSION", None)
    out2 = tmp_path / "o2.mp4"
    r2 = run_multipass_cleanup(src, out2, work_dir=tmp_path / "w2",
                               locate_mode="band", engine="lama",
                               demosaic=False, dehardsub=True)
    assert out2.is_file()
    assert r2.get("action") == "sttn_cleanup", "缺 lama.onnx 应降级 STTN"
