"""端到端去硬字幕实跑脚本（一条命令：去字 → QC 验收 → 对比视频）。

用法示例：
  # B站 视频：路径含 bilibili/BV 时自动选 ocr_telea 引擎
  python dehardsub_run.py --source downloads/batch/bilibili_BVxxxx/media/source.mp4

  # 指定字幕带（绕过 VLM），用 OCR 细字形洞 + TELEA
  python dehardsub_run.py --source in.mp4 --band 270,598,712,100

  # GPU 机器上改用 ProPainter 时序修复（解决运动主体涂抹）
  python dehardsub_run.py --source in.mp4 --engine propainter

输出：<src_stem>_dehardsub.mp4（去字幕成品）、同名 + _compare.mp4（左右对比）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_ops import _default_dehardsub_engine_for  # noqa: E402
from dehardsub_ocr_telea import qc_dehardsub, make_compare, DEFAULT_BAND  # noqa: E402
from visual_cleanup import run_multipass_cleanup  # noqa: E402


def _parse_band(s: str | None) -> list[dict] | None:
    if not s:
        return None
    parts = [int(x) for x in s.split(",")]
    if len(parts) != 4:
        raise SystemExit("--band 需为 x,y,w,h")
    x, y, w, h = parts
    return [{"x": x, "y": y, "w": w, "h": h, "role": "dialogue"}]


def main(argv=None):
    ap = argparse.ArgumentParser(description="端到端去硬字幕（去字→QC→对比）")
    ap.add_argument("--source", required=True, help="源视频路径")
    ap.add_argument("--out", default=None, help="成品路径（默认 <src>_dehardsub.mp4）")
    ap.add_argument("--engine", default=None,
                    help="去字引擎：auto/ocr_telea/propainter/sttn/... 默认 auto（B站→ocr_telea）")
    ap.add_argument("--band", default=None,
                    help="字幕带坐标 x,y,w,h；给定则绕过 VLM 直接用它")
    ap.add_argument("--locate-mode", default="band", help="无 --band 时的定位模式（默认 band）")
    ap.add_argument("--no-compare", action="store_true", help="不生成对比视频")
    args = ap.parse_args(argv)

    src = Path(args.source)
    if not src.exists():
        raise SystemExit(f"源视频不存在: {src}")
    out = Path(args.out) if args.out else src.with_name(f"{src.stem}_dehardsub.mp4")

    engine = args.engine or _default_dehardsub_engine_for(str(src))
    print(f"[dehardsub_run] engine={engine}  source={src.name}", flush=True)

    boxes_override = _parse_band(args.band)
    res = run_multipass_cleanup(
        src, out,
        engine=engine,
        locate_mode=args.locate_mode,
        boxes_override=boxes_override,
        demosaic=False,
    )
    print(f"[dehardsub_run] pipeline action={res.get('action')} engine={res.get('engine')}", flush=True)

    # QC 验收：漏删/误删（用去字幕实际用的 band 做判定，避免错位误判）
    qc_band = res.get("box") or DEFAULT_BAND
    qc = qc_dehardsub(str(src), str(out), band=qc_band, watermark=None)
    print(f"[dehardsub_run] QC missed={qc['missed_count']} damaged={qc['damaged_count']}", flush=True)
    if qc["missed"]:
        print("  missed frames:", [m[0] for m in qc["missed"]], flush=True)
    if qc["damaged"]:
        print("  damaged frames:", [d[0] for d in qc["damaged"]], flush=True)

    if not args.no_compare:
        cmp = out.with_name(f"{out.stem}_compare.mp4")
        make_compare(str(src), str(out), str(cmp))
        print(f"[dehardsub_run] compare -> {cmp}", flush=True)

    print(f"[dehardsub_run] done -> {out}", flush=True)
    return res


if __name__ == "__main__":
    main()
