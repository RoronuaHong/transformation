"""Push finished batch work dirs into materials_hub (external path refs).

Best-effort: missing hub / import errors never fail the pipeline.
Disable with env ``VITUAL_HUB_SYNC=0``.

After register, ``bridge.run_job_dir`` (when enabled) will:
  - demux new videos → silent + track (``VITUAL_HUB_SPLIT_SILENT=0`` to disable)
  - auto_process thumbs/OCR/shots/pHash (``VITUAL_HUB_AUTOPROC=0`` to disable)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SP_ROOT = Path(__file__).resolve().parents[1]
_HUB_ROOT = _SP_ROOT.parent / "materials_hub"


def push_work_dir_to_hub(
    work_dir: Path | str,
    *,
    platform: str = "",
    video_id: str = "",
    title: str = "",
) -> dict | None:
    """Register artifacts under ``work_dir`` into materials_hub. Never raises."""
    if os.environ.get("VITUAL_HUB_SYNC", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return None
    wd = Path(work_dir)
    if not wd.is_dir():
        return None
    hub = str(_HUB_ROOT.resolve())
    if hub not in sys.path:
        sys.path.insert(0, hub)
    try:
        import bridge_subtitle  # type: ignore
    except Exception as e:
        print(f"[hub] skip import materials_hub ({type(e).__name__}: {e})")
        return None
    try:
        st = bridge_subtitle.run_job_dir(
            str(wd),
            root=str(_SP_ROOT),
            dry_run=False,
            platform=platform or "",
            video_id=video_id or "",
            title=title or "",
        )
        print(
            f"[hub] materials registered added={st.get('added', 0)} "
            f"skipped={st.get('skipped', 0)} kinds={st.get('by_kind')}"
        )
        return st
    except Exception as e:
        print(f"[hub] register failed ({type(e).__name__}: {e})")
        return None
