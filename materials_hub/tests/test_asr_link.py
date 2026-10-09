# -*- coding: utf-8 -*-
"""同 job 字幕挂到视频/音轨,检索能命中台词,且不写 description。"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core


def _setup():
    tmp = tempfile.mkdtemp(prefix="hub_asr_")
    saved = (core.INDEX_DIR, core.INDEX_DB, core.ASR_DIR, core.OCR_DIR,
             core.VISUAL_DIR, core.MATERIALS)
    core.INDEX_DIR = tmp
    core.INDEX_DB = os.path.join(tmp, "hub.db")
    core.ASR_DIR = os.path.join(tmp, "asr")
    core.OCR_DIR = os.path.join(tmp, "ocr")
    core.VISUAL_DIR = os.path.join(tmp, "visual")
    core.MATERIALS = os.path.join(tmp, "materials")
    os.makedirs(core.ASR_DIR, exist_ok=True)
    core._init_db()
    return tmp, saved


def _restore(saved):
    (core.INDEX_DIR, core.INDEX_DB, core.ASR_DIR, core.OCR_DIR,
     core.VISUAL_DIR, core.MATERIALS) = saved


def _add(mid, kind, name, tags, path, body=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    core.add_material(id=mid, kind=kind, ext=os.path.splitext(name)[1], name=name,
                      rel_path="", size=1, sha256=mid + "ab", tags=tags, description="",
                      source="", orig_name=name, created_at="2026-01-01")
    core._update_material(mid, location="external", external_path=path)


def test_attach_zh_and_search_video():
    tmp, saved = _setup()
    orig_sync = core._sync_material_vector
    core._sync_material_vector = lambda mid: None
    try:
        sdir = os.path.join(tmp, "subs")
        _add("suben", "subs", "字幕_英文_en.srt", "job:j1",
             os.path.join(sdir, "en.srt"),
             "1\n00:00:01,000 --> 00:00:02,000\nenglish only line\n")
        _add("subzh", "subs", "字幕_中文_zh.srt", "job:j1",
             os.path.join(sdir, "zh.srt"),
             "1\n00:00:01,000 --> 00:00:02,000\n红烧鸡翅出锅了\n")
        _add("vid1", "videos", "source.mp4", "job:j1,role:master",
             os.path.join(tmp, "source.mp4"), "x")
        _add("aud1", "audio", "full_16k.wav", "job:j1,asr",
             os.path.join(tmp, "full_16k.wav"), "x")
        st = core.attach_job_transcripts()
        assert st["attached"] == 2, st
        text = core._asr_text("vid1")
        assert "红烧鸡翅出锅了" in text, text
        assert "english only" not in text
        assert "[00:00:01,000-00:00:02,000]" in text
        hits = core.search("红烧鸡翅", mode="lexical")
        ids = [m["id"] for m in hits]
        assert "vid1" in ids and "aud1" in ids, ids
        assert core.get_material("vid1").get("description") in ("", None)
    finally:
        core._sync_material_vector = orig_sync
        _restore(saved)
        shutil.rmtree(tmp, ignore_errors=True)
