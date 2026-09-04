"""Unit tests for 3-point spot-check."""
from __future__ import annotations

from pathlib import Path

import pytest

from spot_check import (
    check_point_in_duration,
    pick_three_points,
    spot_check_segments,
    spot_check_work_dir,
)


def test_pick_three_points_labels() -> None:
    segs = [
        {"start": 0.1, "end": 1.0, "text": "a"},
        {"start": 2.0, "end": 3.0, "text": "b"},
        {"start": 4.0, "end": 5.0, "text": "c"},
        {"start": 6.0, "end": 7.0, "text": "d"},
        {"start": 8.0, "end": 9.0, "text": "e"},
    ]
    pts = pick_three_points(segs)
    assert [p for p, _ in pts] == ["start", "mid", "end"]
    assert pts[0][1]["text"] == "a"
    assert pts[1][1]["text"] == "c"
    assert pts[2][1]["text"] == "e"


def test_check_point_past_end_fails() -> None:
    row = check_point_in_duration(
        "end",
        {"start": 12.0, "end": 13.0, "text": "late"},
        duration=10.0,
    )
    assert row["status"] == "fail"


def test_spot_check_segments_pass() -> None:
    segs = [
        {"start": 0.5, "end": 1.0, "text": "a"},
        {"start": 5.0, "end": 5.5, "text": "b"},
        {"start": 9.0, "end": 9.5, "text": "c"},
    ]
    out = spot_check_segments(segs, duration=10.0, check_energy=False)
    assert not out["failures"]
    assert any(c["name"] == "spot-three-points" for c in out["checks"])


def test_spot_check_work_dir_smoke(tmp_path: Path) -> None:
    subs = tmp_path / "subs"
    media = tmp_path / "media"
    remix = media / "remix"
    subs.mkdir()
    remix.mkdir(parents=True)
    (subs / "en.srt").write_text(
        "1\n00:00:00,100 --> 00:00:01,000\nA\n\n"
        "2\n00:00:05,000 --> 00:00:06,000\nB\n\n"
        "3\n00:00:09,000 --> 00:00:09,500\nC\n",
        encoding="utf-8",
    )
    (media / "full_16k.wav").write_bytes(b"RIFF" + b"\x00" * 64)
    (remix / "remix_cues.json").write_text(
        '{"intro_sec":2.5,"cues":['
        '{"start":2.6,"end":3.0,"kind":"caption","text":"A"},'
        '{"start":5.0,"end":5.5,"kind":"caption","text":"B"},'
        '{"start":8.0,"end":8.5,"kind":"caption","text":"C"}'
        "]}",
        encoding="utf-8",
    )
    report = spot_check_work_dir(tmp_path, check_energy=False)
    assert not report["failures"]
    assert report["source"] is not None
    assert report["remix"] is not None
