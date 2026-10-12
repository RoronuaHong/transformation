"""P1-2 引用核查三档置信门控单测（不依赖 kb.server 重依赖）。

门控逻辑在 materials_hub/core.py 的 citation_gate，已与判定(decision_citation_supported)一起
迁入，便于在轻量环境单测。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core  # noqa: E402


def test_gate_disabled(monkeypatch):
    """服务关闭/失败：不降级，原样返回。"""
    monkeypatch.setattr(core, "decision_citation_supported", lambda a, s: None)
    a, s, d = core.citation_gate("ans", [{"label": "L", "source": "S", "text": "T"}])
    assert s is None and d is None and a == "ans"


def test_gate_unsupported(monkeypatch):
    """低于 MIN_CONF：降级为仅返回检索原文（前 N 条）。"""
    monkeypatch.setattr(core, "decision_citation_supported", lambda a, s: 0.2)
    ctx = [{"label": "L%d" % i, "source": "S", "text": "T"} for i in range(10)]
    a, s, d = core.citation_gate("ans", ctx)
    assert d == "unsupported"
    assert "前 5 条" in a
    assert a.count("来源") == 5


def test_gate_partial(monkeypatch):
    """[MIN_CONF, HIGH)：部分支撑，放行但标注。"""
    monkeypatch.setattr(core, "decision_citation_supported", lambda a, s: 0.8)
    a, s, d = core.citation_gate("ans", [])
    assert d == "partial" and a == "ans"


def test_gate_strong(monkeypatch):
    """>= HIGH：强支撑，自动放行。"""
    monkeypatch.setattr(core, "decision_citation_supported", lambda a, s: 0.95)
    a, s, d = core.citation_gate("ans", [])
    assert d == "strong"


def test_gate_threshold_from_env(monkeypatch):
    """阈值可由环境变量覆盖。"""
    monkeypatch.setenv("VITUAL_DECISION_MIN_CONF", "0.85")
    monkeypatch.setattr(core, "decision_citation_supported", lambda a, s: 0.8)
    a, s, d = core.citation_gate("ans", [])
    assert d == "unsupported"  # 0.8 < 0.85 降级


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
