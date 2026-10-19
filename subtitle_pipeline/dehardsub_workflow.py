"""Dehardsub detect → confirm → process workflow (2026-10-19).

Phase 1  detect : locate subtitle regions (OCR-primary), write
                 media/dehardsub/dehardsub_regions.json + dehardsub_regions_preview.jpg.
                 Human reviews the boxes / preview image.
Phase 2  confirm: mark confirmed:true (optionally edit boxes), then run the cleanup
                 with the confirmed boxes — no re-detection, no box drift.

Usage:
  .venv/Scripts/python.exe dehardsub_workflow.py detect  downloads/batch/bilibili_BV1fRXxY1Eke
  .venv/Scripts/python.exe dehardsub_workflow.py confirm downloads/batch/bilibili_BV1fRXxY1Eke [sttn|opencv]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_ops import detect_dehardsub_regions, strip_hardsubs, job_dehardsub_dir


def _cmd_detect(work: Path) -> None:
    p = detect_dehardsub_regions(work)
    print(json.dumps(p, ensure_ascii=False, indent=2))
    print(f"\nPreview: {job_dehardsub_dir(work) / 'dehardsub_regions_preview.jpg'}")
    print("Review the boxes. Edit dehardsub_regions.json if needed, set confirmed:true,")
    print("then: python dehardsub_workflow.py confirm <work_dir>")


def _cmd_confirm(work: Path, *, engine: str = "sttn") -> int:
    rj = job_dehardsub_dir(work) / "dehardsub_regions.json"
    if not rj.is_file():
        print("no dehardsub_regions.json — run detect first", file=sys.stderr)
        return 2
    data = json.loads(rj.read_text(encoding="utf-8"))
    boxes = data.get("boxes")
    if not boxes:
        print("regions.json has no boxes", file=sys.stderr)
        return 2
    data["confirmed"] = True
    rj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    out, rep = strip_hardsubs(work, mode="auto", boxes_override=boxes, engine=engine)
    print("clean ->", out)
    print(
        json.dumps(
            {k: rep.get(k) for k in ("action", "box", "boxes", "box_roles", "box_source")},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "detect"
    work = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("downloads/batch/bilibili_BV1fRXxY1Eke")
    if cmd in ("detect", "d"):
        _cmd_detect(work)
        return
    if cmd in ("confirm", "run", "r", "process", "p"):
        engine = sys.argv[3] if len(sys.argv) > 3 else "sttn"
        raise SystemExit(_cmd_confirm(work, engine=engine))
    print(__doc__)


if __name__ == "__main__":
    main()
