"""End-to-end test of the P0.5 pixel-lossless clean-background route.

Runs the REAL media_ops._render_fast_box_segments (delogo/fill fast path) on a
synthetic video whose burned subtitle appears ONLY between 4.0s and 6.0s (10s clip).
After keyframe-snapping + padding, the re-encoded window is ~[2.0, 8.0], leaving
clean (stream-copied) segments at [0,2) and [8,10). Then proves:
  - non-subtitle background at t=1.0s (a copied clean segment) is BIT-IDENTICAL to source;
  - the subtitle region at t=5.0s (a delogo'd hot segment) is CHANGED.

No AI weights needed (delogo is an ffmpeg filter). Exercises the full P0.5 path:
caption-timeline -> keyframe snap -> stream-copy clean -> delogo hot -> concat.

Run: .venv/Scripts/python.exe verify_dehardsub_e2e.py
"""
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import media_ops as mops

ffmpeg = mops.find_ffmpeg()
assert ffmpeg, "ffmpeg not found"
print("ffmpeg:", ffmpeg)

tmp = Path(tempfile.mkdtemp(prefix="dh_e2e_"))
src = tmp / "src.mp4"

# 10s 运动画面，每 1s 一个关键帧；烧录字幕仅出现在 4.0~6.0s（底部黑色方块模拟）
gen = [
    ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=10",
    "-vf", "drawbox=x=80:y=180:w=160:h=40:color=black:t=fill:enable='between(t,4,6)'",
    "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-movflags", "+faststart", str(src),
]
r = subprocess.run(gen, capture_output=True, text=True)
assert r.returncode == 0, r.stderr[-800:]

# 字幕窗口命中时间（模拟 caption timeline 检测），落在 4~6s
hits = [4.0, 4.5, 5.0, 5.5, 6.0]
# delogo 框（覆盖字幕位置，近似即可）
vf = "delogo=x=80:y=180:w=160:h=40"

dest = tmp / "out.mp4"
report: dict = {}
ok = mops._render_fast_box_segments(ffmpeg, src, dest, vf=vf, report=report, hot_label="delogo", hits=hits)
assert ok, f"fast-box render returned False; report={report}"
print("report:", {k: report[k] for k in ("fast_windows", "fast_window_coverage_sec", "fast_segment_count", "fast_copy_clean")})
assert report.get("fast_copy_clean") is True, "expected pixel-lossless copy path to be active"
assert report["fast_window_coverage_sec"] < (mops.probe_duration_sec(src) or 0) * 0.85, "window over-covers video"


def frame_md5(video: Path, t: float) -> str:
    cmd = [ffmpeg, "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    rr = subprocess.run(cmd, capture_output=True)
    assert rr.returncode == 0, rr.stderr[-300:]
    return hashlib.md5(rr.stdout).hexdigest()


# 干净段 (t=1.0s, 在 [0,2) 复制段) 应与原片逐像素一致
src_clean = frame_md5(src, 1.0)
out_clean = frame_md5(dest, 1.0)
print("clean t=1.0  src:", src_clean)
print("clean t=1.0  out:", out_clean)
assert src_clean == out_clean, "PIXEL MISMATCH in clean background (not lossless!)"

# 字幕段 (t=5.0s, 在 [2,8) delogo 段) 应被改动
src_hot = frame_md5(src, 5.0)
out_hot = frame_md5(dest, 5.0)
print("hot   t=5.0  src:", src_hot)
print("hot   t=5.0  out:", out_hot)
assert src_hot != out_hot, "subtitle region unexpectedly unchanged"

print("\nE2E PASS: non-subtitle background is BIT-IDENTICAL to source; subtitle region delogo'd.")
