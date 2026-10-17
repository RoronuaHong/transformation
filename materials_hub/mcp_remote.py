"""Materials Hub — 对外 Agent 用 MCP 网关（官方 SDK / 最新最佳实践）。

与 `mcp_server.py`（手写 stdio、仅本机）不同，本文件面向**外部 Agent**，
按 2026 年最新 MCP 规范实现：

  * 传输层：Streamable HTTP（取代 SSE，远程 Agent 经 URL 接入；同时保留 stdio 模式）
  * 统一端点：除 Materials Hub 素材中心全套工具外，自动并入 subtitle_pipeline 的
    Vitual API 工具（vitual_* / vitual://…），外部 Agent 一个端点拿全部功能
  * 鉴权：   Bearer Token（局域网/内网推荐；结构上预留 OAuth 2.1 升级位）
  * 工具注解：title / readOnlyHint / destructiveHint / idempotentHint / openWorldHint
  * 结构化输出：工具返回原生 dict → SDK 自动产出 structuredContent（省 token、易解析）
  * 游标分页：高量级列表（search / tags / agent_list …）用 cursor 分页，避免一次吐全量
  * 人机确认（HITL）：所有写/删操作必须 confirm=true 或交互式 elicitation 二选一
  * 资源模板：hub://recent/{n} 等只读订阅式数据，Agent 像读文件一样按需拉
  * Prompts：可复用技能模板（fill_missing_thumbs / job_checkup / segment_first）
  * 进度与日志：长任务 report_progress；结构化日志走 stderr
  * CORS：允许跨域，便于浏览器型 Agent 直连

启动（复用已装 mcp 的 venv）：

  # 远程（外部 Agent 经 http 接入）
  subtitle_pipeline\\.venv\\Scripts\\python.exe materials_hub\\mcp_remote.py \\
      --transport streamable-http --host 0.0.0.0 --port 8002 --token "<TOKEN>"

  # 本机 stdio（Claude Desktop / CodeBuddy 类本地客户端）
  python materials_hub\\mcp_remote.py --transport stdio --token "<TOKEN>"

未设 VITUAL_HUB_TOKEN / --token 时开放（本机）；一旦设了，客户端必须带一致 Bearer/--token。

外部客户端配置示例（Claude Desktop / Codex / Cursor）：
  {"mcpServers": {"materials-hub": {
      "url": "http://<host>:8002/mcp",
      "transport": "streamable-http",
      "headers": {"Authorization": "Bearer <TOKEN>"}}}}
"""
from __future__ import annotations

import sys
import os
import json
import time
import hmac
import base64
import logging
import argparse
import threading
import secrets
import hashlib
import html as _html
import urllib.parse as _up
from datetime import datetime, timezone
from typing import Any, Optional

# 让 `import core` / `import agent` 命中 materials_hub 顶层模块（与 mcp_server.py 同款注入）。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import anyio  # mcp 依赖，必然可用
import core
import agent  # Deep Agent 编排层

from pydantic import BaseModel, Field
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import ToolAnnotations, ErrorData
from mcp.shared.exceptions import McpError

from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse
import uvicorn

logger = logging.getLogger("materials-hub-mcp")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---- 协议错误码（JSON-RPC / MCP 约定） ----
INVALID_PARAMS = -32602
INTERNAL = -32603
HUMAN_DECLINED = -32000

KIND_OPTIONS = ["images", "videos", "silent", "docs", "audio", "subs", "anim",
                "raw", "project", "vector", "model3d", "font", "other"]

INSTRUCTIONS = (
    "素材中心（Materials Hub）MCP 网关：检索 / 理解 / 治理一个本地多模态素材库"
    "（视频、字幕、图片、文档、3D、字体等），并支持 Deep Agent 多步编排与交付导出。\n"
    "只读工具无需确认；所有写/删/派生数据工具必须 confirm=true 或交互确认（HITL）。\n"
    "高量级列表支持 cursor 分页。资源（hub://...）为只读订阅式视图。\n"
    "本网关同时并入 subtitle_pipeline 的 Vitual API 工具（vitual_* / vitual://...）："
    "提交/轮询字幕管线任务、admin 队列与导出等；这些工具联网访问 :8901，API 不可达时优雅返回错误。"
)

mcp = FastMCP("materials-hub-remote", instructions=INSTRUCTIONS)


# --------------------------------------------------------------------------
# 工具注解辅助
# --------------------------------------------------------------------------
def _ann(title: str, *, ro: bool = True, destructive: bool = False,
         idempotent: bool = False, open_world: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=ro,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )


# --------------------------------------------------------------------------
# 同步核心调用 → 异步（避免阻塞事件循环）
# --------------------------------------------------------------------------
async def _run(func, *args, **kwargs):
    return await anyio.to_thread.run_sync(lambda: func(*args, **kwargs))


# --------------------------------------------------------------------------
# 游标分页
# --------------------------------------------------------------------------
def _paging(cursor: Optional[str], limit, default: int = 20, max_limit: int = 100):
    limit = max(1, min(int(limit if limit is not None else default), max_limit))
    offset = 0
    if cursor:
        try:
            offset = int(base64.urlsafe_b64decode(cursor.encode()).decode())
        except Exception:
            offset = 0
    return offset, limit


def _enc_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _page_seq(seq: list, offset: int, limit: int):
    total = len(seq)
    page = seq[offset:offset + limit]
    nxt = _enc_cursor(offset + limit) if offset + limit < total else None
    return page, nxt, total


# --------------------------------------------------------------------------
# 精简条目（省 token）
# --------------------------------------------------------------------------
def _brief(m: dict) -> dict:
    out = {k: m.get(k, "") for k in
           ("id", "kind", "ext", "name", "size", "tags", "ai_tags",
            "description", "location")}
    if m.get("location") == "external":
        out["external_path"] = m.get("external_path", "")
    return out


# --------------------------------------------------------------------------
# 人机确认（HITL）：confirm=true 或交互式 elicitation 二选一
# --------------------------------------------------------------------------
class _Confirm(BaseModel):
    proceed: bool = Field(False, description="是否确认执行该写操作（人工复核）")
    reason: str = Field("", description="确认理由 / 备注（可选）")


async def _hitl(ctx: Optional[Context], summary: str, confirm_flag: bool):
    """写操作护栏：程序化 confirm=true 或交互式 elicitation，二者满足其一即可。"""
    if confirm_flag:
        return
    if ctx is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="写操作需要 confirm=true 或交互确认，但当前上下文不支持交互。"))
    try:
        res = await ctx.elicit(summary, _Confirm)
    except Exception:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message="当前客户端不支持 elicitation；写操作请传 confirm=true。"))
    # 注意：Context.elicit 返回 AcceptedElicitation(.action + .data) /
    #       DeclinedElicitation / CancelledElicitation —— 用 .data 而非 .content
    data = getattr(res, "data", None)
    proceed = bool(getattr(res, "action", "") == "accept"
                   and data is not None and getattr(data, "proceed", False))
    if not proceed:
        raise McpError(ErrorData(
            code=HUMAN_DECLINED,
            message="操作被人工拒绝或取消（Human-in-the-loop）。"))


def _not_found(what: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message="not found: " + what))


# ==========================================================================
# 读工具（readOnlyHint=True）
# ==========================================================================
@mcp.tool(name="search_materials", title="检索素材库",
          annotations=_ann("检索素材库（支持中文自然语言 / 语义混合）", open_world=True),
          description=(
              "检索素材库。支持中文自然语言问句，内置中英同义词 + 语义混合排序；"
              "英文等非中文查询会自动译中再中英合并检索。cursor 分页：传上次返回的 "
              "next_cursor 翻页。kind 取值见 KIND_OPTIONS。"))
async def search_materials(ctx: Context, q: str = "", kind: str = "",
                          tag: str = "", mode: str = "auto",
                          limit: int = 20, cursor: Optional[str] = None) -> dict:
    offset, lim = _paging(cursor, limit)
    rows = await _run(core.search, q, kind, tag, lim + 1, offset, mode, "")
    items = [_brief(m) for m in rows[:lim]]
    nxt = _enc_cursor(offset + lim) if len(rows) > lim else None
    return {"items": items, "next_cursor": nxt, "limit": lim, "offset": offset}


@mcp.tool(name="get_material", title="取素材完整记录",
          annotations=_ann("按 id 取素材完整记录"),
          description="按 id 取素材完整记录（含全部字段）。")
async def get_material(ctx: Context, id: str) -> dict:
    m = await _run(core.get_material, id)
    if not m:
        raise _not_found(id)
    return m


@mcp.tool(name="list_tags", title="列出全部标签",
          annotations=_ann("列出全部标签及计数"),
          description="列出全部标签及计数（cursor 分页）。")
async def list_tags(ctx: Context, limit: int = 50, cursor: Optional[str] = None) -> dict:
    offset, lim = _paging(cursor, limit, default=50, max_limit=500)
    allt = await _run(core.distinct_tags)
    page, nxt, total = _page_seq(allt, offset, lim)
    return {"items": [{"tag": t, "count": c} for t, c in page],
            "next_cursor": nxt, "total": total}


@mcp.tool(name="hub_stats", title="素材库健康快照",
          annotations=_ann("素材库健康快照"),
          description="素材库健康快照（总量 / 种类 / 重复 / 引用完整性 / 封面 / 字节）。")
async def hub_stats(ctx: Context) -> dict:
    return await _run(core.health)


@mcp.tool(name="read_text_preview", title="读素材文本预览",
          annotations=_ann("返回素材描述 / 关联文本前 N 字符"),
          description="只读：返回素材描述 + 关联文本文件（.md/.txt/.srt/.ass/.json）前 N 字符，"
                      "理解长文档而不拉整文件（省 token）。")
async def read_text_preview(ctx: Context, id: str, chars: int = 2000) -> dict:
    m = await _run(core.get_material, id)
    if not m:
        raise _not_found(id)
    n = int(chars or 2000)
    txt = (m.get("description") or "").strip()
    ep = m.get("external_path") or ""
    if not txt and ep and ep.lower().endswith((".md", ".txt", ".srt", ".ass", ".json", ".vtt")):
        try:
            with open(ep, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read(n)
        except Exception:
            txt = ""
    return {"id": m["id"], "name": m.get("name", ""), "preview": txt[:n]}


@mcp.tool(name="chunk_search_materials", title="长文档分块检索",
          annotations=_ann("长文档父子分块检索"),
          description="只读：长文档父子分块检索，按段落精准命中 description / 笔记。"
                      "返回命中的素材（按最佳子块得分排序）。")
async def chunk_search_materials(ctx: Context, q: str, kind: str = "",
                                 tag: str = "", limit: int = 10) -> dict:
    rows = await _run(core.chunk_search, q, limit, kind, tag)
    return {"items": [_brief(m) for m in rows]}


@mcp.tool(name="get_shots", title="视频镜头索引",
          annotations=_ann("返回视频镜头索引（片段时间轴）"),
          description="只读：返回视频镜头索引（片段 start/end 时间轴）；未建索引 → {status:'none'}。")
async def get_shots(ctx: Context, id: str) -> dict:
    d = await _run(core.get_shots, id)
    return d if d is not None else {"status": "none"}


@mcp.tool(name="find_similar", title="画面近重复检测",
          annotations=_ann("画面级近重复检测（dHash）"),
          description="只读：画面级近重复检测（dHash 感知哈希，汉明距离≤max_dist）；"
                      "自身未算哈希 → {status:'no_hash'}。")
async def find_similar(ctx: Context, id: str, max_dist: int = 10) -> dict:
    return await _run(core.similar_assets, id, int(max_dist))


@mcp.tool(name="list_missing_covers", title="无封面素材清单",
          annotations=_ann("列出无封面的 videos/silent"),
          description="只读：列出无封面的 videos/silent（可滤 kind=silent|videos）。")
async def list_missing_covers(ctx: Context, kind: str = "", limit: int = 50,
                              skip_failed: bool = True) -> dict:
    rows = await _run(core.list_missing_covers, kind, skip_failed, int(limit))
    return {"items": rows}


@mcp.tool(name="job_checkup", title="Job 资产体检",
          annotations=_ann("某 job 资产体检"),
          description="只读：某 subtitle_pipeline job 资产体检（kinds / 封面 / 镜头 / asr / 失效引用）。")
async def job_checkup(ctx: Context, job_id: str) -> dict:
    return await _run(core.job_checkup, job_id or "")


@mcp.tool(name="job_lang_readiness", title="Job 字幕语种就绪度",
          annotations=_ann("某 job 是否具备点名的 lang: 字幕"),
          description="只读：某 job 是否具备点名的 lang: 字幕；缺该语种只是说明（不整体判死），"
                      "返回 available_langs。")
async def job_lang_readiness(ctx: Context, job_id: str, lang: str = "") -> dict:
    return await _run(core.job_lang_readiness, job_id or "", lang)


@mcp.tool(name="near_duplicate_report", title="全库近重复报告",
          annotations=_ann("全库画面近重复报告"),
          description="只读：全库画面近重复报告（dHash 汉明距离≤max_dist 的对 + 并查集簇）。"
                      "pairs 支持 cursor 分页。")
async def near_duplicate_report(ctx: Context, max_dist: int = 10,
                                limit_pairs: int = 50, cursor: Optional[str] = None) -> dict:
    offset, lim = _paging(cursor, limit_pairs, default=50, max_limit=500)
    rep = await _run(core.near_duplicate_report, int(max_dist), offset + lim)
    pairs = rep.get("pairs", []) if isinstance(rep, dict) else []
    page, nxt, total = _page_seq(pairs, 0, lim) if offset == 0 else (pairs[:lim], None, len(pairs))
    # near_duplicate_report 不支持 offset，这里用切片近似分页
    page = pairs[:lim]
    nxt = _enc_cursor(lim) if len(pairs) > lim else None
    return {"pairs": page, "next_cursor": nxt, "total_pairs": len(pairs),
            "clusters": rep.get("clusters", []) if isinstance(rep, dict) else []}


@mcp.tool(name="governance_report", title="治理合规扫描",
          annotations=_ann("全库治理/合规扫描"),
          description="只读：全库治理 / 合规扫描（占位 / 缺描述 / 未分类 / 无标签）。")
async def governance_report(ctx: Context, limit: int = 50) -> dict:
    return await _run(core.governance_report, int(limit))


@mcp.tool(name="distribution_readiness", title="分发渠道就绪度",
          annotations=_ann("某 job 分发渠道就绪度"),
          description="只读：某 job 分发渠道就绪度评估（封面 / 镜头 / clip 父链 / 标签齐备 → channel_ready）。")
async def distribution_readiness(ctx: Context, job_id: str) -> dict:
    return await _run(core.distribution_readiness, job_id or "")


@mcp.tool(name="learning_summary", title="反馈学习汇总",
          annotations=_ann("汇总 Agent 反馈学习日志"),
          description="只读：汇总 Agent 反馈学习日志（各动作采纳率 + 近期记录），records 支持 cursor 分页。")
async def learning_summary(ctx: Context, limit: int = 50, cursor: Optional[str] = None) -> dict:
    offset, lim = _paging(cursor, limit, default=50, max_limit=200)
    rep = await _run(core.learning_summary, offset + lim)
    recs = rep.get("records", []) if isinstance(rep, dict) else []
    page = recs[:lim]
    nxt = _enc_cursor(lim) if len(recs) > lim else None
    return {"records": page, "next_cursor": nxt,
            "rates": rep.get("rates", {}) if isinstance(rep, dict) else {}}


@mcp.tool(name="related", title="关系反查",
          annotations=_ann("按关系反查关联素材"),
          description="只读：按关系反查关联素材（Graph-RAG 轻量版）。rel 取 all/parent/children/job/role/kind。")
async def related(ctx: Context, id: str, rel: str = "all", limit: int = 20) -> dict:
    if not id:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="id required"))
    rows = await _run(agent._tool_related, {"id": id, "rel": rel, "limit": int(limit)})
    return {"items": rows}


@mcp.tool(name="search_by_image", title="以图/文搜图",
          annotations=_ann("以图搜图或以文搜图"),
          description="只读：以图搜图（query=id/路径）或以文搜图（text=）。mode=auto|phash|clip；"
                      "CLIP 需先 build_image_embeddings。")
async def search_by_image(ctx: Context, query: str = "", id: str = "", path: str = "",
                          text: str = "", max_dist: int = 10, limit: int = 20,
                          mode: str = "auto") -> dict:
    t = (text or "").strip()
    if t:
        return await _run(core.search_by_text_image, t, int(limit), 0.15)
    q = query or id or path or ""
    return await _run(core.search_by_image, q, max_dist=int(max_dist),
                      limit=int(limit), mode=mode)


@mcp.tool(name="image_embed_status", title="CLIP 索引状态",
          annotations=_ann("CLIP 图像向量索引状态"),
          description="只读：CLIP 图像向量索引构建状态（是否已建、覆盖数）。")
async def image_embed_status(ctx: Context) -> dict:
    return await _run(core.image_embed_status)


@mcp.tool(name="agent_status", title="Deep Agent 任务状态",
          annotations=_ann("查询 Deep Agent 任务状态"),
          description="查询 Deep Agent 任务的待办 / 步骤轨迹 / 总结。")
async def agent_status(ctx: Context, task_id: str) -> dict:
    return await _run(agent.agent_status, task_id)


@mcp.tool(name="agent_list", title="Deep Agent 任务盘点",
          annotations=_ann("盘点历史 Deep Agent 任务"),
          description="只读：盘点历史 Deep Agent 任务（按创建时间倒序）；cursor 分页。")
async def agent_list(ctx: Context, limit: int = 20, cursor: Optional[str] = None) -> dict:
    offset, lim = _paging(cursor, limit, default=20, max_limit=200)
    rows = await _run(agent.agent_list, offset + lim)
    page = rows[:lim]
    nxt = _enc_cursor(lim) if len(rows) > lim else None
    return {"items": page, "next_cursor": nxt}


@mcp.tool(name="agent_cancel", title="取消 Deep Agent 任务",
          annotations=_ann("协作式取消 Agent 任务", ro=False, idempotent=True),
          description="协作式取消一个进行中的 Deep Agent 任务（下一步边界终止，进度保留）。")
async def agent_cancel(ctx: Context, task_id: str) -> dict:
    return await _run(agent.agent_cancel, str(task_id or ""))


# ==========================================================================
# 写工具（readOnlyHint=False，全部走 HITL 护栏）
# ==========================================================================
@mcp.tool(name="update_tags", title="合并打标",
          annotations=_ann("合并打标（只增不删）", ro=False, idempotent=True),
          description="写：合并打标（只增不删；系统面标签不可删；删须 remove）。需 confirm=true 或交互确认。")
async def update_tags(ctx: Context, id: str, tags: str = "", remove=None,
                      confirm: bool = False) -> dict:
    await _hitl(ctx, f"即将合并打标素材 {id}：+[{tags}] -[{remove}]", confirm)
    if not await _run(core.get_material, id):
        raise _not_found(id)
    return await _run(core.merge_material_tags, id, tags, remove)


@mcp.tool(name="register_asset", title="登记外部素材",
          annotations=_ann("登记外部文件引用（不复制）", ro=False, idempotent=False, open_world=True),
          description="写：登记外部文件引用（不复制，按原路径索引）。需 confirm=true 或交互确认。")
async def register_asset(ctx: Context, path: str, source: str = "mcp",
                        tags: str = "", description: str = "", confirm: bool = False) -> dict:
    await _hitl(ctx, f"即将登记外部文件引用（不复制）：{path}\n标签：{tags or '(空)'}\n"
                     f"描述：{description or '(空)'}", confirm)
    if not path:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="path required"))
    r = await _run(core.ingest_external, path, source, tags, description)
    if r is None:
        raise McpError(ErrorData(code=INVALID_PARAMS,
                                 message="cannot register (missing/unsupported file): " + path))
    return r


@mcp.tool(name="run_ocr", title="画面 OCR",
          annotations=_ann("视频/图片画面 OCR（派生数据）", ro=False, idempotent=True),
          description="写（派生数据）：对视频/图片做画面 OCR，文本落 sidecar 使画面文字可检索。"
                      "已有结果幂等返回；需 confirm=true 或交互确认。")
async def run_ocr(ctx: Context, id: str, frames: int = 5, force: bool = False,
                 confirm: bool = False) -> dict:
    await _hitl(ctx, f"将对 {id} 跑画面 OCR（派生数据，写 sidecar）", confirm)
    if not await _run(core.get_material, id):
        raise _not_found(id)
    return await _run(core.ocr_material, id, int(frames), bool(force))


@mcp.tool(name="run_visual", title="画面描述",
          annotations=_ann("视频/图片画面描述（派生数据）", ro=False, idempotent=True),
          description="写（派生数据）：本地多模态 VLM 生成画面描述 + 标签落 sidecar 使画面内容可检索。"
                      "已有结果幂等返回；需 confirm=true 或交互确认。")
async def run_visual(ctx: Context, id: str, frames: int = 3, force: bool = False,
                    confirm: bool = False) -> dict:
    await _hitl(ctx, f"将对 {id} 跑画面描述（派生数据，写 sidecar）", confirm)
    if not await _run(core.get_material, id):
        raise _not_found(id)
    return await _run(core.visual_material, id, int(frames), bool(force))


@mcp.tool(name="build_image_embeddings", title="建 CLIP 图像索引",
          annotations=_ann("建 CLIP 图像向量索引（派生）", ro=False, idempotent=True),
          description="写（派生）：为视觉素材建 CLIP 图像向量索引（需 SP venv open_clip + 权重）。"
                      "status_only=true 仅查状态；需 confirm=true 或交互确认。")
async def build_image_embeddings(ctx: Context, force: bool = False, limit: int = 0,
                                 status_only: bool = False, confirm: bool = False) -> dict:
    await _hitl(ctx, "将建 CLIP 图像向量索引（派生数据）", confirm)
    if status_only:
        return await _run(core.image_embed_status)
    await ctx.report_progress(0, 100, "开始建索引")
    r = await _run(core.build_image_embeddings, bool(force), int(limit), None)
    await ctx.report_progress(100, 100, "完成")
    return r


@mcp.tool(name="auto_process", title="全链路自动处理",
          annotations=_ann("素材全链路自动处理（派生）", ro=False, idempotent=True),
          description="写（派生数据 + 可能的 AI 打标）：对新素材跑 封面/OCR/镜头索引/pHash/语义索引 "
                      "全链路，各步骤幂等。需 confirm=true 或交互确认。")
async def auto_process(ctx: Context, limit: int = 0, autotag: bool = False,
                       confirm: bool = False) -> dict:
    await _hitl(ctx, "将对新素材跑全链路自动处理（封面/OCR/镜头/pHash/语义）", confirm)
    await ctx.report_progress(0, 100, "启动全链路")
    r = await _run(core.auto_process_all, int(limit), bool(autotag))
    await ctx.report_progress(100, 100, "完成")
    return r


@mcp.tool(name="agent_run", title="Deep Agent 编排",
          annotations=_ann("Deep Agent 多步编排", ro=False, idempotent=False),
          description="Deep Agent 编排：LLM 先拆待办再逐步调用素材工具完成多步任务。"
                      "默认只读；写任务需 confirm=true（启用 agent 内写工具）。后台异步执行（约 1-3 分钟），"
                      "立即返回 task_id，用 agent_status 轮询。")
async def agent_run(ctx: Context, task: str, confirm: bool = False,
                   max_steps: int = 12) -> dict:
    await _hitl(ctx, f"Deep Agent 将执行（写权限={confirm}）：{task}", confirm)
    if not (task or "").strip():
        raise McpError(ErrorData(code=INVALID_PARAMS, message="task required"))
    task_id = await _run(agent._new_id, task)

    def _runner():
        try:
            agent.agent_run(task, allow_write=confirm, max_steps=int(max_steps), task_id=task_id)
        except Exception:
            agent._mark_error(task_id, "[error] 后台线程异常终止,任务未能完成")

    threading.Thread(target=_runner, daemon=True).start()
    return {"status": "running", "task_id": task_id,
            "hint": "长任务已在后台启动(约 1-3 分钟),用 agent_status 查询进度与结果"}


@mcp.tool(name="agent_resume", title="续跑 Deep Agent 任务",
          annotations=_ann("续跑 max_steps_reached 的任务", ro=False, idempotent=False),
          description="续跑一个 max_steps_reached 的 Deep Agent 任务（复用待办/历史步骤，接续步号）。后台执行。")
async def agent_resume(ctx: Context, task_id: str, extra_steps: int = 6) -> dict:
    tid = str(task_id or "")
    if not tid:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="task_id required"))

    def _runner():
        try:
            agent.agent_resume(tid, extra_steps=int(extra_steps))
        except Exception:
            agent._mark_error(tid, "[error] 续跑后台线程异常终止")

    threading.Thread(target=_runner, daemon=True).start()
    return {"status": "running", "task_id": tid,
            "hint": "续跑已在后台启动,用 agent_status 查询进度与结果"}


@mcp.tool(name="assemble_package", title="组装素材包",
          annotations=_ann("把高层目标落成可交付素材包", ro=False, idempotent=True),
          description="把高层目标落成可交付素材包（多智能体编排 Librarian→Critic→Executor；"
                      "只写 manifest 到 agent_workspace/packages/，绝不改动资产）。")
async def assemble_package(ctx: Context, goal: str, kind: str = "", scope: str = "all",
                           limit: int = 12, queries: Optional[list] = None) -> dict:
    if not (goal or "").strip():
        raise McpError(ErrorData(code=INVALID_PARAMS, message="goal required"))
    return await _run(agent._assemble_package, goal, None, kind, int(limit), scope, queries)


@mcp.tool(name="deliver_package", title="导出交付变体",
          annotations=_ann("导出交付变体（只新建不改资产）", ro=False, idempotent=False),
          description="写：把素材包(manifest)/指定 id 导出为下游交付变体（转码/区间裁剪/格式归一）。"
                      "只新建文件、绝不改动资产本体；需 confirm=true 或交互确认。")
async def deliver_package(ctx: Context, manifest: str = "", ids: Optional[list] = None,
                          fmt: str = "mp4", res: str = "720",
                          clips: Optional[dict] = None, out_dir: str = "",
                          copy_only: bool = False, overwrite: bool = False,
                          confirm: bool = False) -> dict:
    await _hitl(ctx, "即将导出交付文件（只新建,不改资产）", confirm)
    clips_parsed = {}
    for k, v in (clips or {}).items():
        try:
            clips_parsed[str(k)] = (float(v[0]), float(v[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return await _run(core.deliver_package,
                      manifest or None, (ids or None),
                      out_dir=out_dir or None, confirm=True, fmt=fmt, res=res,
                      clips=clips_parsed, copy_only=bool(copy_only),
                      overwrite=bool(overwrite))


@mcp.tool(name="agent_feedback", title="记录 Agent 反馈",
          annotations=_ann("记录 Agent 动作采纳/否决", ro=False, idempotent=False),
          description="写（追加，非资产）：记录 Agent 动作被采纳/否决（持续学习闭环）。"
                      "否决某检索词时传 translated_query + orig_query，使英文与简体否决同一条。")
async def agent_feedback(ctx: Context, action: str, accepted: bool, note: str = "",
                         by: str = "agent", lang: str = "", orig_query: str = "",
                         translated_query: str = "") -> dict:
    if not action:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="action required"))
    r = await _run(core.log_feedback, action, bool(accepted), note, by, lang,
                   orig_query, translated_query)
    if not accepted:
        for q in (translated_query, orig_query):
            q = (str(q or "")).strip()
            if q:
                await _run(core.add_veto, q)
    return r


# ==========================================================================
# 资源（只读订阅式视图，2026 最佳实践：读用 Resources）
# ==========================================================================
@mcp.resource("hub://recent", name="最近登记的素材(默认10)",
              description="按时间倒序的最新素材(默认 10 条)。", mime_type="application/json")
def res_recent_default() -> list:
    return _res_recent_n(10)


@mcp.resource("hub://recent/{n}", name="最近登记的素材",
              description="按时间倒序的最新素材；hub://recent/{n} 指定条数(1-200)。",
              mime_type="application/json")
def _res_recent_n(n: int = 10) -> list:
    n = max(1, min(int(n), 200))
    rows = sorted(core.all_materials(), key=lambda m: m.get("created_at", ""), reverse=True)[:n]
    return [_brief(m) for m in rows]


@mcp.resource("hub://history", name="写操作审计日志(默认50)",
              description="按时间倒序的标签/描述/删除/打标/OCR 变更记录(默认 50)。",
              mime_type="application/json")
def res_history_default() -> list:
    return core.get_history(limit=50)


@mcp.resource("hub://history/{n}", name="写操作审计日志",
              description="按时间倒序的写操作审计；hub://history/{n} 指定条数。",
              mime_type="application/json")
def _res_history_n(n: int = 50) -> list:
    n = max(1, min(int(n), 500))
    return core.get_history(limit=n)


@mcp.resource("hub://job/{job_id}", name="某 job 全套素材",
              description="hub://job/{job_id} 返回该 job 通过 bridge 登记的全部素材。",
              mime_type="application/json")
def res_job(job_id: str) -> list:
    return [_brief(m) for m in (core.search("", tag="job:" + job_id) if job_id else [])]


@mcp.resource("hub://shots/{id}", name="视频镜头索引",
              description="hub://shots/{id} 返回 ffmpeg scenecut 镜头表[{start,end},...]；"
                          "未建索引则 {status:missing}。",
              mime_type="application/json")
def res_shots(id: str) -> dict:
    d = core.get_shots(id)
    return d if d is not None else {"id": id, "status": "missing"}


@mcp.resource("hub://agent/{task_id}", name="Agent 任务实时状态",
              description="hub://agent/{task_id} 返回某 Deep Agent 任务的实时状态"
                          "(待办/步骤/总结/citations/critic/context_digest)。",
              mime_type="application/json")
def res_agent(task_id: str) -> dict:
    return agent.agent_status(task_id)


@mcp.resource("hub://related/{id}/{rel}", name="素材关系反查",
              description="hub://related/{id}/{rel} 沿 parent:/role:/job: 等面标签一跳遍历；"
                          "rel 取 all(默认)/parent/children/job/role/kind。",
              mime_type="application/json")
def res_related(id: str, rel: str = "all") -> dict:
    return {"items": agent._tool_related({"id": id, "rel": rel})}


@mcp.resource("hub://understand/{id}", name="素材结构化理解记录",
              description="hub://understand/{id} 返回该素材的理解记录(画面语种/画质/人物/版权/复核)。",
              mime_type="application/json")
def res_understand(id: str) -> dict:
    d = core.read_understand_record(id)
    return d if d is not None else {"id": id, "status": "missing"}


@mcp.resource("hub://run/{id}", name="素材入库链路状态",
              description="hub://run/{id} 返回该素材入库逐步状态 + 字幕挂接语种；未跑则 {status:missing}。",
              mime_type="application/json")
def res_run(id: str) -> dict:
    d = core.read_run_record(id)
    return d if d is not None else {"id": id, "status": "missing"}


# ==========================================================================
# Prompts（可复用技能模板）
# ==========================================================================
@mcp.prompt(name="fill_missing_thumbs",
            description="找出无封面的 silent/videos 并规划批量补封面(只读盘点→写需 confirm)")
def prompt_fill_missing_thumbs(kind: str = "") -> str:
    kind_hint = kind or "silent 优先,再 videos"
    return (
        "你是素材中心运维 Agent。任务：补齐缺失封面。\n"
        "1. 只读调用 list_missing_covers(kind=%r) 列出无封面条目。\n"
        "2. 汇报 id/kind/name；若只需盘点则 finish。\n"
        "3. 若用户已授权写入：对缺封面条目调用 auto_process(confirm=true)。不要删原片。\n"
        "范围提示: %s\n禁止编造素材 id。" % (kind, kind_hint))


@mcp.prompt(name="job_checkup",
            description="某 subtitle_pipeline job 全套体检(资产种类/封面/镜头/asr/失效)")
def prompt_job_checkup(job_id: str) -> str:
    if not job_id:
        return "job_checkup 需要参数 job_id（job 目录名或 job: 标签值）。"
    return (
        "你是素材中心运维 Agent。任务：体检 job=%s。\n"
        "1. 调用 job_checkup(job_id=%r)(确定性,勿臆造)。\n"
        "2. 也可读 Resource hub://job/%s 对照材料清单。\n"
        "3. 总结：kinds 是否含 videos/silent/audio/subs/notes；master/clip/component ids；"
        "missing_thumbs / missing_shots / clips_missing_parent / broken_ids / asr_ids。\n"
        "4. 只读汇报；写修复需用户 confirm。" % (job_id, job_id, job_id))


@mcp.prompt(name="segment_first",
            description="找画面片段：先母版 shots 逻辑切片,仅导出文件时才搜 role:clip")
def prompt_segment_first(query: str = "", job_id: str = "") -> str:
    scope = ("job_id=%r" % job_id) if job_id else "全库"
    return (
        "你是素材中心检索 Agent。任务：按 DAM 方案 A 找「片段」。\n"
        "范围: %s。查询提示: %r\n规则(必须遵守):\n"
        "1. 默认搜母版: search_materials 用 tag role:master 或 type:media。\n"
        "2. 拿到母版 id 后读 get_shots / Resource hub://shots/{id}，用镜头 start/end 作逻辑切片。\n"
        "3. 仅当用户明确要「导出文件 / 已有 range_*.mp4」时，再搜 role:clip，并汇报 parent:/t_start:/t_end:。\n"
        "4. silent/audio 是声画组件，不是剪辑切片；用 parent: 连回母版。\n"
        "5. 只读汇报；禁止编造 id。" % (scope, query or "(无)"))


# ==========================================================================
# 并入 subtitle_pipeline 的 Vitual API 工具/资源（best-effort，失败不阻断素材中心）
# 让外部 Agent 一个端点拿到「素材中心 + 字幕管线」全部功能。
# 注意：FastMCP 1.29 无 mount/import_server，这里用 add_tool/add_resource 重注册。
# ==========================================================================
def _merge_vitual():
    sp = os.path.normpath(os.path.join(_HERE, "..", "subtitle_pipeline"))
    if sp not in sys.path:
        sys.path.insert(0, sp)
    try:
        import vitual_mcp.server as vs
    except Exception as e:
        logger.warning("Vitual MCP 未并入(导入失败,本网关仅暴露素材中心): %s", e)
        return
    src = vs.mcp
    added_t = added_r = added_tmpl = 0
    for name, tool in list(src._tool_manager._tools.items()):
        try:
            ann = tool.annotations
            if isinstance(ann, dict):
                ann = ToolAnnotations(**ann)
            mcp.add_tool(tool.fn, name=name, title=getattr(tool, "title", None),
                         description=tool.description, annotations=ann)
            added_t += 1
        except Exception as e:
            logger.warning("skip vitual tool %s: %s", name, e)
    for uri, res in list(src._resource_manager._resources.items()):
        try:
            mcp.add_resource(res)
            added_r += 1
        except Exception as e:
            logger.warning("skip vitual resource %s: %s", uri, e)
    for uri, tmpl in list(src._resource_manager._templates.items()):
        try:
            mcp._resource_manager.add_template(
                tmpl.fn, tmpl.uri_template, name=tmpl.name,
                title=getattr(tmpl, "title", None),
                description=tmpl.description,
                mime_type=getattr(tmpl, "mime_type", None))
            added_tmpl += 1
        except Exception as e:
            logger.warning("skip vitual template %s: %s", uri, e)
    logger.info("merged Vitual MCP: tools=%d resources=%d templates=%d",
                added_t, added_r, added_tmpl)


_merge_vitual()


# ==========================================================================
# Bearer 鉴权中间件（纯 ASGI，透传 send 不缓冲 —— 兼容 MCP 的 SSE 流式响应）
#   主令牌(master) 或 OAuth2.1 访问令牌 任一通过。
#   注：刻意不用 BaseHTTPMiddleware——它会破坏流式/SSE 响应，导致会话清理异常。
# ==========================================================================
_OAUTH_OPEN_PATHS = {"/authorize", "/token", "/register"}


class BearerAuthMiddleware:
    def __init__(self, app, token: str = "", oauth=None):
        self.app = app
        self.token = token
        self.oauth = oauth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        # CORS 预检、OAuth 发现端点、OAuth 授权服务器端点、健康检查放行
        if (method == "OPTIONS" or path.startswith("/.well-known")
                or path in _OAUTH_OPEN_PATHS or path == "/healthz"):
            await self.app(scope, receive, send)
            return
        if self.token:
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1")
            bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            host = headers.get(b"host", b"").decode("latin-1")
            scheme = scope.get("scheme", "http")
            meta = f"{scheme}://{host}/.well-known/oauth-protected-resource"
            ok = bool(bearer) and (
                hmac.compare_digest(bearer, self.token)
                or (self.oauth is not None and self.oauth.valid_token(bearer)))
            if not ok:
                await self._deny(send, meta, missing=not bearer)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send, meta: str, missing: bool):
        www = f'Bearer realm="materials-hub", resource_metadata="{meta}"'
        if not missing:
            www += ', error="invalid_token"'
        body = json.dumps({
            "error": "unauthorized" if missing else "invalid_token",
            "hint": ("缺少 Authorization: Bearer <token>；OAuth 客户端请先走 "
                     "authorization_code+PKCE（见 /.well-known/oauth-authorization-server）"
                     if missing else "token 无效或已过期")}).encode("utf-8")
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"www-authenticate", www.encode("latin-1")),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


# ==========================================================================
# 内置 OAuth 2.1 授权服务器（RFC 8414 发现 + RFC 7591 DCR + AuthCode + PKCE）
#   - 公共客户端（无 client_secret），PKCE S256 强制
#   - 运营者在 /authorize 以主令牌(master token)批准 → 发授权码 → 换访问令牌
#   - 令牌为内存态、可吊销、带 TTL（重启失效；私网/本机场景足够）
# ==========================================================================
class OAuthServer:
    def __init__(self, master_token: str, issuer: str = "", path: str = "/mcp"):
        self.master = master_token
        self.issuer = issuer.rstrip("/")
        self.path = path
        self._lock = threading.Lock()
        self._clients = {}   # client_id -> {client_id, redirect_uris, client_name, created}
        self._codes = {}     # code -> {client_id, redirect_uri, code_challenge, method, scope, resource, exp}
        self._access = {}    # token -> {client_id, scope, resource, exp}
        self._refresh = {}   # token -> {client_id, scope, resource, exp}
        self.access_ttl = 3600
        self.refresh_ttl = 30 * 24 * 3600
        self.code_ttl = 120

    # ---- 工具 ----
    def _base(self, request) -> str:
        if self.issuer:
            return self.issuer
        return str(request.base_url).rstrip("/")

    @staticmethod
    def _now() -> float:
        return datetime.now(timezone.utc).timestamp()

    @staticmethod
    def _rand(n: int) -> str:
        return secrets.token_urlsafe(n)

    @staticmethod
    def _pkce_ok(verifier: str, challenge: str, method: str) -> bool:
        if method != "S256" or not verifier or not challenge:
            return False
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        calc = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return hmac.compare_digest(calc, challenge)

    @staticmethod
    async def _form_or_json(request) -> dict:
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                return dict(await request.json())
            form = await request.form()
            return {k: form.get(k) for k in form}
        except Exception:
            return {}

    def valid_token(self, tok: str) -> bool:
        if not tok:
            return False
        with self._lock:
            rec = self._access.get(tok)
        return bool(rec and rec["exp"] > self._now())

    # ---- RFC 8414 / RFC 9725 元数据 ----
    async def metadata(self, request):
        base = self._base(request)
        return JSONResponse({
            "issuer": base,
            "authorization_endpoint": base + "/authorize",
            "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["mcp"],
        })

    async def protected_resource(self, request):
        base = self._base(request)
        return JSONResponse({
            "resource": base + self.path,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp"],
        })

    # ---- RFC 7591 动态客户端注册 ----
    async def register(self, request):
        data = await self._form_or_json(request)
        uris = data.get("redirect_uris") or []
        if isinstance(uris, str):
            uris = [uris]
        uris = [str(u) for u in uris]
        client_id = self._rand(16)
        name = str(data.get("client_name") or "mcp-client")
        with self._lock:
            self._clients[client_id] = {"client_id": client_id, "redirect_uris": uris,
                                        "client_name": name, "created": self._now()}
        return JSONResponse({
            "client_id": client_id,
            "client_id_issued_at": int(self._now()),
            "client_name": name,
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }, status_code=201)

    # ---- 授权端点（运营者以主令牌批准）----
    async def authorize(self, request):
        q = request.query_params
        if request.method == "POST":
            form = await self._form_or_json(request)
            merged = dict(q)
            merged.update({k: v for k, v in form.items()})
            q = merged
        client_id = str(q.get("client_id", ""))
        redirect_uri = str(q.get("redirect_uri", ""))
        state = str(q.get("state", ""))
        code_challenge = str(q.get("code_challenge", ""))
        method = str(q.get("code_challenge_method", "S256"))
        scope = str(q.get("scope", "mcp"))
        base = self._base(request)
        resource = str(q.get("resource") or (base + self.path))
        with self._lock:
            client = self._clients.get(client_id)
        if not client or (client["redirect_uris"] and redirect_uri not in client["redirect_uris"]):
            return JSONResponse({"error": "invalid_request",
                                 "error_description": "unknown client_id or redirect_uri"},
                                status_code=400)
        if not code_challenge or method != "S256":
            return JSONResponse({"error": "invalid_request",
                                 "error_description": "PKCE with S256 is required"},
                                status_code=400)
        key = str(q.get("key", ""))
        if self.master and not hmac.compare_digest(key, self.master):
            return HTMLResponse(self._consent_page(client.get("client_name", "")))
        code = self._rand(24)
        with self._lock:
            self._codes[code] = {"client_id": client_id, "redirect_uri": redirect_uri,
                                 "code_challenge": code_challenge, "method": method,
                                 "scope": scope, "resource": resource,
                                 "exp": self._now() + self.code_ttl}
        sep = "&" if "?" in redirect_uri else "?"
        loc = f"{redirect_uri}{sep}code={_up.quote(code)}&state={_up.quote(state)}"
        return RedirectResponse(loc, status_code=302)

    def _consent_page(self, client_name: str) -> str:
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>授权 Materials Hub MCP</title><style>"
            "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:520px;"
            "margin:3rem auto;padding:0 1rem;color:#222}"
            "input,button{font-size:1rem;padding:.6rem;width:100%;box-sizing:border-box;"
            "margin:.5rem 0;border:1px solid #ccc;border-radius:8px}"
            "button{background:#111;color:#fff;border-color:#111;cursor:pointer}"
            ".muted{color:#888;font-size:.82rem}"
            "</style></head><body>"
            f"<h2>授权访问 Materials Hub MCP</h2>"
            f"<p>客户端 <b>{client_name}</b> 请求访问本网关。</p>"
            "<form method='get' action='/authorize' id='f'>"
            "<input name='key' type='password' placeholder='运营者 Access Token（主令牌）' "
            "autocomplete='off' autofocus required>"
            "<button type='submit'>批准授权</button></form>"
            "<p class='muted'>批准后客户端将获得受 PKCE 保护的访问令牌。"
            "此令牌仅授予本次批准的权限范围。</p>"
            "<script>"
            "document.getElementById('f').addEventListener('submit',function(e){"
            "e.preventDefault();var u=new URL(location.href);"
            "var k=this.k.value;u.searchParams.set('key',k);"
            "location.href=u.toString();});"
            "</script></body></html>")

    # ---- 令牌端点（授权码 / 刷新）----
    def _issue(self, client_id: str, scope: str, resource: str):
        at, rt = self._rand(32), self._rand(32)
        now = self._now()
        with self._lock:
            self._access[at] = {"client_id": client_id, "scope": scope,
                                "resource": resource, "exp": now + self.access_ttl}
            self._refresh[rt] = {"client_id": client_id, "scope": scope,
                                 "resource": resource, "exp": now + self.refresh_ttl}
        return JSONResponse(
            {"access_token": at, "token_type": "Bearer", "expires_in": self.access_ttl,
             "refresh_token": rt, "scope": scope},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @staticmethod
    def _err(error: str, desc: str, status: int = 400):
        return JSONResponse({"error": error, "error_description": desc},
                            status_code=status,
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    async def token(self, request):
        data = await self._form_or_json(request)
        grant = str(data.get("grant_type", ""))
        if grant == "authorization_code":
            code = str(data.get("code", ""))
            cid = str(data.get("client_id", ""))
            redirect_uri = str(data.get("redirect_uri", ""))
            verifier = str(data.get("code_verifier", ""))
            with self._lock:
                rec = self._codes.pop(code, None)
            if not rec:
                return self._err("invalid_grant", "invalid authorization code")
            if rec["exp"] < self._now():
                return self._err("invalid_grant", "authorization code expired")
            if rec["client_id"] != cid or rec["redirect_uri"] != redirect_uri:
                return self._err("invalid_grant", "client_id/redirect_uri mismatch")
            if not self._pkce_ok(verifier, rec["code_challenge"], rec["method"]):
                return self._err("invalid_grant", "PKCE verification failed")
            return self._issue(rec["client_id"], rec["scope"], rec["resource"])
        if grant == "refresh_token":
            rt = str(data.get("refresh_token", ""))
            with self._lock:
                rec = self._refresh.pop(rt, None)
            if not rec or rec["exp"] < self._now():
                return self._err("invalid_grant", "invalid refresh token")
            return self._issue(rec["client_id"], rec["scope"], rec["resource"])
        return self._err("unsupported_grant_type", grant or "(missing)")


# ==========================================================================
# 启动
# ==========================================================================
def main():
    ap = argparse.ArgumentParser(description="Materials Hub 对外 MCP 网关")
    ap.add_argument("--transport", choices=["stdio", "streamable-http"],
                    default="streamable-http", help="stdio(本机) 或 streamable-http(远程)")
    ap.add_argument("--host", default="127.0.0.1", help="streamable-http 监听地址")
    ap.add_argument("--port", type=int, default=8002, help="streamable-http 监听端口")
    ap.add_argument("--path", default="/mcp", help="streamable-http 挂载路径")
    ap.add_argument("--token", default=None,
                    help="Bearer token；未设则用环境变量 VITUAL_HUB_TOKEN；两者皆空则开放")
    ap.add_argument("--allow-origins", default="*",
                    help="CORS 允许来源(逗号分隔)；默认 *（配合 token 鉴权）")
    ap.add_argument("--public-url", default="",
                    help="对外可达 URL(如 https://mcp.example.com)；用于 OAuth issuer/端点，"
                         "反代或 TLS 场景必填；留空则按请求推导")
    args = ap.parse_args()

    token = (args.token or os.environ.get("VITUAL_HUB_TOKEN") or "").strip()
    # stdio 模式：若环境变量设了 token 但客户端没传 --token，则拒绝启动（防无鉴权调起）
    if args.transport == "stdio" and os.environ.get("VITUAL_HUB_TOKEN", "").strip() and not args.token:
        sys.stderr.write("[materials-hub] VITUAL_HUB_TOKEN is set but MCP client did not pass "
                         "--token; refusing to start.\n")
        sys.exit(2)

    # 启动初始化（语义后端 ollama 顺手自愈，失败不阻塞）
    core.init_hub()
    try:
        st = core.ensure_ollama()
        logger.info("embed %s", ("ok:" + st["model"]) if st.get("ok") else "unavailable")
    except Exception as e:
        logger.warning("ensure_ollama skipped: %s", e)

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    # ---- Streamable HTTP ----
    asgi = mcp.streamable_http_app()
    origins = [o.strip() for o in args.allow_origins.split(",") if o.strip()]
    # OAuth 2.1 授权服务器（设了主令牌时授权码流程才真正启用；未设则整站开放、OAuth 无意义）
    oauth = OAuthServer(master_token=token, issuer=(args.public_url or "").strip(),
                        path=args.path)
    # 中间件顺序：先加鉴权(内层)、后加 CORS(最外层) → 401 也带 CORS 头，OPTIONS 由 CORS 预检短路
    asgi.add_middleware(BearerAuthMiddleware, token=token, oauth=oauth)
    asgi.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    # 轻量健康检查（外部监控用，放行于鉴权中间件）
    async def _healthz(request):
        return JSONResponse({"ok": True, "name": "materials-hub-remote",
                             "tools": len(mcp._tool_manager._tools) if hasattr(mcp, "_tool_manager") else None,
                             "token_required": bool(token)})
    asgi.add_route("/healthz", _healthz)

    # OAuth 2.1 端点
    asgi.add_route("/.well-known/oauth-authorization-server", oauth.metadata, methods=["GET"])
    asgi.add_route("/.well-known/oauth-protected-resource", oauth.protected_resource, methods=["GET"])
    asgi.add_route("/.well-known/openid-configuration", oauth.metadata, methods=["GET"])
    asgi.add_route("/register", oauth.register, methods=["POST"])
    asgi.add_route("/authorize", oauth.authorize, methods=["GET", "POST"])
    asgi.add_route("/token", oauth.token, methods=["POST"])

    logger.info("Materials Hub MCP (streamable-http) → http://%s:%d%s  token_required=%s  oauth=%s",
                args.host, args.port, args.path, bool(token), bool(token))
    uvicorn.run(asgi, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
