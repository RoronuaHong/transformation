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


def test_gt_staleness_flags_zero_coverage():
    """GT 标注的 id 全不在语料 → stale=True。

    零覆盖也必须判 stale：否则 evaluate 的指标会全为 0，门禁从「跳过」
    退化成「误报回归」，比不检测更糟。
    """
    gt = {"q1": ["deadbeef0001", "deadbeef0002"], "q2": ["deadbeef0003"]}
    st = ev.gt_staleness(gt, corpora=[])
    assert st["stale"] is True
    assert st["existing"] == 0 and st["total_gt_ids"] == 3
    assert st["coverage"] == 0.0
    assert "零覆盖" in (st.get("note") or "")


def test_gt_staleness_flags_deleted_ids():
    """GT id 多数已不在语料（语料重构后的典型情形）→ stale=True。"""
    gt = {"q1": ["a" * 12, "b" * 12, "c" * 12]}
    corpora = [{"id": "a" * 12}]          # 3 个标注只有 1 个还在
    st = ev.gt_staleness(gt, corpora=corpora)
    assert st["stale"] is True
    assert st["existing"] == 1 and st["missing"] == 2


def test_gt_staleness_flags_gt_with_no_annotations():
    """GT 存在但每条查询的相关集都为空（total=0）→ 同样属零覆盖，stale=True。"""
    gt = {"q1": [], "q2": []}
    st = ev.gt_staleness(gt, corpora=[{"id": "a" * 12}])
    assert st["stale"] is True
    assert st["total_gt_ids"] == 0 and st["coverage"] is None
    assert "零覆盖" in (st.get("note") or "")


def test_gt_staleness_ok_when_ids_exist():
    """GT id 全部存在于语料 → stale=False（不该误跳）。"""
    gt = {"q1": ["a" * 12, "b" * 12], "q2": ["c" * 12]}
    corpora = [{"id": "a" * 12}, {"id": "b" * 12}, {"id": "c" * 12}]
    st = ev.gt_staleness(gt, corpora=corpora)
    assert st["stale"] is False
    assert st["coverage"] == 1.0 and st["empty_queries"] == 0
