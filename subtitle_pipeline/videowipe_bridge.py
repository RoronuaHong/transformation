"""Optional VideoWipe backend — open equivalent of PeelSub's published method.

PeelSub ([peelsub.com](https://peelsub.com) / PeelSub/PeelSub) ships **no algorithm
source**: docs-only managed service. Their public method is:

  1) locate fixed-position hard-sub edges
  2) remove text pixels
  3) reconstruct covered area from surroundings + **frame continuity**
  4) accept on **normal playback** (no residue / smear / flicker)

VideoWipe (KKenny0/videowipe) is the closest open implementation of that stack
(detect → WipePlan → STTN temporal inpaint). Vendor tree lives under
``_vendor/videowipe`` (gitignored).

Usage::

    from videowipe_bridge import wipe_regions
    wipe_regions(src, out, boxes=[{"x":1,"y":1,"w":230,"h":48}])
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


_VENDOR = Path(__file__).resolve().parent / "_vendor" / "videowipe" / "src"


def videowipe_available() -> bool:
    if not _VENDOR.is_dir():
        return False
    if str(_VENDOR) not in sys.path:
        sys.path.insert(0, str(_VENDOR))
    try:
        from videowipe.engine import WipeEngine  # noqa: F401

        return True
    except Exception:
        return False


def wipe_regions(
    src: Path | str,
    dest: Path | str,
    boxes: list[dict[str, int]],
    *,
    device: str = "auto",
    weight: str | None = None,
    trial_frames: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Run VideoWipe STTN over fixed boxes (PeelSub-style covered-area repair).

    ``boxes`` use our ``{x,y,w,h}`` pixels. Needs onnxruntime (or torch) and
    STTN weights (auto-downloaded on first run into ``~/.videowipe/weights``).
    """
    import cv2
    import numpy as np

    src = Path(src)
    dest = Path(dest)
    if not videowipe_available():
        return {
            "ok": False,
            "reason": "videowipe_missing",
            "hint": "clone KKenny0/videowipe → _vendor/videowipe (deps: onnxruntime)",
        }
    from videowipe.api import WipeRequest
    from videowipe.engine import WipeEngine

    cap = cv2.VideoCapture(str(src))
    ok, fr0 = cap.read()
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    if not ok:
        return {"ok": False, "reason": "open_failed"}
    h, w = fr0.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for b in boxes:
        x0, y0 = int(b["x"]), int(b["y"])
        x1 = min(w, x0 + max(1, int(b["w"])))
        y1 = min(h, y0 + max(1, int(b["h"])))
        mask[y0:y1, x0:x1] = 255

    out_dir = dest.parent / "_videowipe_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    mask_path = out_dir / "union_mask.png"
    cv2.imwrite(str(mask_path), mask)
    (out_dir / "bridge_request.json").write_text(
        json.dumps(
            {
                "src": str(src),
                "dest": str(dest),
                "boxes": boxes,
                "device": device,
                "trial_frames": trial_frames,
                "frame_count": n,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    req_kw: dict[str, Any] = {
        "video": str(src),
        "mask": str(mask_path),
        "output_dir": str(out_dir),
    }
    if trial_frames is not None:
        req_kw["trial_range"] = trial_frames
    request = WipeRequest(**req_kw)

    eng_kw: dict[str, Any] = {"device": device}
    if weight:
        eng_kw["weight"] = weight
    try:
        with WipeEngine(**eng_kw) as engine:
            result = engine.run(request)
    except Exception as exc:  # noqa: BLE001 — surface vendor/network failures
        return {
            "ok": False,
            "reason": "videowipe_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    out_path = Path(result.output_path)
    if not out_path.is_file():
        return {
            "ok": False,
            "reason": "no_output",
            "result": result.to_dict(),
        }
    import shutil

    shutil.copy2(out_path, dest)
    return {
        "ok": True,
        "dest": str(dest),
        "boxes": len(boxes),
        "vendor_out": str(out_path),
        "backend": result.backend,
        "timings": dict(result.timings or {}),
    }


__all__ = ["videowipe_available", "wipe_regions"]
