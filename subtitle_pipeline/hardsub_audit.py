#!/usr/bin/env python3
"""硬字幕提取的严格审计：回答"直提结果是否与原片 100% 一致"。

三层验证（缺一不可，自洽 PASS ≠ 与原片一致）：
  1. full-frame 复扫（band_top=0 扫整个画面）：验证下方字幕带之外
     （画面上部/中部）没有漏掉的字幕带；上部新扫出的 interval 就是漏句。
  2. gap 间隙抽帧 OCR：interval 之间的空档抽帧确认真的无字幕，
     防合并/过滤规则吞句（极短句/闪帧句）。
  3. 验收截图：每条 interval 中点抽帧存盘，供人工肉眼核对
     "OCR 文本 = 画面文本、区间 = 画面显示时间"（验收最终靠人看）。

用法（subtitle_pipeline/.venv）：
  python hardsub_audit.py --bvid BV1erPNzeEUE
  python hardsub_audit.py --bvid BV1sgWn68Ewt --shots
报告写到字幕目录 hardsub_audit.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hardsub_compare import locate  # noqa: E402
from retime_from_hardsub import extract_hardsub_intervals  # noqa: E402
from subtitle_timing import _sim  # noqa: E402

MIN_DUR = 0.15


def _dedup_sort(ivs: list[dict]) -> list[dict]:
    return sorted(ivs, key=lambda x: x["start"])


def audit(bvid: str, *, step: int = 4, shots: bool = True) -> dict:
    video, srt, subs_parent = locate(bvid)
    if not video.is_file():
        raise FileNotFoundError(video)

    print(f"[audit] band 扫描（下方字幕带, band_top=0.62, step={step}）...")
    band = _dedup_sort([iv for iv in extract_hardsub_intervals(
        video, band_top=0.62, step=step, require_cjk=True)
        if iv["end"] - iv["start"] >= MIN_DUR])
    print(f"[audit] band intervals = {len(band)}")

    print(f"[audit] 全画面复扫（band_top=0.0）...")
    full = _dedup_sort([iv for iv in extract_hardsub_intervals(
        video, band_top=0.0, step=step, require_cjk=True)
        if iv["end"] - iv["start"] >= MIN_DUR])
    print(f"[audit] full-frame intervals = {len(full)}")

    # 上部/带外疑似漏句 = full-frame 里既不属于 band 台词、也不是水印的 interval。
    # 水印特征：同一短文本反复出现（>=3 次）或 bilibili 平台标——它们不在字幕带
    # 但不是台词；真漏句只会出现一两次且是一句完整台词。
    from collections import Counter
    from subtitle_timing import _norm
    text_count = Counter(_norm(iv["text"]) for iv in full)
    # 水印核心词 = 在全画面反复出现、且与任何台词都不相似的文本
    # （up 主 ID 如"第七代导演峰爷3号机"/"阿咕不咕"、平台标"bilibili"）
    wm_core = {n for n, c in text_count.items() if c >= 3
               and not any(_sim(n, b.get("text", "")) >= 0.6 for b in band)}

    def _is_wm(t: str) -> bool:
        n = _norm(t)
        if not n:
            return True
        if not any("\u4e00" <= ch <= "\u9fff" for ch in n):
            return True  # 纯拉丁（bilibili/Created in updream 等制作标）非台词
        if "bilibili" in n.lower() or n.lower().startswith("bili"):
            return True
        if text_count.get(n, 0) >= 3:
            return True
        # OCR 串读/误读变体：水印词与其它文字叠读（"阿咕不咕b川bli"）
        # 或单字误读（"阿咕不咕"→"阿咕不吃"）
        if any((core in n or n in core) for core in wm_core):
            return True
        return any(_sim(n, core) >= 0.75 and len(n) <= len(core) + 2
                   for core in wm_core)
    extra_top = []
    for iv in full:
        if _is_wm(iv["text"]):
            continue
        # 与 band 台词句时间重叠 = 同一句的不同读法（水印叠读/一框多读/切分变体），
        # 不是漏句；只有时间上不与任何台词句重叠的才是真漏句嫌疑。
        if any(iv["start"] < b["end"] and b["start"] < iv["end"] for b in band):
            continue
        if any(_sim(iv["text"], b["text"]) >= 0.85
               and abs(iv["start"] - b["start"]) < 0.8 for b in band):
            continue
        extra_top.append(iv)

    # gap 间隙抽帧：相邻 interval 间隔 > 0.6s 的中点附近抽帧 OCR（全画面）。
    # 间隙里出现水印文本是正常（水印常驻），只有出现"台词类"文本才算漏句嫌疑。
    gaps_checked: list[dict] = []
    if full:
        import numpy as np
        from dehardsub_ocr_telea import _iter_frames, _probe
        from glyph_detect import _get_ocr
        ocr = _get_ocr()
        fps, W, H, _n = _probe(video)
        # gap 只按台词句（band）之间的空档计算：水印 interval 参与会把
        # gap 中点算进台词句内部，OCR 读到台词造成假漏报
        gaps = []
        for a, b in zip(band, band[1:]):
            if b["start"] - a["end"] > 0.6:
                gaps.append((a["end"] + b["start"]) / 2.0)
        if band and band[0]["start"] > 1.0:
            gaps.append(band[0]["start"] / 2.0)
        print(f"[audit] gap 抽帧验证 {len(gaps)} 个空档...")
        frames_needed = {round(g * fps) for g in gaps}
        found: dict[float, list[str]] = {g: [] for g in gaps}
        for k, f in enumerate(_iter_frames(video)):
            if k in frames_needed:
                res, _ = ocr(f, use_det=True, use_cls=False, use_rec=True)
                if res:
                    for item in res:
                        t = str(item[1]).strip()
                        sc = float(item[2]) if len(item) > 2 else 0.0
                        if t and sc >= 0.12:
                            found.setdefault(
                                round(k / fps, 2), []).append(t)
        for g in gaps:
            texts = found.get(g) or found.get(round(g, 2)) or []
            non_wm = [t for t in texts if not _is_wm(t)]
            gaps_checked.append({"t": round(g, 2), "texts": texts,
                                 "non_wm_texts": non_wm,
                                 "clean": not non_wm})
            print(f"  [{'CLEAN' if not non_wm else 'TEXT '}] t={g:7.2f}s "
                  f"{texts[:3]}" + (f" non_wm={non_wm[:2]}" if non_wm else ""))

    # 验收截图：只对台词句（band）抽帧，便于人工逐条核对
    shots_dir = None
    if shots:
        shots_dir = subs_parent / "preview" / "hardsub_check"
        shots_dir.mkdir(parents=True, exist_ok=True)
        import subprocess
        from pipeline import find_ffmpeg
        ffmpeg = find_ffmpeg()
        print(f"[audit] 验收截图 {len(band)} 张（台词句）-> {shots_dir}")
        for i, iv in enumerate(band, 1):
            mid = max(0.0, (iv["start"] + iv["end"]) / 2.0)
            out = shots_dir / f"{i:02d}_{iv['start']:.2f}-{iv['end']:.2f}.png"
            subprocess.run(
                [ffmpeg, "-y", "-ss", f"{mid:.3f}", "-i", str(video),
                 "-frames:v", "1", str(out)],
                capture_output=True, check=False)
            (shots_dir / f"{i:02d}_{iv['start']:.2f}-{iv['end']:.2f}.txt").write_text(
                iv["text"], encoding="utf-8")

    rep = {
        "bvid": bvid,
        "video": str(video),
        "band_intervals": band,
        "full_intervals": full,
        "extra_top_band": extra_top,     # 上部新扫出的疑似漏句
        "gaps_checked": gaps_checked,    # 间隙是否干净
        "shots_dir": str(shots_dir) if shots_dir else None,
        "leak_suspects": extra_top or [g for g in gaps_checked if not g["clean"]],
        "ok": not extra_top and all(g["clean"] for g in gaps_checked),
    }
    out_path = subs_parent / "hardsub_audit.json"
    out_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[audit] {'PASS 无漏句' if rep['ok'] else 'FAIL 有漏句嫌疑'} -> {out_path}")
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="硬字幕提取严格审计（全画面+间隙+截图）")
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--step", type=int, default=4)
    ap.add_argument("--no-shots", dest="shots", action="store_false")
    args = ap.parse_args(argv)
    rep = audit(args.bvid, step=args.step, shots=args.shots)
    return 0 if rep["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
