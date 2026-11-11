"""Douyin discovery adapter: keyword search ∪ user posts ∪ hot board.

Douyin is heavily anti-bot (X-Bogus signing + cookie gating + datacenter-IP blocking).
This adapter now does an **in-house signed web crawl** of Douyin's web search API using a
pure-Python X-Bogus signer (see ``./xbogus.py``) plus cookies from ``cookies.douyin.txt``
(loaded the same way as ``fetch_media``). It requires NO external service.

If the in-house path cannot run (no cookies present), it transparently falls back to the
optional external open-source parse service pointed at ``DOUYIN_API_BASE``. If neither is
available it returns ``[]`` and logs a clear hint — no crash, no false success.

Required cookies for the signed path: a logged-in ``cookies.douyin.txt`` (Netscape export)
with at least ``ttwid`` / ``sessionid_ss`` / ``sid_tt`` / ``odin_tt``. Without them Douyin
returns a risk-control payload and the adapter yields nothing.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..models import Candidate, TopicConfig
from ..parseutil import parse_count, parse_duration, trunc
from .base import REQUEST_CREDENTIALS
from .xbogus import append_x_bogus

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DOUYIN_API_BASE = (os.environ.get("DOUYIN_API_BASE") or "").rstrip("/")

# Web search / feed endpoints (host is www.douyin.com; X-Bogus signs the query string).
_SEARCH_EP = "/aweme/v1/web/general/search/single/"
_POST_EP = "/aweme/v1/web/aweme/post/"
_HOT_EP = "/aweme/v1/web/hot/search/list/"

# Common params Douyin's web app ships on every request.
_BASE_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "190500",
    "version_name": "19.5.0",
    "cookie_enabled": "true",
    "screen_width": "1920",
    "screen_height": "1080",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "120.0.0.0",
    "msToken": "",
    "webid": "",
}


def parse_douyin_sec_uid(seed: str) -> str | None:
    s = (seed or "").strip()
    if not s:
        return None
    if re.fullmatch(r"[A-Za-z0-9_\-]{10,}", s):
        return s
    m = re.search(r"sec_user_id=([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"(?:user|profile)/([A-Za-z0-9_\-]{10,})", s)
    if m:
        return m.group(1)
    return None


# --------------------------------------------------------------------------- #
# Cookie loading (mirrors fetch_media.resolve_cookies_file, kept local so the
# discover package stays lightweight).
# --------------------------------------------------------------------------- #
def _douyin_cookie_file() -> Path | None:
    for key in ("VITUAL_COOKIES", "YTDLP_COOKIES"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            p = Path(raw)
            if p.is_file():
                return p
    root = Path(__file__).resolve().parents[2]
    for name in ("cookies.douyin.txt", "cookies.txt"):
        p = root / name
        if p.is_file():
            return p
    return None


def _douyin_token_cookie() -> str:
    """A session token supplied via ``DOUYIN_TOKEN`` env (console token field).

    Accepts either a bare ``sessionid`` value or a full ``a=b; c=d`` header blob.
    Returns a ``name=value; ...`` cookie header fragment, or "".
    """
    raw = (
        REQUEST_CREDENTIALS.get().get("douyin_token")
        or os.environ.get("DOUYIN_TOKEN")
        or ""
    ).strip()
    if not raw:
        return ""
    # A header blob (has separators or is long) is passed through verbatim.
    if len(raw) > 80 or ";" in raw or (" " in raw and "=" in raw):
        return raw
    return f"sessionid_ss={raw}; sessionid={raw}; sid_tt={raw}; uid_tt={raw}"


def _douyin_cookie_header() -> str:
    """Netscape ``cookies.douyin.txt`` → ``a=b; c=d`` header (douyin.com only).

    A token supplied via ``DOUYIN_TOKEN`` is merged in first so the console's
    token field can authenticate without a cookie file on disk.
    """
    pairs: list[str] = []
    token = _douyin_token_cookie()
    if token:
        for kv in token.split(";"):
            if "=" in kv:
                pairs.append(kv.strip())
    p = _douyin_cookie_file()
    if p is None:
        return "; ".join(pairs)
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            parts = ln.split("\t")
            if len(parts) >= 7:
                domain, _flag, _path, _secure, _exp, name, value = parts[:7]
                if "douyin" not in domain.lower():
                    continue
                if name and value:
                    pairs.append(f"{name}={value}")
            elif "=" in ln:
                # tolerate a raw "a=b; c=d" header blob
                for kv in ln.split(";"):
                    if "=" in kv:
                        pairs.append(kv.strip())
    except OSError:
        return "; ".join(pairs)
    return "; ".join(pairs)


def _has_cookies() -> bool:
    if _douyin_token_cookie():
        return True  # a supplied token counts as a logged-in session
    h = _douyin_cookie_header()
    return bool(h) and ("ttwid" in h or "sessionid" in h or "sid_tt" in h)


def cdp_cookies_to_netscape(cookies: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Convert a CDP ``Storage.getCookies`` result to Netscape cookies.txt text.

    Keeps only douyin.com cookies (including HttpOnly ones such as ``sessionid_ss``,
    which ``document.cookie`` cannot see). CDP is fed by the dedicated visible Chrome
    launched for manual login, so the user never has to export cookies by hand.
    """
    rows: list[str] = []
    seen: set[tuple[str, str]] = set()
    names: list[str] = []
    for c in cookies:
        domain = str(c.get("domain") or "")
        if "douyin" not in domain.lower():
            continue
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "")
        if not name or not value or (domain, name) in seen:
            continue
        seen.add((domain, name))
        # CDP expires is epoch seconds; -1 means a session cookie.
        try:
            exp = float(c.get("expires") or -1)
            expires = str(int(exp)) if exp > 0 else "0"
        except (TypeError, ValueError):
            expires = "0"
        secure = "TRUE" if c.get("secure") else "FALSE"
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = str(c.get("path") or "/")
        rows.append(
            "\t".join((domain, include_sub, path, secure, expires, name, value))
        )
        names.append(name)
    text = (
        "# Netscape HTTP Cookie File (auto-captured from CDP Chrome)\n"
        + "\n".join(rows)
        + ("\n" if rows else "")
    )
    # Only these prove a real logged-in session. ttwid / odin_tt / passport_csrf_token
    # are issued to anonymous visitors too, so counting them would report a false
    # "logged in" before the user actually signs in.
    login_names = {"sessionid", "sessionid_ss", "sid_tt", "uid_tt", "uid_tt_ss"}
    info = {
        "count": len(rows),
        "logged_in": bool(login_names & set(names)),
        "names": sorted(set(names)),
    }
    return text, info


# --------------------------------------------------------------------------- #
# In-house signed web request
# --------------------------------------------------------------------------- #
def _direct_signed_get(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Vendored X-Bogus signer over direct urllib. Returns the JSON dict or None."""
    qs = urllib.parse.urlencode(payload)
    url = f"https://www.douyin.com{path}?{qs}"
    signed = append_x_bogus(url, _UA)

    headers = {
        "User-Agent": _UA,
        "Referer": "https://www.douyin.com/",
        "Origin": "https://www.douyin.com",
        "Accept": "application/json",
    }
    cookie = _douyin_cookie_header()
    if cookie:
        headers["Cookie"] = cookie

    try:
        req = urllib.request.Request(signed, headers=headers)
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        print(f"[douyin] 签名请求失败 {path}: {type(e).__name__}: {e}")
        return None


# 全局最小请求间隔：抖音会静默丢弃短时间内过密的请求（连接挂起不返回），
# 拉开间隔能显著降低触发风控的概率。
_LAST_REQ_TS = 0.0
_MIN_REQ_GAP = 3.0


def _signed_get(path: str, params: dict[str, Any], *, count: int = 20) -> list[dict[str, Any]]:
    """GET a Douyin web endpoint with an X-Bogus signed query; return aweme dicts.

    Preferred path: execute the signed request inside the user's real logged-in Chrome
    over CDP (the page's own ``frontierSign`` — the vendored signer goes stale whenever
    Douyin rotates the algorithm). Falls back to the vendored signer when no CDP browser
    is open.
    """
    payload = dict(_BASE_PARAMS)
    payload["count"] = str(count)
    for k, v in params.items():
        payload[k] = v

    # 全局最小请求间隔，降低触发抖音静默风控（频繁请求会被静默丢弃连接）
    global _LAST_REQ_TS
    _now = time.time()
    _gap = _MIN_REQ_GAP - (_now - _LAST_REQ_TS)
    if _gap > 0:
        time.sleep(_gap)
    _LAST_REQ_TS = time.time()

    from . import douyin_cdp

    j = douyin_cdp.cdp_get_json(path, payload)
    if j is None:
        # 偶发 CDP 无响应时退避一次重试（优先页面真实签名，不立即回退到已失效的 vendored）
        time.sleep(2.0)
        j = douyin_cdp.cdp_get_json(path, payload)
    if j is None:
        j = _direct_signed_get(path, payload)
    if j is None:
        return []

    if not isinstance(j, dict):
        return []
    if j.get("status_code", 0) not in (0, None):
        print(
            f"[douyin] 签名请求被拒 status_code={j.get('status_code')} "
            f"msg={j.get('err_msg')}"
        )
        return []
    data = j.get("data")
    if isinstance(data, dict):
        arr = data.get("aweme_list") or data.get("aweme_info") or data.get("list") or []
    elif isinstance(data, list):
        # The real search endpoint returns a bare list (often empty on anti-spam).
        arr = data
    else:
        arr = []
    if not arr:
        nil = j.get("search_nil_info") or {}
        nil_type = nil.get("search_nil_type") or nil.get("search_nil_item")
        if nil_type:
            print(
                f"[douyin] 返回空结果 anti-spam={nil_type}："
                "抖音触发了风控/验证码，请在已打开的抖音窗口完成验证后重试。"
            )
        else:
            print(f"[douyin] 返回空结果 status_code={j.get('status_code')}")
    out: list[dict[str, Any]] = []
    for it in arr:
        if isinstance(it, dict):
            out.append(it.get("aweme_info") or it)
    return out


# --------------------------------------------------------------------------- #
# External service fallback (optional). Same JSON contract as before.
# --------------------------------------------------------------------------- #
def _ext_list(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    if not DOUYIN_API_BASE:
        return []
    qs = urllib.parse.urlencode(params)
    url = f"{DOUYIN_API_BASE}{path}?{qs}"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            j = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        print(f"[douyin] {path} fail {type(e).__name__}: {e}")
        return []
    data = j.get("data") if isinstance(j, dict) else j
    if not isinstance(data, dict):
        data = {}
    arr = (
        data.get("list")
        or data.get("data")
        or data.get("awemes")
        or (data if isinstance(data, list) else [])
    )
    out: list[dict[str, Any]] = []
    for it in arr:
        if isinstance(it, dict):
            out.append(it.get("aweme_info") or it)
    return out


def aweme_to_candidate(
    aweme: dict[str, Any], topic: TopicConfig, *, query: str, source: str
) -> Candidate | None:
    aid = str(aweme.get("aweme_id") or aweme.get("awemeId") or "")
    if not aid:
        return None
    author = aweme.get("author") or aweme.get("author_info") or {}
    stats = aweme.get("statistics") or {}
    video = aweme.get("video") or {}
    cover_list = (video.get("cover") or {}).get("url_list") or []
    cover = cover_list[0] if cover_list else None
    desc = aweme.get("desc") or ""
    return Candidate(
        platform="douyin",
        video_id=aid,
        url=aweme.get("share_url") or f"https://www.douyin.com/video/{aid}",
        original_url=aweme.get("share_url") or f"https://www.douyin.com/video/{aid}",
        title=trunc(desc, 200) or str(aid),
        topic_id=topic.id,
        author=author.get("nickname") or author.get("nick_name"),
        author_id=str(author.get("sec_uid") or author.get("uid") or ""),
        published_at=aweme.get("create_time"),
        views=parse_count(stats.get("play_count")),
        likes=parse_count(stats.get("digg_count")),
        comments=parse_count(stats.get("comment_count")),
        favorites=parse_count(stats.get("collect_count") or stats.get("favorite_count")),
        shares=parse_count(stats.get("share_count")),
        duration_sec=parse_duration(video.get("duration")),
        thumb_url=cover,
        description=trunc(desc),
        query=query,
        raw={"source": source, "query": query},
    )


class DouyinAdapter:
    platform = "douyin"

    def list_candidates(self, topic: TopicConfig) -> list[Candidate]:
        keys = topic.keywords_for("douyin") or topic.keywords_for("default")
        seeds = topic.seed_channels.get("douyin") or []
        want = {s.lower() for s in (topic.sources or ["search"])}

        # Nothing we can do without cookies (in-house) or an external service.
        if not _has_cookies() and not DOUYIN_API_BASE:
            print(
                "[douyin] 未配置 cookies.douyin.txt 也未设置 DOUYIN_API_BASE：跳过抖音。"
                " 登录抖音后导出 Netscape cookies 存为 subtitle_pipeline/cookies.douyin.txt，"
                " 或设置 DOUYIN_API_BASE 指向开源解析服务。"
            )
            return []

        seen: set[str] = set()
        out: list[Candidate] = []

        def add(rows: list[dict[str, Any]], *, query: str, source: str) -> None:
            for aweme in rows:
                cand = aweme_to_candidate(aweme, topic, query=query, source=source)
                if not cand or cand.video_id in seen:
                    continue
                seen.add(cand.video_id)
                out.append(cand)

        if "search" in want:
            for kw in keys:
                rows = _signed_get(
                    _SEARCH_EP,
                    {
                        "keyword": kw,
                        "search_channel": "aweme_general",
                        "publish_time": "0",
                        "sort_type": "0",
                        "search_source": "normal_search",
                        "query_correct_type": "1",
                    },
                    count=20,
                )
                if not rows and DOUYIN_API_BASE:
                    rows = _ext_list(
                        "/api/douyin/web/search", {"keyword": kw, "count": 20}
                    )
                add(rows, query=kw, source="search")

        if "seed" in want:
            for s in seeds:
                sid = parse_douyin_sec_uid(s)
                if not sid:
                    print(f"[douyin] skip bad seed={s!r}")
                    continue
                rows = _signed_get(
                    _POST_EP,
                    {
                        "sec_user_id": sid,
                        "max_cursor": "0",
                        "locate_query": "false",
                        "show_live_replay": "false",
                    },
                    count=20,
                )
                if not rows and DOUYIN_API_BASE:
                    rows = _ext_list(
                        "/api/douyin/web/user/post", {"sec_uid": sid, "count": 20}
                    )
                add(rows, query=f"seed:{sid}", source=f"seed:{sid}")

        if "popular" in want or "hot" in want:
            rows = _signed_get(_HOT_EP, {"device_id": ""}, count=20)
            if not rows and DOUYIN_API_BASE:
                rows = _ext_list("/api/douyin/web/hot", {"count": 20})
            add(rows, query="", source="popular")

        print(f"[douyin] kept total={len(out)}")
        return out
