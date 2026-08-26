from __future__ import annotations

from api.try_service import entry_frame_opts, normalize_frame_opts


def test_entry_frame_opts_uses_global_when_not_overridden() -> None:
    global_opts = normalize_frame_opts(
        "auto",
        4.0,
        [{"start": 0, "end": 10}],
        gif_ranges=[{"start": 1, "end": 5}],
    )
    merged = entry_frame_opts(
        global_frames="auto",
        global_gif_sec=4.0,
        global_clips=[{"start": 0, "end": 10}],
        global_gif_ranges=[{"start": 1, "end": 5}],
        entry={"url": "https://example.com", "override": False},
    )
    assert merged == global_opts


def test_entry_frame_opts_applies_per_link_clips() -> None:
    merged = entry_frame_opts(
        global_frames="none",
        global_gif_sec=4.0,
        global_clips=[{"start": 0, "end": 10}],
        global_gif_ranges=[],
        entry={
            "url": "https://example.com",
            "override": True,
            "clips": [{"start": 20, "end": 40}],
        },
    )
    assert merged["frames"] == "none"
    assert merged["clips"] == [{"start": 20.0, "end": 40.0}]
    assert merged["gif_ranges"] == []
