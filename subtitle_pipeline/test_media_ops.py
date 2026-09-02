"""WF-04/05/06 ffmpeg helpers and stage wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from discover.run_batch import parse_stages
from media_ops import (
    MIN_CONCAT,
    REMIX_H,
    REMIX_W,
    MediaOpsError,
    ass_escape,
    compress_vf,
    concat_filter,
    enhance_vf,
    normalize_media_opts,
    vertical_pad_vf,
    wrap_title,
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
    assert "remix" not in parse_stages("all")
    assert parse_stages("fetch,enhance") == frozenset({"fetch", "enhance"})
    assert parse_stages("remix") == frozenset({"remix"})
    mixed_r = parse_stages("all,remix")
    assert "asr" in mixed_r and "remix" in mixed_r


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
    r = compose_try_stages(
        want_translate=False,
        want_notes=False,
        has_media=False,
        want_remix=True,
    )
    assert "remix" in r
    assert compose_try_stages(
        want_translate=True, want_notes=True, want_remix=True
    ) == "all,remix"


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


def test_wrap_title_and_ass_escape() -> None:
    lines = wrap_title("通过精确控制水温、淘洗方式和调味，可以做出口感软糯香甜的米饭。", width=16)
    assert 2 <= len(lines) <= 4
    assert all(len(x) <= 17 for x in lines)
    assert "\\N" not in wrap_title("短标题")[0]
    assert ass_escape("a{b}\\c") == r"a\{b\}\\c"


def test_vertical_pad_vf() -> None:
    vf = vertical_pad_vf()
    assert str(REMIX_W) in vf
    assert str(REMIX_H) in vf
    assert "pad=" in vf
    assert "force_original_aspect_ratio=decrease" in vf


def test_remix_vertical_ffmpeg(tmp_path: Path) -> None:
    import subprocess

    from media_ops import remix_vertical_notes

    media = tmp_path / "media"
    media.mkdir()
    _tiny_mp4(media / "source.mp4", seconds=1.0, color="green")
    notes = tmp_path / "notes" / "zh"
    notes.mkdir(parents=True)
    (notes / "summary.json").write_text(
        '{"title":"蒸米饭","one_liner":"温水浸泡，淘洗两遍。"}',
        encoding="utf-8",
    )
    dest = remix_vertical_notes(tmp_path)
    assert dest.is_file() and dest.stat().st_size > 800
    meta = media / "remix" / "remix_meta.json"
    assert meta.is_file()
    payload = meta.read_text(encoding="utf-8")
    assert "vertical_notes" in payload
    assert "温水浸泡" in payload
    ffmpeg = _ffmpeg()
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(dest)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    blob = (probe.stderr or "") + (probe.stdout or "")
    assert f"{REMIX_W}x{REMIX_H}" in blob


def test_caption_events_follow_clip_clock_not_intro(tmp_path: Path) -> None:
    from media_ops import caption_events_for_body, write_clips_meta

    media = tmp_path / "media"
    clips = media / "clips"
    clips.mkdir(parents=True)
    (tmp_path / "media" / "source.mp4").write_bytes(b"0" * 900)
    write_clips_meta(tmp_path, [(10.0, 20.0), (50.0, 60.0)])
    srt = tmp_path / "subs" / "zh.srt"
    srt.parent.mkdir(parents=True)
    srt.write_text(
        "1\n00:00:10,130 --> 00:00:12,830\n因为他们往里面加了几种特别的东西\n\n"
        "2\n00:00:50,000 --> 00:00:52,000\n淘洗大米也是非常有讲究的\n",
        encoding="utf-8",
    )
    events = caption_events_for_body(tmp_path, srt, body=media / "concat" / "concat.mp4")
    assert events[0][0] == pytest.approx(0.13)
    assert events[0][1] == pytest.approx(2.83)
    assert "因为他们" in events[0][3]
    assert events[1][0] == pytest.approx(10.0)
    assert events[1][1] == pytest.approx(12.0)
    full = caption_events_for_body(tmp_path, srt, body=media / "source.mp4")
    assert full[0][0] == pytest.approx(10.13)


def test_hardsub_crop_vf() -> None:
    from media_ops import hardsub_crop_vf, remix_body_vf

    crop = hardsub_crop_vf()
    assert "crop=" in crop
    vf = remix_body_vf(crop_hardsubs=True, ass_path=None)
    assert "crop=" in vf
    assert "pad=" in vf
    plain = remix_body_vf(crop_hardsubs=False, ass_path=None)
    assert "crop=" not in plain


def test_remix_burns_body_clock_then_prepends_intro(tmp_path: Path) -> None:
    from media_ops import remix_vertical_notes

    media = tmp_path / "media"
    media.mkdir()
    _tiny_mp4(media / "source.mp4", seconds=1.0, color="green")
    notes = tmp_path / "notes" / "zh"
    notes.mkdir(parents=True)
    (notes / "summary.json").write_text(
        '{"title":"蒸米饭","one_liner":"温水浸泡，淘洗两遍。"}',
        encoding="utf-8",
    )
    subs = tmp_path / "subs"
    subs.mkdir()
    (subs / "zh.srt").write_text(
        "1\n00:00:00,100 --> 00:00:00,500\nhello-cue\n",
        encoding="utf-8",
    )
    dest = remix_vertical_notes(tmp_path, media_opts={"remix_intro_sec": 2.5})
    assert dest.is_file()
    assert not (media / "remix" / "captions.ass").is_file()
    cues = json.loads((media / "remix" / "remix_cues.json").read_text(encoding="utf-8"))
    kinds = {row["kind"]: row for row in cues["cues"]}
    assert kinds["title"]["start"] == pytest.approx(0.0, abs=0.05)
    hello = next(row for row in cues["cues"] if row["kind"] == "caption")
    assert hello["text"] == "hello-cue"
    assert hello["start"] == pytest.approx(2.6, abs=0.08)
    vtt = (media / "remix" / "remix.vtt").read_text(encoding="utf-8")
    assert "hello-cue" in vtt
    meta = (media / "remix" / "remix_meta.json").read_text(encoding="utf-8")
    assert '"burn_subs": false' in meta
    assert '"overlay": true' in meta
    assert '"caption_clock": "source"' in meta


def test_run_postproc_remix(tmp_path: Path) -> None:
    from media_ops import run_postproc

    media = tmp_path / "media"
    media.mkdir()
    _tiny_mp4(media / "source.mp4", seconds=0.8)
    out = run_postproc(tmp_path, frozenset({"remix"}))
    assert "remix" in out and out["remix"].is_file()
    assert (media / "remix" / "remix_meta.json").is_file()
