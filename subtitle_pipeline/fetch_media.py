#!/usr/bin/env python3
"""Download media URL → local 16 kHz mono WAV via yt-dlp (multi-site).

Primary path for Bilibili / Douyin / Kuaishou / YouTube / etc.
Does not use local LLMs. Failures are expected when a site changes;
keep yt-dlp updated and pass cookies when needed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse


def clear_proxy_env() -> None:
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(key, None)


def _port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _local_proxy_url() -> str | None:
    """Prefer working local proxies (Clash/V2Ray). YouTube needs this in CN."""
    # socks5:10808 worked on this machine; http:10809 often 403s
    candidates = (
        ("socks5", "127.0.0.1", 10808),
        ("http", "127.0.0.1", 10809),
        ("http", "127.0.0.1", 7890),
        ("socks5", "127.0.0.1", 7891),
    )
    for scheme, host, port in candidates:
        if _port_open(host, port):
            return f"{scheme}://{host}:{port}"
    return None


def _youtube_proxy_url() -> str | None:
    """Prefer socks5:10808 even if env HTTP_PROXY points at flaky 10809."""
    if _port_open("127.0.0.1", 10808):
        chosen = "socks5://127.0.0.1:10808"
        env_px = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("ALL_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
            or os.environ.get("all_proxy")
        )
        if env_px and env_px.rstrip("/") != chosen:
            print(f"[fetch] prefer {chosen} over env proxy {env_px}")
        return chosen
    env_px = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("http_proxy")
        or os.environ.get("all_proxy")
    )
    if env_px and "10809" in env_px:
        print(f"[fetch] skip flaky env proxy {env_px}")
        env_px = None
    return env_px or _local_proxy_url()


def find_ffmpeg() -> str:
    from pipeline import find_ffmpeg as _find

    return _find()


def _file_has_video_stream(path: Path) -> bool:
    """True when ffmpeg reports a video stream (audio-only .webm must not count)."""
    try:
        ffmpeg = find_ffmpeg()
    except FileNotFoundError:
        return False
    try:
        r = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except Exception:
        return False
    blob = f"{r.stderr or ''}\n{r.stdout or ''}"
    return bool(re.search(r"Stream #.*Video:", blob))


def detect_bvid(url: str) -> str | None:
    m = re.search(r"(BV[\w]+)", url, re.I)
    return m.group(1) if m else None


def detect_douyin_id(url: str) -> str | None:
    """Numeric aweme id, or short-link code (v.douyin.com/xxx)."""
    m = re.search(r"(?:/video/|/share/video/)(\d{6,})", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&](?:modal_id|aweme_id)=(\d{6,})", url, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(?:v\.douyin\.com|iesdouyin\.com)/([A-Za-z0-9_-]+)", url, re.I)
    if m:
        code = m.group(1)
        if code.lower() not in ("share", "video", "user", "note"):
            return code
    return None


def is_m3u8_url(url: str) -> bool:
    u = (url or "").strip().lower()
    if not u.startswith(("http://", "https://")):
        return False
    path = urlparse(u).path.lower()
    return path.endswith(".m3u8") or ".m3u8?" in u


def detect_m3u8_id(url: str) -> str | None:
    """Stable id for HLS playlist URLs (path hash or sha256 of path)."""
    if not is_m3u8_url(url):
        return None
    m = re.search(r"/(?:hls_mps|hls)/([a-f0-9]{20,64})/", url, re.I)
    if m:
        return m.group(1)[:40]
    p = urlparse(url.strip())
    base = f"{p.scheme}://{p.netloc}{p.path}"
    return hashlib.sha256(base.encode()).hexdigest()[:16]


def detect_platform(url: str) -> str:
    u = url.lower()
    if is_m3u8_url(url):
        return "hls"
    if "bilibili.com" in u or "b23.tv" in u:
        return "bilibili"
    if "douyin.com" in u or "iesdouyin.com" in u:
        return "douyin"
    if "kuaishou.com" in u or "gifshow.com" in u:
        return "kuaishou"
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    return "generic"


def hls_referer(url: str) -> str:
    """Guess Referer for CDN-hosted HLS (img1.example.com → https://www.example.com/)."""
    override = (os.environ.get("VITUAL_HLS_REFERER") or "").strip()
    if override:
        return override
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("img") and "." in host:
        _, rest = host.split(".", 1)
        return f"https://www.{rest}/"
    if host.startswith("cdn.") and "." in host:
        _, rest = host.split(".", 1)
        return f"https://www.{rest}/"
    return f"https://{host}/"


def _cookie_header_for_url(cookies_file: Path | None, url: str) -> str | None:
    if cookies_file is None or not cookies_file.is_file():
        return None
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    pairs: list[str] = []
    for ln in cookies_file.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = ln.split("\t")
        if len(parts) < 7:
            continue
        domain, _, _path, _secure, _exp, name, value = parts[:7]
        d = domain.lstrip(".").lower()
        if host == d or host.endswith("." + d) or d in host:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs) if pairs else None


def build_hls_ffmpeg_headers(url: str, *, cookies_file: Path | None = None) -> str:
    referer = hls_referer(url)
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    lines = [f"Referer: {referer}", f"User-Agent: {ua}"]
    cookie = _cookie_header_for_url(cookies_file, url)
    if cookie:
        lines.append(f"Cookie: {cookie}")
    return "\r\n".join(lines) + "\r\n"


def _parse_ffmpeg_duration(stderr: str) -> float | None:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def probe_hls_duration(
    url: str,
    *,
    cookies_file: Path | None = None,
    timeout: int = 60,
) -> float | None:
    """Probe HLS playlist duration via ffmpeg metadata (no full decode)."""
    ffmpeg = find_ffmpeg()
    hdr = build_hls_ffmpeg_headers(url, cookies_file=cookies_file)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-headers",
        hdr,
        "-i",
        url,
        "-t",
        "0.001",
        "-f",
        "null",
        "-",
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        merged = (r.stderr or "") + (r.stdout or "")
        return _parse_ffmpeg_duration(merged)
    except subprocess.TimeoutExpired as e:
        err = e.stderr
        if isinstance(err, bytes):
            err = err.decode("utf-8", errors="replace")
        if isinstance(err, str):
            dur = _parse_ffmpeg_duration(err)
            if dur is not None:
                return dur
        raise


def _fetch_hls_source(
    url: str,
    target: Path,
    *,
    cookies_file: Path | None = None,
    copy: bool = True,
    timeout: int = 1800,
) -> None:
    """Download HLS stream to a local file via ffmpeg."""
    ffmpeg = find_ffmpeg()
    hdr = build_hls_ffmpeg_headers(url, cookies_file=cookies_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-headers", hdr, "-i", url]
    if copy:
        cmd += ["-c", "copy"]
    cmd += ["-bsf:a", "aac_adtstoasc", str(target)]
    print(f"[fetch-hls] ffmpeg → {target.name}")
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if r.returncode != 0 or not target.is_file() or target.stat().st_size < 1000:
        err = (r.stderr or r.stdout or "ffmpeg failed").strip()
        raise RuntimeError(f"HLS download failed: {err[-1500:]}")


def _fetch_hls_to_wav(
    url: str,
    media: Path,
    wav: Path,
    *,
    cookies_file: Path | None = None,
) -> tuple[Path, float | None]:
    """HLS → intermediate mp4 → 16 kHz mono wav."""
    src = media / "source.mp4"
    _fetch_hls_source(url, src, cookies_file=cookies_file, copy=True)
    dur = probe_hls_duration(url, cookies_file=cookies_file, timeout=60)
    ffmpeg = find_ffmpeg()
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(wav),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"[fetch-hls] wav -> {wav}")
    return src, dur


def _resolve_js_runtime() -> list[str]:
    """YouTube needs an external JS runtime (EJS). Prefer working Node, else Deno."""
    # Node was verified on this machine; Deno winget path can be present but flaky.
    node = shutil.which("node")
    if node:
        return ["--js-runtimes", f"node:{node}"]
    deno = shutil.which("deno")
    if deno:
        return ["--js-runtimes", f"deno:{deno}"]
    return []


def _yt_dlp_cmd() -> list[str]:
    # Prefer module in current venv
    return [sys.executable, "-m", "yt_dlp"]


BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def is_bilibili_412(err: str) -> bool:
    """True when yt-dlp/Bilibili anti-bot returns HTTP 412."""
    text = err or ""
    low = text.lower()
    if "412" not in text:
        return False
    return "precondition failed" in low or "blocked by server" in low


def bili_browser_headers(cookie: str | None = None) -> dict[str, str]:
    """Browser-like headers that api.bilibili.com accepts without the HTML 412 gate."""
    headers = {
        "User-Agent": BILI_UA,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def bili_get_json(url: str, timeout: int = 15) -> dict[str, Any]:
    """GET JSON from Bilibili APIs. Wrapper so tests can monkeypatch this module."""
    from discover.adapters.bilibili import bili_get_json as _get

    return _get(url, timeout=timeout)


def _first_media_url(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    for key in ("url", "baseUrl", "base_url"):
        raw = item.get(key)
        if raw:
            return str(raw)
    backups = item.get("backup_url") or item.get("backupUrl") or []
    if backups:
        return str(backups[0])
    return None


def playurl_media_url(payload: dict[str, Any], *, prefer: str = "html5") -> str:
    """Pick a direct CDN URL from a playurl JSON payload.

    :param payload: `x/player/playurl` response (or its `data` object)
    :param prefer: `html5` uses muxed `durl`; `audio` uses highest-bandwidth DASH audio
    :raises RuntimeError: when the payload has no usable URL
    """
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        raise RuntimeError("bilibili playurl payload is empty")
    if prefer == "audio":
        audios = ((data.get("dash") or {}).get("audio")) or []
        if not audios:
            raise RuntimeError("bilibili dash playurl missing audio")
        best = max(audios, key=lambda a: int((a or {}).get("bandwidth") or 0))
        url = _first_media_url(best)
        if not url:
            raise RuntimeError("bilibili dash audio missing url")
        return url
    durl = data.get("durl") or []
    if not durl:
        raise RuntimeError("bilibili html5 playurl missing durl")
    url = _first_media_url(durl[0])
    if not url:
        raise RuntimeError("bilibili html5 playurl missing url")
    return url


# Prefer highest available H.264 + AAC (yt-dlp + Bilibili API fallback).
# Ceiling 2160 keeps 4K in play; still prefer AVC+AAC over HEVC when tied.
DEFAULT_VIDEO_MAX_HEIGHT = 2160
# Bilibili qn: 127=8K … 120=4K, 116=1080P60, 112=1080P+, 80=1080P, 64=720P, …
BILI_QN_PREFER = (127, 126, 125, 120, 116, 112, 80, 74, 64, 32, 16)
BILI_QN_HEIGHT = {
    16: 360,
    32: 480,
    64: 720,
    74: 720,
    80: 1080,
    112: 1080,
    116: 1080,
    120: 2160,
    125: 2160,
    126: 2160,
    127: 4320,
}


def yt_dlp_video_format(max_height: int = DEFAULT_VIDEO_MAX_HEIGHT) -> str:
    """yt-dlp -f string: prefer ≤max_height H.264 + AAC, then degrade gracefully."""
    h = max(360, int(max_height))
    # Portrait: also allow width<=h (e.g. 1080x1920). Prefer AVC + m4a/AAC.
    return (
        f"bv*[height<={h}][vcodec^=avc1]+ba[acodec^=mp4a]/"
        f"bv*[height<={h}][vcodec^=avc]+ba[ext=m4a]/"
        f"bv*[width<={h}][vcodec^=avc1]+ba[acodec^=mp4a]/"
        f"bv*[height<={h}][vcodec^=avc]+ba/"
        f"bv*[height<={h}]+ba[ext=m4a]/"
        f"bv*[height<={h}]+ba/"
        f"b[height<={h}][vcodec^=avc][acodec^=mp4a]/"
        f"b[height<={h}]/"
        f"bv*+ba/b"
    )


def _pick_dash_avc_aac(
    dash: dict[str, Any],
    *,
    max_height: int = DEFAULT_VIDEO_MAX_HEIGHT,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Pick best H.264 video ≤ max_height and best AAC audio from a dash object."""
    videos = list(dash.get("video") or [])
    audios = list(dash.get("audio") or [])
    if not videos or not audios:
        return None
    avc = [
        v
        for v in videos
        if str(v.get("codecs") or "").lower().startswith(("avc", "avc1"))
        and int(v.get("height") or 0) <= int(max_height)
        and int(v.get("height") or 0) > 0
    ]
    if not avc:
        avc = [
            v
            for v in videos
            if int(v.get("height") or 0) <= int(max_height) and int(v.get("height") or 0) > 0
        ]
    if not avc:
        return None
    best_v = max(
        avc,
        key=lambda v: (int(v.get("height") or 0), int(v.get("bandwidth") or 0)),
    )
    aac = [
        a
        for a in audios
        if "mp4a" in str(a.get("codecs") or "").lower()
        or str(a.get("mimeType") or a.get("mime_type") or "").lower().find("mp4") >= 0
    ]
    pool = aac or audios
    best_a = max(pool, key=lambda a: int(a.get("bandwidth") or 0))
    if not _first_media_url(best_v) or not _first_media_url(best_a):
        return None
    return best_v, best_a


def _ffmpeg_mux_copy(video: Path, audio: Path, dest: Path) -> None:
    """Mux video+audio streams into mp4 without re-encode when possible."""
    ffmpeg = find_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".mux.mp4")
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c",
        "copy",
        "-shortest",
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    if not tmp.is_file() or tmp.stat().st_size < 1000:
        raise RuntimeError(f"ffmpeg mux produced empty file: {tmp}")
    tmp.replace(dest)


def resolve_bilibili_api_download(
    url: str,
    *,
    max_height: int = DEFAULT_VIDEO_MAX_HEIGHT,
) -> dict[str, Any]:
    """Resolve title/cid and best playable media via official APIs (skips 412 HTML gate).

    Prefers ≤max_height H.264 + AAC. Tries html5 muxed qn (80→16), then DASH AVC+AAC.

    :return: meta with either ``media_url`` (muxed) or ``video_url``+``audio_url`` (dash)
    """
    bvid = detect_bvid(url)
    if not bvid:
        raise ValueError(f"not a bilibili url: {url}")
    view = bili_get_json(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        timeout=15,
    )
    if view.get("code") != 0:
        raise RuntimeError(f"bilibili view api failed: {view.get('message')}")
    data = view.get("data") or {}
    cid = data.get("cid")
    if not cid:
        raise RuntimeError("bilibili view api missing cid")
    dim = data.get("dimension") or {}
    base = {
        "bvid": str(data.get("bvid") or bvid),
        "aid": data.get("aid"),
        "cid": int(cid),
        "title": data.get("title"),
        "duration": data.get("duration"),
        "extractor": "bilibili-api",
        "source_dimension": dim,
    }

    best_mux: dict[str, Any] | None = None
    for qn in BILI_QN_PREFER:
        query = urlencode(
            {
                "bvid": bvid,
                "cid": int(cid),
                "qn": int(qn),
                "type": "mp4",
                "platform": "html5",
                "high_quality": 1,
            }
        )
        play = bili_get_json(f"https://api.bilibili.com/x/player/playurl?{query}", timeout=15)
        if play.get("code") != 0:
            continue
        pdata = play.get("data") or {}
        try:
            media_url = playurl_media_url(play, prefer="html5")
        except RuntimeError:
            continue
        quality = int(pdata.get("quality") or 0)
        size = int(((pdata.get("durl") or [{}])[0] or {}).get("size") or 0)
        cand = {
            "mode": "muxed",
            "media_url": media_url,
            "quality": quality,
            "qn_requested": qn,
            "size": size,
            "format": pdata.get("format"),
        }
        if best_mux is None or quality > int(best_mux["quality"]) or (
            quality == int(best_mux["quality"]) and size > int(best_mux.get("size") or 0)
        ):
            best_mux = cand
        if quality >= 116:
            break

    best_dash: dict[str, Any] | None = None
    # fnval=4048 unlocks dash + 4K/HDR streams when login allows.
    query = urlencode(
        {
            "bvid": bvid,
            "cid": int(cid),
            "qn": 127,
            "fnval": 4048,
            "fourk": 1,
        }
    )
    play = bili_get_json(f"https://api.bilibili.com/x/player/playurl?{query}", timeout=15)
    if play.get("code") == 0:
        pdata = play.get("data") or {}
        picked = _pick_dash_avc_aac(pdata.get("dash") or {}, max_height=max_height)
        if picked:
            vv, aa = picked
            best_dash = {
                "mode": "dash",
                "video_url": _first_media_url(vv),
                "audio_url": _first_media_url(aa),
                "quality": int(vv.get("id") or 0),
                "width": int(vv.get("width") or 0),
                "height": int(vv.get("height") or 0),
                "vcodec": vv.get("codecs"),
                "acodec": aa.get("codecs"),
                "bandwidth": int(vv.get("bandwidth") or 0),
            }

    # Prefer whichever yields higher vertical resolution / qn.
    use_dash = False
    if best_dash and best_mux:
        dash_h = int(best_dash.get("height") or 0)
        mux_h = BILI_QN_HEIGHT.get(int(best_mux["quality"]), 0)
        use_dash = dash_h > mux_h
    elif best_dash and not best_mux:
        use_dash = True

    if use_dash and best_dash:
        out = {**base, **best_dash}
        print(
            f"[fetch] bili playurl dash "
            f"{out.get('width')}x{out.get('height')} {out.get('vcodec')}+{out.get('acodec')}"
        )
        return out
    if best_mux:
        out = {**base, **best_mux}
        print(
            f"[fetch] bili playurl muxed qn={out.get('quality')} "
            f"format={out.get('format')} size={out.get('size')}"
        )
        return out
    raise RuntimeError("bilibili playurl returned no muxed or dash streams")


def download_bilibili_api_source(
    url: str,
    dest: Path,
    *,
    max_height: int = DEFAULT_VIDEO_MAX_HEIGHT,
) -> dict[str, Any]:
    """Download best Bilibili source into ``dest`` (H.264+AAC preferred)."""
    info = resolve_bilibili_api_download(url, max_height=max_height)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if info.get("mode") == "dash":
        v_part = dest.with_suffix(".v.part")
        a_part = dest.with_suffix(".a.part")
        try:
            print("[fetch] bili dash video …")
            _http_download(str(info["video_url"]), v_part, headers=bili_browser_headers())
            print("[fetch] bili dash audio …")
            _http_download(str(info["audio_url"]), a_part, headers=bili_browser_headers())
            _ffmpeg_mux_copy(v_part, a_part, dest)
        finally:
            for p in (v_part, a_part):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
    else:
        _http_download(str(info["media_url"]), dest, headers=bili_browser_headers())
    info["source_file"] = str(dest)
    info["bytes"] = dest.stat().st_size if dest.is_file() else 0
    if not info.get("height"):
        try:
            ffmpeg = find_ffmpeg()
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-i", str(dest)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            m = re.search(r"(\d{2,5})x(\d{2,5})", (r.stderr or "") + (r.stdout or ""))
            if m:
                info["width"], info["height"] = int(m.group(1)), int(m.group(2))
        except Exception:
            pass
    return info


def _http_download(
    url: str,
    dest: Path,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 180,
) -> None:
    """Download a single HTTP file (Bilibili CDN) with browser Referer."""
    import urllib.request

    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=headers or bili_browser_headers())
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as fh:
        shutil.copyfileobj(resp, fh)
    if not tmp.is_file() or tmp.stat().st_size < 1000:
        if tmp.exists():
            tmp.unlink()
        raise RuntimeError(f"download too small: {url}")
    tmp.replace(dest)


def probe_bilibili_duration(url: str) -> dict[str, Any]:
    """Duration/title via view API (does not hit the 412 HTML page)."""
    bvid = detect_bvid(url)
    if not bvid:
        raise ValueError(f"not a bilibili url: {url}")
    view = bili_get_json(
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        timeout=15,
    )
    if view.get("code") != 0:
        raise RuntimeError(f"bilibili view api failed: {view.get('message')}")
    data = view.get("data") or {}
    duration = data.get("duration")
    if duration is None:
        raise RuntimeError("bilibili view api missing duration")
    return {
        "bvid": str(data.get("bvid") or bvid),
        "title": data.get("title"),
        "duration": float(duration),
        "cid": data.get("cid"),
        "aid": data.get("aid"),
    }


def _ffmpeg_to_wav(src: Path, wav: Path) -> None:
    from job_layout import is_usable_wav

    ffmpeg = find_ffmpeg()
    if wav.is_file():
        wav.unlink()
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(wav),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    if not is_usable_wav(wav):
        raise RuntimeError(
            f"ffmpeg produced invalid wav ({wav.stat().st_size if wav.is_file() else 0} bytes) from {src}"
        )


def _fetch_bilibili_api_to_wav(url: str, media: Path, wav: Path, meta_path: Path) -> dict[str, Any]:
    """Download best playable mp4 via playurl API, then extract 16 kHz mono WAV."""
    src = media / "source.mp4"
    print("[fetch] bili 412 fallback → playurl API (prefer ≤1080p H.264+AAC)")
    info = download_bilibili_api_source(url, src, max_height=DEFAULT_VIDEO_MAX_HEIGHT)
    print(f"[fetch] source -> {src.name} ({src.stat().st_size} bytes)")
    _ffmpeg_to_wav(src, wav)
    print(f"[fetch] wav -> {wav}")
    meta = {
        "url": url,
        "platform": "bilibili",
        "bvid": info["bvid"],
        "id": info["bvid"],
        "title": info["title"],
        "duration": info["duration"],
        "extractor": "bilibili-api",
        "source_file": str(src),
        "wav": str(wav),
        "cid": info["cid"],
        "aid": info["aid"],
        "quality": info.get("quality"),
        "mode": info.get("mode"),
        "width": info.get("width"),
        "height": info.get("height"),
        "format": info.get("format"),
        "vcodec": info.get("vcodec"),
        "acodec": info.get("acodec"),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {meta_path}")
    return meta


ROOT = Path(__file__).resolve().parent


def resolve_cookies_file(explicit: Path | None = None) -> Path | None:
    """Prefer explicit path, then env, then local cookies*.txt (gitignored)."""
    if explicit is not None:
        p = Path(explicit)
        return p if p.is_file() else None
    for key in ("VITUAL_COOKIES", "YTDLP_COOKIES"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            p = Path(raw)
            if p.is_file():
                return p
    for name in ("cookies.txt", "cookies.douyin.txt", "cookies_douyin.txt"):
        p = ROOT / name
        if p.is_file():
            return p
    return None


def default_cookies_path() -> Path:
    env = (os.environ.get("VITUAL_COOKIES") or os.environ.get("YTDLP_COOKIES") or "").strip()
    if env:
        return Path(env)
    return ROOT / "cookies.txt"


def cookie_domain_for(platform: str, url: str = "") -> str:
    """Netscape cookie domain for a try URL platform."""
    mapping = {
        "douyin": ".douyin.com",
        "bilibili": ".bilibili.com",
        "youtube": ".youtube.com",
        "kuaishou": ".kuaishou.com",
    }
    if platform in mapping:
        return mapping[platform]
    m = re.search(r"https?://(?:www\.)?([^/]+)", (url or "").lower())
    if m:
        host = m.group(1)
        return host if host.startswith(".") else f".{host}"
    return ".douyin.com"


def _looks_like_netscape(text: str) -> bool:
    if "# netscape" in text.lower() or "http cookie file" in text.lower():
        return True
    for ln in text.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 7 and parts[1] in ("TRUE", "FALSE"):
            return True
    return False


def _looks_like_cookie_header(text: str) -> bool:
    """Detect browser Cookie header: ``a=b; c=d; …`` (multi-pair)."""
    if "\t" in text or text.lstrip().startswith("#"):
        return False
    if ";" not in text or "=" not in text:
        return False
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if len(parts) < 2:
        return False
    return sum(1 for p in parts if "=" in p) >= 2


def _parse_cookie_header(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip()
        if name:
            pairs.append((name, value))
    return pairs


def upsert_try_cookies(raw: str, *, platform: str, url: str = "") -> Path:
    """Merge user-pasted cookies into local Netscape cookies.txt.

    Accepts Netscape dump, Cookie header (``a=b; c=d``), or a single name=value /
    session cookie for the URL's platform domain.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("cookies are empty")

    path = resolve_cookies_file() or default_cookies_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    if _looks_like_netscape(text):
        existing: list[str] = []
        if path.is_file():
            existing = path.read_text(encoding="utf-8", errors="replace").splitlines()
        body = [ln for ln in existing if ln.strip() and not ln.startswith("#")]
        pasted = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        header = [
            "# Netscape HTTP Cookie File",
            "# https://curl.se/docs/http-cookies.html",
            "",
        ]
        path.write_text("\n".join(header + body + pasted) + "\n", encoding="utf-8")
        print(f"[cookies] merged Netscape paste → {path.name} (+{len(pasted)} lines)")
        return path

    domain = cookie_domain_for(platform, url)
    host = domain if domain.startswith(".") else f".{domain}"
    exp = int(time.time()) + 365 * 24 * 3600

    if _looks_like_cookie_header(text):
        pairs = _parse_cookie_header(text)
        if not pairs:
            raise ValueError("cookie header parsed empty")
        replace_names = {n for n, _ in pairs}
        lines: list[str] = []
        if path.is_file():
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = ln.split("\t")
                if len(parts) >= 7 and parts[5] in replace_names and parts[0] == host:
                    continue
                lines.append(ln)
        if not lines or not lines[0].startswith("#"):
            lines = [
                "# Netscape HTTP Cookie File",
                "# https://curl.se/docs/http-cookies.html",
                "",
                *lines,
            ]
        for name, value in pairs:
            lines.append(f"{host}\tTRUE\t/\tTRUE\t{exp}\t{name}\t{value}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[cookies] upserted Cookie header ({len(pairs)} pairs)@{host} → {path.name}")
        return path

    name = "sessionid"
    value = text
    if "\n" not in text and "\t" not in text and "=" in text and not text.startswith("#"):
        left, _, right = text.partition("=")
        if left.strip() and right.strip() and " " not in left.strip():
            name = left.strip()
            value = right.strip()
    if len(value) < 8:
        raise ValueError("cookie value looks too short")

    replace_names = {name}
    if name == "sessionid":
        replace_names.add("sessionid_ss")

    lines = []
    if path.is_file():
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = ln.split("\t")
            if len(parts) >= 7 and parts[5] in replace_names and parts[0] == host:
                continue
            lines.append(ln)
    if not lines or not lines[0].startswith("#"):
        lines = [
            "# Netscape HTTP Cookie File",
            "# https://curl.se/docs/http-cookies.html",
            "",
            *lines,
        ]
    for n in sorted(replace_names):
        lines.append(f"{host}\tTRUE\t/\tTRUE\t{exp}\t{n}\t{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[cookies] upserted {name}@{host} → {path.name}")
    return path


def upsert_session_cookies(sessionid: str, *, domain: str = ".douyin.com") -> Path:
    """Backward-compatible wrapper."""
    plat = "douyin" if "douyin" in domain else "generic"
    return upsert_try_cookies(sessionid, platform=plat)


def _cookie_cli_args(
    *,
    cookies_from_browser: str | None,
    cookies_file: Path | None,
) -> tuple[list[str], Path | None, str | None]:
    """Return (cli_args, cookies_file_used, browser_used). File wins over browser."""
    cf = resolve_cookies_file(cookies_file)
    if cf is not None:
        return ["--cookies", str(cf)], cf, None
    if cookies_from_browser:
        return ["--cookies-from-browser", cookies_from_browser], None, cookies_from_browser
    return [], None, None


def _douyin_cookie_hint() -> str:
    return (
        "Douyin needs fresh cookies. In Chrome (logged into douyin.com), export "
        "Netscape cookies.txt via extension, save as subtitle_pipeline/cookies.txt, "
        "then retry. Or: yt-dlp --cookies-from-browser chrome \"URL\"."
    )


def fetch_to_wav(
    url: str,
    out_dir: Path,
    *,
    cookies_from_browser: str | None = "chrome",
    cookies_file: Path | None = None,
    force: bool = False,
) -> dict:
    """Download best audio and convert to full_16k.wav. Returns meta dict."""
    from job_layout import existing_wav, find_source_media, is_usable_wav, job_media_dir

    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    media = job_media_dir(out_dir)
    wav = media / "full_16k.wav"
    meta_path = media / "fetch_meta.json"
    if wav.is_file() and not is_usable_wav(wav):
        print(f"[fetch] drop invalid wav ({wav.stat().st_size} bytes)")
        wav.unlink()
    reused = existing_wav(out_dir)
    if reused is not None and reused.resolve() != wav.resolve() and not is_usable_wav(wav):
        shutil.move(str(reused), str(wav))
    legacy_meta = out_dir / "fetch_meta.json"
    if not meta_path.exists() and legacy_meta.exists():
        shutil.move(str(legacy_meta), str(meta_path))

    if is_usable_wav(wav) and not force:
        print(f"[fetch] reuse -> {wav}")
        meta = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["wav"] = str(wav)
        return meta

    src_local = find_source_media(out_dir, wav=wav)
    if src_local and (force or not is_usable_wav(wav)):
        print(f"[fetch] local source -> {src_local.name}")
        _ffmpeg_to_wav(src_local, wav)
        print(f"[fetch] wav -> {wav}")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                meta = {}
        meta.update(
            {
                "source_file": str(src_local),
                "wav": str(wav),
            }
        )
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[out] {meta_path}")
        return meta

    ffmpeg = find_ffmpeg()
    platform = detect_platform(url)
    bvid = detect_bvid(url)
    print(f"[fetch] platform={platform} url={url}")

    cookie_args, cookies_file, cookies_from_browser = _cookie_cli_args(
        cookies_from_browser=cookies_from_browser,
        cookies_file=cookies_file,
    )

    if platform == "hls":
        src, dur = _fetch_hls_to_wav(url, media, wav, cookies_file=cookies_file)
        meta = {
            "url": url,
            "platform": platform,
            "bvid": bvid,
            "id": detect_m3u8_id(url),
            "title": f"hls:{detect_m3u8_id(url) or 'stream'}",
            "duration": dur,
            "extractor": "ffmpeg-hls",
            "source_file": str(src),
            "wav": str(wav),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[out] {meta_path}")
        return meta

    # Stage 1: yt-dlp → intermediate audio (m4a/webm/…)
    tmpl = str(media / "source.%(ext)s")
    js_rt = _resolve_js_runtime()
    if js_rt:
        print(f"[fetch] js-runtime={' '.join(js_rt[1:])}")
    else:
        print("[fetch] WARN: no deno/node on PATH — YouTube may 403")

    # YouTube from CN usually needs a working local proxy; dead proxy is worse than none.
    # Bilibili/Douyin go direct — Clash SOCKS often breaks api.bilibili.com TLS.
    proxy_args: list[str] = []
    if platform == "youtube":
        px = _youtube_proxy_url()
        if px:
            proxy_args = ["--proxy", px]
            print(f"[fetch] proxy={px}")
        else:
            clear_proxy_env()
            print("[fetch] no local proxy detected; direct connect")
    else:
        clear_proxy_env()
        print("[fetch] direct connect (no proxy)")

    if cookies_file:
        print(f"[fetch] cookies-file={cookies_file}")
    elif cookies_from_browser:
        print(f"[fetch] cookies-from-browser={cookies_from_browser}")
    elif platform == "douyin":
        print(f"[fetch] WARN: no cookies — {_douyin_cookie_hint()}")

    base = _yt_dlp_cmd() + js_rt + proxy_args + [
        "--no-playlist",
        "--newline",
        "-f",
        "bestaudio/best",
        "-o",
        tmpl,
        "--no-mtime",
    ] + cookie_args

    # Probe info first (title/id)
    info: dict = {}
    probe = base + ["--skip-download", "--print-json", url]
    try:
        r = subprocess.run(
            probe,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if r.returncode == 0 and r.stdout.strip():
            # last json line
            for line in reversed(r.stdout.strip().splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    info = json.loads(line)
                    break
    except Exception as e:
        print(f"[fetch] probe soft-fail: {e}")

    # If probe failed with cookies, retry without browser cookies once
    def run_download(cmd: list[str]) -> subprocess.CompletedProcess:
        print("[fetch] yt-dlp download …")
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )

    dl_cmd = base + [url]
    result = run_download(dl_cmd)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        print(f"[fetch] yt-dlp failed:\n{err[-1200:]}")
        # Douyin never works without cookies — don't strip them.
        if platform == "douyin":
            raise RuntimeError(
                "yt-dlp download failed. "
                + _douyin_cookie_hint()
                + "\n"
                + err[-1500:]
            )
        # retry without cookies-from-browser (Chrome DB lock / stale jar)
        if cookies_from_browser and not cookies_file:
            print("[fetch] retry without browser cookies …")
            cleaned: list[str] = []
            skip = 0
            for c in base:
                if skip:
                    skip -= 1
                    continue
                if c == "--cookies-from-browser":
                    skip = 1
                    continue
                cleaned.append(c)
            result = run_download(cleaned + [url])
            if result.returncode != 0:
                err2 = (result.stderr or result.stdout or "").strip()
                if platform == "youtube" and ("403" in err2 or "Forbidden" in err2):
                    print("[fetch] retry YouTube android/web player clients …")
                    result = run_download(
                        cleaned
                        + [
                            "--extractor-args",
                            "youtube:player_client=android,web",
                            url,
                        ]
                    )
                    if result.returncode != 0:
                        err3 = (result.stderr or result.stdout or "").strip()
                        raise RuntimeError(
                            "yt-dlp download failed. Update yt-dlp or export cookies.txt.\n"
                            + err3[-1500:]
                        )
                else:
                    if platform == "bilibili" and is_bilibili_412(err2):
                        return _fetch_bilibili_api_to_wav(url, media, wav, meta_path)
                    raise RuntimeError(
                        "yt-dlp download failed. Update yt-dlp or export cookies.txt.\n"
                        + err2[-1500:]
                    )
        else:
            if platform == "youtube" and ("403" in err or "Forbidden" in err):
                print("[fetch] retry YouTube android/web player clients …")
                result = run_download(
                    base
                    + [
                        "--extractor-args",
                        "youtube:player_client=android,web",
                        url,
                    ]
                )
                if result.returncode != 0:
                    err3 = (result.stderr or result.stdout or "").strip()
                    raise RuntimeError("yt-dlp download failed.\n" + err3[-1500:])
            else:
                if platform == "bilibili" and is_bilibili_412(err):
                    return _fetch_bilibili_api_to_wav(url, media, wav, meta_path)
                hint = (
                    _douyin_cookie_hint()
                    if platform == "douyin"
                    else "Update yt-dlp or export cookies.txt."
                )
                raise RuntimeError(f"yt-dlp download failed. {hint}\n" + err[-1500:])

    # Find downloaded source file (media/ first, then legacy work-dir root)
    sources: list[Path] = []
    for folder in (media, out_dir):
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            if (
                p.is_file()
                and p.suffix.lower()
                in {".m4a", ".webm", ".mp3", ".m4s", ".mp4", ".opus", ".ogg", ".wav", ".flac", ".mkv"}
                and p.name != "full_16k.wav"
                and p.resolve() != wav.resolve()
            ):
                sources.append(p)
    sources = sorted(sources, key=lambda p: p.stat().st_mtime, reverse=True)
    if not sources:
        raise FileNotFoundError(f"no media downloaded in {out_dir}")
    src = sources[0]
    print(f"[fetch] source -> {src.name} ({src.stat().st_size} bytes)")

    # Convert to 16 kHz mono wav
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(src),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(wav),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    from job_layout import is_usable_wav

    if not is_usable_wav(wav):
        raise RuntimeError(
            f"ffmpeg produced invalid wav ({wav.stat().st_size if wav.is_file() else 0} bytes) from {src}"
        )
    print(f"[fetch] wav -> {wav}")

    meta = {
        "url": url,
        "platform": platform,
        "bvid": bvid,
        "id": info.get("id"),
        "title": info.get("title") or info.get("fulltitle"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "extractor": info.get("extractor"),
        "source_file": str(src),
        "wav": str(wav),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {meta_path}")
    return meta


def ensure_source_video(
    url: str,
    out_dir: Path,
    *,
    cookies_from_browser: str | None = "chrome",
    cookies_file: Path | None = None,
    force: bool = False,
    max_height: int = DEFAULT_VIDEO_MAX_HEIGHT,
) -> Path:
    """Download a muxed video (prefer ≤1080p H.264+AAC) into media/source.mp4."""
    out_dir = Path(out_dir)
    media = out_dir / "media"
    media.mkdir(parents=True, exist_ok=True)
    target = media / "source.mp4"
    max_height = max(360, int(max_height or DEFAULT_VIDEO_MAX_HEIGHT))

    if target.is_file() and target.stat().st_size > 50_000 and not force:
        print(f"[fetch-video] reuse -> {target}")
        return target

    # Prefer any existing video already in the job dir
    if not force:
        for folder in (media, out_dir):
            if not folder.is_dir():
                continue
            for p in sorted(folder.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
                if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
                    if p.resolve() != target.resolve() and p.stat().st_size > 50_000:
                        if _file_has_video_stream(p):
                            print(f"[fetch-video] found -> {p}")
                            return p

    platform = detect_platform(url)
    print(f"[fetch-video] platform={platform} url={url} max_height={max_height}")

    cookie_args, cookies_file, cookies_from_browser = _cookie_cli_args(
        cookies_from_browser=cookies_from_browser,
        cookies_file=cookies_file,
    )
    if platform == "hls":
        if target.is_file() and target.stat().st_size > 50_000:
            return target
        _fetch_hls_source(url, target, cookies_file=cookies_file, copy=True)
        print(f"[fetch-video] source -> {target.name} ({target.stat().st_size} bytes)")
        return target

    js_rt = _resolve_js_runtime()
    proxy_args: list[str] = []
    if platform == "youtube":
        px = _youtube_proxy_url()
        if px:
            proxy_args = ["--proxy", px]
            print(f"[fetch-video] proxy={px}")
        else:
            clear_proxy_env()
    else:
        clear_proxy_env()
        print("[fetch-video] direct connect (no proxy)")

    fmt = yt_dlp_video_format(max_height)
    tmpl = str(media / "source_video.%(ext)s")
    if cookies_file:
        print(f"[fetch-video] cookies-file={cookies_file}")
    elif cookies_from_browser:
        print(f"[fetch-video] cookies-from-browser={cookies_from_browser}")
    print(f"[fetch-video] format={fmt}")
    base = _yt_dlp_cmd() + js_rt + proxy_args + [
        "--no-playlist",
        "--newline",
        "-f",
        fmt,
        "--merge-output-format",
        "mp4",
        "-o",
        tmpl,
        "--no-mtime",
    ] + cookie_args

    def run_download(cmd: list[str]) -> subprocess.CompletedProcess:
        print("[fetch-video] yt-dlp download …")
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )

    result = run_download(base + [url])
    if result.returncode != 0 and platform == "douyin":
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            "yt-dlp video download failed. " + _douyin_cookie_hint() + "\n" + err[-1500:]
        )
    if result.returncode != 0 and cookies_from_browser and not cookies_file:
        err = (result.stderr or result.stdout or "").strip()
        print(f"[fetch-video] yt-dlp failed:\n{err[-800:]}")
        print("[fetch-video] retry without browser cookies …")
        cleaned: list[str] = []
        skip = 0
        for c in base:
            if skip:
                skip -= 1
                continue
            if c == "--cookies-from-browser":
                skip = 1
                continue
            cleaned.append(c)
        result = run_download(cleaned + [url])
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        if platform == "bilibili" and is_bilibili_412(err):
            print("[fetch-video] yt-dlp 412, fallback to playurl API")
            download_bilibili_api_source(url, target, max_height=max_height)
            print(f"[fetch-video] source -> {target.name} ({target.stat().st_size} bytes)")
            return target
        raise RuntimeError("yt-dlp video download failed.\n" + err[-1500:])

    sources = sorted(
        [
            p
            for p in media.iterdir()
            if p.is_file()
            and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
            and "source_video" in p.stem
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not sources:
        # merge may have written source_video.mp4 already as final name
        sources = sorted(
            [
                p
                for p in media.iterdir()
                if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
                and p.name != "source.m4a"
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    if not sources:
        raise FileNotFoundError(f"no video downloaded in {media}")
    src = sources[0]
    if src.resolve() != target.resolve():
        if target.exists():
            target.unlink()
        src.replace(target)
    print(f"[fetch-video] source -> {target.name} ({target.stat().st_size} bytes)")
    return target


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="URL → 16kHz mono WAV via yt-dlp")
    ap.add_argument("url")
    ap.add_argument("--out-dir", type=Path, default=Path("downloads") / "last")
    ap.add_argument("--cookies-from-browser", default="chrome")
    ap.add_argument("--no-cookies", action="store_true")
    ap.add_argument("--cookies", type=Path, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--with-video",
        action="store_true",
        help="also download a ≤480p video for note screenshots",
    )
    args = ap.parse_args(argv)
    try:
        meta = fetch_to_wav(
            args.url,
            args.out_dir,
            cookies_from_browser=None if args.no_cookies else args.cookies_from_browser,
            cookies_file=args.cookies,
            force=args.force,
        )
        if args.with_video:
            v = ensure_source_video(
                args.url,
                args.out_dir,
                cookies_from_browser=None if args.no_cookies else args.cookies_from_browser,
                cookies_file=args.cookies,
                force=args.force,
            )
            meta["video"] = str(v)
        print(json.dumps({k: meta.get(k) for k in ("platform", "title", "bvid", "wav", "video")}, ensure_ascii=False))
        return 0
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
