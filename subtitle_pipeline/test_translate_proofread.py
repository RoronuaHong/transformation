"""多模型翻译校对（生产热路径）回归：纯 fake client，不依赖 Ollama/网络。

跑法：python -m pytest -q test_translate_proofread.py
"""
from __future__ import annotations

import json

import pytest

from llm import config as llm_config
from llm import factory as llm_factory
from llm import verify as V


class FakeClient:
    """按 prompt 类型返回预设响应；记录调用次数，便于断言算力预算。"""

    def __init__(self, *, alt=None, judge=None, back="OK"):
        self.alt = alt or []
        self.judge = judge or {}
        self.back = back
        self.calls = {"second": 0, "judge": 0, "back": 0}

    def complete(self, prompt: str, *, timeout: int = 300) -> str:
        low = prompt.lower()
        if "json array of strings" in low:
            self.calls["second"] += 1
            return json.dumps(self.alt, ensure_ascii=False)
        if "quality judge" in low:
            self.calls["judge"] += 1
            # judge_rubric 的 prompt 末行是 "<lang>: <被评译文>"；按它取分，
            # 才能模拟「主译差 / 副译好」两次调用拿到不同分。
            lines = prompt.strip().splitlines()
            translation = lines[-1].split(":", 1)[-1].strip() if lines else ""
            payload = self.judge.get(translation, {"adequacy": 5, "fluency": 5,
                                                   "terminology": 5, "no_add_omit": 5,
                                                   "culture": 5, "severity": "none"})
            return json.dumps(payload, ensure_ascii=False)
        if "back into" in low:
            self.calls["back"] += 1
            return self.back
        return ""


def _low_judge():
    return {"adequacy": 1, "fluency": 1, "terminology": 1,
            "no_add_omit": 1, "culture": 1, "severity": "critical"}


# ── 配置层：verify role 单一真源 ────────────────────────────────────────

def test_builtin_local_profile_has_independent_verify_model():
    data = llm_config.load_raw_config(None)
    name, roles = llm_config.resolve_profile(data, profile="local")
    assert name == "local"
    assert set(roles) == {"chat", "translate", "verify"}
    # 校对模型必须异族于主译，否则共识永远成立 = 假校对
    assert roles["verify"].model != roles["translate"].model


def test_verify_inherits_chat_when_not_configured():
    data = {"profile": "x", "profiles": {"x": {
        "chat": {"provider": "openai_compat", "model": "gpt-x",
                 "base_url": "https://example.com/v1", "api_key_env": "K"},
        "translate": {"provider": "ollama", "model": "tg"},
    }}}
    _, roles = llm_config.resolve_profile(data, profile="x")
    assert roles["verify"].provider == "openai_compat"
    assert roles["verify"].model == "gpt-x"


def test_explicit_verify_block_wins():
    data = {"profile": "x", "profiles": {"x": {
        "chat": {"provider": "ollama", "model": "chat-m"},
        "translate": {"provider": "ollama", "model": "tg"},
        "verify": {"provider": "ollama", "model": "judge-m"},
    }}}
    _, roles = llm_config.resolve_profile(data, profile="x")
    assert roles["verify"].model == "judge-m"


def test_complete_model_override_is_honoured(monkeypatch):
    """model= 以前被 complete() 忽略；修复后应作为主模型覆盖，且保留 fallback。"""
    llm_factory.configure_llm()
    seen = []

    class C:
        def complete(self, prompt, *, timeout=300):
            seen.append(self.model)
            return "ok"

    def fake_build(cfg):
        c = C()
        c.model = cfg.model
        return c

    monkeypatch.setattr(llm_factory, "build_client", fake_build)
    assert llm_factory.complete("translate", "p", model="custom:9b") == "ok"
    assert seen == ["custom:9b"]


# ── 校对编排：共识 / 择优修复 / 降级 ────────────────────────────────────

def test_consensus_short_circuits_judge():
    """主副译一致 → 直接 accept，不调用 Judge（省算力，热路径关键）。"""
    c = FakeClient(alt=["Hello", "World"])
    lines, r = V.proofread_lines(
        ["你好", "世界"], ["Hello", "World"],
        client=c, mode="consensus",
    )
    assert lines == ["Hello", "World"]
    assert r.status == "ok" and r.consensus == 2 and r.divergent == 0
    assert r.judged == 0 and c.calls["judge"] == 0
    assert r.repaired == 0 and r.human_review == 0


def test_divergent_reject_prefers_better_alt():
    """主译被 Judge 判 reject → 评副译，择优替换并记入报告。"""
    src = ["把勺子洗干净", "然后关火"]
    c = FakeClient(
        alt=["Wash the spoon clean", "Then turn off the heat"],
        judge={"Clean the fire": _low_judge(),
               "Wash the spoon clean": {"adequacy": 5, "fluency": 5, "terminology": 5,
                                        "no_add_omit": 5, "culture": 5, "severity": "none"}},
    )
    lines, r = V.proofread_lines(
        src, ["Clean the fire", "Then turn off the heat"],
        client=c, mode="consensus",
    )
    assert lines[0] == "Wash the spoon clean"
    assert lines[1] == "Then turn off the heat"
    assert r.repaired == 1 and r.judged >= 2
    assert r.flags[0]["reason"] == "judge_preferred_alt"


def test_human_review_is_flagged_not_silently_accepted():
    c = FakeClient(
        alt=["Alternative"],
        judge={"Primary": {"adequacy": 4, "fluency": 4, "terminology": 4,
                           "no_add_omit": 4, "culture": 4, "severity": "minor"},
               "Alternative": {"adequacy": 4, "fluency": 4, "terminology": 4,
                               "no_add_omit": 4, "culture": 4, "severity": "minor"}},
    )
    _lines, r = V.proofread_lines(["SRC"], ["Primary"], client=c, mode="consensus")
    assert r.human_review == 1
    assert r.flags[0]["reason"] == "human_review"


def test_off_mode_is_noop_and_makes_no_llm_calls():
    c = FakeClient(alt=["x"])
    lines, r = V.proofread_lines(["a"], ["b"], client=c, mode="off")
    assert lines == ["b"] and r.status == "disabled"
    assert c.calls == {"second": 0, "judge": 0, "back": 0}


def test_verify_failure_never_breaks_translation():
    class Boom:
        def complete(self, prompt, *, timeout=300):
            raise RuntimeError("ollama down")

    lines, r = V.proofread_lines(["a"], ["PRIMARY"], client=Boom(), mode="consensus")
    assert lines == ["PRIMARY"]          # 原译文必须原样保留
    assert r.status == "error" and "ollama down" in r.error


def test_bad_second_translation_length_still_proofs_other_cues():
    """副译漏行不能把整批校对作废：重试后按序补空，缺失行保留主译并留痕。"""
    c = FakeClient(alt=["only one"])
    lines, r = V.proofread_lines(["a", "b"], ["A", "B"], client=c, mode="consensus")
    assert lines == ["A", "B"] and r.status == "ok"
    assert any(f["reason"] == "second_translation_length_mismatch" for f in r.flags)
    assert c.calls["second"] == 2


def test_same_model_is_rejected_as_fake_proofread(monkeypatch):
    monkeypatch.setattr(V, "_client_models", lambda: ("ollama:same", "ollama:same"))
    c = FakeClient(alt=["x"])
    lines, r = V.proofread_lines(["a"], ["A"], client=c, mode="consensus")
    assert lines == ["A"] and r.status == "skipped"
    assert c.calls["second"] == 0


def test_judge_budget_caps_cost(monkeypatch):
    monkeypatch.setattr(V, "verify_judge_budget", lambda: 1)
    c = FakeClient(alt=["x1", "x2", "x3"])
    _lines, r = V.proofread_lines(
        ["a", "b", "c"], ["A1", "A2", "A3"], client=c, mode="consensus"
    )
    assert r.divergent == 3 and r.judged == 1
    assert r.human_review == 2


def test_normalize_ignores_punctuation_and_case():
    assert V._normalize("Hello,  world!") == V._normalize(" hello world ")
    assert V._normalize("你好！") == V._normalize("你好")


# ── 笔记/文档本地化校对 ─────────────────────────────────────────────────

def test_notes_proofread_preserves_media_metadata():
    src = {"title": "源标题", "summary": "源总结",
           "key_points": [{"title": "要点一", "detail": "细节一",
                           "image": "a.jpg", "start_sec": 12.5}]}
    tgt = {"title": "TITLE", "summary": "SUMMARY",
           "key_points": [{"title": "POINT", "detail": "DETAIL",
                           "image": "a.jpg", "start_sec": 12.5}],
           "source_lang": "zh"}
    c = FakeClient(alt=["TITLE", "SUMMARY", "POINT", "DETAIL"])
    out, r = V.proofread_notes(src, tgt, src_lang="Chinese", tgt_lang="English",
                               client=c, mode="consensus")
    assert r.status == "ok" and r.consensus == 4
    kp = out["key_points"][0]
    assert kp["image"] == "a.jpg" and kp["start_sec"] == 12.5  # 元数据不动
    assert out["source_lang"] == "zh"                            # 语言码不翻


def test_notes_proofread_repairs_rejected_field():
    src = {"title": "洗勺子", "summary": "总述"}
    tgt = {"title": "WRONG", "summary": "总述"}
    c = FakeClient(
        alt=["Wash the spoon", "总述"],
        judge={"WRONG": _low_judge()},
    )
    out, r = V.proofread_notes(src, tgt, src_lang="Chinese", tgt_lang="English",
                               client=c, mode="consensus")
    assert out["title"] == "Wash the spoon"
    assert out["summary"] == "总述"
    assert r.repaired == 1


def test_notes_proofread_disabled_keeps_multiline_payload():
    """校对关闭时不得把多行 summary/detail 压平成单行(`t1_lines` 是压平副本)。"""
    tgt = {"title": "TITLE", "summary": "line1\nline2\nline3"}
    out, r = V.proofread_notes({"title": "源", "summary": "行"}, tgt,
                               src_lang="zh", tgt_lang="en", mode="off")
    assert r.status == "disabled"
    assert out["summary"] == "line1\nline2\nline3"
    assert out == tgt


def test_notes_proofread_failure_keeps_multiline_payload():
    class Boom:
        def complete(self, prompt, *, timeout=300):
            raise RuntimeError("down")

    tgt = {"summary": "keep1\nkeep2"}
    out, r = V.proofread_notes({"summary": "行"}, tgt, src_lang="zh", tgt_lang="en",
                               client=Boom(), mode="consensus")
    assert r.status == "error"
    assert out["summary"] == "keep1\nkeep2"


def test_hard_issues_do_not_consume_judge_budget(monkeypatch):
    """硬错误 cue 走副译/人工复核,不调用 Judge,不应挤占真正分歧 cue 的预算。"""
    monkeypatch.setattr(V, "verify_judge_budget", lambda: 3)
    # 前 3 条主译是 BV 污染但副译干净 → hard_issue_alt 修复,不 Judge;
    # 后 2 条主副译分歧 → 应当各自拿到 Judge(预算 3 足够)。
    primary = ["BV1aaaaaaaaaa", "BV1bbbbbbbbbb", "BV1cccccccccc", "D1", "E1"]
    src = ["a", "b", "c", "d", "e"]
    alt = ["clean a", "clean b", "clean c", "alt d", "alt e"]
    c = FakeClient(alt=alt)
    _lines, r = V.proofread_lines(src, primary, client=c, mode="consensus")
    assert r.divergent == 5 and r.repaired == 3
    assert r.judged == 2 and r.human_review == 0, r.flags


def test_notes_proofread_failure_keeps_payload():
    class Boom:
        def complete(self, prompt, *, timeout=300):
            raise RuntimeError("down")

    tgt = {"title": "KEEP"}
    out, r = V.proofread_notes({"title": "源"}, tgt, src_lang="zh", tgt_lang="en",
                               client=Boom(), mode="consensus")
    assert out == {"title": "KEEP"} and r.status == "error"


# ── QA 报告结构 ─────────────────────────────────────────────────────────

def test_proofread_report_is_json_serializable():
    c = FakeClient(alt=["Hello"])
    _lines, r = V.proofread_lines(["你好"], ["Hello"], client=c, mode="consensus")
    d = r.to_dict()
    json.dumps(d)
    for key in ("status", "mode", "total", "consensus", "divergent",
                "judged", "repaired", "human_review", "avg_judge", "flags"):
        assert key in d


def test_verify_mode_env(monkeypatch):
    monkeypatch.setenv("VITUAL_TRANSLATE_VERIFY", "full")
    assert V.verify_mode() == "full"
    monkeypatch.setenv("VITUAL_TRANSLATE_VERIFY", "bogus")
    assert V.verify_mode() == "consensus"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
