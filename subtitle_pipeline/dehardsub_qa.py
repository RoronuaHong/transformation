"""Dehardsub QA gate: residual score + before/after frame pairs for fixed work_dirs.

Usage:
  .venv/Scripts/python.exe dehardsub_qa.py downloads/batch/bilibili_BV1UQ8RzcEGF [...]
  .venv/Scripts/python.exe dehardsub_qa.py --batch-glob "downloads/batch/bilibili_BV1*"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


def _job_paths(work: Path) -> dict[str, Path]:
    from job_layout import job_media_dir
    from note_frames import existing_source_video

    media = job_media_dir(work)
    clean = media / "dehardsub" / "clean.mp4"
    meta = media / "dehardsub" / "dehardsub_meta.json"
    src = existing_source_video(work)
    # Prefer true source.mp4 over newest-mtime (clean can be newer after a run).
    preferred = media / "source.mp4"
    if preferred.is_file():
        src = preferred
    return {
        "work": work,
        "media": media,
        "source": src or media / "source.mp4",
        "clean": clean,
        "meta": meta,
        "audit": media / "dehardsub" / "_qa",
    }


def _load_box(meta_path: Path, width: int, height: int) -> dict[str, int]:
    from media_ops import clamp_hardsub_box, hardsub_band_box

    if meta_path.is_file():
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        box = data.get("box") or (data.get("regions") or [{}])[0]
        if isinstance(box, dict) and all(k in box for k in ("x", "y", "w", "h")):
            return clamp_hardsub_box(
                {k: int(box[k]) for k in ("x", "y", "w", "h")}, width, height
            )
    return clamp_hardsub_box(hardsub_band_box(width, height, 0.14), width, height)


def _grab_pair(
    src: Path,
    clean: Path,
    t_sec: float,
    dest_dir: Path,
    stem: str,
) -> dict[str, str] | None:
    from media_ops import _grab_jpeg_frame

    dest_dir.mkdir(parents=True, exist_ok=True)
    a = dest_dir / f"{stem}_src.jpg"
    b = dest_dir / f"{stem}_clean.jpg"
    if _grab_jpeg_frame(src, t_sec, a) is None:
        return None
    if _grab_jpeg_frame(clean, t_sec, b) is None:
        return None
    return {"t": f"{t_sec:.2f}", "src": a.name, "clean": b.name}


def qa_one(work: Path, *, samples: int = 5) -> dict[str, Any]:
    from media_ops import probe_duration_sec, probe_video_wh
    from visual_cleanup import sample_validate_video

    paths = _job_paths(work)
    out: dict[str, Any] = {
        "work_dir": str(work),
        "ok": False,
        "source": str(paths["source"]),
        "clean": str(paths["clean"]),
    }
    if not paths["source"].is_file():
        out["error"] = "missing source.mp4"
        return out
    if not paths["clean"].is_file():
        out["error"] = "missing clean.mp4"
        return out

    wh = probe_video_wh(paths["clean"]) or probe_video_wh(paths["source"])
    if not wh:
        out["error"] = "probe failed"
        return out
    width, height = wh
    box = _load_box(paths["meta"], width, height)
    regions = [{"kind": "hardsub", **box}]
    encode_mode = None
    if paths["meta"].is_file():
        meta = json.loads(paths["meta"].read_text(encoding="utf-8"))
        encode_mode = (meta.get("encode") or {}).get("mode")
        out["box_source"] = meta.get("box_source")
        out["action"] = meta.get("action")
        out["box_seed"] = meta.get("box_seed")

    src_val = sample_validate_video(
        paths["source"], regions, samples=samples
    )
    clean_val = sample_validate_video(
        paths["clean"], regions, samples=samples, ref_video=paths["source"]
    )
    dur = probe_duration_sec(paths["source"]) or 0.0
    pairs: list[dict[str, str]] = []
    if dur > 0.5:
        for i, frac in enumerate([0.2, 0.5, 0.8][:3]):
            pair = _grab_pair(
                paths["source"],
                paths["clean"],
                dur * frac,
                paths["audit"],
                f"pair_{i:02d}_{int(dur * frac)}s",
            )
            if pair:
                pairs.append(pair)

    src_h = float(src_val.get("hardsub") or 0.0)
    clean_h = float(clean_val.get("hardsub") or 0.0)
    improved = clean_h < src_h * 0.85 or clean_h <= 0.02
    out.update(
        {
            "ok": bool(clean_val.get("ok")) and improved,
            "box": box,
            "encode_mode": encode_mode,
            "src_residual": src_val.get("hardsub"),
            "clean_residual": clean_val.get("hardsub"),
            "src_combined": src_val.get("combined"),
            "clean_combined": clean_val.get("combined"),
            "improved": improved,
            "pairs": pairs,
            "audit_dir": str(paths["audit"]),
        }
    )
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Dehardsub residual QA for work_dirs")
    p.add_argument("works", nargs="*", type=Path, help="work_dir paths")
    p.add_argument(
        "--batch-glob",
        default="",
        help='Glob under subtitle_pipeline, e.g. "downloads/batch/bilibili_BV1*"',
    )
    p.add_argument("--samples", type=int, default=5)
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write aggregated JSON (default: first work's dehardsub/_qa/dehardsub_qa.json)",
    )
    args = p.parse_args(argv)
    works: list[Path] = [Path(w) for w in args.works]
    if args.batch_glob:
        works.extend(sorted(ROOT.glob(args.batch_glob)))
    works = [w.resolve() for w in works if w.is_dir()]
    if not works:
        print("no work_dirs", file=sys.stderr)
        return 2

    rows = [qa_one(w, samples=max(3, int(args.samples))) for w in works]
    payload = {
        "count": len(rows),
        "pass": sum(1 for r in rows if r.get("ok")),
        "fail": sum(1 for r in rows if not r.get("ok")),
        "jobs": rows,
    }
    out = args.out
    if out is None:
        out = Path(rows[0]["audit_dir"]) / "dehardsub_qa.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[dehardsub_qa] wrote {out}", flush=True)
    return 0 if payload["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
