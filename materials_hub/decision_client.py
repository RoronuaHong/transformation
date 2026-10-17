"""零依赖 SystemOne / Decisions 决策客户端（core / kb / mcp 共用）。

对齐最佳实践：
- 本地 llama-server(/v1/systemone) 与 OpenRouter Decisions API 统一入口；
  识别规则：VITUAL_DECISION_URL 含 'openrouter.ai' 即走云端，需 model + Bearer。
- 云端网关偶发 5xx/超时 → 指数退避重试(默认 2 次)；本地失败即真失败，不重试；
  4xx(格式/鉴权，如误用 choices 数组)不重试，直接降级。
- 云端延迟高(实测 3-12s)，timeout 下限强制 20s，防默认 3s 假失败降级。
- 长上下文会"腐烂"：提供 truncate_state 供调用方截断(检索资料/长文)。
- 埋点可选(recorder 回调)，保持本模块零业务依赖、零重库(不 import open_clip 等)。
"""
import os
import json
import time
import urllib.request as _u
import urllib.error as _ue


_CONF_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "decision.conf")


def _conf_get(key, default=""):
    """配置读取：环境变量优先 > decision.conf(KEY=VALUE) > default。文件回退可热切换。

    _CONF_PATH 提为模块级变量，便于测试 monkeypatch 以隔离真实配置。
    """
    val = os.environ.get(key, "").strip()
    if val:
        return val
    try:
        if os.path.isfile(_CONF_PATH):
            with open(_CONF_PATH, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    if k.strip() == key:
                        return v.strip()
    except Exception:
        pass
    return default


def _endpoint():
    return _conf_get("VITUAL_DECISION_URL").strip().rstrip("/")


def decision_call(body, timeout=None, retries=None, recorder=None):
    """统一调决策服务，返回 answers dict 或 None(服务关/失败)。

    recorder(is_or, duration_s, ok, err) 可选埋点回调。
    响应解析对各后端一致：取顶层 /answers。
    """
    base = _endpoint()
    if not base:
        return None
    is_or = "openrouter.ai" in base
    # 本地 llama-server 与 OpenRouter 决策端点同为 /v1/systemone，统一拼接
    # (历史上对 openrouter 误用 url=base 不拼，导致 POST /api/v1 → 404)。
    url = base + "/v1/systemone"
    if timeout is None:
        try:
            timeout = float(_conf_get("VITUAL_DECISION_TIMEOUT", "3") or "3")
        except ValueError:
            timeout = 3.0
    if is_or and timeout < 20:
        timeout = 20.0
    if retries is None:
        retries = 2 if is_or else 0
    body = dict(body)
    headers = {"Content-Type": "application/json",
               # OpenRouter 经 Cloudflare：非浏览器 UA 会被 1010 指纹封禁(403)，故带浏览器 UA。
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/120.0.0.0 Safari/537.36"}
    if is_or:
        body["model"] = os.environ.get("VITUAL_DECISION_MODEL", "typesafe/jev-1.13")
        key = os.environ.get("OPENROUTER_API_KEY")
        if key:
            headers["Authorization"] = "Bearer " + key
    last_err = None
    for attempt in range(1 + retries):
        t0 = time.time()
        try:
            req = _u.Request(url, data=json.dumps(body).encode("utf-8"),
                             headers=headers, method="POST")
            with _u.urlopen(req, timeout=timeout) as r:
                ans = json.loads(r.read().decode("utf-8")).get("answers", {})
            if recorder:
                recorder(is_or, time.time() - t0, True, None)
            return ans
        except _ue.HTTPError as e:
            last_err = "HTTP %s" % e.code
            if e.code < 500:                      # 客户端错不重试
                if recorder:
                    recorder(is_or, time.time() - t0, False, last_err)
                return None
        except Exception as e:                    # 超时/网络抖动 → 重试
            last_err = e.__class__.__name__
        if attempt < retries:
            time.sleep(min(2 ** attempt, 4))      # 1s, 2s 退避
    if recorder:
        recorder(is_or, time.time() - t0, False, last_err)
    return None


def truncate_state(text, max_chars=None):
    """截断过长的 state，避免 Jev 长上下文腐烂（最佳实践：客户端先过滤/截断）。

    默认上限 2000 字符（约 500-700 token），可通过 VITUAL_DECISION_MAX_CHARS 覆盖。
    """
    if max_chars is None:
        try:
            max_chars = int(os.environ.get("VITUAL_DECISION_MAX_CHARS", "2000"))
        except ValueError:
            max_chars = 2000
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...(已截断)"
