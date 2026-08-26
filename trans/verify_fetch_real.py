#!/usr/bin/env python3
"""Supplemental: prove real yt-dlp fetch works (E2b)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yt_dlp

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "subtitle_pipeline"))
from fetch_media import fetch_to_wav  # noqa: E402

out = ROOT / "e2e_out" / "fetch_real"
out.mkdir(parents=True, exist_ok=True)

opts = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "skip_download": True,
    "playlistend": 20,
}
with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info("ytsearch20:AI short form video", download=False)

entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
cands = [e for e in entries if isinstance(e.get("duration"), (int, float)) and e["duration"] <= 90]
pick = (cands or entries)[0]
url = f"https://www.youtube.com/watch?v={pick['id']}"
print("pick", pick["id"], "dur", pick.get("duration"), repr(str(pick.get("title"))[:70]))

meta = fetch_to_wav(url, out, cookies_from_browser=None, force=True)
wav = Path(meta["wav"])
assert wav.exists() and wav.stat().st_size > 1000, meta
(ROOT / "e2e_out" / "fetch_real_meta.json").write_text(
    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("FETCH_OK", wav, "bytes", wav.stat().st_size)
