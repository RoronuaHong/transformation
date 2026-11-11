"""BV1sgWn68Ewt 30s 版：去水印 + 去硬字幕（与「只去水印」版做对比）。

复用项目模块：
  wm_only.py 的固定水印遮罩方案（已验证）/
  dehardsub_ocr_telea 的 OCR 目标框、细字形洞、亮度兜底、时序平滑、羽化 /
  inpaint_lama / _open_ffmpeg_writer / _enforce_frame_count / note_frames

同一逐帧循环同时写出两个成片，保证三列对比帧号严格对齐：
  wm_30s.mp4   只去水印（字幕保留）
  clean_30s.mp4 去水印 + 去字幕

用法（项目根目录）：
  .venv\\Scripts\\python.exe instances/watermark_test/clean30.py clip
  .venv\\Scripts\\python.exe instances/watermark_test/clean30.py scan
  .venv\\Scripts\\python.exe instances/watermark_test/clean30.py probe
  .venv\\Scripts\\python.exe instances/watermark_test/clean30.py run [ocr_lama|ocr_telea|white_telea]
  .venv\\Scripts\\python.exe instances/watermark_test/clean30.py verify
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "instances/watermark_test"
for p in (ROOT, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from dehardsub_ocr_telea import (  # noqa: E402
    DEFAULT_SCORE_THR,
    band_white_mask,
    collect_target_boxes,
    feather_blend,
    temporal_smooth,
    thin_mask,
)
from glyph_detect import _get_ocr  # noqa: E402
from inpaint_lama import lama_inpaint  # noqa: E402
from media_ops import find_ffmpeg  # noqa: E402
from note_frames import extract_frame  # noqa: E402
from wm_only import (  # noqa: E402
    BL,
    SRC,
    TR,
    bl_template,
    build_glyph_template,
    find_logo_end,
    glyph_mask,
    probe_video,
    read_frames,
    tr_template,
)

CLIP_DIR = HERE / "clip30"
PROBE_DIR = HERE / "probe30"
RESULT = HERE / "result30"
CLIP = CLIP_DIR / "source_30s.mp4"
OUT_WM = CLIP_DIR / "wm_30s.mp4"
OUT = CLIP_DIR / "clean_30s.mp4"
CLIP_SECONDS = 30
# 字幕带按 OCR 扫描结果设定：字幕 x262-697 / y626-670。
# 左边界必须 >255，否则会与片头左下 "updream"（x87-255）相交而误抓成字幕。
BAND = {"x": 260, "y": 600, "w": 520, "h": 100}


# --------------------------------- 片段与检测 ---------------------------------
def make_clip():
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(SRC), "-t", str(CLIP_SECONDS),
           "-map", "0:v:0", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
           "-c:a", "aac", str(CLIP)]
    subprocess.run(cmd, check=True)
    fps, w, h, n = probe_video(CLIP)
    print(f"[clip] {CLIP} {w}x{h} fps={fps} frames={n}", flush=True)


def scan():
    """OCR 定位字幕行（detect→确认），输出 30s 内所有文本框。"""
    ocr = _get_ocr()
    idxs = [60, 120, 300, 480, 750, 870]
    frames = read_frames(CLIP, idxs)
    for fi in idxs:
        if fi not in frames:
            continue
        res, _ = ocr(frames[fi], use_det=True, use_cls=False, use_rec=True)
        for item in res or []:
            pts = np.asarray(item[0], dtype=np.float32)
            b = {"x": int(pts[:, 0].min()), "y": int(pts[:, 1].min()),
                 "w": int(pts[:, 0].max() - pts[:, 0].min()),
                 "h": int(pts[:, 1].max() - pts[:, 1].min())}
            print(fi, b, round(float(item[2]), 2), str(item[1]), flush=True)


def templates():
    # 右上水印模板沿用【全片】统计（30s 内多为亮背景，半透明白字响应弱，
    # 只统计 30s 会把模板挖缺导致水印残留）；左下 logo 用片头帧统计。
    _fps, w, h, n_src = probe_video(SRC)
    idxs = sorted({int(round(k * (n_src - 1) / 47)) for k in range(48)})
    sample = read_frames(SRC, idxs)
    ordered = [sample[k] for k in idxs if k in sample]
    t_tr = tr_template(ordered)
    head = read_frames(CLIP, [8, 40, 80, 120, 140])
    t_bl = bl_template([head[k] for k in sorted(head)])
    logo_end, _peak, _ = find_logo_end(CLIP)
    m_tr = glyph_mask(np.zeros((h, w, 3), np.uint8), [t_tr], [TR])
    m_bl = glyph_mask(np.zeros((h, w, 3), np.uint8), [t_bl], [BL])
    return m_tr, m_bl, logo_end


def collect_boxes():
    """逐帧 OCR 收集字幕框 + 时序平滑（项目默认路线）。"""
    ocr = _get_ocr()
    boxes = []
    cap = cv2.VideoCapture(str(CLIP))
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            boxes.append(collect_target_boxes(fr, ocr, BAND, (), DEFAULT_SCORE_THR))
    finally:
        cap.release()
    boxes = temporal_smooth(boxes)
    print(f"[boxes] frames={len(boxes)} subtitle_boxes={sum(len(b) for b in boxes)}",
          flush=True)
    return boxes


# --------------------------------- 逐帧修补 ---------------------------------
def subtitle_mask(gray, fr, boxes, mode):
    m = np.zeros(fr.shape[:2], np.uint8)
    if mode == "white_telea":
        bm, (bx, by) = band_white_mask(gray, BAND)
        m[by:by + bm.shape[0], bx:bx + bm.shape[1]] = np.maximum(
            m[by:by + bm.shape[0], bx:bx + bm.shape[1]], bm)
        return m
    for b in boxes:
        if mode == "ocr_lama":
            x0, y0 = max(0, b["x"] - 4), max(0, b["y"] - 4)
            x1 = min(fr.shape[1], b["x"] + b["w"] + 4)
            y1 = min(fr.shape[0], b["y"] + b["h"] + 4)
            m[y0:y1, x0:x1] = 255
        else:
            r = thin_mask(gray, b)
            if r:
                s, (mx, my) = r
                m[my:my + s.shape[0], mx:mx + s.shape[1]] = np.maximum(
                    m[my:my + s.shape[0], mx:mx + s.shape[1]], s)
    bm, (bx, by) = band_white_mask(gray, BAND)
    m[by:by + bm.shape[0], bx:bx + bm.shape[1]] = np.maximum(
        m[by:by + bm.shape[0], bx:bx + bm.shape[1]], bm)
    if mode == "ocr_lama":
        m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=3)
    return m


def telea_apply(fr, mask, radius=3, feather_px=4, sigma=1.4):
    if int(mask.max()) == 0:
        return fr
    out = cv2.inpaint(fr, mask, radius, cv2.INPAINT_TELEA)
    return feather_blend(fr, out, mask, feather_px, sigma)


def wm_frame(fr, m_tr, m_bl, do_bl):
    """已验证的只去水印方案：左下彩色 logo 走 LaMa，右上白字走 TELEA+羽化。"""
    base = lama_inpaint(fr, m_bl) if do_bl else fr
    return telea_apply(base, m_tr)


# --------------------------------- 单帧校对 ---------------------------------
def probe():
    PROBE_DIR.mkdir(parents=True, exist_ok=True)
    m_tr, m_bl, logo_end = templates()
    boxes_all = collect_boxes()
    cases = [60, 120, 300, 750]
    frames = read_frames(CLIP, cases)
    modes = ("orig", "ocr_lama", "ocr_telea", "white_telea")

    def render(fr, mode, fi):
        if mode == "orig":
            return fr
        gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32)
        m_sub = subtitle_mask(gray, fr, boxes_all[fi], mode)
        if mode == "ocr_lama":
            m = cv2.bitwise_or(m_sub, m_bl if fi <= logo_end else
                               np.zeros_like(m_sub))
            out = lama_inpaint(fr, m)
            return telea_apply(out, m_tr)
        base = wm_frame(fr, m_tr, m_bl, fi <= logo_end)
        return telea_apply(base, m_sub)

    crop = {"x": 180, "y": 590, "w": 600, "h": 115}
    cw, ch = crop["w"], crop["h"]
    rows = []
    for fi in cases:
        fr = frames[fi]
        line = []
        for mode in modes:
            out = render(fr, mode, fi)
            piece = out[crop["y"]:crop["y"] + ch, crop["x"]:crop["x"] + cw]
            bar = np.full((24, cw, 3), 35, np.uint8)
            cv2.putText(bar, f"f{fi} {mode}", (5, 17), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (230, 230, 230), 1, cv2.LINE_AA)
            line.append(np.vstack([bar, piece]))
        rows.append(np.hstack(line))
    cv2.imwrite(str(PROBE_DIR / "subtitle_sheet.png"), np.vstack(rows))
    full = np.vstack([
        np.hstack([cv2.resize(render(frames[fi], mode, fi), (480, 360))
                   for mode in modes])
        for fi in cases
    ])
    cv2.imwrite(str(PROBE_DIR / "full_sheet.jpg"), full)

    # 放大细节：字幕笔画残留 / 右上水印是否去干净
    for name, cbox in (("subtitle_detail", {"x": 240, "y": 605, "w": 520, "h": 90}),
                       ("topright_detail", {"x": 690, "y": 8, "w": 265, "h": 88})):
        cw2, ch2 = cbox["w"] * 2, cbox["h"] * 2
        rows2 = []
        for fi in cases:
            fr = frames[fi]
            line = []
            for mode in ("orig", "ocr_lama", "ocr_telea"):
                out = render(fr, mode, fi)
                piece = cv2.resize(
                    out[cbox["y"]:cbox["y"] + cbox["h"], cbox["x"]:cbox["x"] + cbox["w"]],
                    (cw2, ch2), interpolation=cv2.INTER_CUBIC)
                bar = np.full((24, cw2, 3), 35, np.uint8)
                cv2.putText(bar, f"f{fi} {mode}", (5, 17), cv2.FONT_HERSHEY_SIMPLEX,
                            0.5, (230, 230, 230), 1, cv2.LINE_AA)
                line.append(np.vstack([bar, piece]))
            rows2.append(np.hstack(line))
        cv2.imwrite(str(PROBE_DIR / f"{name}.png"), np.vstack(rows2))
    print(f"[probe] saved {PROBE_DIR}", flush=True)


# --------------------------------- 全 30s 成片 ---------------------------------
def run(mode="ocr_lama"):
    from _inpaint_shared import _open_ffmpeg_writer
    from visual_cleanup import _enforce_frame_count

    fps, w, h, n = probe_video(CLIP)
    m_tr, m_bl, logo_end = templates()
    boxes_all = collect_boxes()
    print(f"[run] mode={mode} {w}x{h} fps={fps} frames={n} logo_end={logo_end}",
          flush=True)

    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    p_wm = _open_ffmpeg_writer(CLIP, OUT_WM, w, h, fps)
    p_all = _open_ffmpeg_writer(CLIP, OUT, w, h, fps)
    assert p_wm.stdin is not None and p_all.stdin is not None
    cap = cv2.VideoCapture(str(CLIP))
    i = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            do_bl = i <= logo_end
            wm = wm_frame(fr, m_tr, m_bl, do_bl)
            if mode == "ocr_lama":
                gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).astype(np.float32)
                m_sub = subtitle_mask(gray, fr, boxes_all[i] if i < len(boxes_all) else [],
                                      mode)
                m = cv2.bitwise_or(m_sub, m_bl if do_bl else np.zeros_like(m_sub))
                out = lama_inpaint(fr, m) if int(m.max()) else fr
                out = telea_apply(out, m_tr)
            else:
                gray = cv2.cvtColor(wm, cv2.COLOR_BGR2GRAY).astype(np.float32)
                m_sub = subtitle_mask(gray, wm, boxes_all[i] if i < len(boxes_all) else [],
                                      mode)
                out = telea_apply(wm, m_sub)
            p_wm.stdin.write(np.ascontiguousarray(wm).tobytes())
            p_all.stdin.write(np.ascontiguousarray(out).tobytes())
            i += 1
            if i % 200 == 0:
                print(f"[run] {i}/{n}", flush=True)
    except Exception:
        p_wm.kill()
        p_all.kill()
        raise
    finally:
        cap.release()
        for p in (p_wm, p_all):
            try:
                p.stdin.close()
            except Exception:
                pass
    for p in (p_wm, p_all):
        err = p.communicate(timeout=900)[1]
        if p.returncode != 0:
            raise RuntimeError(f"ffmpeg failed {p.returncode}: {(err or b'')[:300]!r}")
    _enforce_frame_count(CLIP, OUT_WM)
    _enforce_frame_count(CLIP, OUT)
    print(f"[run] {OUT_WM} / {OUT} ({i} frames)", flush=True)


# --------------------------------- 对比与验收 ---------------------------------
def verify():
    RESULT.mkdir(parents=True, exist_ok=True)
    _fps, _w, _h, n = probe_video(CLIP)
    cases = [60, 120, 300, 750]
    # 编码管道可能造成 1 帧位移，静帧对比按实测最佳偏移取帧，避免左右画面错位
    base = list(range(300, 312))
    s_seq = read_frames(CLIP, base)
    c_seq = read_frames(OUT, list(range(294, 322)))
    shift_scores = {
        sh: round(float(np.mean([
            np.abs(s_seq[k].astype(np.int16) - c_seq[k + sh].astype(np.int16)).mean()
            for k in base])), 2)
        for sh in range(-3, 7)
    }
    best = min(shift_scores, key=shift_scores.get)
    print(f"[verify] frame-shift diffs {shift_scores} best={best}", flush=True)
    fs = read_frames(CLIP, cases)
    fw = read_frames(OUT_WM, [fi + best for fi in cases])
    fc = read_frames(OUT, [fi + best for fi in cases])
    qc = {"best_shift": best, "frames": {}}
    for fi in cases:
        fj = fi + best
        a, b, c = fs[fi], fw[fj], fc[fj]
        bar = np.full((28, 1816, 3), 35, np.uint8)
        cv2.putText(bar, f"frame {fi}  src | wm-only | wm+subtitle", (5, 19),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (230, 230, 230), 1, cv2.LINE_AA)
        strip = np.hstack([a, np.full((720, 8, 3), 60, np.uint8), b,
                           np.full((720, 8, 3), 60, np.uint8), c])
        cv2.imwrite(str(RESULT / f"cmp3_{fi:04d}.jpg"), strip)
        cv2.imwrite(str(RESULT / f"cmp2_{fi:04d}.jpg"),
                    np.hstack([a, np.full((720, 8, 3), 60, np.uint8), c]))
        # 字幕带局部放大（原片 | 只去水印 | 去水印+字幕）
        crop = np.vstack([bar, np.hstack([
            a[590:705, 180:780], np.full((115, 8, 3), 60, np.uint8),
            b[590:705, 180:780], np.full((115, 8, 3), 60, np.uint8),
            c[590:705, 180:780],
        ])])
        cv2.imwrite(str(RESULT / f"subtitle_{fi:04d}.png"), crop)

        def diff(x, y, y0, y1, x0, x1):
            return round(float(np.abs(x[y0:y1, x0:x1].astype(np.int16) -
                                      y[y0:y1, x0:x1].astype(np.int16)).mean()), 2)
        qc["frames"][f"frame_{fi}"] = {
            "subtitle_band_wm_only": diff(a, b, 600, 700, 260, 700),
            "subtitle_band_clean": diff(a, c, 600, 700, 260, 700),
            "clean_center_clean": diff(a, c, 200, 400, 300, 600),
            "watermark_clean": diff(a, c, 20, 78, 720, 955),
        }
    (RESULT / "qc.json").write_text(json.dumps(qc, indent=2), encoding="utf-8")
    print(f"[verify] qc: {qc['frames']}", flush=True)

    # 三列对比视频（原片 | 只去水印 | 去水印+字幕），带原声
    compare = RESULT / "compare_3x.mp4"
    subprocess.run(
        [find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(CLIP), "-i", str(OUT_WM), "-i", str(OUT),
         "-filter_complex", "[0:v][1:v][2:v]hstack=inputs=3[v]",
         "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "aac", "-shortest", str(compare)],
        check=True, stdout=subprocess.DEVNULL,
    )
    compare2 = RESULT / "compare_src_clean.mp4"
    subprocess.run(
        [find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(CLIP), "-i", str(OUT),
         "-filter_complex", "[0:v][1:v]hstack=inputs=2[v]",
         "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "aac", "-shortest", str(compare2)],
        check=True, stdout=subprocess.DEVNULL,
    )
    print(f"[verify] {compare} / {compare2}", flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "clip"
    if cmd == "clip":
        make_clip()
    elif cmd == "scan":
        scan()
    elif cmd == "probe":
        probe()
    elif cmd == "run":
        run(args[1] if len(args) > 1 else "ocr_lama")
    elif cmd == "verify":
        verify()
    else:
        raise SystemExit(f"unknown command: {cmd}")
