from __future__ import annotations

from discover.adapters.bilibili import item_to_candidate, parse_bili_seed, title_hits_keywords
from discover.adapters.youtube import (
    entry_to_candidate,
    fill_from_watch_html,
    parse_youtube_seed,
    youtube_search_url,
)
from discover.crawl import crawl_topic
from discover.enrich import enrich_shortlist
from discover.models import Candidate, TopicConfig
from discover.parseutil import parse_count, parse_duration, parse_watch_html, unix_from_entry
from discover.rank import rank_pipeline
from discover.topics_loader import load_topics


def _topic() -> TopicConfig:
    return TopicConfig(
        id="life_hacks",
        locales_title={"zh": "生活小技巧"},
        fresh_hours=48,
        daily_quota_per_platform=2,
        min_views={"youtube": 10000, "bilibili": 10000},
        keywords={"bilibili": ["生活小技巧", "收纳"]},
        platforms=["youtube", "bilibili"],
        daily=True,
    )


def test_parse_count_wan_yi() -> None:
    assert parse_count("1.2万") == 12000
    assert parse_count("3万") == 30000
    assert parse_count("1亿") == 100_000_000
    assert parse_count("12,345") == 12345
    assert parse_count(150000) == 150000
    assert parse_count(None) is None
    assert parse_count("") is None


def test_parse_duration_iso_and_clock() -> None:
    assert parse_duration("PT1H2M3S") == 3723
    assert parse_duration("PT1M30S") == 90
    assert parse_duration("1:30") == 90
    assert parse_duration("01:02:03") == 3723
    assert parse_duration(180) == 180


def test_unix_from_entry_pubdate() -> None:
    assert unix_from_entry({"pubdate": 1724000000}) == 1724000000.0
    assert unix_from_entry({"upload_date": "20260819"}) is not None
    assert unix_from_entry({"title": "no date"}) is None


def test_parse_watch_html_json_ld() -> None:
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@type":"VideoObject","uploadDate":"2026-08-19T12:00:00",
     "duration":"PT1M30S","author":{"name":"HackChannel"},
     "interactionStatistic":[{"interactionType":{"@type":"WatchAction"},
       "userInteractionCount":12345}]}
    </script>
    </head></html>
    """
    meta = parse_watch_html(html)
    assert meta["views"] == 12345
    assert meta["duration_sec"] == 90
    assert meta["author"] == "HackChannel"
    assert meta["published_at"] is not None


def test_parse_watch_html_player_fallback() -> None:
    html = (
        '"publishDate":"2026-08-18T08:00:00",'
        '"viewCount":"99901",'
        '"lengthSeconds":"75",'
        '"ownerChannelName":"Tips",'
        '"channelId":"UCBR8-60-B28hp2BmDPdntcQ"'
    )
    meta = parse_watch_html(html)
    assert meta["views"] == 99901
    assert meta["duration_sec"] == 75
    assert meta["author"] == "Tips"
    assert meta["author_id"].startswith("UC")
    assert meta["published_at"] is not None


def test_fill_from_watch_html_only_fills_missing() -> None:
    c = Candidate(
        platform="youtube",
        video_id="abc",
        url="https://www.youtube.com/watch?v=abc",
        title="t",
        topic_id="life_hacks",
        views=10,
        published_at=None,
    )
    fill_from_watch_html(
        c,
        '"publishDate":"2026-08-19","viewCount":"88888"',
    )
    assert c.published_at is not None
    assert c.views == 10
    assert (c.raw or {}).get("enriched") == "watch_html"


def test_youtube_search_url_date_sort() -> None:
    url = youtube_search_url("life hacks")
    assert "search_query=life+hacks" in url
    assert "sp=CAI%3D" in url
    assert not url.startswith("ytsearch")


def test_parse_youtube_seed() -> None:
    assert parse_youtube_seed("UCBR8-60-B28hp2BmDPdntcQ").endswith(
        "/channel/UCBR8-60-B28hp2BmDPdntcQ/videos"
    )
    assert parse_youtube_seed("@DadhowdoI") == "https://www.youtube.com/@DadhowdoI/videos"
    assert parse_youtube_seed("https://www.youtube.com/@x").endswith("/@x/videos")
    assert parse_youtube_seed("") is None


def test_parse_bili_seed() -> None:
    assert parse_bili_seed("123456") == "123456"
    assert parse_bili_seed("https://space.bilibili.com/123456") == "123456"
    assert parse_bili_seed("https://space.bilibili.com/123456?spm=1") == "123456"
    assert parse_bili_seed("nope") is None


def test_bili_play_count_wan() -> None:
    topic = _topic()
    cand = item_to_candidate(
        {
            "bvid": "BV1xx",
            "title": "生活小技巧",
            "play": "1.2万",
            "like": "300",
            "pubdate": 1724000000,
            "duration": 90,
            "author": "up",
            "mid": 99,
        },
        topic,
        query="生活小技巧",
        source="search",
    )
    assert cand is not None
    assert cand.views == 12000
    assert cand.likes == 300
    assert cand.author == "up"
    assert cand.author_id == "99"
    assert cand.published_at == 1724000000.0


def test_entry_to_candidate_skips_deleted() -> None:
    topic = _topic()
    gone = entry_to_candidate(
        {"id": "x", "title": "[Deleted video]"},
        topic,
        query="life hacks",
        source="ytsearch",
    )
    assert gone is None
    ok = entry_to_candidate(
        {
            "id": "abc",
            "title": "Kitchen hack",
            "view_count": 50000,
            "duration": 80,
            "uploader": "Ch",
            "channel_id": "UCabc",
        },
        topic,
        query="kitchen hacks",
        source="results_date",
    )
    assert ok is not None
    assert ok.published_at is None
    assert ok.needs_enrich() is False
    assert ok.url.endswith("watch?v=abc")


def test_title_hits_keywords() -> None:
    assert title_hits_keywords("超实用生活小技巧合集", ["生活小技巧"])
    assert not title_hits_keywords("游戏攻略", ["生活小技巧", "收纳"])


def test_yaml_home_tips_mvp() -> None:
    topics = load_topics()
    assert len(topics) == 1
    t = topics[0]
    assert t.id == "home_tips"
    assert t.daily is False
    assert t.score_mode == "views"
    assert t.quota_scope == "total"
    assert t.daily_quota_per_platform == 5
    assert t.min_duration_sec == 180
    assert t.fresh_hours == 0
    assert t.sources == ["search"]


def test_rank_views_top10_total_skips_shorts() -> None:
    import time

    now = time.time()
    topic = TopicConfig(
        id="weekly_excel",
        locales_title={"zh": "Excel"},
        fresh_hours=0,
        daily_quota_per_platform=10,
        quota_scope="total",
        score_mode="views",
        min_duration_sec=120,
        min_views={"youtube": 0, "bilibili": 0},
        keywords={"default": ["Excel"]},
        platforms=["youtube", "bilibili"],
    )
    pool = []
    for i in range(12):
        pool.append(
            Candidate(
                platform="youtube" if i % 2 == 0 else "bilibili",
                video_id=f"v{i}",
                url=f"https://example.com/{i}",
                title=f"t{i}",
                topic_id="weekly_excel",
                published_at=None,
                views=(i + 1) * 1000,
                duration_sec=180,
            )
        )
    pool.append(
        Candidate(
            platform="youtube",
            video_id="short",
            url="https://example.com/short",
            title="short",
            topic_id="weekly_excel",
            views=9_000_000,
            duration_sec=30,
        )
    )
    ranked = rank_pipeline(pool, topic, now=now)
    assert len(ranked) == 10
    assert ranked[0].video_id == "v11"
    assert ranked[-1].video_id == "v2"
    assert "short" not in {c.video_id for c in ranked}


def test_rank_drops_undated_even_if_viral() -> None:
    import time

    now = time.time()
    topic = _topic()
    undated = Candidate(
        platform="youtube",
        video_id="nodate",
        url="https://www.youtube.com/watch?v=nodate",
        title="nodate",
        topic_id="life_hacks",
        published_at=None,
        views=99_000_000,
        duration_sec=60,
    )
    ranked = rank_pipeline([undated], topic, now=now)
    assert ranked == []


def test_rank_quota_and_fresh() -> None:
    import time

    now = time.time()
    topic = _topic()
    pool = [
        Candidate(
            platform="youtube",
            video_id="old",
            url="https://www.youtube.com/watch?v=old",
            title="old",
            topic_id="life_hacks",
            published_at=now - 400 * 3600,
            views=9_000_000,
            duration_sec=60,
        ),
        Candidate(
            platform="youtube",
            video_id="hot",
            url="https://www.youtube.com/watch?v=hot",
            title="hot",
            topic_id="life_hacks",
            published_at=now - 5 * 3600,
            views=80_000,
            duration_sec=90,
        ),
        Candidate(
            platform="youtube",
            video_id="low",
            url="https://www.youtube.com/watch?v=low",
            title="low",
            topic_id="life_hacks",
            published_at=now - 5 * 3600,
            views=100,
            duration_sec=90,
        ),
        Candidate(
            platform="bilibili",
            video_id="BVhot",
            url="https://www.bilibili.com/video/BVhot",
            title="b",
            topic_id="life_hacks",
            published_at=now - 3 * 3600,
            views=50_000,
            duration_sec=120,
        ),
    ]
    ranked = rank_pipeline(pool, topic, now=now)
    ids = {c.video_id for c in ranked}
    assert "hot" in ids
    assert "BVhot" in ids
    assert "old" not in ids
    assert "low" not in ids


def test_enrich_shortlist_caps_and_prefers_views() -> None:
    topic = _topic()
    topic.daily_quota_per_platform = 2
    pool = [
        Candidate(
            platform="youtube",
            video_id=f"yt{i}",
            url=f"https://www.youtube.com/watch?v=yt{i}",
            title="t",
            topic_id="life_hacks",
            views=i * 1000,
        )
        for i in range(1, 20)
    ]
    short = enrich_shortlist(pool, topic, max_enrich=3)
    assert len(short) == 3
    assert [c.video_id for c in short] == ["yt19", "yt18", "yt17"]


def test_crawl_topic_mock_no_network() -> None:
    topic = _topic()
    pool, ranked, stats = crawl_topic(topic, mock=True, do_enrich=True)
    assert stats["raw"] == 4
    assert stats["platforms"]["youtube"]["raw"] == 3
    ids = {c.video_id for c in ranked}
    assert "mock_yt_hi" in ids
    assert "mock_yt_old" not in ids
    assert "BVmock001" in ids
    assert all((c.raw or {}).get("enriched") is None for c in pool)
