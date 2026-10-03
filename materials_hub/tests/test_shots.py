# -*- coding: utf-8 -*-
"""test_shots.py — 镜头索引(ffmpeg 场景检测,片段级输出)的离线测试。

全 mock 驱动:库用临时文件,mock _probe_duration/ffmpeg_path/subprocess.run,
不跑真 ffmpeg、不连 ollama、不动真实索引与素材。复跑:python tests/test_shots.py
"""
import os
import sys
import json
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

_TMP = None


def _setup():
    """惰性初始化临时环境(直接跑与 pytest 均可用)。"""
    global _TMP
    if _TMP is None:
        _TMP = tempfile.mkdtemp(prefix="hub_shots_test_")
        core.INDEX_DB = os.path.join(_TMP, "hub.db")
        core.SHOT_DIR = os.path.join(_TMP, "shots")
        core._init_db()


def _teardown():
    global _TMP
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP = None


def _mk_material(mid, kind="videos", name="v.mp4"):
    _setup()
    # 用 external 引用临时空文件,remove 时只删索引、绝不触发磁盘移动
    f = os.path.join(_TMP, f"{mid}_{name}")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("x")
    core.add_material(id=mid, kind=kind, ext=os.path.splitext(name)[1], name=name,
                      rel_path="", size=1, sha256="x" * 64, tags="", description="",
                      source="", orig_name=name, created_at="2026-01-01")
    core._update_material(mid, location="external", external_path=f)
    return core.get_material(mid)


class _FakeProc:
    """替代 subprocess.CompletedProcess:build_shot_index 只读 .stderr(bytes)。"""

    def __init__(self, stderr=b""):
        self.stdout = b""
        self.stderr = stderr
        self.returncode = 0


# 模拟 ffmpeg showinfo 输出:两处切点 2.5s / 7.1s(混入非 showinfo 日志行,验证正则解析)
_FAKE_STDERR = (
    b"[Parsed_showinfo_1 @ 000001a0] n:   0 pts:  25000 pts_time:2.500\n"
    b"frame= 12 fps=0.0 q=-0.0 size=N/A time=00:00:07.10 bitrate=N/A speed=12x\n"
    b"[Parsed_showinfo_1 @ 000001a0] n:   1 pts:  71000 pts_time:7.100\n"
)


def _mock_ffmpeg_env(stderr=_FAKE_STDERR):
    """打桩:时长 10s、ffmpeg 可用、subprocess.run 返回固定 stderr。返回还原函数与调用记录。"""
    calls = []
    orig = (core._probe_duration, core.ffmpeg_path, core.subprocess.run)

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _FakeProc(stderr)

    core._probe_duration = lambda p: 10.0
    core.ffmpeg_path = lambda: "ffmpeg"
    core.subprocess.run = fake_run

    def restore():
        core._probe_duration, core.ffmpeg_path, core.subprocess.run = orig
    return restore, calls


def test_shot_sidecar_path_injection():
    _setup()
    # 路径注入防护:含分隔符/冒号/点的 id 拒绝生成 sidecar 路径
    assert core.shot_index_path("../evil") is None
    assert core.shot_index_path("a/b") is None
    assert core.shot_index_path("a:b") is None
    assert core.shot_index_path("a.json") is None
    assert core.shot_index_path("") is None
    p = core.shot_index_path("s0")
    assert p and p.endswith("s0.json") and os.path.dirname(p) == core.SHOT_DIR, p
    print("PASS test_shot_sidecar_path_injection")


def test_build_shot_index_ok_and_history():
    _mk_material("s1")
    restore, calls = _mock_ffmpeg_env()
    try:
        r = core.build_shot_index("s1")
    finally:
        restore()
    assert r["status"] == "ok" and r["scenes"] == 3 and r["duration"] == 10.0, r
    assert calls and calls[0][0] == "ffmpeg", calls          # 用的是打桩的 ffmpeg
    assert "-v" in calls[0] and calls[0][calls[0].index("-v") + 1] == "info", calls[0]
    # sidecar JSON 可读,片段为 3 段 [0-2.5, 2.5-7.1, 7.1-10]
    d = core.get_shots("s1")
    assert d and len(d["scenes"]) == 3, d
    assert d["scenes"][0] == {"start": 0.0, "end": 2.5}, d
    assert d["scenes"][1] == {"start": 2.5, "end": 7.1}, d
    assert d["scenes"][2] == {"start": 7.1, "end": 10.0}, d
    assert d["duration"] == 10.0 and d["threshold"] == 0.30, d
    # history 有 shot_index 审计记录
    hs = core.get_history(limit=10, target_id="s1")
    assert any(h["action"] == "shot_index" and h["detail"] == "scenes=3" for h in hs), hs
    print("PASS test_build_shot_index_ok_and_history")


def test_thin_cuts_and_interval_fallback():
    assert core._thin_cuts([1, 2, 3, 4, 5, 6], 3) == [1, 3, 6]
    assert core._thin_cuts([1.0, 2.0], 10) == [1.0, 2.0]
    _mk_material("s_int")
    restore, calls = _mock_ffmpeg_env(stderr=b"frame=1\n")  # 无 pts_time → 空切点
    # dur>12 才走 interval;打桩改成 100s
    orig_dur = core._probe_duration
    core._probe_duration = lambda p: 100.0
    try:
        r = core.build_shot_index("s_int", force=True)
    finally:
        core._probe_duration = orig_dur
        restore()
    assert r["status"] == "ok" and r["scenes"] >= 5, r
    d = core.get_shots("s_int")
    assert str(d.get("threshold", "")).startswith("interval:"), d
    # 主档空 → 还会尝试 FALLBACK_LOW,至少 1 次 ffmpeg
    assert len(calls) >= 1
    print("PASS test_thin_cuts_and_interval_fallback")


def test_build_shot_index_cached_no_subprocess():
    _mk_material("s2")
    _setup()
    os.makedirs(core.SHOT_DIR, exist_ok=True)
    sc = core.shot_index_path("s2")
    with open(sc, "w", encoding="utf-8") as f:
        json.dump({"duration": 10.0, "threshold": 0.3,
                   "scenes": [{"start": 0.0, "end": 10.0}]}, f)

    def boom(*a, **kw):                                   # cached 路径绝不能碰子进程
        raise AssertionError("subprocess.run must not be called when cached")

    orig = (core.subprocess.run, core.ffmpeg_path)
    core.subprocess.run = boom
    core.ffmpeg_path = lambda: "ffmpeg"
    try:
        r = core.build_shot_index("s2")
    finally:
        core.subprocess.run, core.ffmpeg_path = orig
    assert r["status"] == "cached" and r["scenes"] == 1, r
    print("PASS test_build_shot_index_cached_no_subprocess")


def test_build_shot_index_no_ffmpeg_and_guards():
    _mk_material("s3")
    orig = core.ffmpeg_path
    core.ffmpeg_path = lambda: None                       # 无 ffmpeg → skipped(防真跑)
    try:
        r = core.build_shot_index("s3", force=True)
    finally:
        core.ffmpeg_path = orig
    assert r["status"] == "skipped" and r["reason"] == "no_ffmpeg", r
    # 素材不存在
    r2 = core.build_shot_index("no_such_id")
    assert r2["status"] == "error" and r2["reason"] == "not_found", r2
    # 非视频 → skipped
    _mk_material("s4", kind="docs", name="d.md")
    r3 = core.build_shot_index("s4")
    assert r3["status"] == "skipped" and r3["reason"].startswith("kind="), r3
    print("PASS test_build_shot_index_no_ffmpeg_and_guards")


def test_get_shots_roundtrip():
    _mk_material("s5")
    restore, _calls = _mock_ffmpeg_env()
    try:
        core.build_shot_index("s5")
    finally:
        restore()
    d1 = core.get_shots("s5")
    with open(core.shot_index_path("s5"), "r", encoding="utf-8") as f:
        d2 = json.load(f)
    assert d1 == d2, (d1, d2)                             # 读回与落盘一致
    assert core.get_shots("never_built") is None          # 未建索引 → None
    assert core.get_shots("../evil") is None              # 防注入 id → None
    print("PASS test_get_shots_roundtrip")


if __name__ == "__main__":
    test_shot_sidecar_path_injection()
    test_build_shot_index_ok_and_history()
    test_thin_cuts_and_interval_fallback()
    test_build_shot_index_cached_no_subprocess()
    test_build_shot_index_no_ffmpeg_and_guards()
    test_get_shots_roundtrip()
    _teardown()
    print("OK")
