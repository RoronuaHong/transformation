"""Gold workflow: classification + mask geometry (no GPU required for unit tests)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from dehardsub_gold_workflow import (
    GOLD_TOP_BRIGHT_MAX,
    GOLD_TOP_MAD_LEFT_MAX,
    GOLD_TOP_MAD_RIGHT_MAX,
    boxes_from_regions,
    build_gold_masks,
    classify_boxes,
    gold_workflow_enabled,
    top_gold_contract,
    wipe_top_watermarks,
)

_ROOT = Path(__file__).resolve().parent
_GOLD_DIR = _ROOT / "eval" / "gold" / "dehardsub_gold_workflow"
_BV1P6 = _ROOT / "downloads" / "batch" / "bilibili_BV1P6N76qEuJ"
_GOLD_JPG = _GOLD_DIR / "src_0001_final2.jpg"
_GOLD50 = _GOLD_DIR / "gold50.mp4"
_SRC = _BV1P6 / "media" / "source.mp4"
_REGIONS = _BV1P6 / "media" / "dehardsub" / "dehardsub_regions.json"


def test_gold_workflow_enabled_default(monkeypatch):
    monkeypatch.delenv("VITUAL_DEHARDSUB_GOLD_WORKFLOW", raising=False)
    assert gold_workflow_enabled() is True
    monkeypatch.setenv("VITUAL_DEHARDSUB_GOLD_WORKFLOW", "0")
    assert gold_workflow_enabled() is False


def test_classify_boxes_generic():
    boxes = [
        {"role": "up_watermark", "x": 0, "y": 2, "w": 200, "h": 50},
        {"role": "up_watermark", "x": 600, "y": 140, "w": 120, "h": 50},
        {"role": "dialogue", "x": 216, "y": 399, "w": 428, "h": 62},
    ]
    top, mid, dlg = classify_boxes(boxes, frame_h=480)
    assert len(top) == 1 and top[0]["y"] == 2
    assert len(mid) == 1 and mid[0]["y"] == 140
    assert len(dlg) == 1 and dlg[0]["role"] == "dialogue"


def test_boxes_from_regions_keep_filtered():
    regions = {
        "boxes": [
            {"role": "dialogue", "x": 1, "y": 2, "w": 3, "h": 4},
            {"role": "title", "x": 0, "y": 0, "w": 10, "h": 10, "action": "keep"},
        ]
    }
    got = boxes_from_regions(regions)
    assert len(got) == 1 and got[0]["role"] == "dialogue"


def test_build_gold_masks_dialogue_hits_white_glyphs():
    # Synthetic: dark cloth + white subtitle strokes in gold band area.
    frame = np.full((480, 852, 3), 40, dtype=np.uint8)
    frame[400:430, 300:550] = 220  # bright low-sat "glyphs"
    boxes = [{"role": "dialogue", "x": 216, "y": 399, "w": 428, "h": 62}]
    ns, lama = build_gold_masks(frame, boxes)
    assert int(ns.max()) == 0
    assert int(lama.max()) > 0
    assert int(lama[410:420, 320:330].max()) > 0


def test_dialogue_band_covers_wide_box_and_spill():
    from dehardsub_gold_workflow import (
        _dialogue_band_xyxy,
        _widen_band_to_glyphs,
        dialogue_glyphs_present,
    )
    import cv2

    box = {"role": "dialogue", "x": 192, "y": 403, "w": 468, "h": 67}
    x0, y0, x1, y1 = _dialogue_band_xyxy(box, 852, 480)
    # Must cover full detect box (old fixed-370 band clipped to 241..611).
    assert x0 <= 192 and x1 >= 192 + 468
    frame = np.full((480, 852, 3), 40, dtype=np.uint8)
    # Simulate multi-glyph subtitle line (several blobs, wide span).
    for x in range(180, 780, 40):
        frame[400:430, x : x + 22] = 220
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sat = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[..., 1].astype(np.float32)
    med = cv2.medianBlur(gray.astype(np.uint8), 15).astype(np.float32)
    residual = gray - med
    assert dialogue_glyphs_present(gray, sat, residual, (x0, y0, x1, y1))
    wx0, wy0, wx1, wy1 = _widen_band_to_glyphs(
        gray, sat, residual, (x0, y0, x1, y1), W=852
    )
    assert wx0 <= 180 + 24 and wx1 >= 780 - 40
    ns, lama = build_gold_masks(frame, [box])
    assert int(lama[410, 200]) > 0 and int(lama[410, 700]) > 0


def test_no_subtitle_closeup_skips_dialogue_mask():
    # Bright teeth-like blob in dialogue band but NOT a subtitle line.
    frame = np.full((480, 852, 3), 40, dtype=np.uint8)
    frame[400:430, 400:430] = 230  # single highlight
    boxes = [{"role": "dialogue", "x": 192, "y": 403, "w": 468, "h": 67}]
    ns, lama = build_gold_masks(frame, boxes)
    assert int(lama.max()) == 0


def test_top_ink_mask_skips_flat_bright_window():
    """Bright flat plate must not become a full-box fog mask."""
    from dehardsub_gold_workflow import top_ink_mask, wipe_top_watermarks

    frame = np.full((480, 852, 3), 40, dtype=np.uint8)
    # Flat bright window in top-right (no logo glyphs).
    frame[0:60, 500:840] = 200
    box = {"role": "up_watermark", "x": 500, "y": 1, "w": 350, "h": 54}
    _eb, mask = top_ink_mask(frame, box)
    # Cap / CC filter: mask must stay a small fraction of the box.
    assert int((mask > 0).sum()) < int(350 * 54 * 0.35)
    wiped = wipe_top_watermarks(frame, [box])
    # Mostly unchanged (no full-box replace).
    assert float(np.mean(np.abs(wiped.astype(np.float32) - frame.astype(np.float32)))) < 8.0


def test_top_ink_mask_hits_logo_glyphs_on_dark_bg():
    from dehardsub_gold_workflow import top_ink_mask

    frame = np.full((480, 852, 3), 30, dtype=np.uint8)
    # Several white glyph blobs (logo-like) in top-right.
    for x in (520, 560, 600, 640, 680, 720):
        frame[8:40, x : x + 18] = 230
    box = {"role": "up_watermark", "x": 500, "y": 1, "w": 350, "h": 54}
    _eb, mask = top_ink_mask(frame, box)
    assert int((mask > 0).sum()) > 80
    assert int((mask[8:40, 520:740] > 0).sum()) > 40


def test_wipe_top_gold_ns_clears_white_logo():
    """Gold-frame ns_box_wipe must clear hard white top logos."""
    from dehardsub_gold_workflow import wipe_top_watermarks

    frame = np.full((480, 852, 3), 35, dtype=np.uint8)
    for x in range(852):
        frame[:, x] = np.clip(25 + x // 40, 0, 80)
    for x in (520, 560, 600, 640, 680, 720, 760):
        frame[6:36, x : x + 16] = 235
    boxes = [
        {"role": "up_watermark", "x": 500, "y": 1, "w": 350, "h": 54},
        {"role": "up_watermark", "x": 1, "y": 1, "w": 220, "h": 48},
    ]
    wiped = wipe_top_watermarks(frame, boxes)
    before = float((frame[6:36, 520:780] > 200).mean())
    after = float((wiped[6:36, 520:780] > 200).mean())
    assert before > 0.15 and after < before * 0.35


def test_keyframe_indices_include_ends():
    from dehardsub_gold_workflow import keyframe_indices

    idxs = keyframe_indices(100, 30.0, stride_sec=0.5)
    assert idxs[0] == 0 and idxs[-1] == 99
    assert all(idxs[i] < idxs[i + 1] for i in range(len(idxs) - 1))


def test_wipe_top_ignores_donor_stays_on_ns():
    """Top bar must stay gold ns_box_wipe even when a donor is offered."""
    from dehardsub_gold_workflow import wipe_top_watermarks

    frame = np.full((480, 852, 3), 40, dtype=np.uint8)
    for x in (520, 560, 600, 640, 680, 720):
        frame[8:36, x : x + 14] = 240
    # Poison donor: bright magenta — must NOT be pasted onto top.
    donor = np.full_like(frame, (255, 0, 255))
    boxes = [{"role": "up_watermark", "x": 500, "y": 1, "w": 350, "h": 54}]
    wiped = wipe_top_watermarks(frame, boxes, donor=donor)
    assert float((wiped[8:36, 520:740] > 200).mean()) < 0.08
    # Magenta channel must not dominate (donor rejected).
    assert float(np.mean(wiped[10:40, 520:800, 2])) < 200


def test_fill_mask_from_donor_clears_residual_when_match():
    from dehardsub_gold_workflow import fill_mask_from_donor

    frame = np.full((64, 64, 3), 50, dtype=np.uint8)
    frame[20:40, 20:40] = 230
    donor = np.full((64, 64, 3), 50, dtype=np.uint8)
    mask = np.zeros((64, 64), np.uint8)
    mask[20:40, 20:40] = 255
    out, residual = fill_mask_from_donor(frame, donor, mask)
    assert float(np.mean(out[20:40, 20:40])) < 80
    assert int(residual.max()) == 0


def test_top_gold_contract_forbids_expand_and_donor():
    c = top_gold_contract()
    assert c["box_source"] == "regions_raw"
    assert c["method"] == "ns_box_wipe"
    assert c["expand_static_overlay_box"] is False
    assert c["donor_paste"] is False
    assert c["keyframe_ns_median_plate"] is False
    assert c["morphic_tile"] is False
    assert c["telea"] is False


@pytest.mark.skipif(
    not (_GOLD_JPG.is_file() and _SRC.is_file() and _REGIONS.is_file()),
    reason="BV1P6 gold assets not present",
)
def test_top_wipe_aligns_bv1p6_f0_to_gold_final2():
    """Regression: raw-box ns_box_wipe must stay within gold MAD gates."""
    import cv2

    boxes = boxes_from_regions(_REGIONS)
    cap = cv2.VideoCapture(str(_SRC))
    ok, src = cap.read()
    cap.release()
    assert ok and src is not None
    gold = cv2.imread(str(_GOLD_JPG))
    assert gold is not None and gold.shape == src.shape
    wiped = wipe_top_watermarks(src, boxes)
    top, _, _ = classify_boxes(boxes, frame_h=src.shape[0])
    assert top, "expected top watermark boxes"
    W = src.shape[1]
    for b in top:
        x, y, w, h = int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
        mad = float(
            np.mean(
                np.abs(
                    wiped[y : y + h, x : x + w].astype(np.float32)
                    - gold[y : y + h, x : x + w].astype(np.float32)
                )
            )
        )
        bright = float(
            (cv2.cvtColor(wiped[y : y + h, x : x + w], cv2.COLOR_BGR2GRAY) > 180).mean()
        )
        assert bright <= GOLD_TOP_BRIGHT_MAX
        if x >= int(W * 0.45):
            assert mad <= GOLD_TOP_MAD_RIGHT_MAX, f"right top MAD {mad:.2f}"
        else:
            assert mad <= GOLD_TOP_MAD_LEFT_MAX, f"left top MAD {mad:.2f}"


@pytest.mark.skipif(not _GOLD50.is_file() or not _SRC.is_file(), reason="gold50 missing")
def test_top_wipe_not_worse_than_expand_path_on_gold50():
    """Guardrail: expand_static_overlay_box must not beat raw-box vs gold50."""
    import cv2
    from visual_cleanup import expand_static_overlay_box, ns_box_wipe

    boxes = boxes_from_regions(_REGIONS)
    cap = cv2.VideoCapture(str(_SRC))
    ok, src = cap.read()
    cap.release()
    gcap = cv2.VideoCapture(str(_GOLD50))
    ok2, g50 = gcap.read()
    gcap.release()
    assert ok and ok2 and src is not None and g50 is not None
    top, _, _ = classify_boxes(boxes, frame_h=src.shape[0])
    H, W = src.shape[:2]
    raw = wipe_top_watermarks(src, boxes)
    expanded = src.copy()
    for b in top:
        eb = expand_static_overlay_box(
            {k: int(b[k]) for k in ("x", "y", "w", "h")},
            W,
            H,
            role="up_watermark",
        )
        expanded = ns_box_wipe(expanded, eb, feather=6, radius=5)

    def _mad_sum(fr: np.ndarray) -> float:
        s = 0.0
        for b in top:
            x, y, w, h = int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])
            s += float(
                np.mean(
                    np.abs(
                        fr[y : y + h, x : x + w].astype(np.float32)
                        - g50[y : y + h, x : x + w].astype(np.float32)
                    )
                )
            )
        return s

    assert _mad_sum(raw) < _mad_sum(expanded) - 1.0
