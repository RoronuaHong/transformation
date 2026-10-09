# -*- coding: utf-8 -*-
"""多语言最佳实践落地验收(步骤 1/2/3/4/7/9/10/11)。

conftest 把每个测试隔离到临时空库,因此数据相关用例自行注入素材,不依赖真实 384 条库。
不连 ollama:技能匹配走「原文已命中」路径,lang 归一化/记录读写/汇总均离线。
运行: python tests/test_multilang.py
"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import agent  # noqa: E402

core._init_db()

# 测试环境无 ollama,关闭向量同步,避免 update_tags/attach 触发模型调用
core._sync_material_vector = lambda mid: None
core.build_embeddings = lambda *a, **k: None


def _mk(mid, kind, name, tags="", ext=".mp4"):
    f = os.path.join(tempfile.gettempdir(), f"{mid}_{name}")
    # 字幕素材写一份合法 .srt(否则 srt_cues 解析为空,挂接会被跳过)
    content = ("1\n00:00:00,000 --> 00:00:01,000\nhello\n\n" if ext == ".srt" else "x")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(content)
    core.add_material(id=mid, kind=kind, ext=ext, name=name, rel_path="",
                      size=1, sha256="x" * 64, tags=tags, description="",
                      source="", orig_name=name, created_at="2026-01-01")
    core._update_material(mid, location="external", external_path=f)
    return mid


def test_normalize_lang_tag():
    # 16 受控代码原样保留(含 zh-Hant 连字符)
    for c in core.SITE_LANGS:
        assert core.normalize_lang_tag(c) == c
    # 标准别名收口(下划线/大小写归一)
    assert core.normalize_lang_tag("zh-cn") == "zh"
    assert core.normalize_lang_tag("ZH-HANT") == "zh-Hant"
    assert core.normalize_lang_tag("pt_BR") == "pt"
    assert core.normalize_lang_tag("zh_hant") == "zh-Hant"
    # 模型自由发挥一律不认(步骤 3:lang: 只认站点 16 码)
    assert core.normalize_lang_tag("chinese") is None
    assert core.normalize_lang_tag("日本語") is None
    assert core.normalize_lang_tag("japanese") is None
    assert core.normalize_lang_tag("") is None


def test_parse_lang_filter():
    assert agent._parse_lang_filter("export ja subs lang:ja") == "ja"
    assert agent._parse_lang_filter("导出日文 lang:zh-Hant") == "zh-Hant"
    assert agent._parse_lang_filter("Japanese subtitles") == "ja"
    assert agent._parse_lang_filter("随便看看素材") is None


def test_skill_match_english_via_bridge():
    # 英文技能词直接命中(原文命中,不触发翻译模型)
    assert agent._match_named_skill("package 3 indoor dialogue shots") == "package"
    # 模糊任务不命中技能,仍走开放循环
    assert agent._match_named_skill("帮我看看这些素材") == ""


def test_backfill_and_lang_ja():
    _mk("v10000000001", "videos", "master.mp4", tags="job:J1")
    _mk("s1000000000a", "subs", "sub_ja.srt", tags="job:J1,type:subs", ext=".srt")
    _mk("s1000000000b", "subs", "sub_zh.srt", tags="job:J1,type:subs", ext=".srt")
    r = core.backfill_lang_tags()
    assert r["updated"] == 2
    # lang:ja 单独可搜到(步骤 1:日文译文是独立字幕)
    assert len(core.search("", tag="lang:ja", limit=50)) == 1
    # 母版只挂一条源语(简体优先),日文言进 other_langs(步骤 1)
    rr = core.attach_job_transcripts(mids=["v10000000001"])
    import sys
    subs = [m for m in core.all_materials() if m.get("kind") == "subs"]
    for m in subs:
        p = m.get("external_path") or ""
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as fh:
                b = fh.read()
            print("DBG", m["id"], "cues=", core.srt_cues(b), "repr=", repr(b[:30]), file=sys.stderr)
    assert rr["attached"] == 1
    info = core.job_transcript_info("v10000000001")
    assert info["attached_lang"] == "zh"
    assert "ja" in info["other_langs"]


def test_run_understand_records_roundtrip():
    vid = _mk("v20000000001", "videos", "m2.mp4", tags="job:J2")
    _mk("s2000000000a", "subs", "sub_zh.srt", tags="job:J2,type:subs", ext=".srt")
    rr = core.write_run_record(vid)
    assert rr["id"] == vid and rr["runnable"] is True
    assert core.read_run_record(vid) is not None
    ur = core.write_understand_record(vid)
    for k in ("speech", "visual_zh", "visual_en", "quality", "reviewed"):
        assert k in ur
    assert core.read_understand_record(vid) is not None
    core.set_reviewed(vid, True)
    assert core.read_understand_record(vid)["reviewed"] is True


def test_ops_summary_counts():
    _mk("v30000000001", "videos", "m3.mp4", tags="job:J3")
    _mk("s3000000000a", "subs", "ja.srt", tags="job:J3,type:subs", ext=".srt")
    core.backfill_lang_tags()
    o = core.ops_summary()
    for k in ("total_materials", "visual_bilingual", "source_attached",
              "per_lang_subs", "bad_files", "exports", "feedback_total",
              "check_english_query_chicken_wings_top", "check_lang_ja_subs"):
        assert k in o
    assert set(o["per_lang_subs"].keys()) == set(core.SITE_LANGS)
    assert o["per_lang_subs"]["ja"] >= 1
    assert o["check_lang_ja_subs"] >= 1


def test_job_lang_readiness():
    """步骤 7:缺某一种译文只是「说明」,不把整个任务判死。"""
    _mk("v50000000001", "videos", "m5.mp4", tags="job:J5")
    _mk("s5000000000a", "subs", "zh.srt", tags="job:J5,type:subs", ext=".srt")
    _mk("s5000000000b", "subs", "en.srt", tags="job:J5,type:subs", ext=".srt")
    core.backfill_lang_tags()
    # 已有的语种:present=True,不 blocking
    ok = core.job_lang_readiness("J5", "zh")
    assert ok["present"] is True
    assert ok["blocking"] is None
    # 缺的语种:blocking 写明缺哪种,但仍可按已有语言导出(available_langs 非空)
    miss = core.job_lang_readiness("J5", "ja")
    assert miss["present"] is False
    assert miss["blocking"] == "missing_lang:ja"
    assert set(miss["available_langs"]) >= {"zh", "en"}
    # 未点名语种:只列有哪些,不判死
    none = core.job_lang_readiness("J5", "")
    assert none["present"] is None and none["blocking"] is None


def test_distribution_readiness_blocks_bad_source():
    """步骤 7:源文件打不开 → channel_ready 为假(只说明,不把整任务判死)。"""
    _mk("v60000000001", "videos", "m6.mp4", tags="job:J6")
    # 内链素材指向不存在的磁盘文件(模拟源文件打不开)
    core._update_material("v60000000001", location="internal",
                          rel_path="/nonexistent/v60000000001.mp4")
    r = core.distribution_readiness("J6")
    assert r["channel_ready"] is False
    assert any(b.startswith("source_unopenable:") for b in r["blocking"])


def test_deliver_package_copy_and_dryrun():
    """步骤 6:非视频素材按原样复制交付;confirm=False 只给计划不写文件。"""
    import shutil as _sh

    mid = _mk("v60000000002", "subs", "m8.srt", tags="job:J8,lang:zh")
    # dry-run 不写文件
    plan = core.deliver_package(ids=[mid], confirm=False)
    assert plan["dry_run"] is True
    assert plan["plan"] and plan["plan"][0]["mode"] == "copy"
    assert not plan["written"]
    # 确认后真正复制
    out = core.deliver_package(ids=[mid], confirm=True)
    assert not out["dry_run"]
    assert out["written"] and out["written"][0]["status"] == "copied"
    assert os.path.isfile(out["written"][0]["dst"])
    # 清理交付目录(落在 index/agent_workspace/deliveries 下)
    d = os.path.dirname(out["written"][0]["dst"])
    if d.startswith(os.path.join(core.HUB, "index", "agent_workspace", "deliveries")):
        _sh.rmtree(d, ignore_errors=True)


def test_score_sidecar_no_crash():
    vid = _mk("v40000000001", "videos", "m4.mp4", tags="job:J4")
    q = core.expand_query("去除字幕只留背景")
    qt = core._tokens(q)
    core._score(qt, core.get_material(vid))   # 侧通道 bigram 收紧不抛异常


def test_log_feedback_carries_lang():
    r = core.log_feedback("deliver", True, by="agent", lang="ja",
                          orig_query="日本語字幕を出力", translated_query="导出日文")
    assert "error" not in r


def test_feedback_veto_blocks_brief_query():
    """步骤 10:被否决的译后检索词(户内对话)注册后,组装同目标时该词不再进 brief。"""
    # 模拟:用户在英文界面否决 'indoor dialogue' 的译后词 '户内对话'(跨语言对齐)
    core.add_veto("户内对话")
    assert "户内对话" in core.list_vetoes()
    # 简体组装同一目标,被否决的译后词不得再作为 brief 查询
    res = agent._assemble_package("户内对话", model=None, scope="all", limit=10)
    assert "户内对话" not in res["queries"], res["queries"]


def test_feedback_rejection_is_recorded():
    """否决被写入反馈日志(译后词可追溯),供诊断/对齐;且显式否决才登记 veto。"""
    r = core.log_feedback("assemble_query", False, lang="en",
                          orig_query="indoor dialogue", translated_query="户内对话")
    assert "error" not in r
    # 日志读得到被否决的译后词(诊断/对齐用)
    assert "户内对话" in core.feedback_vetoed_queries()
    # 仅显式 add_veto 才进入实时否决集;普通日志 reject 不会误伤组装
    assert "户内对话" not in core.list_vetoes()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
