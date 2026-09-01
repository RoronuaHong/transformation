from __future__ import annotations

import pytest

from fetch_media import (
    is_bilibili_412,
    playurl_media_url,
    resolve_bilibili_api_download,
)


def test_is_bilibili_412_detects_webpage_error() -> None:
    err = (
        "ERROR: [BiliBili] 1M34y1P76V: Unable to download webpage: "
        "HTTP Error 412: Precondition Failed"
    )
    assert is_bilibili_412(err) is True
    assert is_bilibili_412("HTTP Error 403: Forbidden") is False


def test_playurl_media_url_html5() -> None:
    payload = {
        "code": 0,
        "data": {
            "format": "mp4",
            "durl": [{"url": "https://upos.bilivideo.com/ok.mp4", "size": 12}],
        },
    }
    assert playurl_media_url(payload, prefer="html5") == "https://upos.bilivideo.com/ok.mp4"


def test_playurl_media_url_best_audio() -> None:
    payload = {
        "data": {
            "dash": {
                "audio": [
                    {"id": 30216, "bandwidth": 30000, "baseUrl": "https://a.low"},
                    {"id": 30280, "bandwidth": 132000, "base_url": "https://a.high"},
                ]
            }
        }
    }
    assert playurl_media_url(payload, prefer="audio") == "https://a.high"


def test_resolve_bilibili_api_download_uses_view_and_html5(monkeypatch) -> None:
    calls: list[str] = []

    def fake_json(url: str, timeout: int = 10) -> dict:
        calls.append(url)
        if "web-interface/view" in url:
            return {
                "code": 0,
                "data": {
                    "bvid": "BV1M34y1P76V",
                    "aid": 831512936,
                    "cid": 1283420798,
                    "title": "demo title",
                    "duration": 246,
                },
            }
        if "player/playurl" in url:
            return {
                "code": 0,
                "data": {
                    "timelength": 245807,
                    "durl": [{"url": "https://upos.bilivideo.com/demo.mp4"}],
                },
            }
        raise AssertionError(url)

    monkeypatch.setattr("fetch_media.bili_get_json", fake_json)
    meta = resolve_bilibili_api_download("https://www.bilibili.com/video/BV1M34y1P76V")
    assert meta["bvid"] == "BV1M34y1P76V"
    assert meta["cid"] == 1283420798
    assert meta["title"] == "demo title"
    assert meta["duration"] == 246
    assert meta["media_url"] == "https://upos.bilivideo.com/demo.mp4"
    assert any("web-interface/view" in u for u in calls)
    assert any("platform=html5" in u for u in calls)


def test_resolve_bilibili_api_download_rejects_bad_url() -> None:
    with pytest.raises(ValueError):
        resolve_bilibili_api_download("https://www.youtube.com/watch?v=abc")


def test_is_bilibili_412_detects_blocked_by_server() -> None:
    assert is_bilibili_412("Request is blocked by server (412)") is True


def test_probe_bilibili_duration(monkeypatch) -> None:
    from fetch_media import probe_bilibili_duration

    def fake_json(url: str, timeout: int = 10) -> dict:
        assert "web-interface/view" in url
        return {
            "code": 0,
            "data": {"bvid": "BV1M34y1P76V", "title": "demo", "duration": 246, "cid": 1},
        }

    monkeypatch.setattr("fetch_media.bili_get_json", fake_json)
    out = probe_bilibili_duration("https://www.bilibili.com/video/BV1M34y1P76V")
    assert out["duration"] == 246.0
    assert out["title"] == "demo"
