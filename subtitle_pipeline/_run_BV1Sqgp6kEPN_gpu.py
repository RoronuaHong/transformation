"""Download BV1Sqgp6kEPN and run GPU dehardsub (quality + general VSR routing).

Produces: downloads/mode-renders/BV1Sqgp6kEPN_gpu/media/dehardsub/clean.mp4
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ.setdefault("VITUAL_REQUIRE_GPU", "1")
os.environ.setdefault("VITUAL_ORT_THREADS", "4")

import torch  # noqa: E402

from fetch_media import download_bilibili_api_source  # noqa: E402
from media_ops import run_postproc  # noqa: E402
from ort_runtime import ort_providers  # noqa: E402

URL = "https://www.bilibili.com/video/BV1Sqgp6kEPN/"
BVID = "BV1Sqgp6kEPN"
ROOT = Path(f"downloads/mode-renders/{BVID}_gpu")
MEDIA = ROOT / "media"
SRC = MEDIA / "source.mp4"
CLEAN = MEDIA / "dehardsub" / "clean.mp4"
META = MEDIA / "dehardsub" / "dehardsub_meta.json"


def main() -> None:
    print("[gpu-check] torch.cuda", torch.cuda.is_available(), flush=True)
    if not torch.cuda.is_available():
        raise SystemExit("torch CUDA required (STTN fallback path)")
    print("[gpu-check] ort", ort_providers(), flush=True)

    MEDIA.mkdir(parents=True, exist_ok=True)
    if not SRC.is_file() or SRC.stat().st_size < 100_000:
        print(f"[fetch] {URL}", flush=True)
        t0 = time.perf_counter()
        info = download_bilibili_api_source(URL, SRC, max_height=1080)
        print(
            f"[fetch] ok sec={time.perf_counter()-t0:.1f} bytes={SRC.stat().st_size} "
            f"info={info.get('title') or info}",
            flush=True,
        )
    else:
        print(f"[fetch] reuse {SRC} bytes={SRC.stat().st_size}", flush=True)

    # Force rebuild with current general classify→route path.
    if CLEAN.is_file():
        bak = CLEAN.with_name("clean_prev.mp4")
        try:
            if bak.is_file():
                bak.unlink()
            shutil.move(str(CLEAN), str(bak))
            print(f"[dehardsub] archived previous clean → {bak.name}", flush=True)
        except OSError as exc:
            print(f"[dehardsub] could not archive clean ({exc}); unlink", flush=True)
            CLEAN.unlink(missing_ok=True)
    META.unlink(missing_ok=True)
    (MEDIA / "dehardsub" / "dehardsub_progress.json").unlink(missing_ok=True)

    t0 = time.perf_counter()
    out = run_postproc(
        ROOT,
        frozenset({"dehardsub"}),
        media_opts={
            "postproc_mode": "quality",
            "dehardsub_force": True,
            "dehardsub_polish_residual_floor": 1.0,
            "dehardsub_passes": 1,
            "dehardsub_dialogue_short_route": "auto",
            # Opaque black-plate clips: demosaic LaMa on UI chrome often hangs /
            # false-positives; classify glyph_black path does not need it.
            "dehardsub_demosaic": False,
        },
    )
    print("elapsed", round(time.perf_counter() - t0, 1), flush=True)
    for k, v in out.items():
        print(k, v, round(v.stat().st_size / 1e6, 1), "MB", flush=True)
    if CLEAN.is_file():
        print("CLEAN", CLEAN.resolve(), flush=True)


if __name__ == "__main__":
    main()
