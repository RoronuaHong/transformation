"""Materials Hub — MCP stdio server(零依赖,Python 标准库)。

让 AI 助手(Claude Desktop / CodeBuddy 等)通过 MCP 直接检索素材库。
注册示例(claude_desktop_config.json / CodeBuddy MCP 配置):
  {"mcpServers": {"materials-hub": {
      "command": "python",
      "args": ["d:/MineWeb/2026/Vitual/materials_hub/mcp_server.py", "--token", "<TOKEN>"],
      "env": {"VITUAL_HUB_TOKEN": "<TOKEN>"}}}}
  # 未设 VITUAL_HUB_TOKEN 时不需要 --token(本机开放);一旦设了 token,MCP 客户端必须传一致的 --token 才能启动。

协议:MCP stdio 传输 = 按行分隔的 JSON-RPC 2.0(每行一条,行内不得有换行)。
stdout 只走协议;日志一律 stderr。语义后端(ollama)没起会顺手自动拉起。
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

# 鉴权(与 HTTP server 共用 VITUAL_HUB_TOKEN):若环境变量设了 token,MCP 客户端必须在启动参数里
# 传一致的 --token,否则拒绝启动 —— 防止本机任何进程都能无鉴权调起素材中心 Agent 接口。
HUB_TOKEN = os.environ.get("VITUAL_HUB_TOKEN", "").strip()
_mcp_tok = None
for _i, _a in enumerate(sys.argv):
    if _a == "--token" and _i + 1 < len(sys.argv):
        _mcp_tok = sys.argv[_i + 1]
if HUB_TOKEN and _mcp_tok is None:
    sys.stderr.write("[materials-hub] VITUAL_HUB_TOKEN is set but MCP client did not pass "
                     "--token; refusing to start (add \"--token\" \"<same>\" to mcp args).\n")
    sys.exit(2)
if HUB_TOKEN and _mcp_tok != HUB_TOKEN:
    sys.stderr.write("[materials-hub] MCP --token mismatch; refusing to start.\n")
    sys.exit(2)


def _brief(m):
    """工具返回的精简条目(不带巨型字段,省 token)。"""
    out = {k: m.get(k, "") for k in
           ("id", "kind", "ext", "name", "size", "tags", "ai_tags",
            "description", "location")}
    if m.get("location") == "external":
        out["external_path"] = m.get("external_path", "")
    return out


def t_search(a):
    rows = core.search(a.get("q", ""), a.get("kind", ""), a.get("tag", ""),
                       limit=int(a.get("limit") or 10),
                       mode=a.get("mode", "auto"))
    return [_brief(m) for m in rows]


def t_get(a):
    m = core.get_material(a.get("id", ""))
    if not m:
        raise ValueError("not found: %s" % a.get("id", ""))
    return m


def t_tags(a):
    n = int(a.get("limit") or 50)
    return [{"tag": t, "count": c} for t, c in core.distinct_tags()[:n]]


def t_stats(a):
    return core.health()


# ---------- MCP Resources(只读侧的素材视图,2026 最佳实践:读用 Resources / 写用 Tools) ----------
def _resources_list():
    """把「最近登记」「某 job 全套」这类只读、订阅式数据暴露为 Resource,
    Agent 可像读文件一样按需拉取,比反复调 search 更省 token。"""
    return [
        {"uri": "hub://recent", "name": "最近登记的素材",
         "description": "按时间倒序的最新素材(默认 10 条);hub://recent/{n} 可指定条数",
         "mimeType": "application/json"},
        {"uri": "hub://job", "name": "某 subtitle_pipeline job 全套素材",
         "description": "hub://job/{jobid} 返回该 job 通过 bridge 登记的全部素材",
         "mimeType": "application/json"},
        {"uri": "hub://history", "name": "写操作审计日志",
         "description": "按时间倒序的标签/描述/删除/打标/OCR 变更记录;hub://history/{n} 指定条数",
         "mimeType": "application/json"},
        {"uri": "hub://shots", "name": "视频镜头索引(片段时间轴)",
         "description": "hub://shots/{id} 返回 ffmpeg scenecut 镜头表[{start,end},...];未建索引则 {status:missing}",
         "mimeType": "application/json"},
        {"uri": "hub://agent", "name": "Agent 任务实时状态",
         "description": "hub://agent/{task_id} 返回某 Deep Agent 任务的实时状态(待办/步骤/总结/citations/critic/context_digest);等价于 agent_status 工具,供宿主订阅式读取,省反复调工具",
         "mimeType": "application/json"},
        {"uri": "hub://related", "name": "素材关系反查",
         "description": "hub://related/{id}/{rel} 沿 parent:/role:/job: 等面标签一跳遍历;rel 取 all(默认)/parent/children/job/role/kind",
         "mimeType": "application/json"},
    ]


def _resource_read(uri):
    p = uri.replace("hub://", "").strip("/").split("/")
    if p[0] == "recent":
        n = int(p[1]) if len(p) > 1 and p[1].isdigit() else 10
        rows = sorted(core.all_materials(),
                      key=lambda m: m.get("created_at", ""), reverse=True)[:n]
        return [_brief(m) for m in rows]
    if p[0] == "job":
        jid = p[1] if len(p) > 1 else ""
        return [_brief(m) for m in (core.search("", tag="job:" + jid) if jid else [])]
    if p[0] == "history":
        n = int(p[1]) if len(p) > 1 and p[1].isdigit() else 50
        return core.get_history(limit=n)
    if p[0] == "shots":
        mid = p[1] if len(p) > 1 else ""
        if not mid:
            raise ValueError("hub://shots/{id} requires material id")
        d = core.get_shots(mid)
        return d if d is not None else {"id": mid, "status": "missing"}
    if p[0] == "agent":
        tid = p[1] if len(p) > 1 else ""
        if not tid:
            raise ValueError("hub://agent/{task_id} requires task id")
        import agent
        return agent.agent_status(tid)
    if p[0] == "related":
        mid = p[1] if len(p) > 1 else ""
        rel = p[2] if len(p) > 2 and p[2] else "all"
        if not mid:
            raise ValueError("hub://related/{id}/{rel} requires material id")
        import agent
        return agent._tool_related({"id": mid, "rel": rel})
    raise ValueError("unknown resource: " + uri)


# ---------- 写操作护栏(2026 最佳实践:破坏性工具必须 confirm=true + 人工复核) ----------
def _require_confirm(a):
    """任何写/删操作都必须显式 confirm=true,否则拒绝。
    防止 Agent 自主调用误改素材库(对应 E 维度「写必确认」护栏)。"""
    if not a.get("confirm") is True:
        raise ValueError("write operation requires confirm=true (Human-in-the-loop)")


def t_update_tags(a):
    _require_confirm(a)
    mid = a.get("id", "")
    if not core.get_material(mid):
        raise ValueError("not found: " + mid)
    r = core.update_tags(mid, a.get("tags", ""))
    return r


def t_register(a):
    _require_confirm(a)
    path = a.get("path", "")
    if not path:
        raise ValueError("path required")
    r = core.ingest_external(path, source=a.get("source", "mcp"),
                             tags=a.get("tags", ""), description=a.get("description", ""))
    if r is None:
        raise ValueError("cannot register (missing/unsupported file): " + path)
    return r


def t_text_preview(a):
    """只读:返回素材描述 + 关联文本文件(.md/.txt/.srt/.ass/.json)前 N 字符,
    供 Agent 在不拉整文件的前提下理解长文档内容(省 token)。"""
    m = core.get_material(a.get("id", ""))
    if not m:
        raise ValueError("not found: " + a.get("id", ""))
    n = int(a.get("chars") or 2000)
    txt = (m.get("description") or "").strip()
    ep = m.get("external_path") or ""
    if not txt and ep and ep.lower().endswith((".md", ".txt", ".srt", ".ass", ".json", ".vtt")):
        try:
            with open(ep, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read(n)
        except Exception:
            txt = ""
    return {"id": m["id"], "name": m.get("name", ""), "preview": txt[:n]}


def t_chunk_search(a):
    """只读:长文档父子分块检索(见 core.chunk_search)。适合在 description/笔记里
    按段落精准命中,而非整段匹配。返回命中的素材(按最佳子块得分排序)。"""
    return [_brief(m) for m in core.chunk_search(
        a.get("q", ""), limit=int(a.get("limit") or 10),
        kind=a.get("kind", ""), tag=a.get("tag", ""))]


def t_run_ocr(a):
    """写(派生数据):对视频/图片做画面 OCR,文本落 sidecar 并追加 description。
    会改 description,故仍需 confirm=true;已有结果幂等返回 cached(不重跑)。"""
    _require_confirm(a)
    mid = a.get("id", "")
    if not core.get_material(mid):
        raise ValueError("not found: " + mid)
    return core.ocr_material(mid, frames=int(a.get("frames") or 5),
                             force=bool(a.get("force")))


def t_shots(a):
    """只读:返回视频镜头索引(片段 start/end 时间轴),供下游剪辑 Agent 按片段调用。
    未建索引 → {"status":"none"}(不主动建,保持本工具纯只读零副作用)。"""
    d = core.get_shots(a.get("id", ""))
    if d is None:
        return {"status": "none"}
    return d


def t_similar(a):
    """只读:画面级近重复检测(dHash 感知哈希,汉明距离 ≤ max_dist)。
    自身还没算过哈希 → {"status":"no_hash"}(不主动建,保持纯只读零副作用)。"""
    return core.similar_assets(a.get("id", ""), int(a.get("max_dist") or 10))


def t_related(a):
    """只读:按关系反查关联素材(父/子/job同伙/同role/同kind)。"""
    import agent
    return agent._tool_related(a)


def t_missing_covers(a):
    """只读:列出无封面的 videos/silent(可滤 kind)。Agent「补封面」技能第一步。"""
    return core.list_missing_covers(
        kind=str(a.get("kind") or ""),
        skip_failed=a.get("skip_failed", True) is not False,
        limit=int(a.get("limit") or 50),
    )


def t_job_checkup(a):
    """只读:某 job 资产体检(kinds/封面/镜头/asr/失效引用)。"""
    return core.job_checkup(a.get("job_id") or a.get("job") or "")


def t_near_dupes(a):
    """只读:全库画面近重复报告(对+簇)。"""
    return core.near_duplicate_report(
        max_dist=int(a.get("max_dist") or 10),
        limit_pairs=int(a.get("limit") or 50),
    )


def t_imgsearch(a):
    """只读:以图搜图或以文搜图。"""
    text = (a.get("text") or "").strip()
    if text:
        return core.search_by_text_image(
            text, limit=int(a.get("limit") or 20),
            min_score=float(a.get("min_score") or 0.15),
        )
    q = a.get("query") or a.get("id") or a.get("path") or ""
    return core.search_by_image(
        q,
        max_dist=int(a.get("max_dist") or 10),
        limit=int(a.get("limit") or 20),
        mode=str(a.get("mode") or "auto"),
    )


def t_imgembed(a):
    """写(派生):建 CLIP 图像向量索引;需 confirm=true。"""
    _require_confirm(a)
    if a.get("status_only"):
        return core.image_embed_status()
    return core.build_image_embeddings(
        force=bool(a.get("force")),
        limit=int(a.get("limit") or 0),
    )


# ---------- MCP Prompts(可复用技能模板,2026 最佳实践 §15.1) ----------
_PROMPTS = [
    {
        "name": "fill_missing_thumbs",
        "description": "找出无封面的 silent/videos 并规划批量补封面(只读盘点→写需 confirm)",
        "arguments": [
            {"name": "kind", "description": "silent | videos | 空=两者", "required": False},
        ],
    },
    {
        "name": "job_checkup",
        "description": "某 subtitle_pipeline job 全套体检(资产种类/封面/镜头/asr/失效)",
        "arguments": [
            {"name": "job_id", "description": "job 目录名或 job: 标签值", "required": True},
        ],
    },
    {
        "name": "segment_first",
        "description": "找画面片段:先母版 shots 逻辑切片,仅导出文件时才搜 role:clip",
        "arguments": [
            {"name": "query", "description": "自然语言/标签查询(可选)", "required": False},
            {"name": "job_id", "description": "限定 job(可选)", "required": False},
        ],
    },
]


def _prompts_list():
    return [{"name": p["name"], "description": p["description"],
             "arguments": p.get("arguments") or []} for p in _PROMPTS]


def _prompt_get(name, arguments=None):
    arguments = arguments or {}
    if name == "fill_missing_thumbs":
        kind = (arguments.get("kind") or "").strip()
        kind_hint = kind or "silent 优先,再 videos"
        text = (
            "你是素材中心运维 Agent。任务:补齐缺失封面。\n"
            "1. 只读调用 list_missing_covers(kind=%r) 列出无封面条目。\n"
            "2. 汇报 id/kind/name;若只需盘点则 finish。\n"
            "3. 若用户已授权写入:对缺封面条目调用 auto_process(confirm=true) "
            "或面板 POST /api/thumbs;不要删原片。\n"
            "范围提示: %s\n"
            "禁止编造素材 id。"
        ) % (kind, kind_hint)
        return {"description": "无封面补封面技能",
                "messages": [{"role": "user",
                              "content": {"type": "text", "text": text}}]}
    if name == "job_checkup":
        jid = (arguments.get("job_id") or arguments.get("job") or "").strip()
        if not jid:
            raise ValueError("job_checkup requires argument job_id")
        text = (
            "你是素材中心运维 Agent。任务:体检 job=%s。\n"
            "1. 调用 job_checkup(job_id=%r)(确定性,勿臆造)。\n"
            "2. 也可读 Resource hub://job/%s 对照材料清单。\n"
            "3. 总结:kinds 是否含 videos/silent/audio/subs/notes;"
            "master_ids / clip_ids / component_ids;"
            "missing_thumbs / missing_shots(仅母版) / clips_missing_parent / "
            "broken_ids / asr_ids。\n"
            "4. 只读汇报;写修复需用户 confirm。"
        ) % (jid, jid, jid)
        return {"description": "job 全套体检技能",
                "messages": [{"role": "user",
                              "content": {"type": "text", "text": text}}]}
    if name == "segment_first":
        q = (arguments.get("query") or "").strip()
        jid = (arguments.get("job_id") or arguments.get("job") or "").strip()
        scope = ("job_id=%r" % jid) if jid else "全库"
        text = (
            "你是素材中心检索 Agent。任务:按 DAM 方案 A 找「片段」。\n"
            "范围: %s。查询提示: %r\n"
            "规则(必须遵守):\n"
            "1. 默认搜母版: search_materials 用 tag role:master 或 type:media"
            "(不要默认搜 role:clip)。\n"
            "2. 拿到母版 id 后读 get_shots / Resource hub://shots/{id},"
            "用镜头 start/end 作为逻辑切片(一份实体,不落盘)。\n"
            "3. 仅当用户明确要「导出文件 / 已有 range_*.mp4」时,"
            "再 search tag=role:clip(或 type:clip),并汇报 parent:/t_start:/t_end:。\n"
            "4. silent/audio 是声画组件(role:silent-picture / role:audio-stem),"
            "不是剪辑切片;用 parent: 连回母版。\n"
            "5. 只读汇报;禁止编造 id。"
        ) % (scope, q or "(无)")
        return {"description": "片段优先 shots 技能",
                "messages": [{"role": "user",
                              "content": {"type": "text", "text": text}}]}
    raise ValueError("unknown prompt: " + name)


# ---------- Deep Agent(路线 C:编排层在 agent.py,经 MCP 暴露给宿主) ----------
def t_agent_run(a):
    import agent
    import threading
    # 写权限双重护栏:MCP confirm=true(人工复核)→ 才向 agent 传 allow_write,
    # agent 内部写工具此时才注册;缺省一律只读,与既有写护栏同构。
    # 后台执行:本地 LLM 多步推理耗时 1-3 分钟,同步会卡死 MCP 调用;
    # 立即返回 task_id,调用方用 agent_status 轮询进度(agent 每步落盘 state.json)。
    task = a.get("task", "")
    task_id = a.get("task_id") or agent._new_id(task)

    def _run():
        try:
            agent.agent_run(task,
                            allow_write=(a.get("confirm") is True),
                            max_steps=int(a.get("max_steps") or 12),
                            task_id=task_id)
        except Exception:                                   # 后台线程异常不冒泡,状态已落盘
            pass

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "running", "task_id": task_id,
            "hint": "长任务已在后台启动(约 1-3 分钟),用 agent_status 查询进度与结果"}


def t_agent_status(a):
    import agent
    return agent.agent_status(a.get("task_id", ""))


def t_agent_cancel(a):
    """协作式取消:写 cancel.flag,agent 主循环下一步边界终止(进度保留)。"""
    import agent
    return agent.agent_cancel(str(a.get("task_id") or ""))


def t_auto(a):
    """写(派生数据+可能的 AI 打标):对新素材跑全链路自动处理
    (封面/OCR/镜头索引/pHash/语义索引,可选 LLM 打标)。
    各步骤幂等(已处理过 → cached/skip),需要 confirm=true(人工复核)。"""
    _require_confirm(a)
    return core.auto_process_all(limit=int(a.get("limit") or 0),
                                 autotag=bool(a.get("autotag")))


HANDLERS = {"search_materials": t_search, "get_material": t_get,
            "list_tags": t_tags, "hub_stats": t_stats,
            "update_tags": t_update_tags, "register_asset": t_register,
            "read_text_preview": t_text_preview, "chunk_search_materials": t_chunk_search,
            "run_ocr": t_run_ocr, "get_shots": t_shots, "find_similar": t_similar,
            "list_missing_covers": t_missing_covers, "job_checkup": t_job_checkup,
            "near_duplicate_report": t_near_dupes, "search_by_image": t_imgsearch,
            "related": t_related,
            "build_image_embeddings": t_imgembed,
            "agent_run": t_agent_run, "agent_status": t_agent_status,
            "agent_cancel": t_agent_cancel,
            "auto_process": t_auto}
# __PART2__
_SCHEMA_OBJ = {"type": "object", "properties": {
    "q": {"type": "string", "description": "关键词或中文自然语言问句"},
    "kind": {"type": "string", "enum": ["images", "videos", "silent", "docs", "audio", "subs", "anim", "other"]},
    "tag": {"type": "string"},
    "mode": {"type": "string", "enum": ["auto", "lexical", "semantic"]},
    "limit": {"type": "integer"}}}
_CONFIRM = {"type": "object", "properties": {
    "confirm": {"type": "boolean", "description": "必须为 true 才允许写/删操作(人工复核护栏)"}}}

TOOLS = [
    {"name": "search_materials", "description": "检索素材库(344+ 条,支持中文自然语言问句,内置中英同义词+语义混合排序)",
     "inputSchema": _SCHEMA_OBJ},
    {"name": "get_material", "description": "按 id 取素材完整记录",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "list_tags", "description": "列出全部标签及计数",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "hub_stats", "description": "素材库健康快照(总量/种类/重复/引用完整性/封面/字节)",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read_text_preview", "description": "只读:返回素材描述/关联文本文件前 N 字符,理解长文档而不拉整文件(省 token)",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "chars": {"type": "integer"}}, "required": ["id"]}},
    {"name": "chunk_search_materials", "description": "只读:长文档父子分块检索,按段落精准命中 description/笔记",
     "inputSchema": {"type": "object", "properties": {
         "q": {"type": "string"}, "kind": {"type": "string"},
         "tag": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "update_tags", "description": "写:更新素材标签(需 confirm=true)",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "tags": {"type": "string"},
         "confirm": {"type": "boolean"}}, "required": ["id", "confirm"]}},
    {"name": "register_asset", "description": "写:登记外部文件引用(不复制,按原路径索引,需 confirm=true)",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "source": {"type": "string"},
         "tags": {"type": "string"}, "description": {"type": "string"},
         "confirm": {"type": "boolean"}}, "required": ["path", "confirm"]}},
    {"name": "run_ocr", "description": "写(派生数据):视频/图片画面 OCR(离线 rapidocr,ffmpeg 采样帧),文本入 sidecar 并追加 description 使画面文字可被检索。已有结果幂等返回;需 confirm=true",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "frames": {"type": "integer"},
         "force": {"type": "boolean"}, "confirm": {"type": "boolean"}},
         "required": ["id", "confirm"]}},
    {"name": "get_shots", "description": "只读:返回视频镜头索引(片段 start/end 时间轴, 供下游剪辑 Agent 按片段调用)",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}}, "required": ["id"]}},
    {"name": "auto_process", "description": "写(派生数据+可能的AI打标):对新素材跑 封面/OCR/镜头索引/pHash/语义索引 全链路,各步骤幂等(已处理自动跳过);需 confirm=true",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer"}, "autotag": {"type": "boolean"},
         "confirm": {"type": "boolean"}}, "required": ["confirm"]}},
    {"name": "find_similar", "description": "只读:画面级近重复检测(dHash 感知哈希,汉明距离≤max_dist;与 sha256 精确去重互补)",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "max_dist": {"type": "integer"}}, "required": ["id"]}},
    {"name": "list_missing_covers", "description": "只读:列出无封面的 videos/silent(可滤 kind=silent|videos);补封面技能第一步",
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string"}, "limit": {"type": "integer"},
         "skip_failed": {"type": "boolean"}}}},
    {"name": "job_checkup", "description": "只读:某 subtitle_pipeline job 资产体检(kinds/封面/镜头/asr/失效引用)",
     "inputSchema": {"type": "object", "properties": {
         "job_id": {"type": "string"}}, "required": ["job_id"]}},
    {"name": "near_duplicate_report", "description": "只读:全库画面近重复报告(dHash 汉明距离≤max_dist 的对+并查集簇,一实体多引用)",
     "inputSchema": {"type": "object", "properties": {
         "max_dist": {"type": "integer"}, "limit": {"type": "integer"}}}},
    {"name": "search_by_image", "description": "只读:以图搜图(query=id/路径)或以文搜图(text=)。mode=auto|phash|clip;CLIP 需 imgembed 索引",
     "inputSchema": {"type": "object", "properties": {
        "query": {"type": "string"}, "id": {"type": "string"}, "path": {"type": "string"},
        "text": {"type": "string", "description": "自然语言以文搜图"},
        "max_dist": {"type": "integer"}, "limit": {"type": "integer"},
        "mode": {"type": "string", "enum": ["auto", "phash", "clip"]}}}},
    {"name": "related", "description": "只读:按关系反查关联素材(Graph-RAG 轻量版)。利用 parent:/role:/job: 面标签与 kind 沿一跳遍历:父(rel=parent)/子(rel=children)/同 job 产物(rel=job)/同 role(rel=role)/同 kind(rel=kind)/全部并集(rel=all)",
     "inputSchema": {"type": "object", "properties": {
        "id": {"type": "string", "description": "素材 id"},
        "rel": {"type": "string", "enum": ["all", "parent", "children", "job", "role", "kind"]},
        "limit": {"type": "integer"}}, "required": ["id"]}},
    {"name": "build_image_embeddings", "description": "写(派生):为视觉素材建 CLIP 图像向量索引(需 SP venv open_clip+权重);confirm=true",
     "inputSchema": {"type": "object", "properties": {
         "force": {"type": "boolean"}, "limit": {"type": "integer"},
         "status_only": {"type": "boolean"},
         "confirm": {"type": "boolean"}}, "required": ["confirm"]}},
    {"name": "agent_run", "description": "Deep Agent 编排:LLM 先拆待办再逐步调用素材工具完成多步任务(检索综合/巡检/整理)。默认只读;写任务需 confirm=true(人工复核)。后台异步执行(本地 LLM 多轮,约 1-3 分钟),调用立即返回 task_id,用 agent_status 轮询进度与结果;finish 声明的 id 会经 seen_ids 核验剔除编造项。",
     "inputSchema": {"type": "object", "properties": {
         "task": {"type": "string", "description": "自然语言任务(中文)"},
         "confirm": {"type": "boolean", "description": "仅写类任务设 true(启用 agent 内写工具)"},
         "max_steps": {"type": "integer"}}, "required": ["task"]}},
    {"name": "agent_status", "description": "查询 Deep Agent 任务的待办/步骤轨迹/总结",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "agent_cancel", "description": "协作式取消一个进行中的 Deep Agent 任务(在下一步边界终止,已执行进度保留)",
     "inputSchema": {"type": "object", "properties": {
         "task_id": {"type": "string"}}, "required": ["task_id"]}},
]
# __PART3__
def _dispatch(req):
    """处理一条 JSON-RPC 请求;notification(无 id)返回 None。"""
    if "id" not in req:
        return None                      # notification: initialized 等,不回包
    m = req.get("method", "")
    if m == "initialize":
        v = req.get("params", {}).get("protocolVersion", "2024-11-05")
        # 动态能力声明(2026 最佳实践:按运行环境声明,而非固定值):
        #  - 有 embedding 模型 → 语义检索可用;否则 Agent 应走纯词法
        #  - 设了 VITUAL_RERANK_MODEL → 开启 stage-2 重排
        #  - 设了 VITUAL_HUB_TOKEN → 已鉴权
        cap = {"tools": {}, "resources": {}, "prompts": {}}
        return {"protocolVersion": v, "capabilities": cap,
                "serverInfo": {"name": "materials-hub", "version": "1.0", "hub": {
                    "semantic": core.embed_probe()["ok"],
                    "reranker": bool(os.environ.get("VITUAL_RERANK_MODEL", "").strip()),
                    "token_required": bool(HUB_TOKEN),
                    "count": core.count_materials(),
                }}}
    if m == "resources/list":
        return {"resources": _resources_list()}
    if m == "resources/read":
        uri = (req.get("params", {}) or {}).get("uri", "")
        return {"contents": [{"uri": uri, "mimeType": "application/json",
                              "text": json.dumps(_resource_read(uri), ensure_ascii=False)}]}
    if m == "prompts/list":
        return {"prompts": _prompts_list()}
    if m == "prompts/get":
        params = req.get("params", {}) or {}
        return _prompt_get(params.get("name", ""), params.get("arguments") or {})
    if m == "tools/list":
        return {"tools": TOOLS}
    if m == "tools/call":
        p = req.get("params", {})
        fn = HANDLERS.get(p.get("name", ""))
        if fn is None:
            raise ValueError("unknown tool: %s" % p.get("name"))
        res = fn(p.get("arguments") or {})
        return {"content": [{"type": "text",
                             "text": json.dumps(res, ensure_ascii=False)}]}
    if m == "ping":
        return {}
    raise ValueError("unknown method: %s" % m)


def main():
    sys.stdout.reconfigure(encoding="utf-8")   # 管道下默认 GBK,写中文会 UnicodeEncodeError
    sys.stdin.reconfigure(encoding="utf-8")
    core.init_hub()
    st = core.ensure_ollama()            # 语义检索后端顺手自愈(失败不阻塞)
    print("mcp: embed %s" % ("ok:" + st["model"] if st["ok"] else "unavailable"),
          file=sys.stderr)
    while True:                          # 必须 readline() 循环:for-in 迭代
        line = sys.stdin.readline()      # 对管道有预读缓冲,会卡到 EOF 才出第一行
        if not line:
            break
        line = line.strip()
        if not line or line.startswith("Content-"):   # 兼容带 LSP 头的客户端
            continue
        try:
            req = json.loads(line)
            res = _dispatch(req)
            if res is None:
                continue
            out = {"jsonrpc": "2.0", "id": req.get("id"), "result": res}
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": req.get("id") if isinstance(req, dict) else None,
                   "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
