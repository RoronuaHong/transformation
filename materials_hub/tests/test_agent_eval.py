# -*- coding: utf-8 -*-
"""Agent 评测 / 回归套件（2026 最佳实践：可度量才能改进）。

用 Scripted-chat 回放跑若干「真实任务模板」，断言质量不变量：
  - 任务必达 finish（status=done）
  - 无编造 id（dropped_ids 空 + 无 critic 标 [Critic]）
  - 结构化引用 citations 全部 valid
  - 成功任务沉淀跨任务记忆（playbook）
  - 长任务滚动摘要 context_digest 非空（防 context-rot）
不连 ollama、不写真实库。运行：python tests/test_agent_eval.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import agent  # noqa: E402
from test_agent import Script, _reset, _fake_search, _mat  # noqa: E402


def _run(script, search_items, task="任务"):
    """跑一个场景（chat_models=fake + 桩检索/运维），返回 (r, st)。"""
    over = {
        "chat_models": lambda: ["fake"],
        "search": _fake_search(search_items),
        # maintain 确定性、不碰真实库
        "health": lambda: {"ok": True, "total": 0, "kinds": {}, "duplicates": 0},
        "broken_externals": lambda: [],
    }
    _reset(**over)
    agent._chat = Script(script)
    r = agent.agent_run(task)
    st = agent.agent_status(r["task_id"])
    return r, st


def eval_retrieval_with_citations():
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
    r, _ = _run(script, [m1])
    assert r["status"] == "done", r
    assert r["result_ids"] == ["m1"], r["result_ids"]          # 缺口2:无编造 id
    assert r["dropped_ids"] == [], r["dropped_ids"]
    assert r["critic"] is None, r["critic"]                     # 缺口4:Critic 未误报
    assert all(c["valid"] for c in r["citations"]), r["citations"]
    pb = agent._load_playbook()                                 # 缺口3:跨任务记忆
    assert any(e["q"] == "去马赛克" for e in pb), pb
    assert r["context_digest"] == "", "短任务不应产生滚动摘要"   # 缺口6:短任务无关


def eval_long_task_compression():
    """长任务(>6 步)：滚动摘要非空、防 context-rot。"""
    m1 = _mat("m1")
    script = ['{"todos":[{"id":1,"text":"巡检"},{"id":2,"text":"检索"},'
              '{"id":3,"text":"再巡检"},{"id":4,"text":"整理"}]}']
    script += ['{"action":"maintain","args":{}}',
               '{"action":"search_materials","args":{"q":"x"}}',
               '{"action":"maintain","args":{}}',
               '{"action":"search_materials","args":{"q":"x"}}',
               '{"action":"maintain","args":{}}']
    script += ['{"action":"retrieve","args":{"goal":"g"}}',
               '{"queries":["a","b"]}',
               '{"summary":"子摘要","ids":["m1"]}',
               '{"action":"maintain","args":{}}',   # 第 7 个非-finish 步骤,触发滚动摘要压缩
               '{"action":"finish","args":{"summary":"整理完成","ids":["m1"]},"mark_done":[1,2,3,4]}']
    r, _ = _run(script, [m1])
    assert r["status"] == "done", r
    assert r["result_ids"] == ["m1"], r["result_ids"]
    assert r["context_digest"], "长任务应产生滚动摘要"          # 缺口6:动态压缩
    assert "step1[" in r["context_digest"], r["context_digest"]


def eval_honesty_zero_hits():
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


if __name__ == "__main__":
    cases = [eval_retrieval_with_citations, eval_long_task_compression, eval_honesty_zero_hits]
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
