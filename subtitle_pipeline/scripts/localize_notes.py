#!/usr/bin/env python3
"""Localize {stem}_summary.json notes (重点/要点/难点) into site langs, then register."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discover.content_db import ContentDB
from discover.run_batch import register_work_dir, translate_summary_locale
from job_layout import existing_wav
from langs import PACKS, file_tag, normalize_lang, resolve_targets
from pipeline import (
    ensure_ollama_model,
    find_ollama,
    load_notes_summary,
    write_locale_notes,
    write_notes_index,
)


def localize_notes(work_dir: Path, source_lang: str, stem: str = "full_16k") -> None:
    ollama = find_ollama()
    model = "translategemma:4b"
    ensure_ollama_model(model, role="translate")
    src = normalize_lang(source_lang)
    summary = load_notes_summary(work_dir, stem, src)
    if not summary:
        summary_path = work_dir / f"{stem}_summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    for loc in resolve_targets("site", src):
        data = translate_summary_locale(
            ollama, model, summary, source_lang=src, target_lang=loc
        )
        data["source_lang"] = src
        data["summary_lang"] = loc
        _js, md = write_locale_notes(data, work_dir, loc)
        print(f"[out] {md}")
    write_notes_index(work_dir)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--work-dir", type=Path, required=True)
    p.add_argument("--source-lang", required=True)
    p.add_argument("--platform", default="youtube")
    p.add_argument("--video-id", required=True)
    p.add_argument("--topic", default="life_hacks")
    p.add_argument("--url", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--stem", default="full_16k")
    p.add_argument("--skip-localize", action="store_true")
    args = p.parse_args()
    if not args.skip_localize:
        localize_notes(args.work_dir, args.source_lang, stem=args.stem)
    content = ContentDB()
    try:
        register_work_dir(
            content,
            platform=args.platform,
            video_id=args.video_id,
            topic_id=args.topic,
            canonical_url=args.url,
            work_dir=args.work_dir,
            source_lang=args.source_lang,
            title=args.title or None,
            source_wav=str(existing_wav(args.work_dir) or args.work_dir / "media" / f"{args.stem}.wav"),
            locales=list(PACKS["site"]),
        )
    finally:
        content.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
