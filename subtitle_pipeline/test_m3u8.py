from __future__ import annotations

from discover.run_inbox import canonical_url, parse_url
from fetch_media import (
    build_hls_ffmpeg_headers,
    detect_m3u8_id,
    detect_platform,
    hls_referer,
    is_m3u8_url,
)


M3U8 = (
    "https://img1.bbxcw.com/hls_mps/635d29886cb2d52f7cdf6566ef0b323f2a384e86/"
    "720/index.m3u8?auth_key=abc&expire=999"
)


def test_is_m3u8_url() -> None:
    assert is_m3u8_url(M3U8) is True
    assert is_m3u8_url("https://www.youtube.com/watch?v=abc") is False


def test_detect_platform_hls() -> None:
    assert detect_platform(M3U8) == "hls"


def test_detect_m3u8_id_path_hash() -> None:
    assert detect_m3u8_id(M3U8) == "635d29886cb2d52f7cdf6566ef0b323f2a384e86"


def test_parse_url_hls() -> None:
    platform, vid = parse_url(M3U8)
    assert platform == "hls"
    assert vid == "635d29886cb2d52f7cdf6566ef0b323f2a384e86"


def test_canonical_url_hls_preserves_original() -> None:
    assert canonical_url("hls", "abc", original_url=M3U8) == M3U8


def test_hls_referer_cdn_host() -> None:
    assert hls_referer(M3U8) == "https://www.bbxcw.com/"


def test_build_hls_ffmpeg_headers() -> None:
    hdr = build_hls_ffmpeg_headers(M3U8)
    assert "Referer: https://www.bbxcw.com/" in hdr
    assert "User-Agent:" in hdr
