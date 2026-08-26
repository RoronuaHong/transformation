from __future__ import annotations

from api.try_service import probe_urls


def test_probe_urls_counts_and_blocks_douyin_without_cookies(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.try_service._cookies_file_covers_platform",
        lambda _platform: False,
    )
    out = probe_urls(
        [
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://www.douyin.com/video/7123456789012345678",
        ]
    )
    assert out["valid_count"] == 2
    assert out["counts"]["bilibili"] == 1
    assert out["counts"]["douyin"] == 1
    assert out["platforms"]["bilibili"]["ok"] is True
    assert out["platforms"]["douyin"]["ok"] is False
    assert out["block_submit"] is True


def test_probe_urls_accepts_pasted_netscape_for_douyin(monkeypatch) -> None:
    monkeypatch.setattr(
        "api.try_service._cookies_file_covers_platform",
        lambda _platform: False,
    )
    pasted = "# Netscape HTTP Cookie File\n.douyin.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tabc12345"
    out = probe_urls(
        ["https://www.douyin.com/video/7123456789012345678"],
        pasted_cookies=pasted,
    )
    assert out["platforms"]["douyin"]["ok"] is True
    assert out["block_submit"] is False
