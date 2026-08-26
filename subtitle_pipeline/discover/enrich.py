"""Fill missing ranking fields. No yt-dlp format extract; watch HTML / view API only."""

from __future__ import annotations

from collections import defaultdict

from .models import Candidate, TopicConfig


def enrich_shortlist(
    pool: list[Candidate],
    topic: TopicConfig,
    max_enrich: int,
) -> list[Candidate]:
    """Cap detail fetches per platform (watch/view calls, not ASR)."""
    by: dict[str, list[Candidate]] = defaultdict(list)
    for c in pool:
        if c.needs_enrich():
            by[c.platform].append(c)
    cap = max(8, int(topic.daily_quota_per_platform) * 3)
    picked: list[Candidate] = []
    order = list(topic.platforms) + [p for p in by if p not in topic.platforms]
    for plat in order:
        room = min(cap, max_enrich - len(picked))
        if room <= 0:
            break
        rows = list(by.get(plat) or [])
        rows.sort(key=lambda c: int(c.views or 0), reverse=True)
        picked.extend(rows[:room])
    return picked[:max_enrich]


def enrich_candidates(
    candidates: list[Candidate],
    *,
    max_enrich: int = 40,
    sleep_sec: float = 0.0,
) -> list[Candidate]:
    del sleep_sec  # adapters pace their own calls
    yt = [c for c in candidates if c.platform == "youtube"][:max_enrich]
    bili = [c for c in candidates if c.platform == "bilibili"][:max_enrich]
    if yt:
        from .adapters.youtube import fill_youtube_meta

        print(f"[enrich] youtube watch-html n={len(yt)}")
        fill_youtube_meta(yt)
    if bili:
        from .adapters.bilibili import fill_bilibili_meta

        print(f"[enrich] bilibili view n={len(bili)}")
        fill_bilibili_meta(bili)
    return candidates
