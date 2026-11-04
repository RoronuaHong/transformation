"""Telegram discovery: scrape public channel preview pages (t.me/s/<channel>).

Telegram has no free keyword search, so we scan a set of public channels supplied as
``seed_channels["telegram"]`` (or, from the crawler page, via ``--telegram-channels``).
Posts whose text matches the topic keyword are kept. No auth needed for public channels;
each post becomes a Candidate link (``t.me/<channel>/<id>``). This only collects source
links — retrieving Telegram media downstream is a separate concern.
"""

from __future__ import annotations

import html
import re
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from ..models import Candidate, TopicConfig
from ..parseutil import parse_count, trunc
from .base import Adapter

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Default to the local v2rayN mixed port (http CONNECT works for https t.me).
# Falls back to env HTTP(S)_PROXY, then direct — mirrors the YouTube adapter.
def _build_opener() -> "urllib.request.OpenerDirector":
    px = None
    try:
        from fetch_media import _telegram_proxy_url

        px = _telegram_proxy_url()
    except Exception as e:  # pragma: no cover - defensive
        print(f"[telegram] proxy resolve skip ({type(e).__name__}: {e})")
    if px:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": px, "https": px})
        )
    return urllib.request.build_opener(urllib.request.ProxyHandler())


def parse_telegram_channel(seed: str) -> str | None:
    s = (seed or "").strip().lstrip("@")
    s = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?", "", s)
    s = s.split("?")[0].split("/")[0].strip()
    if re.fullmatch(r"[A-Za-z0-9_]{3,32}", s):
        return s
    return None


def _clean(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def _parse_dt(s: str) -> float | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _parse_messages(html_text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # Split on each post date anchor; the chunk after it holds text + views.
    pieces = re.split(
        r'(<a\s+[^>]*class="tgme_widget_message_date"[^>]*>)', html_text
    )
    for i in range(1, len(pieces), 2):
        anchor = pieces[i]
        chunk = pieces[i + 1] if i + 1 < len(pieces) else ""
        href_m = re.search(r'href="([^"]+)"', anchor)
        if not href_m:
            continue
        cm = re.search(r"t\.me/([^/]+)/(\d+)", href_m.group(1))
        if not cm:
            continue
        channel, post_id = cm.group(1), cm.group(2)
        href = href_m.group(1)
        url = href if href.startswith("http") else "https://" + href.lstrip("/")
        text_m = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk, re.S
        )
        text = _clean(text_m.group(1)) if text_m else ""
        views = None
        # The visible count lives in the tgme_widget_message_views span text
        # (e.g. "5.87M"). The data-view attr is now a base64 tracking token,
        # not a raw integer, so the legacy regex below only catches old pages.
        vm = re.search(
            r'class="tgme_widget_message_views"[^>]*>(.*?)</span>', chunk, re.S
        )
        if vm:
            views = parse_count(vm.group(1))
        if views is None:
            vm2 = re.search(r'data-view="(\d+)"', chunk)
            if vm2:
                views = int(vm2.group(1))
        ts = None
        tm = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        if tm:
            ts = _parse_dt(tm.group(1))
        out.append(
            {
                "channel": channel,
                "post_id": post_id,
                "url": url,
                "text": text,
                "views": views,
                "ts": ts,
            }
        )
    return out


def _fetch_channel_html(channel: str) -> str:
    url = f"https://t.me/s/{channel}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://t.me/",
        },
    )
    try:
        with _build_opener().open(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        print(f"[telegram] 拉取失败 {channel}: {type(e).__name__}: {e}")
        return ""


class TelegramAdapter(Adapter):
    platform = "telegram"

    def list_candidates(self, topic: TopicConfig) -> list[Candidate]:
        seeds = topic.seed_channels.get("telegram") or []
        channels = [c for c in (parse_telegram_channel(s) for s in seeds) if c]
        if not channels:
            # Fallback: treat the keyword itself as a single channel handle.
            kw = (topic.keywords_for("telegram") or [""])[0]
            ch = parse_telegram_channel(kw) if kw else None
            if ch:
                channels = [ch]
        if not channels:
            print("[telegram] 未提供频道(seed_channels.telegram 或关键词作频道名)，跳过")
            return []

        keys = [k.lower() for k in (topic.keywords_for("telegram") or [])]
        seen: set[str] = set()
        out: list[Candidate] = []
        for ch in channels:
            for m in _parse_messages(_fetch_channel_html(ch)):
                if keys and not any(k in (m["text"] or "").lower() for k in keys):
                    continue
                cid = f"{m['channel']}/{m['post_id']}"
                if cid in seen:
                    continue
                seen.add(cid)
                out.append(
                    Candidate(
                        platform="telegram",
                        video_id=cid,
                        url=m["url"],
                        original_url=m["url"],
                        title=trunc(m["text"] or cid, 200) or cid,
                        topic_id=topic.id,
                        author=m["channel"],
                        author_id=m["channel"],
                        published_at=m["ts"],
                        views=m["views"],
                        description=trunc(m["text"]),
                        raw={"source": "telegram", "channel": m["channel"]},
                    )
                )
        print(f"[telegram] channel(s)={channels} kept={len(out)}")
        return out
