"""Materials Hub — Deep Agent 核心编排层(零依赖,纯 Python 标准库)。

路线 C(2026-09-28 拍板):核心编排 stdlib 自实现,协议上通过 MCP 暴露,
未来可无缝切换官方 deepagents(langchain)或接入 CodeBuddy/Claude 等宿主。

Deep Agent 四支柱(区别于单轮 tool-calling):
  1. Planning tool   —— 任务先由 LLM 拆成 todos,逐步推进;状态持久化到
                        index/agent_workspace/<task_id>/state.json,可中断后用
                        agent_status() 查看。
  2. 上下文卸载      —— 过长的观察结果落盘 <ws>/notes/step_NNN.json,
                        上下文只留「文件指针 + 前 200 字符」,主循环不被 344 条
                        检索结果撑爆(与 chunk_search/read_text_preview 同一省 token 哲学)。
  3. Subagents       —— retrieve 子代理(LLM 驱动:多查询→汇总,只回摘要,上下文隔离)
                        与 maintain(确定性巡检:health + broken_externals,不走 LLM)。
  4. 详细系统提示     —— 角色/工具表/规则齐备;写工具默认不存在,只有显式
                        allow_write=True 才注册(MCP 侧再叠 confirm=true 人工复核)。

LLM 后端:本机 ollama chat 模型(与 auto_tag 同一发现逻辑 core.chat_models());
无 chat 模型 → status=skipped 优雅降级,零网络依赖下不报错、不阻断。
"""
import os
import json
import time
import hashlib

import core

# 工作区(派生数据,可整目录删除;与 thumbs/search_cache 同级,不入库)
WORKSPACE = os.path.join(core.HUB, "index", "agent_workspace")
_OFFLOAD_CHARS = 1200        # 观察结果超过该字符数即卸载落盘
_CONTEXT_TAIL = 6            # 主循环上下文最多携带最近 N 条观察
_DEFAULT_STEPS = 12
_SUB_QUERIES = 3             # retrieve 子代理最多并发查询数


# ---------------------------------------------------------------- LLM 后端
def _chat(messages, model, timeout=180):
    """调 ollama /api/chat,强制 JSON 输出;返回解析后的 dict(失败给空 dict)。"""
    r = core._http_json(core._embed_url() + "/api/chat", timeout=timeout,
                        payload={"model": model, "messages": messages,
                                 "format": "json", "stream": False})
    return core._safe_json(((r.get("message") or {}).get("content") or "").strip())


def _brief(m):
    """素材精简视图(省 token;与 mcp_server._brief 字段一致)。"""
    out = {k: m.get(k, "") for k in
           ("id", "kind", "ext", "name", "size", "tags", "ai_tags",
            "description", "location")}
    if m.get("location") == "external":
        out["external_path"] = m.get("external_path", "")
    return out


# ---------------------------------------------------------------- 工作区与状态
def _ws(task_id):
    return os.path.join(WORKSPACE, task_id)


def _new_id(task):
    h = hashlib.sha1((task + str(time.time())).encode("utf-8")).hexdigest()
    return "ag" + h[:10]


def _save(state, ws):
    with open(os.path.join(ws, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def agent_status(task_id):
    """读取某次 Deep Agent 任务的状态/待办/轨迹(不执行任何东西)。"""
    p = os.path.join(_ws(task_id), "state.json")
    if not os.path.isfile(p):
        return {"error": "no such task", "task_id": task_id}
    with open(p, "r", encoding="utf-8") as f:
        s = json.load(f)
    return {"task_id": task_id, "task": s.get("task"), "status": s.get("status"),
            "summary": s.get("summary"), "todos": s.get("todos"),
            "steps": len(s.get("steps") or []), "notes": s.get("notes") or []}


# ---------------------------------------------------------------- 上下文卸载
def _observe(state, ws, obj):
    """把观察结果压缩进上下文:过长的落盘,只回指针 + 摘要。"""
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(obj)
    if len(s) <= _OFFLOAD_CHARS:
        return s
    fname = "notes/step_%03d.json" % len(state.get("steps") or [])
    with open(os.path.join(ws, fname), "w", encoding="utf-8") as f:
        f.write(s)
    state.setdefault("notes", []).append({"file": fname, "chars": len(s)})
    return "[已卸载→%s %d字符] %s" % (fname, len(s), s[:200])


# ---------------------------------------------------------------- 工具表(支柱 3/4)
def _tool_search(a):
    return [_brief(m) for m in core.search(
        a.get("q", ""), a.get("kind", ""), a.get("tag", ""),
        limit=int(a.get("limit") or 10), mode=a.get("mode", "auto"))]


def _tool_chunk(a):
    return [_brief(m) for m in core.chunk_search(
        a.get("q", ""), limit=int(a.get("limit") or 10),
        kind=a.get("kind", ""), tag=a.get("tag", ""))]


def _tool_preview(a):
    m = core.get_material(a.get("id", ""))
    if not m:
        return {"error": "not found: " + a.get("id", "")}
    n = int(a.get("chars") or 1500)
    txt = (m.get("description") or "").strip()
    ep = m.get("external_path") or ""
    if not txt and ep and ep.lower().endswith(
            (".md", ".txt", ".srt", ".ass", ".json", ".vtt")):
        try:
            with open(ep, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read(n)
        except OSError:
            txt = ""
    return {"id": m["id"], "name": m.get("name", ""), "preview": txt[:n]}


def _tool_update_tags(a):
    """写工具:仅 allow_write=True 时注册。"""
    mid = a.get("id", "")
    if not core.get_material(mid):
        return {"error": "not found: " + mid}
    core.update_tags(mid, a.get("tags", ""))
    return {"ok": True, "id": mid}


def _tool_register(a):
    path = a.get("path", "")
    if not path:
        return {"error": "path required"}
    r = core.ingest_external(path, source=a.get("source", "agent"),
                             tags=a.get("tags", ""),
                             description=a.get("description", ""))
    if r is None:
        return {"error": "cannot register (missing/unsupported): " + path}
    return r


def _build_tools(allow_write):
    tools = {
        "search_materials": (_tool_search,
                             "检索素材库;args: q(中文自然语言/关键词), kind?, tag?, limit?, mode?"),
        "get_material": (lambda a: core.get_material(a.get("id", "")) or {"error": "not found"},
                         "按 id 取素材完整记录;args: id"),
        "list_tags": (lambda a: [{"tag": t, "count": c} for t, c in core.distinct_tags()[:int(a.get("limit") or 50)]],
                      "列出全部标签及计数;args: limit?"),
        "hub_stats": (lambda a: core.health(),
                      "库健康快照(总量/种类/重复/引用完整性);args: 无"),
        "chunk_search": (_tool_chunk,
                         "长文档父子分块检索(按段落命中笔记/字幕全文);args: q, kind?, tag?, limit?"),
        "read_text_preview": (_tool_preview,
                              "读素材描述/关联文本前 N 字符(取回被卸载的细节用);args: id, chars?"),
    }
    if allow_write:
        tools["update_tags"] = (_tool_update_tags, "写:更新素材标签;args: id, tags")
        tools["register_asset"] = (_tool_register,
                                   "写:登记外部文件引用(不复制);args: path, tags?, description?")
    return tools


# ---------------------------------------------------------------- 子代理(支柱 3)
_SUB_RETRIEVE_SYS = (
    "你是素材中心的检索子代理。第一步针对目标产出最多 %d 个互补查询词"
    '(输出 {"queries":[...]},中英混合可);第二步汇总检索结果'
    '(输出 {"summary":"中文摘要","ids":["最重要的素材id",...]})。'
    "只输出 JSON,不要输出多余字段。" % _SUB_QUERIES
)


def _sub_retrieve(goal, model, context_tail):
    """retrieve 子代理:多查询检索 + LLM 汇总;只把摘要交回主上下文(上下文隔离)。"""
    r1 = _chat([{"role": "system", "content": _SUB_RETRIEVE_SYS},
                {"role": "user", "content": "目标:%s\n已有上下文:%s" % (goal, context_tail)}],
               model)
    queries = [q for q in (r1.get("queries") or []) if isinstance(q, str)][: _SUB_QUERIES]
    if not queries:
        queries = [goal]
    hits, seen = [], set()
    for q in queries:
        for m in core.search(q, limit=8):
            if m["id"] not in seen:
                seen.add(m["id"])
                hits.append(_brief(m))
    r2 = _chat([{"role": "system", "content": _SUB_RETRIEVE_SYS},
                {"role": "user", "content": "目标:%s\n检索结果(%d 条):\n%s"
                 % (goal, len(hits), json.dumps(hits, ensure_ascii=False))}],
               model)
    return {"summary": r2.get("summary", ""), "ids": r2.get("ids", []),
            "queries": queries, "n_hits": len(hits)}


def _sub_maintain():
    """确定性巡检子代理(不走 LLM,零幻觉):健康快照 + 失效引用。"""
    h = core.health()
    broken = core.broken_externals()
    return {"health_ok": h.get("ok"), "total": h.get("total"),
            "kinds": h.get("kinds"), "duplicates": h.get("duplicates"),
            "broken": len(broken),
            "broken_ids": [b.get("id") for b in broken[:10]],
            "hint": "清理失效引用请走 MCP prune 或 bridge --prune(agent 不持有删权)"}


# ---------------------------------------------------------------- 规划与主循环(支柱 1/4)
_PLAN_SYS = ("你是素材库任务的规划器。把任务拆成 3-6 条可执行待办"
             '(输出 {"todos":[{"id":1,"text":"..."}]}),面向检索/巡检/整理,'
             "不要空话。只输出 JSON。")


def _plan(task, model):
    r = _chat([{"role": "system", "content": _PLAN_SYS},
               {"role": "user", "content": task}], model)
    todos = []
    for i, t in enumerate(r.get("todos") or [], 1):
        if isinstance(t, dict) and t.get("text"):
            todos.append({"id": t.get("id") or i, "text": str(t["text"])[:120],
                          "status": "pending"})
    return todos or [{"id": 1, "text": task[:120], "status": "pending"}]


_LOOP_SYS = """你是「素材中心」的 Deep Agent,管理一个多媒体素材库(图片/视频/文档/音频/字幕)。
每轮只输出一个 JSON 对象:
  {"thought":"简短推理","action":"<工具名或 retrieve/maintain/finish>","args":{...},"mark_done":[已完成todo id]}
finish 的 args 为 {"summary":"中文总结,列关键素材 id"}。

规则:
1. 观察结果过长会被卸载为文件指针;需要细节时用 read_text_preview/get_material 精准取回,不要要求重发全文。
2. 优先用 kind/tag 过滤缩小检索面;需要跨多查询综合时用 retrieve 子代理(只回摘要,省上下文)。
3. 库巡检/失效引用清点用 maintain(确定性,零幻觉)。
4. 每完成一条待办就在 mark_done 里列出其 id;全部完成后必须 finish。
5. 写操作默认不可用;若工具表中没有写工具,不要尝试写入。

可用工具:
"""


def agent_run(task, allow_write=False, max_steps=_DEFAULT_STEPS, model=None, task_id=None):
    """执行一次 Deep Agent 任务。返回最终状态 dict。

    task        自然语言任务(中文);
    allow_write 仅当 True 才注册写工具(MCP 侧还需 confirm=true 才会传 True);
    max_steps   主循环步数上限(防失控,默认 12);
    task_id     可外部预指定(server 侧先拿到 id 供前端轮询)。
    """
    models = core.chat_models()
    if not models:
        return {"status": "skipped", "reason": "no_chat_model",
                "hint": "ollama pull qwen2.5:7b 后重试"}
    model = model or models[0]

    task_id = task_id or _new_id(task)
    ws = _ws(task_id)
    os.makedirs(os.path.join(ws, "notes"), exist_ok=True)
    state = {"task": task, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
             "model": model, "allow_write": bool(allow_write),
             "status": "planning", "todos": [], "steps": [], "notes": [],
             "summary": ""}
    try:
        state["todos"] = _plan(task, model)
    except Exception as e:                                  # noqa: BLE001
        state.update(status="error", summary="plan failed: %s" % e)
        _save(state, ws)
        return {"status": "error", "task_id": task_id, "summary": state["summary"]}

    state["status"] = "running"
    tools = _build_tools(allow_write)
    tool_lines = "".join("  - %s: %s\n" % (n, d) for n, (_, d) in sorted(tools.items()))
    tool_lines += "  - retrieve: 子代理:多查询检索+LLM汇总,只回摘要;args: {goal}\n"
    tool_lines += "  - maintain: 确定性巡检(health+失效引用),零幻觉;args: 无\n"
    tool_lines += "  - finish: 任务完成,输出总结\n"
    sys_prompt = _LOOP_SYS + tool_lines

    context = []                                            # 紧凑观察轨迹(指针化)
    finished = False
    for n in range(1, max_steps + 1):
        budget_hint = ""
        if max_steps - n <= 2:      # 步数将尽,催促收尾(7B 小模型常把 todo 做完却忘了 finish)
            budget_hint = "\n(注意:剩余步数很少,若待办已基本完成,请立即 finish 并在 summary 里给出汇总)"
        user = ("任务:%s\n待办:%s\n最近观察:\n%s%s"
                % (task, json.dumps(state["todos"], ensure_ascii=False),
                   "\n".join(context[-_CONTEXT_TAIL:]) or "(无)", budget_hint))
        act = _chat([{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user}], model)
        action = act.get("action", "")
        args = act.get("args") or {}
        for tid in act.get("mark_done") or []:
            for t in state["todos"]:
                if str(t["id"]) == str(tid):
                    t["status"] = "done"

        if action == "finish":
            state["summary"] = str(args.get("summary") or act.get("thought") or "")[:2000]
            for t in state["todos"]:
                t["status"] = "done"
            finished = True
            state["steps"].append({"n": n, "action": "finish", "ok": True})
            break

        try:
            if action == "retrieve":
                obs = _sub_retrieve(str(args.get("goal") or task), model,
                                    " ".join(context[-2:]))
            elif action == "maintain":
                obs = _sub_maintain()
            elif action in tools:
                obs = tools[action][0](args)
            else:
                obs = {"error": "unknown action: %s" % action,
                       "allowed": sorted(tools) + ["retrieve", "maintain", "finish"]}
        except Exception as e:                              # noqa: BLE001
            obs = {"error": "%s: %s" % (type(e).__name__, e)}

        ptr = _observe(state, ws, obs)
        context.append("step%d[%s] %s" % (n, action, ptr[:400]))
        state["steps"].append({"n": n, "action": action, "ok": "error" not in obs,
                               "thought": str(act.get("thought") or "")[:200],
                               "obs": ptr})

    if not finished:
        state["status"] = "max_steps_reached"
        # 截断时 summary 带上最近几步的实质进展(而非干巴巴一句),agent_status 一眼可见
        tail = " | ".join(
            (s.get("thought") or s.get("action") or "") for s in state["steps"][-3:]
            if isinstance(s, dict))
        state["summary"] = ("步数上限(%d)已达,未 finish;最近进展: %s"
                            % (max_steps, tail))[:2000]
    else:
        state["status"] = "done"
    _save(state, ws)
    return {"status": state["status"], "task_id": task_id, "workspace": ws,
            "summary": state["summary"], "todos": state["todos"],
            "steps": len(state["steps"]), "notes": len(state["notes"])}


if __name__ == "__main__":                                  # 直接调试: python agent.py "任务..."
    core.init_hub()
    r = agent_run(" ".join(os.sys.argv[1:]) or "巡检素材库健康状态")
    print(json.dumps(r, ensure_ascii=False, indent=2))
