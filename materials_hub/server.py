"""Materials Hub — 轻量 Web 服务(零依赖,Python 标准库 http.server)。

运行: python server.py  然后浏览器打开 http://localhost:8000
提供: 上传 / 列表 / 搜索 / 按 kind 筛选 / 打标签 / 编辑描述 / 预览 / 去重整理。
"""
import os
import re
import json
import mimetypes
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import (
    init_hub, HUB, MATERIALS, get_material, all_materials, search,
    count_materials, ingest_file, ingest_dir, scan_materials, update_tags,
    update_description, remove_material, duplicates, sanitize_name,
    distinct_tags, make_thumb, thumb_path, thumbs_status, purge_thumbs,
    missing_thumbnail_ids, thumb_failure_reason, health, broken_externals,
    prune_broken_externals, external_stats, build_embeddings, embed_status,
    ensure_ollama, external_path_allowed,
)

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


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _serve_static(self, name, ctype=None):
        fp = os.path.join(HUB, "static", name)
        if not os.path.exists(fp):
            return self._send(404, b"not found")
        with open(fp, "rb") as f:
            data = f.read()
        return self._send(200, data, ctype or mimetypes.guess_type(fp)[0] or "application/octet-stream")

    def _serve_file(self, fp, ctype):
        """发送文件,支持 HTTP Range(大视频可拖拽/边下边播)。"""
        size = os.path.getsize(fp)
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            if "-" in spec:
                s, e = spec.split("-", 1)
                start = int(s) if s else 0
                end = int(e) if e else size - 1
                if end >= size:
                    end = size - 1
                length = end - start + 1
                with open(fp, "rb") as f:
                    f.seek(start)
                    data = f.read(length)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                self.wfile.write(data)
                return
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not _authorized(self):
            return self._send(401, json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            return self._serve_static("index.html", "text/html; charset=utf-8")
        if p.startswith("/static/"):
            return self._serve_static(p[len("/static/"):])
        if p == "/api/list":
            q = urllib.parse.parse_qs(u.query)
            kind = q.get("kind", [""])[0]
            tag = q.get("tag", [""])[0]
            kw = q.get("q", [""])[0]
            lim = q.get("limit", [""])[0]
            off = q.get("offset", [""])[0]
            mode = q.get("mode", ["auto"])[0]
            limit = int(lim) if lim.isdigit() else None   # 不传 limit = 不分页
            offset = int(off) if off.isdigit() else 0
            rows = search(kw, kind, tag, limit=limit, offset=offset, mode=mode)
            for m in rows:
                # 给视频标注封面是否已就绪,前端据此决定要不要请求 poster
                if m["kind"] == "videos":
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
            return self._json({"total": len(ms), "dupes": len(duplicates()), "kinds": kinds,
                               "thumbs": thumbs_status(), "thumb_job": dict(_THUMB_JOB),
                               "external": external_stats(),
                               "embed": {**embed_status(), "job": dict(_EMBED_JOB)}})
        if p == "/api/health":
            return self._json(health())
        if p == "/api/broken":
            return self._json([{"id": m["id"], "name": m["name"], "kind": m["kind"],
                                "source": m.get("source", ""),
                                "external_path": m.get("external_path", "")}
                               for m in broken_externals()])
        if p == "/api/dupes":
            return self._json(duplicates())
        if p == "/api/tags":
            return self._json([{"tag": t, "count": c} for t, c in distinct_tags()])
        if p == "/api/export":
            fmt = urllib.parse.parse_qs(u.query).get("fmt", ["json"])[0]
            ms = all_materials()
            if fmt == "csv":
                import csv as _csv
                import io as _io
                cols = ["id", "kind", "ext", "name", "size", "sha256", "tags",
                        "description", "source", "orig_name", "location",
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
                "images": mt or "image/*", "videos": mt or "video/*",
                "audio": mt or "audio/*",
                "docs": "application/octet-stream", "subs": "application/octet-stream",
                "other": "application/octet-stream",
            }[m["kind"]]
            return self._serve_file(fp, ctype)
        return self._send(404, b"not found")

    def do_POST(self):
        if not _authorized(self):
            return self._send(401, json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        u = urllib.parse.urlparse(self.path)
        p = u.path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        if p == "/api/upload":
            ctype = self.headers.get("Content-Type", "")
            bm = re.search(r"boundary=([^;]+)", ctype)
            if not bm:
                return self._send(400, b"no boundary")
            files = parse_multipart(raw, bm.group(1).strip().encode())
            res = None
            for _, (filename, content) in files.items():
                if not filename:
                    continue
                os.makedirs(INGEST if False else os.path.join(HUB, "ingest"), exist_ok=True)
                tmp = os.path.join(HUB, "ingest", sanitize_name(filename))
                with open(tmp, "wb") as o:
                    o.write(content)
                res = ingest_file(tmp, move=True)
            return self._json(res or {"status": "empty"})

        if p == "/api/ingest":
            r = ingest_dir(os.path.join(HUB, "ingest"))
            return self._json({"ingested": len(r)})
        if p == "/api/scan":
            r = scan_materials()
            return self._json({"new": len(r)})

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
            update_tags(body.get("id"), body.get("tags", ""))
            return self._json({"ok": True})
        if p == "/api/describe":
            update_description(body.get("id"), body.get("description", ""))
            return self._json({"ok": True})
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

        if p == "/api/remove":
            remove_material(body.get("id"))
            return self._json({"ok": True})
        if p == "/api/prune":
            # 清理失效外部引用的索引(源文件已消失);只删索引,不动磁盘
            bad = prune_broken_externals()
            return self._json({"pruned": len(bad),
                               "items": [m["name"] for m in bad[:50]]})
        return self._send(404, b"not found")

    def log_message(self, *a):
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
    print(f"Materials Hub running -> http://{HUB_HOST}:{PORT}{auth}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
