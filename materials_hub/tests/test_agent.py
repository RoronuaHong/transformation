"""Deep Agent 离线测试(mock LLM 与 core 检索/写入;不连 ollama、不写真实库)。

覆盖:四支柱(规划/卸载/子代理/系统提示工具表)+ 写护栏 + 状态持久化 + 步数上限。
运行: python tests/test_agent.py
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import agent  # noqa: E402

core._init_db()          # 触发表迁移,确保用例可读库(不写真实素材)

# 工作区指到临时目录(测试全程不碰真实 index/agent_workspace)
_TMP = tempfile.mkdtemp(prefix="hub_agent_test_")
agent.WORKSPACE = _TMP

_ORIG_CHAT = agent._chat
_ORIG = {k: getattr(core, k) for k in
         ("chat_models", "search", "chunk_search", "get_material", "distinct_tags",
          "health", "broken_externals", "update_tags", "ingest_external")}


def _reset(**over):
    for k, v in _ORIG.items():
        setattr(core, k, v)
    agent._chat = _ORIG_CHAT
    for k, v in over.items():
        if k == "_chat":
            agent._chat = v
        else:
            setattr(core, k, v)


class Script:
    """按序回放 LLM JSON 响应;记录每次调用的 system 前缀供断言。"""

    def __init__(self, responses):
        self.q = list(responses)
        self.calls = []

    def __call__(self, messages, model, timeout=180):
        self.calls.append(messages[0]["content"][:24])
        return json.loads(self.q.pop(0))


def _fake_search(items):
    def f(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        return items[:limit or len(items)]
    return f


def _mat(mid, desc="d"):
    return {"id": mid, "kind": "videos", "ext": ".mp4", "name": mid + ".mp4",
            "size": 1, "tags": "sp", "ai_tags": "", "description": desc,
            "location": "external", "external_path": "x/" + mid + ".mp4"}


def test_no_chat_model_skipped():
    before = set(os.listdir(_TMP))
    _reset(chat_models=lambda: [])
    r = agent.agent_run("巡检")
    assert r["status"] == "skipped" and r["reason"] == "no_chat_model", r
    assert set(os.listdir(_TMP)) == before, "skipped 时不应创建新工作区"


def test_happy_path_plan_search_finish():
    written_search = [_mat("m1"), _mat("m2")]
    _reset(chat_models=lambda: ["fake"], search=_fake_search(written_search))
    s = Script([
        '{"todos":[{"id":1,"text":"检索B站视频"},{"id":2,"text":"总结"}]}',
        '{"thought":"先搜","action":"search_materials","args":{"q":"bilibili","kind":"videos"}}',
        '{"action":"finish","args":{"summary":"找到2条"},"mark_done":[1,2]}',
    ])
    agent._chat = s
    r = agent.agent_run("找出B站视频并总结")
    assert r["status"] == "done", r
    assert r["steps"] == 2 and r["summary"] == "找到2条"
    assert all(t["status"] == "done" for t in r["todos"]), r["todos"]
    assert s.calls[0].startswith("你是素材库任务的规划器"), s.calls[0]
    assert s.calls[1].startswith("你是「素材中心」的 Deep Agent")
    # 状态持久化 + agent_status 回读
    st = agent.agent_status(r["task_id"])
    assert st["status"] == "done" and st["steps"] == 2 and st["summary"] == "找到2条"
    assert st["todos"] == r["todos"]


def test_offload_large_observation():
    big = [_mat("m%02d" % i, desc="很长的描述" * 60) for i in range(40)]
    _reset(chat_models=lambda: ["fake"], search=_fake_search(big))
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"检索"}]}',
        '{"action":"search_materials","args":{"q":"x","limit":40}}',
        '{"action":"finish","args":{"summary":"ok"},"mark_done":[1]}',
    ])
    r = agent.agent_run("大结果检索")
    assert r["status"] == "done" and r["notes"] == 1, r
    ws = agent._ws(r["task_id"])
    note = os.path.join(ws, "notes", "step_000.json")
    assert os.path.isfile(note), "超长观察必须落盘"
    with open(note, encoding="utf-8") as f:
        assert len(f.read()) > agent._OFFLOAD_CHARS
    # state 里的观察是指针,不是全文
    with open(os.path.join(ws, "state.json"), encoding="utf-8") as f:
        st = json.load(f)
    assert st["steps"][0]["obs"].startswith("[已卸载→notes/step_000.json")
    assert len(st["steps"][0]["obs"]) < agent._OFFLOAD_CHARS


def test_write_blocked_without_allow_write():
    called = []
    _reset(chat_models=lambda: ["fake"], search=_fake_search([]),
           update_tags=lambda mid, tags: called.append((mid, tags)) or {"ok": True})
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"写标签"}]}',
        '{"action":"update_tags","args":{"id":"m1","tags":"a,b"}}',
        '{"action":"finish","args":{"summary":"试过了"},"mark_done":[1]}',
    ])
    r = agent.agent_run("给 m1 打标签")          # 缺省只读
    assert r["status"] == "done"
    assert called == [], "无 allow_write 时写调用必须被拒"
    with open(os.path.join(agent._ws(r["task_id"]), "state.json"), encoding="utf-8") as f:
        st = json.load(f)
    assert "unknown action: update_tags" in st["steps"][0]["obs"], st["steps"][0]


def test_write_allowed_with_flag():
    called = []
    _reset(chat_models=lambda: ["fake"], get_material=lambda mid: _mat(mid),
           update_tags=lambda mid, tags: called.append((mid, tags)) or {"ok": True})
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"写标签"}]}',
        '{"action":"update_tags","args":{"id":"m1","tags":"a,b"}}',
        '{"action":"finish","args":{"summary":"写了"},"mark_done":[1]}',
    ])
    r = agent.agent_run("给 m1 打标签", allow_write=True)
    assert called == [("m1", "a,b")], called
    assert r["status"] == "done"


def test_retrieve_subagent_context_isolated():
    hits = [_mat("m1"), _mat("m2")]
    search_calls = []
    def spy_search(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        search_calls.append(q)
        return hits[:limit or len(hits)]
    _reset(chat_models=lambda: ["fake"], search=spy_search)
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"综合检索"}]}',
        '{"action":"retrieve","args":{"goal":"去字幕相关的成片"}}',
        '{"queries":["dehardsub","去除字幕"]}',                       # 子代理第 1 轮
        '{"summary":"子摘要:两条成片","ids":["m1"]}',                  # 子代理第 2 轮
        '{"action":"finish","args":{"summary":"完成"},"mark_done":[1]}',
    ])
    r = agent.agent_run("找去字幕成片")
    assert search_calls == ["dehardsub", "去除字幕"], search_calls
    with open(os.path.join(agent._ws(r["task_id"]), "state.json"), encoding="utf-8") as f:
        st = json.load(f)
    obs = st["steps"][0]["obs"]
    assert "子摘要:两条成片" in obs and "n_hits" in obs, obs
    assert '"description"' not in obs or len(obs) < agent._OFFLOAD_CHARS  # 只回摘要


def test_maintain_deterministic_no_llm():
    _reset(chat_models=lambda: ["fake"],
           # 键名与 core.health() 真实返回一致,锁定契约防再漂移
           health=lambda: {"ok": True, "total": 2, "kinds": {"videos": 2}, "duplicates": 0},
           broken_externals=lambda: [])
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"巡检"}]}',
        '{"action":"maintain","args":{}}',
        '{"action":"finish","args":{"summary":"健康"},"mark_done":[1]}',
    ])
    r = agent.agent_run("巡检素材库")
    assert r["status"] == "done"
    assert len(agent._chat.q) == 0, "maintain 不应额外消耗 LLM 轮次"
    with open(os.path.join(agent._ws(r["task_id"]), "state.json"), encoding="utf-8") as f:
        st = json.load(f)
    obs = json.loads(st["steps"][0]["obs"])
    assert obs["health_ok"] is True and obs["broken"] == 0, obs
    assert "prune" in obs["hint"]


def test_max_steps_guard():
    _reset(chat_models=lambda: ["fake"],
           health=lambda: {"ok": True}, broken_externals=lambda: [])
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"打转"}]}',
        '{"action":"maintain","args":{}}',
        '{"action":"maintain","args":{}}',
    ])
    r = agent.agent_run("永远不finish", max_steps=2)
    assert r["status"] == "max_steps_reached", r
    assert r["steps"] == 2 and "已用尽" in r["summary"] and "agent_resume" in r["summary"]


def test_tool_error_does_not_crash_loop():
    def boom(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        raise RuntimeError("检索后端挂了")
    _reset(chat_models=lambda: ["fake"], search=boom)
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"检索"}]}',
        '{"action":"search_materials","args":{"q":"x"}}',
        '{"action":"finish","args":{"summary":"兜住了"},"mark_done":[1]}',
    ])
    r = agent.agent_run("故意触发异常")
    assert r["status"] == "done", r
    with open(os.path.join(agent._ws(r["task_id"]), "state.json"), encoding="utf-8") as f:
        st = json.load(f)
    assert "RuntimeError: 检索后端挂了" in st["steps"][0]["obs"]
    assert st["steps"][0]["ok"] is False


def test_finish_drops_unseen_ids():
    written = [_mat("m1"), _mat("m2")]
    _reset(chat_models=lambda: ["fake"], search=_fake_search(written))
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"检索"}]}',
        '{"action":"search_materials","args":{"q":"bilibili"}}',
        '{"action":"finish","args":{"summary":"找到两条","ids":["m1","fake999"]},"mark_done":[1]}',
    ])
    r = agent.agent_run("找出B站视频")
    assert r["status"] == "done"
    assert r["result_ids"] == ["m1"], r["result_ids"]
    assert r["dropped_ids"] == ["fake999"], r["dropped_ids"]
    assert "[核验]" in r["summary"] and "fake999" in r["summary"]
    st = agent.agent_status(r["task_id"])
    assert st["dropped_ids"] == ["fake999"], st["dropped_ids"]


def test_related_tool():
    m1 = _mat("m1"); m1["tags"] = "sp,job:J1,role:master"
    m2 = _mat("m2"); m2["tags"] = "parent:m1"
    m3 = _mat("m3"); m3["tags"] = "job:J1"
    m4 = _mat("m4"); m4["tags"] = "role:master"
    pool = [m1, m2, m3, m4]

    def fake_get(mid):
        return {"m1": m1, "m2": m2, "m3": m3, "m4": m4}.get(mid)

    def fake_search(q="", kind="", tag="", limit=None, offset=0, mode="auto"):
        if tag:
            return [x for x in pool if tag in (x.get("tags") or "")]
        if kind:
            return [x for x in pool if x.get("kind") == kind]
        return list(pool)

    _reset(chat_models=lambda: ["fake"], get_material=fake_get, search=fake_search)
    r = agent._tool_related({"id": "m1", "rel": "all"})
    assert r["count"] == 3, r
    assert {x["id"] for x in r["related"]} == {"m2", "m3", "m4"}, r
    r2 = agent._tool_related({"id": "m1", "rel": "children"})
    assert [x["id"] for x in r2["related"]] == ["m2"], r2
    r3 = agent._tool_related({"id": "nope"})
    assert "error" in r3, r3


def test_record_memory_playbook():
    import os as _os
    m = _mat("m1", desc="去马赛克成片"); m["tags"] = "sp,type:deblur"
    _reset(chat_models=lambda: ["fake"],
           get_material=lambda mid: m if mid == "m1" else None,
           search=_fake_search([m]))
    if _os.path.exists(agent._MEMORY_PATH):
        _os.remove(agent._MEMORY_PATH)
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"检索"}]}',
        '{"action":"search_materials","args":{"q":"去马赛克"}}',
        '{"action":"finish","args":{"summary":"找到","ids":["m1"]},"mark_done":[1]}',
    ])
    r = agent.agent_run("找去马赛克成片")
    assert r["status"] == "done"
    assert _os.path.exists(agent._MEMORY_PATH), "成功任务应沉淀记忆"
    pb = agent._load_playbook()
    assert any(e["q"] == "去马赛克" for e in pb), pb
    # 第二次同查询 → n 累加,且 hint 含有效 facet
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"检索"}]}',
        '{"action":"search_materials","args":{"q":"去马赛克"}}',
        '{"action":"finish","args":{"summary":"找到","ids":["m1"]},"mark_done":[1]}',
    ])
    agent.agent_run("再找去马赛克成片")
    pb2 = agent._load_playbook()
    e = next(x for x in pb2 if x["q"] == "去马赛克")
    assert e["n"] == 2, e
    assert "type:deblur" in e["hint"], e


def test_finish_citations_verified():
    written = [_mat("m1")]
    _reset(chat_models=lambda: ["fake"], search=_fake_search(written))
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"检索"}]}',
        '{"action":"search_materials","args":{"q":"x"}}',
        '{"action":"finish","args":{"summary":"ok",'
        '"citations":[{"claim":"成片","id":"m1"},{"claim":"不存在","id":"deadbeef0000"}]},'
        '"mark_done":[1]}',
    ])
    r = agent.agent_run("检索")
    assert r["status"] == "done"
    cits = {c["id"]: c["valid"] for c in r["citations"]}
    assert cits == {"m1": True, "deadbeef0000": False}, cits


def test_finish_critic_flags_unverified_ids():
    written = [_mat("m1")]
    _reset(chat_models=lambda: ["fake"], search=_fake_search(written))
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"检索"}]}',
        '{"action":"search_materials","args":{"q":"x"}}',
        '{"action":"finish","args":{"summary":"已处理 80ad93332bca 与 999999999999"},"mark_done":[1]}',
    ])
    r = agent.agent_run("检索")
    assert r["status"] == "done"
    assert set(r["critic"]["unverified_ids"]) == {"80ad93332bca", "999999999999"}, r["critic"]
    assert "[Critic]" in r["summary"]
    st = agent.agent_status(r["task_id"])
    assert st["critic"]["unverified_ids"] == r["critic"]["unverified_ids"]


def test_context_compression_rolling_digest():
    _reset(chat_models=lambda: ["fake"])  # maintain 确定性,无需检索后端
    script = ['{"todos":[{"id":1,"text":"巡检"}]}']
    script += ['{"action":"maintain"}'] * 8
    script += ['{"action":"finish","args":{"summary":"done"},"mark_done":[1]}']
    agent._chat = Script(script)
    r = agent.agent_run("巡检")
    assert r["status"] == "done"
    dig = r["context_digest"]
    assert dig, "长任务应产生滚动摘要"
    assert "step1[" in dig, dig          # 最早被逐出的步骤应进入摘要
    assert "step8[" not in dig, dig      # 最近尾窗口(6)内步骤不进摘要
    assert len(dig.splitlines()) >= 2, dig  # step1、step2 先后被逐出并压缩


def test_agent_cancel_cooperative():
    _reset(chat_models=lambda: ["fake"], search=_fake_search([]),
           health=lambda: {"ok": True, "total": 0, "kinds": {}, "duplicates": 0},
           broken_externals=lambda: [])
    base = Script([
        '{"todos":[{"id":1,"text":"巡检"}]}',
        '{"action":"maintain","args":{}}',
        '{"action":"finish","args":{"summary":"done"},"mark_done":[1]}',
    ])
    calls = {"n": 0}

    def chat(messages, model, timeout=180):
        calls["n"] += 1
        if calls["n"] == 2:                      # 第 1 步执行期间请求取消
            cr = agent.agent_cancel("agcanceltest")
            assert cr["cancelled"] is True, cr
        return base(messages, model, timeout)

    agent._chat = chat
    r = agent.agent_run("巡检", task_id="agcanceltest")
    assert r["status"] == "cancelled", r
    assert r["steps"] == 1, r                    # 第 2 步边界即终止,后续步骤未执行
    assert "取消" in r["summary"], r
    st = agent.agent_status("agcanceltest")
    assert st["status"] == "cancelled", st
    assert not os.path.exists(os.path.join(agent._ws("agcanceltest"), "cancel.flag"))
    cr2 = agent.agent_cancel("agcanceltest")     # 已终止的任务再取消 → 拒绝
    assert cr2["cancelled"] is False, cr2


def test_agent_list_and_cleanup():
    _reset(chat_models=lambda: ["fake"], search=_fake_search([]),
           health=lambda: {"ok": True, "total": 0, "kinds": {}, "duplicates": 0},
           broken_externals=lambda: [])
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"t"}]}',
        '{"action":"finish","args":{"summary":"ok"},"mark_done":[1]}',
    ])
    r = agent.agent_run("lctest", task_id="aglctest")
    assert r["status"] == "done", r
    me = [x for x in agent.agent_list() if x["task_id"] == "aglctest"]
    assert me and me[0]["status"] == "done" and me[0]["steps"] == 1, me
    # max_age=0 → 终态任务全删
    cr = agent.agent_cleanup(max_age_hours=0)
    assert "aglctest" in cr["removed"], cr
    assert not os.path.isdir(agent._ws("aglctest")), "终态工作区应被清理"
    # running 任务永不清删,即使超龄
    ws = agent._ws("agprotect")
    os.makedirs(ws, exist_ok=True)
    agent._save({"task": "x", "status": "running", "steps": [], "created_at": "2000-01-01"}, ws)
    cr2 = agent.agent_cleanup(max_age_hours=0)
    assert "agprotect" not in cr2["removed"], cr2
    assert os.path.isdir(ws), "running 工作区不得被清理"


def test_agent_resume_after_max_steps():
    _reset(chat_models=lambda: ["fake"], search=_fake_search([]),
           health=lambda: {"ok": True, "total": 0, "kinds": {}, "duplicates": 0},
           broken_externals=lambda: [])
    # 第一次:max_steps=1 只够规划+maintain,来不及 finish → max_steps_reached
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"巡检"}]}',
        '{"action":"maintain","args":{}}',
    ])
    r = agent.agent_run("巡检", task_id="agresumetest", max_steps=1)
    assert r["status"] == "max_steps_reached", r
    # 续跑:接续步号 finish → done,不重新规划
    agent._chat = Script([
        '{"action":"finish","args":{"summary":"续跑完成","ids":[]},"mark_done":[1]}',
    ])
    r2 = agent.agent_resume("agresumetest", extra_steps=3)
    assert r2["status"] == "done", r2
    st = agent.agent_status("agresumetest")
    assert st["status"] == "done" and st["steps"] == 2, st          # 1 原始 + 1 续跑,步号接续
    # done 任务不可再续跑
    r3 = agent.agent_resume("agresumetest")
    assert "error" in r3, r3


def test_search_recall_fallback():
    # 挑剔检索:只有空 q(浏览模式)或含扩展词(mosaic)才命中;tag=nope 时全路径 0 命中
    def picky(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        if tag == "nope":
            return []
        if not q or "mosaic" in q:
            return [_mat("m9", desc="mosaic doc")]
        return []

    _reset(chat_models=lambda: ["fake"], search=picky)
    r = agent._tool_search({"q": "马赛克处理"})           # 0 命中 → 同义词扩展加 mosaic → 命中
    assert isinstance(r, list) and r[0]["id"] == "m9", r
    r2 = agent._tool_search({"q": "find documents now"})  # 纯英文无同义词 → 浏览兜底 dict
    assert isinstance(r2, dict) and r2.get("fallback") == "browse", r2
    assert [x["id"] for x in r2["results"]] == ["m9"], r2
    assert agent._collect_ids(r2) == ["m9"], "兜底结果 id 必须可进 seen_ids 白名单"
    r3 = agent._tool_search({"q": "x", "tag": "nope"})    # 全路径 0 命中 → 原 0-hit hint
    assert isinstance(r3, dict) and r3["count"] == 0 and "hint" in r3, r3


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_agent: %d/%d green" % (len(fns), len(fns)))
