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
import re
import json
import time
import hashlib

import core

# 工作区(派生数据,可整目录删除;与 thumbs/search_cache 同级,不入库)
WORKSPACE = os.path.join(core.HUB, "index", "agent_workspace")

# 跨任务记忆(确定性沉淀,非 LLM):成功任务的「查询词 → 有效 facet」规律,
# 注入下次任务的系统提示,帮本地 7B 模型少走弯路(对应 2026 Agent Memory 的程序记忆)。
# 落在 WORKSPACE 下(派生数据,测试可随 WORKSPACE 重定向到临时目录)。
_MEMORY_PATH = os.path.join(WORKSPACE, "agent_memory.json")
_OFFLOAD_CHARS = 1200        # 观察结果超过该字符数即卸载落盘
_CONTEXT_TAIL = 6            # 主循环上下文最多携带最近 N 条观察
_DIGEST_MAX = 30              # 滚动摘要最多保留的压缩行数(防无限增长)
_DEFAULT_STEPS = 12
_SUB_QUERIES = 3             # retrieve 子代理最多并发查询数


# ---------------------------------------------------------------- LLM 后端
def _chat(messages, model, timeout=180):
    """调 ollama /api/chat,强制 JSON 输出;失败/空响应一律返回 {}(绝不泄漏 None)。"""
    r = core._http_json(core._embed_url() + "/api/chat", timeout=timeout,
                        payload={"model": model, "messages": messages,
                                 "format": "json", "stream": False}) or {}
    return core._safe_json(((r.get("message") or {}).get("content") or "").strip()) or {}


def _brief(m):
    """素材精简视图(省 token;与 mcp_server._brief 字段一致)。"""
    out = {k: m.get(k, "") for k in
           ("id", "kind", "ext", "name", "size", "tags", "ai_tags",
            "description", "location")}
    if m.get("location") == "external":
        out["external_path"] = m.get("external_path", "")
    return out


def _collect_ids(obs):
    """从工具/子代理观察结果里抽取素材 id(供 finish 核验,杜绝编造 id)。

    覆盖三种形态:检索结果 list[brief]、单条素材 dict(id=)、retrieve 子代理 dict(ids=[...])。
    """
    ids = []
    if isinstance(obs, list):
        for item in obs:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
    elif isinstance(obs, dict):
        if obs.get("id"):
            ids.append(str(obs["id"]))
        elif obs.get("ids"):
            for i in obs["ids"]:
                if i and isinstance(i, str):
                    ids.append(str(i))
        elif isinstance(obs.get("results"), list):   # 检索兜底 dict(results 键)
            for item in obs["results"]:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
    return ids


# ---------------------------------------------------------------- 跨任务记忆
def _load_playbook():
    """读历史「查询词 → 有效 facet」规律(空/损坏返回 [])。"""
    try:
        with open(_MEMORY_PATH, encoding="utf-8") as f:
            return (json.load(f) or {}).get("playbook", []) or []
    except (OSError, ValueError):
        return []


def _save_playbook(pb):
    try:
        os.makedirs(os.path.dirname(_MEMORY_PATH), exist_ok=True)
        with open(_MEMORY_PATH, "w", encoding="utf-8") as f:
            json.dump({"playbook": pb}, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _memory_hint():
    """把历史规律渲染成系统提示片段(无记忆则返回空串)。"""
    pb = _load_playbook()
    if not pb:
        return ""
    lines = "\n".join("  - %s → 优先试 %s" % (e["q"], e.get("hint", ""))
                      for e in pb[:15] if e.get("hint"))
    if not lines:
        return ""
    return ("\n已知检索规律(来自历史任务,仅供参考,仍需实际检索验证):\n"
            + lines + "\n")


def _record_memory(state, ctx):
    """任务成功收尾后,确定性地把「查询词 → 命中素材的 kind/面标签」沉淀进 playbook。

    纯规则、零幻觉:只在 status=done 且有 result_ids 时记;按 q 聚合频次,
    取 top-3 高频 facet 作 hint;仅保留出现 ≥2 次的规律(防一次性噪声),最多 50 条。
    """
    if state.get("status") != "done":
        return
    result_ids = state.get("result_ids") or []
    if not result_ids:
        return
    queries = []
    for s in state.get("steps") or []:
        if s.get("action") in ("search_materials", "retrieve"):
            a = s.get("args") or {}
            q = a.get("q") or a.get("goal") or ""
            if q and str(q).strip():
                queries.append(str(q).strip())
    if not queries:
        return
    facets = {}
    for mid in result_ids:
        m = core.get_material(mid)
        if not m:
            continue
        facets[m.get("kind")] = facets.get(m.get("kind"), 0) + 1
        for t in (m.get("tags") or "").split(","):
            t = t.strip()
            if t and ":" in t:                       # 只取 key:value 面标签
                facets[t] = facets.get(t, 0) + 1
    if not facets:
        return
    pb = _load_playbook()
    by_q = {e["q"]: e for e in pb}
    for q in queries:
        e = by_q.get(q)
        if e is None:
            e = {"q": q, "facets": {}, "n": 0}
            by_q[q] = e
            pb.append(e)
        e["n"] += 1
        for fk, fv in facets.items():
            e["facets"][fk] = e["facets"].get(fk, 0) + fv
    for e in pb:
        top = sorted(e["facets"].items(), key=lambda kv: kv[1], reverse=True)[:3]
        e["hint"] = " ".join(str(k) for k, _ in top)
    pb = pb[:50]                                       # 容量上限,防止无限增长
    _save_playbook(pb)


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
            "steps": len(s.get("steps") or []), "notes": s.get("notes") or [],
            "result_ids": s.get("result_ids", []),
            "dropped_ids": s.get("dropped_ids", []),
            "citations": s.get("citations", []),
            "critic": s.get("critic"),
            "context_digest": s.get("context_digest", "")}


def agent_cancel(task_id):
    """协作式取消一个进行中的 Deep Agent 任务(缺口1:轮询之外的可取消性)。

    在任务工作区写 cancel.flag;agent_run 主循环每步开头检查,命中即在步边界
    终止(已执行步骤/观察全部保留落盘)。不杀线程——避免半写的 state/notes 损坏。
    """
    ws = _ws(task_id)
    p = os.path.join(ws, "state.json")
    if not os.path.isfile(p):
        return {"error": "no such task", "task_id": task_id}
    try:
        with open(p, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return {"error": "state unreadable", "task_id": task_id}
    if s.get("status") not in ("planning", "running"):
        return {"task_id": task_id, "status": s.get("status"), "cancelled": False,
                "reason": "task already %s" % s.get("status")}
    with open(os.path.join(ws, "cancel.flag"), "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    return {"task_id": task_id, "status": s.get("status"), "cancelled": True,
            "hint": "已请求取消,主循环将在下一步边界终止,进度保留"}


_TERMINAL_STATUSES = ("done", "cancelled", "max_steps_reached", "error")


def agent_list(limit=20):
    """列出全部 agent 任务(按创建时间倒序),供发现/清理前盘点。

    任务发现即状态:直接扫 index/agent_workspace/*/state.json,无额外登记表。
    """
    rows = []
    if not os.path.isdir(WORKSPACE):
        return rows
    for tid in os.listdir(WORKSPACE):
        p = os.path.join(_ws(tid), "state.json")
        if not os.path.isfile(p):
            continue                                 # 跳过 agent_memory.json 等非任务目录
        try:
            with open(p, "r", encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        rows.append({"task_id": tid, "task": (s.get("task") or "")[:80],
                     "status": s.get("status"), "created_at": s.get("created_at", ""),
                     "steps": len(s.get("steps") or [])})
    rows.sort(key=lambda x: x["created_at"], reverse=True)
    return rows[:max(1, int(limit))]


def agent_cleanup(max_age_hours=72):
    """清理已终态且超过 max_age_hours 的任务工作区(防 agent_workspace 无限增长)。

    只删终态任务(done/cancelled/max_steps_reached/error);running/planning
    永不动——即使刚创建 1 秒也不会被误删。返回 removed/kept 供审计。
    """
    import shutil
    hours = max(0.0, float(max_age_hours))       # 0 = 立即清理全部终态任务(测试/强制回收)
    cutoff = time.time() - hours * 3600
    removed, kept = [], []
    for row in agent_list(limit=10000):
        tid = row["task_id"]
        try:
            mtime = os.path.getmtime(os.path.join(_ws(tid), "state.json"))
        except OSError:
            continue
        if row["status"] in _TERMINAL_STATUSES and mtime < cutoff:
            shutil.rmtree(_ws(tid), ignore_errors=True)
            removed.append(tid)
        else:
            kept.append(tid)
    return {"removed": removed, "kept": len(kept), "max_age_hours": int(max_age_hours)}


def _mark_error(task_id, summary):
    """把卡在 planning/running 的僵尸任务标为 error(后台线程异常终止时调用)。"""
    p = os.path.join(_ws(task_id), "state.json")
    if not os.path.isfile(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            s = json.load(f)
        if s.get("status") in ("planning", "running"):
            s["status"] = "error"
            s["summary"] = str(summary)[:2000]
            _save(s, _ws(task_id))
    except (OSError, ValueError):
        pass


def agent_resume(task_id, extra_steps=6, model=None):
    """续跑一个 max_steps_reached 的任务:复用 todos/历史步骤/滚动摘要,接续步号再跑。

    本地 7B 长任务常步数耗尽未 finish(真实库历史任务约半数如此);
    续跑不重做已完成步骤——紧凑上下文取 state.steps 尾窗口、滚动摘要原样带回,
    seen_ids(含被卸载的 notes 全文)同步恢复,finish 核验/Critic 白名单依然生效。
    仅 max_steps_reached 可续跑;done/cancelled 拒绝。
    """
    ws = _ws(task_id)
    p = os.path.join(ws, "state.json")
    if not os.path.isfile(p):
        return {"error": "no such task", "task_id": task_id}
    try:
        with open(p, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {"error": "state unreadable", "task_id": task_id}
    if state.get("status") != "max_steps_reached":
        return {"error": "only max_steps_reached tasks can be resumed",
                "task_id": task_id, "status": state.get("status")}
    models = core.chat_models()
    if not models:
        return {"status": "skipped", "reason": "no_chat_model",
                "hint": "ollama pull qwen2.5:7b 后重试"}
    model = model or state.get("model") or models[0]
    state["model"] = model
    state["status"] = "running"
    state["resume_count"] = int(state.get("resume_count") or 0) + 1
    _save(state, ws)          # 续跑即刻落盘,agent_status 马上可见

    ctx = {"last_id": None, "seen_ids": set()}
    context = []
    for st in state.get("steps") or []:
        obs = st.get("obs") or ""
        obj = None
        if obs.startswith("[已卸载→"):                    # 卸载观察:读回 notes 全文再收 id
            fname = obs.split("→", 1)[1].split(" ", 1)[0]
            try:
                with open(os.path.join(ws, fname), encoding="utf-8") as f:
                    obj = json.load(f)
            except (OSError, ValueError):
                obj = None
        else:
            try:
                obj = json.loads(obs)
            except ValueError:
                obj = None
        if obj is not None:
            for _i in _collect_ids(obj):
                ctx["seen_ids"].add(_i)
        context.append("step%s[%s] %s" % (st.get("n"), st.get("action"), str(obs)[:400]))
    context = context[-_CONTEXT_TAIL:]                    # 与内存语义一致:只带尾窗口
    digest = state.get("context_digest") or ""
    tools = _build_tools(bool(state.get("allow_write")), ctx)
    return _loop(state, ws, task_id, tools, model, max(1, int(extra_steps)),
                 len(state.get("steps") or []) + 1, context, digest, ctx)


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
_VALID_KINDS = {"images", "videos", "silent", "docs", "audio", "subs", "anim", "other"}


def _tool_search(a):
    q = str(a.get("q") or "").strip()
    kind, tag = str(a.get("kind") or "").strip(), str(a.get("tag") or "").strip()
    limit, mode = int(a.get("limit") or 10), a.get("mode", "auto")
    # 无效 kind 会连浏览兜底一起拖成 0 命中(实测 qwen2.5:7b 会传 document/video)——校验丢弃
    bad_kind = ""
    if kind and kind not in _VALID_KINDS:
        bad_kind, kind = kind, ""

    def _s(query):
        return [_brief(m) for m in core.search(query, kind, tag, limit=limit, mode=mode)]

    rows = _s(q)
    if not rows and q:
        rows = _s(core.expand_query(q))      # 召回兜底1:中文→英文同义词加词(确定性,零 LLM)
    if not rows and q:                       # 召回兜底2:放宽为浏览模式(保留 kind/tag 过滤)
        rows = _s("")
        if rows:
            # 空结果必须给小模型明确反馈,否则会原查询死循环(实测 qwen2.5:7b 连打 10 次空查询)
            note = ("(已丢弃无效 kind:%s)" % bad_kind) if bad_kind else ""
            return {"results": rows, "count": len(rows), "fallback": "browse",
                    "hint": "原查询 0 命中,已自动放宽为浏览模式(kind=%s tag=%s)%s;"
                            "以下为按时间序素材,请自行筛选相关条目"
                            % (kind or "-", tag or "-", note)}
    if rows:
        return rows
    extra = (";注意 kind=%s 不是合法值(合法:images/videos/silent/docs/audio/subs/anim/other)"
             % bad_kind) if bad_kind else ""
    return {"results": [], "count": 0,
            "hint": "0 hits。请换更短的关键词重试:素材文件名片段(如 BV 号)、"
                    "中文主题词;或用 get_material 按 id 直取;或用 chunk_search 查全文。" + extra}


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


def _tool_update_tags(a, ctx=None):
    """写工具:仅 allow_write=True 时注册。id/tags 一律强转 str(LLM 常给 int)。

    清理 ai_tags(幻觉标签)由 core.update_tags 统一负责(默认 purge_ai_tags=True):
    ai_tags 是 LLM 自动打标产物,实测 qwen2.5:7b 给烹饪视频打 subs/codeformer;
    凡不在新 tags 里的 ai_tags token 一律清除,使「删掉幻觉标签」目标真正落地。

    id 兜底:7B 模型常不复制检索结果里的真实 id(改用 BV号/XXX/12345),此时若最近一次
    检索拿到过有效 id,就自动替用该 id(并在返回里标注 used_fallback_id),让任务能真正推进,
    而不是卡在 not found 死循环。
    """
    mid = str(a.get("id") or "").strip()
    used_fallback = None
    if not mid or not core.get_material(mid):
        fallback = (ctx or {}).get("last_id")
        if fallback and core.get_material(fallback):
            used_fallback = mid
            mid = fallback
        else:
            return {"error": "not found: %s" % mid}
    tags = a.get("tags", "")
    if isinstance(tags, (list, tuple)):
        tags = ",".join(str(t).strip() for t in tags if str(t).strip())
    r = core.update_tags(mid, str(tags))
    if used_fallback:
        r["used_fallback_id"] = used_fallback
    return r


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


def _tool_related(a):
    """只读:按关系反查关联素材(Graph-RAG 轻量版)。

    利用库中已有的 `parent:/role:/job:` 面标签与 kind,沿一跳关系遍历:
      rel=parent   该素材的父(若自身带 parent: 标签)
      rel=children 以该素材为 parent 的所有子(逻辑切片/组件)
      rel=job      同 job 的其它素材(同一次流水线的产物)
      rel=role     同 role 的其它素材(如全部 role:master)
      rel=kind     同 kind 的其它素材
      rel=all      以上并集(默认)
    """
    mid = str(a.get("id") or "").strip()
    rel = str(a.get("rel") or "all").strip().lower()
    limit = int(a.get("limit") or 50)
    m = core.get_material(mid)
    if not m:
        return {"error": "not found: " + mid}
    tags = [t.strip() for t in (m.get("tags") or "").split(",") if t.strip()]
    job = next((t for t in tags if t.startswith("job:")), None)
    roles = [t for t in tags if t.startswith("role:")]
    kind = m.get("kind")
    seen, out = {mid}, []

    def _add(rows):
        for r in rows:
            if r["id"] not in seen:
                seen.add(r["id"])
                out.append(_brief(r))

    if rel in ("parent", "all"):
        for t in tags:
            if t.startswith("parent:"):
                p = core.get_material(t.split(":", 1)[1])
                if p:
                    _add([p])
    if rel in ("children", "all"):
        _add(core.search("", tag="parent:" + mid))
    if rel in ("job", "all") and job:
        _add([x for x in core.search("", tag=job) if x["id"] != mid])
    if rel in ("role", "all") and roles:
        for rtag in roles:
            _add([x for x in core.search("", tag=rtag) if x["id"] != mid])
    if rel in ("kind", "all") and kind:
        _add([x for x in core.search("", kind=kind) if x["id"] != mid])
    return {"id": mid, "rel": rel, "count": len(out[:limit]),
            "related": out[:limit]}


def _build_tools(allow_write, ctx=None):
    tools = {
        "search_materials": (_tool_search,
                             "检索素材库;args: q(中文自然语言/关键词), kind?, tag?, limit?, mode?"),
        "get_material": (lambda a: core.get_material(str(a.get("id") or "").strip())
                         or {"error": "not found"},
                         "按 id 取素材完整记录;args: id"),
        "list_tags": (lambda a: [{"tag": t, "count": c} for t, c in core.distinct_tags()[:int(a.get("limit") or 50)]],
                      "列出全部标签及计数;args: limit?"),
        "hub_stats": (lambda a: core.health(),
                      "库健康快照(总量/种类/重复/引用完整性);args: 无"),
        "chunk_search": (_tool_chunk,
                         "长文档父子分块检索(按段落命中笔记/字幕全文);args: q, kind?, tag?, limit?"),
        "read_text_preview": (_tool_preview,
                              "读素材描述/关联文本前 N 字符(取回被卸载的细节用);args: id, chars?"),
        "list_missing_covers": (
            lambda a: core.list_missing_covers(
                kind=str(a.get("kind") or ""),
                limit=int(a.get("limit") or 50)),
            "列出无封面的 videos/silent;args: kind?, limit?"),
        "job_checkup": (
            lambda a: core.job_checkup(str(a.get("job_id") or a.get("job") or "")),
            "某 job 资产体检(kinds/封面/镜头/asr/失效);args: job_id"),
        "near_duplicate_report": (
            lambda a: core.near_duplicate_report(
                max_dist=int(a.get("max_dist") or 10),
                limit_pairs=int(a.get("limit") or 50)),
            "全库画面近重复报告(对+簇);args: max_dist?, limit?"),
        "search_by_image": (
            lambda a: core.search_by_image(
                str(a.get("query") or a.get("id") or a.get("path") or ""),
                max_dist=int(a.get("max_dist") or 10),
                limit=int(a.get("limit") or 20),
                mode=str(a.get("mode") or "auto")),
            "以图搜图(dHash);args: query|id|path, max_dist?, limit?, mode?"),
        "related": (_tool_related,
                    "只读:按关系反查关联素材(父/子/job同伙/同role/同kind);"
                    "args: id, rel?(parent|children|job|role|kind|all), limit?"),
    }
    if allow_write:
        tools["update_tags"] = (_tool_update_tags,
                               "写:更新素材标签;args: id, tags")
        tools["register_asset"] = (_tool_register,
                                   "写:登记外部文件引用(不复制);args: path, tags?, description?")
    # 给写工具注入 ctx(携带最近检索到的 last_id,用于 id 兜底)
    if ctx is not None:
        for name in ("update_tags",):
            if name in tools:
                fn, desc = tools[name]
                tools[name] = (lambda a, _fn=fn: _fn(a, ctx), desc)
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
    """确定性巡检子代理(不走 LLM,零幻觉):健康快照 + 失效引用 + 无封面 + 近重复簇。"""
    h = core.health()
    broken = core.broken_externals()
    missing = core.list_missing_covers(limit=20)
    silent_miss = [x for x in missing if x.get("kind") == "silent"]
    nd = core.near_duplicate_report(max_dist=10, limit_pairs=20)
    return {"health_ok": h.get("ok"), "total": h.get("total"),
            "kinds": h.get("kinds"), "duplicates": h.get("duplicates"),
            "broken": len(broken),
            "broken_ids": [b.get("id") for b in broken[:10]],
            "missing_covers": len(missing),
            "missing_silent_covers": [x["id"] for x in silent_miss[:10]],
            "near_dupe_clusters": nd.get("cluster_count", 0),
            "near_dupe_pairs": nd.get("pair_count", 0),
            "near_dupe_sample": [
                {"ids": c["ids"], "size": c["size"]}
                for c in (nd.get("clusters") or [])[:5]
            ],
            "hint": "清理失效引用请走 MCP prune 或 bridge --prune;"
                    "补封面用 list_missing_covers + auto_process(confirm);"
                    "近重复详单用 near_duplicate_report / cli near-dupes"}


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
finish 的 args 为 {"summary":"中文总结","ids":["本任务检索结果中出现过的素材 id",...],"citations":[{"claim":"一句话结论","id":"本任务检索到的素材 id"},...]}。ids/citations 里的 id 必须是 search_materials/get_material/retrieve 真实返回过的(12 位十六进制);未出现的会被自动剔除,总结正文中若直接写 id 也会被 Critic 校验剔除。不填 ids/citations 也可以,但 summary 不得编造未检索到的素材。

规则:
1. 观察结果过长会被卸载为文件指针;需要细节时用 read_text_preview/get_material 精准取回,不要要求重发全文。
2. 优先用 kind/tag 过滤缩小检索面;需要跨多查询综合时用 retrieve 子代理(只回摘要,省上下文)。
   search_materials 0 命中时会自动做同义词扩展、再放宽为浏览模式(fallback:"browse");拿到浏览结果请自行筛选相关条目,不要当作精确匹配,也不要反复用同一长句重试。
3. 库巡检/失效引用/无封面盘点用 maintain(确定性,零幻觉);补封面细节用 list_missing_covers;job 资产用 job_checkup。
4. 找片段先 get_shots(母版逻辑切片);仅用户要导出文件时才搜 role:clip。silent/audio 是声画组件不是剪辑切片。
5. 每完成一条待办就在 mark_done 里列出其 id;全部完成后必须 finish。
6. 写操作默认不可用;若工具表中没有写工具,不要尝试写入。
7. 素材 id 必须从 search_materials/get_material 返回的 "id" 字段原样复制(形如 80ad93332bca 的短字母数字串)。
   绝不可用 BV 号(BV1q1...)、文件名、或 XXX 占位符当 id——那样会命中 "not found" 并死循环。
8. 若连续检索 0 命中或与目标无关,必须 finish 并如实说明"未能找到相关素材",严禁编造素材 id 或结论;
   宁可少答、不可胡答。finish 时若提供了 ids,它们会自动与已检索结果比对,未出现的一律剔除。
9. 总结或 citations 中提到的素材 id 只能是本任务真实检索返回过的 12 位十六进制串;任何未在检索结果中出现过的 id 都会被自动剔除并标注 [Critic]/[核验],不要试图用占位或编造 id 蒙混。

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
             "summary": "", "context_digest": ""}
    try:
        state["todos"] = _plan(task, model)
    except Exception as e:                                  # noqa: BLE001
        state.update(status="error", summary="plan failed: %s" % e)
        _save(state, ws)
        return {"status": "error", "task_id": task_id, "summary": state["summary"]}

    state["status"] = "running"
    _save(state, ws)          # 计划完成即落盘:任务从创建起就可被 agent_status 轮询/agent_cancel 取消
    ctx = {"last_id": None, "seen_ids": set()}            # 最近检索到的有效素材 id(供写工具兜底) + 全程见过 id(供 finish 核验)
    tools = _build_tools(bool(allow_write), ctx)
    return _loop(state, ws, task_id, tools, model, int(max_steps), 1, [], "", ctx)


def _loop(state, ws, task_id, tools, model, budget, start_step, context, digest, ctx):
    """Deep Agent 主循环(agent_run 全新任务 / agent_resume 续跑共用)。

    budget:本次调用可执行的步数;start_step:步号起点(续跑接续历史步号);
    context/digest/ctx 由调用方传入(全新任务为空,续跑从 state/notes 重建)。
    每步落盘、步边界协作取消、finish 核验/Critic/滚动摘要/记忆沉淀全部不变。
    """
    task = state["task"]
    tool_lines = "".join("  - %s: %s\n" % (n, d) for n, (_, d) in sorted(tools.items()))
    tool_lines += "  - retrieve: 子代理:多查询检索+LLM汇总,只回摘要;args: {goal}\n"
    tool_lines += "  - maintain: 确定性巡检(health+失效引用),零幻觉;args: 无\n"
    tool_lines += "  - finish: 任务完成,输出总结\n"
    sys_prompt = _LOOP_SYS + tool_lines
    mem_hint = _memory_hint()
    if mem_hint:
        sys_prompt += mem_hint

    finished = False
    fails = 0                                             # 连续无效 LLM 响应计数
    for i in range(budget):
        n = start_step + i
        # 协作式取消(缺口1 补全):每步边界检查 cancel.flag,命中即终止;
        # 已执行步骤/观察全部保留,不会半途丢弃,状态标 cancelled 落盘
        if os.path.exists(os.path.join(ws, "cancel.flag")):
            os.remove(os.path.join(ws, "cancel.flag"))
            state["status"] = "cancelled"
            state["summary"] = ("[取消] 用户于第 %d 步边界请求取消;已执行 %d 步,进度已保留"
                                % (n, len(state["steps"])))[:2000]
            _save(state, ws)
            return {"status": "cancelled", "task_id": task_id, "workspace": ws,
                    "summary": state["summary"], "todos": state["todos"],
                    "steps": len(state["steps"])}
        budget_hint = ""
        if budget - i <= 2:      # 步数将尽,催促收尾(7B 小模型常把 todo 做完却忘了 finish)
            budget_hint = "\n(注意:剩余步数很少,若待办已基本完成,请立即 finish 并在 summary 里给出汇总)"
        recent = "\n".join(context[-_CONTEXT_TAIL:]) or "(无)"
        digest_note = ("\n(前情压缩摘要,早期步骤已归档,无需重做)\n%s" % digest) if digest else ""
        user = ("任务:%s\n待办:%s\n最近观察:\n%s%s%s"
                % (task, json.dumps(state["todos"], ensure_ascii=False),
                   recent, digest_note, budget_hint))
        act = _chat([{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user}], model)
        if not isinstance(act, dict):                       # E2E 实测:LLM 失败曾泄漏 None 直接崩线程
            act = {}
        if not (act.get("action") or act.get("thought")):   # 无效响应:重试一次,再失败诚实中止
            fails += 1
            if fails >= 2:
                state["status"] = "error"
                state["summary"] = ("[中止] LLM(%s)连续 %d 次未返回有效 JSON,第 %d 步中止;"
                                    "可稍后重跑同任务" % (model, fails, n))[:2000]
                _save(state, ws)
                return {"status": "error", "task_id": task_id, "workspace": ws,
                        "summary": state["summary"], "steps": len(state["steps"])}
            obs = {"error": "empty llm response, retrying"}
            ptr = _observe(state, ws, obs)
            context.append("step%d[%s] %s" % (n, "llm_retry", ptr[:400]))
            state["steps"].append({"n": n, "action": "llm_retry", "ok": False, "obs": ptr})
            _save(state, ws)
            continue
        fails = 0
        action = act.get("action", "")
        args = act.get("args") or {}
        for tid in act.get("mark_done") or []:
            for t in state["todos"]:
                if str(t["id"]) == str(tid):
                    t["status"] = "done"

        if action == "finish":
            body = str(args.get("summary") or act.get("thought") or "")[:2000]
            summary = body
            # 核验:finish 声明的 id 必须来自本任务真实检索结果,未出现的剔除并标注
            claimed = [str(x).strip() for x in (args.get("ids") or []) if str(x).strip()]
            valid = [i for i in claimed if i in ctx["seen_ids"]]
            dropped = [i for i in claimed if i not in ctx["seen_ids"]]
            state["result_ids"] = valid
            state["dropped_ids"] = dropped
            if dropped:
                summary += ("\n[核验] 已剔除 %d 个未在本任务检索结果中出现的 id: %s"
                            % (len(dropped), ", ".join(dropped)))
            # 结构化引用(缺口4):逐条校验 citations 的 id 是否在本次检索白名单内
            cits, cit_dropped = [], []
            for c in (args.get("citations") or []):
                cid = str((c.get("id") if isinstance(c, dict) else c) or "").strip()
                if not cid:
                    continue
                ok = cid in ctx["seen_ids"]
                cits.append({"claim": str((c.get("claim") if isinstance(c, dict) else "")
                                          or "")[:300], "id": cid, "valid": ok})
                if not ok:
                    cit_dropped.append(cid)
            state["citations"] = cits
            if cit_dropped:
                summary += ("\n[核验] 已剔除 %d 个引用中未检索到的 id: %s"
                            % (len(cit_dropped), ", ".join(cit_dropped)))
            # 确定性 Critic(缺口4):扫描总结正文里的 12 位素材 id,
            # 凡不在本次检索白名单内即标记——专治 7B 在自由文本里编造 id
            unverified = sorted({i for i in re.findall(r"[0-9a-f]{12}", body)
                                 if i not in ctx["seen_ids"]})
            if unverified:
                state["critic"] = {"unverified_ids": unverified}
                summary += ("\n[Critic] 总结中出现了 %d 个未在本任务检索结果中的素材 id(%s),"
                            "可能无效,已剔除相关引用。"
                            % (len(unverified), ", ".join(unverified)))
            state["summary"] = summary
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

        # 记录最近检索到的有效素材 id,供写工具 id 兜底(7B 常不复制真实 id,改用 BV号/XXX)
        if action == "search_materials":
            items = obs if isinstance(obs, list) else (
                obs.get("results") or [] if isinstance(obs, dict) else [])
            if items and isinstance(items[0], dict) and items[0].get("id"):
                ctx["last_id"] = items[0]["id"]        # 首条结果 id 作兜底基准
        elif action == "get_material" and isinstance(obs, dict) and obs.get("id"):
            ctx["last_id"] = obs["id"]
        elif action == "retrieve" and isinstance(obs, dict):
            ids = obs.get("ids") or []
            if ids and isinstance(ids[0], str):
                ctx["last_id"] = ids[0]

        # 累计全程见到过的素材 id(供 finish 时核验,杜绝编造 id)
        for _i in _collect_ids(obs):
            ctx["seen_ids"].add(_i)

        ptr = _observe(state, ws, obs)
        context.append("step%d[%s] %s" % (n, action, ptr[:400]))
        # 动态上下文压缩(缺口6):超出尾窗口的早期步骤压入滚动摘要,
        # 防本地 7B 长任务遗忘/context rot;摘要本身限行防无限增长
        if len(context) > _CONTEXT_TAIL:
            evicted = context[:-_CONTEXT_TAIL]
            context = context[-_CONTEXT_TAIL:]
            lines = (digest.splitlines() if digest else []) + [l[:200] for l in evicted]
            digest = "\n".join(lines[-_DIGEST_MAX:])
        state["context_digest"] = digest
        state["steps"].append({"n": n, "action": action, "ok": "error" not in obs,
                               "thought": str(act.get("thought") or "")[:200],
                               "args": args, "obs": ptr})
        _save(state, ws)                                  # 每步落盘,供 agent_status 实时轮询进度

    if not finished:
        state["status"] = "max_steps_reached"
        # 截断时 summary 带上最近几步的实质进展(而非干巴巴一句),agent_status 一眼可见
        tail = " | ".join(
            (s.get("thought") or s.get("action") or "") for s in state["steps"][-3:]
            if isinstance(s, dict))
        state["summary"] = ("步数预算(至第 %d 步)已用尽,未 finish;最近进展: %s;可 agent_resume 续跑"
                            % (start_step + budget - 1, tail))[:2000]
    else:
        state["status"] = "done"
    _record_memory(state, ctx)                            # 成功任务沉淀跨任务规律
    _save(state, ws)
    return {"status": state["status"], "task_id": task_id, "workspace": ws,
            "summary": state["summary"], "todos": state["todos"],
            "steps": len(state["steps"]), "notes": len(state["notes"]),
            "result_ids": state.get("result_ids", []),
            "dropped_ids": state.get("dropped_ids", []),
            "citations": state.get("citations", []),
            "critic": state.get("critic"),
            "context_digest": state.get("context_digest", "")}


if __name__ == "__main__":                                  # 直接调试: python agent.py "任务..."
    core.init_hub()
    r = agent_run(" ".join(os.sys.argv[1:]) or "巡检素材库健康状态")
    print(json.dumps(r, ensure_ascii=False, indent=2))
