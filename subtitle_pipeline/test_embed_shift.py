"""Tests for embed_shift (S8) and intro snap helpers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_snap_duration_to_fps() -> None:
    from media_ops import snap_duration_to_fps

    assert snap_duration_to_fps(2.5) == pytest.approx(2.5)
    assert snap_duration_to_fps(2.51) == pytest.approx(2.5)
    assert snap_duration_to_fps(2.52) == pytest.approx(2.533333, abs=1e-5)
    assert snap_duration_to_fps(0) == 0.0


def test_resolve_embed_shift_ms_explicit(monkeypatch) -> None:
    from embed_shift import resolve_embed_shift_ms

    monkeypatch.delenv("VITUAL_EMBED_SHIFT_MS", raising=False)
    assert resolve_embed_shift_ms("youtube", explicit=250) == 250
    assert resolve_embed_shift_ms("youtube", explicit=0) == 0


def test_apply_embed_shift_updates_subs(tmp_path: Path) -> None:
    from embed_shift import apply_embed_shift

    subs = tmp_path / "subs"
    subs.mkdir()
    (subs / "en.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
        encoding="utf-8",
    )
    (subs / "zh.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n你好\n",
        encoding="utf-8",
    )
    result = apply_embed_shift(tmp_path, 200, platform="youtube")
    assert result["ms"] == 200
    assert "en.srt" in result["files"]
    text = (subs / "en.srt").read_text(encoding="utf-8")
    assert "00:00:01,200" in text
    meta = json.loads((tmp_path / "media" / "sync_meta.json").read_text(encoding="utf-8"))
    assert meta["sync_shift_ms"] == 200
    assert meta["reason"] == "embed_ads"
    assert meta["embed_shift_ms"] == 200
