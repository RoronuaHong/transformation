#!/usr/bin/env python3
"""Quick m3u8 smoke: parse → probe duration → cut 10s clip."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discover.run_inbox import parse_url
from fetch_media import build_hls_ffmpeg_headers, find_ffmpeg, probe_hls_duration

DEFAULT_URL = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Verify m3u8 URL with pipeline helpers")
    p.add_argument("url", nargs="?", default=DEFAULT_URL)
    p.add_argument("--out-dir", type=Path, default=ROOT / "downloads" / "m3u8_verify")
    p.add_argument("--seconds", type=float, default=10.0)
    args = p.parse_args(argv)

    url = args.url.strip()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    platform, video_id = parse_url(url)
    print(f"[parse] platform={platform} video_id={video_id}")

    dur = probe_hls_duration(url, timeout=90)
    print(f"[probe] duration_sec={dur}")

    clip = out_dir / f"{platform}_{video_id[:12]}_{int(args.seconds)}s.mp4"
    ffmpeg = find_ffmpeg()
    hdr = build_hls_ffmpeg_headers(url)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-headers",
        hdr,
        "-i",
        url,
        "-t",
        str(args.seconds),
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        str(clip),
    ]
    print(f"[clip] ffmpeg -t {args.seconds} → {clip.name}")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if r.returncode != 0:
        print((r.stderr or r.stdout)[-800:], file=sys.stderr)
        return 1
    size = clip.stat().st_size
    print(f"[ok] clip={clip} bytes={size}")
    return 0 if size > 10_000 else 1


if __name__ == "__main__":
    raise SystemExit(main())
