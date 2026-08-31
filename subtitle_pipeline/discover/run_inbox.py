#!/usr/bin/env python3
"""CLI: manually enqueue a URL into the discover queue."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discover.models import Candidate
from discover.queue_db import QueueDB
from discover.topics_loader import load_topics
from fetch_media import detect_bvid, detect_douyin_id, detect_m3u8_id, detect_platform


def detect_youtube_id(url: str) -> str | None:
    m = re.search(r"(?:v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{6,})", url)
    return m.group(1) if m else None


def parse_url(url: str) -> tuple[str, str]:
    platform = detect_platform(url)
    if platform == "youtube":
        vid = detect_youtube_id(url)
        if not vid:
            raise ValueError(f"cannot parse youtube id from: {url}")
        return platform, vid
    if platform == "bilibili":
        bvid = detect_bvid(url)
        if not bvid:
            raise ValueError(f"cannot parse bvid from: {url}")
        return platform, bvid
    if platform == "douyin":
        did = detect_douyin_id(url)
        if not did:
            raise ValueError(f"cannot parse douyin id from: {url}")
        return platform, did
    if platform == "hls":
        mid = detect_m3u8_id(url)
        if not mid:
            raise ValueError(f"cannot parse m3u8 id from: {url}")
        return platform, mid
    raise ValueError(
        f"unsupported platform for inbox (use youtube|bilibili|douyin|hls): {platform}"
    )


def canonical_url(platform: str, video_id: str, *, original_url: str | None = None) -> str:
    if platform == "hls":
        if not original_url:
            raise ValueError("hls canonical_url requires original_url")
        return original_url.strip()
    if platform == "youtube":
        return f"https://www.youtube.com/watch?v={video_id}"
    if platform == "douyin":
        if video_id.isdigit():
            return f"https://www.douyin.com/video/{video_id}"
        return f"https://v.douyin.com/{video_id}"
    return f"https://www.bilibili.com/video/{video_id}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Enqueue a video URL into pending queue")
    p.add_argument("--url", required=True)
    p.add_argument("--topic", required=True, help="topic id label stored on the job")
    p.add_argument("--topics", type=Path, default=None)
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--title", default=None)
    args = p.parse_args(argv)

    topic_id = args.topic.strip()
    if not topic_id:
        print("error: --topic is empty", file=sys.stderr)
        return 1
    known = {t.id for t in load_topics(args.topics)}
    if known and topic_id not in known:
        print(f"[inbox] topic={topic_id!r} not in yaml, store as label")
    original_url = args.url.strip()
    platform, video_id = parse_url(original_url)
    c = Candidate(
        platform=platform,
        video_id=video_id,
        url=canonical_url(platform, video_id, original_url=original_url),
        original_url=original_url,
        title=args.title or f"inbox:{platform}:{video_id}",
        topic_id=topic_id,
        score=9999.0,
    )
    db = QueueDB(args.db)
    try:
        result = db.enqueue(c, priority="high")
        print(f"[inbox] {result} {platform} {video_id} topic={topic_id}")
        print(f"[queue] {db.path} status={db.count_by_status()}")
    finally:
        db.close()
    return 0 if result in ("inserted", "ignored", "skipped_done") else 1


if __name__ == "__main__":
    raise SystemExit(main())
