from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Candidate, TopicConfig


class Adapter(ABC):
    platform: str

    @abstractmethod
    def list_candidates(self, topic: TopicConfig) -> list[Candidate]:
        """Return raw candidates for one topic (pre-rank)."""


def get_adapter(platform: str) -> Adapter:
    plat = platform.lower().strip()
    if plat == "youtube":
        from .youtube import YouTubeAdapter

        return YouTubeAdapter()
    if plat == "bilibili":
        from .bilibili import BilibiliAdapter

        return BilibiliAdapter()
    raise ValueError(f"unsupported platform (deferred or unknown): {platform}")
