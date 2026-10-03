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

# 工作区/对话线程指到临时目录(测试全程不碰真实 index/)
_TMP = tempfile.mkdtemp(prefix="hub_agent_test_")
agent.WORKSPACE = _TMP
agent.CHATS = os.path.join(_TMP, "chats")

_ORIG_CHAT = agent._chat
_ORIG = {k: getattr(core, k) for k in
         ("chat_models", "search", "chunk_search", "get_material", "get_shots",
          "distinct_tags", "health", "broken_externals", "update_tags",
          "ingest_external")}


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
           update_tags=lambda mid, tags, **kw: called.append((mid, tags)) or {"ok": True})
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
           update_tags=lambda mid, tags, **kw: called.append((mid, tags)) or {"ok": True})
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"写标签"}]}',
        '{"action":"update_tags","args":{"id":"m1","tags":"a,b"}}',
        '{"action":"finish","args":{"summary":"写了"},"mark_done":[1]}',
    ])
    r = agent.agent_run("给 m1 打标签", allow_write=True)
    # 合并语义:保留 _mat 默认 sp + 追加 a,b
    assert called == [("m1", "sp,a,b")], called
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


def test_repeat_action_short_circuits():
    calls = []

    def spy(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        calls.append(q)
        return [_mat("m1")]

    _reset(chat_models=lambda: ["fake"], search=spy)
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"搜"}]}',
        '{"action":"search_materials","args":{"q":"厨房"}}',
        '{"action":"search_materials","args":{"q":"厨房"}}',
        '{"action":"finish","args":{"summary":"好了"},"mark_done":[1]}',
    ])
    r = agent.agent_run("搜厨房")
    assert r["status"] == "done", r
    assert calls == ["厨房"], calls
    with open(os.path.join(agent._ws(r["task_id"]), "state.json"), encoding="utf-8") as f:
        st = json.load(f)
    assert st["steps"][1].get("repeat") is True, st["steps"][1]
    assert "repeat" in st["steps"][1]["obs"], st["steps"][1]["obs"]


def test_stall_break_recovers_shots_related():
    """软 repeat 后再复读 → 硬停转补跑 get_shots/related 并 finish(防 7B 空耗步数)。"""
    mid = "c9fec3b84e4e"
    shots_n, related_via_search = [], []

    def fake_get(m):
        return _mat(mid) if m == mid else None

    def fake_shots(m):
        shots_n.append(m)
        return {"duration": 12.5, "threshold": "0.5",
                "scenes": [{"start": 0.0, "end": 3.2}, {"start": 3.2, "end": 8.0}]}

    def fake_search(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        if tag == "parent:" + mid:
            related_via_search.append(tag)
            return [_mat("childsilent01")]
        return []

    _reset(chat_models=lambda: ["fake"], get_material=fake_get,
           get_shots=fake_shots, search=fake_search)
    # 任务不含「id=… kind=videos」行 → 不走 attached_video 工作流,专测 LoopGuard
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"元数据"},{"id":2,"text":"镜头"}]}',
        '{"action":"get_material","args":{"id":"%s"}}' % mid,
        '{"action":"get_material","args":{"id":"%s"}}' % mid,  # soft repeat
        '{"action":"get_material","args":{"id":"%s"}}' % mid,  # stall break
        '{"action":"finish","args":{"summary":"不应走到"}}',   # 不应消耗
    ])
    r = agent.agent_run("分析素材 %s 的镜头" % mid, max_steps=8)
    assert r["status"] == "done", r
    assert "硬停转" in (r.get("summary") or "") or "停转" in (r.get("summary") or ""), r
    assert shots_n == [mid], shots_n
    assert related_via_search == ["parent:" + mid], related_via_search
    assert mid in (r.get("result_ids") or []), r
    with open(os.path.join(agent._ws(r["task_id"]), "state.json"), encoding="utf-8") as f:
        st = json.load(f)
    acts = [s.get("action") for s in st["steps"]]
    assert "get_shots" in acts and "related" in acts and acts[-1] == "finish", acts
    assert any(s.get("stall_break") for s in st["steps"]), st["steps"]
    assert len(agent._chat.q) == 1, "stall 后应不再继续问 LLM: %s" % agent._chat.q


def test_attached_video_skill_workflow_finishes():
    """上传视频任务走确定性方案 A 工作流,不依赖 LLM 工具调用。"""
    mid = "c9fec3b84e4e"
    shots_n = []

    def fake_get(m):
        return dict(_mat(mid), name="AttackOnTitan.mp4",
                    tags="role:master,has_audio:1") if m == mid else None

    def fake_shots(m):
        shots_n.append(m)
        return {"duration": 100.0, "scenes": [
            {"start": 0, "end": 5}, {"start": 5, "end": 20},
            {"start": 20, "end": 55}, {"start": 55, "end": 90},
            {"start": 90, "end": 100},
        ]}

    def fake_search(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        if tag == "parent:" + mid:
            return [dict(_mat("silenteeeeee"), kind="silent")]
        return []

    _reset(chat_models=lambda: ["fake"], get_material=fake_get,
           get_shots=fake_shots, search=fake_search)
    # plan 仍会调一次 LLM;工作流直接 finish,不再进主循环
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"分析"},{"id":2,"text":"裁剪"}]}',
        '{"action":"finish","args":{"summary":"不应走到主循环"}}',
    ])
    task = (
        "用户刚上传素材到素材中心（已入库）：\n"
        "- id=%s kind=videos name=AttackOnTitan.mp4 ingest=added\n\n"
        "规则：\n- 只使用真实 id\n\n"
        "用户请求：按照最佳的裁剪方式设计方案"
    ) % mid
    r = agent.agent_run(task, max_steps=8)
    assert r["status"] == "done", r
    assert r.get("skill") == "attached_video", r
    assert "方案A工作流" in (r.get("summary") or ""), r.get("summary")
    assert "推荐裁剪窗口" in (r.get("summary") or ""), r.get("summary")
    assert shots_n == [mid], shots_n
    assert mid in (r.get("result_ids") or []), r
    assert len(agent._chat.q) == 2, "工作流跳过规划/主循环,LLM 队列应未消耗: %s" % agent._chat.q
    assert len(r.get("todos") or []) >= 5, r.get("todos")
    assert all(t.get("status") == "done" for t in r["todos"]), r["todos"]
    assert (r.get("progress") or {}).get("pct") == 100, r.get("progress")
    with open(os.path.join(agent._ws(r["task_id"]), "state.json"), encoding="utf-8") as f:
        st = json.load(f)
    acts = [s.get("action") for s in st["steps"]]
    assert acts[:3] == ["get_material", "related", "get_shots"], acts
    assert "crop_plan" in acts and acts[-1] == "finish", acts


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
    # 无效 kind(document/video)会被校验丢弃,不拖累浏览兜底(实测 7B 会传错)
    def kind_stub(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        return [] if q else [_mat("m8", desc="any")]   # 仅浏览模式(q="")命中
    _reset(chat_models=lambda: ["fake"], search=kind_stub)
    r4 = agent._tool_search({"q": "随便什么", "kind": "document"})
    assert isinstance(r4, dict) and r4.get("fallback") == "browse", r4
    assert "document" in r4["hint"], r4                   # hint 注明丢弃的无效 kind


def test_update_tags_merge_preserves_facets():
    # E2E 实测教训:7B 只传新标签,整体替换会拆掉 job:/role: 系统面标签 → 必须合并
    calls = {}
    m = _mat("m1")
    m["tags"] = "sp,job:j9,role:master,bilibili,旧中文"

    def fake_update_tags(mid, tags, purge_ai_tags=True):
        calls["id"], calls["tags"] = mid, tags
        m["tags"] = tags
        return {"ok": True, "id": mid, "tags": tags,
                "ai_tags_kept": [], "ai_tags_removed": []}

    _reset(chat_models=lambda: ["fake"], search=_fake_search([m]),
           get_material=lambda mid: m if mid == "m1" else None)
    orig = core.update_tags
    core.update_tags = fake_update_tags
    try:
        agent._chat = Script([
            '{"todos":[{"id":1,"text":"打标"}]}',
            '{"action":"search_materials","args":{"q":"BV123"}}',
            '{"action":"update_tags","args":{"id":"BV123","tags":"美食,料理"},"mark_done":[1]}',
            '{"action":"finish","args":{"summary":"done"},"mark_done":[1]}',
        ])
        r = agent.agent_run("打标", allow_write=True)
    finally:
        core.update_tags = orig
    assert r["status"] == "done", r
    assert calls["id"] == "m1", calls  # id 兜底:BV123 → m1
    assert calls["tags"] == "sp,job:j9,role:master,bilibili,旧中文,美食,料理", calls


def test_merge_material_tags_protects_system_facets():
    # 直接测 core 护栏:remove 无法删 job:/role:;可删普通标签
    tmp = tempfile.mkdtemp(prefix="hub_merge_", dir=core.HUB)
    old_db, old_idx = core.INDEX_DB, core.INDEX_DIR
    try:
        core.INDEX_DIR = tmp
        core.INDEX_DB = os.path.join(tmp, "hub.db")
        core._init_db()
        core.add_material(
            id="mmmmmmmmmmmm", kind="videos", ext=".mp4", name="a.mp4",
            rel_path="a.mp4", size=1, sha256="m" * 64,
            tags="sp,job:j1,role:master,旧标,美食", description="",
            source="t", orig_name="a.mp4", created_at="2026-09-30T00:00:00",
        )
        r = core.merge_material_tags(
            "mmmmmmmmmmmm", tags="新标",
            remove=["job:j1", "role:master", "旧标"],
        )
        assert r["ok"], r
        tags = set((r["tags"] or "").split(","))
        assert "job:j1" in tags and "role:master" in tags and "sp" in tags, tags
        assert "旧标" not in tags, tags
        assert "美食" in tags and "新标" in tags, tags
        assert "job:j1" in r.get("protected_skipped", []), r
        assert "role:master" in r.get("protected_skipped", []), r
    finally:
        core.INDEX_DB, core.INDEX_DIR = old_db, old_idx
        shutil.rmtree(tmp, ignore_errors=True)


def test_thread_persist_separate_from_checkpoint():
    """UI 对话线程与 agent_workspace checkpoint 解耦:可独立存取/删除。"""
    agent.CHATS = os.path.join(_TMP, "chats_persist")
    if os.path.isdir(agent.CHATS):
        shutil.rmtree(agent.CHATS, ignore_errors=True)
    r = agent.thread_save(messages=[
        {"id": "u1", "role": "user", "content": "缺封面盘点",
         "attachments": [{"id": "aabbccddeeff", "name": "x.png", "kind": "images",
                          "status": "added", "previewUrl": "blob:dead"}]},
        {"id": "a1", "role": "assistant", "content": "找到 3 条", "status": "done",
         "taskId": "agthreadtest", "steps": 2, "skill": "covers"},
    ])
    assert r.get("ok") and r["thread_id"].startswith("th"), r
    assert r["message_count"] == 2, r
    tid = r["thread_id"]
    listed = agent.thread_list()
    assert any(x["thread_id"] == tid for x in listed), listed
    got = agent.thread_get(tid)
    assert got.get("thread_id") == tid and len(got.get("messages") or []) == 2, got
    att = got["messages"][0].get("attachments") or []
    assert att and att[0]["id"] == "aabbccddeeff", att
    assert "previewUrl" not in att[0], att  # blob 不得落盘
    assert "agthreadtest" in (got.get("task_ids") or []), got
    # 覆盖保存同一 thread
    r2 = agent.thread_save(thread_id=tid, messages=[
        {"id": "u1", "role": "user", "content": "缺封面盘点"},
        {"id": "a1", "role": "assistant", "content": "找到 3 条", "taskId": "agthreadtest"},
        {"id": "u2", "role": "user", "content": "再查一遍"},
    ])
    assert r2["message_count"] == 3 and r2["thread_id"] == tid, r2
    # cleanup 任务不影响 chats
    agent.thread_delete(tid)
    assert agent.thread_get(tid).get("error"), agent.thread_get(tid)


def test_harness_patch_todos_and_notes():
    """每步 harness:别名修补、write_todos、卸载笔记可被 read_note 读回。"""
    big = [_mat("m%02d" % i, desc="很长的描述" * 40) for i in range(30)]
    _reset(chat_models=lambda: ["fake"], search=_fake_search(big))
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"检索"}]}',
        '{"tool":"search_materials","args":{"q":"x","limit":30}}',
        '{"action":"write_todos","args":{"todos":[{"id":1,"text":"读笔记","status":"pending"},{"id":2,"text":"收尾","status":"pending"}]}}',
        '{"action":"list_notes","args":{}}',
        '{"name":"read_note","arguments":"{\\"file\\":\\"notes/step_000.json\\"}"}',
        '{"action":"finish","args":{"summary":"读回了笔记"},"mark_done":[1,2]}',
    ])
    r = agent.agent_run("大结果再读笔记")
    assert r["status"] == "done", r
    assert "读回了笔记" in r["summary"], r
    with open(os.path.join(agent._ws(r["task_id"]), "state.json"), encoding="utf-8") as f:
        st = json.load(f)
    acts = [s.get("action") for s in st["steps"]]
    assert acts[0] == "search_materials", acts
    assert "write_todos" in acts and "read_note" in acts, acts
    note_step = next(s for s in st["steps"] if s.get("action") == "read_note")
    assert "很长的描述" in note_step["obs"] or "content" in note_step["obs"], note_step["obs"][:200]
    assert st["todos"][-1]["text"] == "收尾", st["todos"]


def test_retrieve_drops_ids_not_in_hits():
    """子代理摘要里的 id 必须来自真实检索,幻觉 id 不得进入白名单。"""
    hits = [_mat("m1")]
    _reset(chat_models=lambda: ["fake"], search=lambda *a, **k: hits)
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"综合检索"}]}',
        '{"action":"retrieve","args":{"goal":"成片"}}',
        '{"queries":["成片"]}',
        '{"summary":"子摘要","ids":["ffffffffffff","m1"]}',
        '{"action":"finish","args":{"summary":"完成","ids":["ffffffffffff","m1"]},"mark_done":[1]}',
    ])
    r = agent.agent_run("找成片")
    assert "ffffffffffff" not in (r.get("result_ids") or []), r
    assert "m1" in (r.get("result_ids") or []), r
    with open(os.path.join(agent._ws(r["task_id"]), "state.json"), encoding="utf-8") as f:
        st = json.load(f)
    assert "ffffffffffff" in st["steps"][0]["obs"] and "dropped_ids" in st["steps"][0]["obs"]


def test_skill_covers_is_deterministic():
    """缺封面是固定技能,不消耗规划/循环 LLM。"""
    orig = core.list_missing_covers
    core.list_missing_covers = lambda kind="", skip_failed=True, limit=50: [
        {"id": "aabbccddeeff", "kind": kind or "silent", "name": "a.mp4", "tags": ""}
    ]
    try:
        _reset(chat_models=lambda: ["fake"])
        agent._chat = Script(['{"todos":[{"id":1,"text":"不应调用"}]}'])
        r = agent.agent_run("列出缺封面的素材，优先 silent")
        assert r["status"] == "done" and r.get("skill") == "covers", r
        assert "aabbccddeeff" in r["summary"], r
        assert agent._chat.q, "确定性技能不应消耗 LLM 脚本"
    finally:
        core.list_missing_covers = orig


def test_skill_buttons_zh_en_tw_are_deterministic():
    """页面三语建议按钮都进同一条固定技能,不交给开放循环。"""
    phrases = {
        "covers": [
            "列出缺封面的素材，优先 silent",
            "List materials missing covers, silent first",
            "列出缺封面的素材，優先 silent",
        ],
        "dupes": [
            "汇报素材库近重复聚类",
            "Report near-duplicate clusters in the library",
            "彙報素材庫近重複聚類",
        ],
        "maintain": [
            "跑 maintain 健康巡检，汇总损坏或不完整素材",
            "Run maintain health check and summarize broken or incomplete assets",
            "跑 maintain 健康巡檢，彙總損壞或不完整素材",
        ],
        "segment": [
            "检索 role:master 母版，再 get_shots 给出可用镜头时间轴",
            "Find masters with role:master, then get_shots for a useful segment timeline",
            "檢索 role:master 母版，再 get_shots 給出可用鏡頭時間軸",
        ],
    }
    for skill, tasks in phrases.items():
        for task in tasks:
            assert agent._match_named_skill(task) == skill, (skill, task)


def test_hello_does_not_invent_tool_todos():
    """问候不进规划器,也不出现 search/maintain/job_checkup 假完成。"""
    def boom(*_a, **_k):
        raise AssertionError("chitchat must not call the planner")

    _reset(chat_models=lambda: ["fake"])
    agent._chat = boom
    r = agent.agent_run("你好")
    assert r["status"] == "done" and r.get("skill") == "chitchat", r
    assert r["steps"] == 0 and r["todos"] == [], r
    assert "缺封面" in r["summary"], r
    assert "任务已全部完成" not in r["summary"]
    assert agent._is_chitchat("hello")
    assert agent._is_chitchat("你好！")
    assert not agent._is_chitchat("列出缺封面的素材，优先 silent")
    assert not agent._is_chitchat("你好，帮我看这个视频的镜头")


def test_route_scenes_do_not_invent_tools():
    """问候、跑题、含糊指令不进规划器;具体技能和带上下文的「继续」不误伤。"""
    def boom(*_a, **_k):
        raise AssertionError("direct scene must not call the planner")

    _reset(chat_models=lambda: ["fake"])
    agent._chat = boom
    for text, scene, needle in (
        ("你好", "chitchat", "缺封面"),
        ("今天天气怎么样", "offtopic", "素材库"),
        ("帮我看看", "clarify", "要做哪一件"),
        ("继续", "clarify", "要做哪一件"),
    ):
        r = agent.agent_run(text, task_id="agroute" + scene[:6])
        assert r["status"] == "done" and r.get("skill") == scene, (text, r)
        assert r["steps"] == 0 and r["todos"] == [], r
        assert needle in r["summary"], (text, r["summary"])
        assert "任务已全部完成" not in r["summary"]

    agent._chat = Script([
        '{"todos":[{"id":1,"text":"search_materials"},{"id":2,"text":"maintain"},{"id":3,"text":"job_checkup"}]}',
        '{"action":"finish","args":{"summary":"任务已全部完成，无需进一步操作"}}',
    ])
    dumped = agent.agent_run("随便说说", task_id="agroutedump1")
    assert dumped.get("skill") == "clarify" and dumped["steps"] == 0, dumped
    assert "要做哪一件" in dumped["summary"], dumped
    assert agent._chat.q, "未请求的工具名计划不得进入主循环"
    assert agent._classify_turn("继续", "user: 缺封面盘点") == "agent"
    assert agent._classify_turn("找出B站视频并总结") == "agent"
    assert agent._match_named_skill("你好，列出缺封面的素材") == "covers"
    bare = agent.agent_run("帮我裁剪", task_id="agroutecrop0")
    assert bare.get("skill") == "clarify" and "起止秒" in bare["summary"], bare
    assert agent._classify_turn("找出适合裁剪的母版") == "agent"
    assert agent._plan_is_unasked_dump(
        [{"text": "search_materials"}, {"text": "maintain"}, {"text": "job_checkup"}],
        "你好呀朋友",
    )
    assert not agent._plan_is_unasked_dump(
        [{"text": "检索B站视频"}, {"text": "总结"}],
        "找出B站视频并总结",
    )


def test_crop_spans_and_focus_are_deterministic():
    """指定素材的裁剪走固定流程:用户起止优先,点名段落只留对应窗,不编码文件。"""
    mid = "c9fec3b84e4e"

    def fake_get(m):
        return dict(_mat(mid), name="AttackOnTitan.mp4",
                    tags="role:master") if m == mid else None

    def fake_shots(m):
        return {"id": mid, "duration": 100.0, "scenes": [
            {"start": 0, "end": 5}, {"start": 5, "end": 20},
            {"start": 20, "end": 55}, {"start": 55, "end": 90},
            {"start": 90, "end": 100},
        ]}

    def fake_search(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        return []

    _reset(chat_models=lambda: ["fake"], get_material=fake_get,
           get_shots=fake_shots, search=fake_search)
    agent._chat = Script(['{"todos":[{"id":1,"text":"不该规划"}]}'])

    ranged = agent.agent_run(
        "把 %s 从 10秒到30秒 裁剪，并导出成 mp4" % mid, task_id="agcroprange1")
    assert ranged.get("skill") == "crop", ranged
    assert "10.00–30.00" in (ranged.get("summary") or "") or "10.0" in ranged["summary"]
    assert "按你给的起止" in ranged["summary"], ranged["summary"]
    assert "scene#" not in ranged["summary"]
    assert "range_*.mp4" in ranged["summary"]
    wins = (agent.agent_status(ranged["task_id"]).get("crop_windows") or [])
    assert wins and wins[0]["start"] == 10 and wins[0]["end"] == 30, wins
    assert agent._parse_crop_spans("crop %s from 10 to 30 seconds" % mid) == [(10.0, 30.0)]
    assert agent._chat.q, "指定裁剪不得进入规划器"
    assert agent._match_named_skill("找出适合裁剪的母版") == ""

    intro = agent.agent_run("裁剪 %s 的片头" % mid, task_id="agcropintro1")
    assert intro.get("skill") == "crop", intro
    assert "片头建立" in intro["summary"], intro["summary"]
    assert "主戏最长镜" not in intro["summary"], intro["summary"]
    assert "物理文件" not in intro["summary"]


def test_unfinished_todos_do_not_read_as_complete():
    """收尾不得把没跑的待办显示成 100%。空话收尾改成澄清,进度条收起。"""
    _reset(chat_models=lambda: ["fake"], search=_fake_search([_mat("m1")]))
    agent._chat = Script([
        '{"todos":[{"id":1,"text":"检索B站视频"},{"id":2,"text":"再核对一遍"}]}',
        '{"action":"search_materials","args":{"q":"bilibili","kind":"videos"}}',
        '{"action":"finish","args":{"summary":"先找到这些"},"mark_done":[1]}',
    ])
    r = agent.agent_run("找出B站视频并总结", task_id="agpartial01")
    assert r["status"] == "done", r
    assert any(t.get("status") != "done" for t in r["todos"]), r["todos"]
    assert r["progress"]["pct"] == 50, r["progress"]
    assert r["progress"]["label"] == "部分完成", r["progress"]

    agent._chat = Script([
        '{"todos":[{"id":1,"text":"检索"},{"id":2,"text":"巡检"}]}',
        '{"action":"finish","args":{"summary":"任务已全部完成，无需进一步操作。"}}',
    ])
    empty = agent.agent_run("找出B站视频并总结", task_id="agvacuous01")
    assert empty.get("skill") == "clarify", empty
    assert empty["todos"] == [], empty
    assert (empty.get("progress") or {}).get("total") == 0, empty.get("progress")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_agent: %d/%d green" % (len(fns), len(fns)))
