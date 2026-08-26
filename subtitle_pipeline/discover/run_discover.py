#!/usr/bin/env python3
"""CLI: discover candidates → enrich metadata → rank → enqueue SQLite.

Discovery stores links + video info only. Audio is fetched later by yarn batch.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discover.crawl import crawl_topic
from discover.models import make_adhoc_topic
from discover.queue_db import QueueDB
from discover.topics_loader import load_topics


def _parse_platforms(s: str | None) -> list[str]:
    if not s:
        return ["youtube", "bilibili"]
    out: list[str] = []
    for part in s.split(","):
        p = part.strip().lower()
        if p in ("youtube", "bilibili"):
            out.append(p)
        elif p in ("douyin", "kuaishou"):
            print(f"[warn] platform deferred, skip: {p}", file=sys.stderr)
    return out or ["youtube", "bilibili"]


def _top_rows(ranked: list, n: int = 12) -> list[dict]:
    return [
        {
            "platform": c.platform,
            "video_id": c.video_id,
            "score": round(c.score, 3),
            "views": c.views,
            "duration_sec": c.duration_sec,
            "author": c.author,
            "author_id": c.author_id,
            "title": (c.title or "")[:80],
            "url": c.url,
            "query": c.query,
            "published_at": c.published_at,
            "likes": c.likes,
            "comments": c.comments,
        }
        for c in ranked[:n]
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Daily discover (YouTube+Bilibili) → metadata queue (no audio)"
    )
    p.add_argument("--topics", type=Path, default=None, help="topics.yaml path")
    p.add_argument("--topic", action="append", dest="topic_ids", help="only these topic ids")
    p.add_argument(
        "--all-topics",
        action="store_true",
        help="run every topic in yaml (default: only daily: true)",
    )
    p.add_argument(
        "--query",
        default=None,
        help="adhoc search phrase (overrides --topic list; uses ephemeral topic)",
    )
    p.add_argument(
        "--platforms",
        default=None,
        help="comma list for --query or to filter topic platforms: youtube,bilibili",
    )
    p.add_argument("--quota", type=int, default=5, help="adhoc daily_quota_per_platform")
    p.add_argument("--fresh-hours", type=float, default=168, help="adhoc fresh window")
    p.add_argument("--min-views", type=int, default=0, help="adhoc min views floor")
    p.add_argument("--db", type=Path, default=None, help="queue sqlite path")
    p.add_argument("--dry-run", action="store_true", help="rank only, do not enqueue")
    p.add_argument("--mock", action="store_true", help="skip network; inject mock candidates")
    p.add_argument(
        "--no-enrich",
        action="store_true",
        help="skip detail fill (faster; fields may be thin)",
    )
    p.add_argument("--max-enrich", type=int, default=40, help="max watch/view fills per topic")
    p.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT / "downloads" / "reports" / "discover",
        help="write daily JSON report here",
    )
    args = p.parse_args(argv)

    if args.query:
        plats = _parse_platforms(args.platforms)
        topics = [
            make_adhoc_topic(
                args.query,
                platforms=plats,
                fresh_hours=args.fresh_hours,
                quota=args.quota,
                min_views=args.min_views,
            )
        ]
    else:
        topics = load_topics(args.topics)
        if args.topic_ids:
            want = set(args.topic_ids)
            topics = [t for t in topics if t.id in want]
            if not topics:
                print("error: no matching topics", file=sys.stderr)
                return 1
        elif not args.all_topics:
            topics = [t for t in topics if t.daily]
            if not topics:
                print("[discover] skip: no daily topics in yaml")
                return 0
        if args.platforms:
            want_p = set(_parse_platforms(args.platforms))
            for t in topics:
                t.platforms = [x for x in t.platforms if x in want_p] or list(want_p)

    report: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(args.dry_run),
        "query": args.query,
        "note": "metadata only; audio via yarn batch",
        "topics": [],
        "errors": [],
        "enqueued": 0,
        "ignored": 0,
        "skipped_done": 0,
    }

    db = None if args.dry_run else QueueDB(args.db)
    try:
        for topic in topics:
            _pool, ranked, stats = crawl_topic(
                topic,
                mock=bool(args.mock),
                do_enrich=not args.no_enrich,
                max_enrich=args.max_enrich,
            )
            topic_rep = {
                "id": stats["id"],
                "platforms": stats["platforms"],
                "raw": stats["raw"],
                "enriched_needed": stats.get("enriched_needed", 0),
                "enrich_shortlist": stats.get("enrich_shortlist", 0),
                "ranked": stats["ranked"],
                "inserted": 0,
                "top": _top_rows(ranked),
            }
            report["errors"].extend(
                [{"topic": topic.id, **e} for e in stats.get("errors") or []]
            )
            if db is not None:
                for c in ranked:
                    r = db.enqueue(c)
                    if r == "inserted":
                        topic_rep["inserted"] += 1
                        report["enqueued"] += 1
                    elif r == "ignored":
                        report["ignored"] += 1
                    elif r == "skipped_done":
                        report["skipped_done"] += 1
            report["topics"].append(topic_rep)
            print(
                f"[discover] topic={topic.id} raw={stats['raw']} ranked={len(ranked)} "
                f"inserted={topic_rep['inserted']}"
            )
    finally:
        if db is not None:
            print(f"[queue] {db.path} status={db.count_by_status()}")
            db.close()

    args.report_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    out = args.report_dir / f"{day}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[report] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
