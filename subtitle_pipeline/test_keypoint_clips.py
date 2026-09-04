"""Tests for notes key_points → clip ranges bridge."""

from __future__ import annotations

from pathlib import Path

from note_frames import keypoint_clip_ranges


def test_keypoint_clip_ranges_uses_stamped_times(tmp_path: Path) -> None:
    work = tmp_path / "job"
    notes = work / "notes" / "zh"
    notes.mkdir(parents=True)
    (notes / "summary.json").write_text(
        """
        {
          "one_liner": "t",
          "summary": "s",
          "key_points": [
            {"title": "A", "detail": "", "start_sec": 1.0, "end_sec": 4.0},
            {"title": "B", "detail": "", "start_sec": 10.0, "end_sec": 18.0}
          ]
        }
        """,
        encoding="utf-8",
    )
    clips = keypoint_clip_ranges(work, source_lang="zh")
    assert len(clips) == 2
    assert clips[0]["start"] == 1.0
    assert clips[0]["end"] == 4.0
    assert clips[1]["title"] == "B"


def test_keypoint_clip_ranges_aligns_via_srt(tmp_path: Path) -> None:
    work = tmp_path / "job2"
    (work / "notes" / "en").mkdir(parents=True)
    (work / "subs").mkdir(parents=True)
    (work / "notes" / "en" / "summary.json").write_text(
        """
        {
          "one_liner": "t",
          "summary": "s",
          "key_points": [
            {"title": "羽绒大衣", "detail": "keep warm"}
          ]
        }
        """,
        encoding="utf-8",
    )
    (work / "subs" / "en.srt").write_text(
        "1\n00:00:05,000 --> 00:00:08,000\n这件羽绒大衣很保暖\n\n",
        encoding="utf-8",
    )
    clips = keypoint_clip_ranges(work, source_lang="en")
    assert len(clips) >= 1
    assert clips[0]["end"] > clips[0]["start"]
