# -*- coding: utf-8 -*-
"""Dialogue clear-rate gate: verify burned subtitles are actually gone in clean.mp4.

Why this exists
---------------
The hardsub_compare gate only checks SRT <-> source burned-caption *alignment*;
it never checks whether dehardsub actually removed the captions. A run can be
fully green (9/9 jobs done, hardsub_compare PASS) while clean.mp4 still shows
the subtitles because the dialogue_safety gate restored most frames to source.

This module audits source.mp4 vs clean.mp4 inside the dialogue ROIs and fails
when captions remain. Classification logic is lifted from the proven
``_audit_bv1p6_frames.py`` (dense per-frame src/clean audit) but parameterised
so it works for any work_dir.

Public API
----------
    audit_dialogue_clear(src, clean, boxes, *, step=1) -> dict
    dialogue_clear_gate(work_dir, *, max_fail_ratio=0.15, step=2) -> dict

Failure definition (per dialogue ROI frame with captions):
    caption_remains + left_as_source + cleared_dark_smear
Denominator: frames that actually had captions (exclude ``no_caption``), so
empty frames cannot dilute the clear rate. ``cleared_ok`` is the only success;
``partial`` is counted as a failure too (conservative).
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

FAIL_LABELS = ("caption_remains", "left_as_source", "cleared_dark_smear", "partial")


def _stats(gs, gc) -> dict:
    import cv2
    import numpy as np

    w2b = float(((gs > 170) & (gc < 90)).mean())
    mse = float(np.mean((gs.astype(np.float32) - gc.astype(np.float32)) ** 2))
    # Stroke density: outlined white glyphs have high local gradient variance;
    # a white shirt / chin highlight is smooth even when bright fraction is
    # high. Without this, bright clothing is mislabeled as caption_remains.
    lap_s = float(cv2.Laplacian(gs, cv2.CV_64F).var())
    lap_c = float(cv2.Laplacian(gc, cv2.CV_64F).var())
    return {
        "lap_s": lap_s,
        "lap_c": lap_c,
        "bright_s": float((gs > 180).mean()),
        "bright_c": float((gc > 180).mean()),
        "dark_s": float((gs < 30).mean()),
        "dark_c": float((gc < 30).mean()),
        "mad": float(np.mean(np.abs(gs.astype(np.float32) - gc.astype(np.float32)))),
        "w2b": w2b,
        "psnr": 99.0 if mse < 1e-6 else float(20 * __import__("math").log10(255.0 / __import__("math").sqrt(mse))),
    }


def classify_dialogue(st: dict) -> str:
    """Label what happened to a dialogue ROI (same rules as the BV1P6 audit)."""
    bs, bc = st["bright_s"], st["bright_c"]
    dlt = st["dark_c"] - st["dark_s"]
    lap_s = float(st.get("lap_s", 9999) or 9999)
    lap_c = float(st.get("lap_c", lap_s) or lap_s)
    if bs < 0.02 and bc < 0.02:
        return "no_caption"
    # Bright-but-smooth source = white shirt / chin highlight, NOT a caption.
    # Calibrated on BV1P6: real outlined glyphs have Laplacian variance
    # 2146–11440; shirt/highlight frames 114–439. Counting those as caption
    # frames was inflating fail_ratio (f440 bs=0.021 lap=114).
    if lap_s < 800 and bs < 0.16:
        return "no_caption"
    if bc <= max(0.015, bs * 0.35) and dlt <= 0.20:
        return "cleared_ok"
    if bc <= max(0.015, bs * 0.35) and dlt > 0.20:
        return "cleared_dark_smear"
    if abs(bs - bc) < 0.02 and st["mad"] < 8:
        return "left_as_source"
    # Successful wipe over white shirt / chin: source glyphs had high lap_s,
    # clean lost the stroke energy (lap collapsed) while cloth stays bright.
    # Without this, BV1P6 ~35s ("Yes, General.") was false caption_remains
    # even though the text is gone (lap_s≈5000 → lap_c≈40).
    if lap_s >= 800 and lap_c < max(200.0, lap_s * 0.08) and dlt <= 0.20:
        return "cleared_ok"
    if bc > bs * 0.5 and bs >= 0.02:
        return "caption_remains"
    return "partial"


def _dialogue_boxes(regions: dict | None, meta: dict | None) -> list[dict]:
    """Extract dialogue ROIs from regions.json (role per box) or meta (boxes+box_roles)."""
    boxes: list[dict] = []
    if regions:
        for i, b in enumerate(regions.get("boxes") or []):
            if str(b.get("role") or "") == "dialogue":
                boxes.append({"id": f"dialogue_{i}", "x": int(b["x"]), "y": int(b["y"]),
                              "w": int(b["w"]), "h": int(b["h"])})
    if not boxes and meta:
        roles = meta.get("box_roles") or []
        for i, b in enumerate(meta.get("boxes") or []):
            if i < len(roles) and roles[i] == "dialogue":
                boxes.append({"id": f"dialogue_{i}", "x": int(b["x"]), "y": int(b["y"]),
                              "w": int(b["w"]), "h": int(b["h"])})
    return boxes


def audit_dialogue_clear(src: Path, clean: Path, boxes: list[dict], *, step: int = 1) -> dict:
    """Dense per-frame audit of dialogue ROIs. Pure, no env/filesystem side effects.

    Returns a report dict with per-ROI counts, fail_ratio, ``ok`` and
    ``problem_frames`` (every failing frame with timecode + brightness stats).
    ``step=1`` (the default) means EVERY frame is audited — never downsample
    when shipping, or a 10-frame ghost glitch can hide between samples.
    """
    import cv2

    src, clean = Path(src), Path(clean)
    if not src.is_file():
        return {"ok": True, "skipped": True, "reason": "no_source_video", "boxes": 0}
    if not clean.is_file():
        return {"ok": True, "skipped": True, "reason": "no_clean_video", "boxes": 0}
    if not boxes:
        return {"ok": True, "skipped": True, "reason": "no_dialogue_box", "boxes": 0}

    cap_s = cv2.VideoCapture(str(src))
    cap_c = cv2.VideoCapture(str(clean))
    fps = float(cap_s.get(cv2.CAP_PROP_FPS) or 30) or 30.0
    n_s = int(cap_s.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    n_c = int(cap_c.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    n = min(n_s, n_c)
    tallies: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # Sequential read only: cv2 CAP_PROP_POS_FRAMES seek is unreliable on many
    # codecs (off-by-one / keyframe snapping). Seeks here would silently
    # misalign src vs clean and corrupt every per-frame statistic.
    idx = 0
    audited = 0
    problem_frames: list[dict] = []
    while idx < n:
        ok_s, fs = cap_s.read()
        ok_c, fc = cap_c.read()
        if not ok_s or not ok_c:
            break
        if idx % max(1, step) != 0:
            idx += 1
            continue
        for b in boxes:
            x0, y0 = max(0, b["x"]), max(0, b["y"])
            x1 = min(fs.shape[1], b["x"] + b["w"])
            y1 = min(fs.shape[0], b["y"] + b["h"])
            if y1 <= y0 or x1 <= x0:
                continue
            import numpy as np
            gs = cv2.cvtColor(fs[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            gc = cv2.cvtColor(fc[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            st = _stats(gs, gc)
            lab = classify_dialogue(st)
            tallies[b["id"]][lab] += 1
            if lab in FAIL_LABELS and step == 1:
                problem_frames.append({
                    "frame": idx, "t_sec": round(idx / fps, 3), "roi": b["id"],
                    "label": lab, "src_bright": round(st["bright_s"], 4),
                    "out_bright": round(st["bright_c"], 4),
                    "mad": round(st["mad"], 2), "w2b": round(st["w2b"], 4),
                })
        audited += 1
        idx += max(1, step)
    cap_s.release()
    cap_c.release()

    per_roi: dict[str, dict] = {}
    agg_fail = agg_with_caption = agg_total = 0
    counts_total: dict[str, int] = defaultdict(int)
    for bid, counts in sorted(tallies.items()):
        total = sum(counts.values())
        with_caption = total - counts.get("no_caption", 0)
        fails = sum(counts.get(k, 0) for k in FAIL_LABELS)
        pct = {k: round(100.0 * v / total, 1) for k, v in sorted(counts.items(), key=lambda x: -x[1])}
        per_roi[bid] = {
            "n": total, "counts": dict(counts), "pct": pct,
            "with_caption": with_caption, "fails": fails,
            "fail_ratio": round(fails / with_caption, 4) if with_caption else 0.0,
        }
        for k, v in counts.items():
            counts_total[k] += v
        agg_total += total
        agg_with_caption += with_caption
        agg_fail += fails

    fail_ratio = round(agg_fail / agg_with_caption, 4) if agg_with_caption else 0.0
    return {
        "ok": agg_with_caption == 0 or fail_ratio <= 0,  # refined by gate()
        "skipped": False,
        "frames": n, "audited": audited, "step": step, "fps": fps,
        "src": str(src), "clean": str(clean),
        "boxes": len(boxes),
        "counts": dict(counts_total),
        "with_caption": agg_with_caption,
        "fails": agg_fail,
        "fail_ratio": fail_ratio,
        "clear_ratio": round(1.0 - fail_ratio, 4) if agg_with_caption else 1.0,
        "per_roi": per_roi,
        "problem_frames": problem_frames,
    }


def write_problem_contact_sheet(
    work_dir: Path, report: dict, *, max_tiles: int = 48,
) -> str | None:
    """Write a src-vs-clean contact sheet of the worst caption frames.

    Quantitative ratios can be fooled by bright cloth/skin; a human-readable
    sheet is the mandatory second half of the per-frame audit. Largest-gap
    frames are picked so a ghost glitch can never hide in the sample.
    """
    import cv2
    import numpy as np

    pf = report.get("problem_frames") or []
    if not pf:
        return None
    # Rank by brightness residual (ghost/caption severity), keep spread over time.
    pf = sorted(pf, key=lambda p: -(p["out_bright"] - p["src_bright"] * 0.15))
    picks = pf[:max_tiles]
    picks = sorted(picks, key=lambda p: p["frame"])
    media = Path(work_dir) / "media"
    src_path = media / "source.mp4"
    clean_path = media / "dehardsub" / "clean.mp4"
    if not src_path.is_file() or not clean_path.is_file():
        return None
    cs, cc = cv2.VideoCapture(str(src_path)), cv2.VideoCapture(str(clean_path))
    # Sequential scan only (cv2 seek unreliable); record wanted frames in order.
    want = {int(p["frame"]): p for p in picks}
    shots: dict[int, tuple] = {}
    for i in range(int(cs.get(cv2.CAP_PROP_FRAME_COUNT) or 0)):
        ok_s, fs = cs.read()
        ok_c, fc = cc.read()
        if not (ok_s and ok_c):
            break
        if i in want:
            shots[i] = (fs.copy(), fc.copy())
            if len(shots) == len(want):
                break
    cs.release(); cc.release()
    if not shots:
        return None
    boxes = _dialogue_boxes(
        json.loads((media / "dehardsub" / "dehardsub_regions.json").read_text(encoding="utf-8"))
        if (media / "dehardsub" / "dehardsub_regions.json").is_file() else None,
        None,
    )
    if not boxes:
        return None
    b = boxes[0]
    x0, y0 = max(0, b["x"]), max(0, b["y"])
    x1, y1 = fs.shape[1], fs.shape[0]
    x1 = min(x1, b["x"] + b["w"]); y1 = min(y1, b["y"] + b["h"])
    tw, th = 420, 84
    cols = 4
    rows_n = (len(shots) + cols - 1) // cols
    canvas = np.zeros((rows_n * th * 2, cols * tw, 3), np.uint8)
    for k, (idx, (fs, fc)) in enumerate(sorted(shots.items())):
        def tile(im: np.ndarray, title: str) -> np.ndarray:
            roi = cv2.resize(im[y0:y1, x0:x1], (tw, th))
            cv2.rectangle(roi, (0, 0), (tw, 19), (0, 0, 0), -1)
            cv2.putText(roi, title, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                        (255, 255, 255), 1, cv2.LINE_AA)
            return roi
        p = want[idx]
        cell = np.vstack([
            tile(fs, f"f{idx} t={idx/30:.1f}s SRC"),
            tile(fc, f"f{idx} t={idx/30:.1f}s {p['label']} br {p['src_bright']:.2f}->{p['out_bright']:.2f}"),
        ])
        cy, cx = (k // cols) * th * 2, (k % cols) * tw
        canvas[cy:cy + th * 2, cx:cx + tw] = cell
    out = media / "dialogue_problem_frames.jpg"
    cv2.imwrite(str(out), canvas, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return str(out)


def dialogue_clear_gate(
    work_dir: Path, *, max_fail_ratio: float = 0.15, step: int = 1,
) -> dict:
    """Read dehardsub artifacts from a job work_dir and run the clear-rate gate.

    ``max_fail_ratio`` is the max fraction of caption-bearing frames allowed to
    still show captions / dark smear. Reports to ``media/dialogue_clear_gate.json``.
    """
    work_dir = Path(work_dir)
    media = work_dir / "media"
    dh = media / "dehardsub"
    regions_path = dh / "dehardsub_regions.json"
    meta_path = dh / "dehardsub_meta.json"
    clean = dh / "clean.mp4"

    regions = json.loads(regions_path.read_text(encoding="utf-8")) if regions_path.is_file() else None
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else None
    boxes = _dialogue_boxes(regions, meta)

    from note_frames import existing_source_video
    src = existing_source_video(work_dir)
    rep = audit_dialogue_clear(src, clean, boxes, step=step)

    if not rep.get("skipped"):
        fail_ratio = rep["fail_ratio"]
        rep["max_fail_ratio"] = max_fail_ratio
        rep["ok"] = fail_ratio <= max_fail_ratio
        rep["verdict"] = "PASS" if rep["ok"] else "FAIL"
        try:
            sheet = write_problem_contact_sheet(work_dir, rep)
            if sheet:
                rep["problem_sheet"] = sheet
        except Exception as exc:  # noqa: BLE001
            rep["problem_sheet_error"] = f"{type(exc).__name__}: {exc}"
    else:
        rep["verdict"] = "SKIP"

    report_path = media / "dialogue_clear_gate.json"
    try:
        report_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    rep["report"] = str(report_path)
    return rep
