#!/usr/bin/env python3
"""Verify each discovery-path claim. Writes 验证报告.md next to this script."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PIPELINE = ROOT.parent / "subtitle_pipeline"
REPORT = ROOT / "验证报告.md"
sys.path.insert(0, str(PIPELINE))

results: list[dict] = []


def record(vid: str, title: str, ok: bool, detail: str, expected: str = "must") -> None:
    results.append(
        {
            "id": vid,
            "title": title,
            "ok": ok,
            "expected": expected,  # must | allow_fail
            "detail": detail.strip()[:1200],
        }
    )
    flag = "PASS" if ok else ("SOFT-FAIL" if expected == "allow_fail" else "FAIL")
    print(f"[{flag}] {vid} {title}: {detail[:200].replace(chr(10), ' ')}")


def v1_yt_dlp() -> None:
    try:
        import yt_dlp

        ver = getattr(yt_dlp.version, "__version__", "?")
        record("V1", "本机 yt-dlp 可用", True, f"yt_dlp {ver}")
    except Exception as e:
        record("V1", "本机 yt-dlp 可用", False, repr(e))


def v2_youtube_search() -> None:
    try:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "playlistend": 5,
        }
        query = "ytsearch5:AI monetization"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        entries = (info or {}).get("entries") or []
        rows = []
        for e in entries:
            if not e:
                continue
            vid = e.get("id")
            title = e.get("title") or ""
            if vid:
                rows.append(
                    {
                        "id": vid,
                        "title": title[:80],
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "views": e.get("view_count"),
                    }
                )
        if rows:
            record(
                "V2",
                "YouTube ytsearch 出候选",
                True,
                f"got {len(rows)} · sample={rows[0]['id']} {rows[0]['title']!r}",
            )
        else:
            record("V2", "YouTube ytsearch 出候选", False, "entries empty (network/block?)")
    except Exception as e:
        record("V2", "YouTube ytsearch 出候选", False, f"{type(e).__name__}: {e}")


def _bili_get(url: str, timeout: int = 25) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def v3_bilibili_discover() -> None:
    """B站：搜索接口常 412；热门/排行可走通，作为第一期发现主路径。"""
    search_note = ""
    try:
        q = urllib.parse.urlencode(
            {"search_type": "video", "keyword": "AI变现", "page": 1, "page_size": 5}
        )
        data = _bili_get(f"https://api.bilibili.com/x/web-interface/search/type?{q}")
        result = ((data.get("data") or {}).get("result")) or []
        if data.get("code") == 0 and result:
            bvid = result[0].get("bvid")
            title = re.sub(r"<[^>]+>", "", result[0].get("title") or "")
            record(
                "V3",
                "B站发现（搜索或热门）",
                True,
                f"search ok n={len(result)} sample={bvid} {title[:50]!r}",
            )
            return
        search_note = f"search code={data.get('code')} {data.get('message')}"
    except Exception as e:
        search_note = f"search {type(e).__name__}: {e}"

    try:
        data = _bili_get("https://api.bilibili.com/x/web-interface/popular?ps=10")
        lst = ((data.get("data") or {}).get("list")) or []
        if data.get("code") == 0 and lst:
            # client-side keyword soft filter demo
            keys = ("AI", "人工智能", "ChatGPT", "变现", "副业")
            hit = [
                x
                for x in lst
                if any(k.lower() in (x.get("title") or "").lower() for k in keys)
            ]
            sample = (hit or lst)[0]
            record(
                "V3",
                "B站发现（搜索或热门）",
                True,
                f"{search_note}; popular ok n={len(lst)} keyword_hits={len(hit)} "
                f"sample={sample.get('bvid')} view={((sample.get('stat') or {}).get('view'))} "
                f"title={str(sample.get('title'))[:40]!r}",
            )
            return
        record("V3", "B站发现（搜索或热门）", False, f"{search_note}; popular empty code={data.get('code')}")
    except Exception as e:
        record("V3", "B站发现（搜索或热门）", False, f"{search_note}; popular {type(e).__name__}: {e}")


def v4_bilibili_view() -> None:
    bvid = None
    try:
        data = _bili_get("https://api.bilibili.com/x/web-interface/popular?ps=3")
        lst = ((data.get("data") or {}).get("list")) or []
        if lst:
            bvid = lst[0].get("bvid")
    except Exception as e:
        record("V4", "B站 view API 补全", False, f"popular for bvid failed: {e}")
        return
    if not bvid:
        record("V4", "B站 view API 补全", False, "no bvid from popular")
        return
    try:
        time.sleep(0.5)
        data = _bili_get(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}")
        if data.get("code") != 0:
            record(
                "V4",
                "B站 view API 补全",
                False,
                f"code={data.get('code')} {data.get('message')} bvid={bvid}",
            )
            return
        d = data["data"]
        view = (d.get("stat") or {}).get("view")
        title = d.get("title")
        ok = isinstance(view, int) and view >= 0 and bool(title)
        record(
            "V4",
            "B站 view API 补全",
            ok,
            f"bvid={bvid} view={view} title={str(title)[:60]!r}",
        )
    except Exception as e:
        record("V4", "B站 view API 补全", False, f"{type(e).__name__}: {e}")

def _ytdlp_probe(url: str) -> tuple[bool, str]:
    try:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return False, "empty info"
        title = info.get("title") or info.get("id")
        return True, f"id={info.get('id')} title={str(title)[:60]!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def v5_douyin() -> None:
    # Public search is usually blocked; probe a generic douyin host resolve / search via yt-dlp
    ok, detail = _ytdlp_probe("https://www.douyin.com/search/AI%E5%8F%98%E7%8E%B0")
    if ok:
        record("V5", "抖音无cookie搜索/解析", True, detail, expected="allow_fail")
    else:
        record(
            "V5",
            "抖音无cookie搜索/解析",
            False,
            f"预期可失败 → inbox。原因: {detail}",
            expected="allow_fail",
        )


def v6_kuaishou() -> None:
    ok, detail = _ytdlp_probe("https://www.kuaishou.com/search/video?searchKey=AI")
    if ok:
        record("V6", "快手无cookie搜索/解析", True, detail, expected="allow_fail")
    else:
        record(
            "V6",
            "快手无cookie搜索/解析",
            False,
            f"预期可失败 → inbox。原因: {detail}",
            expected="allow_fail",
        )


def v7_rank_quota() -> None:
    now = time.time()

    @dataclass
    class C:
        platform: str
        video_id: str
        views: int
        likes: int
        comments: int
        published_at: float
        topic_id: str = "ai_monetize"

    def score(c: C, velocity_first: bool = False) -> float:
        age_h = max((now - c.published_at) / 3600.0, 1.0)
        velocity = c.views / age_h
        eng = c.likes + 2 * c.comments
        w_v = 3.5 if velocity_first else 2.5
        return math.log1p(c.views) + w_v * math.log1p(velocity) + 0.5 * math.log1p(eng)

    fresh_hours = 48
    pool = [
        C("youtube", "old", 10_000_000, 1, 0, now - 400 * 3600),  # too old
        C("youtube", "low", 100, 0, 0, now - 5 * 3600),  # below min
        C("youtube", "a", 80_000, 100, 10, now - 10 * 3600),
        C("youtube", "b", 200_000, 500, 20, now - 6 * 3600),
        C("youtube", "c", 50_000, 50, 5, now - 20 * 3600),
        C("youtube", "d", 120_000, 200, 8, now - 8 * 3600),
    ]
    min_views = 10_000
    kept = []
    for c in pool:
        age_h = (now - c.published_at) / 3600.0
        if age_h > fresh_hours:
            continue
        if c.views < min_views:
            continue
        kept.append((score(c), c))
    kept.sort(key=lambda x: x[0], reverse=True)
    quota = 3
    top = kept[:quota]
    ids = [c.video_id for _, c in top]
    ok = (
        "old" not in ids
        and "low" not in ids
        and len(top) == 3
        and ids[0] == "b"  # highest views + fresh → should rank first
    )
    detail = f"top_ids={ids} scores={[round(s,2) for s,_ in top]}"
    record("V7", "打分/配额逻辑", ok, detail)


def v8_dedupe_queue() -> None:
    try:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "q.db"
            conn = sqlite3.connect(db)
            conn.execute(
                """
                CREATE TABLE jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  platform TEXT NOT NULL,
                  video_id TEXT NOT NULL,
                  url TEXT,
                  topic_id TEXT,
                  score REAL,
                  status TEXT,
                  UNIQUE(platform, video_id)
                )
                """
            )
            conn.commit()

            def enqueue(platform: str, video_id: str) -> str:
                try:
                    conn.execute(
                        "INSERT INTO jobs(platform,video_id,url,topic_id,score,status) VALUES(?,?,?,?,?,?)",
                        (platform, video_id, "u", "ai_monetize", 1.0, "pending"),
                    )
                    conn.commit()
                    return "inserted"
                except sqlite3.IntegrityError:
                    return "ignored"

            a = enqueue("youtube", "abc")
            b = enqueue("youtube", "abc")
            c = enqueue("bilibili", "abc")
            n = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            conn.close()
            ok = a == "inserted" and b == "ignored" and c == "inserted" and n == 2
            record("V8", "去重 platform+video_id", ok, f"a={a} b={b} c={c} n={n}")
    except Exception as e:
        record("V8", "去重 platform+video_id", False, repr(e))


def v9_inbox_parse() -> None:
    cases = [
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "youtube",
            "dQw4w9WgXcQ",
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ",
            "youtube",
            "dQw4w9WgXcQ",
        ),
        (
            "https://www.bilibili.com/video/BV1GJ411x7h7",
            "bilibili",
            "BV1GJ411x7h7",
        ),
        (
            "https://www.douyin.com/video/7123456789012345678",
            "douyin",
            "7123456789012345678",
        ),
        (
            "https://www.kuaishou.com/short-video/3xabcdef",
            "kuaishou",
            None,  # id pattern loose — platform detect enough
        ),
    ]

    def parse(url: str) -> tuple[str | None, str | None]:
        u = url.lower()
        if "youtube.com" in u or "youtu.be" in u:
            m = re.search(r"(?:v=|youtu\.be/)([\w-]{6,})", url)
            return "youtube", m.group(1) if m else None
        if "bilibili.com" in u or "b23.tv" in u:
            m = re.search(r"(BV[\w]+)", url, re.I)
            return "bilibili", m.group(1) if m else None
        if "douyin.com" in u or "iesdouyin.com" in u:
            m = re.search(r"/video/(\d+)", url)
            return "douyin", m.group(1) if m else None
        if "kuaishou.com" in u:
            m = re.search(r"/short-video/([\w]+)", url)
            return "kuaishou", m.group(1) if m else None
        return None, None

    fails = []
    for url, exp_p, exp_id in cases:
        p, i = parse(url)
        if p != exp_p:
            fails.append(f"platform {url} -> {p}")
        if exp_id is not None and i != exp_id:
            fails.append(f"id {url} -> {i}")
    record("V9", "inbox URL 解析", not fails, "ok" if not fails else "; ".join(fails))


def v10_fetch_media_detect() -> None:
    try:
        from fetch_media import detect_bvid, detect_platform

        checks = [
            (detect_platform("https://www.bilibili.com/video/BV1xx"), "bilibili"),
            (detect_platform("https://www.douyin.com/video/1"), "douyin"),
            (detect_platform("https://www.kuaishou.com/x"), "kuaishou"),
            (detect_platform("https://www.youtube.com/watch?v=1"), "youtube"),
            (detect_bvid("https://www.bilibili.com/video/BV1GJ411x7h7/?spm=1"), "BV1GJ411x7h7"),
        ]
        bad = [f"{a}!={b}" for a, b in checks if a != b]
        record("V10", "fetch_media.detect_* 衔接", not bad, "ok" if not bad else "; ".join(bad))
    except Exception as e:
        record("V10", "fetch_media.detect_* 衔接", False, traceback.format_exc()[-500:])


def write_report() -> None:
    must = [r for r in results if r["expected"] == "must"]
    soft = [r for r in results if r["expected"] == "allow_fail"]
    must_pass = sum(1 for r in must if r["ok"])
    soft_pass = sum(1 for r in soft if r["ok"])
    must_fail = [r for r in must if not r["ok"]]
    # allow_fail: "通" means path exercised; soft fail is ACCEPTABLE
    soft_ok_for_plan = len(soft)  # all soft items are considered validated if ran
    overall = "可通过（发现主路径）" if not must_fail else "主路径有阻塞"

    lines = [
        "# 日更发现 · 验证报告",
        "",
        f"> 生成时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}",
        f"> 脚本：`trans/verify_discover.py`",
        "",
        f"## 总评：{overall}",
        "",
        f"- 必须项：{must_pass}/{len(must)} 通过",
        f"- 允许失败项（抖音/快手）：已实测 {len(soft)} 条；其中成功 {soft_pass}，失败 {len(soft)-soft_pass}（失败则走 inbox，符合设计）",
        "",
        "## 明细",
        "",
        "| ID | 条目 | 结果 | 期望 | 详情 |",
        "|----|------|------|------|------|",
    ]
    for r in results:
        if r["ok"]:
            st = "PASS"
        elif r["expected"] == "allow_fail":
            st = "SOFT-FAIL（设计内）"
        else:
            st = "FAIL"
        detail = r["detail"].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {r['id']} | {r['title']} | {st} | {r['expected']} | {detail} |"
        )

    lines += [
        "",
        "## 结论与下一步",
        "",
    ]
    if not must_fail:
        lines += [
            "1. **YouTube + B 站 + 打分/队列/URL 解析** 可走通，可进入 `discover/` 编码。",
            "2. **抖音/快手** 无 cookie 时按设计降级 inbox；上线前补 `cookies.txt` 再复测。",
            "3. 实现顺序：queue/rank → youtube adapter → bilibili → inbox → douyin/kuaishou。",
        ]
    else:
        lines += [
            "必须项失败，需先修复网络/API/依赖后再编码发现层：",
            *[f"- {r['id']}: {r['detail'][:200]}" for r in must_fail],
        ]
    lines.append("")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[report] {REPORT}")


def main() -> int:
    print("=== discover path verification ===\n")
    v1_yt_dlp()
    v2_youtube_search()
    v3_bilibili_discover()
    v4_bilibili_view()
    v5_douyin()
    v6_kuaishou()
    v7_rank_quota()
    v8_dedupe_queue()
    v9_inbox_parse()
    v10_fetch_media_detect()
    write_report()
    must_fail = any(not r["ok"] and r["expected"] == "must" for r in results)
    return 1 if must_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
