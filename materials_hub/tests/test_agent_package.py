"""素材包组装(#5) + 多智能体编排(#10) 离线测试(mock LLM 与 core;不连 ollama/不写真实库)。

覆盖:Librarian→Critic→Executor 角色链、scope 过滤、缺失 id 丢弃、确定性兜底(无模型)、
命名技能路由、经 agent_run 端到端写出 manifest。
运行: python tests/test_agent_package.py
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import agent  # noqa: E402

core._init_db()

_ORIG = {
    "search": core.search,
    "get_material": core.get_material,
    "expand_query": core.expand_query,
    "chat_models": core.chat_models,
}
_ORIG_CHAT = agent._chat
_ORIG_WS = agent.WORKSPACE


def _reset(**over):
    for k, v in _ORIG.items():
        setattr(core, k, v)
    agent._chat = _ORIG_CHAT
    agent.WORKSPACE = _ORIG_WS
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


def _mat(mid, tags="sp"):
    return {"id": mid, "kind": "videos", "ext": ".mp4", "name": mid + ".mp4",
            "size": 1, "tags": tags, "ai_tags": "", "description": "d",
            "location": "external", "external_path": "x/" + mid + ".mp4",
            "thumb": "x/" + mid + ".jpg"}


def _fake_search(items):
    def f(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        return items[:limit or len(items)]
    return f


def test_match_named_skill_package():
    assert agent._match_named_skill("帮我组装一个关于猫的素材包") == "package"
    assert agent._match_named_skill("打包这些视频做分发") == "package"
    assert agent._match_named_skill("package the bilibili clips") == "package"
    # 不应误伤普通检索
    assert agent._match_named_skill("找猫的视频") == ""


def test_assemble_package_deterministic_no_model():
    ws = tempfile.mkdtemp()
    agent.WORKSPACE = ws
    try:
        mats = [_mat("m1", "role:master"), _mat("m2", "role:master"), _mat("m3", "sp")]
        core.search = _fake_search(mats)
        core.get_material = lambda mid: next(
            (m for m in mats if m["id"] == mid), None)
        core.expand_query = lambda q: q + " 猫"
        core.chat_models = lambda: []
        r = agent._assemble_package("猫素材", model=None, scope="all", limit=10)
        assert r["asset_count"] == 3, r
        assert set(r["ids"]) <= {"m1", "m2", "m3"}
        assert os.path.exists(r["path"])
        assert r["roles"] == ["Librarian", "Critic", "Executor"]
        man = json.load(open(r["path"], encoding="utf-8"))
        assert man["schema"] == "materials-hub/package@1"
        assert man["asset_count"] == 3
    finally:
        agent.WORKSPACE = _ORIG_WS
        shutil.rmtree(ws, ignore_errors=True)


def test_assemble_package_scope_master_filters():
    ws = tempfile.mkdtemp()
    agent.WORKSPACE = ws
    try:
        mats = [_mat("m1", "role:master"), _mat("m2", "role:clip"), _mat("m3", "sp")]
        core.search = _fake_search(mats)
        core.get_material = lambda mid: next(
            (m for m in mats if m["id"] == mid), None)
        core.expand_query = lambda q: q
        core.chat_models = lambda: []
        r = agent._assemble_package("x", model=None, scope="master", limit=10)
        assert r["asset_count"] == 1, r
        assert r["ids"] == ["m1"]
        assert "m2" in r["dropped_ids"]
    finally:
        agent.WORKSPACE = _ORIG_WS
        shutil.rmtree(ws, ignore_errors=True)


def test_assemble_package_dropped_missing():
    ws = tempfile.mkdtemp()
    agent.WORKSPACE = ws
    try:
        mats = [_mat("m1", "sp")]
        core.search = _fake_search(mats)
        core.get_material = lambda mid: None
        core.expand_query = lambda q: q
        core.chat_models = lambda: []
        r = agent._assemble_package("x", model=None, limit=10)
        assert r["asset_count"] == 0, r
        assert r["dropped_ids"] == ["m1"]
    finally:
        agent.WORKSPACE = _ORIG_WS
        shutil.rmtree(ws, ignore_errors=True)


def test_package_skill_via_agent_run():
    ws = tempfile.mkdtemp()
    agent.WORKSPACE = ws
    try:
        mats = [_mat("m1", "role:master"), _mat("m2", "role:master")]
        core.search = _fake_search(mats)
        core.get_material = lambda mid: next(
            (m for m in mats if m["id"] == mid), None)
        core.expand_query = lambda q: q
        core.chat_models = lambda: ["fake"]
        agent._chat = Script([json.dumps({"queries": ["猫", "猫咪"]})])
        r = agent.agent_run("帮我组装一个关于猫的素材包")
        assert r["skill"] == "package", r
        assert "[素材包组装]" in r["summary"], r.get("summary")
        assert r["status"] == "done"
        pkgs = os.listdir(os.path.join(ws, "packages"))
        assert len(pkgs) == 1, pkgs
    finally:
        agent.WORKSPACE = _ORIG_WS
        shutil.rmtree(ws, ignore_errors=True)
