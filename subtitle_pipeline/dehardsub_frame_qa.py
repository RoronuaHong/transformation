"""Multi-timestamp spot-check: source vs clean ROIs (not single-frame eyeballing)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _stats(gray: np.ndarray) -> dict[str, float]:
    return {
        "mean": round(float(gray.mean()), 2),
        "dark": round(float((gray < 30).mean()), 4),
        "bright": round(float((gray > 180).mean()), 4),
        "edge": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1),
    }


def _white_to_black_frac(src_gray: np.ndarray, clean_gray: np.ndarray) -> float:
    """Fraction of pixels that were bright in src but near-black in clean."""
    if src_gray.shape != clean_gray.shape:
        return 0.0
    return float(((src_gray > 170) & (clean_gray < 90)).mean())


def _roi_psnr(src_gray: np.ndarray, clean_gray: np.ndarray) -> float:
    """PSNR between src/clean ROI; 99 = near-identical."""
    if src_gray.shape != clean_gray.shape or src_gray.size < 4:
        return 0.0
    mse = float(
        np.mean(
            (src_gray.astype(np.float32) - clean_gray.astype(np.float32)) ** 2
        )
    )
    if mse < 1e-6:
        return 99.0
    return float(20.0 * np.log10(255.0 / np.sqrt(mse)))


def _flag_spans(flags: list[str]) -> list[dict[str, Any]]:
    """Collapse per-sample flags into contiguous time spans by role+kind (D14)."""
    # flag form: "t=1.2 dialogue_0 darkened ..."
    buckets: dict[str, list[float]] = {}
    for f in flags:
        parts = str(f).split()
        if len(parts) < 3 or not parts[0].startswith("t="):
            continue
        try:
            t = float(parts[0][2:])
        except ValueError:
            continue
        role = parts[1]
        kind = parts[2]
        key = f"{role}|{kind}"
        buckets.setdefault(key, []).append(t)
    spans: list[dict[str, Any]] = []
    for key, ts in buckets.items():
        role, kind = key.split("|", 1)
        ts = sorted(ts)
        start = ts[0]
        prev = ts[0]
        for t in ts[1:]:
            if t - prev > 2.5:
                spans.append(
                    {
                        "role": role,
                        "kind": kind,
                        "start": round(start, 2),
                        "end": round(prev, 2),
                    }
                )
                start = t
            prev = t
        spans.append(
            {
                "role": role,
                "kind": kind,
                "start": round(start, 2),
                "end": round(prev, 2),
            }
        )
    return spans


def qa_pair(
    src: Path,
    clean: Path,
    boxes: list[dict[str, Any]],
    *,
    times_sec: list[float] | None = None,
    out_dir: Path | None = None,
    write_frames: bool = True,
) -> dict[str, Any]:
    src, clean = Path(src), Path(clean)
    cap_s = cv2.VideoCapture(str(src))
    cap_c = cv2.VideoCapture(str(clean))
    fps = float(cap_s.get(cv2.CAP_PROP_FPS) or 30.0) or 30.0
    n = int(cap_s.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if times_sec is None:
        times_sec = [i * max(n / fps, 1.0) / 5 for i in range(1, 5)]
    out_path: Path | None = None
    if write_frames:
        out_path = Path(out_dir) if out_dir else clean.parent / "_qa_frames"
        out_path.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for t in times_sec:
        idx = min(max(0, int(round(t * fps))), max(0, n - 1))
        cap_s.set(cv2.CAP_PROP_POS_FRAMES, idx)
        cap_c.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok_s, fs = cap_s.read()
        ok_c, fc = cap_c.read()
        if not ok_s or not ok_c:
            continue
        jpg_name = ""
        if out_path is not None:
            side = np.hstack([fs, fc])
            cv2.putText(
                side,
                f"t={t:.1f}s L=src R=clean",
                (12, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 255),
                2,
            )
            jpg = out_path / f"qa_t{t:.1f}.jpg"
            cv2.imwrite(str(jpg), side)
            jpg_name = jpg.name
        entry: dict[str, Any] = {"t": t, "frame": idx, "jpg": jpg_name, "rois": {}}
        for i, b in enumerate(boxes):
            x, y, w, h = int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
            if w < 2 or h < 2:
                continue
            y1 = min(fs.shape[0], y + h)
            x1 = min(fs.shape[1], x + w)
            y0 = max(0, y)
            x0 = max(0, x)
            if y1 <= y0 or x1 <= x0:
                continue
            role = f"{b.get('role') or 'box'}_{i}"
            gs = cv2.cvtColor(fs[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            gc = cv2.cvtColor(fc[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
            entry["rois"][role] = {
                "src": _stats(gs),
                "clean": _stats(gc),
                "white_to_black": round(_white_to_black_frac(gs, gc), 4),
                "psnr": round(_roi_psnr(gs, gc), 2),
            }
        rows.append(entry)
    cap_s.release()
    cap_c.release()
    report = {"src": str(src), "clean": str(clean), "samples": rows}
    # Flags: dialogue dark/w2b; watermark bright residual / w2b / half-wipe.
    # Role keys are ``{role}_{i}`` (e.g. dialogue_0) — never exact ``dialogue``.
    flags: list[str] = []
    identical_votes = 0
    identical_total = 0
    for row in rows:
        for role, st in row["rois"].items():
            w2b = float(st.get("white_to_black") or 0.0)
            bright_s = float(st["src"]["bright"])
            bright_c = float(st["clean"]["bright"])
            dark_d = float(st["clean"]["dark"]) - float(st["src"]["dark"])
            psnr = float(st.get("psnr") or 0.0)
            if "watermark" in role or role.startswith("up"):
                if bright_c > 0.02 and bright_c > bright_s * 0.35:
                    flags.append(
                        f"t={row['t']:.1f} {role} bright residual "
                        f"{bright_c:.3f}"
                    )
                if w2b >= 0.04:
                    flags.append(
                        f"t={row['t']:.1f} {role} white_to_black {w2b:.3f}"
                    )
                if bright_s > 0.05 and bright_c < bright_s * 0.2 and dark_d > 0.05:
                    flags.append(
                        f"t={row['t']:.1f} {role} half_wipe "
                        f"{bright_s:.3f}->{bright_c:.3f}"
                    )
            if role.startswith("dialogue"):
                if dark_d > 0.15:
                    flags.append(
                        f"t={row['t']:.1f} {role} darkened "
                        f"{st['src']['dark']:.3f}->{st['clean']['dark']:.3f}"
                    )
                if w2b >= 0.04:
                    flags.append(
                        f"t={row['t']:.1f} {role} white_to_black {w2b:.3f}"
                    )
                # D14: src had readable bright glyphs but clean ≈ src → wipe noop.
                if bright_s >= 0.04:
                    identical_total += 1
                    if psnr >= 40.0 and abs(bright_s - bright_c) < 0.015 and w2b < 0.005:
                        identical_votes += 1
    report["flags"] = flags
    report["spans"] = _flag_spans(flags)
    byte_identical = bool(
        identical_total >= 2 and identical_votes >= max(2, int(0.75 * identical_total))
    )
    report["byte_identical"] = byte_identical
    if byte_identical:
        flags.append("byte_identical dialogue ROIs (wipe likely no-op)")
        report["flags"] = flags
    # Truthful delivery hint for callers (D14).
    report["truthful_ok"] = not flags and not byte_identical
    if out_path is not None:
        (out_path / "qa_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[qa] samples={len(rows)} flags={len(flags)} dir={out_path}", flush=True)
    else:
        print(f"[qa] samples={len(rows)} flags={len(flags)} (no frames)", flush=True)
    for f in flags[:12]:
        print(f"[qa] FLAG {f}", flush=True)
    return report


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--clean", required=True)
    ap.add_argument("--regions", required=True)
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()
    boxes = json.loads(Path(args.regions).read_text(encoding="utf-8")).get("boxes") or []
    qa_pair(Path(args.src), Path(args.clean), boxes, out_dir=Path(args.out_dir) if args.out_dir else None)
