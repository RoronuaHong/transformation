# -*- coding: utf-8 -*-
"""CLIP 平坦分拒绝、台词时间落到镜头。词法路径不调用以文搜图。"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core


def _setup():
    tmp = tempfile.mkdtemp(prefix="hub_clip_")
    saved = (core.INDEX_DIR, core.INDEX_DB, core.ASR_DIR, core.OCR_DIR,
             core.VISUAL_DIR, core.MATERIALS, core.SHOT_DIR)
    core.INDEX_DIR = tmp
    core.INDEX_DB = os.path.join(tmp, "hub.db")
    core.ASR_DIR = os.path.join(tmp, "asr")
    core.OCR_DIR = os.path.join(tmp, "ocr")
    core.VISUAL_DIR = os.path.join(tmp, "visual")
    core.MATERIALS = os.path.join(tmp, "materials")
    core.SHOT_DIR = os.path.join(tmp, "shots")
    os.makedirs(core.ASR_DIR, exist_ok=True)
    os.makedirs(core.SHOT_DIR, exist_ok=True)
    core._init_db()
    core._ASR_TEXT_CACHE.clear()
    return tmp, saved


def _restore(saved):
    (core.INDEX_DIR, core.INDEX_DB, core.ASR_DIR, core.OCR_DIR,
     core.VISUAL_DIR, core.MATERIALS, core.SHOT_DIR) = saved
    core._ASR_TEXT_CACHE.clear()


def _add(mid, kind, name):
    path = os.path.join(core.MATERIALS, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")
    core.add_material(id=mid, kind=kind, ext=os.path.splitext(name)[1], name=name,
                      rel_path="", size=1, sha256=mid + "ab", tags="", description="",
                      source="", orig_name=name, created_at="2026-01-01")
    core._update_material(mid, location="external", external_path=path)


def test_clock_and_scene():
    assert abs(core._clock_sec("5.5") - 5.5) < 1e-6
    assert abs(core._clock_sec("00:00:08,100") - 8.1) < 1e-6
    tmp, saved = _setup()
    try:
        _add("vid1", "videos", "talk.mp4")
        with open(os.path.join(core.SHOT_DIR, "vid1.json"), "w", encoding="utf-8") as f:
            f.write('{"scenes":[{"start":0,"end":4},{"start":4,"end":12}]}')
        assert core._scene_bounds("vid1", 5) == (4.0, 12.0)
    finally:
        _restore(saved)
        shutil.rmtree(tmp, ignore_errors=True)


def test_clip_flat_cluster_rejected():
    tmp, saved = _setup()
    try:
        _add("stamp1", "images", "stamp.png")
        _add("stamp2", "images", "visa.png")

        def fake_clip(text, *, limit=20, min_score=0.15):
            return {"matches": [
                {"id": "stamp1", "score": 0.286},
                {"id": "stamp2", "score": 0.280},
            ]}

        orig = core.search_by_text_image
        core.search_by_text_image = fake_clip
        try:
            out = core._append_clip_hits("鸡翅", [], "images")
        finally:
            core.search_by_text_image = orig
        assert out == []
    finally:
        _restore(saved)
        shutil.rmtree(tmp, ignore_errors=True)


def test_clip_gap_appends_and_lexical_skips():
    tmp, saved = _setup()
    calls = {"n": 0}
    try:
        _add("wing1", "images", "plate.png")
        _add("other", "images", "other.png")

        def fake_clip(text, *, limit=20, min_score=0.15):
            calls["n"] += 1
            return {"matches": [
                {"id": "wing1", "score": 0.41},
                {"id": "other", "score": 0.22},
            ]}

        orig = core.search_by_text_image
        core.search_by_text_image = fake_clip
        orig_embed = core.embed_probe
        core.embed_probe = lambda: {"ok": False}
        try:
            out = core._append_clip_hits("鸡翅", [], "images")
            assert [m["id"] for m in out] == ["wing1"]
            assert out[0]["hit_via"] == "clip"
            hits = core.search("鸡翅", mode="lexical")
            assert calls["n"] == 1
            assert hits == []
        finally:
            core.search_by_text_image = orig
            core.embed_probe = orig_embed
    finally:
        _restore(saved)
        shutil.rmtree(tmp, ignore_errors=True)


def test_mark_hit_from_asr():
    tmp, saved = _setup()
    try:
        _add("vid1", "videos", "talk.mp4")
        with open(core.asr_sidecar_path("vid1"), "w", encoding="utf-8") as f:
            f.write("[00:00:05,760-00:00:08,100] 我发现做饭有一个问题\n")
        with open(os.path.join(core.SHOT_DIR, "vid1.json"), "w", encoding="utf-8") as f:
            f.write('{"scenes":[{"start":0,"end":4},{"start":4,"end":12}]}')
        core._ASR_TEXT_CACHE.clear()
        marked = core._mark_hit("做饭有一个问题", core.get_material("vid1"))
        assert marked["hit_via"] == "asr"
        assert abs(marked["hit_t"] - 5.76) < 0.01
        assert marked["hit_shot"] == 4.0
        assert marked["hit_end"] == 12.0
    finally:
        _restore(saved)
        shutil.rmtree(tmp, ignore_errors=True)
