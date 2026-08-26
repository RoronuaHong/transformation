#!/usr/bin/env python3
"""Quick multi-platform fetch verification after JS runtime fix."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "subtitle_pipeline"))
from fetch_media import fetch_to_wav  # noqa: E402

OUT = ROOT / "e2e_out" / "platform_fetch_verify"
OUT.mkdir(parents=True, exist_ok=True)
rows: list[dict] = []


def record(platform: str, ok: bool, detail: str) -> None:
    rows.append({"platform": platform, "ok": ok, "detail": detail[:500]})
    print(f"[{'PASS' if ok else 'FAIL'}] {platform}: {detail[:200]}")


def bili_url() -> str:
    ua = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com",
    }
    req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/popular?ps=1", headers=ua
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    bvid = data["data"]["list"][0]["bvid"]
    return f"https://www.bilibili.com/video/{bvid}"


def try_fetch(platform: str, url: str, cookies_from_browser: str | None = None) -> None:
    dest = OUT / platform
    dest.mkdir(exist_ok=True)
    try:
        meta = fetch_to_wav(
            url,
            dest,
            cookies_from_browser=cookies_from_browser,
            force=True,
        )
        wav = Path(meta["wav"])
        ok = wav.exists() and wav.stat().st_size > 1000
        record(
            platform,
            ok,
            f"url={url} wav={wav.name} bytes={wav.stat().st_size} title={meta.get('title')!r}",
        )
    except Exception as e:
        record(platform, False, f"url={url} err={type(e).__name__}: {e}")


def main() -> int:
    # YouTube — known short, no cookies
    try_fetch("youtube", "https://www.youtube.com/watch?v=jNQXAC9IVRw", None)

    # Bilibili — popular, no cookies (retry path inside fetch_media)
    try:
        url = bili_url()
        try_fetch("bilibili", url, None)
    except Exception as e:
        record("bilibili", False, f"pick url failed: {e}")

    # Douyin / Kuaishou — expect fail without share URL + cookies
    try_fetch(
        "douyin",
        "https://www.douyin.com/video/7123456789012345678",
        None,
    )
    try_fetch(
        "kuaishou",
        "https://www.kuaishou.com/short-video/3xabcdefghi",
        None,
    )

    report = {
        "rows": rows,
        "summary": {
            "pass": sum(1 for r in rows if r["ok"]),
            "fail": sum(1 for r in rows if not r["ok"]),
        },
    }
    path = OUT / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ROOT / "平台取流验证.md"
    lines = [
        "# 平台取流验证",
        "",
        f"> 输出目录：`{OUT}`",
        "",
        "| 平台 | 结果 | 详情 |",
        "|------|------|------|",
    ]
    for r in rows:
        d = r["detail"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['platform']} | {'PASS' if r['ok'] else 'FAIL'} | {d} |")
    lines += [
        "",
        "## 说明",
        "",
        "- YouTube / B 站：无人值守取流应 PASS。",
        "- 抖音 / 快手：占位/无效链或无 cookie 时 FAIL 属预期，需真实分享链 + `cookies.txt`。",
        "",
    ]
    md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[report] {md}")
    # success if youtube+bilibili pass
    yt_bili = [r for r in rows if r["platform"] in ("youtube", "bilibili")]
    return 0 if all(r["ok"] for r in yt_bili) else 1


if __name__ == "__main__":
    raise SystemExit(main())
