"""Merge multi-slice ASR SRTs onto one timeline (S1 guard).

Never concatenate clip SRTs that all start at 0. Each part needs
``slice_start_sec`` (source clock) or sequential clip durations (clips clock).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from media_ops import clip_span_durations, load_clip_spans
from pipeline import parse_srt, write_srt
from sync_utils import merge_offset_segments, validate_srt_monotonic, write_sync_meta

ROOT = Path(__file__).resolve().parent
_RANGE_RE = re.compile(r"range_(\d{2})", re.I)


def _resolve_clip_srt(clips_dir: Path, index: int, lang: str | None) -> Path | None:
    """Locate per-range SRT for range_XX."""
    stem = f"range_{index:02d}"
    candidates: list[Path] = []
    if lang:
        tag = str(lang).strip()
        candidates.extend(
            [
                clips_dir / stem / f"{tag}.srt",
                clips_dir / f"{stem}_{tag}.srt",
                clips_dir / f"{stem}.{tag}.srt",
            ]
        )
    candidates.extend(
        [
            clips_dir / f"{stem}.srt",
            clips_dir / stem / "en.srt",
            clips_dir / stem / "zh.srt",
        ]
    )
    # Any locale under range_XX/
    sub = clips_dir / stem
    if sub.is_dir():
        candidates.extend(sorted(sub.glob("*.srt")))
    for path in candidates:
        if path.is_file() and path.stat().st_size > 8:
            return path
    return None


def discover_clip_parts(
    work_dir: Path,
    *,
    lang: str | None = None,
    clock: str = "source",
) -> list[dict]:
    """Build merge parts from ``media/clips/clips_meta.json`` + range_XX SRTs."""
    work_dir = Path(work_dir)
    spans = load_clip_spans(work_dir)
    if not spans:
        return []
    clips_dir = work_dir / "media" / "clips"
    durs = clip_span_durations(work_dir, spans)
    parts: list[dict] = []
    cursor = 0.0
    for i, (start, end) in enumerate(spans):
        srt = _resolve_clip_srt(clips_dir, i, lang)
        if srt is None:
            continue
        if clock == "clips":
            offset = cursor
        else:
            offset = float(start)
        dur = durs[i] if i < len(durs) else max(0.05, float(end) - float(start))
        parts.append(
            {
                "srt": str(srt),
                "index": i,
                "slice_start_sec": round(float(offset), 3),
                "span_start": round(float(start), 3),
                "span_end": round(float(end), 3),
                "duration": round(float(dur), 3),
            }
        )
        cursor += float(dur)
    return parts


def merge_slice_parts(
    parts: list[tuple[Path, float]],
    dest: Path,
    *,
    clock: str = "source",
    fix_overlaps: bool = True,
    meta_extra: dict | None = None,
) -> dict:
    """Merge ``(srt_path, offset_sec)`` parts → dest SRT + sync meta."""
    loaded: list[tuple[list[dict], float]] = []
    detail: list[dict] = []
    for path, offset in parts:
        path = Path(path)
        segs = parse_srt(path)
        loaded.append((segs, float(offset)))
        detail.append(
            {
                "srt": path.name,
                "slice_start_sec": round(float(offset), 3),
                "cues": len(segs),
            }
        )
    merged = merge_offset_segments(loaded, fix_overlaps=fix_overlaps)
    warnings = validate_srt_monotonic(merged)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    write_srt(merged, dest)
    meta_path = dest.with_name(dest.stem + "_sync_meta.json")
    # Also write/update media/sync_meta when dest is under a work_dir/subs
    payload = {
        "merged_from_slices": True,
        "clock": clock,
        "parts": detail,
        "cue_count": len(merged),
        "out_srt": str(dest),
        "monotonic_warnings": warnings,
        **(meta_extra or {}),
    }
    write_sync_meta(meta_path, payload)
    media_meta = dest.parent.parent / "media" / "sync_meta.json"
    if dest.parent.name == "subs" and dest.parent.parent.exists():
        prev: dict = {}
        if media_meta.is_file():
            try:
                prev = json.loads(media_meta.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                prev = {}
        prev.update(
            {
                "merged_from_slices": True,
                "slice_merge_clock": clock,
                "slice_merge_parts": detail,
                "cue_count": len(merged),
                "source_srt": str(dest),
            }
        )
        write_sync_meta(media_meta, prev)
    return {
        "dest": str(dest),
        "meta": str(meta_path),
        "cue_count": len(merged),
        "parts": detail,
        "warnings": warnings,
        "clock": clock,
    }


def merge_work_dir_clips(
    work_dir: Path,
    *,
    lang: str | None = None,
    clock: str = "source",
    dest: Path | None = None,
) -> dict:
    """Merge discovered range_XX SRTs into ``subs/{lang}.srt`` (source clock default)."""
    work_dir = Path(work_dir)
    clock_name = (clock or "source").strip().lower()
    if clock_name not in {"source", "clips"}:
        raise ValueError(f"clock must be source|clips, got {clock}")
    discovered = discover_clip_parts(work_dir, lang=lang, clock=clock_name)
    if len(discovered) < 1:
        raise FileNotFoundError(
            f"no range_XX SRTs under {work_dir / 'media' / 'clips'} "
            "(need clips_meta.json + range_00.srt or range_00/{lang}.srt)"
        )
    if len(discovered) < 2 and clock_name == "source":
        # Still allow single-part merge (applies offset) for consistency.
        pass
    parts = [(Path(p["srt"]), float(p["slice_start_sec"])) for p in discovered]
    if dest is None:
        tag = (lang or "en").strip() or "en"
        dest = work_dir / "subs" / f"{tag}.srt"
    return merge_slice_parts(
        parts,
        dest,
        clock=clock_name,
        meta_extra={"work_dir": str(work_dir), "discovered": discovered},
    )


def merge_from_manifest(manifest: Path, dest: Path, *, clock: str = "source") -> dict:
    data = json.loads(Path(manifest).read_text(encoding="utf-8-sig"))
    rows = data.get("parts") if isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows:
        raise ValueError("manifest needs parts: [{srt, slice_start_sec}, ...]")
    base = Path(manifest).parent
    parts: list[tuple[Path, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        srt = Path(str(row.get("srt") or ""))
        if not srt.is_absolute():
            srt = base / srt
        offset = float(row.get("slice_start_sec") or row.get("offset") or 0)
        parts.append((srt, offset))
    clock_name = str((data.get("clock") if isinstance(data, dict) else None) or clock)
    return merge_slice_parts(parts, dest, clock=clock_name)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Merge multi-slice ASR SRTs with per-clip offsets (S1)"
    )
    p.add_argument("--work", type=Path, help="job work_dir with media/clips")
    p.add_argument("--manifest", type=Path, help="JSON parts list")
    p.add_argument(
        "--part",
        action="append",
        default=[],
        help="srt_path:slice_start_sec (repeatable)",
    )
    p.add_argument("--out", type=Path, help="output SRT path")
    p.add_argument("--lang", default="en", help="locale for work_dir merge / dest name")
    p.add_argument(
        "--clock",
        choices=("source", "clips"),
        default="source",
        help="source=span starts; clips=concat body clock",
    )
    args = p.parse_args(argv)

    try:
        if args.work:
            result = merge_work_dir_clips(
                args.work, lang=args.lang, clock=args.clock, dest=args.out
            )
        elif args.manifest:
            if not args.out:
                print("error: --out required with --manifest", file=sys.stderr)
                return 2
            result = merge_from_manifest(args.manifest, args.out, clock=args.clock)
        elif args.part:
            if not args.out:
                print("error: --out required with --part", file=sys.stderr)
                return 2
            parts: list[tuple[Path, float]] = []
            for raw in args.part:
                if ":" not in raw:
                    print(f"error: bad --part {raw!r} (want path:sec)", file=sys.stderr)
                    return 2
                # Windows paths have drive colon — split from the right once for seconds.
                path_s, _, sec_s = raw.rpartition(":")
                parts.append((Path(path_s), float(sec_s)))
            result = merge_slice_parts(parts, args.out, clock=args.clock)
        else:
            p.print_help()
            return 2
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
