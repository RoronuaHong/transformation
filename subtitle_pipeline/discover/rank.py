from __future__ import annotations

import math
import time
from collections import defaultdict

from .models import Candidate, TopicConfig


def age_hours(published_at: float | None, now: float | None = None) -> float:
    if published_at is None:
        return 1.0
    ts = now if now is not None else time.time()
    return max((ts - float(published_at)) / 3600.0, 1.0)


def score_candidate(c: Candidate, topic: TopicConfig, *, now: float | None = None) -> float:
    views = int(c.views or 0)
    if topic.score_mode == "views":
        return float(views)
    age_h = age_hours(c.published_at, now)
    velocity = views / age_h
    eng = int(c.likes or 0) + 2 * int(c.comments or 0)
    w_v = 3.5 if topic.score_mode == "velocity_first" else 2.5
    return (
        1.0 * math.log1p(views)
        + w_v * math.log1p(velocity)
        + 0.5 * math.log1p(eng)
    )


def filter_fresh(
    candidates: list[Candidate],
    topic: TopicConfig,
    *,
    now: float | None = None,
) -> list[Candidate]:
    if float(topic.fresh_hours) <= 0:
        return list(candidates)
    ts = now if now is not None else time.time()
    out: list[Candidate] = []
    for c in candidates:
        if c.published_at is None:
            continue
        if (ts - float(c.published_at)) / 3600.0 > topic.fresh_hours:
            continue
        out.append(c)
    return out


def filter_min_duration(candidates: list[Candidate], topic: TopicConfig) -> list[Candidate]:
    floor = int(topic.min_duration_sec or 0)
    if floor <= 0:
        return candidates
    out: list[Candidate] = []
    for c in candidates:
        if c.duration_sec is None:
            continue
        if int(c.duration_sec) < floor:
            continue
        out.append(c)
    return out


def filter_min_views(candidates: list[Candidate], topic: TopicConfig) -> list[Candidate]:
    out: list[Candidate] = []
    for c in candidates:
        if c.views is None:
            continue
        floor = topic.min_views_for(c.platform)
        if int(c.views) < floor:
            continue
        out.append(c)
    return out


def filter_exclude_authors(candidates: list[Candidate], topic: TopicConfig) -> list[Candidate]:
    if not topic.exclude_authors:
        return candidates
    ban = {a.lower() for a in topic.exclude_authors}
    return [c for c in candidates if not (c.author and c.author.lower() in ban)]


def apply_scores(
    candidates: list[Candidate],
    topic: TopicConfig,
    *,
    now: float | None = None,
) -> list[Candidate]:
    for c in candidates:
        c.score = score_candidate(c, topic, now=now)
    return candidates


def take_quota(
    candidates: list[Candidate],
    topic: TopicConfig,
) -> list[Candidate]:
    """Top-N by score. Default: N across platforms (keyword Top 10)."""
    n = max(0, int(topic.daily_quota_per_platform))
    if topic.quota_scope != "platform":
        rows = sorted(candidates, key=lambda x: x.score, reverse=True)
        return rows[:n]
    by_plat: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        by_plat[c.platform].append(c)
    out: list[Candidate] = []
    for _plat, rows in by_plat.items():
        rows.sort(key=lambda x: x.score, reverse=True)
        out.extend(rows[:n])
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def rank_pipeline(
    candidates: list[Candidate],
    topic: TopicConfig,
    *,
    now: float | None = None,
) -> list[Candidate]:
    pool = filter_exclude_authors(candidates, topic)
    pool = filter_fresh(pool, topic, now=now)
    pool = filter_min_duration(pool, topic)
    pool = filter_min_views(pool, topic)
    pool = apply_scores(pool, topic, now=now)
    return take_quota(pool, topic)
