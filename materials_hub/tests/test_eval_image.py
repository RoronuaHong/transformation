# -*- coding: utf-8 -*-
"""test_eval_image.py — CLIP 图像检索门禁逻辑离线单测(check_gate 纯函数 + evaluate 指标)。

全 mock 驱动:不连 CLIP/ollama、不动真实库。复跑:python tests/test_eval_image.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eval_image as ei
import core

_BASE = {"p5": 0.35, "r20": 0.98, "mrr": 0.81, "ndcg": 0.84}


def test_check_gate_pass_fail_and_tolerance():
    ok, d = ei.check_gate(0.37, 1.0, 0.83, 0.86, baseline=_BASE)
    assert ok, d                                   # 全部达标
    ok2, d2 = ei.check_gate(0.10, 1.0, 0.83, 0.86, baseline=_BASE)
    assert not ok2 and not d2[0]["pass"], d2       # p5 崩 → FAIL(证明能抓回归)
    ok3, _ = ei.check_gate(0.34, 0.97, 0.80, 0.83, baseline=_BASE)
    assert ok3, "容差 0.02 内的微小波动应仍 PASS"
    print("PASS test_check_gate_pass_fail_and_tolerance")


def test_evaluate_metrics_with_mock():
    def fake(q, limit=20):
        # 排序:A(相关) rank1, C(不相关) rank2, B(相关) rank3
        return {"matches": [{"id": "A"}, {"id": "C"}, {"id": "B"}]}
    orig = core.search_by_text_image
    core.search_by_text_image = fake
    try:
        per, p5, r20, mrr, ndcg = ei.evaluate({"q": ["A", "B"]}, limit=20)
    finally:
        core.search_by_text_image = orig
    assert p5 == 2 / 5.0, p5                       # A,B 都在 top5 → 2/5
    assert r20 == 1.0, r20
    assert abs(mrr - 1.0) < 1e-9, mrr              # 首个相关在 rank1
    import math
    dcg = 1 / math.log2(2) + 1 / math.log2(4)      # rank1 + rank3
    idcg = 1 / math.log2(2) + 1 / math.log2(3)     # 2 个相关的理想
    assert abs(ndcg - dcg / idcg) < 1e-3, ndcg
    print("PASS test_evaluate_metrics_with_mock")


def test_evaluate_handles_list_and_dict():
    """search_by_text_image 返回 list 或 dict 都要能取到 id。"""
    for ret in ([{"id": "A"}], {"matches": [{"id": "A"}]}, {"results": [{"id": "A"}]}):
        orig = core.search_by_text_image
        core.search_by_text_image = (lambda r: (lambda q, limit=20: r))(ret)
        try:
            _, _, _, _, _ = ei.evaluate({"q": ["A"]}, limit=5)
        finally:
            core.search_by_text_image = orig
    print("PASS test_evaluate_handles_list_and_dict")


if __name__ == "__main__":
    test_check_gate_pass_fail_and_tolerance()
    test_evaluate_metrics_with_mock()
    test_evaluate_handles_list_and_dict()
    print("OK")
