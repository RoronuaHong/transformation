"""查询归一层(缩写/简称归一)单测。

原 expand_query 对纯英文查询"原样返回",导致 4K/GPU/语音识别 等缩写查询无法归一;
现增 QUERY_ABBREV 层(英文缩写词边界匹配 + 中文简称),数据驱动对齐语料 token。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core  # noqa: E402


def test_abbrev_expand():
    assert "upscale" in core.expand_query("4k画质提升").split()
    assert "esrgan" in core.expand_query("4k画质提升").split()
    assert "gpu" in core.expand_query("GPU 加速").split()
    assert "asr" in core.expand_query("语音识别结果").split()
    assert "ocr" in core.expand_query("文字识别").split()


def test_abbrev_word_boundary():
    """英文缩写按词边界:said/brain 不含独立 ai → 不误展开。"""
    assert core.expand_query("said") == "said"
    assert core.expand_query("brain") == "brain"


def test_abbrev_keeps_synonyms():
    """缩写层不破坏原中文同义词 + weak 逻辑。"""
    assert "dehardsub" in core.expand_query("去字幕").split()
    assert "gpu" in core.expand_query("显卡对比").split()


def test_abbrev_english_query_now_expands():
    """原'纯英文查询原样返回'缺口已补:含 4k 的英文查询追加 upscale/esrgan。"""
    ex = core.expand_query("render in 4k")
    assert "upscale" in ex.split() and "esrgan" in ex.split()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
