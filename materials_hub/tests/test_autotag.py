"""自动打标离线测试(monkeypatch 掉网络与 DB,不依赖 ollama chat 模型)。

运行: python tests/test_autotag.py
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

# 触发素材表迁移(幂等):确保 ai_tags 等新列存在,避免直接写库用例因缺列报错。
core._init_db()


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
    # 词法 tags 列完全不被 LLM 打标触碰(防污染)
    assert "tags" not in written, written
    # LLM 标签写入独立 ai_tags 列,并追加新标签(去重)
    assert "dehardsub" in written["ai_tags"].split(","), written
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
    # 自包含:桩掉 all_materials,不依赖真实库里恰好有 5 条素材
    orig_cm, orig_all = core.chat_models, core.all_materials
    core.chat_models = lambda: []
    core.all_materials = lambda: [{"id": "m%d" % i, "name": "v%d.mp4" % i}
                                  for i in range(5)]
    try:
        res = core.auto_tag_all(limit=5)
        assert all(r["status"] == "skipped" for r in res) and len(res) == 5
    finally:
        core.chat_models, core.all_materials = orig_cm, orig_all


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


def test_normalize_llm_tags_guard():
    raw = ["Video", "媒体", "dehardsub", "dehardsub", "scene", "type:render",
           "sp", "job:9", "land scape", "face_fix", ("a" * 25), "人物"]
    out = core._normalize_llm_tags(raw)
    # 通用噪声标签(中英文)一律丢弃
    assert "video" not in out and "媒体" not in out and "scene" not in out
    assert "人物" not in out
    # 系统溯源标签不动、不写入(冒号会被字符过滤剥掉,故按无冒号前缀严格校验)
    assert all(not t.startswith(("type", "job", "sp")) for t in out), out
    assert "job9" not in out and "typerender" not in out
    # 有意义的语义标签保留
    assert "dehardsub" in out
    assert "land-scape" in out          # 空格 → 连字符
    assert "face-fix" in out            # 下划线 → 连字符
    assert ("a" * 25) not in out        # 超长(>20)丢弃
    assert out.count("dehardsub") == 1  # 去重保序
    assert len(out) <= core._LLM_TAG_MAX


def test_normalize_drops_job_type_prefix_leak():
    # 回归: 2026-09-28 验证发现的漏洞。字符过滤会剥掉冒号,
    # job:fetch/type:render 若只按 "job:"/"type:" 校验会漏过(变成 jobfetch/typerender 入列)。
    # 归一化后必须彻底丢弃, 不得有任何 type*/job* 前缀泄漏。
    out = core._normalize_llm_tags(
        ["job:fetch", "type:render", "Video", "媒体", "scene", "fix", "good", "Unknown"])
    assert out == [], out
    # 即便模型漏写冒号(typerender)也应拦掉
    out2 = core._normalize_llm_tags(["typerender", "jobfetch", "deblur", "segment"])
    assert "typerender" not in out2 and "jobfetch" not in out2, out2
    assert "deblur" in out2 and "segment" in out2


def test_autotag_drops_noise_tags_on_write():
    written = {}
    _patch(
        lambda url, payload=None, timeout=90:
            {"response": '{"desc":"人物视频素材","tags":["video","媒体","dehardsub","scene"]}'},
        getm=lambda mid: {"id": mid, "name": "a.png", "tags": "sp,type:render",
                          "description": "已有较长描述内容超过二十个字符用于测试覆盖逻辑"},
        upd=lambda mid, **f: written.update(f),
    )
    res = core.auto_tag_material({"id": "x", "name": "a.png"}, models=["llama3"])
    assert res["status"] == "ok", res
    # 词法 tags 列不被触碰
    assert "tags" not in written, written
    # LLM 标签经护栏后写入 ai_tags:好标签留,噪声(视频/媒体/scene)丢弃
    ai = written["ai_tags"].split(",")
    assert "dehardsub" in ai
    assert "video" not in ai and "媒体" not in ai and "scene" not in ai


def test_auto_tag_all_snapshots_backup():
    bak = core.AUTOTAG_BACKUP
    tmp_bak = bak + ".test"
    core.AUTOTAG_BACKUP = tmp_bak
    orig_all, orig_get, orig_http, orig_cm = (
        core.all_materials, core.get_material, core._http_json, core.chat_models)
    try:
        if os.path.exists(tmp_bak):
            os.remove(tmp_bak)
        mats = [{"id": "x", "name": "a.png"}, {"id": "y", "name": "b.png"}]
        core.all_materials = lambda: mats
        core.get_material = lambda mid: {"id": mid, "name": "a.png",
                                         "tags": "sp,type:render", "description": "x" * 30}
        core._http_json = lambda url, payload=None, timeout=90: \
            {"response": '{"desc":"d","tags":["dehardsub"]}'}
        core.chat_models = lambda: ["llama3"]
        res = core.auto_tag_all(limit=2)
        assert all(r["status"] == "ok" for r in res), res
        assert os.path.exists(tmp_bak), "应生成 tags 快照备份"
        with open(tmp_bak, encoding="utf-8") as f:
            snap = json.load(f)
        assert snap["x"]["tags"] == "sp,type:render"
    finally:
        core.AUTOTAG_BACKUP = bak
        core.all_materials, core.get_material = orig_all, orig_get
        core._http_json, core.chat_models = orig_http, orig_cm
        if os.path.exists(tmp_bak):
            os.remove(tmp_bak)


def test_autotag_undo_restores_backup():
    bak = core.AUTOTAG_BACKUP
    tmp_bak = bak + ".test"
    core.AUTOTAG_BACKUP = tmp_bak
    orig_get, orig_upd = core.get_material, core._update_material
    try:
        if os.path.exists(tmp_bak):
            os.remove(tmp_bak)
        snap = {"x": {"tags": "sp,type:render", "ai_tags": ""},
                "y": {"tags": "sp", "ai_tags": ""}}
        with open(tmp_bak, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        restored = {}
        # y 当前已被改(tags 多了 dehardsub、ai_tags 多了 dehardsub),x 未变 → 只应恢复 y
        core.get_material = lambda mid: {"id": mid,
                                         "tags": "sp,type:render,dehardsub" if mid == "y"
                                         else "sp,type:render",
                                         "ai_tags": "dehardsub" if mid == "y" else ""}
        core._update_material = lambda mid, **f: restored.update({mid: f})
        n = core.autotag_undo()
        assert n == 1, n
        assert restored.get("y", {}).get("tags") == "sp", restored
        assert restored.get("y", {}).get("ai_tags") == "", restored
        assert not os.path.exists(tmp_bak), "撤销后应删除备份文件"
    finally:
        core.AUTOTAG_BACKUP = bak
        core.get_material, core._update_material = orig_get, orig_upd
        if os.path.exists(tmp_bak):
            os.remove(tmp_bak)


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
