#!/usr/bin/env python3
"""由硬字幕对比门禁驱动的缺句定向补回（"少字幕"的修复入口）。

流程：hardsub_compare 报缺句 → 对每条缺句窗口从源 wav 切片 →
用 relisten_clip 以「OCR 文本做 initial_prompt」重听 → 相似则用重听文本、
否则回退 OCR 文本 → 按原片显示区间插入 zh.srt → 重跑对比确认 coverage=100%。
之后用 `pipeline.py translate --from-srt ... --force` 重翻 20 语。

用法（subtitle_pipeline/.venv）：
  python hardsub_recover.py --bvid BV1erPNzeEUE --dry-run   # 只打印补句计划
  python hardsub_recover.py --bvid BV1erPNzeEUE             # 补句并写回 zh.srt
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
from retime_from_hardsub import parse_srt, write_srt  # noqa: E402
from subtitle_timing import _sim, compare_coverage, normalize_cue_timing  # noqa: E402


def find_wav(bvid: str) -> Path | None:
    """优先 job 布局的 full_16k.wav，其次批次目录里任意可用 wav。"""
    _video, _srt, _media = locate(bvid)
    bdir = Path(_video).parent.parent  # batch/bilibili_<bvid>
    for cand in (bdir / "media" / "full_16k.wav", bdir / "media" / "source.wav"):
        if cand.is_file():
            return cand
    w = existing_wav(bdir)
    return w


def recover_missing(
    bvid: str,
    *,
    dry_run: bool = False,
    step: int = 3,
    sim_thr: float = 0.5,
    model_size: str | None = None,
) -> dict:
    """补回 zh.srt 里被 ASR 整句漏掉的原片硬字幕行。返回恢复报告。"""
    video, srt, _media = locate(bvid)
    rep0 = compare_job(video, srt, None, step=step)
    missing = list(rep0.get("missing_in_srt") or [])
    if not missing:
        print("[recover] 无缺句，无需补回")
        return {"recovered": [], "skipped": [], "compare": rep0}

    wav = find_wav(bvid)
    if wav is None:
        raise FileNotFoundError(f"no source wav for {bvid}; cannot relisten missing windows")

    model_id = resolve_whisper_id(model_size or "small")
    device = "cuda"
    compute = pick_compute_type(device, model_id)
    print(f"[recover] loading whisper {model_id} on {device} compute={compute}")
    from faster_whisper import WhisperModel

    model = WhisperModel(model_id, device=device, compute_type=compute,
                         cpu_threads=4, num_workers=1)

    cues = parse_srt(srt.read_text(encoding="utf-8"))
    recovered: list[dict] = []
    skipped: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="hardsub_recover_") as td:
        for k, m in enumerate(missing, 1):
            clip = Path(td) / f"miss_{k}.wav"
            cut_wav_clip(wav, m["start"], m["end"], clip, pad=0.35)
            # relisten 只做「该窗口确有语音」的确认（sim>=sim_thr）；
            # 插入文本一律用 OCR 文本——原片显示的字幕才是对齐真源，
            # relisten 的同音误听（"镇抚使"→"震抚使"）不能覆盖显示文本。
            text = relisten_clip(model, clip, "zh", m["text"])
            sim = _sim(text, m["text"])
            item = {
                "window": f"{m['start']:.2f}-{m['end']:.2f}",
                "hardsub_text": m["text"],
                "relisten_text": text,
                "sim": round(sim, 3),
                "inserted_text": m["text"],
                "source": "ocr_confirmed_by_relisten" if text and sim >= sim_thr
                          else "ocr_unverified",
            }
            recovered.append(item)
            print(f"  [+] {item['window']} {item['source']} (relisten sim={sim:.2f}): {item['inserted_text']}")

    if dry_run:
        print("[recover] dry-run：未写 zh.srt")
        return {"recovered": recovered, "skipped": skipped, "dry_run": True}

    if recovered:
        existing_starts = {round(float(c["start"]), 2) for c in cues}
        added = []
        for r in recovered:
            s0, e0 = (float(x) for x in r["window"].split("-"))
            if round(s0, 2) in existing_starts:
                continue  # 已有同起点 cue，不重复插
            added.append({"start": round(s0, 3), "end": round(e0, 3),
                          "text": r["inserted_text"]})
        if added:
            cues = sorted(cues + added, key=lambda c: float(c["start"]))
            cues = normalize_cue_timing(cues)
            write_srt(cues, srt)
            print(f"[recover] zh.srt +{len(added)} cues -> {srt}")

    # 补完必须复检：对比必须 PASS，否则门禁仍不放行
    rep1 = compare_job(video, srt, None, step=step)
    return {"recovered": recovered, "skipped": skipped, "compare": rep1}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按原片硬字幕补回 ASR 漏句")
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--model", default=None, help="whisper 模型（默认 small）")
    args = ap.parse_args(argv)
    out = recover_missing(args.bvid, dry_run=args.dry_run, step=args.step,
                          model_size=args.model)
    rep = out.get("compare") or {}
    print(f"[recover] done: +{len(out.get('recovered') or [])} cues, "
          f"coverage={rep.get('coverage', 0):.0%} ok={rep.get('ok')}")
    for m in rep.get("missing_in_srt", []):
        print(f"  [STILL MISSING] {m['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
