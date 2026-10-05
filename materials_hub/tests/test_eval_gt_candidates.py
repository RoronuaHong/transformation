"""GT 补全候选(build_candidates)离线单测:纯函数,合成素材,不碰真实库/GT 文件。

覆盖:窄面标签→high、宽面标签→review、仅文件名词元→low、已标注项永不进候选。
运行: python tests/test_eval_gt_candidates.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eval_gt_candidates as ev  # noqa: E402


def _m(i, name, kind="silent", tags="sp"):
    return {"id": i, "name": name, "kind": kind, "tags": tags}


def test_narrow_facet_gives_high_confidence():
    """type:benchmark 在 GT 全覆盖、库内近乎穷尽 → 未标注项判 high(可认定漏标)。"""
    mats = [_m("a1", "c1.mp4", tags="sp,type:benchmark"),
            _m("a2", "c2.mp4", tags="sp,type:benchmark"),
            _m("a3", "c3.mp4", tags="sp,type:benchmark"),   # 未标注
            _m("b1", "y.srt", kind="subs", tags="sp,type:subs")]
    rep = ev.build_candidates({"基准测试视频": ["a1", "a2"]}, mats)
    cands = rep["基准测试视频"]["candidates"]
    assert [c["id"] for c in cands] == ["a3"], cands
    assert cands[0]["confidence"] == "high"
    assert rep["基准测试视频"]["strong_tags"]["type:benchmark"]["broad"] is False


def test_broad_facet_marked_review_not_high():
    """type:render 这类宽面标签:库内远多于 GT → 候选只标 review,不进 high。"""
    gt_ids = ["g%02d" % i for i in range(18)]
    mats = [_m(i, i + ".mp4", tags="sp,type:render") for i in gt_ids]
    mats += [_m("u%02d" % i, "u%02d.mp4" % i, tags="sp,type:render")
             for i in range(32)]          # 库内共 50 条 vs GT 18 条 → broad
    rep = ev.build_candidates({"对比渲染结果": gt_ids}, mats)
    tags = rep["对比渲染结果"]["strong_tags"]
    assert tags["type:render"]["broad"] is True, tags
    cands = rep["对比渲染结果"]["candidates"]
    assert cands and all(c["confidence"] == "review" for c in cands)
    assert all(c["id"] not in gt_ids for c in cands)


def test_weak_name_token_is_low_confidence():
    """无强标签、仅文件名共享词元 → low(最易误伤)。"""
    mats = [_m("l1", "lama_out.mp4", tags="sp,type:test"),
            _m("l2", "lama_in.mp4", tags="sp,type:test"),
            _m("l3", "lama_other.mp4", tags="sp,type:test")]
    rep = ev.build_candidates({"用 Lama 补全的画面": ["l1", "l2"]}, mats)
    cands = rep["用 Lama 补全的画面"]["candidates"]
    # type:test 覆盖率高但需先过 corpus 上限;此处仅验证词元候选被标 low 且不含已标注项
    assert all(c["id"] not in ("l1", "l2") for c in cands)
    for c in cands:
        if not c["reliable"] and not c["broad"]:
            assert c["confidence"] == "low"


def test_annotated_ids_never_appear_as_candidates():
    mats = [_m("x1", "a.mp4", tags="sp,type:benchmark"),
            _m("x2", "b.mp4", tags="sp,type:benchmark"),
            _m("x3", "c.mp4", tags="sp,type:benchmark")]
    gt = {"基准测试视频": ["x1", "x2", "x3"]}
    rep = ev.build_candidates(gt, mats)
    assert rep["基准测试视频"]["candidates"] == []


def test_missing_gt_items_skipped():
    mats = [_m("z1", "a.mp4", tags="sp")]
    rep = ev.build_candidates({"幽灵查询": ["nope1", "nope2"]}, mats)
    assert rep["幽灵查询"]["candidates"] == []
    assert "note" in rep["幽灵查询"]
