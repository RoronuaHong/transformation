"""Export-site helpers: source cue_clock vs remix overlay URLs."""
from __future__ import annotations

import json
from pathlib import Path

from discover.export_site import remix_overlay_public


def test_remix_overlay_public_none_without_video(tmp_path: Path) -> None:
    assert remix_overlay_public("slug", tmp_path) is None


def test_remix_overlay_public_reads_meta(tmp_path: Path) -> None:
    derived = tmp_path / "derived" / "demo-slug"
    derived.mkdir(parents=True)
    (derived / "remix.mp4").write_bytes(b"0" * 900)
    (derived / "remix.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHi\n", encoding="utf-8")
    (derived / "remix_cues.json").write_text(
        json.dumps(
            {
                "audio_clock": True,
                "intro_sec": 2.5,
                "source_clock": "clips",
                "cues": [],
            }
        ),
        encoding="utf-8",
    )
    out = remix_overlay_public("demo-slug", tmp_path)
    assert out is not None
    assert out["clock"] == "remix"
    assert out["video"] == "/derived/demo-slug/remix.mp4"
    assert out["vtt"].endswith("remix.vtt")
    assert out["cues"].endswith("remix_cues.json")
    assert out["intro_sec"] == 2.5
    assert out["source_clock"] == "clips"
    assert out["audio_clock"] is True
