"""Shared parsers for discover metadata (no network)."""

from __future__ import annotations

import calendar
import json
import re
from datetime import datetime, timezone
from typing import Any

_ISO_DATE = re.compile(
    r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})(?:[T ](?P<h>\d{2}):(?P<min>\d{2}):(?P<s>\d{2}))?"
)


def parse_count(v: Any) -> int | None:
    """int, '12,345', '1.2万', '3亿'."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    mult = 1.0
    if s.endswith("亿"):
        mult = 100_000_000.0
        s = s[:-1]
    elif s.endswith("万"):
        mult = 10_000.0
        s = s[:-1]
    try:
        return int(float(s) * mult)
    except (TypeError, ValueError):
        return None


def parse_duration(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    m = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", s, re.I)
    if m:
        h, mi, sec = (int(x) if x else 0 for x in m.groups())
        return h * 3600 + mi * 60 + sec
    parts = s.split(":")
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def parse_unix(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return parse_iso_datetime(str(v))
    if n > 1e12:
        n = n / 1000.0
    if n < 1e9:
        return None
    return n


def parse_iso_datetime(s: str) -> float | None:
    raw = (s or "").strip()
    if not raw:
        return None
    if len(raw) >= 8 and raw[:8].isdigit() and raw[4] not in "-/":
        try:
            dt = datetime.strptime(raw[:8], "%Y%m%d")
            return float(calendar.timegm(dt.timetuple()))
        except ValueError:
            pass
    m = _ISO_DATE.search(raw)
    if not m:
        return None
    hour = int(m.group("h") or 0)
    minute = int(m.group("min") or 0)
    sec = int(m.group("s") or 0)
    dt = datetime(
        int(m.group("y")),
        int(m.group("m")),
        int(m.group("d")),
        hour,
        minute,
        sec,
        tzinfo=timezone.utc,
    )
    return dt.timestamp()


def unix_from_entry(entry: dict[str, Any]) -> float | None:
    for key in (
        "timestamp",
        "release_timestamp",
        "pubdate",
        "created",
        "ctime",
        "senddate",
        "pubtime",
    ):
        ts = parse_unix(entry.get(key))
        if ts is not None:
            return ts
    for key in ("upload_date", "release_date", "uploadDate", "datePublished", "publishDate"):
        ts = parse_iso_datetime(str(entry.get(key) or ""))
        if ts is not None:
            return ts
    return None


def parse_watch_html(html: str) -> dict[str, Any]:
    """Pull upload time / views / duration from a YouTube watch page."""
    out: dict[str, Any] = {}
    for blob in re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    ):
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            data = next(
                (x for x in data if isinstance(x, dict) and x.get("@type") == "VideoObject"),
                data[0] if data else {},
            )
        if not isinstance(data, dict):
            continue
        if data.get("uploadDate") or data.get("datePublished"):
            out["published_at"] = parse_iso_datetime(
                str(data.get("uploadDate") or data.get("datePublished"))
            )
        if data.get("duration"):
            out["duration_sec"] = parse_duration(data.get("duration"))
        for stat in data.get("interactionStatistic") or []:
            if not isinstance(stat, dict):
                continue
            itype = str(stat.get("interactionType") or stat.get("@type") or "")
            if "Watch" in itype or "view" in itype.lower():
                out["views"] = parse_count(stat.get("userInteractionCount"))
        if data.get("author"):
            author = data["author"]
            if isinstance(author, dict):
                out["author"] = author.get("name")
            elif isinstance(author, str):
                out["author"] = author
    m = re.search(r'"publishDate"\s*:\s*"([^"]+)"', html)
    if m and out.get("published_at") is None:
        out["published_at"] = parse_iso_datetime(m.group(1))
    m = re.search(r'"uploadDate"\s*:\s*"([^"]+)"', html)
    if m and out.get("published_at") is None:
        out["published_at"] = parse_iso_datetime(m.group(1))
    m = re.search(r'"viewCount"\s*:\s*"?(\d+)"?', html)
    if m and out.get("views") is None:
        out["views"] = parse_count(m.group(1))
    m = re.search(r'"lengthSeconds"\s*:\s*"?(\d+)"?', html)
    if m and out.get("duration_sec") is None:
        out["duration_sec"] = parse_count(m.group(1))
    m = re.search(r'"ownerChannelName"\s*:\s*"([^"]+)"', html)
    if m and not out.get("author"):
        out["author"] = m.group(1)
    m = re.search(r'"channelId"\s*:\s*"(UC[^"]+)"', html)
    if m:
        out["author_id"] = m.group(1)
    return {k: v for k, v in out.items() if v is not None}


def trunc(s: Any, n: int = 500) -> str | None:
    if not s:
        return None
    t = re.sub(r"<[^>]+>", "", str(s)).strip()
    return t[:n] if t else None
