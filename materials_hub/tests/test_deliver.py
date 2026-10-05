"""按需交付(#9 优化转码) 离线测试(mock ffmpeg / get_material;不连真实 ffmpeg / 不写真实库)。

覆盖:
  - deliver_package dry-run(不写文件) / confirm 真正导出 / 缺失 id / 区间裁剪命令构造
  - 命名技能路由 deliver、_run_deliver_skill(dry vs 写权限)
  - agent 工具 _tool_deliver 的 confirm 护栏
运行: python tests/test_deliver.py
"""
import os
import sys
import json
import shutil
import tempfile
import subprocess as _subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import agent  # noqa: E402

core._init_db()

_ORIG = {"ffmpeg_path": core.ffmpeg_path, "get_material": core.get_material,
         "search": core.search, "expand_query": core.expand_query,
         "chat_models": core.chat_models, "deliver_package": core.deliver_package}
_ORIG_SUBPROC_RUN = core.subprocess.run
_ORIG_WS = agent.WORKSPACE
_FAKE_EXE = "fake-ffmpeg"


def _reset():
    for k, v in _ORIG.items():
        setattr(core, k, v)
    core.subprocess.run = _ORIG_SUBPROC_RUN
    agent.WORKSPACE = _ORIG_WS


def _mat(mid):
    p = os.path.join(tempfile.gettempdir(), "deliver_src_%s.mp4" % mid)
    with open(p, "wb") as f:
        f.write(b"\x00\x01")
    return {"id": mid, "kind": "videos", "name": mid + ".mp4",
            "tags": "sp", "location": "external", "external_path": p}


def _fake_get(mats):
    def f(mid):
        return next((m for m in mats if m["id"] == mid), None)
    return f


def _fake_run_mkdir_and_create_dst(cmd, **kw):
    # 模拟 ffmpeg 成功:在输出路径落一个非空文件
    dst = cmd[-1]
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(b"rendered")
    return _subprocess.CompletedProcess(cmd, 0, b"", b"")


def test_build_deliver_cmd_transcode():
    src = "/x/a.mp4"
    dst = "/o/a.mp4"
    cmd = core._build_deliver_cmd(_FAKE_EXE, src, dst, fmt="mp4", res="720")
    assert cmd[0] == _FAKE_EXE
    assert "-i" in cmd and src in cmd
    assert "libx264" in cmd
    assert "scale=-2:720" in cmd
    assert cmd[-3] == "-f" and cmd[-1] == dst


def test_build_deliver_cmd_clip():
    src = "/x/a.mp4"
    dst = "/o/a.mp4"
    cmd = core._build_deliver_cmd(_FAKE_EXE, src, dst, fmt="mp4",
                                  res="0", clip=(10.0, 25.5))
    assert "-ss" in cmd and "10.000" in cmd
    assert "-t" in cmd and "15.500" in cmd  # 25.5 - 10.0
    assert "libx264" in cmd


def test_build_deliver_cmd_copy_only():
    src = "/x/a.mp4"
    dst = "/o/a.mp4"
    cmd = core._build_deliver_cmd(_FAKE_EXE, src, dst, fmt="mp4",
                                  res="0", copy_only=True)
    assert "-c" in cmd and "copy" in cmd
    assert "libx264" not in cmd


def test_deliver_package_dry_run_no_files():
    mats = [_mat("m1"), _mat("m2")]
    core.ffmpeg_path = lambda: _FAKE_EXE
    core.get_material = _fake_get(mats)
    try:
        r = core.deliver_package(ids=["m1", "m2"], confirm=False, fmt="mp4", res="720")
        assert r["dry_run"] is True
        assert len(r["plan"]) == 2
        assert all(p["status"] == "planned" for p in r["plan"])
        assert r["written"] == [] and r["errors"] == []
        # 不应落任何交付文件
        assert not os.path.exists(r["out_dir"]) or os.listdir(r["out_dir"]) == []
    finally:
        _reset()


def test_deliver_package_confirm_writes():
    mats = [_mat("m1")]
    core.ffmpeg_path = lambda: _FAKE_EXE
    core.get_material = _fake_get(mats)
    core.subprocess.run = _fake_run_mkdir_and_create_dst
    try:
        r = core.deliver_package(ids=["m1"], confirm=True, fmt="mp4", res="720")
        assert r["dry_run"] is False
        assert len(r["written"]) == 1, r
        assert r["written"][0]["id"] == "m1"
        assert os.path.exists(r["written"][0]["dst"])
    finally:
        _reset()


def test_deliver_package_non_video_copies_instead_of_transcode():
    """非视频类(docs/subs/audio/images)按原样复制交付,保留原扩展名,不调 ffmpeg。

    实例教训:对 .srt/.ass 硬转 mp4 必然失败(ffmpeg 无法把字幕当视频编码)。"""
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "note.md")
    with open(src, "w", encoding="utf-8") as f:
        f.write("# hi\n")
    core.ffmpeg_path = lambda: _FAKE_EXE
    core.get_material = lambda mid: {"id": mid, "kind": "docs", "name": "note.md",
                                     "location": "external", "external_path": src}
    try:
        r = core.deliver_package(ids=["d1"], out_dir=os.path.join(tmp, "out"),
                                 confirm=True, fmt="mp4")
        assert r["dry_run"] is False
        assert len(r["written"]) == 1, r
        assert r["written"][0]["status"] == "copied", r
        dst = r["written"][0]["dst"]
        assert dst.endswith(".md"), dst          # 保留原扩展名,不强行转 .mp4
        assert os.path.exists(dst)
        assert r["plan"][0]["mode"] == "copy"    # 非视频走复制而非转码
    finally:
        _reset()
        shutil.rmtree(tmp, ignore_errors=True)


def test_deliver_package_missing_id():
    core.ffmpeg_path = lambda: _FAKE_EXE
    core.get_material = lambda mid: None
    try:
        r = core.deliver_package(ids=["deadbeef0000"], confirm=False)
        assert r["plan"][0]["status"] == "not_found"
        assert any(e["status"] == "not_found" for e in r["errors"])
    finally:
        _reset()


def test_deliver_package_manifest_resolves_assets():
    mats = [_mat("m1"), _mat("m2")]
    core.ffmpeg_path = lambda: _FAKE_EXE
    core.get_material = _fake_get(mats)
    ws = tempfile.mkdtemp()
    try:
        man = {"assets": [{"id": "m1"}, {"id": "m2"}]}
        mp = os.path.join(ws, "pkg.json")
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(man, f)
        r = core.deliver_package(manifest_path=mp, confirm=False)
        assert {p["id"] for p in r["plan"]} == {"m1", "m2"}
    finally:
        _reset()
        shutil.rmtree(ws, ignore_errors=True)


def test_tool_deliver_requires_confirm():
    assert agent._tool_deliver({"ids": ["m1"]}).get("error")
    # confirm=true 但走真实 deliver_package(无 ffmpeg):返回计划/错误而非抛
    r = agent._tool_deliver({"ids": ["m1"], "confirm": True})
    assert "plan" in r or "error" in r


def test_match_named_skill_deliver():
    assert agent._match_named_skill("把素材包导出成 720p mp4") == "deliver"
    assert agent._match_named_skill("deliver these clips") == "deliver"
    assert agent._match_named_skill("导出文件给剪辑") == "deliver"
    assert agent._match_named_skill("找猫的视频") == ""


def _fake_deliver(**kw):
    confirm = kw.get("confirm")
    return {"dry_run": not confirm, "out_dir": "/tmp/deliver_out",
            "plan": [{"id": "m1", "status": "planned"}],
            "written": [{"id": "m1", "dst": "/tmp/deliver_out/m1.mp4", "status": "ok"}]
            if confirm else [], "skipped": [], "errors": []}


def test_run_deliver_skill_dry_no_write_perm():
    ws = tempfile.mkdtemp()
    agent.WORKSPACE = ws
    core.get_material = _fake_get([_mat("m1")])
    core.deliver_package = lambda **kw: _fake_deliver(**kw)
    try:
        state = {"task": "导出 m1abcdef0001 成 mp4", "steps": [], "model": None,
                 "todos": agent._named_skill_todos("deliver")}
        ctx = {"seen_ids": set(), "allow_write": False}
        call = lambda name, a, i: {}
        s = agent._run_deliver_skill(state, ws, ctx, call)
        assert "dry-run" in s, s
        assert state["steps"][-1]["skill"] == "deliver"
        assert state["steps"][-1]["args"]["confirm"] is False
    finally:
        _reset()
        shutil.rmtree(ws, ignore_errors=True)


def test_match_named_skill_publish():
    assert agent._match_named_skill("一键出片：猫的混剪") == "publish"
    assert agent._match_named_skill("组装一个关于猫的素材包并导出成 mp4") == "publish"
    # 仅组装(无导出意图)仍是 package;仅导出已有素材包仍是 deliver
    assert agent._match_named_skill("帮我组装一个关于猫的素材包") == "package"
    assert agent._match_named_skill("把素材包导出成 720p mp4") == "deliver"


def _setup_pkg_mocks(mats):
    core.search = lambda q, kind="", tag="", limit=None, offset=0, mode="auto": mats[
        :limit or len(mats)]
    core.get_material = lambda mid: next(
        (m for m in mats if m["id"] == mid), None)
    core.expand_query = lambda q: q
    core.chat_models = lambda: []
    core.deliver_package = lambda **kw: _fake_deliver(**kw)


def test_run_publish_skill_dry_run():
    ws = tempfile.mkdtemp()
    agent.WORKSPACE = ws
    core.ffmpeg_path = lambda: _FAKE_EXE
    try:
        _setup_pkg_mocks([_mat("m1"), _mat("m2")])
        state = {"task": "一键出片：猫的混剪", "steps": [], "model": None,
                 "todos": agent._named_skill_todos("publish")}
        ctx = {"seen_ids": set(), "allow_write": False}
        s = agent._run_publish_skill(state, ws, ctx, lambda n, a, i: {})
        assert "一键出片" in s, s
        assert "素材包 2 条" in s, s
        assert "dry-run" in s, s
        assert len(state["steps"]) == 2, state["steps"]
        assert state["steps"][0]["action"] == "assemble_package"
        assert state["steps"][1]["action"] == "deliver"
        assert all(x["skill"] == "publish" for x in state["steps"])
    finally:
        _reset()
        shutil.rmtree(ws, ignore_errors=True)


def test_run_publish_skill_with_write_perm():
    ws = tempfile.mkdtemp()
    agent.WORKSPACE = ws
    core.ffmpeg_path = lambda: _FAKE_EXE
    try:
        _setup_pkg_mocks([_mat("m1")])
        state = {"task": "一键出片：猫的混剪", "steps": [], "model": None,
                 "todos": agent._named_skill_todos("publish")}
        ctx = {"seen_ids": set(), "allow_write": True}
        s = agent._run_publish_skill(state, ws, ctx, lambda n, a, i: {})
        assert "已导出" in s, s
        assert state["steps"][1]["args"]["confirm"] is True
    finally:
        _reset()
        shutil.rmtree(ws, ignore_errors=True)


def test_run_publish_skill_no_assets_stops():
    ws = tempfile.mkdtemp()
    agent.WORKSPACE = ws
    core.ffmpeg_path = lambda: _FAKE_EXE
    try:
        _setup_pkg_mocks([])
        state = {"task": "一键出片：不存在的题材", "steps": [], "model": None,
                 "todos": agent._named_skill_todos("publish")}
        ctx = {"seen_ids": set(), "allow_write": True}
        s = agent._run_publish_skill(state, ws, ctx, lambda n, a, i: {})
        assert "未检索到素材" in s, s
        # 0 素材时不应进入交付步骤
        assert len(state["steps"]) == 1, state["steps"]
    finally:
        _reset()
        shutil.rmtree(ws, ignore_errors=True)


def test_run_deliver_skill_with_write_perm():
    ws = tempfile.mkdtemp()
    agent.WORKSPACE = ws
    core.get_material = _fake_get([_mat("m1")])
    core.deliver_package = lambda **kw: _fake_deliver(**kw)
    try:
        state = {"task": "导出 m1abcdef0001 成 mp4", "steps": [], "model": None,
                 "todos": agent._named_skill_todos("deliver")}
        ctx = {"seen_ids": set(), "allow_write": True}
        call = lambda name, a, i: {}
        s = agent._run_deliver_skill(state, ws, ctx, call)
        assert "已导出" in s, s
        assert state["steps"][-1]["args"]["confirm"] is True
    finally:
        _reset()
        shutil.rmtree(ws, ignore_errors=True)
