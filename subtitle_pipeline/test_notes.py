from __future__ import annotations

from pathlib import Path

from pipeline import notes_headings, normalize_key_points, write_summary


def test_notes_headings_zh_en_fallback() -> None:
    assert notes_headings("zh")["focuses"] == "重点"
    assert notes_headings("zh")["key_points"] == "要点"
    assert notes_headings("zh")["hard_points"] == "难点"
    assert notes_headings("en")["focuses"] == "Core points"
    assert notes_headings("xx")["key_points"] == "Key points"


def test_write_summary_is_study_notes(tmp_path: Path) -> None:
    data = {
        "title": "醋的十个用法",
        "source_lang": "en",
        "summary_lang": "zh",
        "one_liner": "用家里的醋做清洁，而不是买一堆专用剂。",
        "summary": "本片讲白醋做家务清洁的用法与禁忌。",
        "focuses": [{"title": "白醋是通用清洁剂", "detail": "去水垢、除味、杀菌都靠酸。"}],
        "key_points": [{"title": "玻璃用 1:1 醋水", "detail": "喷匀再擦。"}],
        "hard_points": [{"title": "别浇在大理石上", "detail": "酸会咬石材。"}],
    }
    js, md = write_summary(data, tmp_path, "full_16k")
    text = md.read_text(encoding="utf-8")
    assert "## 重点" in text
    assert "## 要点" in text
    assert "## 难点" in text
    assert "## 总体总结" in text
    assert "本片讲白醋做家务清洁的用法与禁忌。" in text
    assert "白醋是通用清洁剂" in text
    dumped = js.read_text(encoding="utf-8")
    assert "focuses" in dumped
    assert "hard_points" in dumped
    nested = tmp_path / "notes" / "zh" / "summary.md"
    assert nested.exists()
    assert "## 重点" in nested.read_text(encoding="utf-8")
    assert (tmp_path / "notes" / "README.md").exists()
    assert not (tmp_path / "full_16k_summary.md").exists()
    assert not (tmp_path / "full_16k_summary.json").exists()


def test_sync_notes_docs_classifies_by_lang(tmp_path: Path) -> None:
    from pipeline import sync_notes_docs

    (tmp_path / "full_16k_summary.json").write_text(
        '{"title": "T", "source_lang": "en", "summary_lang": "en", '
        '"one_liner": "Hi", "focuses": [{"title": "A", "detail": "a"}], '
        '"key_points": [], "hard_points": []}',
        encoding="utf-8",
    )
    (tmp_path / "full_16k_summary_zh.json").write_text(
        '{"title": "题", "one_liner": "你好", "focuses": [{"title": "甲", "detail": "一"}]}',
        encoding="utf-8",
    )
    paths = sync_notes_docs(tmp_path)
    assert (tmp_path / "notes" / "en" / "summary.md").exists()
    assert (tmp_path / "notes" / "zh" / "summary.md").exists()
    zh = (tmp_path / "notes" / "zh" / "summary.md").read_text(encoding="utf-8")
    assert "## 重点" in zh
    assert "甲" in zh
    assert (tmp_path / "notes" / "README.md").exists()
    assert len(paths) == 2


def test_purge_legacy_summary_files(tmp_path: Path) -> None:
    from pipeline import purge_legacy_summary_files, sync_notes_docs

    (tmp_path / "full_16k_summary.json").write_text(
        '{"title": "T", "source_lang": "en", "summary_lang": "en", "one_liner": "Hi"}',
        encoding="utf-8",
    )
    (tmp_path / "full_16k_summary.md").write_text("# old\n", encoding="utf-8")
    (tmp_path / "full_16k_summary_zh.json").write_text(
        '{"title": "题", "one_liner": "你好"}', encoding="utf-8"
    )
    (tmp_path / "full_16k_keypoints.json").write_text("{}", encoding="utf-8")
    sync_notes_docs(tmp_path)
    removed = purge_legacy_summary_files(tmp_path)
    assert len(removed) >= 3
    assert (tmp_path / "notes" / "en" / "summary.md").exists()
    assert (tmp_path / "notes" / "zh" / "summary.md").exists()
    assert not (tmp_path / "full_16k_summary.json").exists()
    assert not (tmp_path / "full_16k_summary.md").exists()
    assert not (tmp_path / "full_16k_summary_zh.json").exists()
    assert not (tmp_path / "full_16k_keypoints.json").exists()


def test_normalize_key_points_mixed() -> None:
    assert normalize_key_points(
        [{"title": "A", "detail": "d"}, "bare", {"title": "  "}]
    ) == [{"title": "A", "detail": "d"}, {"title": "bare", "detail": ""}]
