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


def test_parse_stages_deblur() -> None:
    """deblur 阶段必须可被 parse_stages 解析并属于 POSTPROC_STAGES。"""
    assert parse_stages("deblur") == frozenset({"deblur"})
    assert "deblur" in parse_stages("all,deblur")
    assert "deblur" not in parse_stages("all")  # opt-in，不进 all
    assert "deblur" in parse_stages("dehardsub,deblur,enhance")


def test_normalize_media_opts_deblur() -> None:
    """锁定 deblur / deblur_engine 透传（此前 media_ops 完全没有该选项）。"""
    # 默认关闭、默认引擎 realesrgan
    base = normalize_media_opts({})
    assert base["deblur"] is False
    assert base["deblur_engine"] == "realesrgan"
    # 字符串真值
    assert normalize_media_opts({"deblur": "true"})["deblur"] is True
    assert normalize_media_opts({"deblur": "off"})["deblur"] is False
    # 合法引擎保留
    assert normalize_media_opts({"deblur_engine": "basicvsr++"})["deblur_engine"] == "basicvsr++"
    assert (
        normalize_media_opts({"deblur_engine": "REALESRGAN"})["deblur_engine"] == "realesrgan"
    )
    # 非法引擎 → 回落默认
    assert normalize_media_opts({"deblur_engine": "bogus"})["deblur_engine"] == "realesrgan"


def test_normalize_media_opts_demosaic_engine() -> None:
    """锁定 demosaic_engine 透传：真实管线必须能选中 codeformer/sttn。

    回归：此参数曾只在 run_multipass_cleanup 上存在，media_ops 从未透传，
    导致 CodeFormer 神经去马赛克在真实调用链中永远不可达。
    """
    # 默认 lama（通用马赛克）；非法值也回退 lama
    assert normalize_media_opts({})["dehardsub_demosaic_engine"] == "lama"
    # 合法值原样保留
    assert (
        normalize_media_opts({"dehardsub_demosaic_engine": "codeformer"})[
            "dehardsub_demosaic_engine"
        ]
        == "codeformer"
    )
    assert (
        normalize_media_opts({"dehardsub_demosaic_engine": "opencv"})[
            "dehardsub_demosaic_engine"
        ]
        == "opencv"
    )
    assert (
        normalize_media_opts({"dehardsub_demosaic_engine": "sttn"})[
            "dehardsub_demosaic_engine"
        ]
        == "sttn"
    )
    assert (
        normalize_media_opts({"dehardsub_demosaic_engine": "LAMA"})[
            "dehardsub_demosaic_engine"
        ]
        == "lama"
    )
    # 非法值 → 回退 lama
    assert (
        normalize_media_opts({"dehardsub_demosaic_engine": "bogus"})[
            "dehardsub_demosaic_engine"
        ]
        == "lama"
    )


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


def test_write_clips_meta_stores_probed_duration(tmp_path: Path, monkeypatch) -> None:
    from media_ops import clips_meta_path, write_clips_meta

    clips = tmp_path / "media" / "clips"
    clips.mkdir(parents=True)
    (clips / "range_00.mp4").write_bytes(b"x" * 900)
    (clips / "range_01.mp4").write_bytes(b"x" * 900)

    def fake_probe(path: Path) -> float | None:
        if path.name == "range_00.mp4":
            return 10.062
        if path.name == "range_01.mp4":
            return 10.041
        return None

    monkeypatch.setattr("media_ops.probe_duration_sec", fake_probe)
    write_clips_meta(tmp_path, [(10.0, 20.0), (50.0, 60.0)])
    meta = json.loads(clips_meta_path(tmp_path).read_text(encoding="utf-8"))
    assert meta["spans"][0]["duration"] == 10.062
    assert meta["spans"][1]["duration"] == 10.041


def test_clip_span_durations_prefers_meta(tmp_path: Path) -> None:
    from media_ops import clip_span_durations, write_clips_meta

    write_clips_meta(tmp_path, [(10.0, 20.0), (50.0, 60.0)])
    meta_path = tmp_path / "media" / "clips" / "clips_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["spans"][0]["duration"] = 10.06
    meta["spans"][1]["duration"] = 10.04
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    durs = clip_span_durations(tmp_path, [(10.0, 20.0), (50.0, 60.0)])
    assert durs == [10.06, 10.04]


def test_caption_events_use_meta_durations_at_stitch(tmp_path: Path) -> None:
    from media_ops import caption_events_for_body, write_clips_meta

    media = tmp_path / "media"
    clips = media / "clips"
    clips.mkdir(parents=True)
    srt = tmp_path / "subs" / "zh.srt"
    srt.parent.mkdir(parents=True)
    srt.write_text(
        "1\n00:00:50,000 --> 00:00:52,000\n淘洗大米也是非常有讲究的\n",
        encoding="utf-8",
    )
    write_clips_meta(tmp_path, [(10.0, 20.0), (50.0, 60.0)])
    meta_path = clips / "clips_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["spans"][0]["duration"] = 10.06
    meta["spans"][1]["duration"] = 10.04
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    events = caption_events_for_body(tmp_path, srt, body=media / "concat" / "concat.mp4")
    assert len(events) == 1
    assert events[0][0] == pytest.approx(10.06)
    assert events[0][1] == pytest.approx(12.06)


def test_scale_span_durations_fits_body() -> None:
    from media_ops import scale_span_durations

    assert scale_span_durations([10.0, 10.0], None) == [10.0, 10.0]
    assert scale_span_durations([10.0, 10.0], 20.0) == [10.0, 10.0]
    out = scale_span_durations([10.06, 10.04], 20.2)
    assert sum(out) == pytest.approx(20.2)
    assert out[0] == pytest.approx(10.06 * (20.2 / 20.1))


def test_intro_duration_in_remix_uses_concat_clock(tmp_path: Path, monkeypatch) -> None:
    from media_ops import intro_duration_in_remix

    remix = tmp_path / "remix.mp4"
    body = tmp_path / "body.mp4"
    remix.write_bytes(b"x")
    body.write_bytes(b"x")

    def fake_probe(path):
        return 22.64 if path.name == "remix.mp4" else 20.01

    monkeypatch.setattr("media_ops.probe_duration_sec", fake_probe)
    assert intro_duration_in_remix(remix, body, 2.53) == pytest.approx(2.63)


def test_parse_leading_silence_starts_at_zero() -> None:
    from media_ops import parse_leading_silence_sec

    log = (
        "[silencedetect @ 0x1] silence_start: 0\n"
        "[silencedetect @ 0x1] silence_end: 2.512 | silence_duration: 2.512\n"
    )
    assert parse_leading_silence_sec(log) == pytest.approx(2.512)
    assert parse_leading_silence_sec("silence_start: 1.2\nsilence_end: 3.0\n") is None
    assert parse_leading_silence_sec("") is None


def test_overlay_intro_prefers_leading_silence(tmp_path: Path, monkeypatch) -> None:
    from media_ops import overlay_intro_sec

    remix = tmp_path / "remix.mp4"
    body = tmp_path / "body.mp4"
    remix.write_bytes(b"x")
    body.write_bytes(b"x")
    monkeypatch.setattr("media_ops.probe_leading_silence_sec", lambda _p: 2.512)
    monkeypatch.setattr("media_ops.probe_stream_duration_sec", lambda _p, stream="a:0": 22.6)
    monkeypatch.setattr("media_ops.probe_duration_sec", lambda _p: 22.6)
    assert overlay_intro_sec(remix, body, 2.53, requested=2.5) == pytest.approx(2.512)


def test_overlay_intro_rejects_whole_file_silence(tmp_path: Path, monkeypatch) -> None:
    from media_ops import overlay_intro_sec

    remix = tmp_path / "remix.mp4"
    body = tmp_path / "body.mp4"
    remix.write_bytes(b"x")
    body.write_bytes(b"x")
    monkeypatch.setattr("media_ops.probe_leading_silence_sec", lambda _p: 22.6)
    monkeypatch.setattr("media_ops.probe_stream_duration_sec", lambda p, stream="a:0": 22.6 if p.name == "remix.mp4" else 20.1)
    monkeypatch.setattr("media_ops.probe_duration_sec", lambda p: 22.6 if p.name == "remix.mp4" else 20.1)
    assert overlay_intro_sec(remix, body, 2.53, requested=2.5) == pytest.approx(2.5)


def test_caption_events_scale_to_encoded_body(tmp_path: Path) -> None:
    from media_ops import caption_events_for_body, write_clips_meta

    media = tmp_path / "media"
    clips = media / "clips"
    clips.mkdir(parents=True)
    srt = tmp_path / "subs" / "zh.srt"
    srt.parent.mkdir(parents=True)
    srt.write_text(
        "1\n00:00:50,000 --> 00:00:52,000\n淘洗大米也是非常有讲究的\n",
        encoding="utf-8",
    )
    write_clips_meta(tmp_path, [(10.0, 20.0), (50.0, 60.0)])
    meta_path = clips / "clips_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["spans"][0]["duration"] = 10.06
    meta["spans"][1]["duration"] = 10.04
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    events = caption_events_for_body(
        tmp_path,
        srt,
        body=media / "concat" / "concat.mp4",
        timeline_dur=20.2,
    )
    scale = 20.2 / 20.1
    assert events[0][0] == pytest.approx(10.06 * scale)


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


def test_caption_events_honor_explicit_clips_clock(tmp_path: Path) -> None:
    from media_ops import caption_events_for_body, write_clips_meta

    media = tmp_path / "media"
    clips = media / "clips"
    clips.mkdir(parents=True)
    write_clips_meta(tmp_path, [(0.0, 10.0), (10.0, 20.0)])
    srt = tmp_path / "subs" / "en.srt"
    srt.parent.mkdir(parents=True)
    srt.write_text(
        "1\n00:00:00,100 --> 00:00:02,000\nA\n\n"
        "2\n00:00:15,000 --> 00:00:16,000\nB\n\n"
        "3\n00:01:10,000 --> 00:01:12,000\nC\n",
        encoding="utf-8",
    )
    # Remuxed name would look like source clock; explicit clock=clips must win.
    remux = media / "remix" / "_src_av.mp4"
    remux.parent.mkdir(parents=True)
    remux.write_bytes(b"0" * 900)
    events = caption_events_for_body(
        tmp_path, srt, body=remux, shift=2.5, clock="clips"
    )
    assert len(events) == 2
    assert events[0][0] == pytest.approx(2.6)
    assert events[1][0] == pytest.approx(2.5 + 15.0)
    assert all(e[3] != "C" for e in events)


def test_clamp_overlay_cues_drops_past_end() -> None:
    from media_ops import clamp_overlay_cues

    cues = [
        {"start": 0.0, "end": 2.0, "kind": "title", "text": "T"},
        {"start": 2.0, "end": 5.0, "kind": "caption", "text": "A"},
        {"start": 9.5, "end": 12.0, "kind": "caption", "text": "B"},
        {"start": 10.2, "end": 11.0, "kind": "caption", "text": "late"},
    ]
    out = clamp_overlay_cues(cues, 10.0)
    assert [c["text"] for c in out] == ["T", "A", "B"]
    assert out[-1]["end"] == pytest.approx(10.12)


def test_src_av_sidecar_keeps_clips_clock(tmp_path: Path) -> None:
    from media_ops import caption_clock_for_body, write_clips_meta

    media = tmp_path / "media"
    remix = media / "remix"
    remix.mkdir(parents=True)
    write_clips_meta(tmp_path, [(0.0, 5.0), (5.0, 10.0)])
    remux = remix / "_src_av.mp4"
    remux.write_bytes(b"0" * 64)
    (remix / "_src_av_meta.json").write_text(
        '{"clock":"clips","src":"_clips.mp4"}', encoding="utf-8"
    )
    assert caption_clock_for_body(tmp_path, remux) == "clips"


def test_purge_stale_locale_remixes(tmp_path: Path) -> None:
    from media_ops import purge_stale_locale_remixes

    remix = tmp_path / "remix"
    remix.mkdir()
    (remix / "remix.mp4").write_bytes(b"0" * 64)
    (remix / "remix.vtt").write_text("WEBVTT\n", encoding="utf-8")
    (remix / "remix_cues.json").write_text("{}", encoding="utf-8")
    (remix / "remix_meta.json").write_text("{}", encoding="utf-8")
    (remix / "_body.mp4").write_bytes(b"0" * 32)
    (remix / "remix_zh.mp4").write_bytes(b"0" * 32)
    (remix / "remix_ja_cues.json").write_text("{}", encoding="utf-8")
    (remix / "remix_ko_meta.json").write_text("{}", encoding="utf-8")
    removed = purge_stale_locale_remixes(remix)
    assert set(removed) == {"remix_zh.mp4", "remix_ja_cues.json", "remix_ko_meta.json"}
    assert (remix / "remix.mp4").is_file()
    assert (remix / "remix_cues.json").is_file()
    assert (remix / "_body.mp4").is_file()
    assert not (remix / "remix_zh.mp4").is_file()


def test_write_media_status(tmp_path: Path) -> None:
    from media_ops import write_media_status

    media = tmp_path / "media"
    media.mkdir()
    path = write_media_status(tmp_path, {"postproc": "ok", "remix": "ok"})
    assert path.name == "media_status.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["remix"] == "ok"
    assert "updated_at" in data


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
    intro = float(cues["intro_sec"])
    assert cues.get("audio_clock") is True
    assert intro == pytest.approx(2.5, abs=0.25)
    assert intro == pytest.approx(float(kinds["title"]["end"]), abs=0.02)
    assert hello["start"] == pytest.approx(intro + 0.1, abs=0.05)
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


def _tiny_mp4_with_bottom_bar(path: Path, *, seconds: float = 1.0) -> None:
    from media_ops import _run_ffmpeg, find_ffmpeg

    ffmpeg = find_ffmpeg()
    _run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x224466:s=640x360:d={seconds}:r=25,"
            f"drawbox=x=0:y=ih*0.82:w=iw:h=ih*0.18:color=white:t=fill",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=440:d={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ]
    )


def test_detect_and_strip_hardsubs(tmp_path: Path) -> None:
    from media_ops import detect_hardsubs, locate_hardsub_box_band, probe_video_wh, strip_hardsubs

    media = tmp_path / "media"
    media.mkdir()
    src = media / "source.mp4"
    _tiny_mp4_with_bottom_bar(src, seconds=1.2)
    report = detect_hardsubs(src, samples=4)
    assert report["samples"] >= 1
    assert report["detected"] is True
    src_wh = probe_video_wh(src)
    located = locate_hardsub_box_band(src, src_wh[0], src_wh[1])
    assert located is not None
    assert located["box"]["y"] > src_wh[1] * 0.65
    cleaned, meta = strip_hardsubs(
        tmp_path, video=src, force=False, ratio=0.14, mode="band", engine="opencv"
    )
    assert str(meta["action"]).startswith(("delogo_", "inpaint_", "multipass"))
    assert cleaned.name == "clean.mp4"
    assert cleaned.is_file()
    out_wh = probe_video_wh(cleaned)
    assert src_wh is not None and out_wh is not None
    assert out_wh == src_wh
    assert meta.get("out_size") == f"{src_wh[0]}x{src_wh[1]}"
    _skipped, meta2 = strip_hardsubs(
        tmp_path, video=cleaned, force=False, ratio=0.14, mode="band", engine="opencv"
    )
    assert meta2["action"] == "skip" or str(meta2["action"]).startswith(
        ("delogo_", "inpaint_", "multipass")
    )
    assert _skipped.is_file()


def test_hardsub_fill_keeps_frame(tmp_path: Path) -> None:
    from media_ops import hardsub_delogo_vf, hardsub_fill_filter, probe_video_wh, strip_hardsubs

    assert "delogo=" in hardsub_delogo_vf(480, 360, 0.14)
    assert "overlay=" in hardsub_fill_filter(480, 360, 0.14)
    media = tmp_path / "media"
    media.mkdir()
    src = media / "source.mp4"
    _tiny_mp4_with_bottom_bar(src, seconds=0.8)
    src_wh = probe_video_wh(src)
    cleaned, meta = strip_hardsubs(
        tmp_path, video=src, force=True, mode="band", ratio=0.14, engine="opencv"
    )
    assert str(meta["action"]).startswith(("delogo_", "inpaint_", "multipass"))
    assert probe_video_wh(cleaned) == src_wh
    assert meta["box"]["w"] < src_wh[0]


def test_run_postproc_dehardsub(tmp_path: Path) -> None:
    from media_ops import probe_video_wh, run_postproc

    media = tmp_path / "media"
    media.mkdir()
    src = media / "source.mp4"
    _tiny_mp4_with_bottom_bar(src, seconds=1.0)
    src_wh = probe_video_wh(src)
    out = run_postproc(
        tmp_path,
        frozenset({"dehardsub"}),
        media_opts={"dehardsub_mode": "band", "dehardsub_engine": "opencv"},
    )
    assert "dehardsub" in out
    meta_path = media / "dehardsub" / "dehardsub_meta.json"
    assert meta_path.is_file()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert str(meta["action"]).startswith(("delogo_", "inpaint_", "multipass"))
    assert probe_video_wh(out["dehardsub"]) == src_wh


def test_multipass_cleanup_hardsub_and_mosaic(tmp_path: Path) -> None:
    import cv2
    import numpy as np
    from media_ops import probe_video_wh, strip_hardsubs
    from visual_cleanup import mosaic_block_score, run_multipass_cleanup

    media = tmp_path / "media"
    media.mkdir()
    src = media / "source.mp4"
    # Synthetic: white hardsub bar + mosaic block.
    w, h, fps, n = 320, 240, 10, 12
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(src), fourcc, fps, (w, h))
    assert writer.isOpened()
    for i in range(n):
        frame = np.full((h, w, 3), 40, dtype=np.uint8)
        frame[40:140, 40:200] = (30, 90, 40)
        # mosaic 16px tiles
        for y in range(150, 214, 16):
            for x in range(40, 168, 16):
                c = 80 + ((x // 16 + y // 16) % 2) * 70
                frame[y : y + 16, x : x + 16] = (c, c, c)
        # hardsub glyphs
        cv2.putText(
            frame,
            "HELLO SUB",
            (60, 210),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        writer.write(frame)
    writer.release()

    # score a real mosaic patch
    patch = np.zeros((64, 64), dtype=np.uint8)
    for y in range(0, 64, 16):
        for x in range(0, 64, 16):
            patch[y : y + 16, x : x + 16] = 60 + ((x + y) // 16 % 2) * 90
    assert mosaic_block_score(patch, block=16) > 0.15

    dest = media / "dehardsub" / "clean.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = run_multipass_cleanup(
        src,
        dest,
        work_dir=tmp_path,
        locate_mode="band",
        max_passes=2,
        demosaic=True,
        dehardsub=True,
        residual_ok=0.05,
        engine="opencv",
    )
    assert meta["action"] == "multipass_cleanup"
    assert meta["pass_count"] >= 1
    assert dest.is_file()
    assert probe_video_wh(dest) == probe_video_wh(src)

    cleaned, report = strip_hardsubs(
        tmp_path,
        video=src,
        force=True,
        mode="auto",
        passes=2,
        demosaic=True,
        engine="opencv",
    )
    assert report["action"] == "multipass_cleanup"
    assert cleaned.is_file()
    assert int(report.get("pass_count") or 0) >= 1
