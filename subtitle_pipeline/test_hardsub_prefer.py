"""Hardsub-OCR-before-ASR wiring (no GPU / no video required)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import discover.run_batch as rb


def test_hardsub_prefer_off_skips(tmp_path: Path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    (media / "source.mp4").write_bytes(b"fake")
    monkeypatch.setenv("VITUAL_HARDSUB_PREFER", "0")
    assert rb._try_hardsub_source_before_asr(tmp_path, source_lang="zh") is None


def test_hardsub_prefer_no_video(tmp_path: Path):
    assert rb._try_hardsub_source_before_asr(tmp_path, source_lang="zh") is None


def test_hardsub_gate_auto_strict_when_asr_on_hardsub(monkeypatch, tmp_path: Path):
    """Burned captions + ASR source → gate upgrades warn→strict and raises."""
    media = tmp_path / "media"
    subs = tmp_path / "subs"
    media.mkdir()
    subs.mkdir()
    (media / "source.mp4").write_bytes(b"fake")
    (subs / "zh.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n", encoding="utf-8"
    )
    monkeypatch.setenv("VITUAL_HARDSUB_GATE", "warn")

    def _fake_compare(video, srt, report_path, **kwargs):
        if report_path:
            Path(report_path).write_text("{}", encoding="utf-8")
        return {
            "ok": False,
            "skipped": False,
            "coverage": 0.25,
            "missing_in_srt": [{"text": "大家应该没有害怕我的理由吧"}],
            "drift": [],
        }

    monkeypatch.setattr("hardsub_compare.compare_job", _fake_compare, raising=False)
    import hardsub_compare

    monkeypatch.setattr(hardsub_compare, "compare_job", _fake_compare)

    raised = False
    try:
        rb._hardsub_gate(
            tmp_path,
            "full_16k",
            "zh",
            bvid=None,
            hardsub_source=False,
            hardsub_detected=True,
        )
    except RuntimeError as e:
        raised = True
        assert "hardsub gate failed" in str(e)
    assert raised


def test_hardsub_gate_warn_ok_when_ocr_source(monkeypatch, tmp_path: Path):
    media = tmp_path / "media"
    subs = tmp_path / "subs"
    media.mkdir()
    subs.mkdir()
    (media / "source.mp4").write_bytes(b"fake")
    (subs / "zh.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n大家应该没有害怕我的理由吧\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VITUAL_HARDSUB_GATE", "warn")

    def _fake_compare(video, srt, report_path, **kwargs):
        if report_path:
            Path(report_path).write_text("{}", encoding="utf-8")
        return {
            "ok": False,
            "skipped": False,
            "coverage": 0.9,
            "missing_in_srt": [{"text": "x"}],
            "drift": [],
        }

    import hardsub_compare

    monkeypatch.setattr(hardsub_compare, "compare_job", _fake_compare)
    # OCR source + warn → do not raise even if ok=false
    rep = rb._hardsub_gate(
        tmp_path,
        "full_16k",
        "zh",
        bvid=None,
        hardsub_source=True,
        hardsub_detected=True,
    )
    assert rep.get("ok") is False
