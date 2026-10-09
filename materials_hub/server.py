"""Materials Hub — 轻量 Web 服务(零依赖,Python 标准库 http.server)。

运行: python server.py  然后浏览器打开 http://localhost:8000
提供: 上传 / 列表 / 搜索 / 按 kind 筛选 / 打标签 / 编辑描述 / 预览 / 去重整理。
"""
import os
import re
import sys
import json
import time
import mimetypes
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import obs                      # 可观测:结构化日志 + 指标埋点(§8 G 维度)

from core import (
    init_hub, HUB, MATERIALS, get_material, all_materials, search,
    count_materials, ingest_file, ingest_dir, scan_materials,
    update_description, remove_material, duplicates, sanitize_name,
    distinct_tags, tags_for_ui, make_thumb, thumb_path, thumbs_status, purge_thumbs,
    missing_thumbnail_ids, thumb_failure_reason, health, broken_externals,
    prune_broken_externals, external_stats,     build_embeddings, embed_status, chat_models,
    ensure_ollama, external_path_allowed,
    auto_process_all, pending_processing,
    split_all_videos_to_silent, split_video_to_silent_and_audio,
    reclassify_video_audio_kinds, apply_media_facet_tags, link_relation_parents,
    merge_material_tags, is_system_facet_tag,
    read_run_record, read_understand_record, set_reviewed, distribution_readiness,
    deliver_package,
)
from gateway import proxy_target, forward as gateway_forward

PORT = 8000
# 鉴权(可选):设置 VITUAL_HUB_TOKEN 后,所有请求(含面板)都需带 token(Bearer 头或 ?token=),
# 否则返回 401。未设置则保持本地开放(向后兼容)。
HUB_TOKEN = os.environ.get("VITUAL_HUB_TOKEN", "").strip()
# 绑定地址:默认只听本机 127.0.0.1;要跨机访问再设 VITUAL_HUB_HOST=0.0.0.0 且务必同时设 token。
HUB_HOST = os.environ.get("VITUAL_HUB_HOST", "127.0.0.1").strip()


def _authorized(handler):
    if not HUB_TOKEN:
        return True
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        if auth[len("Bearer "):].strip() == HUB_TOKEN:
            return True
    tok = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query).get("token", [""])[0]
    return tok == HUB_TOKEN


def _safe_resolve(base, rel):
    """防 ../ 穿越:把 base+rel 解析为真实路径,确保仍落在 base 内;否则返回 None。"""
    base = os.path.realpath(base)
    target = (os.path.realpath(os.path.join(base, rel))
              if not os.path.isabs(rel) else os.path.realpath(rel))
    if target == base or target.startswith(base + os.sep):
        return target
    return None

# 批量生成封面的后台任务状态(抽帧耗时,放后台线程 + 前端轮询进度)
_THUMB_JOB = {"running": False, "total": 0, "done": 0, "made": 0, "error": ""}
# 语义索引构建任务状态(同样放后台,避免长请求把浏览器挂住)
_EMBED_JOB = {"running": False, "total": 0, "done": 0, "embedded": 0, "error": ""}
# Deep Agent 任务状态(7B 本地模型一次任务要跑几分钟,必须后台线程+轮询)
_AGENT_JOB = {"running": False, "task": "", "task_id": "", "status": "", "error": ""}

# 上传/入库/扫描后自动处理队列(事件驱动,免去手动点按钮)
_AUTOPROC_JOB = {"running": False, "processed": 0, "error": "", "kicked_by": ""}


def _run_autoproc_job(autotag=False, kicked_by=""):
    try:
        r = auto_process_all(limit=0, autotag=autotag)
        _AUTOPROC_JOB["processed"] = r.get("processed", 0)
        _AUTOPROC_JOB["kicked_by"] = kicked_by
    except Exception as e:  # noqa: BLE001  # 后台线程异常不能让进程挂掉
        _AUTOPROC_JOB["error"] = str(e)
    finally:
        _AUTOPROC_JOB["running"] = False


def enqueue_autoproc(autotag=False, kicked_by=""):
    """入库(上传/整理/扫描)后调用:有素材待处理则后台跑一遍自动处理链。
    已在跑则跳过——单次全量 pass 会覆盖新入库素材(幂等);无待处理项则直接返回。"""
    if _AUTOPROC_JOB["running"]:
        return
    if not any(pending_processing(m["id"]).values()
               for m in all_materials()
               if m.get("kind") in ("videos", "silent", "images")):
        return
    _AUTOPROC_JOB["running"] = True
    _AUTOPROC_JOB["error"] = ""
    _AUTOPROC_JOB["kicked_by"] = kicked_by
    threading.Thread(target=_run_autoproc_job, args=(autotag, kicked_by),
                     daemon=True).start()


def _thread_context(thread_id):
    """同一对话线程的近期发言,注入下一轮 Deep Agent(短时记忆,不进 checkpoint 压缩)。"""
    tid = str(thread_id or "").strip()
    if not tid:
        return ""
    import agent as _agent_mod
    th = _agent_mod.thread_get(tid)
    if th.get("error"):
        return ""
    lines = []
    for m in (th.get("messages") or [])[-6:]:
        role = m.get("role")
        content = str(m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            lines.append("%s: %s" % (role, content[:500]))
    return "\n".join(lines)[:2500]


def _run_agent_job(task, allow_write, max_steps, task_id, thread_context=""):
    import agent                                  # 延迟导入:agent 依赖 ollama 可用性
    try:
        r = agent.agent_run(task, allow_write=allow_write, max_steps=max_steps,
                            task_id=task_id, thread_context=thread_context or "")
        _AGENT_JOB.update(running=False, status=r.get("status", "done"),
                          task_id=r.get("task_id", task_id), error=r.get("summary", "")[:200])
    except Exception as e:                        # noqa: BLE001
        _AGENT_JOB.update(running=False, status="error", task_id=task_id,
                          error="%s: %s" % (type(e).__name__, e))


def _run_embed_job(force=False, limit=0):
    try:
        def prog(done, total):
            _EMBED_JOB["done"], _EMBED_JOB["total"] = done, total
        r = build_embeddings(force=force, limit=limit, progress=prog)
        _EMBED_JOB["embedded"] = r.get("embedded", 0)
        if not r.get("available"):
            _EMBED_JOB["error"] = "embed-unavailable"
    except Exception as e:
        _EMBED_JOB["error"] = str(e)
    finally:
        _EMBED_JOB["running"] = False


def _run_thumb_job(limit=0):
    try:
        ids = missing_thumbnail_ids()
        if limit:
            ids = ids[:limit]
        _THUMB_JOB.update(total=len(ids), done=0, made=0)
        for i, mid in enumerate(ids, 1):
            if make_thumb(mid):
                _THUMB_JOB["made"] += 1
            _THUMB_JOB["done"] = i
    except Exception as e:  # 后台线程异常不能让进程挂掉
        _THUMB_JOB["error"] = str(e)
    finally:
        _THUMB_JOB["running"] = False


def parse_multipart(raw, boundary):
    """手写 multipart 解析,避免依赖已废弃的 cgi 模块。"""
    files = {}
    parts = raw.split(b"--" + boundary)
    for part in parts:
        if b"filename=" not in part:
            continue
        header, _, content = part.partition(b"\r\n\r\n")
        if content.endswith(b"\r\n"):
            content = content[:-2]
        fn = re.search(rb'filename="([^"]*)"', header)
        nm = re.search(rb'name="([^"]*)"', header)
        if fn:
            key = nm.group(1).decode() if nm else "file"
            files[key] = (fn.group(1).decode("utf-8", "ignore"), content)
    return files


def _cors_headers(handler):
    """允许本机 Next(:3000) 直传大文件到 hub(:8000),绕过 rewrite 体积上限。"""
    origin = (handler.headers.get("Origin") or "").strip()
    if not origin:
        return
    if origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers",
                            "Content-Type, Authorization")
        handler.send_header("Access-Control-Max-Age", "86400")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        _cors_headers(self)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_OPTIONS(self):
        # 预检:浏览器跨域直传 multipart 到 :8000
        self.send_response(204)
        _cors_headers(self)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_static(self, name, ctype=None):
        fp = os.path.join(HUB, "static", name)
        if not os.path.exists(fp):
            return self._send(404, b"not found")
        with open(fp, "rb") as f:
            data = f.read()
        # 静态文件禁缓存协商:面板迭代频繁,避免浏览器拿旧 html/js 造成"改了没生效"假象
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(fp)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        _cors_headers(self)
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, fp, ctype, head_only=False):
        """发送文件,支持 HTTP Range(大视频可拖拽/边下边播);流式写出避免整文件进内存。"""
        size = os.path.getsize(fp)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            if "-" in spec:
                s, e = spec.split("-", 1)
                try:
                    start = int(s) if s else 0
                    end = int(e) if e else size - 1
                except ValueError:
                    start, end = 0, size - 1
                if start < 0:
                    start = 0
                if end >= size:
                    end = size - 1
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    _cors_headers(self)
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        # 媒体短缓存:利于拖拽 seek 复用已缓冲段,又避免永久脏缓存
        self.send_header("Cache-Control", "public, max-age=3600")
        _cors_headers(self)
        self.end_headers()
        if head_only:
            return
        with open(fp, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                left -= len(chunk)

    def do_HEAD(self):
        """浏览器探测媒体时常发 HEAD;缺省 501 会导致部分环境无法 seek。"""
        if not _authorized(self):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            _cors_headers(self)
            self.end_headers()
            return
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p.startswith("/api/file/"):
            mid = p[len("/api/file/"):]
            m = get_material(mid)
            if not m:
                return self._send(404, b"not found")
            if m.get("location") == "external":
                fp = m["external_path"]
                if not external_path_allowed(fp):
                    return self._send(403, b"forbidden")
            else:
                fp = _safe_resolve(HUB, m["rel_path"])
                if not fp:
                    return self._send(403, b"forbidden")
            if not os.path.exists(fp):
                return self._send(404, b"missing")
            mt = mimetypes.guess_type(fp)[0]
            ctype = {
                "images": mt or "image/*",
                "videos": mt or "video/*",
                "silent": mt or "video/*",
                "anim": mt or "image/gif",
                "audio": mt or "audio/*",
            }.get(m["kind"], mt or "application/octet-stream")
            return self._serve_file(fp, ctype, head_only=True)
        if p.startswith("/api/thumb/"):
            mid = p[len("/api/thumb/"):]
            fp = thumb_path(mid)
            if not (fp and os.path.isfile(fp) and os.path.getsize(fp) > 0):
                return self._send(404, b"not found")
            return self._serve_file(fp, "image/jpeg", head_only=True)
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _maybe_gateway(self):
        """Same-origin routes for transform workbench + ops-api (no iframe)."""
        u = urllib.parse.urlparse(self.path)
        hit = proxy_target(u.path)
        if not hit:
            return False
        origin, up_path = hit
        gateway_forward(self, origin, up_path)
        return True

    def do_GET(self):
        if not _authorized(self):
            return self._send(401, json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if self._maybe_gateway():
            return
        u = urllib.parse.urlparse(self.path)
        p = u.path
        # 兼容直连 :8000 的 /hub-api/* 调用(绕过 transform 代理重写时),剥前缀统一走 /api/*
        if p.startswith("/hub-api/"):
            p = "/api/" + p[len("/hub-api/"):]
        elif p == "/hub-api":
            p = "/api"
        if p in ("/", "/index.html"):
            return self._serve_static("index.html", "text/html; charset=utf-8")
        if p.startswith("/static/"):
            return self._serve_static(p[len("/static/"):])
        if p in ("/api/list", "/api/materials", "/api/search"):
            q = urllib.parse.parse_qs(u.query)
            kind = q.get("kind", [""])[0]
            tag = q.get("tag", [""])[0]
            kw = q.get("q", [""])[0]
            lim = q.get("limit", [""])[0]
            off = q.get("offset", [""])[0]
            mode = q.get("mode", ["auto"])[0]
            sort = q.get("sort", [""])[0]
            limit = int(lim) if lim.isdigit() else None   # 不传 limit = 不分页
            offset = int(off) if off.isdigit() else 0
            rows = search(kw, kind, tag, limit=limit, offset=offset, mode=mode,
                          sort=sort if sort in ("newest", "oldest", "name", "size") else "")
            for m in rows:
                # 给视频标注封面是否已就绪,前端据此决定要不要请求 poster
                if m["kind"] in ("videos", "silent"):
                    tp = thumb_path(m["id"])
                    m["thumb"] = os.path.exists(tp) and os.path.getsize(tp) > 0
                # 外部引用标注原文件是否还在(缺失则卡片提示,不再发起必然 404 的请求)
                if m.get("location") == "external":
                    m["missing"] = not (m.get("external_path")
                                        and os.path.exists(m["external_path"]))
            return self._json(rows)
        if p == "/api/count":
            q = urllib.parse.parse_qs(u.query)
            return self._json({"total": count_materials(q.get("q", [""])[0],
                                                         q.get("kind", [""])[0],
                                                         q.get("tag", [""])[0],
                                                         q.get("mode", ["auto"])[0])})
        if p == "/api/stats":
            ms = all_materials()
            kinds = {}
            for m in ms:
                kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
            # 自动处理链待处理数(videos/silent/images 中还有缺项的素材条数)
            pending = sum(1 for m in ms
                          if m.get("kind") in ("videos", "silent", "images")
                          and any(pending_processing(m["id"]).values()))
            return self._json({"total": len(ms), "dupes": len(duplicates()), "kinds": kinds,
                               "thumbs": thumbs_status(), "thumb_job": dict(_THUMB_JOB),
                               "external": external_stats(),
                               "embed": {**embed_status(), "job": dict(_EMBED_JOB)},
                               "auto": {"pending": pending}})
        if p == "/api/agent":
            # Deep Agent 状态:?task_id= 始终读 checkpoint(每步落盘的 todos/progress)。
            # 任务进行中也要带回这些字段,否则前端轮询只能看到 running,进度条不走。
            qs = urllib.parse.parse_qs(u.query)
            tid = (qs.get("task_id") or [""])[0]
            if tid:
                import agent
                st = agent.agent_status(tid)
                st["job"] = dict(_AGENT_JOB)
                st["running"] = bool(
                    _AGENT_JOB["running"] and tid == _AGENT_JOB["task_id"])
                return self._json(st)
            if _AGENT_JOB["running"]:
                return self._json({"running": True, "job": dict(_AGENT_JOB)})
            return self._json({"running": False, "job": dict(_AGENT_JOB)})

        if p == "/api/agent/caps":
            # Agent 能力/前置检测:本机是否有可用的 chat 模型(无则 Agent 会 skipped)
            cms = chat_models()
            return self._json({"chat_models": cms, "model_ready": bool(cms),
                               "model": cms[0] if cms else ""})

        if p == "/api/agent/threads":
            # Deep Agent 对话线程:无 id → 列表;?id=th… → 完整 messages
            import agent as _agent_mod
            qs = urllib.parse.parse_qs(u.query)
            tid = (qs.get("id") or qs.get("thread_id") or [""])[0].strip()
            if tid:
                return self._json(_agent_mod.thread_get(tid))
            limit = int((qs.get("limit") or ["40"])[0] or 40)
            return self._json({"threads": _agent_mod.thread_list(limit)})

        if p == "/api/auto/status":
            # 后台自动处理任务状态(上传/入库事件驱动触发,可轮询)
            return self._json(dict(_AUTOPROC_JOB))

        if p == "/api/health":
            h = health()
            # 告警(P1-7):失效外链超阈值时附 alerts,便于监控轮询告警。
            # 阈值用 VITUAL_ALERT_BROKEN 设(0=不告警);默认不改动 health() 本身结构。
            try:
                thr = int(os.environ.get("VITUAL_ALERT_BROKEN", "0") or 0)
                if thr > 0:
                    n_broken = len(broken_externals())
                    if n_broken > thr:
                        h["alerts"] = [{"level": "warn", "code": "broken_externals",
                                        "count": n_broken, "threshold": thr}]
            except Exception:  # noqa: BLE001
                pass
            return self._json(h)
        if p == "/api/metrics":
            # 指标埋点(§8 G 维度):检索/HTTP/MCP 的调用数、失败率、零命中率、耗时分位。
            # live = 本进程内存(实时);logs_today = 今日落盘日志聚合(跨进程,首个请求也有数据)。
            return self._json({"metrics": obs.snapshot(),
                               "logs_today": obs.aggregate_from_logs(days=1)})
        if p == "/api/broken":
            return self._json([{"id": m["id"], "name": m["name"], "kind": m["kind"],
                                "source": m.get("source", ""),
                                "external_path": m.get("external_path", "")}
                               for m in broken_externals()])
        if p == "/api/dupes":
            return self._json(duplicates())
        if p.startswith("/api/readiness/"):
            # 步骤 5/7:某 job 分发渠道就绪度(channel_ready + 命名空间化 blocking)
            return self._json(distribution_readiness(p[len("/api/readiness/"):]))
        if p.startswith("/api/run/"):
            # 步骤 1/12:入库链逐步状态。JSON 的键是稳定英文键(thumb/tech/describe/ocr/
            # visual/shots/phash/asr),由界面按语言解释,避免 16 套流程文案。
            return self._json(read_run_record(p[len("/api/run/"):]) or {})
        if p.startswith("/api/understand/"):
            # 步骤 4/5:结构化理解记录(speech/visual_zh/visual_en/quality/reviewed)。
            # 派生数据,只读;绝不进 description 主排序字段。
            return self._json(read_understand_record(p[len("/api/understand/"):]) or {})
        if p == "/api/deliver_file":
            # 步骤 6:交付产物下载(沙箱在 index/agent_workspace 内)
            qp = urllib.parse.parse_qs(u.query)
            rawp = (qp.get("path") or [""])[0]
            if not rawp:
                return self._send(400, b"missing path")
            fp = os.path.realpath(urllib.parse.unquote(rawp))
            root = os.path.realpath(os.path.join(HUB, "index", "agent_workspace"))
            if fp != root and not fp.startswith(root + os.sep):
                return self._send(403, b"forbidden")
            if not os.path.isfile(fp):
                return self._send(404, b"missing")
            mt = mimetypes.guess_type(fp)[0] or "application/octet-stream"
            return self._serve_file(fp, mt, head_only=False)
        if p == "/api/tags":
            qs = urllib.parse.parse_qs(u.query)
            lang = (qs.get("lang") or ["zh"])[0]
            ui = (qs.get("ui") or ["1"])[0] not in ("0", "false", "no")
            if ui:
                return self._json(tags_for_ui(lang=lang))
            return self._json([{"tag": t, "count": c} for t, c in distinct_tags()])
        if p == "/api/export":
            fmt = urllib.parse.parse_qs(u.query).get("fmt", ["json"])[0]
            ms = all_materials()
            if fmt == "csv":
                import csv as _csv
                import io as _io
                cols = ["id", "kind", "ext", "name", "size", "sha256", "tags",
                        "ai_tags", "description", "source", "orig_name", "location",
                        "external_path", "created_at"]
                buf = _io.StringIO()
                w = _csv.writer(buf)
                w.writerow(cols)
                for m in ms:
                    w.writerow([m.get(c, "") for c in cols])
                body = buf.getvalue().encode("utf-8-sig")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 'attachment; filename="hub_export.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = json.dumps(ms, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="hub_export.json"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p.startswith("/api/thumb/"):
            mid = p[len("/api/thumb/"):]
            fp = thumb_path(mid)
            if not (os.path.exists(fp) and os.path.getsize(fp) > 0):
                if not thumbs_status()["ffmpeg"]:
                    return self._json({"error": "ffmpeg-unavailable",
                                       "hint": "本机未找到 ffmpeg,前端已回退到浏览器截帧"}, 404)
                fp = make_thumb(mid)
            if not fp or not os.path.exists(fp):
                return self._json({"error": "thumbnail-failed", "id": mid,
                                   "reason": thumb_failure_reason(mid)}, 404)
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if p.startswith("/api/file/"):
            mid = p[len("/api/file/"):]
            m = get_material(mid)
            if not m:
                return self._send(404, b"not found")
            if m.get("location") == "external":
                fp = m["external_path"]
                if not external_path_allowed(fp):
                    return self._send(403, b"forbidden")
            else:
                fp = _safe_resolve(HUB, m["rel_path"])
                if not fp:
                    return self._send(403, b"forbidden")
            if not os.path.exists(fp):
                return self._send(404, b"missing")
            mt = mimetypes.guess_type(fp)[0]
            ctype = {
                "images": mt or "image/*",
                "videos": mt or "video/*",
                "silent": mt or "video/*",
                "anim": mt or "image/gif",
                "audio": mt or "audio/*",
                "docs": "application/octet-stream",
                "subs": "application/octet-stream",
                "other": "application/octet-stream",
            }.get(m["kind"], mt or "application/octet-stream")
            return self._serve_file(fp, ctype)
        return self._send(404, b"not found")

    def do_POST(self):
        if not _authorized(self):
            return self._send(401, json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if self._maybe_gateway():
            return
        u = urllib.parse.urlparse(self.path)
        p = u.path
        # 兼容直连 :8000 的 /hub-api/* 调用(绕过 transform 代理重写时),剥前缀统一走 /api/*
        if p.startswith("/hub-api/"):
            p = "/api/" + p[len("/hub-api/"):]
        elif p == "/hub-api":
            p = "/api"
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        if p == "/api/upload":
            ctype = self.headers.get("Content-Type", "")
            bm = re.search(r"boundary=([^;]+)", ctype)
            if not bm:
                return self._json({"error": "no boundary"}, 400)
            boundary = bm.group(1).strip().strip('"').encode()
            try:
                files = parse_multipart(raw, boundary)
                res = None
                added = False
                for _, (filename, content) in files.items():
                    if not filename:
                        continue
                    os.makedirs(os.path.join(HUB, "ingest"), exist_ok=True)
                    tmp = os.path.join(HUB, "ingest", sanitize_name(filename))
                    with open(tmp, "wb") as o:
                        o.write(content)
                    r = ingest_file(tmp, move=True)
                    if r and r.get("status") == "added":
                        added = True
                    res = r or res
                if added:
                    enqueue_autoproc(kicked_by="upload")
                return self._json(res or {"status": "empty"})
            except Exception as e:  # noqa: BLE001
                return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

        if p == "/api/ingest":
            r = ingest_dir(os.path.join(HUB, "ingest"))
            if r:
                enqueue_autoproc(kicked_by="ingest")
            return self._json({"ingested": len(r)})
        if p == "/api/scan":
            r = scan_materials()
            if r:
                enqueue_autoproc(kicked_by="scan")
            return self._json({"new": len(r)})
        if p == "/api/split-silent":
            # 库内有声视频 → 无声画面 + 音轨（DAM：picture / silent picture / stem）
            try:
                opt = json.loads(raw or b"{}")
            except Exception:
                opt = {}
            mid = (opt.get("id") or "").strip()
            force = bool(opt.get("force"))
            if mid:
                r = split_video_to_silent_and_audio(mid, force=force)
                if r.get("status") in ("ok", "reclassified") or r.get("silent_status") == "added":
                    enqueue_autoproc(kicked_by="split-silent")
                return self._json(r)
            r = split_all_videos_to_silent(force=force, limit=int(opt.get("limit") or 0))
            if r.get("ok"):
                enqueue_autoproc(kicked_by="split-silent")
            return self._json(r)
        if p == "/api/reclassify-media":
            return self._json({
                "reclassify": reclassify_video_audio_kinds(),
                "facets": apply_media_facet_tags(),
                "link_parents": link_relation_parents(),
            })

        if p == "/api/thumbs":
            # {purge:true} 清空封面缓存;否则后台批量抽帧(limit 可限个数)
            try:
                opt = json.loads(raw or b"{}")
            except Exception:
                opt = {}
            if opt.get("purge"):
                return self._json({"purged": purge_thumbs()})
            if _THUMB_JOB["running"]:
                return self._json({"running": True, "job": dict(_THUMB_JOB)})
            if not thumbs_status()["ffmpeg"]:
                return self._json({"ffmpeg": False,
                                   "hint": "本机未安装 ffmpeg;前端已用浏览器 canvas 截帧代替"})
            _THUMB_JOB.update(running=True, total=0, done=0, made=0, error="")
            threading.Thread(target=_run_thumb_job, args=(int(opt.get("limit") or 0),),
                             daemon=True).start()
            return self._json({"running": True, "job": dict(_THUMB_JOB)})

        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        if p == "/api/tag":
            # 安全合并:保留系统面标签(role:/job:/parent:/type:/sp/…),仅替换内容标签。
            # 旧 update_tags 是整体替换,经面板回传会抹掉关系链标签(与 merge 契约冲突)。
            mid = str(body.get("id") or "").strip()
            m = get_material(mid)
            if not m:
                return self._json({"ok": False, "error": "not found: " + mid})
            provided = [t.strip() for t in str(body.get("tags", "")).split(",") if t.strip()]
            cur = [t.strip() for t in (m.get("tags") or "").split(",") if t.strip()]
            cur_content = [t for t in cur if not is_system_facet_tag(t)]
            remove = [t for t in cur_content if t not in provided]
            r = merge_material_tags(mid, tags=",".join(provided), remove=remove)
            return self._json(r)
        if p == "/api/describe":
            update_description(body.get("id"), body.get("description", ""))
            return self._json({"ok": True})
        if p == "/api/review":
            # 步骤 5:只有人确认后才置复核(默认 false)。写操作需 confirm,
            # 且只落理解记录 sidecar,不进主排序。
            if not body.get("confirm"):
                return self._json({"ok": False, "error": "confirm required"})
            _mid = str(body.get("id") or "").strip()
            if not get_material(_mid):
                return self._json({"ok": False, "error": "not found: " + _mid})
            return self._json(set_reviewed(_mid, bool(body.get("value", True))))
        if p == "/api/deliver":
            # 步骤 6:按需交付(只读计划 / 确认后写文件)。confirm 护栏:未确认只给计划。
            ids = [str(x).strip() for x in (body.get("ids") or []) if str(x).strip()]
            lang = (body.get("lang") or "").strip() or None
            if not ids:
                return self._json({"error": "ids required"}, 400)
            # 点名语种:把该 job 下同 lang 的字幕文件一起交付(复制)
            speech_lang = lang
            if lang:
                job_tag = None
                for mid in ids:
                    mm = get_material(mid)
                    if mm:
                        for t in (mm.get("tags") or "").split(","):
                            if t.startswith("job:"):
                                job_tag = t
                                break
                    if job_tag:
                        break
                if job_tag:
                    subs = search("", tag="%s,lang:%s" % (job_tag, lang), limit=20)
                    for s in subs:
                        if s.get("kind") == "subs" and s["id"] not in ids:
                            ids.append(s["id"])
            res = deliver_package(
                ids=ids, confirm=bool(body.get("confirm")),
                fmt=str(body.get("fmt") or "mp4"),
                res=str(body.get("res") or "720"),
                copy_only=bool(body.get("copy_only")),
            )
            # 把绝对路径转成可下载 URL(沙箱内)
            for ent in (res.get("written") or []):
                if ent.get("dst"):
                    ent["url"] = "/api/deliver_file?path=" + urllib.parse.quote(ent["dst"])
            for ent in (res.get("plan") or []):
                if ent.get("dst"):
                    ent["url"] = "/api/deliver_file?path=" + urllib.parse.quote(ent["dst"])
            res["speech_lang"] = speech_lang
            return self._json(res)
        if p == "/api/embed":
            # {} 增量构建语义索引;{"force":true} 全量重建;{"limit":N} 限量
            try:
                opt = json.loads(raw or b"{}")
            except Exception:
                opt = {}
            info = embed_status()
            if not info["available"]:
                return self._json({"available": False, "err": info.get("err", ""),
                                   "hint": "未发现本地 embedding 模型;" 
                                           "ollama pull nomic-embed-text 或用 VITUAL_EMBED_MODEL 指定"})
            if _EMBED_JOB["running"]:
                return self._json({"running": True, "job": dict(_EMBED_JOB)})
            _EMBED_JOB.update(running=True, total=0, done=0, embedded=0, error="")
            threading.Thread(target=_run_embed_job,
                             args=(bool(opt.get("force")), int(opt.get("limit") or 0)),
                             daemon=True).start()
            return self._json({"running": True, "job": dict(_EMBED_JOB),
                               "model": info["model"]})

        if p == "/api/auto":
            # 手动触发全量自动处理链;与上传/入库的后台任务共用 _AUTOPROC_JOB 锁,避免并发
            if _AUTOPROC_JOB["running"]:
                return self._json({"running": True, "job": dict(_AUTOPROC_JOB)})
            r = auto_process_all(limit=int(body.get("limit") or 0),
                                 autotag=bool(body.get("autotag")))
            return self._json(r)

        if p == "/api/agent":
            # Deep Agent:{"task":str,"allow_write":bool(默认 False),"max_steps":int(默认 12)}
            # 后台线程执行,前端轮询 GET /api/agent?task_id=xxx 取 todos/summary
            try:
                opt = json.loads(raw or b"{}")
            except Exception:
                opt = {}
            task = str(opt.get("task") or "").strip()
            if not task:
                return self._json({"error": "task required"})
            if _AGENT_JOB["running"]:
                return self._json({"running": True, "job": dict(_AGENT_JOB)})
            import agent as _agent_mod
            tid = _agent_mod._new_id(task)        # 预生成:前端立刻能轮询
            _AGENT_JOB.update(running=True, task=task, task_id=tid,
                              status="planning", error="")
            threading.Thread(
                target=_run_agent_job,
                args=(task, bool(opt.get("allow_write")),
                      max(3, min(int(opt.get("max_steps") or 12), 20)), tid,
                      _thread_context(opt.get("thread_id") or opt.get("thread"))),
                daemon=True).start()
            return self._json({"running": True, "task_id": tid, "job": dict(_AGENT_JOB)})

        if p == "/api/agent/cancel":
            # 协作式取消:写 cancel.flag,主循环步边界终止
            try:
                opt = json.loads(raw or b"{}")
            except Exception:
                opt = {}
            tid = str(opt.get("task_id") or "").strip()
            if not tid:
                return self._json({"error": "task_id required"})
            import agent as _agent_mod
            return self._json(_agent_mod.agent_cancel(tid))

        if p == "/api/agent/threads":
            # 保存/删除对话线程(与 agent_workspace checkpoint 解耦)
            try:
                opt = json.loads(raw or b"{}")
            except Exception:
                opt = {}
            import agent as _agent_mod
            action = str(opt.get("action") or "save").strip().lower()
            if action in ("delete", "remove"):
                tid = str(opt.get("thread_id") or opt.get("id") or "").strip()
                if not tid:
                    return self._json({"error": "thread_id required"})
                return self._json(_agent_mod.thread_delete(tid))
            return self._json(_agent_mod.thread_save(
                thread_id=opt.get("thread_id") or opt.get("id"),
                title=str(opt.get("title") or ""),
                messages=opt.get("messages"),
                task_ids=opt.get("task_ids"),
            ))

        if p == "/api/remove":
            remove_material(body.get("id"))
            return self._json({"ok": True})
        if p == "/api/prune":
            # 清理失效外部引用的索引(源文件已消失);只删索引,不动磁盘
            bad = prune_broken_externals()
            return self._json({"pruned": len(bad),
                               "items": [m["name"] for m in bad[:50]]})
        return self._send(404, b"not found")

    def do_PUT(self):
        if not _authorized(self):
            return self._send(401, json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if self._maybe_gateway():
            return
        return self._send(404, b"not found")

    def do_DELETE(self):
        if not _authorized(self):
            return self._send(401, json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if self._maybe_gateway():
            return
        return self._send(404, b"not found")

    def do_PATCH(self):
        if not _authorized(self):
            return self._send(401, json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if self._maybe_gateway():
            return
        return self._send(404, b"not found")

    def setup(self):
        """为每个连接打上 request id 与起始时间(供结构化日志算耗时)。"""
        super().setup()
        self._t0 = time.perf_counter()
        self._rid = obs.new_id()

    def log_message(self, fmt, *a):
        """改成结构化日志:带 rid / 方法 / 路径 / 状态码 / 耗时。

        基类在每请求处理后调用本方法,args 形如 ('"GET /api/x HTTP/1.1"', '200', '-')。
        默认静默(不刷 stderr),设 VITUAL_LOG_STDERR=1 才回显,避免污染 MCP/管道输出。
        """
        try:
            status = 0
            if len(a) >= 2:
                try:
                    status = int(str(a[1]).split()[0])
                except (ValueError, IndexError):
                    status = 0
            ms = (time.perf_counter() - getattr(self, "_t0", time.perf_counter())) * 1000.0
            path = (self.path or "").split("?")[0]
            obs.record("http", duration_ms=ms, ok=(status < 400),
                       rid=getattr(self, "_rid", "-"), method=self.command,
                       path=path, status=status)
            if os.environ.get("VITUAL_LOG_STDERR"):
                sys.stderr.write("[hub] %s %s %s %.1fms rid=%s\n"
                                 % (self.command, path, status, ms,
                                    getattr(self, "_rid", "-")))
        except Exception:  # noqa: BLE001
            pass


def main():
    st = ensure_ollama()          # 语义检索依赖;没起就自动拉起,失败回退词法
    if st["ok"]:
        print(f"ollama ready -> {st['model']}")
    else:
        print("ollama unavailable -> semantic search falls back to lexical"
              + (f" ({st['err']})" if st.get("err") else ""))
    init_hub()
    srv = ThreadingHTTPServer((HUB_HOST, PORT), Handler)
    auth = " (token required: VITUAL_HUB_TOKEN set)" if HUB_TOKEN else " (open, no token)"
    print(f"Materials Hub API -> http://{HUB_HOST}:{PORT}/api/*{auth}")
    print(f"  UI moved to transform: http://127.0.0.1:3000/zh/hub  (proxy /hub-api → this server)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
