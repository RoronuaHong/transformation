#!/usr/bin/env python3
"""从带硬字幕的源视频 OCR 提取每句"显示区间"，按文字对齐后把 20 语 SRT 重排到
原片字幕显示时间（严格对齐用户要求的"按原视频显示时间出轴"）。

用法（subtitle_pipeline/.venv）：
  python retime_from_hardsub.py --bvid BV1sgWn68Ewt --dry-run
  python retime_from_hardsub.py --bvid BV1sgWn68Ewt
  python retime_from_hardsub.py --bvid BV1erPNzeEUE

只改时间戳、不改译文文字。dry-run 仅打印提取区间+对齐报告，不写文件。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from subtitle_timing import (  # noqa: E402
    _norm,
    _sim,
    align_cues_to_intervals,
    apply_interval_timing,
    merge_adjacent_intervals,
    normalize_cue_timing,
)

BATCH = Path(r"D:\MineWeb\2026\Vitual\subtitle_pipeline\downloads\batch")


def find_subs(bdir: Path) -> Path:
    """定位含 zh.srt 的字幕目录（不同视频布局不同，递归查找）。

    若存在多份 zh.srt（如 subs/ 与 media/*_dehardsub_subs/subs/ 同时存在），
    优先取 dehardsub 目录（与 import_local_subs 的入库优先级一致），并告警，
    避免"重写 A 目录、入库 B 目录"导致入库数据未重排。
    """
    hits = sorted(bdir.rglob("zh.srt"))
    if not hits:
        return bdir / "subs"
    if len(hits) > 1:
        for h in hits:
            print(f"  [warn] 多份 zh.srt: {h.parent}，仅重写其中一份；"
                  "请确认入库脚本读取的是同一目录")
        dehard = next((h for h in hits if "dehardsub" in str(h).lower()), None)
        return (dehard or hits[0]).parent
    return hits[0].parent


def parse_srt(text: str) -> list[dict]:
    import re

    ts = re.compile(
        r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})"
    )
    blocks = re.split(r"\n\s*\n", text.strip())
    cues = []
    for b in blocks:
        lines = [l.strip() for l in b.splitlines() if l.strip()]
        mi, m = -1, None
        for li, l in enumerate(lines):
            m = ts.search(l)
            if m:
                mi = li  # 用枚举行下标，不能用 lines.index(m.group(0))：
                break    # 时间轴行常带坐标后缀(X1:/Y1:)，m.group(0) 只是子串 → index 抛 ValueError
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(x) for x in m.groups())
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        txt = " ".join(lines[mi + 1 :]).strip()
        if txt:
            cues.append({"start": start, "end": end, "text": txt})
    return cues


def write_srt(cues: list[dict], path: Path) -> None:
    out = []
    for i, c in enumerate(cues, 1):
        h1, m1, s1 = int(c["start"] // 3600), int((c["start"] % 3600) // 60), int(c["start"] % 60)
        ms1 = int(round((c["start"] - int(c["start"])) * 1000))
        h2, m2, s2 = int(c["end"] // 3600), int((c["end"] % 3600) // 60), int(c["end"] % 60)
        ms2 = int(round((c["end"] - int(c["end"])) * 1000))
        if ms1 == 1000:
            ms1 = 0; s1 += 1
        if ms2 == 1000:
            ms2 = 0; s2 += 1
        out.append(
            f"{i}\n"
            f"{h1:02d}:{m1:02d}:{s1:02d},{ms1:03d} --> {h2:02d}:{m2:02d}:{s2:02d},{ms2:03d}\n"
            f"{c['text']}\n"
        )
    path.write_text("\n".join(out), encoding="utf-8")


def _has_cjk(t: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (t or ""))


def extract_hardsub_intervals(video: Path, *, band_top: float = 0.62,
                              score_thr: float = 0.12, step: int = 2,
                              require_cjk: bool = True) -> list[dict]:
    import numpy as np
    from dehardsub_ocr_telea import _iter_frames, _probe
    from glyph_detect import _get_ocr

    ocr = _get_ocr()
    fps, W, H, _n = _probe(video)
    band_y = band_top * H
    per_frame_boxes: list[list[tuple[tuple[int, int], str]]] = []
    cell_count: Counter = Counter()
    total = 0
    for k, f in enumerate(_iter_frames(video)):
        if k % step != 0:
            continue
        total += 1
        res, _ = ocr(f, use_det=True, use_cls=False, use_rec=True)
        boxes: list[tuple[tuple[int, int], str]] = []
        if res:
            for item in res:
                pts = np.asarray(item[0], dtype=np.float32)
                x0, y0 = pts[:, 0].min(), pts[:, 1].min()
                x1, y1 = pts[:, 0].max(), pts[:, 1].max()
                cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
                if cy < band_y:
                    continue
                txt = _norm(str(item[1]))
                score = float(item[2]) if len(item) > 2 else 0.0
                if score < score_thr or not txt:
                    continue
                # 源为中日韩时，丢弃纯拉丁文本框：剔除片头/角标浮水印
                # （如 "Createdin updream"），不影响含中文的行。
                if require_cjk and not _has_cjk(txt):
                    continue
                cell = (int(cx) // 24, int(cy) // 24)
                boxes.append((cell, txt))
                cell_count[cell] += 1
        boxes.sort(key=lambda b: (b[0][1], b[0][0]))
        per_frame_boxes.append(boxes)
        if total % 500 == 0:
            print(f"  [extract] {total} frames scanned", flush=True)

    # 常驻水印/logo：出现在 >50% 帧的 cell 视为水印，排除出字幕串。
    wm_cells = {c for c, n in cell_count.items() if total and n > 0.5 * total}

    # 逐帧字幕串 → 连续同文聚合为显示区间
    intervals: list[dict] = []
    cur_text = None
    cur_start = cur_end = 0.0
    for idx, boxes in enumerate(per_frame_boxes):
        s = " ".join(t for cell, t in boxes if cell not in wm_cells)
        tsec = idx * step / fps
        if not s:
            if cur_text is not None:
                intervals.append({"text": cur_text, "start": cur_start, "end": cur_end})
                cur_text = None
            continue
        if cur_text is None:
            cur_text, cur_start, cur_end = s, tsec, tsec
        elif _sim(s, cur_text) >= 0.8:
            cur_end = tsec
        else:
            intervals.append({"text": cur_text, "start": cur_start, "end": cur_end})
            cur_text, cur_start, cur_end = s, tsec, tsec
    if cur_text is not None:
        intervals.append({"text": cur_text, "start": cur_start, "end": cur_end})

    # 丢短噪点 + 合并闪烁/切分造成的相邻同文区间（规则见 merge_adjacent_intervals）
    intervals = [iv for iv in intervals if iv["end"] - iv["start"] >= 0.15]
    merged = merge_adjacent_intervals(intervals)
    merged.sort(key=lambda x: x["start"])
    print(f"  [extract] {len(merged)} on-screen subtitle intervals "
          f"(wm_cells={len(wm_cells)})")
    return merged


def retime(bvid: str, dry_run: bool, step: int, band_top: float) -> None:
    bdir = BATCH / f"bilibili_{bvid}"
    video = bdir / "media" / "source.mp4"
    subs = find_subs(bdir)
    if not video.exists():
        print(f"[skip] {bvid}: no source.mp4"); return
    if not subs.is_dir():
        print(f"[skip] {bvid}: no subs dir"); return

    print(f"[extract] {bvid} <- {video}")
    zh = parse_srt((subs / "zh.srt").read_text(encoding="utf-8"))
    require_cjk = any(_has_cjk(c["text"]) for c in zh)
    intervals = extract_hardsub_intervals(
        video, band_top=band_top, step=step, require_cjk=require_cjk
    )

    if dry_run:
        print("\n--- 提取到的原片硬字幕显示区间 ---")
        for iv in intervals:
            print(f"  {iv['start']:7.2f}-{iv['end']:7.2f} ({iv['end']-iv['start']:5.2f}s) | {iv['text'][:24]!r}")
    aligned = align_cues_to_intervals(zh, intervals)
    applied = apply_interval_timing(zh, aligned)
    base = [(c["start"], c["end"]) for c in applied]

    def _est_dur(text: str) -> float:
        """按字数估算合理时长（CJK≈0.32s/字，拉丁≈0.08s/字），夹在 0.8~3.0s。"""
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        lat = max(0, len(text) - cjk)
        return max(0.8, min(3.0, cjk * 0.32 + lat * 0.08))

    # 解析合并区间 + 未匹配 cue 的合理短时长：
    # 1) 若某匹配区间的文字与"本 cue + 后续连续未匹配 cue"的文字整体高度相似
    #    （连续字幕无间隙被 OCR 并成一句），按字数比例拆分给这些 cue，消除重叠；
    # 2) 仍未匹配的 cue（原片根本未显示，如 ASR 误切长段）改用估算短时长，
    #    绝不留膨胀的 ASR 时间（避免"消失太迟"）。
    times = list(base)
    handled: set[int] = set()
    for i, (ci, iv) in enumerate(aligned):
        if iv is None or i in handled:
            continue
        ivt = _norm(iv["text"])
        covered = [i]
        for j in range(i + 1, len(zh)):
            if aligned[j][1] is not None:
                break
            covered.append(j)
        joined = "".join(_norm(zh[k]["text"]) for k in covered)
        if len(covered) > 1 and _sim(joined, ivt) >= 0.8:
            total = max(1, sum(len(_norm(zh[k]["text"])) for k in covered))
            cur = iv["start"]
            for k in covered:
                frac = len(_norm(zh[k]["text"])) / total
                dur = (iv["end"] - iv["start"]) * frac
                times[k] = (round(cur, 3), round(cur + dur, 3))
                handled.add(k)
                cur += dur
        else:
            times[i] = (round(iv["start"], 3), round(iv["end"], 3))
            handled.add(i)
    for i, (ci, iv) in enumerate(aligned):
        if iv is None and i not in handled:
            s = times[i][0]
            times[i] = (round(s, 3), round(s + _est_dur(zh[i]["text"]), 3))
    new_times = times
    matched = sum(1 for _, iv in aligned if iv)

    print(f"\n=== {bvid} 对齐报告 (cues={len(zh)}, intervals={len(intervals)}, "
          f"matched={matched}) ===")
    for i, (c, (ci, iv)) in enumerate(zip(zh, aligned)):
        old = f"{c['start']:.2f}-{c['end']:.2f}"
        if iv:
            new = f"{new_times[i][0]:.2f}-{new_times[i][1]:.2f}"
            d = new_times[i][1] - new_times[i][0]
            print(f"  #{i+1:2d} {'OK ' if _sim(c['text'], iv['text'])>=0.8 else '?  '} "
                  f"{old} -> {new} ({d:5.2f}s) | {c['text'][:14]!r} ~ {iv['text'][:14]!r}")
        else:
            new = f"{new_times[i][0]:.2f}-{new_times[i][1]:.2f}"
            d = new_times[i][1] - new_times[i][0]
            print(f"  #{i+1:2d} UNMATCHED {old} -> {new} ({d:5.2f}s est/split) | {c['text'][:14]!r}")

    if dry_run:
        print("\n[dry-run] 未写文件")
        return

    for srt in sorted(subs.glob("*.srt")):
        cues = parse_srt(srt.read_text(encoding="utf-8"))
        if len(cues) != len(new_times):
            print(f"  [warn] {srt.name}: cue 数 {len(cues)} != {len(new_times)}，跳过")
            continue
        for i, c in enumerate(cues):
            c["start"], c["end"] = new_times[i]
        cues = normalize_cue_timing(cues)
        write_srt(cues, srt)
        print(f"  [write] {srt.name}: {len(cues)} cues")
    print(f"[done] {bvid} 已按原片硬字幕时间重排（dry-run=False）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--band-top", type=float, default=0.62)
    args = ap.parse_args()
    retime(args.bvid, args.dry_run, args.step, args.band_top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
