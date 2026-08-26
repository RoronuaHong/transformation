"""YouTube discovery: search + seed channels. Metadata only.

Search uses yt-dlp flat extract (one call per keyword). Missing dates/views
are filled from the watch page HTML (JSON-LD), not full format extraction.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from ..models import Candidate, TopicConfig
from ..parseutil import parse_count, parse_duration, trunc, unix_from_entry, parse_watch_html
from .base import Adapter


def youtube_ydl_opts(*, extract_flat: bool = True, playlistend: int | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": extract_flat,
        "socket_timeout": 20,
        "retries": 1,
        "ignoreerrors": True,
    }
    if playlistend is not None:
        opts["playlistend"] = playlistend
    try:
        from fetch_media import _youtube_proxy_url

        px = _youtube_proxy_url()
        if px:
            opts["proxy"] = px
    except Exception as e:
        print(f"[youtube] proxy skip ({type(e).__name__}: {e})")
    return opts


def youtube_search_url(keyword: str) -> str:
    """Watch results sorted by upload date (sp=CAI=). Not ytsearchdateN."""
    q = urllib.parse.quote_plus(keyword.strip())
    return f"https://www.youtube.com/results?search_query={q}&sp=CAI%3D"


def parse_youtube_seed(seed: str) -> str | None:
    s = (seed or "").strip()
    if not s:
        return None
    if s.startswith("http://") or s.startswith("https://"):
        if "/videos" in s:
            return s
        s = s.rstrip("/")
        if re.search(r"/channel/UC[\w-]+", s) or re.search(r"/@[\w.-]+", s):
            return s + "/videos"
        return s
    if s.startswith("UC") and len(s) >= 22:
        return f"https://www.youtube.com/channel/{s}/videos"
    handle = s if s.startswith("@") else f"@{s}"
    return f"https://www.youtube.com/{handle}/videos"


def _thumb(e: dict[str, Any]) -> str | None:
    thumbs = e.get("thumbnails") or []
    if thumbs and isinstance(thumbs, list) and isinstance(thumbs[-1], dict):
        url = thumbs[-1].get("url")
        if url:
            return str(url)
    thumb = e.get("thumbnail")
    return str(thumb) if thumb else None


def entry_to_candidate(
    e: dict[str, Any],
    topic: TopicConfig,
    *,
    query: str,
    source: str,
) -> Candidate | None:
    vid = e.get("id")
    if not vid or e.get("_type") == "playlist":
        return None
    title = (e.get("title") or "").strip() or str(vid)
    if title in {"[Deleted video]", "[Private video]"}:
        return None
    return Candidate(
        platform="youtube",
        video_id=str(vid),
        url=f"https://www.youtube.com/watch?v={vid}",
        original_url=f"https://www.youtube.com/watch?v={vid}",
        title=title,
        topic_id=topic.id,
        author=(e.get("uploader") or e.get("channel") or None),
        author_id=(
            str(e["channel_id"])
            if e.get("channel_id")
            else (str(e["uploader_id"]) if e.get("uploader_id") else None)
        ),
        published_at=unix_from_entry(e),
        views=parse_count(e.get("view_count") or e.get("views")),
        likes=parse_count(e.get("like_count")),
        comments=parse_count(e.get("comment_count")),
        duration_sec=parse_duration(e.get("duration")),
        thumb_url=_thumb(e),
        description=trunc(e.get("description")),
        query=query,
        raw={"query": query, "id": vid, "source": source},
    )


def fill_from_watch_html(c: Candidate, html: str) -> None:
    meta = parse_watch_html(html)
    if meta.get("published_at") and c.published_at is None:
        c.published_at = float(meta["published_at"])
    if meta.get("views") is not None and c.views is None:
        c.views = int(meta["views"])
    if meta.get("duration_sec") is not None and c.duration_sec is None:
        c.duration_sec = int(meta["duration_sec"])
    if meta.get("author") and not c.author:
        c.author = str(meta["author"])
    if meta.get("author_id") and not c.author_id:
        c.author_id = str(meta["author_id"])
    c.raw = dict(c.raw or {})
    c.raw["enriched"] = "watch_html"


def fill_youtube_meta(candidates: list[Candidate]) -> None:
    """Fill missing date/views from watch pages via yt-dlp's proxied urlopen."""
    todo = [c for c in candidates if c.needs_enrich()]
    if not todo:
        return
    import yt_dlp

    opts = youtube_ydl_opts(extract_flat=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        for i, c in enumerate(todo, 1):
            try:
                resp = ydl.urlopen(c.url)
                html = resp.read().decode("utf-8", errors="replace")
                fill_from_watch_html(c, html)
            except Exception as e:
                c.raw = dict(c.raw or {})
                c.raw["enrich_error"] = f"{type(e).__name__}: {e}"
                print(f"[youtube] watch fail id={c.video_id} {type(e).__name__}: {e}")
            if i == 1 or i % 10 == 0:
                print(f"[youtube] watch {i}/{len(todo)} id={c.video_id}")


class YouTubeAdapter(Adapter):
    platform = "youtube"

    def __init__(self, *, search_n: int = 20) -> None:
        self.search_n = search_n

    def list_candidates(self, topic: TopicConfig) -> list[Candidate]:
        import yt_dlp

        keywords = topic.keywords_for("youtube") or topic.keywords_for("default")
        seen: set[str] = set()
        out: list[Candidate] = []
        opts = youtube_ydl_opts(extract_flat=True, playlistend=self.search_n)
        with yt_dlp.YoutubeDL(opts) as ydl:
            for kw in keywords:
                if topic.search_mode == "date":
                    primary = youtube_search_url(kw)
                    primary_src = "results_date"
                    fallback = f"ytsearch{self.search_n}:{kw}"
                    fallback_src = "ytsearch"
                else:
                    primary = f"ytsearch{self.search_n}:{kw}"
                    primary_src = "ytsearch"
                    fallback = youtube_search_url(kw)
                    fallback_src = "results_date"
                n = self._ingest(
                    ydl,
                    primary,
                    topic,
                    seen,
                    out,
                    query=kw,
                    source=primary_src,
                )
                if n == 0:
                    n = self._ingest(
                        ydl,
                        fallback,
                        topic,
                        seen,
                        out,
                        query=kw,
                        source=fallback_src,
                    )
                    print(
                        f"[youtube] search fallback {fallback_src} kw={kw!r} "
                        f"added={n} total={len(out)}"
                    )
                else:
                    print(
                        f"[youtube] search {primary_src} kw={kw!r} "
                        f"added={n} total={len(out)}"
                    )
            for seed in topic.seed_channels.get("youtube") or []:
                url = parse_youtube_seed(seed)
                if not url:
                    continue
                n = self._ingest(
                    ydl,
                    url,
                    topic,
                    seen,
                    out,
                    query=f"seed:{seed}",
                    source="seed",
                )
                print(f"[youtube] seed={seed!r} added={n} total={len(out)}")
        return out

    def _ingest(
        self,
        ydl: Any,
        target: str,
        topic: TopicConfig,
        seen: set[str],
        out: list[Candidate],
        *,
        query: str,
        source: str,
    ) -> int:
        added = 0
        try:
            info = ydl.extract_info(target, download=False)
        except Exception as e:
            print(f"[youtube] fail source={source} err={type(e).__name__}: {e}")
            return 0
        entries = (info or {}).get("entries") if isinstance(info, dict) else None
        if not entries and isinstance(info, dict) and info.get("id"):
            entries = [info]
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            cand = entry_to_candidate(e, topic, query=query, source=source)
            if not cand or cand.video_id in seen:
                continue
            seen.add(cand.video_id)
            out.append(cand)
            added += 1
        return added
