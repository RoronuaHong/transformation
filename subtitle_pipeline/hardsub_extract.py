#!/usr/bin/env python3
"""硬字幕直提：原片画面上烧录的字幕本身就是最准确的源字幕。

对带硬字幕的源片，不再依赖 ASR 转写当源字幕（ASR 会漏句、同音错字、拆错句界），
而是逐帧 OCR 直接提取每句「显示区间 + 显示文本」写成源语 SRT：
  - 时间轴 = 原片字幕实际显示时间（用户要求的"按原视频显示时间出轴"）；
  - 文本   = 原片显示文本（完整性由画面保证，无 ASR 整句漏转）；
  - 可选 relisten 逐条确认该区间确有语音（防 OCR 把场景文字当字幕）。
无硬字幕的片子仍走 ASR（transcribe_video），本脚本只覆盖硬字幕源片。

用法（subtitle_pipeline/.venv）：
  python hardsub_extract.py --bvid BV1erPNzeEUE --dry-run   # 只打印直提结果
  python hardsub_extract.py --bvid BV1erPNzeEUE             # 写 subs/zh.srt
  python hardsub_extract.py --bvid BV1erPNzeEUE --step 1    # 更高边界精度(更慢)

写完建议立即门禁自检 + 重翻：
  python hardsub_compare.py --bvid BV1erPNzeEUE --strict
  python pipeline.py translate --from-srt <subs>/zh.srt --source-lang zh --force
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hardsub_compare import compare_job, locate  # noqa: E402
from job_layout import existing_wav  # noqa: E402
from multipass_asr import cut_wav_clip, relisten_clip  # noqa: E402
from pipeline import pick_compute_type, resolve_whisper_id  # noqa: E402
from retime_from_hardsub import extract_hardsub_intervals, write_srt  # noqa: E402
from subtitle_timing import _sim, normalize_cue_timing  # noqa: E402


def extract_source_srt(
    bvid: str,
    *,
    dry_run: bool = False,
    step: int = 2,
    min_dur: float = 0.15,
    verify_audio: bool = True,
    model_size: str | None = None,
) -> dict:
    """OCR 直提原片硬字幕 → 源语 SRT。返回 {cues, verified, report}。"""
    video, srt, _media = locate(bvid)
    if not video.is_file():
        raise FileNotFoundError(f"no source video: {video}")

    intervals = extract_hardsub_intervals(video, step=step, require_cjk=True)
    intervals = [iv for iv in intervals if iv["end"] - iv["start"] >= min_dur]
    print(f"[extract] {len(intervals)} subtitle intervals (step={step}, min_dur={min_dur})")

    verified: list[dict] = []
    if verify_audio:
        wav = existing_wav(video.parent.parent) or _fallback_wav(video)
        if wav is None:
            print("[verify] no source wav; skip relisten verify (OCR text used as-is)")
        else:
            model_id = resolve_whisper_id(model_size or "small")
            device = "cuda"
            compute = pick_compute_type(device, model_id)
            print(f"[verify] loading whisper {model_id} on {device} compute={compute}")
            from faster_whisper import WhisperModel

            model = WhisperModel(model_id, device=device, compute_type=compute,
                                 cpu_threads=4, num_workers=1)
            with tempfile.TemporaryDirectory(prefix="hardsub_extract_") as td:
                for k, iv in enumerate(intervals, 1):
                    clip = Path(td) / f"iv_{k}.wav"
                    cut_wav_clip(wav, iv["start"], iv["end"], clip, pad=0.35)
                    heard = relisten_clip(model, clip, "zh", iv["text"])
                    sim = _sim(heard, iv["text"])
                    verified.append({"interval": f"{iv['start']:.2f}-{iv['end']:.2f}",
                                     "text": iv["text"], "heard": heard,
                                     "sim": round(sim, 3)})
                    flag = "OK " if sim >= 0.5 else "?  "
                    print(f"  [{flag}] {iv['start']:7.2f}-{iv['end']:7.2f} "
                          f"ocr={iv['text'][:18]!r} heard={heard[:18]!r} sim={sim:.2f}")
            model = None

    cues = [
        {"start": round(float(iv["start"]), 3),
         "end": round(float(iv["end"]), 3),
         "text": (iv["text"] or "").strip()}
        for iv in intervals if (iv["text"] or "").strip()
    ]
    cues = normalize_cue_timing(cues)

    if dry_run:
        print("\n--- 直提字幕（dry-run 未写文件）---")
        for i, c in enumerate(cues, 1):
            print(f"  #{i:2d} {c['start']:7.2f}-{c['end']:7.2f} | {c['text']}")
        return {"cues": cues, "verified": verified, "dry_run": True}

    write_srt(cues, srt)
    print(f"[extract] zh.srt <- {len(cues)} cues (OCR 直提) -> {srt}")

    # 自检：直提轴必须被门禁接受（同一来源应 100% 覆盖）
    rep = compare_job(video, srt, None, step=step)
    print(f"[self-check] coverage={rep.get('coverage', 0):.0%} ok={rep.get('ok')} "
          f"missing={len(rep.get('missing_in_srt', []))} drift={len(rep.get('drift', []))}")
    return {"cues": cues, "verified": verified, "compare": rep}


def _fallback_wav(video: Path) -> Path | None:
    bdir = video.parent.parent  # batch/bilibili_<bvid>
    for cand in (bdir / "media" / "full_16k.wav", bdir / "media" / "source.wav"):
        if cand.is_file():
            return cand
    return existing_wav(bdir)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OCR 直提原片硬字幕为源语 SRT")
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--step", type=int, default=2, help="OCR 采样步长，越小边界越准越慢")
    ap.add_argument("--min-dur", type=float, default=0.15)
    ap.add_argument("--no-verify", action="store_true", help="跳过 relisten 语音确认")
    ap.add_argument("--model", default=None, help="whisper 模型（默认 small）")
    args = ap.parse_args(argv)
    out = extract_source_srt(args.bvid, dry_run=args.dry_run, step=args.step,
                             min_dur=args.min_dur, verify_audio=not args.no_verify,
                             model_size=args.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
