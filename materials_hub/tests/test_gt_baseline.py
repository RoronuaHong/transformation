# -*- coding: utf-8 -*-
"""GT 重定基线机制的回归测试(2026-11-08)。

覆盖:
  1. P@5 归一化:|rel|<5 时 P@min(5,|rel|),|rel|>=5 时仍 /5(历史基线不变);
  2. gen_ground_truth 语料感知:不写空查询、不写陈旧 id、不复活死查询;
  3. eval_search 删除写死的 11 条死查询后,GT 是唯一 oracle(无 GT 则语料派生),
     GT 含旧同名键也不回退到死查询集;
  4. evaluate_extra 语料感知(不再假定 chicken wings / lang:ja 存在)。
全部 mock 驱动,不依赖真实语料/模型。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eval_search as ev  # noqa: E402
import eval_image as ei  # noqa: E402
import gen_ground_truth as gg  # noqa: E402
import core  # noqa: E402


def _fake_materials():
    return [
        {"id": "m1", "name": "旺财.jpg", "orig_name": "旺财.jpg", "tags": "",
         "description": "", "rel_path": "旺财.jpg", "external_path": ""},
        {"id": "m2", "name": "橄榄油.jpg", "orig_name": "橄榄油.jpg", "tags": "",
         "description": "", "rel_path": "橄榄油.jpg", "external_path": ""},
    ]


def test_text_p5_normalized_small_gold():
    """|rel|=1 的完美检索,P@5 应为 1.0 而非 0.2(结构性封顶修复)。"""
    orig = core.search
    core.search = lambda q, limit=20, mode="auto": [{"id": "A"}]
    try:
        _, p5, r20, mrr, ndcg = ev.evaluate("lexical", {"q1": ["A"]})
    finally:
        core.search = orig
    assert abs(p5 - 1.0) < 1e-9, p5
    assert abs(r20 - 1.0) < 1e-9, r20
    assert abs(mrr - 1.0) < 1e-9, mrr
    assert abs(ndcg - 1.0) < 1e-9, ndcg
    print("PASS test_text_p5_normalized_small_gold")


def test_text_p5_unchanged_for_large_gold():
    """|rel|>=5 时仍按标准 /5,保证历史基线数值含义不变。"""
    orig = core.search
    core.search = lambda q, limit=20, mode="auto": [{"id": "A"}, {"id": "B"}]
    try:
        _, p5, _, _, _ = ev.evaluate("lexical", {"q1": ["A", "B", "C", "D", "E"]})
    finally:
        core.search = orig
    assert abs(p5 - 2 / 5.0) < 1e-9, p5
    print("PASS test_text_p5_unchanged_for_large_gold")


def test_image_p5_normalized_small_gold():
    """图像门禁同款归一化(|rel|=2 完美检索 → 1.0)。"""
    orig = core.search_by_text_image
    core.search_by_text_image = lambda q, limit=20: [
        {"id": "A"}, {"id": "X"}, {"id": "B"}]
    try:
        _, p5, _, _, _ = ei.evaluate({"q": ["A", "B"]}, limit=20)
    finally:
        core.search_by_text_image = orig
    assert abs(p5 - 1.0) < 1e-9, p5
    print("PASS test_image_p5_normalized_small_gold")


def test_gt_generator_no_empty_queries():
    """派生 GT 不含空相关集(空集会把门禁均值拉成 0 → 假失败)。"""
    ms = [
        {"id": "m1", "name": "旺财.jpg", "orig_name": "旺财.jpg", "tags": "",
         "description": "", "rel_path": "旺财.jpg", "external_path": ""},
        {"id": "m2", "name": "橄榄油.jpg", "orig_name": "橄榄油.jpg", "tags": "",
         "description": "", "rel_path": "橄榄油.jpg", "external_path": ""},
    ]
    gt = gg.build_gt(ms, verify=False)
    assert gt and all(len(v) > 0 for v in gt.values()), gt
    assert all(set(v) <= {"m1", "m2"} for v in gt.values()), gt
    print("PASS test_gt_generator_no_empty_queries")


def test_gt_merge_drops_stale_and_never_resurrects_dead():
    """合并模式:丢弃 id 全失效的旧查询,且不写回任何空相关集。"""
    have = {"m1", "m2"}
    old = {
        "去除字幕只留背景": ["dead1", "dead2"],   # 旧流水线查询,全失效
        "旺财": ["m1", "dead3"],                  # 部分失效
        "空查询": [],                              # 空集
    }
    kept = gg.valid_existing(old, have)
    assert "去除字幕只留背景" not in kept, kept
    assert "空查询" not in kept, kept
    assert kept["旺财"] == ["m1"], kept
    print("PASS test_gt_merge_drops_stale_and_never_resurrects_dead")


def test_gt_candidates_skip_generic_tokens():
    """停用/扩展名 token 不应成为查询(过泛会把整库判为相关)。"""
    ms = [{"id": "m1", "name": "微信图片_20240616.png",
           "orig_name": "微信图片_20240616.png", "tags": "", "description": "",
           "rel_path": "微信图片_20240616.png", "external_path": ""}]
    toks = gg.candidate_queries(ms)
    assert not ({"png", "微信图片", "图片"} & set(toks)), toks
    print("PASS test_gt_candidates_skip_generic_tokens")


def test_eval_search_has_no_hardcoded_dead_queries():
    """写死的 11 条旧流水线查询集已删除;GT 是唯一 oracle。"""
    assert not hasattr(ev, "EVAL_QUERIES"), "EVAL_QUERIES 不应再存在"
    # 旧查询名恰好出现在 GT 里时,也不能回退到死查询集(原 intersection 隐患)
    gt = {"人脸修复": ["A"]}          # 这是旧 EVAL_QUERIES 里的键名
    orig = core.search
    core.search = lambda q, limit=20, mode="auto": [{"id": "A"}]
    try:
        per, p5, _, _, _ = ev.evaluate("lexical", gt)
    finally:
        core.search = orig
    assert len(per) == 1 and per[0][0] == "人脸修复", per
    assert abs(p5 - 1.0) < 1e-9, p5
    print("PASS test_eval_search_has_no_hardcoded_dead_queries")


def test_derived_ground_truth_is_corpus_aware():
    """无权威 GT 时按当前语料派生,且过滤检索召回不到的噪声 token。"""
    ms = _fake_materials() + [
        {"id": "m3", "name": "LUOB6.png", "orig_name": "LUOB6.png", "tags": "",
         "description": "", "rel_path": "LUOB6.png", "external_path": ""},
    ]
    orig = core.search
    # 只让「旺财/橄榄油」可召回,luob 查不到 → 验证 verify=True 会剔除噪声
    core.search = (lambda q, limit=20, mode="auto":
                   [{"id": "m1"}] if q == "旺财" else
                   ([{"id": "m2"}] if q == "橄榄油" else []))
    try:
        derived = ev.derived_ground_truth(ms)
    finally:
        core.search = orig
    assert "luob" not in derived, derived
    assert derived.get("旺财") == ["m1"] and derived.get("橄榄油") == ["m2"], derived
    assert all(v for v in derived.values()), derived
    print("PASS test_derived_ground_truth_is_corpus_aware")


def test_evaluate_without_gt_uses_derived_not_dead_queries():
    """gt=None 时不再跑 11 条死查询(均值 0.09),而是语料派生自检。"""
    ms = _fake_materials()
    orig_all, orig_search = core.all_materials, core.search
    core.all_materials = lambda: ms
    core.search = (lambda q, limit=20, mode="auto":
                   [{"id": "m1"}] if q == "旺财" else
                   ([{"id": "m2"}] if q == "橄榄油" else []))
    try:
        per, p5, r20, mrr, ndcg = ev.evaluate("lexical", None)
    finally:
        core.all_materials, core.search = orig_all, orig_search
    assert per and all(q in ("旺财", "橄榄油") for q, *_ in per), per
    assert abs(p5 - 1.0) < 1e-9 and abs(r20 - 1.0) < 1e-9, (p5, r20)
    print("PASS test_evaluate_without_gt_uses_derived_not_dead_queries")


def test_evaluate_extra_corpus_aware():
    """额外检查不再假定 chicken wings / lang:ja 存在,返回语料派生字段。"""
    ms = _fake_materials()
    orig_all, orig_search = core.all_materials, core.search
    core.all_materials = lambda: ms
    core.search = (lambda q, limit=20, mode="auto":
                   [{"id": "m1"}] if q == "旺财" else
                   ([{"id": "m2"}] if q == "橄榄油" else []))
    try:
        ex = ev.evaluate_extra()
    finally:
        core.all_materials, core.search = orig_all, orig_search
    assert "english_query" in ex and "lang_facets" in ex, ex
    assert "chicken wings" not in str(ex), ex
    assert isinstance(ex["lang_facets"], dict), ex
    print("PASS test_evaluate_extra_corpus_aware")


if __name__ == "__main__":
    test_text_p5_normalized_small_gold()
    test_text_p5_unchanged_for_large_gold()
    test_image_p5_normalized_small_gold()
    test_gt_generator_no_empty_queries()
    test_gt_merge_drops_stale_and_never_resurrects_dead()
    test_gt_candidates_skip_generic_tokens()
    test_eval_search_has_no_hardcoded_dead_queries()
    test_derived_ground_truth_is_corpus_aware()
    test_evaluate_without_gt_uses_derived_not_dead_queries()
    test_evaluate_extra_corpus_aware()
    print("OK")
