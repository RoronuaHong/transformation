"""Telegram discovery via public channel web previews (t.me/s/<channel>).

Verified 2026-11-10 against Telegram's logged-out preview surface:

* ``?q=<keyword>`` is Telegram's own server-side in-channel search; hits come
  back newest-first with ``<mark class="highlight">`` markup.
* ``?before=<postId>`` pages backwards through matches; post ids are dense,
  monotonically increasing integers (``data-post="channel/id"``).
* Only **public channels** expose posts. Bots, private groups, user accounts
  and preview-disabled channels return a launch/card page with zero posts.

Polite-crawl rules applied here: <=1 request/sec per channel, 429/503 backoff,
cached dedupe by ``channel/postId``, and an ``og:title`` / markup check that
distinguishes "not a public channel" from "no keyword matches".
"""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

from ..models import Candidate, TopicConfig
from ..parseutil import parse_count, trunc
from .base import Adapter

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_MIN_INTERVAL = 1.0
_MAX_PAGES = 5
_last_request_at: dict[str, float] = {}


class TelegramChannelError(Exception):
    """Base class for channel-level failures surfaced to the console."""


class TelegramFetchError(TelegramChannelError):
    """Network / anti-bot failure while reading a preview page."""


class TelegramNoPreview(TelegramChannelError):
    """Handle is a bot / private group / preview-disabled channel."""


def _throttle(channel: str) -> None:
    now = time.monotonic()
    wait = _MIN_INTERVAL - (now - _last_request_at.get(channel, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_request_at[channel] = time.monotonic()


def preview_url(channel: str, *, query: str = "", before: int | None = None) -> str:
    params: dict[str, str] = {}
    if query:
        params["q"] = query
    if before is not None:
        params["before"] = str(before)
    url = f"https://t.me/s/{channel}"
    return url + ("?" + urllib.parse.urlencode(params) if params else "")


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


def classify_channel_page(html_text: str, *, channel: str) -> str:
    """Return 'ok' | 'no_match' | 'no_preview' for a preview response."""
    if "tgme_widget_message_date" in html_text or "data-post=" in html_text:
        return "ok"
    titles = re.findall(r"<title>(.*?)</title>", html_text, re.I | re.S)
    titles += re.findall(
        r'<meta\s+property="og:title"\s+content="([^"]*)"', html_text, re.I
    )
    joined = " ".join(html.unescape(t).lower() for t in titles)
    # Bot / deep-link launch pages expose no posts at all.
    if (
        "launch @" in joined
        or "telegram: launch" in joined
        or ("telegram:" in joined and "@" + channel.lower() in joined)
    ):
        return "no_preview"
    low = html_text.lower()
    if (
        "no posts" in low
        or "nothing found" in low
        or "tgme_channel_info" in low
        or "tgme_page_title" in low
        or "subscribers" in low
    ):
        return "no_match"
    return "no_preview"


def _fetch_channel_html(
    channel: str, *, query: str = "", before: int | None = None
) -> str:
    url = preview_url(channel, query=query, before=before)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://t.me/",
        },
    )
    last: Exception | None = None
    for attempt in range(3):
        _throttle(channel)
        try:
            with _build_opener().open(req, timeout=12) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 502, 503) and attempt < 2:
                time.sleep(1.5 * (2**attempt))
                continue
            raise TelegramFetchError(
                f"Telegram：读取 t.me/s/{channel} 失败（HTTP {e.code}），请稍后重试或检查网络/代理"
            ) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < 2:
                time.sleep(1.5 * (2**attempt))
                continue
            raise TelegramFetchError(
                f"Telegram：无法访问 t.me/s/{channel}（{type(e).__name__}: {e}）"
            ) from e
    raise TelegramFetchError(f"Telegram：读取 t.me/s/{channel} 失败（{last}）")


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

        keys = [k.lower() for k in (topic.keywords_for("telegram") or []) if k]
        query = (topic.keywords_for("telegram") or [""])[0]
        quota = max(1, int(topic.daily_quota_per_platform or 10))
        seen: set[str] = set()
        out: list[Candidate] = []
        invalid: list[str] = []

        for ch in channels:
            cursor: int | None = None
            pages = 0
            while pages < _MAX_PAGES and len(out) < quota:
                page = _fetch_channel_html(ch, query=query, before=cursor)
                state = classify_channel_page(page, channel=ch)
                if state == "no_preview":
                    invalid.append(ch)
                    break
                msgs = _parse_messages(page)
                if not msgs:
                    break
                ids: list[int] = []
                added = 0
                # Telegram's ?q= search is server-side (hits are wrapped in
                # <mark class="highlight">). Trust its matching instead of
                # re-filtering locally, which drops stemmed/highlighted hits.
                server_side = bool(query) and "highlight" in page
                for m in msgs:
                    pid = str(m.get("post_id") or "")
                    if pid.isdigit():
                        ids.append(int(pid))
                    cid = f"{m['channel']}/{m['post_id']}"
                    if cid in seen:
                        continue
                    if not server_side and keys and not any(
                        k in (m["text"] or "").lower() for k in keys
                    ):
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
                            raw={"source": "telegram", "channel": m["channel"], "query": query},
                        )
                    )
                    added += 1
                    if len(out) >= quota:
                        break
                lowest = min(ids) if ids else None
                if added == 0 or lowest is None or (cursor is not None and lowest >= cursor):
                    break
                cursor = lowest
                pages += 1
            print(f"[telegram] channel={ch} q={query!r} pages={pages} kept={len(out)}")

        if not out and invalid:
            raise TelegramNoPreview(
                "Telegram：@"
                + ", @".join(invalid)
                + " 不是公开频道（bot / 私有频道 / 未开启预览时 t.me/s 没有帖子），请换成公开频道 @handle"
            )
        return out
