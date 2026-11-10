"""E2E Stage 2: run real ASR via multipass_asr directly (no torch import) to dodge
the broken MKL backend and the torch CUDA page-file exhaustion on this box."""
import os
import sys

os.environ["CT2_USE_MKL"] = "0"
os.environ["CT2_USE_OPENBLAS"] = "1"
os.environ["CT2_USE_EIGEN"] = "1"
os.environ["OMP_NUM_THREADS"] = "2"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pathlib import Path
import json

from multipass_asr import transcribe_multipass, save_raw

WORK = Path("downloads/batch/bilibili_E2E_20s/media/source_subs")
WAV = WORK / "media" / "source.wav"
MODEL = r"D:\LLM\whisper-models\faster-whisper-small"
SUBS = WORK / "subs"
SUBS.mkdir(parents=True, exist_ok=True)


def _t(v):
    # seconds -> SRT timestamp
    if hasattr(v, "seconds"):
        v = v.seconds
    h = int(v // 3600)
    m = int((v % 3600) // 60)
    s = int(v % 60)
    ms = int(round((v - int(v)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segs, path: Path):
    lines = []
    for i, s in enumerate(segs, 1):
        start = s.get("start") if isinstance(s, dict) else s.start
        end = s.get("end") if isinstance(s, dict) else s.end
        text = (s.get("text") if isinstance(s, dict) else s.text).strip()
        lines.append(f"{i}\n{_t(start)} --> {_t(end)}\n{text}\n")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


segs, raw = transcribe_multipass(
    WAV, MODEL, "cpu", "int8", "zh", hotwords="", release_cuda=(lambda: None), relisten=True
)
print(f"[e2e-asr] segments={len(segs)}")
save_raw(WORK / "media" / "multipass_raw.json", raw, meta={"source_lang": "zh", "model": MODEL})
srt = write_srt(segs, SUBS / "zh.srt")
print(f"[e2e-asr] wrote {srt} ({len(segs)} cues)")
for s in segs[:8]:
    txt = s.get("text") if isinstance(s, dict) else s.text
    st = s.get("start") if isinstance(s, dict) else s.start
    print(f"  {round(float(st),1)}s  {txt[:70]}")
