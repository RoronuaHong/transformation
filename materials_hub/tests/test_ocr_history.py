# -*- coding: utf-8 -*-
"""test_ocr_history.py — 写操作审计(history 表)与画面 OCR 集成的离线测试。

全 mock 驱动:库用临时文件,不连 ollama、不动真实索引与素材。复跑:python tests/test_ocr_history.py
"""
import os
import sys
import json
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

_TMP = None


def _setup():
    """惰性初始化临时环境(直接跑与 pytest 均可用)。"""
    global _TMP
    if _TMP is None:
        _TMP = tempfile.mkdtemp(prefix="hub_test_")
        core.INDEX_DB = os.path.join(_TMP, "hub.db")
        core.OCR_DIR = os.path.join(_TMP, "ocr")
        core._init_db()


def _teardown():
    global _TMP
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP = None


def _mk_material(mid, kind="videos", name="v.mp4"):
    _setup()
    # 用 external 引用临时空文件,remove 时只删索引、绝不触发磁盘移动
    f = os.path.join(_TMP, f"{mid}_{name}")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("x")
    core.add_material(id=mid, kind=kind, ext=os.path.splitext(name)[1], name=name,
                      rel_path="", size=1, sha256="x" * 64, tags="", description="",
                      source="", orig_name=name, created_at="2026-01-01")
    # add_material 的 INSERT 不含 location 列,须显式补写为 external(删除只动索引)
    core._update_material(mid, location="external", external_path=f)
    return core.get_material(mid)


def test_history_records_write_ops():
    _mk_material("h1")
    core.update_tags("h1", "a,b")
    core.update_description("h1", "new desc")
    hs = core.get_history(limit=10)
    actions = [h["action"] for h in hs]
    assert "update_tags" in actions and "update_description" in actions, actions
    top = hs[0]
    assert top["ts"] and top["target_id"] == "h1" and top["actor"] == "app"
    assert "->" in top["detail"]
    # target 过滤
    assert all(h["target_id"] == "h1" for h in core.get_history(target_id="h1"))
    print("PASS test_history_records_write_ops")


def test_history_remove_and_order():
    _mk_material("h2")
    _mk_material("h3")
    core.remove_material("h2")
    core.remove_material("h3")
    hs = core.get_history(limit=2)
    assert [h["action"] for h in hs] == ["remove", "remove"], hs
    assert hs[0]["target_id"] == "h3" and hs[1]["target_id"] == "h2"  # 倒序
    assert "location=external" in hs[0]["detail"]
    print("PASS test_history_remove_and_order")


def test_ocr_sidecar_in_material_text():
    _mk_material("o1")
    os.makedirs(core.OCR_DIR, exist_ok=True)
    sc = core.ocr_sidecar_path("o1")
    assert sc and sc.endswith("o1.txt")
    with open(sc, "w", encoding="utf-8") as f:
        f.write("[0.5s] 画面里的检测文字 OCR-TEST-123")
    m = core.get_material("o1")
    txt = core._material_text(m)
    assert "OCR-TEST-123" in txt, txt          # sidecar 进可检索全文
    # 路径注入防护:含分隔符的 id 拒绝生成 sidecar 路径
    assert core.ocr_sidecar_path("../evil") is None
    assert core.ocr_sidecar_path("") is None
    print("PASS test_ocr_sidecar_in_material_text")


def test_ocr_material_cached_and_guards():
    _mk_material("o2", kind="docs", name="d.md")     # 非 video/image → skipped
    r = core.ocr_material("o2")
    assert r["status"] == "skipped" and "kind=" in r["reason"], r
    _mk_material("o3")
    sc = core.ocr_sidecar_path("o3")
    os.makedirs(core.OCR_DIR, exist_ok=True)
    with open(sc, "w", encoding="utf-8") as f:
        f.write("cached text")
    r = core.ocr_material("o3")                       # 已有 sidecar → cached,不调子进程
    assert r["status"] == "cached" and r["chars"] > 0, r
    _mk_material("o4")
    orig_ff = core.ffmpeg_path                        # force 且无 ffmpeg → skipped(打桩防真跑)
    core.ffmpeg_path = lambda: None
    try:
        r2 = core.ocr_material("o4", force=True)
    finally:
        core.ffmpeg_path = orig_ff
    assert r2["status"] == "skipped" and r2["reason"] == "no_ffmpeg", r2
    print("PASS test_ocr_material_cached_and_guards")


def test_parse_ocr_runner_stdout():
    raw = b'2026-01-01 log line\n[{"file":"a.jpg","text":"hello"},{"file":"b.jpg","text":""}]'
    data = core._parse_ocr_runner_stdout(raw)
    assert data and data[0]["text"] == "hello", data
    assert core._parse_ocr_runner_stdout(b"garbage") is None
    assert core._parse_ocr_runner_stdout(b'{"error":"x"}') is None  # 对象非数组 → None
    print("PASS test_parse_ocr_runner_stdout")


def test_ocr_lexical_low_weight_channel():
    """OCR 走独立低权重词法通道:稀有画面文字能命中,但权重远低于 name/description。"""
    _mk_material("o5", name="zzz.mp4")
    os.makedirs(core.OCR_DIR, exist_ok=True)
    sc = core.ocr_sidecar_path("o5")
    token = "qiaomaoguantou"                   # 只出现在 OCR sidecar 里的稀有 token
    with open(sc, "w", encoding="utf-8") as f:
        f.write("[1.0s] 画面文字 " + token)
    core._OCR_TEXT_CACHE.pop("o5", None)
    m = core.get_material("o5")
    s_ocr = core._score(core._tokens(token), m)
    assert s_ocr > 0, s_ocr                   # 稀有画面文字仍可被词法命中
    # 但通道是低权重:name 命中(3.0)显著高于仅 OCR 命中(0.35)
    m_name = dict(m, name=token + ".mp4")
    s_name = core._score(core._tokens(token), m_name)
    assert s_ocr < s_name, (s_ocr, s_name)
    assert core._OCR_FIELD_WEIGHT < core._FIELD_WEIGHT["description"]
    # 语义文档文本也收录 OCR
    assert token in core.doc_text(m), core.doc_text(m)
    print("PASS test_ocr_lexical_low_weight_channel")


def test_ocr_text_cache_invalidates_on_mtime():
    """_ocr_text 缓存按 (mtime,size) 失效:长驻进程在另一进程重跑 OCR 后不会读旧文本。"""
    _mk_material("o6")
    os.makedirs(core.OCR_DIR, exist_ok=True)
    sc = core.ocr_sidecar_path("o6")
    core._OCR_TEXT_CACHE.pop("o6", None)
    with open(sc, "w", encoding="utf-8") as f:
        f.write("first-ocr")
    assert "first-ocr" in core._ocr_text("o6")
    with open(sc, "w", encoding="utf-8") as f:
        f.write("second-ocr")
    st = os.stat(sc)
    os.utime(sc, (st.st_atime + 10, st.st_mtime + 10))   # 强制 mtime 变化
    assert "second-ocr" in core._ocr_text("o6"), core._ocr_text("o6")
    print("PASS test_ocr_text_cache_invalidates_on_mtime")


if __name__ == "__main__":
    test_history_records_write_ops()
    test_history_remove_and_order()
    test_ocr_sidecar_in_material_text()
    test_ocr_material_cached_and_guards()
    test_parse_ocr_runner_stdout()
    test_ocr_lexical_low_weight_channel()
    test_ocr_text_cache_invalidates_on_mtime()
    _teardown()
    print("OK")
