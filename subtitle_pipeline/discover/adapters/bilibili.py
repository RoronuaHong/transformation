"""Bilibili discovery: search ∪ popular ∪ ranking ∪ seed UPs. Metadata only."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from ..models import Candidate, TopicConfig
from ..parseutil import parse_count, parse_duration, trunc, unix_from_entry
from .base import Adapter

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _cookie_header() -> str:
    env = (os.environ.get("BILI_COOKIE") or "").strip()
    if env:
        if "buvid3=" in env.lower():
            return env
        buvid = f"{uuid.uuid4()}".upper() + "infoc"
        return f"buvid3={buvid}; {env}"
    # Prefer logged-in Netscape cookies.txt (SESSDATA) for high-qn playurl.
    header = _cookie_header_from_netscape()
    if header:
        return header
    buvid = f"{uuid.uuid4()}".upper() + "infoc"
    return f"buvid3={buvid}"


def _cookie_header_from_netscape() -> str:
    """Load .bilibili.com cookies from subtitle_pipeline/cookies.txt if present."""
    root = Path(__file__).resolve().parents[2]
    path = Path(os.environ.get("VITUAL_COOKIES") or os.environ.get("YTDLP_COOKIES") or root / "cookies.txt")
    if not path.is_file():
        return ""
    pairs: list[str] = []
    try:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not ln.strip() or ln.startswith("#"):
                continue
            parts = ln.split("\t")
            if len(parts) < 7:
                continue
            domain, _flag, _path, _secure, _exp, name, value = parts[:7]
            if "bilibili" not in domain.lower():
                continue
            if name and value:
                pairs.append(f"{name}={value}")
    except OSError:
        return ""
    return "; ".join(pairs)


def bili_get_json(url: str, timeout: int = 10) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Cookie": _cookie_header(),
        },
    )
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
            time.sleep(0.4 * (attempt + 1))
    if last_err is not None:
        raise last_err
    return {}


def parse_bili_seed(seed: str) -> str | None:
    s = (seed or "").strip()
    if not s:
        return None
    if s.isdigit():
        return s
    m = re.search(r"space\.bilibili\.com/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"[?&]mid=(\d+)", s)
    if m:
        return m.group(1)
    return None


def title_hits_keywords(title: str, keywords: list[str]) -> bool:
    t = re.sub(r"<[^>]+>", "", title or "").strip().lower()
    if not t:
        return False
    return any(k.lower() in t for k in keywords if k)


def item_to_candidate(
    item: dict[str, Any],
    topic: TopicConfig,
    *,
    query: str | None,
    source: str,
) -> Candidate | None:
    bvid = item.get("bvid")
    if not bvid:
        return None
    title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    author = (owner.get("name") if owner else None) or (
        str(item["author"]) if item.get("author") else None
    )
    author_id = None
    if owner.get("mid") is not None:
        author_id = str(owner["mid"])
    elif item.get("mid") is not None:
        author_id = str(item["mid"])
    stat = item.get("stat") if isinstance(item.get("stat"), dict) else {}
    if stat:
        views = parse_count(stat.get("view") or item.get("play"))
        likes = parse_count(stat.get("like"))
        comments = parse_count(stat.get("reply"))
        coins = parse_count(stat.get("coin"))
        favorites = parse_count(stat.get("favorite"))
        shares = parse_count(stat.get("share"))
    else:
        views = parse_count(item.get("play") or item.get("view"))
        likes = parse_count(item.get("like"))
        comments = parse_count(item.get("review") or item.get("video_review"))
        coins = parse_count(item.get("coins") or item.get("coin"))
        favorites = parse_count(item.get("favorites") or item.get("favorite"))
        shares = parse_count(item.get("share"))
    return Candidate(
        platform="bilibili",
        video_id=str(bvid),
        url=f"https://www.bilibili.com/video/{bvid}",
        original_url=f"https://www.bilibili.com/video/{bvid}",
        title=title or str(bvid),
        topic_id=topic.id,
        author=author,
        author_id=author_id,
        published_at=unix_from_entry(item),
        views=views,
        likes=likes,
        comments=comments,
        coins=coins,
        favorites=favorites,
        shares=shares,
        duration_sec=parse_duration(item.get("duration") or item.get("length")),
        thumb_url=(item.get("pic") or item.get("cover") or None),
        description=trunc(item.get("description") or item.get("desc")),
        query=query,
        raw={"source": source, "bvid": bvid, "query": query},
    )


def fill_bilibili_meta(candidates: list[Candidate]) -> None:
    for i, c in enumerate(candidates, 1):
        try:
            data = bili_get_json(
                f"https://api.bilibili.com/x/web-interface/view?bvid={c.video_id}"
            )
        except Exception as e:
            c.raw = dict(c.raw or {})
            c.raw["enrich_error"] = f"{type(e).__name__}: {e}"
            print(f"[bilibili] view fail id={c.video_id} {type(e).__name__}: {e}")
            continue
        if data.get("code") != 0:
            print(f"[bilibili] view code={data.get('code')} id={c.video_id}")
            continue
        d = data.get("data") or {}
        owner = d.get("owner") if isinstance(d.get("owner"), dict) else {}
        stat = d.get("stat") if isinstance(d.get("stat"), dict) else {}
        c.title = (d.get("title") or c.title or c.video_id).strip()
        c.author = (owner.get("name") if owner else None) or c.author
        if owner.get("mid") is not None:
            c.author_id = str(owner["mid"])
        c.published_at = unix_from_entry(d) or c.published_at
        if stat.get("view") is not None:
            c.views = parse_count(stat.get("view"))
        if stat.get("like") is not None:
            c.likes = parse_count(stat.get("like"))
        if stat.get("reply") is not None:
            c.comments = parse_count(stat.get("reply"))
        c.coins = parse_count(stat.get("coin")) if stat else c.coins
        c.favorites = parse_count(stat.get("favorite")) if stat else c.favorites
        c.shares = parse_count(stat.get("share")) if stat else c.shares
        c.duration_sec = parse_duration(d.get("duration")) or c.duration_sec
        c.description = trunc(d.get("desc")) or c.description
        c.thumb_url = d.get("pic") or c.thumb_url
        c.raw = dict(c.raw or {})
        c.raw["enriched"] = "view"
        if i == 1 or i % 10 == 0:
            print(f"[bilibili] view {i}/{len(candidates)} id={c.video_id}")
        time.sleep(0.15)


class BilibiliAdapter(Adapter):
    platform = "bilibili"

    def list_candidates(self, topic: TopicConfig) -> list[Candidate]:
        keys = topic.keywords_for("bilibili") or topic.keywords_for("default")
        seen: set[str] = set()
        out: list[Candidate] = []
        want = {s.lower() for s in (topic.sources or ["search"])}
        search_rows = self._from_search(keys, order=topic.bili_search_order) if "search" in want else []
        popular_rows = self._from_popular() if "popular" in want else []
        rank_rows = self._from_ranking() if "ranking" in want else []
        seed_rows = (
            self._from_seeds(topic.seed_channels.get("bilibili") or [])
            if "seed" in want
            else []
        )
        print(
            f"[bilibili] rows search={len(search_rows)} popular={len(popular_rows)} "
            f"ranking={len(rank_rows)} seed={len(seed_rows)}"
        )

        def add(rows: list[dict[str, Any]], *, require_kw: bool, default_query: str) -> int:
            added = 0
            for item in rows:
                source = str(item.get("_source") or default_query)
                title = str(item.get("title") or "")
                if require_kw and keys and not title_hits_keywords(title, keys):
                    continue
                cand = item_to_candidate(
                    item,
                    topic,
                    query=str(item.get("_query") or default_query),
                    source=source,
                )
                if not cand or cand.video_id in seen:
                    continue
                seen.add(cand.video_id)
                out.append(cand)
                added += 1
            return added

        n_s = add(search_rows, require_kw=False, default_query=(keys[0] if keys else "search"))
        n_p = add(popular_rows, require_kw=True, default_query="popular")
        n_r = add(rank_rows, require_kw=True, default_query="ranking")
        n_seed = add(seed_rows, require_kw=False, default_query="seed")
        print(
            f"[bilibili] kept search={n_s} popular_hit={n_p} ranking_hit={n_r} "
            f"seed={n_seed} total={len(out)}"
        )
        return out

    def _from_search(self, keywords: list[str], *, order: str = "totalrank") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        order_key = order if order in {"totalrank", "pubdate", "click", "dm", "stow"} else "totalrank"
        for kw in keywords:
            q = urllib.parse.urlencode(
                {
                    "search_type": "video",
                    "keyword": kw,
                    "page": 1,
                    "page_size": 30,
                    "order": order_key,
                }
            )
            try:
                data = bili_get_json(
                    f"https://api.bilibili.com/x/web-interface/search/type?{q}"
                )
            except Exception as e:
                print(f"[bilibili] search fail kw={kw!r} {type(e).__name__}: {e}")
                continue
            if data.get("code") != 0:
                print(f"[bilibili] search code={data.get('code')} kw={kw!r} msg={data.get('message')}")
                continue
            for r in ((data.get("data") or {}).get("result")) or []:
                if not isinstance(r, dict):
                    continue
                r = dict(r)
                r["_source"] = "search"
                r["_query"] = kw
                rows.append(r)
            time.sleep(0.25)
        return rows

    def _from_popular(self) -> list[dict[str, Any]]:
        try:
            data = bili_get_json("https://api.bilibili.com/x/web-interface/popular?ps=50")
        except Exception as e:
            print(f"[bilibili] popular fail {type(e).__name__}: {e}")
            return []
        if data.get("code") != 0:
            print(f"[bilibili] popular code={data.get('code')}")
            return []
        rows = []
        for r in ((data.get("data") or {}).get("list")) or []:
            r = dict(r)
            r["_source"] = "popular"
            rows.append(r)
        return rows

    def _from_ranking(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for rid in (0, 160):
            try:
                data = bili_get_json(
                    f"https://api.bilibili.com/x/web-interface/ranking/v2?rid={rid}&type=all"
                )
            except Exception as e:
                print(f"[bilibili] ranking rid={rid} fail {type(e).__name__}: {e}")
                continue
            if data.get("code") != 0:
                print(f"[bilibili] ranking rid={rid} code={data.get('code')}")
                continue
            for r in ((data.get("data") or {}).get("list")) or []:
                r = dict(r)
                r["_source"] = f"ranking:{rid}"
                rows.append(r)
            time.sleep(0.2)
        return rows

    def _from_seeds(self, seeds: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for seed in seeds:
            mid = parse_bili_seed(seed)
            if not mid:
                print(f"[bilibili] skip bad seed={seed!r}")
                continue
            q = urllib.parse.urlencode({"mid": mid, "pn": 1, "ps": 30, "order": "pubdate"})
            try:
                data = bili_get_json(f"https://api.bilibili.com/x/space/arc/search?{q}")
            except Exception as e:
                print(f"[bilibili] seed mid={mid} fail {type(e).__name__}: {e}")
                continue
            if data.get("code") != 0:
                print(f"[bilibili] seed mid={mid} code={data.get('code')}")
                continue
            vlist = ((data.get("data") or {}).get("list") or {}).get("vlist") or []
            for r in vlist:
                r = dict(r)
                r["_source"] = f"seed:{mid}"
                r["_query"] = f"seed:{mid}"
                rows.append(r)
            time.sleep(0.2)
        return rows
