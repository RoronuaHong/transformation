"""CI / local sync gate: unit tests + optional work_dir audit + spot-check."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_WORK = ROOT / "downloads" / "batch" / "youtube_kV7RuutRx-s"

# Fast sync-related suite (no full-minute remix ffmpeg burns).
UNIT_TARGETS = [
    "test_embed_shift.py",
    "test_export_site.py",
    "test_spot_check.py",
    "test_segment_subs.py",
    "test_merge_slices.py",
    "test_media_ops.py",
]
UNIT_IGNORE = "remix_burns or run_postproc or vertical_ffmpeg"


def _run(cmd: list[str]) -> int:
    print(f"[sync:gate] $ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(ROOT))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Sync quality gate")
    p.add_argument(
        "--ci",
        action="store_true",
        help="CI mode: unit tests required; work_dir audit soft-skips if missing",
    )
    p.add_argument("--skip-unit", action="store_true")
    p.add_argument("--skip-audit", action="store_true")
    p.add_argument("--work", type=Path, default=DEFAULT_WORK)
    p.add_argument("--require-remix", action="store_true")
    args = p.parse_args(argv)

    py = sys.executable
    rc = 0

    if not args.skip_unit:
        unit_rc = _run(
            [
                py,
                "-m",
                "pytest",
                *UNIT_TARGETS,
                "-q",
                "--tb=line",
                "-k",
                f"not ({UNIT_IGNORE})",
            ]
        )
        if unit_rc != 0:
            rc = unit_rc

    if not args.skip_audit:
        work = Path(args.work)
        if not work.is_dir():
            msg = f"[sync:gate] work_dir missing: {work}"
            if args.ci or not args.require_remix:
                print(f"{msg} (audit skipped)")
            else:
                print(msg, file=sys.stderr)
                rc = rc or 2
        else:
            audit_cmd = [py, str(ROOT / "sync_audit.py"), str(work)]
            if args.require_remix or not args.ci:
                # Local default: prefer remix gate when smoke work_dir exists.
                if (work / "media" / "remix" / "remix.mp4").is_file():
                    audit_cmd.append("--require-remix")
            audit_rc = _run(audit_cmd)
            if audit_rc != 0:
                rc = audit_rc
            spot_rc = _run([py, str(ROOT / "spot_check.py"), str(work)])
            if spot_rc != 0:
                rc = spot_rc

    summary = {"ok": rc == 0, "exit": rc, "ci": bool(args.ci)}
    print(json.dumps(summary, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
