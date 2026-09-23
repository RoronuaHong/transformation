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
    ingest_file, ingest_dir, scan_materials, update_tags,
    update_description, remove_material, duplicates, sanitize_name,
    distinct_tags, make_thumb, thumb_path, thumbs_status, purge_thumbs,
    missing_thumbnail_ids, thumb_failure_reason,
)

PORT = 8000

# 批量生成封面的后台任务状态(抽帧耗时,放后台线程 + 前端轮询进度)
_THUMB_JOB = {"running": False, "total": 0, "done": 0, "made": 0, "error": ""}


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
            rows = search(kw, kind, tag) if (kw or tag or kind) else all_materials()
            for m in rows:  # 给视频标注封面是否已就绪,前端据此决定要不要请求 poster
                if m["kind"] == "videos":
                    tp = thumb_path(m["id"])
                    m["thumb"] = os.path.exists(tp) and os.path.getsize(tp) > 0
            return self._json(rows)
        if p == "/api/stats":
            ms = all_materials()
            kinds = {}
            for m in ms:
                kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
            return self._json({"total": len(ms), "dupes": len(duplicates()), "kinds": kinds,
                               "thumbs": thumbs_status(), "thumb_job": dict(_THUMB_JOB)})
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
            else:
                fp = m["rel_path"]
                if not os.path.isabs(fp):
                    fp = os.path.join(HUB, fp)
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
        if p == "/api/remove":
            remove_material(body.get("id"))
            return self._json({"ok": True})
        return self._send(404, b"not found")

    def log_message(self, *a):
        pass


def main():
    init_hub()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Materials Hub running -> http://localhost:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
