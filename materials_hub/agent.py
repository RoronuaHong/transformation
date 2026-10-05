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

持久化分层(对齐 LangGraph / deepagents 实践):
  · Checkpoint —— agent_workspace/<task_id>/state.json:单次 run 的短时记忆
                  (todos/steps/progress),可轮询/取消/续跑;可被 cleanup 回收。
  · Thread     —— agent_chats/<thread_id>.json:UI 对话全文(append-style 快照),
                  与 checkpoint 解耦;刷新/重开仍可恢复旧对话。绝不把 UI 历史
                  绑在会压缩/清理的 checkpoint 上。

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

# UI 对话线程(与 task checkpoint 分离;刷新后仍可列出/恢复)
CHATS = os.path.join(core.HUB, "index", "agent_chats")

# 跨任务记忆(确定性沉淀,非 LLM):成功任务的「查询词 → 有效 facet」规律,
# 注入下次任务的系统提示,帮本地 7B 模型少走弯路(对应 2026 Agent Memory 的程序记忆)。
# 落在 WORKSPACE 下(派生数据,测试可随 WORKSPACE 重定向到临时目录)。
_MEMORY_PATH = os.path.join(WORKSPACE, "agent_memory.json")
_OFFLOAD_CHARS = 1200        # 观察结果超过该字符数即卸载落盘
_CONTEXT_TAIL = 6            # 主循环上下文最多携带最近 N 条观察
_DIGEST_MAX = 30              # 滚动摘要最多保留的压缩行数(防无限增长)
_DEFAULT_STEPS = 12
_SUB_QUERIES = 3             # retrieve 子代理最多并发查询数
# LoopGuard(对齐 deer-flow / RunGuard):指纹滑动窗口;soft 注入纠正,hard 强制收尾
_LOOP_WARN = 2               # 同一指纹累计出现次数 → 软警告(含首次成功)
_LOOP_HARD = 3               # → 硬停转(强制 recover+finish),不再问 LLM
_LOOP_WINDOW = 16
_TASK_ID_RE = re.compile(r"(?:id[=:]\s*)?([0-9a-f]{12})\b")
_ATTACHED_VIDEO_RE = re.compile(
    r"id=([0-9a-f]{12})\s+kind=(videos|silent)\b", re.I)
_VIDEO_SKILL_HINT = re.compile(
    r"裁剪|镜头|时间轴|分析|方案|分类|母版|get_shots|related|全面测试|水平|上传",
    re.I)
# 兼容旧名(测试/外部可能引用)
_STALL_SOFT = 1


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
    progress = s.get("progress") or _progress_from_state(s)
    return {"task_id": task_id, "task": s.get("task"), "status": s.get("status"),
            "summary": s.get("summary"), "todos": s.get("todos"),
            "steps": len(s.get("steps") or []), "notes": s.get("notes") or [],
            "result_ids": s.get("result_ids", []),
            "dropped_ids": s.get("dropped_ids", []),
            "citations": s.get("citations", []),
            "critic": s.get("critic"),
            "context_digest": s.get("context_digest", ""),
            "progress": progress,
            "skill": s.get("skill"),
            "crop_windows": s.get("crop_windows") or []}


def _progress_from_state(s):
    """从 todos/steps 推导人类可读进度(供轮询 UI)。"""
    todos = s.get("todos") or []
    status = s.get("status") or ""
    if todos:
        done = sum(1 for t in todos if (t.get("status") or "") == "done")
        total = len(todos)
        cur = next((t for t in todos
                    if (t.get("status") or "") in ("running", "in_progress")), None)
        if cur:
            label = cur.get("text") or "执行中"
        elif status == "done" and done >= total:
            label = "已完成"
        elif status == "done":
            label = "部分完成"
        elif done < total:
            label = (todos[done].get("text") if done < total else "进行中") or "进行中"
        else:
            label = "收尾中"
        if status == "done" and done >= total and total:
            pct = 100
        else:
            pct = int(round(100.0 * done / total)) if total else 0
        return {"done": done, "total": total, "pct": min(100, pct), "label": label}
    steps = s.get("steps") or []
    n = len(steps)
    last = steps[-1] if steps else {}
    label = (last.get("action") if isinstance(last, dict) else None) or status or "…"
    return {"done": n, "total": max(n, 1), "pct": 100 if status == "done" else 0,
            "label": str(label)}


def _set_progress(state, ws, todo_id=None, label=None, status_for=None):
    """更新某条 todo 为 running/done,并刷新 progress 落盘(轮询可见)。"""
    todos = state.get("todos") or []
    if todo_id is not None and status_for:
        for t in todos:
            if str(t.get("id")) == str(todo_id):
                t["status"] = status_for
                if label:
                    t["text"] = label
                break
    # 仅一条 running:把其余非 done 置回 pending
    if status_for == "running" and todo_id is not None:
        for t in todos:
            if str(t.get("id")) != str(todo_id) and t.get("status") == "running":
                t["status"] = "pending"
    state["progress"] = _progress_from_state(state)
    if label and state["progress"]:
        state["progress"]["label"] = label
    _save(state, ws)
    return state["progress"]


def _skill_todo_template(mid):
    """方案 A 工作流待办(人类可读,替代易跑偏的 LLM 规划)。"""
    short = (mid or "")[:12]
    return [
        {"id": 1, "text": "读取母版元数据 · %s" % short, "status": "pending"},
        {"id": 2, "text": "反查子组件 related/children", "status": "pending"},
        {"id": 3, "text": "读取镜头时间轴 get_shots", "status": "pending"},
        {"id": 4, "text": "设计裁剪窗(片头/推进/主戏/中段/片尾)", "status": "pending"},
        {"id": 5, "text": "汇总方案并完成", "status": "pending"},
    ]


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
    不影响 agent_chats 对话线程(UI 历史与 checkpoint 解耦)。
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


# ---------------------------------------------------------------- 对话线程(UI 历史)
def _thread_path(thread_id):
    return os.path.join(CHATS, "%s.json" % thread_id)


def _new_thread_id():
    h = hashlib.sha1(("th" + str(time.time()) + str(os.getpid())).encode("utf-8")).hexdigest()
    return "th" + h[:12]


def _sanitize_message(m):
    """只保留可序列化 UI 字段;剥离 blob/previewUrl。"""
    if not isinstance(m, dict):
        return None
    role = m.get("role")
    if role not in ("user", "assistant", "system"):
        return None
    out = {
        "id": str(m.get("id") or _new_thread_id()),
        "role": role,
        "content": str(m.get("content") or "")[:20000],
    }
    if m.get("taskId") or m.get("task_id"):
        out["taskId"] = str(m.get("taskId") or m.get("task_id"))
    if m.get("status"):
        out["status"] = str(m.get("status"))[:64]
    if isinstance(m.get("todos"), list):
        out["todos"] = m["todos"][:40]
    if isinstance(m.get("steps"), int):
        out["steps"] = m["steps"]
    if isinstance(m.get("progress"), dict):
        out["progress"] = m["progress"]
    if m.get("skill"):
        out["skill"] = str(m.get("skill"))[:64]
    wins = m.get("cropWindows") or m.get("crop_windows")
    if isinstance(wins, list):
        clean_w = []
        for w in wins[:24]:
            if not isinstance(w, dict):
                continue
            clean_w.append({
                "id": str(w.get("id") or "")[:16],
                "label": str(w.get("label") or "")[:40],
                "start": w.get("start"),
                "end": w.get("end"),
                "duration": w.get("duration"),
            })
        if clean_w:
            out["cropWindows"] = clean_w
    atts = m.get("attachments")
    if isinstance(atts, list) and atts:
        clean = []
        for a in atts[:20]:
            if not isinstance(a, dict) or not a.get("id"):
                continue
            clean.append({
                "localId": str(a.get("localId") or a.get("id")),
                "id": str(a["id"])[:32],
                "name": str(a.get("name") or "")[:240],
                "kind": str(a.get("kind") or "")[:32],
                "status": str(a.get("status") or "")[:32],
                "mime": str(a.get("mime") or "")[:80],
            })
        if clean:
            out["attachments"] = clean
    return out


def _title_from_messages(messages, fallback="新对话"):
    for m in messages or []:
        if isinstance(m, dict) and m.get("role") == "user":
            t = (m.get("content") or "").strip().replace("\n", " ")
            if t:
                return t[:48]
    return fallback


def thread_list(limit=40):
    """列出对话线程摘要(按 updated_at 倒序)。不含完整 messages。"""
    rows = []
    if not os.path.isdir(CHATS):
        return rows
    for name in os.listdir(CHATS):
        if not name.endswith(".json"):
            continue
        p = os.path.join(CHATS, name)
        try:
            with open(p, "r", encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        tid = s.get("thread_id") or name[:-5]
        msgs = s.get("messages") or []
        rows.append({
            "thread_id": tid,
            "title": s.get("title") or _title_from_messages(msgs),
            "created_at": s.get("created_at", ""),
            "updated_at": s.get("updated_at", ""),
            "message_count": len(msgs),
            "task_ids": s.get("task_ids") or [],
        })
    rows.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
    return rows[:max(1, int(limit))]


def thread_get(thread_id):
    """读取完整对话线程(含 messages)。"""
    tid = str(thread_id or "").strip()
    if not tid or not re.match(r"^th[0-9a-f]{8,20}$", tid):
        return {"error": "invalid thread_id", "thread_id": tid}
    p = _thread_path(tid)
    if not os.path.isfile(p):
        return {"error": "no such thread", "thread_id": tid}
    try:
        with open(p, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return {"error": "thread unreadable", "thread_id": tid}
    s["thread_id"] = tid
    return s


def thread_save(thread_id=None, title="", messages=None, task_ids=None):
    """创建或覆盖保存对话线程(UI 快照整表写入)。

    messages 为前端 Turn 列表;与 agent_workspace checkpoint 无关。
    未传 thread_id 时自动分配 th… 新 id。
    """
    msgs_in = messages if isinstance(messages, list) else []
    clean = []
    for m in msgs_in[:200]:
        sm = _sanitize_message(m)
        if sm:
            clean.append(sm)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    tid = str(thread_id or "").strip()
    if tid and not re.match(r"^th[0-9a-f]{8,20}$", tid):
        return {"error": "invalid thread_id", "thread_id": tid}
    if not tid:
        tid = _new_thread_id()
    os.makedirs(CHATS, exist_ok=True)
    p = _thread_path(tid)
    prev = {}
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                prev = json.load(f) or {}
        except (OSError, ValueError):
            prev = {}
    tids = task_ids if isinstance(task_ids, list) else (prev.get("task_ids") or [])
    # 从消息里补齐 task_ids
    seen = set()
    merged_tids = []
    for x in list(tids) + [m.get("taskId") for m in clean if m.get("taskId")]:
        if not x:
            continue
        sx = str(x)
        if sx in seen:
            continue
        seen.add(sx)
        merged_tids.append(sx)
    title_s = (title or "").strip() or prev.get("title") or _title_from_messages(clean)
    state = {
        "thread_id": tid,
        "title": title_s[:80],
        "created_at": prev.get("created_at") or now,
        "updated_at": now,
        "messages": clean,
        "task_ids": merged_tids[:50],
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    return {
        "ok": True,
        "thread_id": tid,
        "title": state["title"],
        "message_count": len(clean),
        "updated_at": now,
        "created_at": state["created_at"],
        "task_ids": state["task_ids"],
    }


def thread_delete(thread_id):
    """删除对话线程文件;不触碰关联的 agent_workspace checkpoint。"""
    tid = str(thread_id or "").strip()
    if not tid or not re.match(r"^th[0-9a-f]{8,20}$", tid):
        return {"error": "invalid thread_id", "thread_id": tid}
    p = _thread_path(tid)
    if not os.path.isfile(p):
        return {"error": "no such thread", "thread_id": tid, "deleted": False}
    try:
        os.remove(p)
    except OSError as e:
        return {"error": str(e), "thread_id": tid, "deleted": False}
    return {"ok": True, "thread_id": tid, "deleted": True}


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
    _seed_ids_from_task(state.get("task") or "", ctx)
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
    """Write tool: merge tags (add-only). System facet tags never deleted.

    Uses core.merge_material_tags. Id fallback via ctx last_id when LLM
    passes BV/placeholder instead of hub id.

    E2E lesson: 7B only sends new tags; replace would wipe job:/role:/type:.
    Merge keeps existing; remove= deletes only non-system tags.
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
    r = core.merge_material_tags(mid, a.get("tags", ""), a.get("remove"))
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


def _tool_get_shots(a):
    """读镜头时间轴;给 7B 可读摘要(scene_count + scenes),避免误以为无数据。"""
    mid = str(a.get("id") or "").strip()
    d = core.get_shots(mid)
    if not d:
        return {"error": "no_shots", "id": mid,
                "hint": "尚无镜头索引;可 finish 说明缺口,或换 related/job_checkup"}
    scenes = d.get("scenes") or []
    return {
        "id": mid,
        "duration": d.get("duration"),
        "threshold": d.get("threshold"),
        "scene_count": len(scenes),
        "scenes": scenes[:40],
        "hint": "scenes[].start/end 为逻辑切片(秒);据此给裁剪区间后 finish。"
                "禁止再 get_material;silent/audio≠剪辑切片。",
    }


def _tool_assemble_package(a):
    """只读:把高层目标落成可交付素材包(Librarian→Critic→Executor);只写 manifest,不改资产。"""
    return _assemble_package(
        str(a.get("goal") or a.get("task") or "").strip(),
        kind=str(a.get("kind") or "").strip(),
        limit=int(a.get("limit") or 12),
        scope=str(a.get("scope") or "all").strip().lower(),
        queries=a.get("queries") or None)


def _tool_deliver(a):
    """写:把素材包/指定 id 导出为下游交付变体(转码/区间裁剪/格式归一)。

    安全:只新建交付文件、绝不改动资产本体;必须显式 confirm=true 才真正导出,
    否则返回 dry-run 计划(与 MCP t_deliver 同一护栏)。"""
    if not a.get("confirm") is True:
        return {"error": "deliver requires confirm=true (Human-in-the-loop)"}
    ids = [str(x).strip() for x in (a.get("ids") or []) if str(x).strip()]
    clips = None
    raw_clips = a.get("clips") or {}
    if isinstance(raw_clips, dict):
        clips = {}
        for k, v in raw_clips.items():
            try:
                clips[str(k)] = (float(v[0]), float(v[1]))
            except (TypeError, ValueError, IndexError):
                continue
    return core.deliver_package(
        manifest_path=str(a.get("manifest") or "").strip() or None,
        ids=ids or None, out_dir=(str(a.get("out_dir") or "").strip() or None),
        confirm=True, fmt=str(a.get("fmt") or "mp4").strip(),
        res=str(a.get("res") or "720").strip(), clips=clips,
        copy_only=bool(a.get("copy_only")),
        overwrite=bool(a.get("overwrite")))


def _seed_ids_from_task(task, ctx):
    """从任务正文预填 seen_ids/last_id(上传附带的 id=xxxxxxxx 真源)。"""
    for mid in _TASK_ID_RE.findall(task or ""):
        ctx["seen_ids"].add(mid)
        ctx["last_id"] = mid


def _tool_sig(action, args):
    """Canonical fingerprint: action + sorted args (RunGuard / deer-flow)."""
    return "%s|%s" % (
        action,
        json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)[:240],
    )


class _LoopGuard:
    """指纹滑动窗口防空转(deer-flow LoopDetectionMiddleware / RunGuard 同构)。

    observe(sig) → None | "warn" | "hard"
      - warn: 同指纹累计 ≥ _LOOP_WARN → 下一轮注入纠正提示
      - hard: 同指纹累计 ≥ _LOOP_HARD → 调用方必须强制收尾,不再执行该工具
    """

    def __init__(self, warn=_LOOP_WARN, hard=_LOOP_HARD, window=_LOOP_WINDOW):
        self.warn, self.hard, self.window = warn, hard, window
        self._hist = []

    def observe(self, sig):
        if not sig:
            return None
        self._hist.append(sig)
        if len(self._hist) > self.window:
            self._hist = self._hist[-self.window:]
        n = self._hist.count(sig)
        if n >= self.hard:
            return "hard"
        if n >= self.warn:
            return "warn"
        return None


def _actions_done(state):
    """本任务已成功执行过的 action 集合(不含 repeat 短路)。"""
    out = set()
    for s in state.get("steps") or []:
        if isinstance(s, dict) and s.get("ok") and not s.get("repeat") and s.get("action"):
            out.add(s["action"])
    return out


def _attached_video_ids(task):
    """解析前端 buildAgentTask 写入的「id=… kind=videos|silent」真源行。"""
    return [m.group(1) for m in _ATTACHED_VIDEO_RE.finditer(task or "")]


def _crop_windows(scenes, duration=0):
    """确定性裁剪窗口(覆盖片头/中段/最长/片尾),不依赖 LLM 编造时间码。"""
    if not scenes:
        return []
    scored = []
    for i, s in enumerate(scenes):
        if not isinstance(s, dict):
            continue
        a, b = float(s.get("start") or 0), float(s.get("end") or 0)
        scored.append((i, a, b, max(0.0, b - a)))
    if not scored:
        return []
    n = len(scored)
    picks = []
    # 片头 / 前段
    picks.append(("片头建立", scored[0]))
    if n > 2:
        picks.append(("前段推进", scored[min(2, n - 1)]))
    # 最长镜头 → 高潮/主戏
    longest = max(scored, key=lambda x: x[3])
    picks.append(("主戏最长镜", longest))
    # 中段
    mid = scored[n // 2]
    picks.append(("中段过渡", mid))
    # 片尾
    picks.append(("片尾收束", scored[-1]))
    # 去重(同 index 只留一次),保序
    seen, out = set(), []
    for label, (idx, a, b, dur) in picks:
        if idx in seen:
            continue
        seen.add(idx)
        out.append({
            "label": label,
            "start": round(a, 3),
            "end": round(b, 3),
            "duration": round(dur, 3),
            "scene_index": idx,
        })
    if duration:
        out.append({
            "label": "整片时长",
            "start": 0.0,
            "end": round(float(duration), 3),
            "duration": round(float(duration), 3),
            "scene_index": -1,
        })
    return out


def _remember_crop_windows(state, mid, crops):
    """把本条素材的裁剪窗记到状态上,供轮询和对话界面展示。"""
    rows = []
    for c in crops or []:
        if not isinstance(c, dict):
            continue
        rows.append({
            "id": mid,
            "label": c.get("label") or "",
            "start": c.get("start"),
            "end": c.get("end"),
            "duration": c.get("duration"),
        })
    prev = [r for r in (state.get("crop_windows") or [])
            if isinstance(r, dict) and r.get("id") != mid]
    state["crop_windows"] = (prev + rows)[:24]


def _format_crop_plan(mid, mat, shots, related, crops):
    """把确定性结果写成可读中文总结(workflow 收尾,不靠 7B 编时间码)。"""
    name = (mat or {}).get("name") or mid
    tags = (mat or {}).get("tags") or ""
    dur = (shots or {}).get("duration")
    n_sc = int((shots or {}).get("scene_count") or 0)
    n_rel = int((related or {}).get("count") or 0)
    lines = [
        "[方案A工作流] 母版 %s（%s）" % (name, mid),
        "标签: %s" % (tags or "(无)"),
        "时长: %ss · 逻辑镜头: %d · 子组件(related/children): %d"
        % (dur if dur is not None else "?", n_sc, n_rel),
    ]
    if n_rel:
        kids = (related or {}).get("related") or []
        bits = ["%s/%s" % (k.get("kind"), k.get("id")) for k in kids[:6] if isinstance(k, dict)]
        if bits:
            lines.append("关联: " + ", ".join(bits))
    if crops:
        specified = all(str(c.get("label") or "").startswith("指定") for c in crops)
        lines.append("按指定起止:" if specified else "推荐裁剪窗口(覆盖片头/推进/主戏/中段/片尾):")
        for c in crops:
            if c.get("scene_index", 0) == -1:
                lines.append("  · %s: 0–%ss（整片）" % (c["label"], c["end"]))
            elif int(c.get("scene_index") or 0) < 0:
                lines.append(
                    "  · %s: %.2f–%.2fs（%.1fs）"
                    % (c["label"], c["start"], c["end"], c["duration"])
                )
            else:
                lines.append(
                    "  · %s: %.2f–%.2fs（%.1fs, scene#%d）"
                    % (c["label"], c["start"], c["end"], c["duration"], c["scene_index"])
                )
        lines.append("说明: 以上为逻辑切片时间轴,不是物理 clip 文件;"
                     "silent/audio 为声画组件。导出时再搜 role:clip。")
    elif shots and shots.get("error"):
        lines.append("镜头索引缺失: %s — 可先对母版建 shots 再裁。"
                     % shots.get("error"))
    else:
        lines.append("暂无可用镜头时间轴,无法给出精确裁剪窗。")
    return "\n".join(lines)


def _skill_attached_videos(state, ws, tools, ctx, start_n=1):
    """上传视频 → 确定性方案 A 工作流(Anthropic: 固定序列用 workflow 而非开放 Agent)。

    执行 get_material → related(children) → get_shots → 裁剪窗挑选。
    返回 (summary|None, next_step_no, context_lines, digest_extra)。
    summary 非空表示已可直接 finish(分析/裁剪类任务)。
    """
    task = state.get("task") or ""
    mids = _attached_video_ids(task)
    if not mids:
        return None, start_n, [], ""
    n = start_n
    context, parts = [], []
    # 用工作流待办覆盖 LLM 规划,轮询可见人类进度
    state["todos"] = _skill_todo_template(mids[0])
    state["skill"] = "attached_video"
    _set_progress(state, ws, label="方案 A 工作流启动")

    action_todo = {"get_material": 1, "related": 2, "get_shots": 3}
    for mid in mids[:3]:
        ctx["last_id"] = mid
        ctx["seen_ids"].add(mid)
        bundle = {}
        for action, args in (
            ("get_material", {"id": mid}),
            ("related", {"id": mid, "rel": "children", "limit": 20}),
            ("get_shots", {"id": mid}),
        ):
            if action not in tools:
                continue
            tid = action_todo.get(action)
            if tid:
                _set_progress(state, ws, todo_id=tid, status_for="running",
                              label=(state["todos"][tid - 1]["text"]
                                     if tid <= len(state["todos"]) else action))
            try:
                obs = tools[action][0](args)
            except Exception as e:                              # noqa: BLE001
                obs = {"error": "%s: %s" % (type(e).__name__, e)}
            for _i in _collect_ids(obs):
                ctx["seen_ids"].add(_i)
            if isinstance(obs, dict) and obs.get("id"):
                ctx["last_id"] = obs["id"]
            ptr = _observe(state, ws, obs)
            context.append("step%d[%s] %s" % (n, action, ptr[:400]))
            state["steps"].append({
                "n": n, "action": action,
                "ok": isinstance(obs, dict) and "error" not in obs,
                "args": args, "obs": ptr,
                "_sig": _tool_sig(action, args),
                "skill": "attached_video",
            })
            bundle[action] = obs
            n += 1
            if tid:
                _set_progress(state, ws, todo_id=tid, status_for="done")
            else:
                _save(state, ws)

        mat = bundle.get("get_material") if isinstance(bundle.get("get_material"), dict) else {}
        shots = bundle.get("get_shots") if isinstance(bundle.get("get_shots"), dict) else {}
        related = bundle.get("related") if isinstance(bundle.get("related"), dict) else {}
        scenes = (shots or {}).get("scenes") or []
        crops = _crop_windows(scenes, (shots or {}).get("duration") or 0)
        _remember_crop_windows(state, mid, crops)
        plan = _format_crop_plan(mid, mat, shots, related, crops)
        parts.append(plan)
        _set_progress(state, ws, todo_id=4, status_for="running",
                      label="设计裁剪窗(片头/推进/主戏/中段/片尾)")
        ptr = _observe(state, ws, {"id": mid, "crop_windows": crops, "plan": plan})
        context.append("step%d[crop_plan] %s" % (n, ptr[:400]))
        state["steps"].append({
            "n": n, "action": "crop_plan", "ok": True,
            "args": {"id": mid}, "obs": ptr,
            "_sig": _tool_sig("crop_plan", {"id": mid}),
            "skill": "attached_video",
        })
        n += 1
        _set_progress(state, ws, todo_id=4, status_for="done")

    summary = "\n\n".join(parts)
    force = bool(_VIDEO_SKILL_HINT.search(task)) or ("用户刚上传" in task)
    return (summary if force else None), n, context, summary[:800]


def _stall_recover(state, tools, ctx):
    """LLM 死循环停转:确定性补跑主路径工具(get_shots/related),再交 finish。

    实测 qwen2.5:7b 会对同一 get_material 连打 10+ 次(软 hint 无效);
    有 last_id 时直接推进方案 A 主路径,避免空耗步数。
    """
    mid = ctx.get("last_id")
    done = _actions_done(state)
    recovered = {}
    if mid and "get_shots" in tools and "get_shots" not in done:
        try:
            recovered["get_shots"] = tools["get_shots"][0]({"id": mid})
        except Exception as e:                              # noqa: BLE001
            recovered["get_shots"] = {"error": "%s: %s" % (type(e).__name__, e)}
    if mid and "related" in tools and "related" not in done:
        try:
            recovered["related"] = tools["related"][0](
                {"id": mid, "rel": "children", "limit": 20})
        except Exception as e:                              # noqa: BLE001
            recovered["related"] = {"error": "%s: %s" % (type(e).__name__, e)}
    for obs in recovered.values():
        for _i in _collect_ids(obs):
            ctx["seen_ids"].add(_i)
    shots = recovered.get("get_shots") or {}
    rel = recovered.get("related") or {}
    n_scenes = int(shots.get("scene_count") or 0) if isinstance(shots, dict) else 0
    n_rel = int(rel.get("count") or 0) if isinstance(rel, dict) else 0
    crops = []
    if isinstance(shots, dict) and not shots.get("error"):
        crops = _crop_windows(shots.get("scenes") or [], shots.get("duration") or 0)
    parts = ["[LoopGuard硬停转] 重复工具调用已打断"]
    if mid:
        parts.append("素材 %s" % mid)
    if "get_shots" in recovered:
        parts.append("get_shots→%d 镜" % n_scenes)
    if "related" in recovered:
        parts.append("related(children)→%d 条" % n_rel)
    if crops:
        sample = crops[:4]
        segs = "; ".join("%s %.1f-%.1fs" % (c["label"], c["start"], c["end"])
                         for c in sample)
        parts.append("裁剪窗: %s" % segs)
    elif mid and "get_shots" in recovered and n_scenes == 0:
        parts.append("尚无可用镜头时间轴")
    summary = "；".join(parts)
    return recovered, summary


def _build_tools(allow_write, ctx=None):
    tools = {
        "search_materials": (_tool_search,
                             "检索素材库;args: q(中文自然语言/关键词), kind?, tag?, limit?, mode?"),
        "get_material": (lambda a: core.get_material(str(a.get("id") or "").strip())
                         or {"error": "not found", "retryable": False,
                             "hint": "id 无效;换真实 12 位 id 或 finish。同一 id 勿再调"},
                         "按 id 取素材完整记录;同一 id 只调一次,成功后改 get_shots/related/finish;"
                         "args: id"),
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
        "get_shots": (
            _tool_get_shots,
            "读母版镜头时间轴(scenes[].start/end);同一母版只调一次,拿到后基于时间轴 finish;"
            "args: id(母版)"),
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
            "以图搜图(dHash/CLIP);args: query|id|path, max_dist?, limit?, mode?"),
        "search_by_text_image": (
            lambda a: core.search_by_text_image(
                str(a.get("q") or a.get("text") or a.get("query") or ""),
                limit=int(a.get("limit") or 20)),
            "以文搜图(CLIP);args: q|text, limit?"),
        "related": (_tool_related,
                    "按关系反查(父/子/job/role/kind);同一 id+rel 只调一次;"
                    "args: id, rel?(parent|children|job|role|kind|all), limit?"),
        "assemble_package": (_tool_assemble_package,
                             "只读:把高层目标落成可交付素材包(Librarian 拆解检索→Critic 核验"
                             "→Executor 写 manifest 到 agent_workspace/packages/);绝不改动资产;"
                             "args: goal, kind?, scope?(all|master|clips), limit?, queries?"),
    }
    if allow_write:
        tools["update_tags"] = (_tool_update_tags,
                               "写:合并打标(只增不删;系统面标签永不动;删须 remove);"
                               "args: id, tags, remove?")
        tools["register_asset"] = (_tool_register,
                                   "写:登记外部文件引用(不复制);args: path, tags?, description?")
        tools["deliver"] = (_tool_deliver,
                            "写:把素材包(manifest)/指定 id 导出为下游交付变体(转码/区间裁剪/"
                            "格式归一);只新建文件不改资产;args: manifest?, ids?, confirm(必须 true),"
                            " fmt?, res?(720/1080/0), clips?{id:[s,e]}, out_dir?, copy_only?, overwrite?")
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
    # 子代理只许回真实命中的 id(Deep Agent: 子上下文隔离,摘要不得把幻觉 id 漏进主白名单)
    hit_ids = [h.get("id") for h in hits if isinstance(h, dict) and h.get("id")]
    allow = set(hit_ids)
    claimed = [str(i) for i in (r2.get("ids") or []) if isinstance(i, str) and i]
    valid = [i for i in claimed if i in allow]
    dropped = [i for i in claimed if i not in allow]
    if not valid:
        valid = hit_ids[:5]
    return {"summary": r2.get("summary", ""), "ids": valid,
            "dropped_ids": dropped, "queries": queries, "n_hits": len(hits)}


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


# ---------------------------------------------------------------- 目标→素材包组装(#5) + 多智能体编排(#10)
_GOAL_DECOMPOSE_SYS = (
    "你是素材包的检索规划器。把用户的高层目标拆成最多 %d 个互补的中文/英文检索查询词"
    '(输出 {"queries":["..."]});只输出 JSON。' % _SUB_QUERIES
)


def _decompose_goal(goal, model):
    """Librarian 子代理:目标拆解。有模型走 LLM 多查询;否则确定性兜底(目标+同义词扩展)。"""
    if model:
        try:
            r = _chat([{"role": "system", "content": _GOAL_DECOMPOSE_SYS},
                       {"role": "user", "content": "目标:%s" % goal}], model)
            qs = [q for q in (r.get("queries") or [])
                  if isinstance(q, str) and q.strip()][:_SUB_QUERIES]
            if qs:
                return qs
        except Exception:                                 # noqa: BLE001
            pass
    base = (goal or "").strip()
    qs = [base]
    try:
        exp = core.expand_query(base)
        if exp and exp != base:
            qs.append(exp)
    except Exception:                                     # noqa: BLE001
        pass
    return qs[:_SUB_QUERIES] or [base or "素材"]


def _brief_pkg(rec):
    """素材包资产精简视图(含 role/has_cover,供下游工作台消费)。"""
    tags = rec.get("tags") or ""
    role = ""
    for t in tags.split(","):
        if t.startswith("role:"):
            role = t.split(":", 1)[1]
            break
    return {
        "id": rec.get("id"), "kind": rec.get("kind"),
        "name": rec.get("name", ""), "role": role,
        "has_cover": bool(rec.get("thumb")),
        "tags": tags,
        "description": (rec.get("description") or "")[:200],
    }


def _write_package_manifest(goal, manifest):
    d = os.path.join(WORKSPACE, "packages")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"\W+", "_", (goal or "goal"))[:40].strip("_") or "goal"
    fname = "%s_%s.json" % (safe, time.strftime("%Y%m%d_%H%M%S"))
    path = os.path.join(d, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return path


def _assemble_package(goal, model=None, kind="", limit=12, scope="all",
                      queries=None, ws=None):
    """#5 目标→素材包组装 + #10 多智能体编排(进程内 Supervisor→Librarian→Critic→Executor)。

    只读资产:绝不调用任何写工具;仅把交付 manifest 写到 agent_workspace/packages/。
    角色链:
      Librarian —— 目标拆解 + 多查询检索,只收真实命中;
      Critic    —— 确定性核验(真实存在 + scope 过滤),丢弃编造/不符的 id;
      Executor  —— 组装 manifest JSON 并落盘,给出下一步建议。
    """
    if scope not in ("all", "master", "clips"):
        scope = "all"
    qs = ([str(q) for q in queries if str(q).strip()][:_SUB_QUERIES]
          if queries else _decompose_goal(goal, model))
    if not qs:
        qs = [(goal or "素材").strip()]

    # Librarian: 多查询检索,只收真实命中
    seen, candidates = set(), []
    per = max(4, (limit // max(1, len(qs))) + 4)
    for q in qs:
        try:
            for m in core.search(q, kind=kind, limit=per):
                mid = m.get("id")
                if mid and mid not in seen:
                    seen.add(mid)
                    candidates.append(m)
        except Exception:                                 # noqa: BLE001
            continue

    # Critic: 确定性核验(真实存在 + scope 过滤)
    verified, dropped = [], []
    for m in candidates:
        mid = m.get("id")
        rec = core.get_material(mid) if mid else None
        if not rec:
            dropped.append(mid); continue
        tags = rec.get("tags") or ""
        if scope == "master" and "role:master" not in tags:
            dropped.append(mid); continue
        if scope == "clips" and "role:clip" not in tags:
            dropped.append(mid); continue
        verified.append(rec)
    verified = verified[:limit]

    # Executor: 组装 manifest 并落盘
    manifest = {
        "goal": goal, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "schema": "materials-hub/package@1",
        "queries": qs, "scope": scope, "kind": kind or "all",
        "asset_count": len(verified),
        "assets": [_brief_pkg(r) for r in verified],
        "dropped_ids": dropped, "source": "assemble_package",
        "roles": ["Librarian", "Critic", "Executor"],
        "suggested_next": ("素材包为只读交付物;物理切片/导出请把 assets[].id 与时间轴交给工作台"
                           "逐条裁剪并登记 role:clip;补封面/打标走 auto_process / autotag。"),
    }
    path = _write_package_manifest(goal, manifest)
    summary = ("[素材包组装] 角色链 Librarian→Critic→Executor 完成。目标:%s · 检索查询 %d · "
               "候选 %d · 核验通过 %d(丢弃 %d) · 已写出 %s"
               % (goal[:40], len(qs), len(candidates), len(verified),
                  len(dropped), os.path.basename(path)))
    return {"path": path, "summary": summary,
            "asset_count": len(verified),
            "ids": [r.get("id") for r in verified[:8]],
            "dropped_ids": dropped, "queries": qs,
            "manifest": manifest, "roles": manifest["roles"]}


_PKG_PREFIX_RE = re.compile(
    r"^(?:帮我|请|麻烦|能不能|可以)?\s*(?:把|将)?\s*(?:这些|这个|关于)?\s*"
    r"(?:素材|视频|内容)?\s*(?:组装|打包|整理|生成|做一个|做一份|产出)\s*"
    r"(?:一个|一份|一下)?\s*(?:关于)?", re.I)


def _extract_package_goal(task):
    t = (task or "").strip()
    g = _PKG_PREFIX_RE.sub("", t).strip(" ：:，,。.、")
    return g or t


def _run_package_skill(state, ws, ctx, call):
    """Skill:目标→素材包组装(确定性工作流,内部跑 Librarian→Critic→Executor 角色链)。"""
    task = state.get("task") or ""
    goal = _extract_package_goal(task)
    model = state.get("model")
    res = _assemble_package(goal, model=model, ws=ws)
    for i in res.get("ids", []):
        ctx["seen_ids"].add(str(i))
    n = len(state.get("steps") or []) + 1
    ptr = _observe(state, ws, {
        "package": os.path.basename(res.get("path", "")),
        "asset_count": res.get("asset_count"),
        "roles": res.get("roles"),
    })
    state["steps"].append({
        "n": n, "action": "assemble_package", "ok": True,
        "args": {"goal": goal[:80]}, "obs": ptr,
        "_sig": _tool_sig("assemble_package", {"goal": goal[:80]}),
        "skill": "package",
    })
    if res.get("dropped_ids"):
        res["summary"] += "\n丢弃(id 不存在或不符 scope): " + ", ".join(
            str(x) for x in res["dropped_ids"][:8])
    return res.get("summary")


def _run_deliver_skill(state, ws, ctx, call):
    """Skill:把素材包/指定 id 导出为下游交付变体(转码/区间裁剪/格式归一)。

    写操作:仅在用户已授权写入(allow_write,即 CLI --write / MCP confirm=true)
    时真正导出;否则返回 dry-run 计划并提示需开启写权限。绝不改动资产本体。"""
    task = state.get("task") or ""
    # 解析 manifest 路径(任务里出现的 .json)或 id(12 位十六进制)
    manifest = ""
    for tok in re.findall(r"\S+\.json", task):
        if os.path.exists(tok):
            manifest = tok
            break
    ids = re.findall(r"\b[0-9a-f]{12}\b", task)
    allow_write = bool(ctx.get("allow_write"))
    res = core.deliver_package(
        manifest_path=manifest or None, ids=ids or None,
        confirm=allow_write, fmt="mp4", res="720")
    if res.get("error"):
        return "[交付] " + res["error"]
    n_plan = len(res.get("plan") or [])
    n_written = len(res.get("written") or [])
    n_skip = len(res.get("skipped") or [])
    n_err = len(res.get("errors") or [])
    if allow_write:
        summary = ("[交付] 已导出 %d 个交付变体(计划 %d · 跳过 %d · 失败 %d),"
                   "落到 %s" % (n_written, n_plan, n_skip, n_err, res.get("out_dir")))
    else:
        summary = ("[交付] dry-run:计划导出 %d 个(未写文件;开启 --write / confirm=true "
                   "才真正转码导出)。目标目录 %s" % (n_plan, res.get("out_dir")))
    for i in ids:
        ctx["seen_ids"].add(str(i))
    n = len(state.get("steps") or []) + 1
    ptr = _observe(state, ws, {
        "deliver_out_dir": res.get("out_dir"), "plan": n_plan,
        "written": n_written, "skipped": n_skip, "errors": n_err,
        "dry_run": res.get("dry_run"),
    })
    state["steps"].append({
        "n": n, "action": "deliver", "ok": True,
        "args": {"manifest": bool(manifest), "ids": ids[:8],
                 "confirm": allow_write}, "obs": ptr,
        "_sig": _tool_sig("deliver", {"manifest": bool(manifest),
                                      "ids": ids[:8]}),
        "skill": "deliver",
    })
    return summary


# ---------------------------------------------------------------- 规划与主循环(支柱 1/4)
_PLAN_SYS = ("你是素材库任务的规划器。把任务拆成 3-5 条可执行待办"
             '(输出 {"todos":[{"id":1,"text":"..."}]})。'
             "每条必须对应真实工具(get_material/get_shots/related/search_materials/"
             "maintain/job_checkup/search_by_image 等);"
             "同一素材 get_material 只规划一次;时间轴用 get_shots(不是 related);"
             "不要规划「设备兼容性测试」「全面测试水平」等无工具步骤。"
             "用户只是问候、跑题或没说清要做什么时,输出 {\"todos\":[]}。"
             "禁止把这种话默认拆成 search_materials、maintain、job_checkup。"
             "面向检索/巡检/整理,不要空话。只输出 JSON。")


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
4. 找片段/裁剪设计:先 get_material(一次)→related(children)→get_shots;用 scenes[].start/end 给裁剪区间后 finish。
   仅用户要导出文件时才搜 role:clip。silent/audio 是声画组件不是剪辑切片。
5. 每完成一条待办就在 mark_done 里列出其 id;全部完成后必须 finish。
6. 写操作默认不可用;若工具表中没有写工具,不要尝试写入。
   有 update_tags 时为**合并语义**(只增不删;job:/role:/type: 等系统面标签永不动);
   删普通标签须显式传 remove,不要指望「只传新标签」会替换整串。
7. 素材 id 必须从 search_materials/get_material 返回的 "id" 字段原样复制(形如 80ad93332bca 的短字母数字串)。
   绝不可用 BV 号(BV1q1...)、文件名、或 XXX 占位符当 id——那样会命中 "not found" 并死循环。
8. 若连续检索 0 命中或与目标无关,必须 finish 并如实说明"未能找到相关素材",严禁编造素材 id 或结论;
   宁可少答、不可胡答。finish 时若提供了 ids,它们会自动与已检索结果比对,未出现的一律剔除。
9. 总结或 citations 中提到的素材 id 只能是本任务真实检索返回过的 12 位十六进制串;任何未在检索结果中出现过的 id 都会被自动剔除并标注 [Critic]/[核验],不要试图用占位或编造 id 蒙混。
10. 同一 action+相同 args 严禁重复。若观察出现 status=repeat / stall,必须立刻换工具(get_shots/related/finish),
    禁止再调同一工具;无「设备兼容性测试」类工具,勿空转。

可用工具:
"""


_IMAGE_ATTACH_RE = re.compile(
    r"id=([0-9a-f]{12})\s+kind=(images|anim)\b", re.I)
_JOB_ATTACH_RE = re.compile(r"job[:：\s]+([A-Za-z0-9_\-]{4,})", re.I)


_CHITCHAT_RE = re.compile(
    r"^(?:"
    r"你好啊?|您好|嗨|哈喽|哈啰|hi|hello|hey|在吗|在嘛|在麼|"
    r"你是谁|你是誰|你能做什么|你能做什麼|你会什么|你会什麼|"
    r"谢谢|謝謝|thanks|thank you|早上好|晚上好|早安|晚安"
    r")[!！。.~～?？\s]*$",
    re.I,
)


def _norm_turn(task):
    t = re.sub(r"\s+", "", (task or "").strip())
    return t.strip("!！。.~～?？,，")


def _has_library_signal(task):
    return bool(re.search(
        r"封面|重复|重複|巡检|巡檢|镜头|鏡頭|素材|检索|檢索|搜索|找出|查找|"
        r"job|search|maintain|视频|視頻|图片|圖片|母版|切片|字幕|标签|入库",
        task or "", re.I))


def _is_chitchat(task):
    """问候、致谢、问能力。不是素材任务。"""
    t = _norm_turn(task)
    if not t or len(t) > 16 or _has_library_signal(t):
        return False
    return bool(_CHITCHAT_RE.match(t)) or t.lower() in ("help", "帮助")


def _is_offtopic(task):
    """与素材库无关的闲聊。库内词优先,不判跑题。"""
    if _has_library_signal(task) or _match_named_skill(task):
        return False
    return bool(re.search(
        r"天气|笑话|股票|彩票|星座|菜谱|做饭|写诗|写一首|新闻联播|讲个笑话",
        task or ""))


_VAGUE_RE = re.compile(
    r"^(?:"
    r"看看|看一下|帮我看|帮我看看|处理一下|搞一下|优化一下|测试一下|全面测试|"
    r"检查一下|查一下|帮忙|帮我|随便|不知道|嗯|好的?|ok|okay|行|可以|继续|接着"
    r")$",
    re.I,
)


def _is_vague(task):
    """没有对象的短指令。7B 会把它规划成 search+maintain+job_checkup。"""
    t = _norm_turn(task)
    if not t or _has_library_signal(t) or _match_named_skill(task):
        return False
    if _VAGUE_RE.match(t):
        return True
    return False


def _classify_turn(task, thread_context=""):
    """规则路由,在规划器之前。返回 chitchat|offtopic|clarify|agent。

    优先级:问候 > 跑题 > 无对象的裁剪 > 含糊澄清 > 开放任务。
    「继续」且本对话已有内容时交给开放循环,不重复追问。
    """
    if _is_chitchat(task):
        return "chitchat"
    if _is_offtopic(task):
        return "offtopic"
    if _wants_crop(task) and not _crop_material_ids(task):
        return "clarify"
    if _is_vague(task):
        if (thread_context or "").strip() and re.match(
                r"^(继续|接着|continue|goon)$", _norm_turn(task), re.I):
            return "agent"
        return "clarify"
    return "agent"


def _scene_reply(scene, task):
    en = bool(re.match(r"^[A-Za-z]", (task or "").strip()))
    if scene == "offtopic":
        if en:
            return ("I only handle the materials library. "
                    "Ask for missing covers, near-duplicates, a health check, "
                    "a job checkup, a shot timeline, or upload an image or video.")
        return ("我只管素材库。可以说：缺封面、近重复、健康巡检、体检 job:xxxx、"
                "找母版镜头，或上传图片、视频。")
    if scene == "clarify":
        if _wants_crop(task):
            if en:
                return ("Which video should I crop? Give a material id or upload a file, "
                        "and optional start-end seconds (for example 10 to 30). "
                        "You can also ask for the intro, the ending, or the longest shot.")
            return ("要裁哪一条？给出素材 id 或上传视频，并写上起止秒（例如 10到30）。"
                    "也可以只说片头、片尾或主戏。")
        if en:
            return ("Which one should I run: missing covers, near-duplicates, "
                    "a health check, a job checkup, or analyze an upload?")
        return ("要做哪一件：缺封面、近重复、健康巡检、体检某个 job，还是分析上传的视频或图片？")
    if en:
        return ("Hello. I can list missing covers, report near-duplicates, "
                "run a health check, check a job, or analyze an uploaded video or image.")
    return ("你好。可以直接说：缺封面、近重复、健康巡检、体检某个 job，"
            "或上传视频、图片。")


_DUMP_TOOLS = {"search_materials", "maintain", "job_checkup"}
_VACUOUS_RE = re.compile(r"任务已全部完成|无需进一步操作|无需进一步|没有需要处理")


def _plan_is_unasked_dump(todos, task):
    """规划器默认吐出的工具名清单,且用户没有提出这些动作。"""
    texts = [(t.get("text") or "").strip().lower() for t in (todos or [])]
    if len(texts) < 2 or not set(texts) <= _DUMP_TOOLS:
        return False
    if _has_library_signal(task):
        return False
    return True


def _chitchat_reply(task):
    return _scene_reply("chitchat", task)


_CROP_WORD_RE = re.compile(r"裁剪|crop\b|剪辑", re.I)
_CROP_SPAN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:秒|s|sec)?\s*(?:-|~|～|到|至|\bto\b)\s*(\d+(?:\.\d+)?)\s*(?:秒|s|sec)?",
    re.I,
)
_CROP_FOCUS = ("片头", "推进", "主戏", "中段", "片尾", "整片")


def _parse_crop_spans(task):
    """用户写出的起止秒。返回 [(start, end), ...]，未写则空。"""
    spans = []
    for m in _CROP_SPAN_RE.finditer(task or ""):
        a, b = float(m.group(1)), float(m.group(2))
        if b > a >= 0:
            spans.append((a, b))
    return spans[:8]


def _crop_material_ids(task):
    """裁剪对象:上传行里的视频 id,否则正文里的 12 位素材 id。"""
    attached = _attached_video_ids(task)
    if attached:
        return attached[:3]
    seen, out = set(), []
    for mid in _TASK_ID_RE.findall(task or ""):
        if mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
    return out[:3]


def _wants_crop(task):
    """这句是在要求裁剪,而不是顺便提到「适合裁剪」。"""
    t = task or ""
    if not _CROP_WORD_RE.search(t):
        return False
    if (re.search(r"找出|检索|搜索|查找", t)
            and not _crop_material_ids(t) and not _parse_crop_spans(t)):
        return False
    return True


def _user_crop_windows(spans, duration=0):
    """把用户起止收成裁剪窗;超出片长的尾部裁掉。"""
    dur = float(duration or 0)
    out = []
    for i, (a, b) in enumerate(spans):
        end = min(b, dur) if dur else b
        if end <= a:
            continue
        out.append({
            "label": "指定区间" if len(spans) == 1 else "指定区间%d" % (i + 1),
            "start": round(a, 3),
            "end": round(end, 3),
            "duration": round(end - a, 3),
            "scene_index": -2,
        })
    return out


def _focus_crop_windows(crops, task):
    """用户点名片头/片尾/主戏时只留对应窗;没点名则全部保留。"""
    wants = [k for k in _CROP_FOCUS if k in (task or "")]
    if not wants:
        return crops
    picked = [c for c in crops if any(k in (c.get("label") or "") for k in wants)]
    return picked or crops


def _match_named_skill(task):
    """明确技能走确定性工作流;模糊任务(如只说「镜头」)仍走开放循环。

    按钮文案有简体/繁体/英文三套,必须都能命中同一条技能。
    """
    t = task or ""
    # 裁剪意图优先:显式「裁剪/切/crop」且点名素材 id 时,即便含「导出成」也走 crop
    # (crop 技能只产出时间窗,编码导出由 deliver/工作台接管,避免误导向 deliver)。
    if _wants_crop(t) and _crop_material_ids(t):
        return "crop"
    if re.search(r"交付(?!包)|导出|转码|导出成|导出文件|deliver|export", t, re.I):
        return "deliver"
    if re.search(r"素材包|打包|组装|混剪|分发包|交付包|素材集合|package|deliverable", t, re.I):
        return "package"
    if re.search(r"缺封面|无封面|無封面|missing\s+covers?", t, re.I):
        return "covers"
    if re.search(r"近重复|近重複|near-?\s?dup", t, re.I):
        return "dupes"
    if re.search(r"健康巡检|健康巡檢|health\s+check", t, re.I):
        return "maintain"
    if _IMAGE_ATTACH_RE.search(t) or re.search(r"以图搜图|以圖搜圖|相似画面|相似畫面", t):
        return "image"
    if re.search(r"以文搜图|以文搜圖", t):
        return "text_image"
    if "role:master" in t and re.search(r"get_shots|镜头时间轴|鏡頭時間軸|segment timeline", t, re.I):
        return "segment"
    if re.search(r"体检|體檢", t) and _JOB_ATTACH_RE.search(t):
        return "job"
    return ""


def _named_skill_todos(skill):
    labels = {
        "covers": ["列出无封面(silent 优先)", "汇总"],
        "dupes": ["全库近重复报告", "汇总"],
        "maintain": ["确定性巡检", "汇总"],
        "image": ["以图搜图", "汇总"],
        "text_image": ["以文搜图", "汇总"],
        "segment": ["检索母版", "读取镜头时间轴", "汇总"],
        "job": ["job 体检", "汇总"],
        "crop": ["读取母版", "反查子组件", "读取镜头时间轴", "给出裁剪窗"],
        "package": ["拆解目标为检索查询", "Librarian 检索候选素材",
                    "Critic 核验候选", "Executor 组装素材包", "汇总"],
        "deliver": ["解析目标素材/素材包", "按需导出交付变体(转码/裁剪)",
                    "汇总交付清单"],
    }
    return [{"id": i, "text": text, "status": "pending"}
            for i, text in enumerate(labels.get(skill) or ["执行", "汇总"], 1)]


def _skill_call(state, ws, ctx, n, action, args, obs, skill, todo_id):
    """技能工作流的一步:标进度、收 id、卸载、落盘。"""
    todos = state.get("todos") or []
    label = action
    if todo_id and 1 <= todo_id <= len(todos):
        label = todos[todo_id - 1].get("text") or action
        _set_progress(state, ws, todo_id=todo_id, status_for="running", label=label)
    for _i in _collect_ids(obs):
        ctx["seen_ids"].add(_i)
    if isinstance(obs, dict):
        for m in (obs.get("matches") or obs.get("materials") or []):
            if isinstance(m, dict) and m.get("id"):
                ctx["seen_ids"].add(str(m["id"]))
        if obs.get("id"):
            ctx["last_id"] = obs["id"]
    ptr = _observe(state, ws, obs)
    state["steps"].append({
        "n": n, "action": action,
        "ok": not (isinstance(obs, dict) and obs.get("error")),
        "args": args, "obs": ptr,
        "_sig": _tool_sig(action, args),
        "skill": skill,
    })
    if todo_id:
        _set_progress(state, ws, todo_id=todo_id, status_for="done", label=label)
    else:
        _save(state, ws)
    return n + 1


def _fmt_rows(rows, limit=8):
    lines = []
    for row in (rows or [])[:limit]:
        if not isinstance(row, dict):
            continue
        lines.append("- %s · %s · %s" % (
            row.get("id") or "?", row.get("kind") or row.get("mode") or "",
            row.get("name") or ""))
    return lines


def _run_named_skill(state, ws, tools, ctx, skill):
    """Skill A–E / 巡检:固定工具序列,不把这一步交给 7B 选工具。

    返回中文总结;条件不足(例如以图搜图没有 id)时返回 None,交给开放循环。
    """
    task = state.get("task") or ""
    n = 1

    def call(action, args, todo_id):
        nonlocal n
        fn = tools.get(action)
        if not fn:
            obs = {"error": "tool missing: %s" % action}
        else:
            try:
                obs = fn[0](args)
            except Exception as e:                          # noqa: BLE001
                obs = {"error": "%s: %s" % (type(e).__name__, e)}
        n = _skill_call(state, ws, ctx, n, action, args, obs, skill, todo_id)
        return obs

    if skill == "covers":
        silent = call("list_missing_covers", {"kind": "silent", "limit": 20}, 1)
        videos = call("list_missing_covers", {"kind": "videos", "limit": 20}, 1)
        srows = silent if isinstance(silent, list) else []
        vrows = videos if isinstance(videos, list) else []
        lines = ["[Skill A 缺封面] silent %d · videos %d" % (len(srows), len(vrows))]
        lines += _fmt_rows(srows + vrows)
        if not srows and not vrows:
            lines.append("当前没有缺封面的 silent/videos。")
        else:
            lines.append("补封面需显式确认后再 auto_process,本次只盘点。")
        return "\n".join(lines)

    if skill == "dupes":
        obs = call("near_duplicate_report", {"max_dist": 10, "limit": 20}, 1)
        obs = obs if isinstance(obs, dict) else {}
        lines = ["[Skill C 近重复] 簇 %s · 对 %s · 已哈希 %s" % (
            obs.get("cluster_count", 0), obs.get("pair_count", 0), obs.get("hashed", 0))]
        for c in (obs.get("clusters") or [])[:5]:
            if isinstance(c, dict):
                lines.append("- 簇 %s (%s)" % (", ".join(c.get("ids") or []), c.get("size")))
                for i in c.get("ids") or []:
                    ctx["seen_ids"].add(str(i))
        for p in (obs.get("pairs") or [])[:5]:
            if isinstance(p, dict):
                lines.append("- 对 %s / %s 距离 %s" % (p.get("a"), p.get("b"), p.get("dist")))
        lines.append("只汇报,不删除。精确重复仍走 sha256 dupes。")
        return "\n".join(lines)

    if skill == "maintain":
        obs = _sub_maintain()
        n = _skill_call(state, ws, ctx, n, "maintain", {}, obs, skill, 1)
        lines = ["[巡检] 总量 %s · 健康 %s · 失效引用 %s · 缺封面 %s · 近重复簇 %s" % (
            obs.get("total"), obs.get("health_ok"), obs.get("broken"),
            obs.get("missing_covers"), obs.get("near_dupe_clusters"))]
        if obs.get("broken_ids"):
            lines.append("失效: " + ", ".join(obs["broken_ids"][:8]))
        if obs.get("missing_silent_covers"):
            lines.append("silent 缺封面: " + ", ".join(obs["missing_silent_covers"][:8]))
        return "\n".join(lines)

    if skill == "image":
        ids = [m.group(1) for m in _IMAGE_ATTACH_RE.finditer(task)]
        if not ids:
            return None
        lines = ["[Skill D 以图搜图]"]
        for mid in ids[:3]:
            ctx["seen_ids"].add(mid)
            obs = call("search_by_image", {"query": mid, "mode": "auto", "limit": 8}, 1)
            obs = obs if isinstance(obs, dict) else {}
            lines.append("查询 %s · mode %s · %s" % (
                mid, obs.get("mode") or obs.get("status"), obs.get("status") or "ok"))
            matches = obs.get("matches") or []
            if not matches:
                lines.append(obs.get("hint") or obs.get("error") or "无相似画面")
            else:
                lines += _fmt_rows(matches)
        return "\n".join(lines)

    if skill == "text_image":
        qm = re.search(r"用户请求：(.+)", task)
        q = (qm.group(1).strip() if qm else task.strip())[:80]
        obs = call("search_by_text_image", {"q": q, "limit": 8}, 1)
        obs = obs if isinstance(obs, dict) else {}
        lines = ["[Skill D 以文搜图] q=%s · %s" % (q, obs.get("status") or "ok")]
        lines += _fmt_rows(obs.get("matches") or [])
        if not obs.get("matches"):
            lines.append(obs.get("hint") or obs.get("error") or "无匹配")
        return "\n".join(lines)

    if skill == "segment":
        found = call("search_materials",
                     {"q": "", "tag": "role:master", "kind": "videos", "limit": 5}, 1)
        rows = found if isinstance(found, list) else (
            (found.get("results") or []) if isinstance(found, dict) else [])
        mid = ""
        for row in rows:
            if isinstance(row, dict) and row.get("id"):
                mid = row["id"]
                break
        lines = ["[Skill E 找片段] 母版 %d 条" % len(rows)]
        lines += _fmt_rows(rows, 5)
        if mid and "get_shots" in tools:
            shots = call("get_shots", {"id": mid}, 2)
            shots = shots if isinstance(shots, dict) else {}
            scenes = shots.get("scenes") or []
            lines.append("母版 %s 镜头 %s 时长 %s" % (
                mid, shots.get("scene_count", len(scenes)), shots.get("duration")))
            for sc in scenes[:6]:
                if isinstance(sc, dict):
                    lines.append("- %.2f–%.2fs" % (float(sc.get("start") or 0),
                                                   float(sc.get("end") or 0)))
            lines.append("以上是逻辑时间轴,不是物理 clip。")
        elif not mid:
            lines.append("没有 role:master 母版。")
        return "\n".join(lines)

    if skill == "job":
        m = _JOB_ATTACH_RE.search(task)
        if not m:
            return None
        jid = m.group(1)
        obs = call("job_checkup", {"job_id": jid}, 1)
        obs = obs if isinstance(obs, dict) else {}
        if obs.get("error"):
            return "[Skill B 体检] %s" % obs["error"]
        lines = ["[Skill B job 体检] %s · %s 条 · ok=%s" % (
            obs.get("job") or jid, obs.get("count"), obs.get("ok"))]
        lines.append("kinds: %s" % (obs.get("kinds") or {}))
        for key, label in (
            ("master_ids", "母版"), ("component_ids", "组件"), ("clip_ids", "clip"),
            ("missing_thumbs", "缺封面"), ("missing_shots", "缺镜头"),
            ("broken_ids", "失效"), ("asr_ids", "ASR"),
        ):
            vals = obs.get(key) or []
            if vals:
                lines.append("%s: %s" % (label, ", ".join(str(x) for x in vals[:8])))
                for x in vals:
                    if isinstance(x, str):
                        ctx["seen_ids"].add(x)
        return "\n".join(lines)

    if skill == "crop":
        return _run_crop_skill(state, ws, ctx, call)

    if skill == "package":
        return _run_package_skill(state, ws, ctx, call)

    if skill == "deliver":
        return _run_deliver_skill(state, ws, ctx, call)

    return None


def _run_crop_skill(state, ws, ctx, call):
    """任意裁剪请求:指定起止、点名段落,或按镜头轴出五段窗。

    只产出逻辑时间窗。用户明确要导出文件时,说明交给工作台切 range_*.mp4,
    这一步不编码。
    """
    task = state.get("task") or ""
    spans = _parse_crop_spans(task)
    export = bool(re.search(r"导出|切成文件|切成\s*mp4|导出成", task))
    parts = []
    for mid in _crop_material_ids(task)[:3]:
        ctx["last_id"] = mid
        ctx["seen_ids"].add(mid)
        mat = call("get_material", {"id": mid}, 1)
        related = call("related", {"id": mid, "rel": "children", "limit": 20}, 2)
        shots = call("get_shots", {"id": mid}, 3)
        mat = mat if isinstance(mat, dict) else {}
        related = related if isinstance(related, dict) else {}
        shots = shots if isinstance(shots, dict) else {}
        duration = (shots or {}).get("duration") or 0
        scenes = (shots or {}).get("scenes") or []
        if spans:
            crops = _user_crop_windows(spans, duration)
            mode = "按你给的起止"
        else:
            crops = _focus_crop_windows(
                _crop_windows(scenes, duration), task)
            mode = "按镜头时间轴"
        _remember_crop_windows(state, mid, crops)
        plan = _format_crop_plan(mid, mat, shots, related, crops)
        if export:
            plan += ("\n物理文件不在这一步生成。把上面的起止交给工作台逐条裁剪,"
                     "才会写出 range_*.mp4 并登记 role:clip。")
        n = len(state.get("steps") or []) + 1
        ptr = _observe(state, ws, {"id": mid, "crop_windows": crops, "mode": mode})
        state["steps"].append({
            "n": n, "action": "crop_plan", "ok": bool(crops),
            "args": {"id": mid, "mode": mode}, "obs": ptr,
            "_sig": _tool_sig("crop_plan", {"id": mid, "mode": mode}),
            "skill": "crop",
        })
        todos = state.get("todos") or []
        if len(todos) >= 4:
            _set_progress(state, ws, todo_id=4, status_for="done",
                          label=todos[3].get("text") or "给出裁剪窗")
        else:
            _save(state, ws)
        parts.append("[%s]\n%s" % (mode, plan))
    if not parts:
        return None
    return "\n\n".join(parts)


def _close_named_skill(state, ws, task_id, ctx, summary, skill):
    state["summary"] = summary[:2000]
    state["result_ids"] = list(ctx["seen_ids"])[:8]
    for t in state["todos"]:
        t["status"] = "done"
    state["steps"].append({
        "n": len(state["steps"]) + 1, "action": "finish", "ok": True,
        "args": {"summary": summary[:500]}, "skill": skill,
    })
    state["status"] = "done"
    state["skill"] = skill
    state["progress"] = {
        "done": len(state["todos"]), "total": len(state["todos"] or [1]),
        "pct": 100, "label": "已完成",
    }
    _record_memory(state, ctx)
    _save(state, ws)
    return {"status": "done", "task_id": task_id, "workspace": ws,
            "summary": state["summary"], "todos": state["todos"],
            "steps": len(state["steps"]), "notes": len(state["notes"]),
            "result_ids": state.get("result_ids", []),
            "dropped_ids": [], "citations": [], "critic": None,
            "context_digest": summary[:800], "skill": skill,
            "progress": state["progress"]}


def agent_run(task, allow_write=False, max_steps=_DEFAULT_STEPS, model=None, task_id=None,
              thread_context=""):
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
             "summary": "", "context_digest": "",
             "progress": {"done": 0, "total": 1, "pct": 0, "label": "规划中…"}}
    _save(state, ws)

    if not _attached_video_ids(task) and not _match_named_skill(task):
        scene = _classify_turn(task, thread_context)
        if scene in ("chitchat", "offtopic", "clarify"):
            reply = _scene_reply(scene, task)
            state.update(
                status="done", skill=scene, summary=reply, todos=[],
                progress={"done": 0, "total": 0, "pct": 0, "label": ""},
            )
            _save(state, ws)
            return {"status": "done", "task_id": task_id, "workspace": ws,
                    "summary": reply, "todos": [], "steps": 0, "notes": 0,
                    "result_ids": [], "dropped_ids": [], "citations": [],
                    "critic": None, "context_digest": "", "skill": scene,
                    "progress": state["progress"]}

    # 上传视频+裁剪/分析意图:跳过易跑偏的 LLM 规划,直接用工作流待办(进度立刻可见)
    mids_early = _attached_video_ids(task)
    force_skill = bool(mids_early) and (
        bool(_VIDEO_SKILL_HINT.search(task)) or ("用户刚上传" in task))
    named = "" if force_skill else _match_named_skill(task)
    if force_skill:
        state["todos"] = _skill_todo_template(mids_early[0])
        state["skill"] = "attached_video"
        _set_progress(state, ws, label="方案 A 工作流准备中…")
    elif named:
        state["todos"] = _named_skill_todos(named)
        state["skill"] = named
        _set_progress(state, ws, label="技能 %s 准备中…" % named)
    else:
        try:
            state["todos"] = _plan(task, model)
        except Exception as e:                                  # noqa: BLE001
            state.update(status="error", summary="plan failed: %s" % e)
            _save(state, ws)
            return {"status": "error", "task_id": task_id, "summary": state["summary"]}
        state["progress"] = _progress_from_state(state)
        if _plan_is_unasked_dump(state["todos"], task):
            reply = _scene_reply("clarify", task)
            state.update(
                status="done", skill="clarify", summary=reply, todos=[],
                progress={"done": 0, "total": 0, "pct": 0, "label": ""},
            )
            _save(state, ws)
            return {"status": "done", "task_id": task_id, "workspace": ws,
                    "summary": reply, "todos": [], "steps": 0, "notes": 0,
                    "result_ids": [], "dropped_ids": [], "citations": [],
                    "critic": None, "context_digest": "", "skill": "clarify",
                    "progress": state["progress"]}
        _save(state, ws)

    state["status"] = "running"
    _save(state, ws)          # 计划完成即落盘:任务从一开始就可被 agent_status 轮询/agent_cancel 取消
    ctx = {"last_id": None, "seen_ids": set()}            # 最近检索到的有效素材 id(供写工具兜底) + 全程见过 id(供 finish 核验)
    _seed_ids_from_task(task, ctx)
    tools = _build_tools(bool(allow_write), ctx)

    # 上传视频:优先确定性方案 A 工作流(Anthropic/aiarch:固定序列勿交给开放 Agent)
    skill_sum, next_n, skill_ctx, skill_digest = _skill_attached_videos(
        state, ws, tools, ctx, start_n=1)
    if skill_sum:
        _set_progress(state, ws, todo_id=5, status_for="running", label="汇总方案并完成")
        state["summary"] = skill_sum[:2000]
        state["result_ids"] = [ctx["last_id"]] if ctx.get("last_id") else list(ctx["seen_ids"])[:5]
        for t in state["todos"]:
            t["status"] = "done"
        state["steps"].append({
            "n": next_n, "action": "finish", "ok": True,
            "args": {"summary": skill_sum[:500]},
            "skill": "attached_video",
        })
        state["status"] = "done"
        state["skill"] = "attached_video"
        state["context_digest"] = skill_digest
        state["progress"] = {
            "done": len(state["todos"]), "total": len(state["todos"] or [1]),
            "pct": 100, "label": "已完成",
        }
        _record_memory(state, ctx)
        _save(state, ws)
        return {"status": "done", "task_id": task_id, "workspace": ws,
                "summary": state["summary"], "todos": state["todos"],
                "steps": len(state["steps"]), "notes": len(state["notes"]),
                "result_ids": state.get("result_ids", []),
                "dropped_ids": [], "citations": [], "critic": None,
                "context_digest": skill_digest, "skill": "attached_video",
                "progress": state["progress"]}

    if named:
        named_sum = _run_named_skill(state, ws, tools, ctx, named)
        if named_sum:
            return _close_named_skill(state, ws, task_id, ctx, named_sum, named)

    return _loop(state, ws, task_id, tools, model, int(max_steps),
                 next_n if skill_ctx else 1,
                 skill_ctx, skill_digest, ctx,
                 thread_context=thread_context)


def _skill_step_hint(task, done):
    """Skills 中间件:按任务意图告诉模型「这一步只该调哪个工具」(7B 易跳步)。"""
    t = task or ""
    done = done or set()
    cards = []

    def nxt(action, label):
        step = action if action not in done else "finish"
        cards.append("Skill %s: 本步只调用 %s(已完成: %s)"
                     % (label, step, ",".join(sorted(done)[:6]) or "无"))

    if re.search(r"缺封面|无封面|無封面|missing\s+covers?", t, re.I):
        nxt("list_missing_covers", "A缺封面")
    elif re.search(r"近重复|近重複|near-?\s?dup", t, re.I):
        nxt("near_duplicate_report", "C近重复")
    elif re.search(r"job_checkup|体检\s*job|體檢\s*job|job[:：\s]", t, re.I):
        nxt("job_checkup", "B体检")
    elif re.search(r"以图搜图|以圖搜圖|相似画面|相似畫面|search_by_image", t, re.I):
        nxt("search_by_image", "D以图搜图")
    elif re.search(r"以文搜图|以文搜圖|search_by_text_image", t, re.I):
        nxt("search_by_text_image", "D以文搜图")
    elif re.search(r"健康巡检|健康巡檢|health\s+check|maintain|失效引用", t, re.I):
        nxt("maintain", "巡检")
    elif re.search(r"镜头|时间轴|role:master|找片段", t, re.I):
        if "search_materials" not in done and "get_material" not in done:
            nxt("search_materials", "E找片段")
        elif "get_shots" not in done:
            nxt("get_shots", "E找片段")
        else:
            nxt("finish", "E找片段")
    return ("\n" + "\n".join(cards)) if cards else ""


def _patch_tool_call(act, tools):
    """PatchToolCalls:把 7B 常见的别名/嵌套工具调用收成 {action,args}。"""
    if not isinstance(act, dict):
        return {}
    act = dict(act)
    if not act.get("action"):
        for k in ("tool", "name", "function", "tool_name"):
            if act.get(k):
                act["action"] = act[k]
                break
    tc = act.get("tool_call")
    if not act.get("action") and isinstance(tc, dict):
        act["action"] = tc.get("name") or tc.get("action") or ""
        if not act.get("args"):
            act["args"] = tc.get("args") or tc.get("arguments") or {}
    tcs = act.get("tool_calls")
    if not act.get("action") and isinstance(tcs, list) and tcs:
        first = tcs[0] if isinstance(tcs[0], dict) else {}
        fn = first.get("function") if isinstance(first.get("function"), dict) else first
        fn = fn or {}
        act["action"] = fn.get("name") or fn.get("action") or ""
        if not act.get("args"):
            act["args"] = fn.get("arguments") or fn.get("args") or {}
    if isinstance(act.get("args"), str):
        act["args"] = core._safe_json(act["args"]) or {}
    if not act.get("args") and act.get("arguments") is not None:
        raw = act.get("arguments")
        act["args"] = core._safe_json(raw) if isinstance(raw, str) else (
            raw if isinstance(raw, dict) else {})
    if not isinstance(act.get("args"), dict):
        act["args"] = {}
    action = str(act.get("action") or "").strip()
    aliases = {
        "write_todo": "write_todos",
        "todowrite": "write_todos",
        "todo_write": "write_todos",
    }
    action = aliases.get(action.lower().replace("-", "_"), action)
    act["action"] = action
    return act


def _apply_write_todos(state, args):
    """TodoList 工具:运行中重写待办(Deep Agent write_todos),不是只在规划时写一次。"""
    raw = args.get("todos") or args.get("items") or []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return {"error": "todos required", "hint": 'args.todos=[{"id":1,"text":"...","status":"pending"}]'}
    todos = []
    for i, t in enumerate(raw, 1):
        if isinstance(t, str) and t.strip():
            todos.append({"id": i, "text": t.strip()[:120], "status": "pending"})
        elif isinstance(t, dict) and (t.get("text") or t.get("content")):
            st = str(t.get("status") or "pending")
            if st not in ("pending", "running", "in_progress", "done"):
                st = "pending"
            todos.append({
                "id": t.get("id") or i,
                "text": str(t.get("text") or t.get("content"))[:120],
                "status": st,
            })
    if not todos:
        return {"error": "empty todos"}
    # Deep Agent: 同时只能有一条 in_progress
    seen_run = False
    for t in todos:
        if t["status"] in ("running", "in_progress"):
            if seen_run:
                t["status"] = "pending"
            else:
                t["status"] = "in_progress"
                seen_run = True
    state["todos"] = todos
    state["progress"] = _progress_from_state(state)
    return {"ok": True, "count": len(todos), "todos": todos}


def _arm_running_todo(state, ws):
    """每步开始前把第一条未完成待办标为 running,进度条文案跟人读步骤对齐。"""
    todos = state.get("todos") or []
    if any((t.get("status") or "") in ("running", "in_progress") for t in todos):
        state["progress"] = _progress_from_state(state)
        _save(state, ws)
        return
    for t in todos:
        if (t.get("status") or "") != "done":
            t["status"] = "running"
            state["progress"] = _progress_from_state(state)
            if state.get("progress"):
                state["progress"]["label"] = t.get("text") or "执行中"
            _save(state, ws)
            return


def _complete_running_todo(state):
    """工具成功且模型没写 mark_done 时,收掉当前 running 待办(避免待办永远 pending)。"""
    for t in state.get("todos") or []:
        if (t.get("status") or "") in ("running", "in_progress"):
            t["status"] = "done"
            return


def _list_notes(state):
    notes = state.get("notes") or []
    return {"notes": notes[-20:],
            "hint": "细节已卸载。read_note 的 file 用 notes/step_000.json"}


def _read_note(ws, args):
    """Filesystem 读回卸载笔记;只允许 notes/step_NNN.json。"""
    rel = str(args.get("file") or args.get("path") or "").replace("\\", "/").lstrip("/")
    if rel.startswith("notes/"):
        rel = rel[len("notes/"):]
    if not re.match(r"^step_\d{3}\.json$", rel):
        return {"error": "only notes/step_NNN.json", "file": rel}
    p = os.path.join(ws, "notes", rel)
    if not os.path.isfile(p):
        return {"error": "not found", "file": "notes/" + rel}
    with open(p, "r", encoding="utf-8") as f:
        raw = f.read()
    return {"file": "notes/" + rel, "chars": len(raw),
            "content": raw[:900], "truncated": len(raw) > 900}


def _loop(state, ws, task_id, tools, model, budget, start_step, context, digest, ctx,
          thread_context=""):
    """Deep Agent 主循环(agent_run 全新任务 / agent_resume 续跑共用)。

    每一步按 harness 顺序走:arm todo → skill 提示 → 模型 → patch 工具调用
    → write_todos / task 子代理 / 读笔记 / 业务工具 → 卸载 → 滚动摘要 → 落盘。
    budget:本次可执行步数;start_step:步号起点;thread_context:同一对话的既往结论。
    """
    task = state["task"]
    tool_lines = "".join("  - %s: %s\n" % (n, d) for n, (_, d) in sorted(tools.items()))
    tool_lines += "  - write_todos: 重写待办列表;args: {todos:[{id,text,status}]}\n"
    tool_lines += "  - task: 派生子代理(上下文隔离,只回摘要);args: {name:retrieve|maintain, goal}\n"
    tool_lines += "  - retrieve: 子代理:多查询检索+LLM汇总,只回摘要;args: {goal}\n"
    tool_lines += "  - maintain: 确定性巡检(health+失效引用),零幻觉;args: 无\n"
    tool_lines += "  - list_notes: 列出已卸载的观察文件;args: 无\n"
    tool_lines += "  - read_note: 读回某条卸载笔记;args: {file:notes/step_000.json}\n"
    tool_lines += "  - finish: 任务完成,输出总结\n"
    sys_prompt = _LOOP_SYS + tool_lines
    mem_hint = _memory_hint()
    if mem_hint:
        sys_prompt += mem_hint

    finished = False
    fails = 0                                             # 连续无效 LLM 响应计数
    stall_nudge = ""                                      # 重复调用后注入下一轮的强制纠正
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
        _arm_running_todo(state, ws)
        recent = "\n".join(context[-_CONTEXT_TAIL:]) or "(无)"
        digest_note = ("\n(前情压缩摘要,早期步骤已归档,无需重做)\n%s" % digest) if digest else ""
        prior_note = ("\n(本对话已有记录,延续结论,不要重做)\n%s" % thread_context) if thread_context else ""
        skill_hint = _skill_step_hint(task, _actions_done(state))
        files = [n.get("file") for n in (state.get("notes") or [])[-8:] if isinstance(n, dict) and n.get("file")]
        note_ix = ("\n(Filesystem 已卸载: %s。要细节就 read_note,不要重跑工具)"
                   % ", ".join(files)) if files else ""
        user = ("任务:%s\n待办:%s\n最近观察:\n%s%s%s%s%s%s%s"
                % (task, json.dumps(state["todos"], ensure_ascii=False),
                   recent, digest_note, prior_note, note_ix, skill_hint, stall_nudge, budget_hint))
        stall_nudge = ""
        act = _chat([{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user}], model)
        act = _patch_tool_call(act, tools)
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
        state["progress"] = _progress_from_state(state)
        sig = _tool_sig(action, args)
        prior_ok = [
            s for s in state["steps"]
            if isinstance(s, dict) and s.get("ok") and s.get("_sig") == sig
            and not s.get("repeat")
        ]
        n_soft = sum(
            1 for s in state["steps"]
            if isinstance(s, dict) and s.get("_sig") == sig and s.get("repeat")
        )
        # 同参已成功 → 软提示;再犯且可恢复 → LoopGuard 硬停转(deer-flow hard_limit)
        if action not in ("finish", "retrieve", "llm_retry", "") and len(prior_ok) >= 1:
            can_hard = bool(ctx.get("last_id")) or action in (
                "get_material", "search_materials", "get_shots", "related",
                "search_by_image", "search_by_text_image", "read_text_preview",
            )
            if n_soft >= _STALL_SOFT and can_hard:
                recovered, summary = _stall_recover(state, tools, ctx)
                for name, obs_r in recovered.items():
                    ptr_r = _observe(state, ws, obs_r)
                    context.append("step%d[%s] %s" % (n, name, ptr_r[:400]))
                    state["steps"].append({
                        "n": n, "action": name,
                        "ok": isinstance(obs_r, dict) and "error" not in obs_r,
                        "args": {"id": ctx.get("last_id")}, "obs": ptr_r,
                        "_sig": "stall|%s" % name, "stall_recover": True,
                    })
                    n += 1
                state["summary"] = summary[:2000]
                state["result_ids"] = [ctx["last_id"]] if ctx.get("last_id") else []
                for t in state["todos"]:
                    t["status"] = "done"
                state["steps"].append({
                    "n": n, "action": "finish", "ok": True,
                    "args": {"summary": summary},
                    "stall_break": True,
                })
                finished = True
                break
            next_tools = [x for x in ("get_shots", "related", "finish") if x != action]
            obs = {
                "status": "repeat",
                "hint": "该工具与参数已成功执行过,不要重复;请做下一步或 finish",
                "action": action,
                "next": next_tools,
                "forbidden": action,
            }
            ptr = _observe(state, ws, obs)
            context.append("step%d[%s] %s" % (n, action, ptr[:400]))
            if len(context) > _CONTEXT_TAIL:
                evicted = context[:-_CONTEXT_TAIL]
                context = context[-_CONTEXT_TAIL:]
                lines = (digest.splitlines() if digest else []) + [l[:200] for l in evicted]
                digest = "\n".join(lines[-_DIGEST_MAX:])
            state["context_digest"] = digest
            state["steps"].append({
                "n": n, "action": action, "ok": True, "args": args,
                "obs": ptr, "_sig": sig, "repeat": True,
            })
            stall_nudge = (
                "\n(LoopGuard: %s 已成功,严禁再调用。下一步: %s 或 finish)"
                % (action, " / ".join(next_tools))
            )
            _save(state, ws)
            continue

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
            ran = [s for s in state["steps"]
                   if s.get("ok") and s.get("action") not in
                   ("finish", "llm_retry", "write_todos", "list_notes", "read_note")]
            if _VACUOUS_RE.search(summary) and not ran and not ctx.get("seen_ids"):
                state["summary"] = _scene_reply("clarify", task)
                state["skill"] = "clarify"
            # 只勾掉本步 mark_done 或已经做过的待办。未执行的保持未完成。
            # 避免「你好」被收成 search/maintain/job_checkup 三条全勾。
            finished = True
            state["steps"].append({"n": n, "action": "finish", "ok": True})
            break

        try:
            if action == "write_todos":
                obs = _apply_write_todos(state, args)
            elif action == "list_notes":
                obs = _list_notes(state)
            elif action == "read_note":
                obs = _read_note(ws, args)
            elif action == "task":
                sub = str(args.get("name") or args.get("subagent") or "retrieve").strip()
                goal = str(args.get("goal") or args.get("task") or task)
                if sub == "maintain":
                    obs = _sub_maintain()
                else:
                    obs = _sub_retrieve(goal, model, " ".join(context[-2:]))
                if isinstance(obs, dict):
                    obs["subagent"] = sub
            elif action == "retrieve":
                obs = _sub_retrieve(str(args.get("goal") or task), model,
                                    " ".join(context[-2:]))
            elif action == "maintain":
                obs = _sub_maintain()
            elif action in tools:
                obs = tools[action][0](args)
            else:
                obs = {"error": "unknown action: %s" % action,
                       "allowed": sorted(tools) + [
                           "retrieve", "maintain", "finish", "write_todos",
                           "task", "list_notes", "read_note"]}
        except Exception as e:                              # noqa: BLE001
            obs = {"error": "%s: %s" % (type(e).__name__, e)}

        # 记录最近检索到的有效素材 id,供写工具 id 兜底(7B 常不复制真实 id,改用 BV号/XXX)
        if action == "search_materials":
            items = obs if isinstance(obs, list) else (
                obs.get("results") or [] if isinstance(obs, dict) else [])
            if items and isinstance(items[0], dict) and items[0].get("id"):
                ctx["last_id"] = items[0]["id"]        # 首条结果 id 作兜底基准
        elif action in ("get_material", "get_shots", "related") and isinstance(obs, dict) and obs.get("id"):
            ctx["last_id"] = obs["id"]
        elif action == "retrieve" and isinstance(obs, dict):
            ids = obs.get("ids") or []
            if ids and isinstance(ids[0], str):
                ctx["last_id"] = ids[0]

        # 累计全程见到过的素材 id(供 finish 时核验,杜绝编造 id)
        for _i in _collect_ids(obs):
            ctx["seen_ids"].add(_i)

        ok_obs = not (isinstance(obs, dict) and "error" in obs)
        if ok_obs and action not in ("write_todos", "list_notes", "read_note") and not (act.get("mark_done") or []):
            _complete_running_todo(state)

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
                               "args": args, "obs": ptr, "_sig": sig})
        state["progress"] = _progress_from_state(state)
        # 进行中提示:当前动作名
        if state["progress"] and action:
            state["progress"]["label"] = "%s · %s" % (
                state["progress"].get("label") or "执行中", action)
        _save(state, ws)                                  # 每步落盘,供 agent_status 实时轮询进度

    if not finished:
        state["status"] = "max_steps_reached"
        # 截断时 summary 带上最近几步的实质进展(而非干巴巴一句),agent_status 一眼可见
        tail = " | ".join(
            (s.get("action") or "") for s in state["steps"][-3:]
            if isinstance(s, dict))
        state["summary"] = ("步数预算(至第 %d 步)已用尽,未 finish;最近进展: %s;可 agent_resume 续跑"
                            % (start_step + budget - 1, tail))[:2000]
        state["progress"] = _progress_from_state(state)
    else:
        state["status"] = "done"
        if state.get("skill") in ("clarify", "chitchat", "offtopic"):
            state["todos"] = []
            state["progress"] = {"done": 0, "total": 0, "pct": 0, "label": ""}
        else:
            for t in state.get("todos") or []:
                if t.get("status") in ("running", "in_progress"):
                    t["status"] = "pending"
            state["progress"] = _progress_from_state(state)
    _record_memory(state, ctx)                            # 成功任务沉淀跨任务规律
    _save(state, ws)
    return {"status": state["status"], "task_id": task_id, "workspace": ws,
            "summary": state["summary"], "todos": state["todos"],
            "steps": len(state["steps"]), "notes": len(state["notes"]),
            "result_ids": state.get("result_ids", []),
            "dropped_ids": state.get("dropped_ids", []),
            "citations": state.get("citations", []),
            "critic": state.get("critic"),
            "context_digest": state.get("context_digest", ""),
            "skill": state.get("skill") or "",
            "progress": state.get("progress")}


if __name__ == "__main__":                                  # 直接调试: python agent.py "任务..."
    core.init_hub()
    r = agent_run(" ".join(os.sys.argv[1:]) or "巡检素材库健康状态")
    print(json.dumps(r, ensure_ascii=False, indent=2))
