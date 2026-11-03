"""Douyin discovery adapter: keyword search ∪ user posts ∪ hot board.

Douyin is heavily anti-bot (X-Bogus / a_bogus signing + cookie gating). A datacenter
IP is almost always blocked. The robust path is an **external open-source parse service**
(env ``DOUYIN_API_BASE``), e.g. Evil0ctal/Douyin_TikTok_Download_API or any fork that
exposes the contract below. Without it, this adapter returns ``[]`` and logs a clear hint —
no crash, no false success.

Expected service contract (GET, JSON):
  {base}/api/douyin/web/search?keyword=K&count=N
  {base}/api/douyin/web/user/post?sec_uid=S&count=N
  {base}/api/douyin/web/hot?count=N
each returning data.list = [ { aweme_id, desc, author:{nickname,uid,sec_uid},
  statistics:{play_count,digg_count,comment_count,share_count}, create_time,
  video:{cover:{url_list:[...]}}, share_url } ]  (items may be wrapped in aweme_info).

To switch to an in-house web crawler later, add a signed (X-Bogus) request path here;
the cookie/proxy helpers from fetch_media.py (clear_proxy_env, cookies.douyin.txt) are
the right building blocks.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..models import Candidate, TopicConfig
from ..parseutil import parse_count, parse_duration, trunc

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DOUYIN_API_BASE = (os.environ.get("DOUYIN_API_BASE") or "").rstrip("/")


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
        if not isinstance(it, dict):
            continue
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
        if not DOUYIN_API_BASE:
            print(
                "[douyin] DOUYIN_API_BASE 未配置：跳过抖音（反爬强，需指向开源解析服务；"
                "见 discover/adapters/douyin.py 顶部契约）"
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
                add(_ext_list("/api/douyin/web/search", {"keyword": kw, "count": 20}),
                    query=kw, source="search")
        if "seed" in want:
            for s in seeds:
                sid = parse_douyin_sec_uid(s)
                if not sid:
                    print(f"[douyin] skip bad seed={s!r}")
                    continue
                add(_ext_list("/api/douyin/web/user/post", {"sec_uid": sid, "count": 20}),
                    query=f"seed:{sid}", source=f"seed:{sid}")
        if "popular" in want or "hot" in want:
            add(_ext_list("/api/douyin/web/hot", {"count": 20}), query="", source="popular")
        print(f"[douyin] kept total={len(out)}")
        return out
