"""Canonical per-job work-dir layout.

{work_dir}/
  media/   wav, source.*, frames/, fetch_meta, sync_meta, glossary, asr extras
  subs/    {lang}.srt
  notes/   {lang}/summary.md + summary.json
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from langs import coalesce_source_lang, file_tag

STEM_DEFAULT = "full_16k"

MEDIA_FLAT: dict[str, str] = {
    f"{STEM_DEFAULT}.wav": "full_16k.wav",
    "fetch_meta.json": "fetch_meta.json",
    f"{STEM_DEFAULT}_sync_meta.json": "sync_meta.json",
    f"{STEM_DEFAULT}_glossary.json": "glossary.json",
    f"{STEM_DEFAULT}_multipass_raw.json": "multipass_raw.json",
    f"{STEM_DEFAULT}_danmaku_hotwords.json": "danmaku_hotwords.json",
}

SOURCE_SUFFIXES = {".m4a", ".webm", ".mp3", ".m4s", ".mp4", ".opus", ".ogg", ".flac", ".mkv"}


def job_media_dir(work_dir: Path) -> Path:
    d = Path(work_dir) / "media"
    d.mkdir(parents=True, exist_ok=True)
    return d


def job_subs_dir(work_dir: Path) -> Path:
    d = Path(work_dir) / "subs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def existing_wav(work_dir: Path, stem: str = STEM_DEFAULT) -> Path | None:
    """Return the job WAV if present (nested media/ first, then legacy flat)."""
    work_dir = Path(work_dir)
    for path in (work_dir / "media" / f"{stem}.wav", work_dir / f"{stem}.wav"):
        if path.is_file():
            return path
    return None


def locale_srt_path(work_dir: Path, lang: str, stem: str = STEM_DEFAULT) -> Path:
    """Canonical subs/{lang}.srt; fall back to legacy flat files if still present."""
    tag = file_tag(coalesce_source_lang(lang))
    nested = Path(work_dir) / "subs" / f"{tag}.srt"
    if nested.is_file():
        return nested
    for name in (f"{stem}_{tag}.srt", f"{stem}_{tag}_fixed.srt", f"{stem}_src.srt"):
        flat = Path(work_dir) / name
        if flat.is_file():
            return flat
    return nested


def list_locale_srts(work_dir: Path, stem: str = STEM_DEFAULT) -> dict[str, Path]:
    """Map language tag → SRT path. Nested subs/ wins over legacy flat files."""
    work_dir = Path(work_dir)
    found: dict[str, Path] = {}
    prefix = f"{stem}_"
    for p in work_dir.glob(f"{stem}_*.srt"):
        tag = p.stem[len(prefix) :]
        if tag.endswith("_fixed"):
            tag = tag[: -len("_fixed")]
        if tag == "src":
            tag = "en"
        found.setdefault(tag, p)
    subs = work_dir / "subs"
    if subs.is_dir():
        for p in subs.glob("*.srt"):
            tag = p.stem
            if tag.endswith("_fixed"):
                continue
            if tag == "src":
                tag = "en"
            found[tag] = p
    return found


def _relocate(src: Path, dest: Path, stats: dict[str, list[str]]) -> None:
    if not src.is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dest.resolve():
        return
    if dest.exists():
        src.unlink()
        stats["deleted"].append(str(src))
        return
    shutil.move(str(src), str(dest))
    stats["moved"].append(f"{src.name} -> {dest.relative_to(src.parent)}")


def reorganize_job_dir(work_dir: Path, stem: str = STEM_DEFAULT) -> dict[str, list[str]]:
    """Move artifacts into media/subs/notes; drop txt, _fixed, and src duplicates."""
    work_dir = Path(work_dir)
    stats: dict[str, list[str]] = {"moved": [], "deleted": []}
    if not work_dir.is_dir():
        return stats

    media = job_media_dir(work_dir)
    subs = work_dir / "subs"

    for flat_name, nested_name in MEDIA_FLAT.items():
        _relocate(work_dir / flat_name, media / nested_name, stats)

    for src in list(work_dir.iterdir()):
        if src.is_file() and src.suffix.lower() in SOURCE_SUFFIXES and src.name.lower().startswith("source."):
            _relocate(src, media / src.name, stats)

    by_tag: dict[str, Path] = {}
    extras: list[Path] = []
    prefix = f"{stem}_"
    for p in sorted(work_dir.glob(f"{stem}_*.srt")):
        tag = p.stem[len(prefix) :]
        is_fixed = tag.endswith("_fixed")
        if is_fixed:
            tag = tag[: -len("_fixed")]
        if tag == "src":
            extras.append(p)
            continue
        prev = by_tag.get(tag)
        if prev is None:
            by_tag[tag] = p
            continue
        # Prefer the non-_fixed file as source of truth.
        if is_fixed:
            extras.append(p)
        else:
            extras.append(prev)
            by_tag[tag] = p

    for tag, src in by_tag.items():
        _relocate(src, subs / f"{tag}.srt", stats)
    for extra in extras:
        if extra.is_file():
            extra.unlink()
            stats["deleted"].append(str(extra))

    src_nested = subs / "src.srt"
    en_nested = subs / "en.srt"
    if src_nested.is_file() and en_nested.is_file():
        src_nested.unlink()
        stats["deleted"].append(str(src_nested))
    elif src_nested.is_file() and not en_nested.is_file():
        _relocate(src_nested, en_nested, stats)

    for p in list(work_dir.glob(f"{stem}_*.txt")):
        if p.is_file():
            p.unlink()
            stats["deleted"].append(str(p))
    for p in list(work_dir.glob("*_fixed.srt")) + list(work_dir.glob("*_fixed.txt")):
        if p.is_file():
            p.unlink()
            stats["deleted"].append(str(p))
    for p in list(work_dir.glob("*.part")):
        if p.is_file():
            p.unlink()
            stats["deleted"].append(str(p))
    for p in list(subs.glob("*_fixed.srt")) + list(subs.glob("*.txt")):
        if p.is_file():
            p.unlink()
            stats["deleted"].append(str(p))

    from pipeline import purge_legacy_summary_files

    for removed in purge_legacy_summary_files(work_dir, stem):
        stats["deleted"].append(str(removed))

    subs_dir = work_dir / "subs"
    if subs_dir.is_dir() and not any(subs_dir.iterdir()):
        subs_dir.rmdir()

    return stats


def prune_empty_job_dirs(root: Path) -> list[str]:
    """Remove leftover incomplete job folders (empty or only .part files)."""
    removed: list[str] = []
    root = Path(root)
    if not root.is_dir():
        return removed
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        files = [p for p in d.rglob("*") if p.is_file()]
        if not files:
            shutil.rmtree(d)
            removed.append(str(d))
            continue
        if all(p.suffix == ".part" or p.name.endswith(".part") for p in files):
            shutil.rmtree(d)
            removed.append(str(d))
    return removed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Reorganize job work dirs into media/subs/notes")
    p.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent / "downloads" / "batch",
        help="batch work root",
    )
    p.add_argument("--dir", type=Path, default=None, help="single work dir")
    args = p.parse_args(argv)
    targets = [args.dir] if args.dir else sorted(p for p in args.root.iterdir() if p.is_dir())
    for work in targets:
        stats = reorganize_job_dir(work)
        print(
            f"[layout] {work.name} moved={len(stats['moved'])} "
            f"deleted={len(stats['deleted'])}"
        )
    if args.dir is None:
        for gone in prune_empty_job_dirs(args.root):
            print(f"[layout] removed empty {gone}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
