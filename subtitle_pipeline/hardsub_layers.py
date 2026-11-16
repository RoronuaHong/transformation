#!/usr/bin/env python3
"""分层硬字幕直提：解决「同屏多字幕层 / 常驻水印 / 字幕组落款」污染源字幕的问题。

背景（B 站鬼畜二创实测 BV14hAEzRENh，同 UP「第七代导演峰爷」）：
同一帧里可能同时存在 4 层文字
  1) 原片对白字幕（白字，固定 y 带，如「念在我们是多年的兄弟」）
  2) UP 主自制旁白字幕（黄字，居中，y 与对白层不同，如「纯属娱乐，感谢经典！」）
  3) UP 主常驻水印（左下「第七代导演峰爷 / 一原创AI作品，版权所有者」）
  4) 原片字幕组落款（如「Amara.org 剧集提供的字幕」）
旧的 retime_from_hardsub.extract_hardsub_intervals 把下半屏所有 CJK 文本框按 y
拼成一句，只按「常驻 cell」剔除水印，结果水印/放大标题/落款混进同一条时间轴。

本模块按层取证（每层单独取证再合并时间轴）：
  1. 逐帧 OCR，记录每个文本框的绝对 bbox；
  2. 常驻水印：位置指纹或「文本+高度」指纹在 >50% 帧出现 → 判水印，整框丢弃；
  3. 字幕组落款/版权行：正则剔除（Amara.org / 剧集提供 / MING PAO ...）；
  4. 同帧内的框按 y 间隙聚成「文字块」，块内按行顺序拼成一句；
  5. 文字块按 y 位置归入「轨道」（白字层 / 黄字层各一条），轨道内做文本连续聚合，
     得到该层显示区间；所有轨道区间合并为一条全局时间轴，重叠者合并成一条；
  6. 产出 subs/zh.srt（源字幕）与 media/hardsub_truth.json（门禁真源）。

逐帧框数据可缓存（--dump-frames / --from-frames），改聚类/分轨参数无需重跑 OCR。

用法（subtitle_pipeline/.venv）：
  python hardsub_layers.py --bvid BV14hAEzRENh --write-srt
  python hardsub_layers.py --bvid BV14hAEzRENh --write-srt --verify   # 加 relisten 确认
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retime_from_hardsub import BATCH, write_srt  # noqa: E402
from subtitle_timing import _norm, _sim, merge_adjacent_intervals, normalize_cue_timing  # noqa: E402

# 字幕组落款 / 版权声明 / 站点水印：这些不是视频内容，不能进字幕轨
CREDIT_RE = re.compile(
    r"amara|openload|subtitle\s*by|字幕由|字幕组|剧集提供|提供字幕|all\s*rights\s*reserved"
    r"|版权所有|版权方|侵权|仅供学习|禁止转载|请勿|商用|mpa|translate|字幕版权归",
    re.I,
)

WM_CELL = 24          # 文本指纹的 y 分桶粒度（px）
WM_Y_CELL = 20        # 位置指纹的 y 分桶粒度（px）
WM_FRAME_RATIO = 0.50  # 常驻帧占比阈值：超过即判为水印
BLOCK_GAP_RATIO = 0.9  # 块内行距阈值（相对块高）
TRACK_CY_RATIO = 0.35  # 归入同轨的中心距阈值（相对较小块高）


def _has_cjk(t: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (t or ""))


def _norm_box_text(t: str) -> str:
    return re.sub(r"\s+", "", t or "")


def _pos_fp(box: dict) -> tuple[int]:
    """位置指纹：**只用 y 中心分桶**。

    实测（BV14hAEzRENh）水印第二行「一原创AI作品，版权所有者」的 OCR 文本长短
    一直变（中心 x 跨桶），但 y 高度固定；只用 y 才能稳定命中常驻水印。
    三层文字 y 相隔 40–50px（对白≈416 / 水印首行≈436 / 水印次行≈464），20px 分桶
    可区分，且短句字幕屏占 <50% 不会被误判为水印。
    """
    return (int((box["y0"] + box["y1"]) / 2 / WM_Y_CELL),)


def _text_fp(box: dict) -> tuple[int, int]:
    """文本+高度指纹：位置轻微抖动时仍能命中（同一行字形高度相近）。"""
    t = _norm_box_text(box["text"])[:6]
    cy = int((box["y0"] + box["y1"]) / 2 / WM_CELL)
    h = int((box["y1"] - box["y0"]) / WM_CELL)
    return (hash(t) & 0xFFFF, cy + h)


def _is_credit(text: str) -> bool:
    if CREDIT_RE.search(text or ""):
        return True
    # UP 主常驻水印第二行「(一)原创AI作品，版权所有者」的 OCR 变体极多
    # （惩权/瓶权/履权/展所有者…），位置也抖；但一定含「AI作」且不含「导演/峰爷」。
    # 放大版标题「第七代导演峰爷 一原创AI作品」含「导演」，不会被误杀。
    t = _norm_box_text(text)
    if ("AI作" in t or "AIH作" in t) and "导演" not in t and "峰爷" not in t:
        return True
    return False


def _cluster_blocks(boxes: list[dict]) -> list[dict]:
    """同帧内的文本框按 y 间隙聚成文字块；块内按 (y, x) 顺序拼成一句。"""
    if not boxes:
        return []
    ordered = sorted(boxes, key=lambda b: (b["y0"], b["x0"]))
    blocks: list[dict] = []
    cur = [ordered[0]]
    for b in ordered[1:]:
        prev = cur[-1]
        gap = b["y0"] - prev["y1"]
        h = max(1, prev["y1"] - prev["y0"])
        if gap > h * BLOCK_GAP_RATIO:
            blocks.append(_make_block(cur))
            cur = [b]
        else:
            cur.append(b)
    blocks.append(_make_block(cur))
    return blocks


def _make_block(items: list[dict]) -> dict:
    parts = [i["text"] for i in sorted(items, key=lambda x: (x["y0"], x["x0"]))]
    return {
        "text": " ".join(p for p in parts if p).strip(),
        "y0": min(i["y0"] for i in items),
        "y1": max(i["y1"] for i in items),
        "x0": min(i["x0"] for i in items),
        "x1": max(i["x1"] for i in items),
    }


def _assign_track(block: dict, tracks: list[dict]) -> dict:
    """把文字块归入最近的轨道（按中心距）；没有近似的就新建轨道。"""
    cy = (block["y0"] + block["y1"]) / 2
    h = max(1, block["y1"] - block["y0"])
    best = None
    best_d = 1e9
    for tr in tracks:
        d = abs(cy - tr["cy"])
        limit = max(1.0, min(h, tr["h"])) * TRACK_CY_RATIO
        if d <= limit and d < best_d:
            best, best_d = tr, d
    if best is None:
        tr = {"cy": cy, "h": h, "frames": 0}
        tracks.append(tr)
        return tr
    # 轨道中心缓慢更新（避免抖动导致切轨/串层）
    best["cy"] = best["cy"] * 0.9 + cy * 0.1
    best["h"] = max(best["h"], h)
    return best


def _scan_frames(video: Path, *, band_top: float, score_thr: float, step: int,
                 band_crop: bool = True) -> list[list[dict]]:
    """逐帧 OCR，返回每采样帧的**原始文本框**（已剔除纯拉丁与落款行）。

    注意：这里必须返回原始框而不是文字块 —— 对白与常驻水印在垂直方向交叠，
    先聚类会把两者焊成一个块，导致水印指纹失效、剔不掉水印。
    水印过滤必须在框层完成，之后才做块聚类（见 main）。
    """
    import numpy as np
    from dehardsub_ocr_telea import _iter_frames, _probe
    from glyph_detect import _get_ocr

    ocr = _get_ocr()
    fps, W, H, _n = _probe(video)
    band_y = int(band_top * H)

    frames: list[list[dict]] = []
    total = 0
    for k, f in enumerate(_iter_frames(video)):
        if k % step != 0:
            continue
        total += 1
        y_off = 0
        if band_crop:
            f = f[band_y:, :]
            y_off = band_y
        res, _ = ocr(f, use_det=True, use_cls=False, use_rec=True)
        boxes: list[dict] = []
        for item in (res or []):
            pts = np.asarray(item[0], dtype=np.float32)
            x0, y0 = float(pts[:, 0].min()), float(pts[:, 1].min())
            y1 = float(pts[:, 1].max())
            x1 = float(pts[:, 0].max())
            y0 += y_off
            y1 += y_off
            if (y0 + y1) / 2 < band_y:
                continue
            txt = _norm(str(item[1]))
            score = float(item[2]) if len(item) > 2 else 0.0
            if score < score_thr or not txt:
                continue
            if not _has_cjk(txt):
                continue          # 纯拉丁：片头/角标浮水印
            if _is_credit(txt):
                continue          # 字幕组落款 / 版权声明
            boxes.append({"x0": x0, "y0": y0, "x1": x1, "y1": y1, "text": txt})
        frames.append(boxes)
        if total % 500 == 0:
            print(f"  [layers] {total} frames scanned", flush=True)
    return frames


def _drop_watermarks(frames: list[list[dict]]) -> tuple[list[list[dict]], int]:
    """常驻水印：位置指纹或文本指纹在 >WM_FRAME_RATIO 的帧出现 → 整框丢弃。

    在**框层**执行（不是块层）：对白与水印在 y 上交叠，先聚类会焊死二者。
    """
    pos_count: Counter = Counter()
    txt_count: Counter = Counter()
    total = len(frames)
    for fboxes in frames:
        for b in fboxes:
            pos_count[_pos_fp(b)] += 1
            txt_count[_text_fp(b)] += 1
    thr = WM_FRAME_RATIO * total
    wm_pos = {fp for fp, n in pos_count.items() if n > thr}
    wm_txt = {fp for fp, n in txt_count.items() if n > thr}
    kept: list[list[dict]] = []
    for fboxes in frames:
        kept.append([b for b in fboxes
                     if _pos_fp(b) not in wm_pos and _text_fp(b) not in wm_txt])
    return kept, len(wm_pos | wm_txt)


def _intervals_from_frames(frames: list[list[dict]], *, fps: float, step: int) -> tuple[list[dict], list[dict]]:
    """原始框 → 文字块 → 轨道 → 每轨文本连续聚合 → 区间列表。"""
    tracks: list[dict] = []
    per_frame: list[dict[int, str]] = []
    for fboxes in frames:
        slot: dict[int, str] = {}
        for b in _cluster_blocks(fboxes):
            tr = _assign_track(b, tracks)
            slot[id(tr)] = (slot.get(id(tr), "") + " " + b["text"]).strip()
        per_frame.append(slot)

    intervals: list[dict] = []
    for ti, tr in enumerate(tracks):
        cur_text = None
        cur_start = cur_end = 0.0
        for idx, slot in enumerate(per_frame):
            s = slot.get(id(tr), "")
            tsec = idx * step / fps
            if not s:
                if cur_text is not None:
                    intervals.append({"text": cur_text, "start": cur_start, "end": cur_end,
                                      "track": ti})
                    cur_text = None
                continue
            if cur_text is None:
                cur_text, cur_start, cur_end = s, tsec, tsec
            elif _sim(s, cur_text) >= 0.8:
                cur_end = tsec
            else:
                intervals.append({"text": cur_text, "start": cur_start, "end": cur_end,
                                  "track": ti})
                cur_text, cur_start, cur_end = s, tsec, tsec
        if cur_text is not None:
            intervals.append({"text": cur_text, "start": cur_start, "end": cur_end,
                              "track": ti})
    intervals = [iv for iv in intervals if iv["end"] - iv["start"] >= 0.15]
    intervals = merge_adjacent_intervals(intervals)
    intervals.sort(key=lambda x: x["start"])
    tracks_info = [
        {"track": i, "cy": round(t["cy"], 1), "h": round(t["h"], 1),
         "intervals": sum(1 for iv in intervals if iv["track"] == i)}
        for i, t in enumerate(tracks)
    ]
    return intervals, tracks_info


def _merge_overlapping(intervals: list[dict]) -> list[dict]:
    """同一时刻多层文字（时间高度重合）→ 合并成一条 cue（文本按行顺序拼接）。"""
    out: list[dict] = []
    for iv in intervals:
        if out:
            prev = out[-1]
            overlap = min(prev["end"], iv["end"]) - max(prev["start"], iv["start"])
            span = max(0.01, min(prev["end"] - prev["start"], iv["end"] - iv["start"]))
            if overlap / span >= 0.8:
                prev["end"] = max(prev["end"], iv["end"])
                prev["start"] = min(prev["start"], iv["start"])
                if iv["text"] not in prev["text"]:
                    prev["text"] = (prev["text"] + " " + iv["text"]).strip()
                continue
        out.append(dict(iv))
    return out


def cues_from_intervals(intervals: list[dict], *, duration: float | None = None) -> list[dict]:
    cues = [
        {"start": round(float(iv["start"]), 3),
         "end": round(float(iv["end"]), 3),
         "text": (iv["text"] or "").strip()}
        for iv in intervals if (iv["text"] or "").strip()
    ]
    if duration:
        for c in cues:
            c["start"] = max(0.0, min(c["start"], duration))
            c["end"] = max(c["start"] + 0.05, min(c["end"], duration))
    return normalize_cue_timing(cues)


def relisten_verify(cues: list[dict], wav: Path, model_size: str = "large-v3") -> list[dict]:
    """逐条语音确认（只确认不改正文；低相似度条目供人工复核）。"""
    import tempfile
    import wave

    from multipass_asr import cut_wav_clip, relisten_clip
    from pipeline import pick_compute_type, resolve_whisper_id

    with wave.open(str(wav), "rb") as w:
        wav_dur = w.getnframes() / float(w.getframerate())

    model_id = resolve_whisper_id(model_size)
    device = "cuda"
    compute = pick_compute_type(device, model_id)
    print(f"[verify] loading whisper {model_id} on {device} compute={compute}", flush=True)
    from faster_whisper import WhisperModel

    model = WhisperModel(model_id, device=device, compute_type=compute,
                         cpu_threads=4, num_workers=1)
    out: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="hardsub_layers_") as td:
        for k, c in enumerate(cues, 1):
            start = max(0.0, min(float(c["start"]), max(0.0, wav_dur - 0.2)))
            end = max(start + 0.2, min(float(c["end"]), wav_dur))
            clip = Path(td) / f"c_{k}.wav"
            try:
                cut_wav_clip(wav, start, end, clip, pad=0.35)
                heard = relisten_clip(model, clip, "zh", c["text"])
            except Exception as e:  # noqa: BLE001 —— 单条失败不阻断整体
                heard = f"<clip-error {type(e).__name__}>"
            sim = _sim(heard, c["text"])
            out.append({"index": k, "start": start, "end": end,
                        "text": c["text"], "heard": heard, "sim": round(sim, 3)})
            flag = "OK " if sim >= 0.5 else "?  "
            print(f"  [{flag}] {start:7.2f}-{end:7.2f} "
                  f"ocr={c['text'][:26]!r} heard={heard[:26]!r} sim={sim:.2f}", flush=True)
    model = None
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="分层硬字幕直提（原片多字幕层场景）")
    ap.add_argument("--bvid", required=True)
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--band-top", type=float, default=0.62)
    ap.add_argument("--min-dur", type=float, default=0.15)
    ap.add_argument("--write-srt", action="store_true", help="写 subs/zh.srt")
    ap.add_argument("--verify", action="store_true", help="加 relisten 语音确认")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--json", type=Path, default=None, help="真源 JSON 输出路径")
    ap.add_argument("--dump-frames", type=Path, default=None, help="逐帧框数据缓存（JSON）")
    ap.add_argument("--from-frames", type=Path, default=None, help="复用逐帧框缓存，跳过 OCR")
    args = ap.parse_args(argv)

    bdir = BATCH / f"bilibili_{args.bvid}"
    video = bdir / "media" / "source.mp4"
    if not video.is_file():
        print(f"[skip] {args.bvid}: no source.mp4")
        return 1

    from dehardsub_ocr_telea import _probe

    fps, _W, _H, _n = _probe(video)
    duration = _n / fps if fps else None

    if args.from_frames and args.from_frames.is_file():
        frames = json.loads(args.from_frames.read_text(encoding="utf-8"))
        # 缓存是旧规则扫描时落的：这里再按当前规则剔一次（幂等），避免重跑 OCR
        frames = [
            [b for b in fboxes if _has_cjk(b.get("text") or "") and not _is_credit(b.get("text") or "")]
            for fboxes in frames
        ]
        print(f"[layers] loaded {len(frames)} cached frames from {args.from_frames}")
    else:
        print(f"[layers] {args.bvid} <- {video}", flush=True)
        frames = _scan_frames(video, band_top=args.band_top, score_thr=0.12, step=args.step)
        if args.dump_frames:
            args.dump_frames.parent.mkdir(parents=True, exist_ok=True)
            args.dump_frames.write_text(json.dumps(frames, ensure_ascii=False), encoding="utf-8")
            print(f"[layers] frames cached -> {args.dump_frames}")

    clean, wm = _drop_watermarks(frames)
    print(f"[layers] watermark signatures dropped: {wm}")

    intervals, tracks_info = _intervals_from_frames(clean, fps=fps, step=args.step)
    print(f"[layers] {len(tracks_info)} tracks, {len(intervals)} intervals")
    for t in tracks_info:
        print(f"  track{t['track']}: cy={t['cy']} h={t['h']} intervals={t['intervals']}")

    merged = _merge_overlapping(intervals)
    cues = cues_from_intervals(merged, duration=duration)
    print("\n--- 分层后的原片字幕区间（合并后） ---")
    for i, c in enumerate(cues, 1):
        print(f"  #{i:2d} {c['start']:7.2f}-{c['end']:7.2f} | {c['text']}")

    verified: list[dict] = []
    if args.verify:
        wav = bdir / "media" / "full_16k.wav"
        if wav.is_file():
            verified = relisten_verify(cues, wav, args.model)
        else:
            print("[verify] no full_16k.wav; skip relisten")

    truth_path = args.json or (bdir / "media" / "hardsub_truth.json")
    truth_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bvid": args.bvid,
        "video": str(video),
        "step": args.step,
        "duration": duration,
        "tracks": tracks_info,
        "intervals": merged,
        "cues": cues,
        "verified": verified,
    }
    truth_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[layers] truth -> {truth_path}")

    if args.write_srt:
        subs = bdir / "subs"
        subs.mkdir(parents=True, exist_ok=True)
        zh = subs / "zh.srt"
        write_srt(cues, zh)
        print(f"[layers] zh.srt <- {len(cues)} cues -> {zh}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
