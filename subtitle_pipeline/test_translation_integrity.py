"""翻译完整性回归：BV id 污染 / 串稿 / 简繁不同步 / 同污共识漏洞。

2026-11-13 两个真实 job 的错误必须在这里被复现并拦住：
- BV1sgWn68Ewt：占位标题当热词 → 20 语 6 条 cue 全是 BV 号，vi 轨串厨房词
- BV1erPNzeEUE：zh 与 zh-Hant 内容不一致，ja 轨直接是繁体中文
"""
from __future__ import annotations

import json

import pytest

import translation_integrity as ti
from langs import to_traditional
from llm import verify as V
from multipass_asr import pick_text


class FakeClient:
    def __init__(self, alt):
        self.alt = alt
        self.calls = {"second": 0, "judge": 0, "back": 0}

    def complete(self, prompt: str, *, timeout: int = 300) -> str:
        low = prompt.lower()
        if "json array of strings" in low:
            self.calls["second"] += 1
            return json.dumps(self.alt, ensure_ascii=False)
        if "quality judge" in low:
            self.calls["judge"] += 1
            return json.dumps({"adequacy": 5, "fluency": 5, "terminology": 5,
                               "no_add_omit": 5, "culture": 5, "severity": "none"})
        if "back into" in low:
            self.calls["back"] += 1
            return "OK"
        return ""


def test_hard_pollution_detection():
    assert ti.is_hard_pollution("BV1sgWn68Ewt")
    assert ti.is_hard_pollution("bV1sgWn68Ewt")
    assert ti.is_hard_pollution("try:bilibili:BV1sgWn68Ewt")
    assert ti.is_hard_pollution("{'Vietnamese': 'Lau đũa'}")
    assert ti.is_hard_pollution("https://www.bilibili.com/video/BV1sgWn68Ewt")
    assert not ti.is_hard_pollution("我是中国用户")
    assert not ti.is_hard_pollution("I'm a Chinese user")
    assert not ti.is_hard_pollution("")


def test_placeholder_title_is_not_an_asr_hotword():
    assert ti.sanitize_hotwords("try:bilibili:BV1sgWn68Ewt") is None
    assert ti.sanitize_hotwords("BV1sgWn68Ewt") is None
    assert ti.sanitize_hotwords("空气炸锅\ntry:bilibili:BV1sgWn68Ewt") == "空气炸锅"
    assert ti.sanitize_hotwords("洗勺, 串味") == "洗勺, 串味"


def test_voting_rejects_id_hallucination_from_pass_b():
    assert pick_text("我是中国用户", "BV1sgWn68Ewt", "我是中国用户") == "我是中国用户"
    assert pick_text("BV1sgWn68Ewt", "BV1sgWn68Ewt", "我是中国用户") == "我是中国用户"
    assert pick_text("BV1sgWn68Ewt", "BV1sgWn68Ewt", "BV1sgWn68Ewt") == ""


def test_source_repair_restores_cues_from_multipass_raw(tmp_path):
    raw = {"passes": {
        "A": [{"start": 11.7, "end": 13.6, "text": "甚至为它吃狂"}],
        "B": [{"start": 11.7, "end": 13.6, "text": "BV1sgWn68Ewt"}],
        "C": [{"start": 11.7, "end": 13.6, "text": "甚至为他痴狂"}],
    }}
    (tmp_path / "media").mkdir()
    (tmp_path / "media" / "multipass_raw.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )
    segs = [{"start": 11.7, "end": 13.6, "text": "BV1sgWn68Ewt"}]
    fixed, report = ti.repair_source_segments(segs, tmp_path)
    assert fixed[0]["text"] == "甚至为他痴狂"   # C 遍优先
    assert report["repaired"] == 1 and report["after"]["ok"]
    assert (tmp_path / "media" / "translate_source_qa.json").is_file()


def test_nearest_clean_no_overlap_uses_midpoint():
    """零重叠时按时间中点就近恢复,不能取列表首条(`best_ov=-1` 曾使回退成死代码)。"""
    passes = [
        {"text": "A_far", "start": 0.0, "end": 1.0},     # midpoint 0.5
        {"text": "B_near", "start": 50.0, "end": 51.0},  # midpoint 50.5 最近
    ]
    assert ti._nearest_clean(passes, 100.0, 102.0) == "B_near"


def test_unrepairable_source_fails_fast(tmp_path):
    segs = [{"start": 1.0, "end": 2.0, "text": "BV1sgWn68Ewt"}]
    with pytest.raises(RuntimeError, match="拒绝翻译"):
        ti.repair_source_segments(segs, tmp_path)


def test_unrelated_glossary_is_dropped():
    kitchen = {"names": {}, "terms": {"洗勺": "wash the spoon", "空气炸锅": "air fryer"},
               "asr_fix": {"不穿位": "不串味"}}
    gloss, report = ti.filter_glossary_by_source(kitchen, ["我是中国用户", "我不在意"])
    assert gloss == {} and report["dropped"] == 3 and not report["ok"]
    relevant = {"terms": {"AI": "AI"}}
    gloss2, report2 = ti.filter_glossary_by_source(relevant, ["我曾经爱过另外一个AI"])
    assert gloss2 == relevant and report2["kept"] == 1


def test_translation_qa_catches_all_three_real_world_failures():
    src = [{"text": x} for x in ("甚至为他痴狂", "但我不能保证", "是谁说要打死我的")]
    bad = [{"text": x} for x in ("BV1sgWn68Ewt",
                                "{'Vietnamese': 'Lau đũa'}",
                                "是谁说要打死我的")]
    r = ti.qa_translation_integrity(src, bad, target_lang="en")
    reasons = {i["reason"] for i in r["issues"]}
    assert {"hard_pollution", "untranslated"} <= reasons
    assert not r["ok"]

    ja = [{"text": x} for x in ("誰說要打死我", "彼は", "いいえ")]
    ja_src = [{"text": x} for x in ("是谁说要打死我的", "小人对您忠心耿耿", "绝无二心")]
    r2 = ti.qa_translation_integrity(ja_src, ja, target_lang="ja")
    assert "wrong_language" in {i["reason"] for i in r2["issues"]}
    assert "ja_no_kana" in {w["reason"] for w in r2["warnings"]}

    hant = [{"text": x} for x in ("甚至為他痴狂", "但我不能保證", "是誰說要打死我")]
    r3 = ti.qa_translation_integrity(src, hant, target_lang="zh-Hant")
    assert "zh_hant_length_mismatch" in {i["reason"] for i in r3["issues"]}
    # 简繁同形（大人的武功）是合法繁中，不能误报未翻译
    same = [{"text": "大人的武功"}]
    assert ti.qa_translation_integrity(same, same, target_lang="zh-Hant")["ok"]


def test_wrong_language_detection():
    assert ti.wrong_language("张君宝向他的领主行礼。", "Spanish", "区区张君宝")
    assert ti.wrong_language("谁说要打死我", "Japanese", "是谁说要打死我的")
    assert ti.wrong_language("大人的武功", "Korean", "大人的武功")
    assert not ti.wrong_language("I am loyal to you", "English", "小人对您忠心耿耿")
    assert not ti.wrong_language("大人的武功", "zh-Hant", "大人的武功")
    assert not ti.wrong_language("대인의 무공", "Korean", "大人的武功")
    assert not ti.wrong_language("彼は忠実です", "Japanese", "小人对您忠心耿耿")
    # 照抄中文（== 繁体化源句，含繁体异体）= 漏译，必须判 wrong_language
    assert ti.wrong_language("威風八面", "ja", "威风八面")
    assert ti.wrong_language("天下無敵", "ja", "天下无敌")
    # 视频1 真实失败串：异体「絶」曾漏检，异体归一后必须抓住
    assert ti.wrong_language("絶無二心", "ja", "绝无二心")
    assert ti.wrong_language("所向披靡", "ja", "所向披靡")
    assert ti.wrong_language("区区張君宝", "ja", "区区张君宝")
    # 与源**不同形**的四字串（无 C 的迭代符号）仍放行：不是照抄，属日语写法
    assert not ti.wrong_language("忠孝節義", "ja", "威风八面")
    # qa：照抄源 → wrong_language 硬错误（ok=False）；不同形熟语只留 ja_no_kana
    r_bad = ti.qa_translation_integrity([{"text": "威风八面"}],
                                        [{"text": "威風八面"}], target_lang="ja")
    assert not r_bad["ok"]
    assert "wrong_language" in {i["reason"] for i in r_bad["issues"]}
    r_ok = ti.qa_translation_integrity([{"text": "威风八面"}],
                                       [{"text": "忠孝節義"}], target_lang="ja")
    assert "wrong_language" not in {i["reason"] for i in r_ok["issues"]}
    assert any(w["reason"] == "ja_no_kana" for w in r_ok["warnings"])


def test_same_pollution_consensus_cannot_bypass_proofread():
    # 主副译都输出 BV id：旧逻辑会判 consensus 直接放行。
    c = FakeClient(alt=["BV1sgWn68Ewt"])
    lines, r = V.proofread_lines(["甚至为他痴狂"], ["BV1sgWn68Ewt"],
                                 client=c, mode="consensus")
    assert r.status == "ok" and r.consensus == 0 and r.divergent == 1
    assert r.human_review == 1 and r.flags[0]["reason"] == "hard_issue_unresolved"
    assert c.calls["judge"] == 0

    # 主译污染、副译干净 → 直接采用副译（真实修复，不只是打标）。
    c2 = FakeClient(alt=["I was even crazy about it"])
    lines2, r2 = V.proofread_lines(["甚至为他痴狂"], ["BV1sgWn68Ewt"],
                                   client=c2, mode="consensus")
    assert lines2 == ["I was even crazy about it"]
    assert r2.repaired == 1 and r2.flags[0]["reason"] == "hard_issue_alt"


def test_overlong_catches_context_leak_and_line_shift():
    # 这两句就是站点文章页暴露的真实错误：把语境说明/邻句翻成了日语字幕
    assert ti.overlong("武侠ドラマ：忠実な部下が主君に敬意を払う", "小人对大人的景仰", "ja")
    assert ti.overlong("（大人へ）　この上では、ただただ申し上げます。", "威风八面", "Japanese")
    assert not ti.overlong("威風堂々", "威风八面", "ja")
    assert not ti.overlong("Votre serviteur vous est totalement loyal.", "小人对您忠心耿耿", "fr")
    bad = [{"text": "武侠ドラマ：忠実な部下が主君に敬意を払う"}]
    src = [{"text": "小人对大人的景仰"}]
    assert "overlong" in {i["reason"] for i in
                          ti.qa_translation_integrity(src, bad, target_lang="ja")["issues"]}


def test_glossary_context_is_shared_by_cli_and_batch(tmp_path):
    from pipeline import format_glossary, load_job_glossary

    (tmp_path / "media").mkdir()
    gloss = {"names": {"张君宝": "Zhang Junbao"}, "terms": {}, "asr_fix": {},
             "context": "Wuxia drama: 大人 = my lord, not adult."}
    (tmp_path / "media" / "glossary.json").write_text(
        json.dumps(gloss, ensure_ascii=False), encoding="utf-8"
    )
    assert load_job_glossary(tmp_path) == gloss
    assert "Context" in format_glossary(gloss)
    kept, report = ti.filter_glossary_by_source(gloss, ["区区张君宝"])
    assert kept["context"] == gloss["context"] and report["ok"]


def test_zh_hant_is_deterministic_and_aligned():
    pairs = [
        ("我是中国用户", "我是中國用戶"),
        ("还不仅仅如此", "還不僅僅如此"),
        ("我曾经爱过另外一个AI", "我曾經愛過另外一個AI"),
        ("但我不能保证", "但我不能保證"),
        ("我是谁说要打死我的", "我是誰說要打死我的"),
        ("区区张君宝", "區區張君寶"),
        ("怎会是大人的对手", "怎會是大人的對手"),
        ("犹如滔滔江水", "猶如滔滔江水"),
        ("延绵不绝", "延綿不絕"),
        ("天下无敌", "天下無敵"),
    ]
    for zh, hant in pairs:
        assert to_traditional(zh) == hant
        assert len(zh) == len(hant)


def test_translate_segments_never_calls_llm_for_zh_hant(monkeypatch):
    import pipeline

    def boom(*_a, **_kw):
        raise AssertionError("zh→zh-Hant must not call the translate LLM")

    monkeypatch.setattr(pipeline, "ollama_chat", boom)
    report: dict = {}
    segs = [{"start": 1.0, "end": 2.0, "text": "我是中国用户"},
            {"start": 2.0, "end": 3.0, "text": "延绵不绝"}]
    out = pipeline.translate_segments("", "", segs, "zh-Hant", "zh",
                                      proofread_report=report)
    assert [s["text"] for s in out] == ["我是中國用戶", "延綿不絕"]
    assert report["status"] == "ok" and report["mode"] == "deterministic_zh_hant"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
