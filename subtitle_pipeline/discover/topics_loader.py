from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import TopicConfig

DEFAULT_TOPICS = Path(__file__).resolve().parent / "topics.yaml"


def load_topics(path: Path | None = None) -> list[TopicConfig]:
    p = Path(path) if path else DEFAULT_TOPICS
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    rows = data.get("topics") or []
    out: list[TopicConfig] = []
    for raw in rows:
        out.append(_parse_topic(raw))
    return out


def load_topic_by_id(topic_id: str, path: Path | None = None) -> TopicConfig:
    for t in load_topics(path):
        if t.id == topic_id:
            return t
    raise KeyError(f"unknown topic_id: {topic_id}")


def _parse_topic(raw: dict[str, Any]) -> TopicConfig:
    kw = raw.get("keywords") or {}
    keywords: dict[str, list[str]] = {}
    for k, v in kw.items():
        if isinstance(v, list):
            keywords[str(k)] = [str(x) for x in v]
        elif isinstance(v, str):
            keywords[str(k)] = [v]
    return TopicConfig(
        id=str(raw["id"]),
        locales_title={str(k): str(v) for k, v in (raw.get("locales_title") or {}).items()},
        fresh_hours=float(raw["fresh_hours"]) if raw.get("fresh_hours") is not None else 48.0,
        daily_quota_per_platform=int(raw.get("daily_quota_per_platform") or 3),
        min_views={str(k): int(v) for k, v in (raw.get("min_views") or {}).items()},
        keywords=keywords,
        platforms=[str(x) for x in (raw.get("platforms") or ["youtube", "bilibili"])],
        exclude_authors=[str(x) for x in (raw.get("exclude_authors") or [])],
        seed_channels={
            str(k): [str(x) for x in (v or [])]
            for k, v in (raw.get("seed_channels") or {}).items()
        },
        score_mode=str(raw.get("score_mode") or "views"),
        quota_scope=str(raw.get("quota_scope") or "total"),
        min_duration_sec=int(raw.get("min_duration_sec") or 0),
        search_mode=str(raw.get("search_mode") or "relevance"),
        bili_search_order=str(raw.get("bili_search_order") or "totalrank"),
        sources=[str(x) for x in (raw.get("sources") or ["search"])],
        daily=bool(raw["daily"]) if "daily" in raw else True,
    )
