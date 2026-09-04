"""S8: shift work_dir captions when embed player clock differs from downloaded wav."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pipeline import parse_srt, write_srt
from sync_utils import shift_segments, write_sync_meta

ROOT = Path(__file__).resolve().parent
PRESETS_EXAMPLE = ROOT / "embed_shift.presets.example.json"
PRESETS_LOCAL = ROOT / "embed_shift.presets.json"


def load_embed_shift_presets() -> dict[str, int]:
    """Platform → ms. Local presets override example; env wins over both for `*`."""
    out: dict[str, int] = {}
    for path in (PRESETS_EXAMPLE, PRESETS_LOCAL):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for key, val in data.items():
            if str(key).startswith("_"):
                continue
            try:
                out[str(key).strip().lower()] = int(val)
            except (TypeError, ValueError):
                continue
    return out


def resolve_embed_shift_ms(
    platform: str | None = None,
    *,
    explicit: int | None = None,
) -> int:
    """Return milliseconds to delay captions (positive) or advance (negative)."""
    if explicit is not None:
        return int(explicit)
    env = os.environ.get("VITUAL_EMBED_SHIFT_MS")
    if env is not None and str(env).strip() != "":
        return int(env)
    presets = load_embed_shift_presets()
    plat = (platform or "").strip().lower()
    if plat and plat in presets:
        return int(presets[plat])
    if "*" in presets:
        return int(presets["*"])
    return 0


def apply_embed_shift(
    work_dir: Path,
    ms: int,
    *,
    reason: str = "embed_ads",
    platform: str | None = None,
    in_place: bool = True,
) -> dict:
    """Shift every ``subs/*.srt`` and refresh ``media/sync_meta.json``.

    Use when the **embed iframe** clock leads/lags the downloaded wav used for ASR.
    Measure three points (start/mid/end) against the embed player before setting ms.
    """
    work_dir = Path(work_dir)
    subs = work_dir / "subs"
    if not subs.is_dir():
        raise FileNotFoundError(f"no subs/ under {work_dir}")
    ms = int(ms)
    shifted_files: list[str] = []
    for srt in sorted(subs.glob("*.srt")):
        segs = parse_srt(srt)
        if not segs:
            continue
        out_segs = shift_segments(segs, ms)
        if in_place:
            dest = srt
        else:
            dest = srt.with_name(f"{srt.stem}_shift{ms}{srt.suffix}")
        write_srt(out_segs, dest)
        shifted_files.append(dest.name)
        write_sync_meta(
            dest.with_name(dest.stem + "_sync_meta.json"),
            {
                "source_srt": str(srt) if dest != srt else str(dest),
                "out_srt": str(dest),
                "sync_shift_ms": ms,
                "reason": reason,
                "platform": platform,
                "cue_count": len(out_segs),
            },
        )
    media = work_dir / "media"
    media.mkdir(parents=True, exist_ok=True)
    meta_path = media / "sync_meta.json"
    prev: dict = {}
    if meta_path.is_file():
        try:
            prev = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            prev = {}
    prev_shift = int(prev.get("sync_shift_ms") or 0)
    payload = {
        **prev,
        "sync_shift_ms": (prev_shift + ms) if in_place else ms,
        "embed_shift_ms": ms,
        "reason": reason,
        "platform": platform,
        "shifted_srts": shifted_files,
    }
    write_sync_meta(meta_path, payload)
    return {
        "work_dir": str(work_dir),
        "ms": ms,
        "reason": reason,
        "files": shifted_files,
        "sync_meta": str(meta_path),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="S8 embed-ads: shift work_dir SRTs to match embed player clock"
    )
    p.add_argument("--work", type=Path, required=True, help="job work_dir")
    p.add_argument(
        "--ms",
        type=int,
        default=None,
        help="shift milliseconds (omit → preset/env for --platform)",
    )
    p.add_argument("--platform", default=None, help="youtube|bilibili|… for presets")
    p.add_argument("--reason", default="embed_ads")
    p.add_argument(
        "--copy",
        action="store_true",
        help="write *_shiftN.srt instead of overwriting",
    )
    args = p.parse_args(argv)
    ms = resolve_embed_shift_ms(args.platform, explicit=args.ms)
    if ms == 0:
        print("[embed_shift] ms=0 — nothing to apply (set --ms or presets/env)")
        return 0
    result = apply_embed_shift(
        args.work,
        ms,
        reason=args.reason,
        platform=args.platform,
        in_place=not args.copy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
