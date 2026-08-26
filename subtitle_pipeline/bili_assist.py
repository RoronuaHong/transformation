#!/usr/bin/env python3
"""Optional Bilibili danmaku → hotword hints for ASR."""

from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from urllib.parse import urlencode


def _get_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        import json

        return json.loads(resp.read().decode("utf-8"))


def _get_text(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def resolve_bvid(bvid: str) -> tuple[str, int, str]:
    bvid = bvid.strip()
    m = re.search(r"(BV[\w]+)", bvid, re.I)
    if not m:
        raise ValueError(f"invalid bvid: {bvid}")
    bv = m.group(1)
    data = _get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}")
    if data.get("code") != 0:
        raise RuntimeError(f"view api failed: {data.get('message')}")
    title = data["data"]["title"]
    cid = int(data["data"]["cid"])
    return bv, cid, title


def fetch_danmaku_texts(cid: int) -> list[str]:
    xml = _get_text(f"https://comment.bilibili.com/{cid}.xml")
    # strip namespaces / control
    texts: list[str] = []
    try:
        root = ET.fromstring(xml)
        for d in root.iter("d"):
            t = (d.text or "").strip()
            if t:
                texts.append(t)
    except ET.ParseError:
        for m in re.finditer(r"<d p=\"[^\"]*\">([^<]+)</d>", xml):
            texts.append(m.group(1).strip())
    return texts


def fetch_top_replies(aid: int, limit: int = 30) -> list[str]:
    q = urlencode({"type": 1, "oid": aid, "mode": 3})
    data = _get_json(f"https://api.bilibili.com/x/v2/reply/main?{q}")
    if data.get("code") != 0:
        return []
    out = []
    for r in (data.get("data") or {}).get("replies") or []:
        msg = ((r.get("content") or {}).get("message") or "").strip()
        if msg:
            out.append(re.sub(r"\s+", " ", msg)[:80])
        if len(out) >= limit:
            break
    return out


def hotwords_from_danmaku(
    bvid: str,
    top_n: int = 25,
    min_count: int = 2,
) -> tuple[str, dict]:
    """Build Whisper initial_prompt-ish string from danmaku/comments."""
    bv, cid, title = resolve_bvid(bvid)
    view = _get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bv}")
    aid = int(view["data"]["aid"])
    danmaku = fetch_danmaku_texts(cid)
    replies = fetch_top_replies(aid)

    # Prefer short punchy phrases (likely chorus / meme)
    c: Counter[str] = Counter()
    for t in danmaku:
        t = re.sub(r"\[.*?\]", "", t).strip()
        if 2 <= len(t) <= 24:
            c[t] += 1
        # also pull 跑路* style substrings
        for m in re.finditer(r"跑路了[^，。！？\s]{0,8}", t):
            c[m.group(0)] += 2

    for t in replies:
        for m in re.finditer(r"跑路了[^，。！？\s]{0,10}", t):
            c[m.group(0)] += 3

    ranked = [(w, n) for w, n in c.most_common(80) if n >= min_count]
    ranked = ranked[:top_n]
    # Always include title
    parts = [title]
    parts.extend(w for w, _ in ranked)
    prompt = "。".join(parts)[:500]
    meta = {
        "bvid": bv,
        "cid": cid,
        "title": title,
        "danmaku_n": len(danmaku),
        "top": ranked[:15],
        "prompt": prompt,
    }
    print(f"[danmaku] {bv} 「{title}」 phrases={len(ranked)} from {len(danmaku)} bullets")
    for w, n in ranked[:8]:
        print(f"  {n}× {w}")
    return prompt, meta
