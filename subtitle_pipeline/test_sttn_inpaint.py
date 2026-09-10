"""Native STTN tiles + text-only hole (not a downscaled caption bar)."""

from pathlib import Path

from sttn_inpaint import STTN_H, STTN_W, spatial_sampling_policy, sttn_tiles, sttn_view, text_hole_for_tile


def test_sttn_tiles_are_native_and_cover_box() -> None:
    box = {"x": 267, "y": 895, "w": 907, "h": 122}
    tiles = sttn_tiles(box, 1440, 1080)
    assert len(tiles) >= 2
    for tile in tiles:
        assert tile["w"] == STTN_W
        assert tile["h"] == STTN_H
    x0 = min(t["x"] for t in tiles)
    x1 = max(t["x"] + t["w"] for t in tiles)
    y0 = min(t["y"] for t in tiles)
    y1 = max(t["y"] + t["h"] for t in tiles)
    assert x0 <= box["x"]
    assert x1 >= box["x"] + box["w"]
    assert y0 <= box["y"]
    assert y1 >= box["y"] + box["h"]
    crop, _hole = sttn_view(box, 1440, 1080)
    assert crop["w"] == STTN_W
    assert crop["h"] == STTN_H


def test_text_hole_is_caption_box_not_glyphs() -> None:
    import numpy as np

    tile = {"x": 259, "y": 785, "w": 432, "h": 240}
    box = {"x": 267, "y": 895, "w": 400, "h": 122}
    img = np.zeros((240, 432, 3), dtype=np.uint8)
    img[:] = (48, 62, 78)
    hole = text_hole_for_tile(img, box, tile)
    assert hole.shape == (240, 432)
    # Box maps to tile-local y: 895-785=110 .. 110+122=232, x: 267-259=8 .. 8+400=408
    assert hole[160, 180] == 255
    assert hole[40, 200] == 0  # above caption box
    local = hole[110:232, 8:408]
    assert float((local > 0).mean()) > 0.98


def test_spatial_fill_clears_flat_bar_text() -> None:
    import numpy as np

    from sttn_inpaint import band_is_flat, fill_caption_spatial

    frame = np.zeros((200, 400, 3), dtype=np.uint8)
    frame[:] = (50, 50, 50)
    frame[20:40, :] = (40, 40, 40)  # horizontal groove
    box = {"x": 40, "y": 120, "w": 300, "h": 50}
    frame[130:170, 80:200] = (245, 245, 245)
    assert band_is_flat(frame, box)
    out = fill_caption_spatial(frame, box)
    assert int(out[150, 140].max()) < 100
    # groove outside glyphs preserved
    assert int(out[30, 200].mean()) < 45


def test_tbe_recovers_grain_from_other_frames() -> None:
    """TBE should paste real background grain, not a flat lerp smear."""
    import numpy as np

    from sttn_inpaint import build_temporal_plate, fill_caption_tbe

    rng = np.random.default_rng(0)
    # Keep caption well right of left-UI protect strip (w//5).
    box = {"x": 120, "y": 120, "w": 220, "h": 40}
    frames = []
    for i in range(12):
        fr = np.zeros((200, 400, 3), dtype=np.uint8)
        fr[:] = (48, 52, 58)
        fr = np.clip(
            fr.astype(np.int16) + rng.integers(-8, 9, fr.shape), 0, 255
        ).astype(np.uint8)
        x0 = 140 + (i % 6) * 20
        fr[130:155, x0 : x0 + 60] = (250, 250, 250)
        frames.append(fr)
    plate, hits = build_temporal_plate(frames, box, min_hits=2)
    assert float((hits[130:155, 140:260] >= 2).mean()) > 0.5
    dirty = frames[0].copy()
    out = fill_caption_tbe(
        dirty, box, plate, hits, min_hits=2, spatial_residual=False
    )
    assert int(out[140, 170].mean()) < 90
    assert float(out[130:155, 140:260].astype(np.float32).std()) > 2.0


def test_spatial_sampling_policy_is_more_aggressive_for_short_wide_segments() -> None:
    policy = spatial_sampling_policy(n_hint=69, frame_w=1920, box_w=1759, has_mosaic=False)
    assert policy["min_hits"] == 2
    assert policy["sample_stride"] >= 4


def test_spatial_sampling_policy_stays_conservative_for_long_or_mosaic_runs() -> None:
    long_policy = spatial_sampling_policy(n_hint=8210, frame_w=1920, box_w=1759, has_mosaic=False)
    assert long_policy["sample_stride"] >= 3
    mosaic_policy = spatial_sampling_policy(n_hint=69, frame_w=1920, box_w=1759, has_mosaic=True)
    assert mosaic_policy["min_hits"] >= 3
    assert mosaic_policy["sample_stride"] <= long_policy["sample_stride"]


def test_auto_engine_calls_sttn(tmp_path: Path, monkeypatch: object) -> None:
    import shutil

    from media_ops import strip_hardsubs
    from test_media_ops import _tiny_mp4_with_bottom_bar
    from visual_cleanup import run_multipass_cleanup

    called: dict[str, object] = {}

    def fake_encode(src, dest, box, **_kw):
        shutil.copy2(src, dest)
        called["box"] = dict(box)
        return {"engine": "sttn", "frames": 4, "device": "cpu"}

    monkeypatch.setattr("sttn_inpaint.encode_sttn", fake_encode)
    media = tmp_path / "media"
    media.mkdir()
    src = media / "source.mp4"
    _tiny_mp4_with_bottom_bar(src, seconds=0.8)
    dest = media / "dehardsub" / "clean.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = run_multipass_cleanup(
        src,
        dest,
        work_dir=tmp_path,
        locate_mode="band",
        engine="sttn",
        demosaic=False,
        dehardsub=True,
    )
    assert meta["action"] in ("sttn_cleanup", "hybrid_cleanup")
    assert called.get("box")
    cleaned, report = strip_hardsubs(
        tmp_path, video=src, force=True, mode="band", engine="sttn", demosaic=False
    )
    assert report["action"] in ("sttn_cleanup", "hybrid_cleanup")
    assert cleaned.is_file()


def test_encode_sttn_auto_routes_flat_bar(tmp_path: Path, monkeypatch: object) -> None:
    """Default auto: flat UI bars must not enter STTN tiles."""
    import shutil

    import cv2
    import numpy as np

    from sttn_inpaint import encode_sttn

    src = tmp_path / "flat.mp4"
    w, h, fps, n = 320, 240, 10, 8
    writer = cv2.VideoWriter(str(src), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert writer.isOpened()
    for _ in range(n):
        fr = np.full((h, w, 3), 48, dtype=np.uint8)
        fr[190:230, 40:280] = (40, 40, 40)  # flat dark bar
        fr[200:220, 80:200] = (245, 245, 245)  # glyphs
        writer.write(fr)
    writer.release()
    dest = tmp_path / "out.mp4"
    box = {"x": 40, "y": 190, "w": 240, "h": 40}
    routed: dict[str, str] = {}

    def fake_spatial(src_p, dest_p, box_p, **_kw):
        routed["mode"] = "temporal_flat"
        shutil.copy2(src_p, dest_p)
        return {"engine": "sttn", "mode": "temporal_flat", "frames": n}

    def boom_tiles(*_a, **_k):
        raise AssertionError("tiles path must not run for flat bars")

    monkeypatch.setenv("VITUAL_STTN_FORCE", "auto")
    monkeypatch.setattr("sttn_inpaint._encode_spatial", fake_spatial)
    # If routing wrongly picks tiles, default_ckpt / torch would run; stub tiles builder.
    monkeypatch.setattr("sttn_inpaint.sttn_tiles", boom_tiles)
    stats = encode_sttn(src, dest, box)
    assert routed.get("mode") == "temporal_flat"
    assert stats.get("mode") == "temporal_flat"
    assert dest.is_file()


def test_band_is_flat_rejects_textured_desk() -> None:
    """Wood/scene texture behind glyphs must not route to temporal_flat."""
    import numpy as np

    from sttn_inpaint import band_is_flat

    rng = np.random.default_rng(0)
    frame = np.zeros((240, 400, 3), dtype=np.uint8)
    # High-contrast wood-like grain (core_var ≫ UI strip).
    noise = rng.integers(15, 200, (50, 300, 3), dtype=np.uint8)
    frame[170:220, 50:350] = noise
    frame[185:205, 100:220] = (245, 245, 245)
    assert band_is_flat(frame, {"x": 50, "y": 170, "w": 300, "h": 50}) is False


def test_band_is_flat_rejects_midtone_clothing() -> None:
    """Smooth suit/skin midtones must not count as a UI caption bar."""
    import numpy as np

    from sttn_inpaint import band_is_flat

    frame = np.zeros((240, 400, 3), dtype=np.uint8)
    frame[170:220, 50:350] = (170, 165, 160)  # suit gray
    frame[185:205, 80:280] = (245, 245, 245)  # white glyphs
    # dark outline
    frame[184:206, 80:82] = (20, 20, 20)
    assert band_is_flat(frame, {"x": 50, "y": 170, "w": 300, "h": 50}) is False


def test_caption_band_is_black_bar_detects_dark_ui_strip() -> None:
    import numpy as np

    from sttn_inpaint import caption_band_is_black_bar, fill_caption_black_bar

    frame = np.zeros((240, 400, 3), dtype=np.uint8)
    frame[:] = (80, 90, 100)
    frame[180:230, 20:380] = (8, 8, 8)
    frame[195:215, 80:300] = (245, 245, 245)
    box = {"x": 20, "y": 180, "w": 360, "h": 50}
    assert caption_band_is_black_bar(frame, box) is True
    out = fill_caption_black_bar(frame, box)
    assert int(out[200, 150].max()) < 20
    assert int(out[100, 200].mean()) > 50


def test_caption_band_is_black_bar_rejects_bright_scene() -> None:
    import numpy as np

    from sttn_inpaint import caption_band_is_black_bar

    frame = np.zeros((240, 400, 3), dtype=np.uint8)
    frame[180:230, 20:380] = (120, 110, 90)
    frame[195:215, 80:300] = (245, 245, 245)
    assert caption_band_is_black_bar(frame, {"x": 20, "y": 180, "w": 360, "h": 50}) is False


def test_classify_dialogue_route_black_plate(tmp_path: Path) -> None:
    import subprocess

    import cv2
    import numpy as np

    from media_ops import find_ffmpeg
    from sttn_inpaint import classify_dialogue_route

    ffmpeg = find_ffmpeg()
    src = tmp_path / "black_plate.mp4"
    # Synthesize a short clip: midtone scene + bottom black caption bar.
    frame = np.zeros((240, 400, 3), dtype=np.uint8)
    frame[:] = (90, 100, 110)
    frame[185:225, 40:360] = (6, 6, 6)
    frame[195:215, 80:320] = (245, 245, 245)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    raw = tmp_path / "raw.avi"
    wr = cv2.VideoWriter(str(raw), fourcc, 10.0, (400, 240))
    for _ in range(8):
        wr.write(frame)
    wr.release()
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(raw),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    seed = {"x": 10, "y": 160, "w": 380, "h": 70}
    info = classify_dialogue_route(src, seed, samples=5)
    assert info["ok"]
    assert info["route"] == "glyph_black"
    assert info["box"]["w"] < seed["w"]
    assert info["box"]["h"] <= seed["h"]


def test_classify_dialogue_route_textured_tiles(tmp_path: Path) -> None:
    import subprocess

    import cv2
    import numpy as np

    from media_ops import find_ffmpeg
    from sttn_inpaint import classify_dialogue_route

    ffmpeg = find_ffmpeg()
    src = tmp_path / "textured.mp4"
    rng = np.random.default_rng(0)
    frame = rng.integers(40, 200, size=(240, 400, 3), dtype=np.uint8)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    raw = tmp_path / "raw_tex.avi"
    wr = cv2.VideoWriter(str(raw), fourcc, 10.0, (400, 240))
    for _ in range(8):
        wr.write(frame)
    wr.release()
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(raw),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    info = classify_dialogue_route(
        src, {"x": 20, "y": 170, "w": 360, "h": 50}, samples=5
    )
    assert info["ok"]
    assert info["route"] == "tiles"


def test_find_black_caption_box_tightens_centered_plate() -> None:
    import numpy as np

    from sttn_inpaint import find_black_caption_box

    frame = np.zeros((240, 400, 3), dtype=np.uint8)
    frame[:] = (90, 100, 110)
    # Oversized seed covers scene + a smaller centered black plate.
    frame[185:225, 80:320] = (6, 6, 6)
    frame[195:215, 110:290] = (245, 245, 245)
    seed = {"x": 10, "y": 160, "w": 380, "h": 70}
    found = find_black_caption_box(frame, seed)
    assert found is not None
    assert found["x"] >= 70
    assert found["x"] + found["w"] <= 330
    assert found["h"] >= 20
    assert found["w"] < seed["w"]


def test_erase_glyphs_on_black_bar_clears_puttext() -> None:
    import cv2
    import numpy as np

    from sttn_inpaint import erase_glyphs_on_black_bar

    frame = np.zeros((240, 400, 3), dtype=np.uint8)
    frame[:] = (90, 100, 110)
    frame[180:230, 40:360] = (8, 8, 8)
    cv2.putText(
        frame,
        "HELLO",
        (70, 215),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.4,
        (245, 245, 245),
        3,
        cv2.LINE_AA,
    )
    out, n = erase_glyphs_on_black_bar(frame, {"x": 20, "y": 170, "w": 360, "h": 60})
    assert n > 200
    # Plate should be solid black; scene midtones untouched.
    assert int(out[200, 200].max()) < 40
    assert int(out[100, 200].mean()) > 50


def test_encode_black_bar_crop_removes_bottom_band(tmp_path: Path) -> None:
    import subprocess

    import cv2
    import numpy as np

    from media_ops import find_ffmpeg
    from sttn_inpaint import encode_black_bar_crop

    ffmpeg = find_ffmpeg()
    src = tmp_path / "src.mp4"
    # 64x48, bottom 12 rows black with white mark.
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x406080:s=64x48:r=10:d=0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    dest = tmp_path / "crop.mp4"
    stats = encode_black_bar_crop(
        src, dest, {"x": 0, "y": 36, "w": 64, "h": 12}, out_w=64, out_h=48
    )
    assert stats["engine"] == "crop"
    assert dest.is_file()
    cap = cv2.VideoCapture(str(dest))
    ok, fr = cap.read()
    cap.release()
    assert ok and fr is not None
    assert fr.shape[0] == 48 and fr.shape[1] == 64
    # Bottom of output should no longer be the pure black band (zoomed content).
    assert float(np.mean(fr[-6:, :, :])) > 20.0


def test_ffmpeg_writing_mp4_is_playable_before_close(tmp_path: Path) -> None:
    """Fragmented writing temps must open mid-stream (no 'moov atom not found')."""
    import subprocess
    import time

    import numpy as np

    from media_ops import find_ffmpeg
    from sttn_inpaint import _WRITE_MOVFLAGS, _finalize_writing_mp4, _open_ffmpeg_writer

    assert "frag_keyframe" in _WRITE_MOVFLAGS
    assert "empty_moov" in _WRITE_MOVFLAGS

    ffmpeg = find_ffmpeg()
    src = tmp_path / "src.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x48:r=10:d=0.3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    writing = tmp_path / "_clean_sttn_writing.mp4"
    dest = tmp_path / "clean.mp4"
    fps = 10.0
    # Writer uses gop≈fps (min 12); need ~2 GOPs before the first media fragment sticks.
    gop = max(12, min(60, int(round(fps))))
    proc = _open_ffmpeg_writer(src, writing, 64, 48, fps)
    assert proc.stdin is not None
    frame = np.zeros((48, 64, 3), dtype=np.uint8)
    frame[:] = (40, 80, 120)
    for i in range(gop * 2 + 4):
        proc.stdin.write(np.ascontiguousarray(frame).tobytes())
        proc.stdin.flush()
        if i == gop * 2:
            time.sleep(0.5)
    time.sleep(0.4)
    probe = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(writing), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr
    for _ in range(2):
        proc.stdin.write(np.ascontiguousarray(frame).tobytes())
    proc.stdin.close()
    err = proc.communicate(timeout=60)[1]
    assert proc.returncode == 0, (err or b"").decode("utf-8", errors="replace")[:300]
    _finalize_writing_mp4(writing, dest)
    assert dest.is_file() and dest.stat().st_size > 800
    assert not writing.is_file() or writing.resolve() == dest.resolve()
