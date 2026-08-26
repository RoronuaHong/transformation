"""Crawl one topic: adapters → cheap enrich → rank. No audio."""

from __future__ import annotations

import traceback

from .adapters.base import get_adapter
from .enrich import enrich_candidates, enrich_shortlist
from .models import Candidate, TopicConfig
from .rank import rank_pipeline


def crawl_topic(
    topic: TopicConfig,
    *,
    mock: bool = False,
    do_enrich: bool = True,
    max_enrich: int = 40,
) -> tuple[list[Candidate], list[Candidate], dict]:
    """Return (pool, ranked, stats)."""
    stats: dict = {"id": topic.id, "platforms": {}, "raw": 0, "errors": []}
    pool: list[Candidate] = []
    for plat in topic.platforms:
        if plat in ("douyin", "kuaishou"):
            stats["errors"].append({"platform": plat, "error": "deferred"})
            continue
        try:
            if mock:
                from .adapters.mock import mock_candidates

                rows = mock_candidates(topic, plat)
            else:
                rows = get_adapter(plat).list_candidates(topic)
            stats["platforms"][plat] = {"raw": len(rows)}
            pool.extend(rows)
        except Exception as e:
            stats["platforms"][plat] = {"raw": 0, "error": str(e)}
            stats["errors"].append(
                {
                    "platform": plat,
                    "error": f"{type(e).__name__}: {e}",
                    "trace": traceback.format_exc()[-400:],
                }
            )
    stats["raw"] = len(pool)
    stats["enriched_needed"] = sum(1 for c in pool if c.needs_enrich())
    if do_enrich and not mock and pool:
        short = enrich_shortlist(pool, topic, max_enrich)
        stats["enrich_shortlist"] = len(short)
        print(
            f"[enrich] topic={topic.id} pool={len(pool)} "
            f"need={stats['enriched_needed']} shortlist={len(short)}"
        )
        enrich_candidates(short, max_enrich=len(short))
    ranked = rank_pipeline(pool, topic)
    stats["ranked"] = len(ranked)
    return pool, ranked, stats
