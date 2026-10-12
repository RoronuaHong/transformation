"""查询意图识别 GT：规则通道 + 决策模型兜底通道。

GT 分两组：
  GT_RULES      规则必须判对的（无网络、零延迟，任何环境都跑）
  GT_RULE_MISS  现有关键词规则会漏判（回落 text）的样本，用于量化模型兜底价值，
                仅在设置 VITUAL_DECISION_URL 时断言（决策服务已起才跑）
"""
import os

import pytest

import core

# (query, 期望意图)
GT_RULES = [
    ("job:abc123", "id"),
    ("type:subs", "id"),
    ("tag:food", "id"),
    ("9ce0ad1442b8", "id"),
    ("sub\\a\\clip.mp4", "id"),
    ("这句台词是什么", "speech"),
    ("字幕里提到红烧肉吗", "speech"),
    ("视频里说了什么", "speech"),
    ("红烧鸡翅", "text"),
    ("夕阳下的海边", "text"),
]

# 规则词表未收录 -> 规则一律回落 text，但这些其实该走 speech / id
GT_RULE_MISS = [
    ("BK1234abcdef.mp4", "id"),  # 裸文件名：无分隔符、不匹配十六进制全串，规则漏判
    ("他讲了啥", "speech"),
    ("主角最后一句是啥来着", "speech"),
    ("帮我找下那句'再来一份'", "speech"),
]


@pytest.mark.parametrize("q,expect", GT_RULES)
def test_rules_channel(q, expect):
    assert core._query_intent(q) == expect


def test_default_is_pure_rules_no_decision():
    """未设 VITUAL_DECISION_URL 时完全走规则，不产生任何网络调用。"""
    saved = os.environ.pop("VITUAL_DECISION_URL", None)
    try:
        assert core.decision_endpoint() == ""
        assert core.decision_choice_intent("这句台词是什么") is None
        assert core._query_intent_decided("这句台词是什么") == "speech"
        assert core._query_intent_decided("红烧鸡翅") == "text"
    finally:
        if saved is not None:
            os.environ["VITUAL_DECISION_URL"] = saved


@pytest.mark.skipif(
    not os.environ.get("VITUAL_DECISION_URL"),
    reason="未设 VITUAL_DECISION_URL（决策服务未起），跳过模型兜底断言",
)
@pytest.mark.parametrize("q,expect", GT_RULES + GT_RULE_MISS)
def test_decision_fallback(q, expect):
    """规则命中直接返回；规则兜底为 text 时由决策模型兜底。"""
    assert core._query_intent_decided(q) == expect


def test_arbiter_annotate_mapping():
    """决策仲裁映射（纯函数，不依赖 CLIP / 网络）。"""
    auto = [{"zh": "菜品"}, {"zh": "人物"}, {"zh": "风景"}]
    scores = {"a0": 0.97, "a1": 0.01, "a2": None}  # 风景无分 -> fail
    core._arbiter_annotate(auto, scores, 0.6)
    assert [x["arbiter"] for x in auto] == ["pass", "fail", "fail"]


def test_arbiter_annotate_threshold():
    scores = {"a0": 0.65}
    items = [{"zh": "x"}]
    core._arbiter_annotate(items, scores, 0.6)
    assert items[0]["arbiter"] == "pass"
    core._arbiter_annotate(items, scores, 0.7)  # 抬高阈值 -> fail
    assert items[0]["arbiter"] == "fail"

