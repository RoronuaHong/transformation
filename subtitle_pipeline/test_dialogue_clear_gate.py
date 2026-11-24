"""Dialogue clear-rate gate: classification + batch wiring (no GPU needed)."""
from __future__ import annotations

from pathlib import Path

import pytest

import dialogue_clear_gate as dcg
import discover.run_batch as rb


def test_classify_dialogue_branches():
    def st(bs, bc, dark_s=0.0, dark_c=0.0, mad=0.0, lap_s=9999.0, lap_c=None):
        return {
            "bright_s": bs,
            "bright_c": bc,
            "dark_s": dark_s,
            "dark_c": dark_c,
            "mad": mad,
            "lap_s": lap_s,
            "lap_c": lap_s if lap_c is None else lap_c,
        }

    assert dcg.classify_dialogue(st(0.0, 0.0)) == "no_caption"
    assert dcg.classify_dialogue(st(0.10, 0.01, dark_c=0.05)) == "cleared_ok"
    assert dcg.classify_dialogue(st(0.10, 0.01, dark_c=0.30)) == "cleared_dark_smear"
    assert dcg.classify_dialogue(st(0.10, 0.10, mad=2.0)) == "left_as_source"
    assert dcg.classify_dialogue(st(0.10, 0.08, mad=20.0)) == "caption_remains"
    assert dcg.classify_dialogue(st(0.10, 0.04, mad=20.0)) == "partial"
    # BV1P6 ~35s white shirt after wipe: bright_c stays high, glyph lap collapses.
    assert (
        dcg.classify_dialogue(
            st(0.112, 0.080, mad=23.0, dark_c=0.11, lap_s=5019.0, lap_c=42.0)
        )
        == "cleared_ok"
    )
    # Real leftover glyphs keep high lap_c → still caption_remains.
    assert (
        dcg.classify_dialogue(
            st(0.112, 0.080, mad=23.0, dark_c=0.05, lap_s=5019.0, lap_c=4200.0)
        )
        == "caption_remains"
    )


def test_fail_labels_definition():
    assert set(dcg.FAIL_LABELS) >= {
        "caption_remains", "left_as_source", "cleared_dark_smear"
    }


def test_audit_skips_without_boxes_or_clean(tmp_path: Path):
    src = tmp_path / "source.mp4"
    src.write_bytes(b"fake")
    rep = dcg.audit_dialogue_clear(src, tmp_path / "missing.mp4",
                                   [{"id": "d", "x": 0, "y": 0, "w": 10, "h": 10}])
    assert rep["skipped"] and rep["reason"] == "no_clean_video"
    rep2 = dcg.audit_dialogue_clear(src, src, [])
    assert rep2["skipped"] and rep2["reason"] == "no_dialogue_box"


def test_gate_off_skips(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("VITUAL_DIALOGUE_CLEAR_GATE", "off")
    rep = rb._dialogue_clear_gate(tmp_path, dehardsub_done=True)
    assert rep["skipped"] and rep["reason"] == "gate_disabled"


def test_gate_no_dehardsub_output_skips(tmp_path: Path):
    rep = rb._dialogue_clear_gate(tmp_path, dehardsub_done=True)
    assert rep["skipped"] and rep["reason"] == "no_dehardsub_output"


def test_gate_strict_raises_on_bad_clear_rate(monkeypatch, tmp_path: Path):
    media = tmp_path / "media"
    (media / "dehardsub").mkdir(parents=True)
    (media / "dehardsub" / "clean.mp4").write_bytes(b"fake")
    monkeypatch.setenv("VITUAL_DIALOGUE_CLEAR_GATE", "strict")
    monkeypatch.setattr(
        "dialogue_clear_gate.dialogue_clear_gate",
        lambda *a, **k: {
            "skipped": False, "verdict": "FAIL", "ok": False,
            "fail_ratio": 0.8, "clear_ratio": 0.2, "report": "x.json",
        },
        raising=False,
    )
    with pytest.raises(RuntimeError, match="dialogue clear gate failed"):
        rb._dialogue_clear_gate(tmp_path, dehardsub_done=True)


def test_gate_warn_does_not_raise(monkeypatch, tmp_path: Path):
    media = tmp_path / "media"
    (media / "dehardsub").mkdir(parents=True)
    (media / "dehardsub" / "clean.mp4").write_bytes(b"fake")
    monkeypatch.setenv("VITUAL_DIALOGUE_CLEAR_GATE", "warn")
    monkeypatch.setattr(
        "dialogue_clear_gate.dialogue_clear_gate",
        lambda *a, **k: {
            "skipped": False, "verdict": "FAIL", "ok": False,
            "fail_ratio": 0.8, "clear_ratio": 0.2, "report": "x.json",
        },
        raising=False,
    )
    rep = rb._dialogue_clear_gate(tmp_path, dehardsub_done=True)
    assert rep["ok"] is False
