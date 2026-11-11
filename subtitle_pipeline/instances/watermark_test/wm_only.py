"""BV1sgWn68Ewt 只去水印（保留硬字幕）的试验与正式处理。

复用 subtitle_pipeline 既有模块：
  inpaint_lama.lama_inpaint / dehardsub_ocr_telea.feather_blend /
  visual_cleanup.restore_edge_overlay, _enforce_frame_count /
  _inpaint_shared._open_ffmpeg_writer / note_frames.extract_frame

水印两处（960x720 源）：
  TR 右上 "阿咕不咕 bilibili"：全片，半透明白字
  BL 左下 "Created in updream"：仅片头数秒，彩色图形 + 白字

用法（在 subtitle_pipeline 根目录）：
  .venv\\Scripts\\python.exe instances/watermark_test/wm_only.py probe
  .venv\\Scripts\\python.exe instances/watermark_test/wm_only.py run
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from dehardsub_ocr_telea import feather_blend  # noqa: E402
from inpaint_lama import lama_inpaint  # noqa: E402
from note_frames import extract_frame  # noqa: E402

SRC = ROOT / "downloads/batch/bilibili_BV1sgWn68Ewt/media/source.mp4"
HERE = ROOT / "instances/watermark_test"
PROBE = HERE / "probe"
CLEAN = ROOT / "downloads/batch/bilibili_BV1sgWn68Ewt/media/wm_only.mp4"

TR = {"x": 720, "y": 20, "w": 235, "h": 58}
BL = {"x": 8, "y": 628, "w": 255, "h": 84}
# rawvideo pipe 编码会让成片帧序列整体位移（实测不跳过时 +2 帧、跳过 2 帧时 -2 帧），
# 跳过 1 帧正好抵消；末尾由 _enforce_frame_count 补末帧到同帧数。
LEAD_FRAMES = 1


def probe_video(path):
    cap = cv2.VideoCapture(str(path))
    assert cap.isOpened(), path
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 30)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return fps, w, h, n


def read_frames(path, idxs):
    """按帧号顺序精确取帧（不用 seek，避免关键帧漂移）。"""
    cap = cv2.VideoCapture(str(path))
    want = set(int(i) for i in idxs)
    out = {}
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        if i in want:
            out[i] = fr
            if len(out) == len(want):
                break
        i += 1
    cap.release()
    return out


def _roi(fr, box):
    return fr[box["y"]:box["y"] + box["h"], box["x"]:box["x"] + box["w"]]


def build_glyph_template(frames, box, *, threshold=0.60, dilate=1, resp_thr=15):
    """跨多镜头统计「局部亮/暗细节响应」→ 固定水印字形模板。

    水印每帧都在同一位置，背景纹理随镜头变化；取响应均值可把水印形状
    稳定留下、把随机背景亮点平均掉。比整框修补保留 1:1 原始画面。
    ``threshold`` 越高遮罩越贴合笔画（背景保留越多，但过低会残留水印边缘）。
    """
    acc = None
    for fr in frames:
        gray = cv2.cvtColor(_roi(fr, box), cv2.COLOR_BGR2GRAY).astype(np.float32)
        bg = cv2.GaussianBlur(gray, (0, 0), 6)
        wh = gray - bg          # 半透明白字 → 亮于局部背景
        bh = bg - gray          # 描边/阴影 → 暗于局部背景
        resp = ((wh > resp_thr) | (bh > resp_thr)).astype(np.float32)
        acc = resp if acc is None else acc + resp
    mean = acc / max(1, len(frames))
    m = (mean > threshold).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8), iterations=1)
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    if dilate:
        m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=dilate)
    return m


def build_color_template(frames, box, *, threshold=0.25, dilate=2):
    """彩色 logo 图形（蓝/紫高饱和，与橙发/白字背景色相分离）固定模板。"""
    acc = None
    for fr in frames:
        hsv = cv2.cvtColor(_roi(fr, box), cv2.COLOR_BGR2HSV)
        hit = ((hsv[..., 0] > 90) & (hsv[..., 0] < 172) &
               (hsv[..., 1] > 55) & (hsv[..., 2] > 80)).astype(np.float32)
        acc = hit if acc is None else acc + hit
    m = (acc / max(1, len(frames)) > threshold).astype(np.uint8) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=1)
    if dilate:
        m = cv2.dilate(m, np.ones((3, 3), np.uint8), iterations=dilate)
    return m


def bl_template(head_frames):
    """左下 logo = 白字字形 ∪ 彩色图形。"""
    glyph = build_glyph_template(head_frames, BL, threshold=0.30, dilate=2)
    color = build_color_template(head_frames, BL, threshold=0.25, dilate=2)
    return cv2.bitwise_or(glyph, color)


def full_mask(fr, boxes):
    m = np.zeros(fr.shape[:2], np.uint8)
    for b in boxes:
        m[b["y"]:b["y"] + b["h"], b["x"]:b["x"] + b["w"]] = 255
    return m


def glyph_mask(fr, templates, boxes):
    m = np.zeros(fr.shape[:2], np.uint8)
    for b, t in zip(boxes, templates):
        y0, x0 = b["y"], b["x"]
        m[y0:y0 + t.shape[0], x0:x0 + t.shape[1]] = np.maximum(
            m[y0:y0 + t.shape[0], x0:x0 + t.shape[1]], t
        )
    return m


def find_logo_end(path):
    """左下 updream 图形含蓝/紫高饱和像素，橙发不在此色相区间 → 逐帧定位淡出帧。"""
    cap = cv2.VideoCapture(str(path))
    counts = []
    i = 0
    while True:
        ok, fr = cap.read()
        if not ok or i > 30 * 9:
            break
        roi = fr[BL["y"]:BL["y"] + BL["h"], BL["x"]:BL["x"] + BL["w"]]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        counts.append(int(((hsv[..., 0] > 95) & (hsv[..., 0] < 165) &
                           (hsv[..., 1] > 70)).sum()))
        i += 1
    cap.release()
    peak = max(counts) if counts else 0
    if peak < 50:
        return 0, peak, counts
    last = max(k for k, c in enumerate(counts) if c > peak * 0.4)
    return last + 8, peak, counts


def tr_template(sample_frames, **kw):
    # 校对矩阵定案：半透明白字 AA 边缘必须整体盖住，阈值 0.34 + 膨胀 2
    # （更紧的遮罩会残留 "阿咕不咕/bilibili" 鬼影，且 TELEA 在孤立小洞上出白点）。
    opts = {"threshold": 0.34, "dilate": 2}
    opts.update(kw)
    return build_glyph_template(sample_frames, TR, **opts)


def head_frames():
    frames = read_frames(SRC, [8, 40, 80, 120, 140])
    return [frames[k] for k in sorted(frames)]


# ------------------------------- 单帧方案对比 -------------------------------
def probe():
    PROBE.mkdir(parents=True, exist_ok=True)
    fps, w, h, n = probe_video(SRC)
    print(f"[probe] {w}x{h} fps={fps} frames={n}", flush=True)
    logo_end, peak, counts = find_logo_end(SRC)
    print(f"[probe] BL logo peak={peak} end_frame={logo_end} ({logo_end/fps:.2f}s)", flush=True)

    idxs = sorted({int(round(k * (n - 1) / 47)) for k in range(48)})
    sample = read_frames(SRC, idxs)
    ordered = [sample[k] for k in idxs if k in sample]
    t_tr = tr_template(ordered)
    t_bl = bl_template(head_frames())
    cv2.imwrite(str(PROBE / "mask_tr.png"), t_tr)
    cv2.imwrite(str(PROBE / "mask_bl.png"), t_bl)

    cases = [30, 120, 750, 1260, 2400]
    frames = read_frames(SRC, cases)
    methods = ("orig", "glyph_telea", "glyph_lama", "full_lama", "copyfill")

    def render(fr, mname, fi):
        if mname == "orig":
            return fr
        boxes = [TR] + ([BL] if fi <= logo_end else [])
        if mname == "glyph_telea":
            m = glyph_mask(fr, [t_tr] + ([t_bl] if fi <= logo_end else []), boxes)
            out = cv2.inpaint(fr, m, 3, cv2.INPAINT_TELEA)
            return feather_blend(fr, out, m, 4, 1.4)
        if mname == "glyph_lama":
            m = glyph_mask(fr, [t_tr] + ([t_bl] if fi <= logo_end else []), boxes)
            return lama_inpaint(fr, m)
        if mname == "full_lama":
            return lama_inpaint(fr, full_mask(fr, boxes))
        if mname == "copyfill":
            from visual_cleanup import restore_edge_overlay
            out = fr.copy()
            for b in boxes:
                out = restore_edge_overlay(out, b)
            return out
        raise ValueError(mname)

    def sheet(crop_box, name, scale=2):
        cw = crop_box["w"] * scale
        ch = crop_box["h"] * scale
        bar = np.full((26, cw, 3), 35, np.uint8)
        rows = []
        for fi in cases:
            if fi not in frames:
                continue
            fr = frames[fi]
            line = []
            for mi, mn in enumerate(methods):
                out = render(fr, mn, fi) if fi in frames else fr
                piece = cv2.resize(
                    _roi(out, crop_box), (cw, ch), interpolation=cv2.INTER_CUBIC
                )
                head = bar.copy()
                cv2.putText(head, f"f{fi} {mn}", (5, 18), cv2.FONT_HERSHEY_SIMPLEX,
                            0.5, (230, 230, 230), 1, cv2.LINE_AA)
                line.append(np.vstack([head, piece]))
            rows.append(np.hstack(line))
        cv2.imwrite(str(PROBE / name), np.vstack(rows))
        print(f"[probe] saved {PROBE/name}", flush=True)

    sheet({"x": 690, "y": 8, "w": 265, "h": 88}, "cmp_tr.png", 2)
    sheet({"x": 0, "y": 610, "w": 285, "h": 110}, "cmp_bl.png", 2)


def tune():
    """模板阈值/膨胀参数校对矩阵：遮罩贴得越紧，原始背景保留越多。"""
    PROBE.mkdir(parents=True, exist_ok=True)
    _fps, _w, _h, n = probe_video(SRC)
    logo_end, _peak, _ = find_logo_end(SRC)
    idxs = sorted({int(round(k * (n - 1) / 47)) for k in range(48)})
    sample = read_frames(SRC, idxs)
    ordered = [sample[k] for k in idxs if k in sample]
    t_bl = bl_template(head_frames())
    cases = [30, 120, 1260, 2400]
    frames = read_frames(SRC, cases)
    crop = {"x": 690, "y": 8, "w": 265, "h": 88}
    cw, ch = crop["w"] * 2, crop["h"] * 2

    def bar(text):
        b = np.full((26, cw, 3), 35, np.uint8)
        cv2.putText(b, text, (5, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (230, 230, 230), 1, cv2.LINE_AA)
        return b

    def piece(fr):
        return cv2.resize(_roi(fr, crop), (cw, ch), interpolation=cv2.INTER_CUBIC)

    rows = [np.hstack([np.vstack([bar("mask"), np.full((ch, cw, 3), 35, np.uint8)])] +
                      [np.vstack([bar(f"f{fi} orig"), piece(frames[fi])]) for fi in cases])]
    params = ((0.34, 2), (0.45, 1), (0.55, 1), (0.65, 1), (0.75, 1), (0.60, 2))
    for thr, dil in params:
        t = tr_template(ordered, threshold=thr, dilate=dil)
        cv2.imwrite(str(PROBE / f"mask_tune_{int(thr*100)}_{dil}.png"), t)
        disp = cv2.cvtColor(cv2.resize(t, (cw, ch), interpolation=cv2.INTER_NEAREST),
                            cv2.COLOR_GRAY2BGR)
        line = [np.vstack([bar(f"thr{thr} d{dil}"), disp])]
        for fi in cases:
            fr = frames[fi]
            boxes = [TR] + ([BL] if fi <= logo_end else [])
            temps = [t] + ([t_bl] if fi <= logo_end else [])
            m = glyph_mask(fr, temps, boxes)
            out = feather_blend(fr, cv2.inpaint(fr, m, 3, cv2.INPAINT_TELEA), m, 4, 1.4)
            line.append(np.vstack([bar(f"f{fi} thr{thr} d{dil}"), piece(out)]))
        rows.append(np.hstack(line))
    cv2.imwrite(str(PROBE / "tune_tr.png"), np.vstack(rows))
    print(f"[tune] saved {PROBE/'tune_tr.png'}", flush=True)

    bl_crop = {"x": 0, "y": 610, "w": 285, "h": 110}
    bw, bh = bl_crop["w"] * 2, bl_crop["h"] * 2

    def bbar(text):
        b = np.full((26, bw, 3), 35, np.uint8)
        cv2.putText(b, text, (5, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (230, 230, 230), 1, cv2.LINE_AA)
        return b

    def bpiece(fr):
        return cv2.resize(_roi(fr, bl_crop), (bw, bh), interpolation=cv2.INTER_CUBIC)

    t_tr = tr_template(ordered)
    bl_rows = []
    for fi in (30, 120):
        fr = frames[fi]
        m = glyph_mask(fr, [t_tr, t_bl], [TR, BL])
        outs = [
            ("orig", fr),
            ("telea glyph", feather_blend(fr, cv2.inpaint(fr, m, 3, cv2.INPAINT_TELEA),
                                          m, 4, 1.4)),
            ("lama glyph", lama_inpaint(fr, m)),
            ("lama full", lama_inpaint(fr, full_mask(fr, [TR, BL]))),
        ]
        bl_rows.append(np.hstack([
            np.vstack([bbar(f"f{fi} {name}"), bpiece(o)]) for name, o in outs
        ]))
    cv2.imwrite(str(PROBE / "tune_bl.png"), np.vstack(bl_rows))
    print(f"[tune] saved {PROBE/'tune_bl.png'}", flush=True)


# -------------------------------- 全片正式处理 --------------------------------
def run():
    from _inpaint_shared import _open_ffmpeg_writer
    from visual_cleanup import _enforce_frame_count

    fps, w, h, n = probe_video(SRC)
    logo_end, peak, _ = find_logo_end(SRC)
    print(f"[run] {w}x{h} fps={fps} frames={n} logo_end={logo_end}", flush=True)

    idxs = sorted({int(round(k * (n - 1) / 47)) for k in range(48)})
    sample = read_frames(SRC, idxs)
    ordered = [sample[k] for k in idxs if k in sample]
    t_tr = tr_template(ordered)
    t_bl = bl_template(head_frames())
    print(f"[run] mask px: TR={int((t_tr > 0).sum())} BL={int((t_bl > 0).sum())}",
          flush=True)

    CLEAN.parent.mkdir(parents=True, exist_ok=True)
    m_tr = glyph_mask(np.zeros((h, w, 3), np.uint8), [t_tr], [TR])
    m_bl = glyph_mask(np.zeros((h, w, 3), np.uint8), [t_bl], [BL])
    proc = _open_ffmpeg_writer(SRC, CLEAN, w, h, fps)
    assert proc.stdin is not None
    cap = cv2.VideoCapture(str(SRC))
    i = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            if i < LEAD_FRAMES:
                i += 1
                continue
            # 右上白字：细到中等的固定字形遮罩 + TELEA 局部扩散 + 羽化
            out = feather_blend(fr, cv2.inpaint(fr, m_tr, 3, cv2.INPAINT_TELEA),
                                m_tr, 4, 1.4)
            # 左下彩色 logo：字形遮罩洞较大，TELEA 会拉出扇形条纹 → LaMa 内容识别填充
            if i <= logo_end:
                out = lama_inpaint(out, m_bl)
            proc.stdin.write(np.ascontiguousarray(out).tobytes())
            i += 1
            if i % 300 == 0:
                print(f"[run] {i}/{n}", flush=True)
    except Exception:
        proc.kill()
        raise
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    err = proc.communicate(timeout=900)[1]
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed {proc.returncode}: {(err or b'')[:300]!r}")
    _enforce_frame_count(SRC, CLEAN)
    print(f"[run] saved {CLEAN} ({CLEAN.stat().st_size} bytes, {i} frames)", flush=True)

    # 前后对比静帧（保留字幕，肉眼验收水印是否消失）
    cmp_dir = HERE / "result"
    cmp_dir.mkdir(parents=True, exist_ok=True)
    for t in (1.0, 4.0, 25.0, 42.0, 80.0):
        a = extract_frame(SRC, t, cmp_dir / f"src_{int(t):03d}s.jpg", width=960)
        b = extract_frame(CLEAN, t, cmp_dir / f"clean_{int(t):03d}s.jpg", width=960)
        fa, fb = cv2.imread(str(a)), cv2.imread(str(b))
        strip_tr = np.hstack([
            cv2.resize(fa[8:96, 690:960], (540, 176), interpolation=cv2.INTER_CUBIC),
            np.full((176, 8, 3), 60, np.uint8),
            cv2.resize(fb[8:96, 690:960], (540, 176), interpolation=cv2.INTER_CUBIC),
        ])
        cv2.imwrite(str(cmp_dir / f"tr_{int(t):03d}s.png"), strip_tr)
        strip_bl = np.hstack([
            cv2.resize(fa[610:720, 0:285], (570, 220), interpolation=cv2.INTER_CUBIC),
            np.full((220, 8, 3), 60, np.uint8),
            cv2.resize(fb[610:720, 0:285], (570, 220), interpolation=cv2.INTER_CUBIC),
        ])
        cv2.imwrite(str(cmp_dir / f"bl_{int(t):03d}s.png"), strip_bl)
        full = np.hstack([fa, np.full((720, 8, 3), 60, np.uint8), fb])
        cv2.imwrite(str(cmp_dir / f"full_{int(t):03d}s.jpg"), full)

    # 验收：字幕带应逐像素基本不变（只剩重编码噪声）；水印区应显著变化
    check = [300, 750, 1260, 2400]
    fs, fo = read_frames(SRC, check), read_frames(CLEAN, check)
    qc = {}
    for fi in check:
        a, b = fs[fi].astype(np.int16), fo[fi].astype(np.int16)
        qc[f"frame_{fi}"] = {
            "subtitle_mean_abs_diff": round(float(np.abs(
                a[600:700, 260:700] - b[600:700, 260:700]).mean()), 2),
            "watermark_mean_abs_diff": round(float(np.abs(
                a[20:78, 720:955] - b[20:78, 720:955]).mean()), 2),
        }
    (cmp_dir / "qc.json").write_text(json.dumps(qc, indent=2), encoding="utf-8")
    print(f"[run] qc: {qc}", flush=True)

    # 左右对比视频（原片 | 去水印成片，带原声）
    from media_ops import find_ffmpeg
    compare = cmp_dir / "compare_side_by_side.mp4"
    subprocess.run(
        [find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(SRC), "-i", str(CLEAN),
         "-filter_complex", "[0:v][1:v]hstack=inputs=2[v]",
         "-map", "[v]", "-map", "1:a?", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "20", "-c:a", "aac", "-shortest", str(compare)],
        check=True, stdout=subprocess.DEVNULL,
    )
    print(f"[run] compare video: {compare}", flush=True)
    print(f"[run] compare frames in {cmp_dir}", flush=True)


def verify():
    """帧号精确对齐的验收：对比图 + 字幕带/水印区差异 + 帧偏移诊断。

    ffmpeg -ss 在两个编码不同的文件上会取到不同时刻（关键帧结构不同），
    所以成品对比图必须按帧号顺序读，不能按时间戳抽帧。
    """
    fps, _w, _h, _n = probe_video(SRC)
    cmp_dir = HERE / "result"
    cmp_dir.mkdir(parents=True, exist_ok=True)
    base = list(range(700, 712))
    s_frames = read_frames(SRC, base)
    c_frames = read_frames(CLEAN, list(range(696, 720)))
    shift_scores = {}
    for shift in range(-3, 4):
        ds = [float(np.abs(s_frames[fi].astype(np.int16) -
                           c_frames[fi + shift].astype(np.int16)).mean())
              for fi in base]
        shift_scores[shift] = round(float(np.mean(ds)), 2)
    print(f"[verify] frame-shift diffs {shift_scores}", flush=True)

    def cut_peaks(seq):
        ds = [float(np.abs(seq[k].astype(np.int16) -
                           seq[k - 1].astype(np.int16)).mean())
              for k in range(1, len(seq))]
        top = sorted(range(len(ds)), key=lambda k: ds[k], reverse=True)[:6]
        return [(k + 1, round(ds[k], 1)) for k in top]

    seq_s = read_frames(SRC, list(range(400)))
    seq_c = read_frames(CLEAN, list(range(400)))
    print(f"[verify] cut peaks src={cut_peaks(seq_s)} clean={cut_peaks(seq_c)}",
          flush=True)

    cases = [30, 120, 750, 1260, 2400]
    best = min(shift_scores, key=shift_scores.get)
    # 成片若有编码位移，对比/QC 一律按对齐后的帧号取，避免「假残留/假误删」
    c_cases = [fi + best for fi in cases]
    fs, fo = read_frames(SRC, cases), read_frames(CLEAN, c_cases)
    qc = {"frame_shift_diffs": shift_scores, "best_shift": best, "frames": {}}
    for fi, fj in zip(cases, c_cases):
        a, b = fs[fi], fo[fj]
        tag = f"{fi // 30:03d}s"
        tr = np.hstack([
            cv2.resize(a[8:96, 690:960], (540, 176), interpolation=cv2.INTER_CUBIC),
            np.full((176, 8, 3), 60, np.uint8),
            cv2.resize(b[8:96, 690:960], (540, 176), interpolation=cv2.INTER_CUBIC),
        ])
        cv2.imwrite(str(cmp_dir / f"tr_{tag}.png"), tr)
        bl = np.hstack([
            cv2.resize(a[610:720, 0:285], (570, 220), interpolation=cv2.INTER_CUBIC),
            np.full((220, 8, 3), 60, np.uint8),
            cv2.resize(b[610:720, 0:285], (570, 220), interpolation=cv2.INTER_CUBIC),
        ])
        cv2.imwrite(str(cmp_dir / f"bl_{tag}.png"), bl)
        cv2.imwrite(str(cmp_dir / f"full_{tag}.jpg"),
                    np.hstack([a, np.full((720, 8, 3), 60, np.uint8), b]))
        cv2.imwrite(str(cmp_dir / f"src_{tag}.jpg"), a)
        cv2.imwrite(str(cmp_dir / f"clean_{tag}.jpg"), b)

        def diff(y0, y1, x0, x1):
            return round(float(np.abs(a[y0:y1, x0:x1].astype(np.int16) -
                                      b[y0:y1, x0:x1].astype(np.int16)).mean()), 2)

        qc["frames"][f"frame_{fi}"] = {
            "subtitle_band": diff(600, 700, 260, 700),
            "clean_center": diff(200, 400, 300, 600),
            "top_right_watermark": diff(20, 78, 720, 955),
            "bottom_left_logo": diff(628, 710, 8, 263),
        }
    (cmp_dir / "qc.json").write_text(json.dumps(qc, indent=2), encoding="utf-8")
    print(f"[verify] qc: {qc['frames']}", flush=True)
    print(f"[verify] aligned compare frames in {cmp_dir}", flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    {"probe": probe, "tune": tune, "run": run, "verify": verify}[cmd]()
