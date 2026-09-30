# -*- coding: utf-8 -*-
"""P1-C / P2-B: list_missing_covers / job_checkup / split_new_videos / MCP prompts。

离线:不连 ollama、不跑真 ffmpeg。运行:
  python tests/test_agent_skills.py
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import mcp_server  # noqa: E402


def _setup_tmp():
    # 与 conftest 一致:临时库放 HUB 同盘,避免 health() relpath 跨盘 ValueError
    tmp = tempfile.mkdtemp(prefix="hub_skills_", dir=core.HUB)
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


def _add(mid, kind, name, tags="", location="internal"):
    rel = os.path.join("materials", kind, name)
    abs_p = os.path.join(core.HUB, rel) if False else os.path.join(core.MATERIALS, kind, name)
    # 文件放 MATERIALS 下;rel_path 相对 HUB 可能跨盘——测试用 abs 写进 rel_path 时
    # get_material 走 rel_path;make_thumb 用 _material_abs_path。这里写假文件到 MATERIALS。
    os.makedirs(os.path.dirname(abs_p), exist_ok=True)
    with open(abs_p, "wb") as f:
        f.write(b"\x00" * 16)
    core.add_material(
        id=mid, kind=kind, ext=os.path.splitext(name)[1] or ".mp4", name=name,
        rel_path=os.path.relpath(abs_p, core.HUB) if abs_p.startswith(core.HUB)
        else abs_p,
        size=16, sha256=mid + "0" * 20, tags=tags, description="",
        source="test", orig_name=name,
        created_at="2026-09-29T00:00:00",
        location=location,
    )
    return mid


def test_list_missing_covers_filters_kind():
    tmp = _setup_tmp()
    try:
        _add("v1aaaaaaaaaa", "videos", "a.mp4", "sp,job:j1")
        _add("s1bbbbbbbbbb", "silent", "b_silent.mp4", "sp,job:j1,demux")
        # 给 videos 造封面,silent 仍缺
        os.makedirs(core.THUMBS, exist_ok=True)
        with open(core.thumb_path("v1aaaaaaaaaa"), "wb") as f:
            f.write(b"jpg")
        all_miss = core.list_missing_covers()
        ids = {x["id"] for x in all_miss}
        assert "s1bbbbbbbbbb" in ids, ids
        assert "v1aaaaaaaaaa" not in ids, ids
        only_s = core.list_missing_covers(kind="silent")
        assert [x["id"] for x in only_s] == ["s1bbbbbbbbbb"], only_s
        print("PASS test_list_missing_covers_filters_kind")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_job_checkup_kinds_and_gaps():
    tmp = _setup_tmp()
    try:
        _add("v2cccccccccc", "videos", "c.mp4", "sp,job:upload_x,type:media")
        _add("a2dddddddddd", "audio", "full_16k.wav", "sp,job:upload_x,asr,role:audio-stem")
        _add("n2eeeeeeeeee", "docs", "summary.md", "sp,job:upload_x,type:notes")
        r = core.job_checkup("upload_x")
        assert r["count"] >= 3, r
        assert r["kinds"].get("videos") == 1, r
        assert r["kinds"].get("audio") == 1, r
        assert r["has_notes"] is True, r
        assert "v2cccccccccc" in r["missing_thumbs"], r
        assert "a2dddddddddd" in r["asr_ids"], r
        assert r["has_silent"] is False, r
        print("PASS test_job_checkup_kinds_and_gaps")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_split_new_videos_skips_non_video(monkey=None):
    tmp = _setup_tmp()
    try:
        _add("s3ffffffffff", "silent", "d_silent.mp4", "sp")
        calls = []

        def fake_split(mid, force=False):
            calls.append(mid)
            return {"status": "ok", "silent_status": "added", "silent_id": "newsilent"}

        core.split_video_to_silent_and_audio = fake_split
        r = core.split_new_videos(["s3ffffffffff", "nosuch"])
        assert r["ran"] == 0, r
        assert calls == [], calls
        _add("v3gggggggggg", "videos", "e.mp4", "sp")
        r2 = core.split_new_videos(["v3gggggggggg"])
        assert r2["ran"] == 1 and r2["ok"] == 1, r2
        assert r2["new_ids"] == ["newsilent"], r2
        print("PASS test_split_new_videos_skips_non_video")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mcp_prompts_and_tools():
    tmp = _setup_tmp()
    try:
        _add("s4hhhhhhhhhh", "silent", "f_silent.mp4", "sp,job:j2")
        pl = mcp_server._prompts_list()
        names = {p["name"] for p in pl}
        assert "fill_missing_thumbs" in names and "job_checkup" in names, names
        assert "segment_first" in names, names
        pg = mcp_server._prompt_get("fill_missing_thumbs", {"kind": "silent"})
        assert "list_missing_covers" in pg["messages"][0]["content"]["text"]
        pg2 = mcp_server._prompt_get("job_checkup", {"job_id": "j2"})
        assert "j2" in pg2["messages"][0]["content"]["text"]
        pg3 = mcp_server._prompt_get("segment_first", {"query": "x"})
        assert "get_shots" in pg3["messages"][0]["content"]["text"]
        miss = mcp_server.t_missing_covers({"kind": "silent"})
        assert any(x["id"] == "s4hhhhhhhhhh" for x in miss), miss
        chk = mcp_server.t_job_checkup({"job_id": "j2"})
        assert chk["count"] >= 1, chk
        # initialize 声明 prompts
        init = mcp_server._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2024-11-05"}})
        assert "prompts" in init["capabilities"], init
        plist = mcp_server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"})
        assert len(plist["prompts"]) >= 3, plist
        print("PASS test_mcp_prompts_and_tools")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_maintain_includes_missing_covers():
    tmp = _setup_tmp()
    try:
        import agent
        _add("s5iiiiiiiiii", "silent", "g_silent.mp4", "sp")
        # redirect agent's core is same module
        r = agent._sub_maintain()
        assert "missing_covers" in r, r
        assert r["missing_covers"] >= 1, r
        assert "s5iiiiiiiiii" in r.get("missing_silent_covers", []), r
        print("PASS test_maintain_includes_missing_covers")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # 保存可能被 monkeypatch 的函数
    _orig_split = core.split_video_to_silent_and_audio
    fails = 0
    for name, fn in list(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            core.split_video_to_silent_and_audio = _orig_split
            fn()
        except Exception as e:
            fails += 1
            print("FAIL %s: %s: %s" % (name, type(e).__name__, e))
    core.split_video_to_silent_and_audio = _orig_split
    if fails:
        sys.exit(1)
    print("ALL PASS")
