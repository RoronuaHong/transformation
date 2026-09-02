from __future__ import annotations

from pathlib import Path

import pytest

from segment_subs import (
    enrich_segment_cues,
    find_cue_range,
    remap_cues_to_spans,
    stamp_row_cues,
)


def _write_srt(path: Path, lines: list[tuple[float, float, str]]) -> None:
    body: list[str] = []
    for i, (start, end, text) in enumerate(lines, start=1):
        body.append(str(i))
        body.append(f"00:00:{start:06.3f} --> 00:00:{end:06.3f}".replace(".", ","))
        body.append(text)
        body.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")


def test_remap_cues_to_spans_two_clips() -> None:
    segs = [
        {"start": 0.31, "end": 1.65, "text": "大家好"},
        {"start": 10.13, "end": 12.83, "text": "因为他们往里面加了几种特别的东西"},
        {"start": 50.0, "end": 52.0, "text": "淘洗大米也是非常有讲究的"},
        {"start": 19.5, "end": 21.0, "text": "跨过第一段结尾"},
    ]
    out = remap_cues_to_spans(segs, [(10.0, 20.0), (50.0, 60.0)])
    by_text = {row["text"]: row for row in out}
    assert "大家好" not in by_text
    assert by_text["因为他们往里面加了几种特别的东西"]["start"] == pytest.approx(0.13)
    assert by_text["因为他们往里面加了几种特别的东西"]["end"] == pytest.approx(2.83)
    assert by_text["淘洗大米也是非常有讲究的"]["start"] == pytest.approx(10.0)
    assert by_text["淘洗大米也是非常有讲究的"]["end"] == pytest.approx(12.0)
    assert by_text["跨过第一段结尾"]["start"] == pytest.approx(9.5)
    assert by_text["跨过第一段结尾"]["end"] == pytest.approx(10.0)


def test_remap_cues_uses_probed_clip_durations() -> None:
    segs = [
        {"start": 50.0, "end": 52.0, "text": "第二段"},
    ]
    out = remap_cues_to_spans(
        segs,
        [(10.0, 20.0), (50.0, 60.0)],
        durations=[10.06, 10.04],
    )
    assert out[0]["start"] == pytest.approx(10.06)
    assert out[0]["end"] == pytest.approx(12.06)


def test_find_cue_range_overlaps() -> None:
    segs = [
        {"start": 0.0, "end": 2.0, "text": "a"},
        {"start": 2.0, "end": 5.0, "text": "b"},
        {"start": 5.0, "end": 8.0, "text": "c"},
    ]
    assert find_cue_range(segs, 1.0, 6.0) == (0, 2)
    assert find_cue_range(segs, 9.0, 10.0) is None


def test_enrich_segment_cues_writes_multilingual_subs(tmp_path: Path) -> None:
    work = tmp_path / "job"
    subs = work / "subs"
    _write_srt(
        subs / "zh.srt",
        [(0.0, 2.0, "你好"), (2.0, 5.0, "世界"), (5.0, 8.0, "再见")],
    )
    _write_srt(
        subs / "en.srt",
        [(0.0, 2.0, "hello"), (2.0, 5.0, "world"), (5.0, 8.0, "bye")],
    )

    meta = enrich_segment_cues(
        work,
        segment_id="range_00",
        start_sec=1.5,
        end_sec=6.0,
    )
    assert meta is not None
    assert meta["cue_start"] == 0
    assert meta["cue_end"] == 2
    assert (work / "media" / "segments" / "range_00" / "subs" / "zh.srt").is_file()
    assert (work / "media" / "segments" / "range_00" / "subs" / "en.srt").is_file()
    zh_text = (work / "media" / "segments" / "range_00" / "subs" / "zh.srt").read_text(
        encoding="utf-8"
    )
    assert "你好" in zh_text
    assert "世界" in zh_text
    assert "再见" in zh_text
    assert "00:00:00,000 --> 00:00:00,500" in zh_text
    assert "00:00:00,500 --> 00:00:03,500" in zh_text
    assert "00:00:03,500 --> 00:00:04,500" in zh_text

    row = stamp_row_cues({"title": "片段 1"}, meta)
    assert row["cue_start"] == 0
    assert row["cue_end"] == 2
    assert row["start_sec"] == 1.5
