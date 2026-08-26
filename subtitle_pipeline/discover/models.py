from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Candidate:
    platform: str  # youtube | bilibili
    video_id: str
    url: str
    title: str
    topic_id: str
    original_url: str | None = None  # as found / pasted, before canonicalize
    author: str | None = None
    author_id: str | None = None  # YT channel_id / B站 mid
    published_at: float | None = None  # unix seconds
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    coins: int | None = None  # bilibili
    favorites: int | None = None  # bilibili favorite
    shares: int | None = None
    duration_sec: int | None = None
    thumb_url: str | None = None
    description: str | None = None
    query: str | None = None  # search keyword that found this
    score: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def needs_enrich(self) -> bool:
        """True if play-count ranking still lacks views / duration / author."""
        if self.views is None or self.duration_sec is None:
            return True
        if not self.author:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TopicConfig:
    id: str
    locales_title: dict[str, str]
    fresh_hours: float
    daily_quota_per_platform: int
    min_views: dict[str, int]
    keywords: dict[str, list[str]]
    platforms: list[str]
    exclude_authors: list[str] = field(default_factory=list)
    seed_channels: dict[str, list[str]] = field(default_factory=dict)
    score_mode: str = "views"  # views | default | velocity_first
    quota_scope: str = "total"  # total | platform
    min_duration_sec: int = 0
    search_mode: str = "relevance"  # relevance | date
    bili_search_order: str = "totalrank"
    sources: list[str] = field(default_factory=lambda: ["search"])
    daily: bool = True

    def keywords_for(self, platform: str) -> list[str]:
        if platform in self.keywords and self.keywords[platform]:
            return list(self.keywords[platform])
        return list(self.keywords.get("default") or [])

    def min_views_for(self, platform: str) -> int:
        if platform in self.min_views:
            return int(self.min_views[platform])
        return int(next(iter(self.min_views.values()), 0))


def make_adhoc_topic(
    query: str,
    *,
    platforms: list[str] | None = None,
    topic_id: str = "adhoc",
    fresh_hours: float = 168,
    quota: int = 5,
    min_views: int = 0,
) -> TopicConfig:
    """Ephemeral topic driven by a single search phrase."""
    q = query.strip()
    plats = platforms or ["youtube", "bilibili"]
    return TopicConfig(
        id=topic_id,
        locales_title={"zh": q, "en": q},
        fresh_hours=fresh_hours,
        daily_quota_per_platform=quota,
        min_views={p: min_views for p in plats},
        keywords={"default": [q], "youtube": [q], "bilibili": [q]},
        platforms=plats,
        score_mode="views",
        quota_scope="total",
        search_mode="relevance",
        bili_search_order="totalrank",
        sources=["search"],
    )
