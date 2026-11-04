"""CDP bridge for Douyin: sign + fetch inside the user's real logged-in browser.

Douyin rotates its X-Bogus algorithm, so a vendored signer goes stale (28-char era vs
the current 16-char ``frontierSign``). Instead of chasing that treadmill, we execute the
signed request *inside the dedicated login Chrome* (launched with
``--remote-debugging-port``) via CDP ``Runtime.evaluate``: the page's own
``byted_acrawler.frontierSign`` produces a genuine signature and ``fetch`` carries the
user's real cookies. This is equivalent to the user browsing — it does not bypass any
verification; if Douyin serves a captcha, the request is still gated (see the search
endpoint being held until the on-page challenge is solved).

Disable by setting ``VITUAL_DOUYIN_CDP=""``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from websockets.sync.client import connect

_CDP_BASE = os.environ.get("VITUAL_DOUYIN_CDP", "http://127.0.0.1:9223").rstrip("/")
_JS_TIMEOUT_MS = 8000
_RECV_TIMEOUT = _JS_TIMEOUT_MS / 1000 + 6  # CDP round-trip budget on top of the JS timer


def cdp_base() -> str:
    return _CDP_BASE


def _opener() -> urllib.request.OpenerDirector:
    # CDP is local; never route it through a system proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _douyin_page_ws_url(timeout: float = 3.0) -> str | None:
    """webSocketDebuggerUrl of the open douyin.com tab, or None."""
    if not _CDP_BASE:
        return None
    try:
        with _opener().open(_CDP_BASE + "/json/list", timeout=timeout) as r:
            tabs = json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    for t in tabs:
        if t.get("type") == "page" and "douyin" in str(t.get("url", "")).lower():
            ws = t.get("webSocketDebuggerUrl")
            if ws:
                return ws
    return None


# Keep the template free of braces so it can be .format()ed safely.
_EVAL_TEMPLATE = """(async()=>{try{
  const p=new URLSearchParams(__PARAMS__);
  let s=null;
  try{ s=byted_acrawler.frontierSign(p.toString()); }catch(e){ s=null; }
  if(s && typeof s==='object') s=s['X-Bogus']||s['x-bogus']||null;
  const qs=p.toString()+(s?'&X-Bogus='+encodeURIComponent(String(s)):'');
  const ctl=new AbortController(); const to=setTimeout(()=>ctl.abort(),__TIMEOUT__);
  try{
    const r=await fetch('__PATH__?'+qs,{credentials:'include',
      headers:{'Accept':'application/json'},signal:ctl.signal});
    const txt=await r.text();
    return JSON.stringify({http:r.status,body:txt.slice(0,4000000)});
  }finally{ clearTimeout(to); }
}catch(e){ return JSON.stringify({error:String(e).slice(0,200)}); }})()"""


def build_evaluate_expression(path: str, params: dict[str, Any]) -> str:
    """Pure helper: the JS evaluated inside the page (unit-tested without a browser)."""
    return (
        _EVAL_TEMPLATE.replace("__PARAMS__", json.dumps(params, ensure_ascii=False))
        .replace("__TIMEOUT__", str(_JS_TIMEOUT_MS))
        .replace("__PATH__", path)
    )


def _parse_eval_result(value: Any) -> dict[str, Any] | None:
    """Pure helper: CDP evaluate value -> Douyin JSON dict (None on failure)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        outer = json.loads(value)
    except ValueError:
        return None
    if not isinstance(outer, dict) or "error" in outer:
        return None
    try:
        body = json.loads(outer.get("body") or "")
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def cdp_get_json(path: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Signed GET via the real browser. Returns the JSON dict, or None on any failure
    (caller falls back to the vendored signer). Never raises."""
    ws_url = _douyin_page_ws_url()
    if not ws_url:
        return None
    expr = build_evaluate_expression(path, params)
    try:
        with connect(ws_url, open_timeout=3, close_timeout=2, max_size=None) as ws:
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                "expression": expr, "awaitPromise": True, "returnByValue": True}}))
            while True:
                msg = json.loads(ws.recv(timeout=_RECV_TIMEOUT))
                if msg.get("id") == 1:
                    break
    except (OSError, TimeoutError, ValueError):
        return None
    result = (msg.get("result") or {}).get("result") or {}
    if result.get("subtype") == "error":
        return None
    return _parse_eval_result(result.get("value"))
