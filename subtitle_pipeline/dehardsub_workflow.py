"""Dehardsub detect → confirm → process workflow.

Phase 1  detect : locate subtitle / watermark regions (OCR-primary), write
                 media/dehardsub/dehardsub_regions.json + dehardsub_regions_preview.jpg.
                 Human reviews the boxes / preview image.
Phase 2  confirm: mark confirmed:true (optionally edit boxes), then run the cleanup
                 with the confirmed boxes — no re-detection, no box drift.

Default engine is hybrid ``ocr_telea`` (dialogue OCR+TELEA; title/watermark
boxfill/edge; up_watermark OCR then boxfill fallback). Optional ``lama``.

Usage:
  .venv/Scripts/python.exe dehardsub_workflow.py detect  downloads/batch/bilibili_BV1fRXxY1Eke
  .venv/Scripts/python.exe dehardsub_workflow.py confirm downloads/batch/bilibili_BV1fRXxY1Eke [ocr_telea|lama|auto]
  .venv/Scripts/python.exe dehardsub_workflow.py confirm ... --force   # bypass D4 gates
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_ops import (
    build_dehardsub_gates,
    detect_dehardsub_regions,
    job_dehardsub_dir,
    strip_hardsubs,
)


def _cmd_detect(work: Path) -> None:
    p = detect_dehardsub_regions(work)
    print(json.dumps(p, ensure_ascii=False, indent=2))
    print(f"\nPreview: {job_dehardsub_dir(work) / 'dehardsub_regions_preview.jpg'}")
    gates = p.get("gates") or {}
    if not gates.get("ok", True):
        print(
            f"GATES NOT OK: {gates.get('reasons')} — raise top watermark boxes "
            f"before confirm (or confirm --force).",
            file=sys.stderr,
        )
    print("Review the boxes. Edit dehardsub_regions.json if needed, set confirmed:true,")
    print("then: python dehardsub_workflow.py confirm <work_dir>")


def _cmd_confirm(work: Path, *, engine: str = "auto", force: bool = False) -> int:
    from note_frames import existing_source_video

    rj = job_dehardsub_dir(work) / "dehardsub_regions.json"
    if not rj.is_file():
        print("no dehardsub_regions.json — run detect first", file=sys.stderr)
        return 2
    data = json.loads(rj.read_text(encoding="utf-8"))
    boxes = data.get("boxes")
    if not boxes:
        print("regions.json has no boxes", file=sys.stderr)
        return 2
    src = existing_source_video(work)
    if src is None or not src.is_file():
        print(f"no source video in {work}", file=sys.stderr)
        return 2
    gates = build_dehardsub_gates(src, boxes)
    data["gates"] = {
        "ok": bool(gates.get("ok")),
        "top_min_h_ok": bool(gates.get("top_min_h_ok")),
        "outside_box_bright_blobs": int(gates.get("outside_box_bright_blobs") or 0),
        "reasons": list(gates.get("reasons") or []),
        "spill_warnings": gates.get("spill_warnings") or [],
    }
    if gates.get("boxes_normalized"):
        boxes = list(gates["boxes_normalized"])
        data["boxes"] = boxes
    # Keep wipe_plan actions; refresh geometry. Never call with duration=0
    # (that wiped windows on gates-blocked writes).
    from media_ops import build_wipe_plan, probe_duration_sec

    dur = float(probe_duration_sec(src) or 0.0)
    prev_plan = {str(t.get("id")): t for t in (data.get("wipe_plan") or []) if t.get("id")}
    wipe_plan = build_wipe_plan(boxes, duration_sec=dur)
    for t in wipe_plan:
        prev = prev_plan.get(str(t.get("id")))
        if prev and str(prev.get("action") or "").lower() in ("remove", "keep"):
            t["action"] = str(prev.get("action")).lower()
        # Preserve prior windows if rebuild somehow empty.
        if prev and (prev.get("windows") or []) and not (t.get("windows") or []):
            t["windows"] = list(prev.get("windows") or [])
        # Mirror keep onto boxes so strip_hardsubs filter sees it even without plan.
        if t["action"] == "keep":
            try:
                idx = int(str(t["id"]).rsplit("_", 1)[-1])
                if 0 <= idx < len(boxes):
                    boxes[idx]["action"] = "keep"
            except ValueError:
                pass
    data["wipe_plan"] = wipe_plan
    data["boxes"] = boxes
    if (not gates.get("ok")) and (not force):
        data["confirmed"] = False
        rj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            "GATES BLOCKED confirm/process: "
            + ", ".join(gates.get("reasons") or [])
            + " — edit boxes or re-run with --force",
            file=sys.stderr,
        )
        return 3
    data["confirmed"] = True
    rj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    out, rep = strip_hardsubs(
        work,
        mode="auto",
        boxes_override=boxes,
        engine=engine,
        force=force,
    )
    print("clean ->", out)
    print(
        json.dumps(
            {
                k: rep.get(k)
                for k in (
                    "action",
                    "box",
                    "boxes",
                    "box_roles",
                    "box_source",
                    "quality",
                    "quality_note",
                    "gates",
                    "wipe_plan",
                    "active_windows",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if rep.get("action") != "gates_blocked" else 3


def main() -> None:
    argv = list(sys.argv[1:])
    force = False
    if "--force" in argv:
        force = True
        argv = [a for a in argv if a != "--force"]
    cmd = argv[0] if argv else "detect"
    work = Path(argv[1]) if len(argv) > 1 else Path("downloads/batch/bilibili_BV1fRXxY1Eke")
    if cmd in ("detect", "d"):
        _cmd_detect(work)
        return
    if cmd in ("confirm", "run", "r", "process", "p"):
        engine = argv[2] if len(argv) > 2 else "auto"
        raise SystemExit(_cmd_confirm(work, engine=engine, force=force))
    print(__doc__)


if __name__ == "__main__":
    main()
