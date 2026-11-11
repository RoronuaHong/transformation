from __future__ import annotations

from abc import ABC, abstractmethod
from contextvars import ContextVar

from ..models import Candidate, TopicConfig

# Per-search credentials. The crawler console accepts YouTube / Douyin tokens
# with each request; keeping them in os.environ leaks one operator's token into
# concurrent searches. Adapters read this first and fall back to process env.
REQUEST_CREDENTIALS: ContextVar[dict[str, str]] = ContextVar(
    "request_credentials", default={}
)


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
    if plat == "douyin":
        from .douyin import DouyinAdapter

        return DouyinAdapter()
    if plat == "telegram":
        from .telegram import TelegramAdapter

        return TelegramAdapter()
    raise ValueError(f"unsupported platform (deferred or unknown): {platform}")
