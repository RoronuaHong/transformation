"""自动打标离线测试(monkeypatch 掉网络与 DB,不依赖 ollama chat 模型)。

运行: python tests/test_autotag.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402


def _patch(http, getm=None, upd=None, allm=None):
    core._http_json = http
    if getm is not None:
        core.get_material = getm
    if upd is not None:
        core._update_material = upd
    if allm is not None:
        core.all_materials = allm


def test_no_chat_model_skipped():
    res = core.auto_tag_material({"id": "x", "name": "a.png"}, models=[])
    assert res["status"] == "skipped", res


def test_dry_returns_suggestion_no_write():
    called = {}
    _patch(lambda url, payload=None, timeout=90: called.update(url=url)
           or {"response": '{"desc":"一段中文描述","tags":["dehardsub","subs"]}'})
    res = core.auto_tag_material({"id": "x", "name": "a.png"}, dry=True, models=["llama3"])
    assert res["status"] == "dry", res
    assert res["desc"] == "一段中文描述"
    assert "dehardsub" in res["tags"] and "subs" in res["tags"]
    assert called.get("url", "").endswith("/api/generate")


def test_real_write_preserves_and_appends():
    written = {}
    _patch(
        lambda url, payload=None, timeout=90: {"response": '{"desc":"新描述","tags":["dehardsub"]}'},
        getm=lambda mid: {"id": mid, "name": "a.png",
                          "tags": "sp,type:render,codeformer",
                          "description": "已有较长描述内容超过二十个字符用于测试覆盖逻辑"},
        upd=lambda mid, **f: written.update(f),
    )
    res = core.auto_tag_material({"id": "x", "name": "a.png"}, models=["llama3"])
    assert res["status"] == "ok", res
    # 标签:保留既有溯源标签 + 追加新标签(去重)
    assert "sp" in written["tags"] and "type:render" in written["tags"]
    assert "codeformer" in written["tags"] and "dehardsub" in written["tags"]
    # 描述:原文>20字 → 保留原文,不覆盖
    assert written["description"] == "已有较长描述内容超过二十个字符用于测试覆盖逻辑"


def test_short_desc_is_overwritten():
    written = {}
    _patch(
        lambda url, payload=None, timeout=90: {"response": '{"desc":"短描述被覆盖","tags":[]}'},
        getm=lambda mid: {"id": mid, "name": "a.png", "tags": "sp", "description": "短"},
        upd=lambda mid, **f: written.update(f),
    )
    core.auto_tag_material({"id": "x", "name": "a.png"}, models=["llama3"])
    assert written["description"] == "短描述被覆盖"


def test_fenced_json_parsed():
    _patch(lambda url, payload=None, timeout=90:
           {"response": '```json\n{"desc":"围栏解析","tags":["out"]}\n```'})
    res = core.auto_tag_material({"id": "x", "name": "a.png"}, dry=True, models=["llama3"])
    assert res["status"] == "dry" and res["desc"] == "围栏解析" and "out" in res["tags"]


def test_parse_error_status():
    _patch(lambda url, payload=None, timeout=90: {"response": "不是合法json"})
    res = core.auto_tag_material({"id": "x", "name": "a.png"}, models=["llama3"])
    assert res["status"] == "parse_error", res


def test_autotag_all_without_model():
    orig = core.chat_models
    core.chat_models = lambda: []
    try:
        res = core.auto_tag_all(limit=5)
        assert all(r["status"] == "skipped" for r in res) and len(res) == 5
    finally:
        core.chat_models = orig


def test_rule_tag_offline_no_model():
    # 规则打标完全离线、不触发任何网络;自包含 get_material 桩,避免受其它测试影响
    orig_get = core.get_material
    m = {"id": "x", "name": "dehardsub_demo.mp4", "kind": "video",
         "tags": "sp,type:test", "description": "", "external_path": "", "rel_path": ""}
    core.get_material = lambda mid: {"id": mid, "name": m["name"], "tags": "", "description": ""}
    try:
        res = core.rule_tag_material(m, dry=True)
    finally:
        core.get_material = orig_get
    assert res["status"] == "dry"
    assert "dehardsub" in res["tags"], res["tags"]
    assert "dehardsub" in res["added"], res["added"]
    assert res["desc"] == "去字幕、保留背景的素材", res["desc"]


def test_rule_tag_preserves_existing_and_fills_desc():
    orig_get, orig_upd = core.get_material, core._update_material
    written = {}
    core.get_material = lambda mid: {"id": mid, "name": "bilibili_video.mp4",
                                     "tags": "sp,bilibili,job:123,type:media",
                                     "description": ""}
    core._update_material = lambda mid, **f: written.update(f)
    m = {"id": "x", "name": "bilibili_video.mp4", "kind": "video",
         "tags": "sp,bilibili,job:123,type:media", "description": "",
         "external_path": "", "rel_path": ""}
    try:
        res = core.rule_tag_material(m)
    finally:
        core.get_material, core._update_material = orig_get, orig_upd
    assert res["status"] == "ok"
    # 既有标签全部保留
    for t in ("sp", "bilibili", "job:123", "type:media"):
        assert t in written["tags"], written["tags"]
    # 描述仅在为空时填充
    assert "B站下载的素材" == written["description"], written["description"]


def test_rule_tag_cleanup_strips_added_keeps_bridge():
    mats = [
        {"id": "a", "tags": "sp,type:render,deblur,test"},     # rule-added: deblur,test
        {"id": "b", "tags": "sp,bilibili,job:1,segment"},        # rule-added: segment; bilibili 保留
        {"id": "c", "tags": "plain,type:media"},                # 无规则标签
    ]
    store = {}
    orig_all, orig_get, orig_upd = core.all_materials, core.get_material, core._update_material
    core.all_materials = lambda: mats
    core.get_material = lambda mid: next(m for m in mats if m["id"] == mid)
    core._update_material = lambda mid, **f: store.update({mid: f["tags"]})
    try:
        n = core.rule_tag_cleanup()
    finally:
        core.all_materials, core.get_material, core._update_material = orig_all, orig_get, orig_upd
    assert n == 3, n  # a: deblur+test(2) + b: segment(1)
    assert "deblur" not in store["a"] and "test" not in store["a"]
    assert "type:render" in store["a"] and "sp" in store["a"]
    assert "bilibili" in store["b"] and "segment" not in store["b"]
    # c 无任何规则标签 → 不应被改动(既不被写,也不被改坏)
    assert "c" not in store or store["c"] == "plain,type:media"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fail = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            fail += 1
            print("FAIL", t.__name__, "->", e)
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(1 if fail else 0)
