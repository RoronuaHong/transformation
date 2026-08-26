from __future__ import annotations

from pathlib import Path

from job_layout import list_locale_srts, locale_srt_path, reorganize_job_dir


def test_reorganize_job_dir_classifies_and_drops_dupes(tmp_path: Path) -> None:
    (tmp_path / "full_16k.wav").write_bytes(b"RIFF")
    (tmp_path / "source.mp4").write_bytes(b"mp4")
    (tmp_path / "fetch_meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "full_16k_sync_meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "full_16k_en.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
    (tmp_path / "full_16k_en.txt").write_text("dup", encoding="utf-8")
    (tmp_path / "full_16k_en_fixed.srt").write_text("fixed", encoding="utf-8")
    (tmp_path / "full_16k_src.srt").write_text("src", encoding="utf-8")
    (tmp_path / "full_16k_zh.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\n嗨\n", encoding="utf-8")
    (tmp_path / "full_16k_zh.txt").write_text("dup", encoding="utf-8")
    (tmp_path / "full_16k_summary.json").write_text(
        '{"title": "T", "source_lang": "en", "summary_lang": "en"}',
        encoding="utf-8",
    )

    stats = reorganize_job_dir(tmp_path)

    assert (tmp_path / "media" / "full_16k.wav").is_file()
    assert (tmp_path / "media" / "source.mp4").is_file()
    assert (tmp_path / "media" / "fetch_meta.json").is_file()
    assert (tmp_path / "media" / "sync_meta.json").is_file()
    assert (tmp_path / "subs" / "en.srt").is_file()
    assert (tmp_path / "subs" / "zh.srt").is_file()
    assert not (tmp_path / "full_16k_en.txt").exists()
    assert not (tmp_path / "full_16k_en_fixed.srt").exists()
    assert not (tmp_path / "full_16k_src.srt").exists()
    assert not (tmp_path / "subs" / "src.srt").exists()
    assert not (tmp_path / "full_16k_summary.json").exists()
    assert "Hi" in (tmp_path / "subs" / "en.srt").read_text(encoding="utf-8")
    assert stats["moved"]
    assert stats["deleted"]


def test_locale_srt_path_prefers_nested(tmp_path: Path) -> None:
    nested = tmp_path / "subs" / "en.srt"
    nested.parent.mkdir(parents=True)
    nested.write_text("nested", encoding="utf-8")
    (tmp_path / "full_16k_en.srt").write_text("flat", encoding="utf-8")
    assert locale_srt_path(tmp_path, "en").read_text(encoding="utf-8") == "nested"
    found = list_locale_srts(tmp_path)
    assert found["en"] == nested
    assert locale_srt_path(tmp_path, "ja").as_posix().endswith("subs/ja.srt")
