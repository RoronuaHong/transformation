#!/usr/bin/env python3
"""Export content.db → transform/content/articles.json for Next.js static reads."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discover.content_db import ContentDB
from langs import coalesce_source_lang
from note_frames import copy_clips_to_site, copy_frames_to_site
from media_ops import copy_derived_to_site
from pipeline import parse_srt


def _srt_timestamp(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    ms_total = int(round(float(sec) * 1000.0))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def cues_from_locale_srts(locales: dict[str, dict]) -> list[dict]:
    """Merge per-locale SRTs into site cues (index-aligned; source timeline wins)."""
    parsed: dict[str, list[dict]] = {}
    for loc, data in locales.items():
        raw_path = data.get("srt_path")
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_file():
            continue
        segs = parse_srt(path)
        if segs:
            parsed[loc] = segs
    if not parsed:
        return []
    primary: list[dict] | None = None
    for key in ("zh", "en", "ja", "pt", "de", "ru", "ko"):
        if key in parsed:
            primary = parsed[key]
            break
    if primary is None:
        primary = next(iter(parsed.values()))
    cues: list[dict] = []
    for i, seg in enumerate(primary):
        text: dict[str, str] = {}
        for loc, segs in parsed.items():
            if i < len(segs) and segs[i].get("text"):
                text[loc] = str(segs[i]["text"])
        cues.append(
            {
                "start": _srt_timestamp(float(seg["start"])),
                "end": _srt_timestamp(float(seg["end"])),
                "text": text,
            }
        )
    return cues


def remix_overlay_public(slug: str, site_public: Path) -> dict | None:
    """Public URLs for WF-07 overlay (remix clock). Distinct from article ``cues`` (source clock)."""
    slug = str(slug or "").strip()
    if not slug:
        return None
    derived = Path(site_public) / "derived" / slug
    video = derived / "remix.mp4"
    if not video.is_file() or video.stat().st_size < 800:
        return None
    out: dict = {
        "clock": "remix",
        "video": f"/derived/{slug}/remix.mp4",
    }
    vtt = derived / "remix.vtt"
    cues = derived / "remix_cues.json"
    if vtt.is_file() and vtt.stat().st_size > 20:
        out["vtt"] = f"/derived/{slug}/remix.vtt"
    if cues.is_file() and cues.stat().st_size > 20:
        out["cues"] = f"/derived/{slug}/remix_cues.json"
        try:
            meta = json.loads(cues.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            meta = {}
        if isinstance(meta, dict):
            if meta.get("intro_sec") is not None:
                out["intro_sec"] = meta.get("intro_sec")
            if meta.get("source_clock"):
                out["source_clock"] = meta.get("source_clock")
            if meta.get("audio_clock") is not None:
                out["audio_clock"] = meta.get("audio_clock")
    return out


def export_articles(db: ContentDB, *, site_public: Path | None = None) -> list[dict]:
    site_public = site_public or (ROOT.parent / "transform" / "public")
    rows = db._conn.execute(
        "SELECT * FROM articles WHERE status IN ('ready','published') ORDER BY updated_at DESC"
    ).fetchall()
    out: list[dict] = []
    for a in rows:
        source_lang = coalesce_source_lang(a["source_lang"] or "en")
        article_slug = str(a["slug"] or "")
        locales: dict[str, dict] = {}
        for loc in db.list_locales(int(a["id"])):
            outline = []
            try:
                outline = json.loads(loc["outline_json"] or "[]")
            except json.JSONDecodeError:
                outline = []
            key_points: list[dict] = []
            focuses: list[dict] = []
            hard_points: list[dict] = []
            try:
                key_points = json.loads(loc["key_points_json"] or "[]")
            except (json.JSONDecodeError, KeyError):
                key_points = []
            try:
                focuses = json.loads(loc["focuses_json"] or "[]")
            except (json.JSONDecodeError, KeyError):
                focuses = []
            try:
                hard_points = json.loads(loc["hard_points_json"] or "[]")
            except (json.JSONDecodeError, KeyError):
                hard_points = []
            # Keep /frames|clips/{slug}/… aligned with article slug (export destination).
            if article_slug:
                fixed: list[dict] = []
                for kp in key_points:
                    row = dict(kp) if isinstance(kp, dict) else {"title": str(kp)}
                    img = row.get("image")
                    if isinstance(img, str) and img.startswith("/frames/"):
                        name = img.rsplit("/", 1)[-1]
                        row["image"] = f"/frames/{article_slug}/{name}"
                    clip = row.get("clip")
                    if isinstance(clip, str) and clip.startswith("/clips/"):
                        name = clip.rsplit("/", 1)[-1]
                        row["clip"] = f"/clips/{article_slug}/{name}"
                    fixed.append(row)
                key_points = fixed
            loc_key = coalesce_source_lang(loc["locale"], fallback=source_lang)
            locales[loc_key] = {
                "title": loc["title"],
                "one_liner": loc["one_liner"],
                "summary": loc["summary"],
                "outline": outline,
                "keypoints_intro": loc["keypoints_intro"] or "",
                "key_points": key_points,
                "focuses": focuses,
                "hard_points": hard_points,
                "srt_path": loc["srt_path"],
            }
        work = a["work_dir"]
        if work:
            n = copy_frames_to_site(Path(work), str(a["slug"]), site_public)
            if n:
                print(f"[export] frames slug={a['slug']} n={n}")
            nc = copy_clips_to_site(Path(work), str(a["slug"]), site_public)
            if nc:
                print(f"[export] clips slug={a['slug']} n={nc}")
            nd = copy_derived_to_site(Path(work), str(a["slug"]), site_public)
            if nd:
                print(f"[export] derived slug={a['slug']} n={nd}")
        article = {
            "platform": a["platform"],
            "video_id": a["video_id"],
            "topic": a["topic_id"],
            "slug": a["slug"],
            "canonical_url": a["canonical_url"],
            "embed_url": a["embed_url"],
            "source_lang": source_lang,
            "title_src": a["title_src"],
            "author": a["author"],
            "locales": locales,
            # Full-length / embed timeline (source audio). Not the 9:16 remix clock.
            "cue_clock": "source",
            "cues": cues_from_locale_srts(locales),
        }
        remix = remix_overlay_public(article_slug, site_public)
        if remix:
            article["remix"] = remix
        out.append(article)
    return out

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--content-db", type=Path, default=None)
    p.add_argument(
        "--out",
        type=Path,
        default=ROOT.parent / "transform" / "content" / "articles.json",
    )
    p.add_argument(
        "--site-public",
        type=Path,
        default=ROOT.parent / "transform" / "public",
    )
    args = p.parse_args(argv)
    db = ContentDB(args.content_db)
    try:
        articles = export_articles(db, site_public=args.site_public)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps({"articles": articles}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        n_cues = sum(len(a.get("cues") or []) for a in articles)
        print(f"[export] {len(articles)} articles cues={n_cues} → {args.out}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
