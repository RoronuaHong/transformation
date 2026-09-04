"""Native STTN tiles + text-only hole (not a downscaled caption bar)."""

from pathlib import Path

from sttn_inpaint import STTN_H, STTN_W, sttn_tiles, sttn_view, text_hole_for_tile


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
    assert meta["action"] == "sttn_cleanup"
    assert called.get("box")
    cleaned, report = strip_hardsubs(
        tmp_path, video=src, force=True, mode="band", engine="sttn", demosaic=False
    )
    assert report["action"] == "sttn_cleanup"
    assert cleaned.is_file()
