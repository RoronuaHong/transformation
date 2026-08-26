from __future__ import annotations

from pathlib import Path

from segment_subs import enrich_segment_cues, find_cue_range, stamp_row_cues


def _write_srt(path: Path, lines: list[tuple[float, float, str]]) -> None:
    body: list[str] = []
    for i, (start, end, text) in enumerate(lines, start=1):
        body.append(str(i))
        body.append(f"00:00:{start:06.3f} --> 00:00:{end:06.3f}".replace(".", ","))
        body.append(text)
        body.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")


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

    row = stamp_row_cues({"title": "片段 1"}, meta)
    assert row["cue_start"] == 0
    assert row["cue_end"] == 2
    assert row["start_sec"] == 1.5
