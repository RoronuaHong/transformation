#!/usr/bin/env python3
"""与原片硬字幕逐条对比的发布门禁（用户强制要求：字幕交付前必须比对照原片）。

从带硬字幕的源视频 OCR 提取每句「显示区间」，与源语 SRT 做**双向覆盖对比**：
既查「SRT 有而硬字幕无」（ASR 幻觉/多切），更重点查「硬字幕有而 SRT 无」
（ASR 整句漏转——这是 align 抓不到、导致"少字幕"的真凶）。产出 JSON 报告，
缺句/大漂移返回非零码，供主流程在入库前拦截。

用法（subtitle_pipeline/.venv）：
  python hardsub_compare.py --bvid BV1erPNzeEUE            # 库内批次视频，写报告
  python hardsub_compare.py --bvid BV1erPNzeEUE --json -    # 报告打印到 stdout
  python hardsub_compare.py --video path/source.mp4 --srt path/zh.srt --report path/report.json
  python hardsub_compare.py --bvid BV1erPNzeEUE --strict    # 缺句/漂移即 exit 2

作为库函数被调用（主流程入库前门禁、pipeline verify-hardsub 子命令）：
  from hardsub_compare import compare_job
  report = compare_job(video=Path(...), srt=Path(...), report_path=Path(...))
  if not report["ok"]: ...

注意：OCR 很吃 CPU；step 越大越快但越可能漏短句，默认 step=2。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retime_from_hardsub import (  # noqa: E402
    BATCH,
    extract_hardsub_intervals,
    find_subs,
    parse_srt,
)
from subtitle_timing import compare_coverage  # noqa: E402

REPORT_NAME = "hardsub_compare.json"


def locate(bvid: str) -> tuple[Path, Path, Path]:
    """返回 (video, zh_srt, media_dir)。"""
    bdir = BATCH / f"bilibili_{bvid}"
    video = bdir / "media" / "source.mp4"
    subs = find_subs(bdir)
    srt = subs / "zh.srt"
    media = subs.parent  # <stem>_subs 目录
    return video, srt, media


def compare_job(video: Path, srt: Path, report_path: Path | None = None, *,
                step: int = 2, band_top: float = 0.62,
                drift_thr: float = 0.6) -> dict:
    """跑一次「原片硬字幕 vs 源 SRT」覆盖对比，写 JSON 报告并返回。

    缺原片/缺 SRT 时返回 ok=True + skipped 说明（不让无硬字幕的纯音频任务被门禁误杀）。
    """
    skip = None
    if not video.is_file():
        skip = f"no source video: {video}"
    elif not srt.is_file():
        skip = f"no source SRT: {srt}"
    if skip:
        rep = {"ok": True, "skipped": True, "reason": skip,
               "video": str(video), "srt": str(srt)}
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        return rep

    cues = parse_srt(srt.read_text(encoding="utf-8"))
    require_cjk = any(any("\u4e00" <= ch <= "\u9fff" for ch in (c.get("text") or "")) for c in cues)
    intervals = extract_hardsub_intervals(
        video, band_top=band_top, step=step, require_cjk=require_cjk
    )
    rep = compare_coverage(cues, intervals, drift_thr=drift_thr)
    rep.update({"video": str(video), "srt": str(srt), "skipped": False})
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return rep


def _print_human(rep: dict) -> None:
    if rep.get("skipped"):
        print(f"[hardsub-compare] skipped: {rep.get('reason')}")
        return
    print(f"[hardsub-compare] coverage={rep['coverage']:.2%} "
          f"cues={rep['cue_count']} hardsub={rep['interval_count']} "
          f"matched={rep['matched']} ok={rep['ok']}")
    for m in rep.get("missing_in_srt", []):
        print(f"  [MISSING] {m['start']:.2f}-{m['end']:.2f} | {m['text']}")
    for m in rep.get("minor_missing", []):
        print(f"  [minor] {m['start']:.2f}-{m['end']:.2f} | {m['text']}")
    for d in rep.get("drift", []):
        print(f"  [DRIFT {d['delta']:+.2f}s] #{d['index']+1} {d['text']} "
              f"(srt {d['srt_start']:.2f} vs hardsub {d['hardsub_start']:.2f})")
    for e in rep.get("extra_in_srt", []):
        print(f"  [extra] #{e['index']+1} {e['text']}")
    print(f"[hardsub-compare] {'PASS' if rep['ok'] else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="与原片硬字幕逐条对比（发布门禁）")
    ap.add_argument("--bvid", help="批次视频 BV 号（自动定位 source.mp4 与 zh.srt）")
    ap.add_argument("--video", type=Path, help="直接指定带硬字幕的源视频")
    ap.add_argument("--srt", type=Path, help="直接指定源语 SRT")
    ap.add_argument("--report", type=Path, help="JSON 报告路径")
    ap.add_argument("--json", nargs="?", const="-", default=None,
                    help="把 JSON 报告打到 stdout（'-'）或指定文件")
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--band-top", type=float, default=0.62)
    ap.add_argument("--drift-thr", type=float, default=0.6)
    ap.add_argument("--strict", action="store_true", help="缺句/漂移即返回 exit 2")
    args = ap.parse_args(argv)

    if args.bvid:
        video, srt, media = locate(args.bvid)
        report_path = args.report or (media / REPORT_NAME)
    elif args.video and args.srt:
        video, srt = args.video, args.srt
        report_path = args.report or (srt.parent / REPORT_NAME)
    else:
        ap.error("需要 --bvid 或同时给 --video 与 --srt")

    rep = compare_job(video, srt, report_path, step=args.step,
                      band_top=args.band_top, drift_thr=args.drift_thr)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_human(rep)
        if report_path:
            print(f"[hardsub-compare] report -> {report_path}")
    if args.strict and not rep["ok"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
