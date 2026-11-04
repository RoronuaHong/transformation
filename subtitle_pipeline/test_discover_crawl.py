from __future__ import annotations

import json

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


def test_parse_count_kmb() -> None:
    # Telegram / YouTube expose English magnitude suffixes in visible text.
    assert parse_count("5.87M") == 5_870_000
    assert parse_count("12.3K") == 12_300
    assert parse_count("2.04M") == 2_040_000
    assert parse_count("1.5B") == 1_500_000_000
    assert parse_count("1,234K") == 1_234_000
    assert parse_count("938.5K") == 938_500


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


# --------------------------------------------------------------------------- #
# Douyin in-house (X-Bogus signed) crawler
# --------------------------------------------------------------------------- #
def test_xbogus_signature_shape_and_determinism(monkeypatch) -> None:
    from discover.adapters.xbogus import _X_BOGUS_SHORT_STR, x_bogus

    url = "https://www.douyin.com/aweme/v1/web/general/search/single/?keyword=cat&aid=6383"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    # freeze time so two calls with identical input produce identical output
    monkeypatch.setattr("discover.adapters.xbogus.time.time", lambda: 1700000000.0)
    s1 = x_bogus(url, ua)
    s2 = x_bogus(url, ua)
    assert s1 == s2
    assert len(s1) == 28  # 21 input bytes -> 28 chars in the custom base64 alphabet
    assert set(s1) <= set(_X_BOGUS_SHORT_STR)
    # different query -> different signature
    assert x_bogus(url + "&keyword=dog", ua) != s1


def test_douyin_inhouse_search_returns_candidates(monkeypatch) -> None:
    from discover.adapters.douyin import DouyinAdapter

    # pretend logged-in cookies exist (so the in-house path is attempted)
    monkeypatch.setattr(
        "discover.adapters.douyin._douyin_cookie_header",
        lambda: "ttwid=abc; sessionid=xyz",
    )

    fake_aweme = {
        "aweme_id": "70000000001",
        "desc": "萌宠合集",
        "share_url": "https://www.douyin.com/video/70000000001",
        "create_time": 1700000000,
        "author": {"nickname": "萌宠君", "sec_uid": "MS4xxx"},
        "statistics": {
            "play_count": "12.3万",
            "digg_count": "3000",
            "comment_count": "200",
            "share_count": "100",
        },
        "video": {"duration": "30000", "cover": {"url_list": ["http://img/1.jpg"]}},
    }
    body = json.dumps(
        {"status_code": 0, "data": {"aweme_list": [{"aweme_info": fake_aweme}]}}
    ).encode()

    captured: dict[str, str] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    def _fake_urlopen(req, *a, **k):
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    topic = TopicConfig(
        id="adhoc",
        locales_title={"zh": "cat"},
        fresh_hours=168,
        daily_quota_per_platform=5,
        min_views={"douyin": 0},
        keywords={"default": ["cat"], "douyin": ["cat"]},
        platforms=["douyin"],
        sources=["search"],
    )
    cands = DouyinAdapter().list_candidates(topic)
    assert len(cands) == 1
    c = cands[0]
    assert c.platform == "douyin"
    assert c.video_id == "70000000001"
    assert c.author == "萌宠君"
    assert c.views == 123000
    # the in-house path must sign the request with X-Bogus
    assert "X-Bogus=" in captured["url"]


def test_douyin_signed_get_handles_list_data_and_antispam(monkeypatch, capsys) -> None:
    """Real search endpoint returns ``data`` as a bare list, and reports anti-spam."""
    from discover.adapters import douyin as d

    fake_aweme = {
        "aweme_id": "7123456789",
        "desc": "猫",
        "statistics": {"play_count": "1000"},
        "author": {"nickname": "u"},
        "video": {"duration": "10000"},
    }

    class _Resp:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def _ok(req, *a, **k):
        return _Resp(json.dumps({"status_code": 0, "data": [fake_aweme]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", _ok)
    rows = d._signed_get(d._SEARCH_EP, {"keyword": "cat"})
    assert len(rows) == 1 and rows[0]["aweme_id"] == "7123456789"

    def _blocked(req, *a, **k):
        return _Resp(json.dumps({
            "status_code": 0,
            "data": [],
            "search_nil_info": {"search_nil_type": "antispam_check", "search_nil_item": "hit_shark"},
        }).encode())

    monkeypatch.setattr("urllib.request.urlopen", _blocked)
    assert d._signed_get(d._SEARCH_EP, {"keyword": "cat"}) == []
    assert "antispam_check" in capsys.readouterr().out


def test_douyin_cdp_bridge_pure_helpers() -> None:
    from discover.adapters.douyin_cdp import build_evaluate_expression, _parse_eval_result

    expr = build_evaluate_expression("/aweme/v1/web/hot/search/list/", {"count": "20"})
    assert "frontierSign" in expr
    assert "/aweme/v1/web/hot/search/list/" in expr
    assert '"count": "20"' in expr
    assert "AbortController" in expr  # JS-side timeout so a gated request cannot hang us

    # well-formed evaluate value -> parsed body dict
    ok = _parse_eval_result(json.dumps({"http": 200, "body": json.dumps({"status_code": 0, "data": []})}))
    assert ok == {"status_code": 0, "data": []}
    # error bubbled from the page / non-JSON body / wrong type -> None
    assert _parse_eval_result(json.dumps({"error": "boom"})) is None
    assert _parse_eval_result(json.dumps({"http": 200, "body": "not json"})) is None
    assert _parse_eval_result(None) is None
    assert _parse_eval_result("") is None


def test_douyin_no_cookies_no_service_returns_empty(monkeypatch) -> None:
    from discover.adapters.douyin import DouyinAdapter

    monkeypatch.setattr("discover.adapters.douyin._douyin_cookie_header", lambda: "")
    monkeypatch.setattr("discover.adapters.douyin.DOUYIN_API_BASE", "")
    topic = TopicConfig(
        id="adhoc",
        locales_title={"zh": "cat"},
        fresh_hours=168,
        daily_quota_per_platform=5,
        min_views={"douyin": 0},
        keywords={"default": ["cat"], "douyin": ["cat"]},
        platforms=["douyin"],
        sources=["search"],
    )
    assert DouyinAdapter().list_candidates(topic) == []


# --------------------------------------------------------------------------- #
# Telegram adapter (public-channel scrape)
# --------------------------------------------------------------------------- #
def test_cdp_cookies_anonymous_not_reported_as_logged_in() -> None:
    """ttwid / odin_tt / passport_csrf_token are issued to anonymous visitors."""
    from discover.adapters.douyin import cdp_cookies_to_netscape

    anon = [
        {"domain": ".douyin.com", "name": n, "value": "x", "expires": -1}
        for n in ("ttwid", "odin_tt", "passport_csrf_token", "__ac_nonce")
    ]
    _text, info = cdp_cookies_to_netscape(anon)
    assert info["count"] == 4
    assert info["logged_in"] is False


def test_cdp_cookies_to_netscape_filters_and_detects_login() -> None:
    from discover.adapters.douyin import cdp_cookies_to_netscape

    cdp = [
        {"domain": ".douyin.com", "name": "ttwid", "value": "abc", "expires": 1900000000, "secure": True, "path": "/"},
        # HttpOnly login cookie is invisible to document.cookie but present via CDP
        {"domain": ".douyin.com", "name": "sessionid_ss", "value": "s3cr3t", "expires": -1, "httpOnly": True, "path": "/"},
        {"domain": "other.example.com", "name": "junk", "value": "1", "expires": -1},
        {"domain": "www.douyin.com", "name": "", "value": "empty-name", "expires": -1},
        {"domain": ".douyin.com", "name": "ttwid", "value": "abc", "expires": 1900000000, "secure": True, "path": "/"},
    ]
    text, info = cdp_cookies_to_netscape(cdp)
    assert info["count"] == 2  # deduped, non-douyin + empty-name dropped
    assert info["logged_in"] is True
    assert "name=sessionid_ss" not in text and "sessionid_ss\ts3cr3t" in text
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert all(len(ln.split("\t")) == 7 for ln in lines)
    assert all("douyin" in ln.split("\t")[0] for ln in lines)


def test_get_adapter_telegram() -> None:
    from discover.adapters.base import get_adapter

    assert get_adapter("telegram").platform == "telegram"


def test_parse_platforms_allows_telegram() -> None:
    from discover.run_discover import _parse_platforms

    out = _parse_platforms("youtube,bilibili,douyin,telegram")
    assert set(out) == {"youtube", "bilibili", "douyin", "telegram"}


_SAMPLE_TG_HTML = """
<div class="tgme_widget_message ">
<a class="tgme_widget_message_date" href="https://t.me/durov/101"><time datetime="2026-11-04T12:00:00+00:00">Nov 4</time></a>
<div class="tgme_widget_message_bubble"><div class="tgme_widget_message_text js-message_text" data-ssr="1">萌宠合集 超可爱小猫</div></div>
<span class="tgme_widget_message_views" data-view="12300">12.3K</span>
</div>
<div class="tgme_widget_message ">
<a class="tgme_widget_message_date" href="https://t.me/durov/102"><time datetime="2026-11-03T09:30:00+00:00">Nov 3</time></a>
<div class="tgme_widget_message_text js-message_text" data-ssr="1">普通新闻，不相关</div>
<span class="tgme_widget_message_views" data-view="500">500</span>
</div>
"""


def test_telegram_adapter_parses_and_filters(monkeypatch) -> None:
    from discover.adapters.telegram import TelegramAdapter

    monkeypatch.setattr(
        "discover.adapters.telegram._fetch_channel_html", lambda channel: _SAMPLE_TG_HTML
    )
    topic = TopicConfig(
        id="adhoc",
        locales_title={"zh": "萌宠"},
        fresh_hours=168,
        daily_quota_per_platform=5,
        min_views={"telegram": 0},
        keywords={"default": ["萌宠"], "telegram": ["萌宠"]},
        platforms=["telegram"],
        seed_channels={"telegram": ["durov"]},
        sources=["search"],
    )
    cands = TelegramAdapter().list_candidates(topic)
    # only the post matching the keyword "萌宠" should survive
    assert len(cands) == 1
    c = cands[0]
    assert c.platform == "telegram"
    assert c.video_id == "durov/101"
    assert c.url == "https://t.me/durov/101"
    assert c.views == 12300
    assert c.published_at is not None
    assert "萌宠" in (c.title or "")


def test_parse_telegram_channel_normalizes() -> None:
    from discover.adapters.telegram import parse_telegram_channel

    assert parse_telegram_channel("@Durov") == "Durov"
    assert parse_telegram_channel("https://t.me/s/durov") == "durov"
    assert parse_telegram_channel("t.me/durov") == "durov"
    assert parse_telegram_channel("not a channel!!") is None

