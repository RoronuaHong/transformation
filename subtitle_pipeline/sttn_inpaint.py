"""STTN / caption restore for burned-in text.

Flat UI bars soft-smear under STTN, so those use native per-glyph horizontal
fill. Textured scenes still use native 1:1 STTN tiles (no bar downscale).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

STTN_W = 432
STTN_H = 240
REF_LENGTH = 10
NEIGHBOR_STRIDE = 5
MAX_LOAD = max(6, int(os.environ.get("VITUAL_STTN_MAX_LOAD", "12") or 12))


def default_ckpt() -> Path:
    env = (os.environ.get("VITUAL_STTN_CKPT") or "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "models" / "sttn.pth"


def sttn_tiles(
    box: dict[str, int],
    frame_w: int,
    frame_h: int,
    *,
    overlap: int = 64,
) -> list[dict[str, int]]:
    """1:1 432×240 tiles covering the caption box (no downscale)."""
    bx = max(0, int(box["x"]))
    by = max(0, int(box["y"]))
    bw = max(2, int(box["w"]))
    bh = max(2, int(box["h"]))
    y1 = min(frame_h, by + bh + 8)
    y0 = max(0, y1 - STTN_H)
    th = min(STTN_H, frame_h - y0)
    x_lo = max(0, bx - 8)
    x_hi = min(frame_w, bx + bw + 8)
    span = max(1, STTN_W - overlap)
    tiles: list[dict[str, int]] = []
    x = x_lo
    while True:
        tx = 0 if frame_w < STTN_W else min(x, frame_w - STTN_W)
        tw = min(STTN_W, frame_w - tx)
        rec = {"x": int(tx), "y": int(y0), "w": int(tw), "h": int(th)}
        if not tiles or rec != tiles[-1]:
            tiles.append(rec)
        if tx + tw >= x_hi or tx + tw >= frame_w:
            break
        nxt = tx + span
        if nxt <= tx:
            break
        x = nxt
    return tiles


def sttn_view(box: dict[str, int], frame_w: int, frame_h: int) -> tuple[dict[str, int], dict[str, int]]:
    """First native tile + caption box in tile coords (tests / debug)."""
    tiles = sttn_tiles(box, frame_w, frame_h)
    crop = tiles[0]
    hole = {
        "x": max(0, int(box["x"]) - crop["x"]),
        "y": max(0, int(box["y"]) - crop["y"]),
        "w": min(int(box["w"]), crop["w"]),
        "h": min(int(box["h"]), crop["h"]),
    }
    return crop, hole


def extract_tile(frame: Any, tile: dict[str, int]) -> Any:
    """Crop tile and pad to STTN_W×STTN_H."""
    import numpy as np

    x, y, w, h = int(tile["x"]), int(tile["y"]), int(tile["w"]), int(tile["h"])
    patch = frame[y : y + h, x : x + w]
    if patch.shape[0] == STTN_H and patch.shape[1] == STTN_W:
        return patch
    canvas = np.zeros((STTN_H, STTN_W, patch.shape[2]), dtype=patch.dtype)
    canvas[: patch.shape[0], : patch.shape[1]] = patch
    return canvas


def band_is_flat(bgr: Any, box: dict[str, int]) -> bool:
    """True when the caption bar is a low-variance UI strip (STTN soft-smears these)."""
    import cv2
    import numpy as np

    y = max(0, int(box["y"]))
    x = max(0, int(box["x"]))
    h = max(1, int(box["h"]))
    w = max(1, int(box["w"]))
    roi = bgr[y : y + h, x : x + w]
    if roi.size < 16:
        return False
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    mask = gray < 160
    if float(mask.mean()) < 0.2:
        return False
    return float(gray[mask].var()) < 400.0


def horiz_lerp_fill(bgr: Any, hole_u8: Any) -> Any:
    """Fill hole runs by lerping nearest clean left/right pixels (keeps horizontal grain)."""
    import numpy as np

    out = bgr.astype(np.float32).copy()
    hmask = hole_u8 > 0
    for y in np.where(hmask.any(axis=1))[0]:
        row_h = hmask[y]
        width = int(row_h.size)
        x = 0
        while x < width:
            if not row_h[x]:
                x += 1
                continue
            x0 = x
            while x < width and row_h[x]:
                x += 1
            x1 = x
            left = out[y, x0 - 1] if x0 > 0 else None
            right = out[y, x1] if x1 < width else None
            if left is None and right is None:
                continue
            if left is None:
                left = right
            if right is None:
                right = left
            nseg = x1 - x0
            for i, xi in enumerate(range(x0, x1)):
                t = (i + 1) / (nseg + 1.0)
                out[y, xi] = left * (1.0 - t) + right * t
    return np.clip(out, 0, 255).astype(np.uint8)


def text_hole_parts(bgr: Any, box: dict[str, int]) -> list[Any]:
    """Per-glyph holes (core + dark outline). Avoid one sausage that lerps into a smear."""
    import cv2
    import numpy as np

    from media_ops import _hardsub_chroma_mask

    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    y0 = max(0, int(box["y"]))
    x0 = max(0, int(box["x"]))
    y1 = min(gray.shape[0], y0 + max(1, int(box["h"])))
    x1 = min(gray.shape[1], x0 + max(1, int(box["w"])))
    if y1 <= y0 or x1 <= x0:
        return []
    bg_est = float(np.percentile(gray[y0:y1, x0:x1], 40))
    thr = max(170.0, min(210.0, bg_est + 90.0))
    bright = gray >= thr
    near = cv2.dilate(bright.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1) > 0
    bright |= (gray >= max(140.0, thr - 35.0)) & near
    chroma = _hardsub_chroma_mask(bgr)
    near_ui = cv2.dilate(chroma.astype(np.uint8), np.ones((9, 9), np.uint8), iterations=2) > 0
    bright[near_ui] = False
    clip = np.zeros(gray.shape, dtype=np.uint8)
    clip[y0:y1, x0:x1] = 255
    core = cv2.bitwise_and((bright.astype(np.uint8) * 255), clip)
    n_cc, labels, stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
    darker = gray < (bg_est - 8.0)
    parts: list[Any] = []
    for i in range(1, n_cc):
        if int(stats[i, cv2.CC_STAT_AREA]) < 8:
            continue
        comp = (labels == i).astype(np.uint8) * 255
        dil = cv2.dilate(comp, np.ones((3, 3), np.uint8), iterations=3)
        local = np.zeros_like(core)
        local[(comp > 0) | ((dil > 0) & darker)] = 255
        local = cv2.bitwise_and(local, clip)
        local[near_ui] = 0
        if int(local.max()) > 0:
            parts.append(local)
    return parts


def text_hole_for_tile(tile_bgr: Any, box: dict[str, int], tile: dict[str, int]) -> Any:
    """Caption-box hole inside this tile — no per-glyph mask."""
    import numpy as np

    h, w = tile_bgr.shape[:2]
    hole = np.zeros((h, w), dtype=np.uint8)
    bx0 = int(box["x"])
    by0 = int(box["y"])
    bx1 = bx0 + max(1, int(box["w"]))
    by1 = by0 + max(1, int(box["h"]))
    tx0 = int(tile["x"])
    ty0 = int(tile["y"])
    tx1 = tx0 + int(tile["w"])
    ty1 = ty0 + int(tile["h"])
    ix0 = max(bx0, tx0)
    iy0 = max(by0, ty0)
    ix1 = min(bx1, tx1)
    iy1 = min(by1, ty1)
    if ix1 > ix0 and iy1 > iy0:
        hole[iy0 - ty0 : iy1 - ty0, ix0 - tx0 : ix1 - tx0] = 255
    return hole


def fill_caption_spatial(bgr: Any, box: dict[str, int]) -> Any:
    """Native-res caption restore for flat UI bars (per-glyph horizontal lerp)."""
    import cv2
    import numpy as np

    from media_ops import _hardsub_chroma_mask

    out = bgr.copy()
    parts = text_hole_parts(out, box)
    if not parts:
        return out
    union = np.zeros(out.shape[:2], dtype=np.uint8)
    for part in parts:
        out = horiz_lerp_fill(out, part)
        union = np.maximum(union, part)
    y0 = max(0, int(box["y"]))
    x0 = max(0, int(box["x"]))
    y1 = min(out.shape[0], y0 + max(1, int(box["h"])))
    x1 = min(out.shape[1], x0 + max(1, int(box["w"])))
    clip = np.zeros(out.shape[:2], dtype=np.uint8)
    clip[y0:y1, x0:x1] = 255
    near_ui = cv2.dilate(
        _hardsub_chroma_mask(bgr).astype(np.uint8), np.ones((9, 9), np.uint8), iterations=2
    ) > 0
    bg_est = float(
        np.percentile(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)[y0:y1, x0:x1], 40)
    )
    for _ in range(2):
        gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
        prox = cv2.dilate(union, np.ones((5, 5), np.uint8), iterations=1) > 0
        resid = (
            (gray < (bg_est - 10.0)) & prox & (clip > 0) & (~near_ui)
        ).astype(np.uint8) * 255
        if int(resid.max()) == 0:
            break
        n_cc, labels, stats, _ = cv2.connectedComponentsWithStats(resid, connectivity=8)
        for i in range(1, n_cc):
            if int(stats[i, cv2.CC_STAT_AREA]) < 3:
                continue
            local = (labels == i).astype(np.uint8) * 255
            out = horiz_lerp_fill(out, local)
        union = np.maximum(union, resid)
    return out


def _soft_alpha(hole_u8: Any) -> Any:
    import cv2
    import numpy as np

    return np.clip(cv2.GaussianBlur(hole_u8, (5, 5), 0).astype(np.float32) / 255.0, 0.0, 1.0)


def _load_generator(ckpt: Path, device: Any) -> Any:
    import torch

    root = Path(__file__).resolve().parent / "sttn"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from model.sttn import InpaintGenerator  # type: ignore

    model = InpaintGenerator().to(device)
    data = torch.load(str(ckpt), map_location=device, weights_only=False)
    state = data["netG"] if isinstance(data, dict) and "netG" in data else data
    if isinstance(state, dict) and state and next(iter(state)).startswith("module."):
        state = {k.replace("module.", "", 1): v for k, v in state.items()}
    model.load_state_dict(state)
    model.eval()
    if device.type == "cuda":
        model = model.half()
    return model


def _get_ref_index(neighbor_ids: list[int], length: int) -> list[int]:
    refs: list[int] = []
    for i in range(0, length, REF_LENGTH):
        if i not in neighbor_ids:
            refs.append(i)
        if len(refs) >= 2:
            break
    return refs


def _hole_frames(hole_u8: Any, length: int) -> list[Any]:
    if isinstance(hole_u8, list):
        if len(hole_u8) != length:
            raise ValueError(f"hole list length {len(hole_u8)} != clip {length}")
        return hole_u8
    return [hole_u8] * length


def _complete_clip(model: Any, frames_rgb: list[Any], hole_u8: Any, device: Any) -> list[Any]:
    """Run STTN on a short RGB clip already sized 240×432."""
    import numpy as np
    import torch

    length = len(frames_rgb)
    holes = _hole_frames(hole_u8, length)
    arr = np.stack(frames_rgb, axis=0).astype(np.float32) / 255.0
    feats = torch.from_numpy(arr).permute(0, 3, 1, 2).unsqueeze(0) * 2.0 - 1.0
    mstack = np.stack([(h > 0).astype(np.float32) for h in holes], axis=0)
    masks = torch.from_numpy(mstack).unsqueeze(1).unsqueeze(0)
    binaries = [np.expand_dims((h > 0).astype(np.float32), 2) for h in holes]
    feats = feats.to(device)
    masks = masks.to(device)
    if device.type == "cuda":
        feats = feats.half()
        masks = masks.half()
    comp = [None] * length
    with torch.no_grad():
        masked = (feats * (1.0 - masks)).view(length, 3, STTN_H, STTN_W)
        del feats
        encoded = model.encoder(masked)
        del masked
        _c = encoded.size(1)
        encoded = encoded.view(1, length, _c, encoded.size(2), encoded.size(3))
        for f in range(0, length, NEIGHBOR_STRIDE):
            neighbor_ids = list(
                range(max(0, f - NEIGHBOR_STRIDE), min(length, f + NEIGHBOR_STRIDE + 1))
            )
            ref_ids = _get_ref_index(neighbor_ids, length)
            ids = neighbor_ids + ref_ids
            feat_in = encoded[0, ids].contiguous()
            mask_in = masks[0, ids].contiguous()
            pred_feat = model.infer(feat_in, mask_in)
            pred_img = torch.tanh(model.decoder(pred_feat[: len(neighbor_ids)])).detach()
            pred_np = (
                ((pred_img.float() + 1.0) / 2.0).cpu().permute(0, 2, 3, 1).numpy() * 255.0
            )
            pred_np = np.nan_to_num(pred_np, nan=0.0, posinf=255.0, neginf=0.0)
            del pred_feat, pred_img, feat_in, mask_in
            for i, idx in enumerate(neighbor_ids):
                b = binaries[idx]
                orig = frames_rgb[idx].astype(np.float32)
                img = pred_np[i].astype(np.float32) * b + orig * (1.0 - b)
                bad = ~np.isfinite(img)
                if np.any(bad):
                    img = np.where(bad, orig, img)
                if comp[idx] is None:
                    comp[idx] = img
                else:
                    comp[idx] = comp[idx] * 0.5 + img * 0.5
        del encoded, masks
        if device.type == "cuda":
            import gc

            gc.collect()
            torch.cuda.empty_cache()
    out = []
    for i in range(length):
        b = binaries[i]
        merged = np.array(comp[i]).astype(np.float32) * b + frames_rgb[i].astype(
            np.float32
        ) * (1.0 - b)
        merged = np.nan_to_num(merged, nan=0.0, posinf=255.0, neginf=0.0)
        out.append(np.clip(merged, 0, 255).astype(np.uint8))
    return out


def _tile_x_ramps(tiles: list[dict[str, int]], overlap: int = 64) -> list[Any]:
    import numpy as np

    ramps: list[Any] = []
    n = len(tiles)
    for i, tile in enumerate(tiles):
        w = int(tile["w"])
        ramp = np.ones(w, dtype=np.float32)
        ov = min(overlap, max(1, w // 4))
        if i > 0:
            ramp[:ov] = np.linspace(0.0, 1.0, ov, dtype=np.float32)
        if i < n - 1:
            ramp[-ov:] = np.linspace(1.0, 0.0, ov, dtype=np.float32)
        ramps.append(ramp)
    return ramps


def _open_ffmpeg_writer(
    src: Path,
    dest: Path,
    frame_w: int,
    frame_h: int,
    fps: float,
) -> Any:
    from media_ops import DEFAULT_AUDIO_K, find_ffmpeg, has_audio_stream

    ffmpeg = find_ffmpeg()
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s",
        f"{frame_w}x{frame_h}",
        "-r",
        f"{fps:.4f}",
        "-i",
        "pipe:0",
        "-i",
        str(src),
        "-map",
        "0:v:0",
    ]
    if has_audio_stream(src):
        cmd.extend(["-map", "1:a:0?", "-c:a", "aac", "-b:a", f"{DEFAULT_AUDIO_K}k"])
    else:
        cmd.append("-an")
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-shortest",
            str(dest),
        ]
    )
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def _encode_spatial(
    src: Path,
    dest: Path,
    box: dict[str, int],
    *,
    first: Any,
    cap: Any,
    frame_w: int,
    frame_h: int,
    fps: float,
    n_hint: int,
) -> dict[str, Any]:
    import numpy as np

    print(
        f"[sttn] mode=spatial_flat native fill box={box} "
        "(flat UI — STTN would soft-smear)",
        flush=True,
    )
    proc = _open_ffmpeg_writer(src, dest, frame_w, frame_h, fps)
    assert proc.stdin is not None
    frames_done = 0
    try:
        frame = first
        while frame is not None:
            out = fill_caption_spatial(frame, box)
            proc.stdin.write(np.ascontiguousarray(out).tobytes())
            frames_done += 1
            if n_hint and frames_done % 60 == 0:
                print(f"[sttn] wrote {frames_done}/{n_hint}", flush=True)
            ok, nxt = cap.read()
            frame = nxt if ok else None
        if n_hint:
            print(f"[sttn] wrote {frames_done}/{n_hint}", flush=True)
    except Exception:
        proc.kill()
        raise
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
    stderr = proc.communicate(timeout=300)[1]
    if proc.returncode != 0 or frames_done < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"spatial encode failed ({proc.returncode}): {err}")
    if not dest.is_file() or dest.stat().st_size < 800:
        raise RuntimeError(f"spatial encode produced empty file: {dest}")
    return {
        "engine": "sttn",
        "mode": "spatial_flat",
        "frames": frames_done,
        "device": "cpu",
        "bytes": dest.stat().st_size,
    }


def encode_sttn(
    src: Path,
    dest: Path,
    box: dict[str, int],
    *,
    ckpt: Path | None = None,
    mosaic_boxes: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Restore caption region with STTN tiles (box hole, no glyph mask).

    Default is STTN. Set ``VITUAL_STTN_FORCE=flat`` (or ``0``/``false``/``no``)
    to use the legacy spatial_flat path on flat UI bars.
    """
    import cv2
    import numpy as np
    import torch

    from media_ops import probe_video_wh

    src = Path(src)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    wh = probe_video_wh(src)
    if wh is None:
        raise RuntimeError(f"cannot probe video: {src}")
    frame_w, frame_h = wh

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {src}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 25.0) or 25.0
    n_hint = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    ok, first = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError(f"empty video: {src}")

    # Default: STTN. Opt into flat spatial fill only when explicitly requested.
    prefer_flat = (os.environ.get("VITUAL_STTN_FORCE") or "1").strip().lower() in (
        "0",
        "false",
        "no",
        "flat",
    )
    if prefer_flat and band_is_flat(first, box):
        try:
            return _encode_spatial(
                src,
                dest,
                box,
                first=first,
                cap=cap,
                frame_w=frame_w,
                frame_h=frame_h,
                fps=fps,
                n_hint=n_hint,
            )
        finally:
            cap.release()

    weights = Path(ckpt) if ckpt else default_ckpt()
    if not weights.is_file() or weights.stat().st_size < 1_000_000:
        cap.release()
        raise FileNotFoundError(
            f"STTN checkpoint missing: {weights}. "
            "Set VITUAL_STTN_CKPT or place models/sttn.pth"
        )
    def _build_region(b: dict[str, int]) -> dict[str, Any] | None:
        ts = sttn_tiles(b, frame_w, frame_h)
        if not ts:
            return None
        return {
            "box": b,
            "tiles": ts,
            "ramps": _tile_x_ramps(ts),
            "band_y": int(ts[0]["y"]),
            "band_h": int(ts[0]["h"]),
        }

    regions: list[dict[str, Any]] = []
    cap_region = _build_region(box)
    if cap_region:
        regions.append(cap_region)
    # 马赛克块也作为 STTN 的「洞」接入同一套 tiles（人脸/大块由时序补内容）。
    for mb in mosaic_boxes or []:
        r = _build_region(mb)
        if r:
            regions.append(r)
    if not regions:
        cap.release()
        raise RuntimeError("STTN produced no tiles for regions")

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    if device.type == "cuda":
        torch.cuda.empty_cache()
    print(
        f"[sttn] mode=tiles device={device} ckpt={weights.name} tiles={len(tiles)} "
        f"native={STTN_W}x{STTN_H} box={box}",
        flush=True,
    )
    model = _load_generator(weights, device)
    # Write to a temp file so a crash never leaves a truncated clean.mp4.
    writing = dest.with_name("_clean_sttn_writing.mp4")
    if writing.is_file():
        writing.unlink(missing_ok=True)
    proc = _open_ffmpeg_writer(src, writing, frame_w, frame_h, fps)
    assert proc.stdin is not None

    chunk = max(6, min(MAX_LOAD, 16))
    step = max(1, chunk - 2)
    pending: list[Any] = []
    frames_done = 0

    def _composite_frame(orig: Any, regions_data: list[Any], idx: int) -> Any:
        out = orig.copy()
        for r in regions_data:
            tile_preds = r["tile_preds"]
            tile_holes = r["tile_holes"]
            tiles = r["tiles"]
            ramps = r["ramps"]
            band_y = r["band_y"]
            band_h = r["band_h"]
            num = np.zeros((band_h, frame_w, 3), dtype=np.float32)
            den = np.zeros((band_h, frame_w), dtype=np.float32)
            for ti, tile in enumerate(tiles):
                preds = tile_preds[ti]
                if preds is None:
                    continue
                pred = cv2.cvtColor(preds[idx], cv2.COLOR_RGB2BGR).astype(np.float32)
                alpha = _soft_alpha(tile_holes[ti][idx])
                tw = int(tile["w"])
                th = int(tile["h"])
                a = alpha[:th, :tw] * ramps[ti][None, :tw]
                x = int(tile["x"])
                ly = int(tile["y"]) - band_y
                num[ly : ly + th, x : x + tw] += pred[:th, :tw] * a[:, :, None]
                den[ly : ly + th, x : x + tw] += a
            if float(den.max()) <= 1e-4:
                continue
            den3 = np.maximum(den[:, :, None], 1e-6)
            blend = np.clip(den, 0.0, 1.0)[:, :, None]
            band = out[band_y : band_y + band_h].astype(np.float32)
            avg = num / den3
            band = band * (1.0 - blend) + avg * blend
            out[band_y : band_y + band_h] = np.clip(band, 0, 255).astype(np.uint8)
        return out

    def flush_clip(bgr_frames: list[Any], regions: list[dict], *, last: bool) -> None:
        nonlocal frames_done
        n = len(bgr_frames)
        write_n = n if last else min(n, step)
        regions_data: list[Any] = []
        for r in regions:
            tiles = r["tiles"]
            box = r["box"]
            tile_preds: list[Any] = []
            tile_holes: list[list[Any]] = []
            for tile in tiles:
                rgbs: list[Any] = []
                holes: list[Any] = []
                any_hole = False
                for fr in bgr_frames:
                    patch = extract_tile(fr, tile)
                    hole = text_hole_for_tile(patch, box, tile)
                    if hole.shape[0] != STTN_H or hole.shape[1] != STTN_W:
                        padded = np.zeros((STTN_H, STTN_W), dtype=np.uint8)
                        padded[: hole.shape[0], : hole.shape[1]] = hole
                        hole = padded
                    if int(hole.max()) > 0:
                        any_hole = True
                    rgbs.append(cv2.cvtColor(patch, cv2.COLOR_BGR2RGB))
                    holes.append(hole)
                tile_holes.append(holes)
                if any_hole:
                    tile_preds.append(_complete_clip(model, rgbs, holes, device))
                else:
                    tile_preds.append(None)
            regions_data.append(
                {
                    "tile_preds": tile_preds,
                    "tile_holes": tile_holes,
                    "tiles": tiles,
                    "ramps": r["ramps"],
                    "band_y": r["band_y"],
                    "band_h": r["band_h"],
                }
            )
        for i in range(write_n):
            out = _composite_frame(bgr_frames[i], regions_data, i)
            proc.stdin.write(np.ascontiguousarray(out).tobytes())
            frames_done += 1
        if n_hint:
            print(f"[sttn] wrote {frames_done}/{n_hint}", flush=True)
        keep = n - write_n
        if keep > 0:
            pending[:] = bgr_frames[-keep:]
        else:
            pending.clear()

    try:
        bgr_buf: list[Any] = [first]
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            bgr_buf.append(frame)
            if len(bgr_buf) >= chunk:
                flush_clip(bgr_buf, regions, last=False)
                bgr_buf = list(pending)
        if bgr_buf:
            flush_clip(bgr_buf, regions, last=True)
    except Exception:
        proc.kill()
        raise
    finally:
        cap.release()
        try:
            proc.stdin.close()
        except Exception:
            pass
    stderr = proc.communicate(timeout=300)[1]
    if proc.returncode != 0 or frames_done < 1:
        err = (stderr or b"").decode("utf-8", errors="replace")[:400]
        try:
            writing.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(f"STTN encode failed ({proc.returncode}): {err}")
    if not writing.is_file() or writing.stat().st_size < 800:
        raise RuntimeError(f"STTN produced empty file: {writing}")
    # Windows often locks clean.mp4 if a player/IDE has it open — retry.
    import shutil
    import time

    last_err: OSError | None = None
    for _ in range(8):
        try:
            if dest.is_file():
                dest.unlink()
            writing.replace(dest)
            last_err = None
            break
        except OSError as exc:
            last_err = exc
            time.sleep(0.4)
    if last_err is not None:
        # Fall back to copy; leave writing for manual recovery.
        try:
            shutil.copy2(writing, dest)
            writing.unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"STTN wrote {writing} but could not replace {dest}: {exc}"
            ) from exc
    return {
        "engine": "sttn",
        "mode": "tiles",
        "frames": frames_done,
        "tiles": tiles,
        "ckpt": weights.name,
        "device": str(device),
        "bytes": dest.stat().st_size,
    }
