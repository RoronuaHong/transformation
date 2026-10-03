# -*- coding: utf-8 -*-
"""方案 A: master / clip / parent / shots-first 标签契约。

离线运行:
  python tests/test_taxonomy_plan_a.py
"""
import json
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import bridge_subtitle as bridge  # noqa: E402
import mcp_server  # noqa: E402


def _setup_tmp():
    tmp = tempfile.mkdtemp(prefix="hub_tax_", dir=core.HUB)
    core.INDEX_DIR = tmp
    core.MATERIALS = os.path.join(tmp, "materials")
    core.OCR_DIR = os.path.join(tmp, "ocr")
    core.SHOT_DIR = os.path.join(tmp, "shots")
    core.PHASH_DIR = os.path.join(tmp, "phash")
    core.THUMBS = os.path.join(tmp, "thumbs")
    core.INDEX_DB = os.path.join(tmp, "hub.db")
    for d in (core.MATERIALS, core.OCR_DIR, core.SHOT_DIR, core.PHASH_DIR, core.THUMBS):
        os.makedirs(d, exist_ok=True)
    for k in ("videos", "silent", "audio", "images", "docs", "subs"):
        os.makedirs(os.path.join(core.MATERIALS, k), exist_ok=True)
    core._init_db()
    return tmp


def _add(mid, kind, name, tags="", abs_path=None):
    if abs_path:
        abs_p = abs_path
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
    else:
        abs_p = os.path.join(core.MATERIALS, kind, name)
        os.makedirs(os.path.dirname(abs_p), exist_ok=True)
    with open(abs_p, "wb") as f:
        f.write(b"\x00" * 16)
    rel = abs_p if not abs_p.startswith(core.HUB) else os.path.relpath(abs_p, core.HUB)
    core.add_material(
        id=mid, kind=kind, ext=os.path.splitext(name)[1] or ".mp4", name=name,
        rel_path=rel, size=16, sha256=mid + "0" * 20, tags=tags, description="",
        source="test", orig_name=name, created_at="2026-09-30T00:00:00",
    )
    return mid


def test_media_facet_master_vs_clip():
    master = core.media_facet_tags(r"D:/sp/downloads/batch/yt_j1/media/source.mp4", "videos")
    assert "role:master" in master and "role:picture" in master, master
    assert "role:clip" not in master, master
    clip = core.media_facet_tags(
        r"D:/sp/downloads/batch/yt_j1/media/clips/range_00.mp4", "videos"
    )
    assert "role:clip" in clip and "type:clip" in clip, clip
    assert "role:master" not in clip, clip
    silent = core.media_facet_tags(r"D:/hub/materials/silent/a_silent.mp4", "silent")
    assert "role:silent-picture" in silent and "has_audio:0" in silent, silent
    print("PASS test_media_facet_master_vs_clip")


def test_clip_timecodes_from_meta():
    tmp = tempfile.mkdtemp(prefix="clips_meta_")
    try:
        clips = os.path.join(tmp, "clips")
        os.makedirs(clips)
        with open(os.path.join(clips, "clips_meta.json"), "w", encoding="utf-8") as f:
            json.dump({
                "clock": "clips",
                "spans": [
                    {"file": "range_00.mp4", "start": 1.5, "end": 4.25, "duration": 2.75},
                ],
            }, f)
        path = os.path.join(clips, "range_00.mp4")
        with open(path, "wb") as f:
            f.write(b"x")
        tags = core.media_facet_tags(path, "videos")
        assert "t_start:1.500" in tags and "t_end:4.250" in tags, tags
        print("PASS test_clip_timecodes_from_meta")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bridge_type_clip():
    assert bridge.type_from_rel("downloads/batch/yt_j1/media/clips/range_00.mp4") == "clip"
    assert bridge.type_from_rel("downloads/batch/yt_j1/media/source.mp4") == "media"
    assert bridge.type_from_rel("downloads/batch/yt_j1/odd/file.bin") == ""
    print("PASS test_bridge_type_clip")


def test_tag_ui_meta_and_scrub_from():
    m = core.tag_ui_meta("role:master", "zh")
    assert m["label"] == "角色·母版" and not m["hide"], m
    assert core.tag_ui_meta("from:abc", "zh")["hide"] is True
    assert core.tag_ui_meta("type:media", "zh")["label"] == "类型·源片"
    assert core.tag_ui_meta("sp", "zh")["label"] == "来源·流水线"
    tmp = _setup_tmp()
    try:
        _add("pmaster11111", "videos", "m.mp4", "role:master,job:j1")
        _add("pchild222222", "silent", "m_silent.mp4",
             "role:silent-picture,parent:pmaster11111,from:pmaster11111,sp")
        r = core.scrub_deprecated_from_tags()
        assert r["cleaned"] == 1, r
        tags = core.get_material("pchild222222")["tags"]
        assert "parent:pmaster11111" in tags and "from:pmaster11111" not in tags, tags
        ui = core.tags_for_ui(lang="zh")
        names = {x["tag"] for x in ui}
        assert "from:pmaster11111" not in names
        assert any(x["tag"] == "sp" and x["label"] == "来源·流水线" for x in ui), ui
        print("PASS test_tag_ui_meta_and_scrub_from")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_link_clip_parents_and_job_checkup():
    tmp = _setup_tmp()
    try:
        _add("masteraaaaaa", "videos", "src.mp4",
             "sp,job:jtax,type:media,role:master,role:picture")
        _add("clipbbbbbbbb", "videos", "range_00.mp4",
             "sp,job:jtax,type:clip,role:clip")
        r = core.link_clip_parents(mids=["clipbbbbbbbb"])
        assert r["linked"] == 1, r
        m = core.get_material("clipbbbbbbbb")
        assert "parent:masteraaaaaa" in (m.get("tags") or ""), m.get("tags")
        chk = core.job_checkup("jtax")
        assert "masteraaaaaa" in chk["master_ids"], chk
        assert "clipbbbbbbbb" in chk["clip_ids"], chk
        assert chk["clips_missing_parent"] == [], chk
        # clip 不要求 shots
        assert "clipbbbbbbbb" not in chk["missing_shots"], chk
        assert "masteraaaaaa" in chk["missing_shots"], chk
        print("PASS test_link_clip_parents_and_job_checkup")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pending_shots_skip_clip():
    tmp = _setup_tmp()
    try:
        _add("clipcccccccc", "videos", "range_01.mp4", "role:clip,type:clip")
        p = core.pending_processing("clipcccccccc")
        assert p["shots"] is False, p
        print("PASS test_pending_shots_skip_clip")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_coerce_and_component_parents():
    tmp = _setup_tmp()
    try:
        # 旧库只有 role:picture → coerce 补 master
        coerced = core.coerce_role_tags(
            "sp,type:media,role:picture,has_audio:1", kind="videos"
        )
        assert "role:master" in coerced, coerced
        assert "role:clip" not in coerced, coerced
        # clip 互斥去掉 master
        c2 = core.coerce_role_tags(
            "role:master,role:picture,role:clip", kind="videos",
            path=r"D:/x/media/clips/range_00.mp4",
        )
        assert "role:clip" in c2 and "role:master" not in c2, c2
        _add("masterdddddd", "videos", "BV_demo.mp4",
             "sp,job:jcomp,bilibili,type:media,role:master")
        _add("silenteeeeee", "silent", "BV_demo_silent.mp4",
             "role:silent-picture,has_audio:0")
        _add("audiofffffff", "audio", "BV_demo_track.wav",
             "role:audio-stem,has_audio:1")
        r = core.link_component_parents()
        assert r["linked"] >= 2, r
        s = core.get_material("silenteeeeee")
        a = core.get_material("audiofffffff")
        assert "parent:masterdddddd" in (s.get("tags") or ""), s.get("tags")
        assert "parent:masterdddddd" in (a.get("tags") or ""), a.get("tags")
        assert "job:jcomp" in (s.get("tags") or ""), s.get("tags")
        assert "bilibili" in (a.get("tags") or ""), a.get("tags")
        # split 跳过 clip
        _add("clipgggggggg", "videos", "range_02.mp4", "role:clip,type:clip,job:jcomp")
        skip = core.split_video_to_silent_and_audio("clipgggggggg")
        assert skip.get("status") == "skipped" and skip.get("reason") == "role:clip", skip
        # images 不缺 shots
        _add("imghhhhhhhhh", "images", "p.jpg", "sp")
        pend = core.pending_processing("imghhhhhhhhh")
        assert pend["shots"] is False, pend
        print("PASS test_coerce_and_component_parents")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mcp_segment_first_prompt():
    pl = mcp_server._prompts_list()
    names = {p["name"] for p in pl}
    assert "segment_first" in names, names
    pg = mcp_server._prompt_get("segment_first", {"query": "厨房", "job_id": "j1"})
    text = pg["messages"][0]["content"]["text"]
    assert "role:master" in text and "role:clip" in text and "get_shots" in text, text
    print("PASS test_mcp_segment_first_prompt")


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except Exception as e:
            fails += 1
            print("FAIL %s: %s: %s" % (name, type(e).__name__, e))
    if fails:
        sys.exit(1)
    print("ALL PASS")
