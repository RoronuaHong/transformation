"""Tests for multi-slice SRT merge (S1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from media_ops import write_clips_meta
from merge_slices import discover_clip_parts, merge_slice_parts, merge_work_dir_clips
from pipeline import parse_srt
from sync_utils import merge_offset_segments


def test_merge_offset_segments_applies_slice_starts() -> None:
    a = [{"start": 0.1, "end": 1.0, "text": "A"}]
    b = [{"start": 0.2, "end": 0.8, "text": "B"}]
    out = merge_offset_segments([(a, 10.0), (b, 50.0)])
    assert len(out) == 2
    assert out[0]["start"] == pytest.approx(10.1)
    assert out[0]["text"] == "A"
    assert out[1]["start"] == pytest.approx(50.2)
    assert out[1]["text"] == "B"


def test_merge_offset_fixes_overlap() -> None:
    a = [{"start": 0.0, "end": 2.0, "text": "A"}]
    b = [{"start": 0.0, "end": 1.0, "text": "B"}]
    out = merge_offset_segments([(a, 10.0), (b, 11.0)])
    assert out[0]["end"] == pytest.approx(11.0)
    assert out[1]["start"] == pytest.approx(11.0)


def test_merge_offset_same_start_does_not_remain_overlapping() -> None:
    a = [{"start": 0.0, "end": 1.0, "text": "A"}]
    b = [{"start": 0.0, "end": 1.0, "text": "B"}]
    out = merge_offset_segments([(a, 0.0), (b, 0.0)])
    assert len(out) == 1
    assert out[0]["text"] == "A"
    assert out[0]["end"] == pytest.approx(1.0)


def test_merge_slice_parts_writes_meta(tmp_path: Path) -> None:
    a = tmp_path / "a.srt"
    b = tmp_path / "b.srt"
    a.write_text("1\n00:00:00,100 --> 00:00:01,000\nA\n", encoding="utf-8")
    b.write_text("1\n00:00:00,200 --> 00:00:00,800\nB\n", encoding="utf-8")
    dest = tmp_path / "full.srt"
    result = merge_slice_parts([(a, 10.0), (b, 50.0)], dest, clock="source")
    assert result["cue_count"] == 2
    segs = parse_srt(dest)
    assert segs[0]["start"] == pytest.approx(10.1)
    assert segs[1]["start"] == pytest.approx(50.2)
    meta = json.loads(Path(result["meta"]).read_text(encoding="utf-8"))
    assert meta["merged_from_slices"] is True
    assert len(meta["parts"]) == 2


def test_merge_work_dir_clips_source_clock(tmp_path: Path) -> None:
    media = tmp_path / "media" / "clips"
    media.mkdir(parents=True)
    write_clips_meta(tmp_path, [(10.0, 20.0), (50.0, 60.0)])
    (media / "range_00.srt").write_text(
        "1\n00:00:00,500 --> 00:00:01,500\nfirst\n", encoding="utf-8"
    )
    (media / "range_01" / "en.srt").parent.mkdir(parents=True)
    (media / "range_01" / "en.srt").write_text(
        "1\n00:00:00,100 --> 00:00:00,900\nsecond\n", encoding="utf-8"
    )
    parts = discover_clip_parts(tmp_path, lang="en", clock="source")
    assert len(parts) == 2
    assert parts[0]["slice_start_sec"] == pytest.approx(10.0)
    assert parts[1]["slice_start_sec"] == pytest.approx(50.0)

    result = merge_work_dir_clips(tmp_path, lang="en", clock="source")
    segs = parse_srt(Path(result["dest"]))
    assert segs[0]["text"] == "first"
    assert segs[0]["start"] == pytest.approx(10.5)
    assert segs[1]["start"] == pytest.approx(50.1)
    media_meta = tmp_path / "media" / "sync_meta.json"
    assert media_meta.is_file()
    assert json.loads(media_meta.read_text(encoding="utf-8"))["merged_from_slices"] is True


def test_merge_work_dir_clips_concat_clock(tmp_path: Path) -> None:
    media = tmp_path / "media" / "clips"
    media.mkdir(parents=True)
    write_clips_meta(tmp_path, [(10.0, 20.0), (50.0, 55.0)])
    (media / "range_00.srt").write_text(
        "1\n00:00:00,100 --> 00:00:01,000\nA\n", encoding="utf-8"
    )
    (media / "range_01.srt").write_text(
        "1\n00:00:00,200 --> 00:00:00,800\nB\n", encoding="utf-8"
    )
    result = merge_work_dir_clips(tmp_path, lang="en", clock="clips")
    segs = parse_srt(Path(result["dest"]))
    # first span duration from meta end-start = 10s when no probed mp4
    assert segs[0]["start"] == pytest.approx(0.1)
    assert segs[1]["start"] == pytest.approx(10.2)
