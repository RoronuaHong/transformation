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
    assert r["steps"] == 2 and "步数上限" in r["summary"]


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    shutil.rmtree(_TMP, ignore_errors=True)
    print("test_agent: %d/%d green" % (len(fns), len(fns)))
