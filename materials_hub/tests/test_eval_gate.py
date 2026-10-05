"""评估回归门禁(--gate)离线测试:纯函数,不连 ollama / 不依赖真实语料。

覆盖:基线达成、单项回落判失败、容差边界、自定义基线、退出码语义。
运行: python tests/test_eval_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eval_search as ev  # noqa: E402


def _base():
    """从 GATE_BASELINE 派生,避免基线调整时测试写死数字而失效。"""
    b = ev.GATE_BASELINE
    return b["p5"], b["r20"], b["mrr"], b["ndcg"]


def test_gate_passes_at_baseline():
    ok, details = ev.check_gate(*_base())
    assert ok is True
    assert len(details) == 4
    assert all(d["pass"] for d in details)
    assert [d["metric"] for d in details] == ["p5", "r20", "mrr", "ndcg"]


def test_gate_fails_when_p5_drops():
    p5, r20, mrr, ndcg = _base()
    ok, details = ev.check_gate(p5 - 0.20, r20, mrr, ndcg)
    assert ok is False
    p5_row = next(d for d in details if d["metric"] == "p5")
    assert p5_row["pass"] is False
    assert p5_row["delta"] < 0
    # 其余指标仍应通过
    assert all(d["pass"] for d in details if d["metric"] != "p5")


def test_gate_tolerance_allows_minor_noise():
    p5, r20, mrr, ndcg = _base()
    tol = ev.GATE_TOL
    # 低于基线但在容差内 → 仍通过
    ok, _ = ev.check_gate(p5 - tol / 2.0, r20, mrr, ndcg)
    assert ok is True
    # 超出容差 → 失败
    ok2, _ = ev.check_gate(p5 - tol * 2.0, r20, mrr, ndcg)
    assert ok2 is False


def test_gate_custom_baseline():
    base = {"p5": 0.10, "r20": 0.10, "mrr": 0.10, "ndcg": 0.10}
    ok, details = ev.check_gate(0.20, 0.20, 0.20, 0.20, baseline=base)
    assert ok is True
    assert details[0]["baseline"] == 0.10
    ok2, _ = ev.check_gate(0.05, 0.05, 0.05, 0.05, baseline=base)
    assert ok2 is False


def test_gate_baseline_constants_sane():
    # 基线必须四指标齐全且在 [0,1]
    for k in ev.GATE_METRICS:
        v = ev.GATE_BASELINE[k]
        assert 0.0 <= v <= 1.0, k
    assert ev.GATE_TOL >= 0
