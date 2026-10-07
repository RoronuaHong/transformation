# -*- coding: utf-8 -*-
"""Agent 评测 / 回归套件（2026 最佳实践：可度量才能改进）。

用 Scripted-chat 回放跑若干「真实任务模板」，断言质量不变量：
  - 任务必达 finish（status=done）
  - 无编造 id（dropped_ids 空 + 无 critic 标 [Critic]）
  - 结构化引用 citations 全部 valid
  - 成功任务沉淀跨任务记忆（playbook）
  - 长任务滚动摘要 context_digest 非空（防 context-rot）
不连 ollama、不写真实库。

函数名统一 `test_eval_*` 而非 `eval_*`——pytest 默认只收集 `test*` 前缀函数，
写 `eval_*` 会让这 5 个场景**不被 pytest 收集**（只能手动 python 跑），
等于 Agent 质量门形同虚设。改名后随全套件 `pytest` 一起回归。
运行：pytest tests/test_agent_eval.py  或  python tests/test_agent_eval.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import agent  # noqa: E402
from test_agent import Script, _reset, _fake_search, _mat  # noqa: E402


def _run(script, search_items, task="任务", **extra):
    """跑一个场景（chat_models=fake + 桩检索/运维），返回 (r, st)。

    extra: 额外 core 桩覆盖(如自定义 search/get_material),供特定场景注入。
    """
    over = {
        "chat_models": lambda: ["fake"],
        "search": _fake_search(search_items),
        # maintain 确定性、不碰真实库
        "health": lambda: {"ok": True, "total": 0, "kinds": {}, "duplicates": 0},
        "broken_externals": lambda: [],
    }
    over.update(extra)
    _reset(**over)
    agent._chat = Script(script)
    r = agent.agent_run(task)
    st = agent.agent_status(r["task_id"])
    return r, st


def test_eval_retrieval_with_citations():
    """检索综合 + 引用溯源：ids/citations 合法、记忆沉淀、短任务不压缩。"""
    m1 = _mat("m1"); m1["tags"] = "sp,type:deblur"
    script = [
        '{"todos":[{"id":1,"text":"检索成片"},{"id":2,"text":"综合"}]}',
        '{"action":"search_materials","args":{"q":"去马赛克"}}',
        '{"action":"retrieve","args":{"goal":"去马赛克成片"}}',
        '{"queries":["deblur","去马赛克"]}',
        '{"summary":"子摘要:1 条成片","ids":["m1"]}',
        '{"action":"finish","args":{"summary":"找到去马赛克成片 m1","ids":["m1"],'
        '"citations":[{"claim":"去马赛克成片","id":"m1"}]},"mark_done":[1,2]}',
    ]
    if os.path.exists(agent._MEMORY_PATH):      # 场景自足:不依赖上次运行的残留记忆
        os.remove(agent._MEMORY_PATH)
    r, _ = _run(script, [m1],
                get_material=lambda mid: m1 if mid == "m1" else None)  # facet 沉淀依赖 get_material
    assert r["status"] == "done", r
    assert r["result_ids"] == ["m1"], r["result_ids"]          # 缺口2:无编造 id
    assert r["dropped_ids"] == [], r["dropped_ids"]
    assert r["critic"] is None, r["critic"]                     # 缺口4:Critic 未误报
    assert all(c["valid"] for c in r["citations"]), r["citations"]
    pb = agent._load_playbook()                                 # 缺口3:跨任务记忆
    assert any(e["q"] == "去马赛克" for e in pb), pb
    assert r["context_digest"] == "", "短任务不应产生滚动摘要"   # 缺口6:短任务无关


def test_eval_long_task_compression():
    """长任务(>6 步)：滚动摘要非空、防 context-rot。"""
    m1 = _mat("m1")
    # 关键:每一步的 (action, args) 必须互不相同。
    # 早期版本靠重复 `maintain`(args 恒为 {})凑步数,但那会命中「同参已成功 → 软提示,
    # 再犯且可恢复 → LoopGuard 硬停转」(agent.py:2521-2548),在上下文长到 6 条之前就被
    # break 掉,滚动摘要永远为空——该场景因此长期假绿/失效。改用不同 q 的多次检索,
    # sig 各不相同,不会被去重或硬停转打断,才能真实触发 >_CONTEXT_TAIL 的压缩。
    script = ['{"todos":[{"id":1,"text":"多轮检索"},{"id":2,"text":"汇总"}]}']
    script += ['{"action":"search_materials","args":{"q":"x%d"}}' % i for i in range(1, 8)]
    script += ['{"action":"finish","args":{"summary":"多轮检索完成","ids":["m1"]},'
               '"mark_done":[1,2]}']
    r, _ = _run(script, [m1])
    assert r["status"] == "done", r
    assert r["result_ids"] == ["m1"], r["result_ids"]
    assert r["context_digest"], "长任务应产生滚动摘要"          # 缺口6:动态压缩
    assert "step1[" in r["context_digest"], r["context_digest"]


def test_eval_honesty_zero_hits():
    """诚实弃权（0 命中）：不得编造 id、不得误报 critic、须如实说明。"""
    script = [
        '{"todos":[{"id":1,"text":"检索"},{"id":2,"text":"结论"}]}',
        '{"action":"search_materials","args":{"q":"不存在的东西"}}',
        '{"action":"search_materials","args":{"q":"也不存在"}}',
        '{"action":"finish","args":{"summary":"两次检索均无命中,未能找到相关素材","ids":[]},"mark_done":[1,2]}',
    ]
    r, _ = _run(script, [])  # 空库
    assert r["status"] == "done", r
    assert r["result_ids"] == [] and r["dropped_ids"] == [], r   # 缺口2:无编造
    assert r["critic"] is None, r["critic"]
    assert ("未能" in r["summary"] or "无命中" in r["summary"]
            or "没有" in r["summary"]), r["summary"]


def test_eval_related_traversal():
    """关系反查整合：related 沿 parent: 面标签一跳遍历，衍生 id 合法入引用。"""
    m1 = _mat("m1"); m1["tags"] = "sp,role:master"
    m2 = _mat("m2"); m2["tags"] = "sp,parent:m1"

    def tag_search(q, kind="", tag="", limit=None, offset=0, mode="auto"):
        rows = [m for m in (m1, m2)
                if not tag or tag in [t.strip() for t in (m["tags"] or "").split(",")]]
        return rows[:limit or len(rows)]

    script = [
        '{"todos":[{"id":1,"text":"检索母版"},{"id":2,"text":"找衍生"},{"id":3,"text":"总结"}]}',
        '{"action":"search_materials","args":{"q":"母版"}}',
        '{"action":"related","args":{"id":"m1","rel":"children"}}',
        '{"action":"finish","args":{"summary":"母版 m1 及其衍生 m2","ids":["m1","m2"],'
        '"citations":[{"claim":"母版","id":"m1"},{"claim":"衍生","id":"m2"}]},'
        '"mark_done":[1,2,3]}',
    ]
    orig = agent._tool_related
    calls = []
    agent._tool_related = lambda a: (calls.append(dict(a)), orig(a))[1]
    try:
        r, _ = _run(script, [m1, m2], search=tag_search,
                    get_material=lambda mid: {"m1": m1, "m2": m2}.get(mid))
    finally:
        agent._tool_related = orig
    assert calls and calls[0] == {"id": "m1", "rel": "children"}, calls  # 缺口5:related 被真实调用
    assert r["status"] == "done", r
    assert r["result_ids"] == ["m1", "m2"], r["result_ids"]
    assert r["dropped_ids"] == [], r["dropped_ids"]
    assert all(c["valid"] for c in r["citations"]), r["citations"]


def test_eval_memory_reuse():
    """跨任务记忆复用：同查询二次执行，playbook 计数累加且 facet 沉淀成 hint。"""
    m1 = _mat("m1", desc="去马赛克成片"); m1["tags"] = "sp,type:deblur"
    script = [
        '{"todos":[{"id":1,"text":"检索"}]}',
        '{"action":"search_materials","args":{"q":"去马赛克"}}',
        '{"action":"finish","args":{"summary":"找到成片","ids":["m1"],'
        '"citations":[{"claim":"成片","id":"m1"}]},"mark_done":[1]}',
    ]
    if os.path.exists(agent._MEMORY_PATH):
        os.remove(agent._MEMORY_PATH)
    _run(script, [m1], task="第一次检索",
         get_material=lambda mid: m1 if mid == "m1" else None)  # facet 沉淀依赖 get_material
    e1 = next((e for e in agent._load_playbook() if e["q"] == "去马赛克"), None)
    assert e1 and e1["n"] == 1, e1
    _run(script, [m1], task="第二次检索",
         get_material=lambda mid: m1 if mid == "m1" else None)
    e2 = next((e for e in agent._load_playbook() if e["q"] == "去马赛克"), None)
    assert e2 and e2["n"] == 2, e2                        # 缺口3:计数累加
    assert "deblur" in (e2.get("hint") or ""), e2         # 有效 facet 沉淀


if __name__ == "__main__":
    cases = [test_eval_retrieval_with_citations, test_eval_long_task_compression,
             test_eval_honesty_zero_hits, test_eval_related_traversal, test_eval_memory_reuse]
    fails = 0
    for fn in cases:
        try:
            fn()
            print("PASS %s" % fn.__name__)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print("FAIL %s: %s: %s" % (fn.__name__, type(e).__name__, e))
    if fails:
        sys.exit(1)
    print("test_agent_eval: %d/%d scenarios green" % (len(cases) - fails, len(cases)))
