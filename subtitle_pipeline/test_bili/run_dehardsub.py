"""真实样本端到端去字幕测试（不入素材库）。

镜像 _validate_BV1JMec6xEPQ.py 的 STEP 6 调用，但只跑 strip_hardsubs 本身
（不调用会注册素材库的 pipeline.py），并把产物留在 test_bili/ 下。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from media_ops import strip_hardsubs

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "clip15.mp4"
WORK = Path(sys.argv[2]) if len(sys.argv) > 2 else (Path(__file__).resolve().parent / "work")
WORK.mkdir(parents=True, exist_ok=True)

t0 = time.perf_counter()
cleaned, report = strip_hardsubs(
    WORK,
    video=SRC,
    force=True,
    passes=2,
    demosaic=False,
    mode="band",
    dialogue_short_route="auto",
)
print("elapsed_s", round(time.perf_counter() - t0, 1))
print("cleaned_path", cleaned)
print("exists", cleaned.is_file() if cleaned else None)
keys = ("action", "box_source", "encode", "caption_coverage", "caption_covered_sec",
        "box", "active_windows", "mode", "passes", "engine", "fast_copy_clean")
print(json.dumps({k: report.get(k) for k in keys}, ensure_ascii=False, indent=2, default=str))
