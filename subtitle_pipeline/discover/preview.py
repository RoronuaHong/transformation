"""Keyword preview for the crawler console.

Search-only: returns candidates from the four adapters and never writes to the
queue or the store. The console lets the operator pick items, then calls the
import endpoint separately.
"""

from __future__ import annotations

import math
import os
import re
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from pathlib import Path
from typing import Any

from .adapters.base import REQUEST_CREDENTIALS
from .crawl import crawl_topic
from .models import Candidate, make_adhoc_topic

PLATFORMS = ("bilibili", "douyin", "youtube", "telegram")

META: dict[str, dict[str, str]] = {
    "bilibili": {"label": "哔哩哔哩", "hint": "公开搜索接口，关键词直接检索，无需登录"},
    "douyin": {"label": "抖音", "hint": "需要登录态 Token（sessionid）或 9223 CDP 窗口"},
    "youtube": {"label": "YouTube", "hint": "yt-dlp 经本机代理检索，建议填 Data API Key 提升稳定性"},
    "telegram": {"label": "Telegram", "hint": "无全站搜索，按公开频道 @handle 做频道内关键词搜索"},
}

_ROOT = Path(__file__).resolve().parents[1]
MAX_KEYWORD_LEN = 80
_WS = re.compile(r"\s+")


def normalize_keyword(raw: str) -> str:
    """NFKC + collapsed whitespace, so the same intent maps to one query.

    Full-width spaces and duplicated spaces are a common reason for a confusing
    "zero results" page when the content actually exists.
    """
    text = unicodedata.normalize("NFKC", str(raw or ""))
    return _WS.sub(" ", text).strip()[:MAX_KEYWORD_LEN]


def _query_parts(keyword: str) -> list[str]:
    parts = [p for p in re.split(r"[\s,，、/]+", keyword.lower()) if p]
    # A long CJK phrase has no spaces; use bigrams so partial title matches
    # still score without pulling in a tokenizer dependency.
    if len(parts) == 1 and len(parts[0]) >= 4 and re.search(r"[\u4e00-\u9fff]", parts[0]):
        q = parts[0]
        return [q[i : i + 2] for i in range(len(q) - 1)]
    return parts


def _relevance(c: Candidate, keyword: str, parts: list[str]) -> float:
    """Exact phrase > all terms > partial terms, popularity only as tie-break.

    Previously the preview sorted purely by views, letting a high-traffic but
    unrelated video outrank the actual keyword match. Search UX guidance puts
    relevance first and keeps the query visible on the results page.
    """
    title = unicodedata.normalize("NFKC", c.title or "").lower()
    desc = unicodedata.normalize("NFKC", (c.description or "")[:200]).lower()
    q = keyword.lower()
    if q and q in title:
        base = 100.0
    elif parts and all(p in title for p in parts):
        base = 70.0
    elif parts:
        base = 12.0 * sum(1 for p in parts if p in title)
    else:
        base = 0.0
    if q and q in desc:
        base += 8.0
    return base + 2.0 * math.log1p(max(0, int(c.views or 0)))


def _douyin_cookie_ready() -> bool:
    candidates = [
        Path(os.environ.get("VITUAL_COOKIES") or ""),
        _ROOT / "cookies.douyin.txt",
        _ROOT / "cookies.txt",
    ]
    for path in candidates:
        if not str(path) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        # Anonymous ttwid alone is not enough: search needs a logged-in session.
        if any(k in text for k in ("sessionid_ss", "sessionid", "sid_tt", "uid_tt")):
            return True
    return False


def _cdp_ready() -> bool:
    base = (os.environ.get("VITUAL_DOUYIN_CDP") or "http://127.0.0.1:9223").rstrip("/")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + "/json/version", timeout=1.2) as resp:
            return resp.status == 200
    except Exception:
        return False


def capabilities() -> dict[str, Any]:
    """Per-platform readiness, used by the console before the first search."""
    cookie_ok = _douyin_cookie_ready()
    cdp_ok = _cdp_ready()
    return {
        "platforms": [
            {
                "key": key,
                "label": META[key]["label"],
                "hint": META[key]["hint"],
                "ready": key != "douyin" or cookie_ok or cdp_ok,
                "needs_login": key == "douyin" and not (cookie_ok or cdp_ok),
                "needs_channels": key == "telegram",
                "needs_token": key in ("youtube", "douyin"),
                "cdp": cdp_ok if key == "douyin" else None,
            }
            for key in PLATFORMS
        ]
    }


def _item(c: Candidate, platform: str, keyword: str) -> dict[str, Any]:
    return {
        "id": f"{platform}:{c.video_id}",
        "platform": platform,
        "video_id": str(c.video_id),
        "url": c.url or c.original_url or "",
        "original_url": c.original_url or c.url or "",
        "title": c.title or str(c.video_id),
        "author": c.author or "",
        "author_id": c.author_id,
        "thumb_url": c.thumb_url,
        "duration_sec": c.duration_sec,
        "views": c.views,
        "published_at": c.published_at,
        "description": c.description,
        "query": c.query or keyword,
    }


def _empty_note(platform: str, *, channels: list[str], login_ready: bool, error: str) -> str:
    if error:
        if error.startswith("Telegram："):
            return error[:220]
        return f"搜索失败：{error[:160]}"
    if platform == "douyin":
        if not login_ready:
            return "抖音需要登录态：在 9223 CDP 窗口登录后点「同步登录态」，或粘贴 Cookie。"
        return "抖音未返回结果：登录态可能已过期或触发风控，请重新同步 Cookie、换关键词或稍后再试。"
    if platform == "telegram":
        if not channels:
            return "Telegram 没有全站关键词搜索，请填写公开频道 @handle 后重试。"
        return "该频道内没有匹配这个关键词的公开帖子（已在频道内搜索并翻页），换频道或换关键词试试。"
    if platform == "youtube":
        return "YouTube 未返回结果：检查本机 10808 代理 / yt-dlp，或换一个英文关键词。"
    return "没有匹配结果：换个关键词，或稍后重试。"


def _failed_result(platform: str, exc: BaseException) -> dict[str, Any]:
    detail = f"{type(exc).__name__}: {exc}"
    return {
        "key": platform,
        "label": META[platform]["label"],
        "count": 0,
        "raw": 0,
        "error": detail,
        "note": f"搜索失败：{detail}",
        "items": [],
    }


def _timeout_result(platform: str, timeout: float) -> dict[str, Any]:
    return {
        "key": platform,
        "label": META[platform]["label"],
        "count": 0,
        "raw": 0,
        "error": "timeout",
        "note": f"超过 {timeout:.0f}s 仍未返回，已跳过该平台；可只勾选它单独重试。",
        "items": [],
    }


def preview_search(
    keyword: str,
    platforms: list[str],
    *,
    quota: int = 12,
    telegram_channels: str = "",
    timeout: float = 40.0,
    youtube_token: str = "",
    douyin_token: str = "",
) -> dict[str, Any]:
    """Return candidates (no DB writes) from every selected platform.

    ``youtube_token`` / ``douyin_token`` are request-scoped (ContextVar), so two
    concurrent searches cannot leak credentials into each other. Platform
    futures share one global deadline: a single slow platform no longer blocks
    the three that already finished.
    """
    keyword = normalize_keyword(keyword)
    selected = [p for p in PLATFORMS if p in platforms]
    channels = [c.strip().lstrip("@") for c in telegram_channels.split(",") if c.strip()]
    creds = {
        "youtube_token": (youtube_token or "").strip(),
        "douyin_token": (douyin_token or "").strip(),
    }
    login_ready = _douyin_cookie_ready() or _cdp_ready() or bool(creds["douyin_token"])
    parts = _query_parts(keyword)

    def one_impl(platform: str) -> dict[str, Any]:
        if not keyword:
            return {
                "key": platform,
                "label": META[platform]["label"],
                "count": 0,
                "raw": 0,
                "error": "",
                "note": "关键词为空，请输入关键词后重试。",
                "items": [],
            }
        topic = make_adhoc_topic(
            keyword,
            platforms=[platform],
            topic_id=f"preview:{platform}",
            fresh_hours=24 * 365 * 20,
            quota=quota,
            min_views=0,
            seed_channels={"telegram": channels} if channels else None,
        )
        pool, _ranked, stats = crawl_topic(topic, do_enrich=False)
        seen: set[str] = set()
        urls: set[str] = set()
        rows: list[dict[str, Any]] = []
        ranked_pool = sorted(
            pool,
            key=lambda c: (_relevance(c, keyword, parts), int(c.views or 0)),
            reverse=True,
        )
        for c in ranked_pool:
            vid = str(c.video_id)
            link = (c.url or c.original_url or "").strip().lower()
            if not vid or vid in seen or not link or link in urls:
                continue
            seen.add(vid)
            urls.add(link)
            rows.append(_item(c, platform, keyword))
            if len(rows) >= quota:
                break
        pstats = (stats.get("platforms") or {}).get(platform) or {}
        error = str(pstats.get("error") or "")
        if not error and stats.get("errors"):
            for e in stats["errors"]:
                if e.get("platform") == platform:
                    error = str(e.get("error") or "")
                    break
        return {
            "key": platform,
            "label": META[platform]["label"],
            "count": len(rows),
            "raw": int(pstats.get("raw") or 0),
            "error": error,
            "note": "" if rows else _empty_note(
                platform, channels=channels, login_ready=login_ready, error=error
            ),
            "items": rows,
        }

    def one(platform: str) -> dict[str, Any]:
        token = REQUEST_CREDENTIALS.set(creds)
        try:
            return one_impl(platform)
        finally:
            REQUEST_CREDENTIALS.reset(token)

    started = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    timed_out: list[str] = []
    if selected:
        ex = ThreadPoolExecutor(max_workers=len(selected))
        try:
            futures = {ex.submit(one, p): p for p in selected}
            try:
                for fut in as_completed(futures, timeout=timeout):
                    platform = futures[fut]
                    try:
                        results[platform] = fut.result()
                    except Exception as e:  # noqa: BLE001 - surface per-platform failure
                        results[platform] = _failed_result(platform, e)
            except FutureTimeout:
                timed_out = [p for _fut, p in futures.items() if p not in results]
                for platform in timed_out:
                    results[platform] = _timeout_result(platform, timeout)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    ordered = [results[p] for p in PLATFORMS if p in results]
    return {
        "keyword": keyword,
        "query": keyword,
        "quota": quota,
        "searched_at": time.time(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "timed_out": timed_out,
        "platforms": ordered,
        "items": [row for group in ordered for row in group["items"]],
    }
