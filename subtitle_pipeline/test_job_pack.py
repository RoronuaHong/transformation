"""Tests for Job Pack zip + safe path resolve."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_pack import build_job_pack_bytes, safe_resolve_under


def test_safe_resolve_rejects_traversal(tmp_path: Path) -> None:
    work = tmp_path / "pack"
    (work / "subs").mkdir(parents=True)
    (work / "subs" / "zh.srt").write_text("1\n", encoding="utf-8")
    hit = safe_resolve_under(work, "subs/zh.srt")
    assert hit.name == "zh.srt"
    with pytest.raises(ValueError):
        safe_resolve_under(work, "../outside.txt")
    with pytest.raises(ValueError):
        safe_resolve_under(work, "subs/../../etc/passwd")


def test_build_job_pack_includes_subs_notes_meta(tmp_path: Path) -> None:
    work = tmp_path / "youtube_abc"
    (work / "subs").mkdir(parents=True)
    (work / "notes" / "zh").mkdir(parents=True)
    (work / "media" / "enhance").mkdir(parents=True)
    (work / "subs" / "zh.srt").write_text("x", encoding="utf-8")
    (work / "notes" / "zh" / "summary.json").write_text('{"a":1}', encoding="utf-8")
    (work / "media" / "enhance" / "enhanced.mp4").write_bytes(b"0" * 1000)
    data = build_job_pack_bytes(work, pack_id="youtube_abc")
    assert data[:2] == b"PK"
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
    assert "meta.json" in names
    assert "subs/zh.srt" in names
    assert "notes/zh/summary.json" in names
    assert "media/enhance/enhanced.mp4" in names
