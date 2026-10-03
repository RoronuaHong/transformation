"""Same-origin gateway: materials_hub (:8000) proxies transform + ops-api.

Why not \"paste crop as a route\": the clip/workbench UI is the Next.js app
(transform/), not a standalone HTML page. Hub stays the front door; /zh etc.
and /ops-api are reverse-proxied so the browser never iframes another origin.
"""
from __future__ import annotations

import http.client
import os
import urllib.parse

# Match transform/lib/locales.ts
TRANSFORM_LOCALES = frozenset({
    "zh", "en", "ru", "ja", "ko", "pt", "de", "zh-Hant",
    "es", "fr", "ar", "hi", "id", "vi", "th", "tr",
})

TRANSFORM_ORIGIN = os.environ.get(
    "VITUAL_TRANSFORM_ORIGIN", "http://127.0.0.1:3000"
).rstrip("/")
OPS_ORIGIN = os.environ.get(
    "VITUAL_API_UPSTREAM", "http://127.0.0.1:8901"
).rstrip("/")

_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})


def is_app_path(path: str) -> bool:
    """Paths that belong to the Next workbench (not hub /api|/static)."""
    if path.startswith("/_next/") or path == "/_next":
        return True
    if path in ("/favicon.ico", "/robots.txt", "/sitemap.xml"):
        return True
    seg = path.lstrip("/").split("/", 1)[0]
    # Next locale segment may be URL-encoded (zh-Hant)
    try:
        seg = urllib.parse.unquote(seg)
    except Exception:
        pass
    return seg in TRANSFORM_LOCALES


def is_ops_path(path: str) -> bool:
    return path == "/ops-api" or path.startswith("/ops-api/")


def proxy_target(path: str) -> tuple[str, str] | None:
    """Return (origin, upstream_path) or None if hub should handle."""
    if is_ops_path(path):
        rest = path[len("/ops-api"):] or "/"
        if not rest.startswith("/"):
            rest = "/" + rest
        return OPS_ORIGIN, rest
    if is_app_path(path):
        return TRANSFORM_ORIGIN, path
    return None


def forward(handler, origin: str, upstream_path: str) -> None:
    """Buffering reverse proxy (stdlib). Enough for Next HTML/RSC + ops JSON."""
    parsed = urllib.parse.urlparse(origin)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    qs = urllib.parse.urlparse(handler.path).query
    req_path = upstream_path + (("?" + qs) if qs else "")

    length = int(handler.headers.get("Content-Length") or 0)
    body = handler.rfile.read(length) if length > 0 else None

    headers = {}
    for k, v in handler.headers.items():
        if k.lower() in _HOP:
            continue
        headers[k] = v
    headers["Host"] = parsed.netloc or host
    # Tell Next it is behind the hub gateway (optional consumers).
    headers["X-Forwarded-Host"] = handler.headers.get("Host", f"127.0.0.1:8000")
    headers["X-Forwarded-Proto"] = "http"
    headers["X-Vitual-Gateway"] = "materials_hub"

    conn_cls = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    conn = conn_cls(host, port, timeout=60)
    try:
        conn.request(handler.command, req_path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        handler.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in _HOP:
                continue
            handler.send_header(k, v)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
    except OSError as e:
        msg = (
            f"upstream unavailable ({origin}): {e}\n"
            f"Start transform: cd transform && npm run dev\n"
            f"Start ops API: yarn api (or :8901)\n"
        ).encode()
        handler.send_response(502)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", str(len(msg)))
        handler.end_headers()
        handler.wfile.write(msg)
    finally:
        conn.close()
