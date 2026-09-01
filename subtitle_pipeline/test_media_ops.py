"""WF-04/05/06 ffmpeg helpers and stage wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

from discover.run_batch import parse_stages
from media_ops import (
    MIN_CONCAT,
    MediaOpsError,
    compress_vf,
    concat_filter,
    enhance_vf,
    normalize_media_opts,
)


def test_enhance_vf_strengths() -> None:
    light = enhance_vf("light")
    medium = enhance_vf("medium")
    strong = enhance_vf("strong")
    assert "unsharp" in light
    assert "unsharp" in medium
    assert "lanczos" in strong
    assert "unsharp" in strong
    assert "scale=-2:trunc" in medium


def test_compress_vf_keep_height() -> None:
    assert compress_vf(0) is None
    assert "720" in (compress_vf(720) or "")


def test_concat_filter_requires_two() -> None:
    with pytest.raises(MediaOpsError):
        concat_filter(1)
    fc = concat_filter(2, height=720)
    assert "concat=n=2:v=1:a=1" in fc
    assert "[0:v]" in fc and "[1:a]" in fc


def test_normalize_media_opts_clamps() -> None:
    opts = normalize_media_opts(
        {"enhance_strength": "nuke", "compress_height": 999, "compress_crf": 99}
    )
    assert opts["enhance_strength"] == "medium"
    assert opts["compress_height"] == 720
    assert opts["compress_crf"] == 32
    keep = normalize_media_opts({"compress_height": 0, "enhance_strength": "strong"})
    assert keep["compress_height"] == 0
    assert keep["enhance_strength"] == "strong"


def test_parse_stages_postproc() -> None:
    assert "enhance" in parse_stages("enhance")
    assert parse_stages("compress") == frozenset({"compress"})
    assert parse_stages("concat") == frozenset({"concat"})
    mixed = parse_stages("all,enhance,compress")
    assert "asr" in mixed
    assert "enhance" in mixed
    assert "compress" in mixed
    assert "enhance" not in parse_stages("all")
    assert parse_stages("fetch,enhance") == frozenset({"fetch", "enhance"})


def test_compose_try_stages_postproc() -> None:
    from api.try_service import compose_try_stages

    assert compose_try_stages(want_translate=True, want_notes=True) == "all"
    assert compose_try_stages(
        want_translate=True, want_notes=True, want_enhance=True
    ) == "all,enhance"
    s = compose_try_stages(
        want_translate=False,
        want_notes=False,
        has_media=False,
        want_compress=True,
    )
    assert "compress" in s
    assert "notes" not in s.split(",")
    c = compose_try_stages(
        want_translate=False,
        want_notes=False,
        want_concat=True,
    )
    assert "concat" in c
    assert "clips" in c


def _ffmpeg() -> str:
    try:
        from fetch_media import find_ffmpeg

        return find_ffmpeg()
    except FileNotFoundError:
        pytest.skip("ffmpeg not found")


def _tiny_mp4(path: Path, *, seconds: float = 0.6, color: str = "blue") -> Path:
    import subprocess

    ffmpeg = _ffmpeg()
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=320x240:d={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}",
        "-shortest",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        pytest.skip(f"could not mint test mp4: {(r.stderr or '')[-400:]}")
    return path


def test_enhance_compress_concat_ffmpeg(tmp_path: Path) -> None:
    from media_ops import compress_video, concat_videos, enhance_video

    a = _tiny_mp4(tmp_path / "a.mp4", color="blue")
    b = _tiny_mp4(tmp_path / "b.mp4", color="red")
    enhanced = enhance_video(a, tmp_path / "enhanced.mp4", strength="light")
    assert enhanced.is_file() and enhanced.stat().st_size > 800
    compressed = compress_video(enhanced, tmp_path / "compressed.mp4", max_height=240, crf=30)
    assert compressed.is_file() and compressed.stat().st_size > 800
    cat = concat_videos([a, b], tmp_path / "cat.mp4", height=240)
    assert cat.is_file() and cat.stat().st_size > 800
    with pytest.raises(MediaOpsError):
        concat_videos([a], tmp_path / "one.mp4")
    assert MIN_CONCAT == 2


def test_run_postproc_chain(tmp_path: Path) -> None:
    from media_ops import run_postproc

    media = tmp_path / "media"
    media.mkdir()
    src = _tiny_mp4(media / "source.mp4", seconds=0.8)
    assert src.is_file()
    out = run_postproc(
        tmp_path,
        frozenset({"enhance", "compress"}),
        media_opts={"enhance_strength": "light", "compress_height": 240, "compress_crf": 30},
    )
    assert "enhance" in out and out["enhance"].is_file()
    assert "compress" in out and out["compress"].is_file()
    (tmp_path / "enhance_meta.json") if False else None
    assert (media / "enhance" / "enhance_meta.json").is_file()
    assert (media / "compress" / "compress_meta.json").is_file()
