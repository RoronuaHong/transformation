from __future__ import annotations

import time

from ..models import Candidate, TopicConfig


def mock_candidates(topic: TopicConfig, platform: str) -> list[Candidate]:
    """Offline fixtures for smoke tests (no network)."""
    now = time.time()
    kw = (topic.keywords_for(platform) or ["mock"])[0]
    if platform == "youtube":
        return [
            Candidate(
                platform="youtube",
                video_id="mock_yt_hi",
                url="https://www.youtube.com/watch?v=mock_yt_hi",
                title="Mock AI monetization high velocity",
                topic_id=topic.id,
                author="mock_channel",
                author_id="UC_mock",
                published_at=now - 6 * 3600,
                views=200_000,
                likes=500,
                comments=20,
                duration_sec=180,
                query=kw,
            ),
            Candidate(
                platform="youtube",
                video_id="mock_yt_mid",
                url="https://www.youtube.com/watch?v=mock_yt_mid",
                title="Mock mid",
                topic_id=topic.id,
                author="mid_ch",
                author_id="UC_mid",
                published_at=now - 12 * 3600,
                views=80_000,
                likes=100,
                comments=10,
                duration_sec=240,
                query=kw,
            ),
            Candidate(
                platform="youtube",
                video_id="mock_yt_old",
                url="https://www.youtube.com/watch?v=mock_yt_old",
                title="Mock too old",
                topic_id=topic.id,
                published_at=now - 400 * 3600,
                views=10_000_000,
                duration_sec=600,
                query=kw,
            ),
        ]
    if platform == "bilibili":
        return [
            Candidate(
                platform="bilibili",
                video_id="BVmock001",
                url="https://www.bilibili.com/video/BVmock001",
                title="Mock B站 AI变现",
                topic_id=topic.id,
                author="mock_up",
                author_id="12345",
                published_at=now - 8 * 3600,
                views=150_000,
                likes=200,
                comments=30,
                coins=50,
                favorites=80,
                shares=10,
                duration_sec=300,
                query=kw,
            ),
        ]
    return []
