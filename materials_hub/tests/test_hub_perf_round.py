# -*- coding: utf-8 -*-
"""面板性能轮（2026-11-13）回归网:排序缓存共享 / stats 单次载入 /
ffmpeg 并发上限 / 长列表 CSS 虚拟化。

四件事都属于「不报错但会随数据规模静默劣化」的优化，必须用机制测试锁住，
否则以后任何一次重构都可能把它们悄悄改回 O(N) 或无限并发。
"""
import os
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import server  # noqa: E402


def _rows(n=3):
    return [{"id": "m%d" % i, "name": "clip %d" % i,
             "created_at": "2026-11-13T00:00:%02dZ" % i, "size": 100 + i,
             "kind": "images"} for i in range(n)]


def _rank_stub(monkeypatch, rows, calls, delay=0.0):
    """替换掉会连 ollama / 读库的真实排序，只统计「算了几次」。"""
    monkeypatch.setattr(core, "_RANK_CACHE", {})
    monkeypatch.setattr(core, "_RANK_COMPUTING", {})
    monkeypatch.setattr(core, "_SEARCH_CACHE", {})
    monkeypatch.setattr(core, "_maybe_translate", lambda q: q)
    monkeypatch.setattr(core, "favorite_ids", lambda: set())
    monkeypatch.setattr(core, "_mark_hit", lambda q, m: m)
    monkeypatch.setattr(core, "_cache_persist", lambda: None)
    monkeypatch.setattr(core.obs, "record", lambda *a, **kw: None)

    def fake(_q, _kind, _tag, _mode):
        calls.append((_q, _kind, _tag, _mode))
        if delay:
            time.sleep(delay)
        return [dict(m) for m in rows], False

    monkeypatch.setattr(core, "_ranked_uncached", fake)


def test_rank_cache_shared_between_count_and_pages(monkeypatch):
    """/api/count 与两页 /api/list 只跑一次 ranking（语义模式尤其贵）。"""
    rows, calls = _rows(5), []
    _rank_stub(monkeypatch, rows, calls)

    total = core.count_materials("dog", mode="lexical")
    p1 = core.search("dog", limit=2, offset=0, mode="lexical")
    p2 = core.search("dog", limit=2, offset=2, mode="lexical")

    assert total == len(rows)
    assert [m["id"] for m in p1 + p2] == ["m0", "m1", "m2", "m3"]
    assert len(calls) == 1, "count + 两页 list 应共享同一份排序结果"


def test_rank_cache_invalidated_on_generation_bump(monkeypatch):
    """写操作 bump 代际后必须重算，不能拿旧排名糊弄增删后的列表。"""
    calls = []
    _rank_stub(monkeypatch, _rows(), calls)

    core.count_materials("dog", mode="lexical")
    core.count_materials("dog", mode="lexical")
    assert len(calls) == 1

    core.bump_generation()
    core.count_materials("dog", mode="lexical")
    assert len(calls) == 2


def test_rank_cache_single_computation_under_concurrency(monkeypatch):
    """同一查询并发到达时只算一次（防惊群），不同查询仍可并行。"""
    calls = []
    _rank_stub(monkeypatch, _rows(), calls, delay=0.08)
    barrier = threading.Barrier(6)
    results = []

    def worker():
        barrier.wait(timeout=5)
        results.append(core._ranked("dog", mode="lexical")[0])

    ts = [threading.Thread(target=worker) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)

    assert all(not t.is_alive() for t in ts)
    assert len(results) == 6
    assert all([m["id"] for m in r] == ["m0", "m1", "m2"] for r in results)
    assert len(calls) == 1


def test_stats_helpers_reuse_loaded_rows(monkeypatch):
    """/api/stats 的三个聚合函数传 ms 后不得再全表载入或逐条 get_material。"""
    ms = [{"id": "m0", "kind": "images", "location": "internal",
           "tags": "", "external_path": "", "rel_path": "x.jpg"}]

    def forbidden(*_a, **_kw):
        raise AssertionError("不应重复全表载入 / 逐条查库")

    monkeypatch.setattr(core, "all_materials", forbidden)
    monkeypatch.setattr(core, "get_material", forbidden)

    assert core.external_stats(ms)["internal"] == 1
    assert isinstance(core.kind_subfacets(ms), dict)
    assert isinstance(core.pending_map(ms), dict)


class _FakeHandler:
    """只实现 /api/stats 分支用到的 handler 协议，不起 socket。"""

    def __init__(self, path="/api/stats"):
        self.path = path
        self.headers = {}
        self.payload = None

    def _maybe_gateway(self):
        return False

    def _json(self, obj, code=200):
        self.payload = obj
        return obj


def test_api_stats_single_load_and_generation_invalidation(monkeypatch):
    """单次 /api/stats 只 all_materials 一次；TTL 内复用；代际变化立即失效。"""
    monkeypatch.setattr(server, "_STATS_CACHE",
                        {"gen": -1, "ts": 0.0, "ttl": 15.0, "data": None})
    ms = [{"id": "m0", "kind": "images", "location": "internal",
           "form": "other", "topics": "", "usage": "", "tags": ""}]
    loads, gen = [], [7]
    monkeypatch.setattr(server, "all_materials", lambda: loads.append(1) or ms)
    monkeypatch.setattr(server, "get_material",
                        lambda *_a, **_kw: (_ for _ in ()).throw(
                            AssertionError("stats 不应 N+1 get_material")))
    monkeypatch.setattr(server, "hub_generation", lambda: gen[0])
    monkeypatch.setattr(server, "duplicates", lambda: [])
    monkeypatch.setattr(server, "thumbs_status", lambda: {"ffmpeg": False})
    monkeypatch.setattr(server, "embed_status", lambda: {"available": False})
    monkeypatch.setattr(server, "pending_map", lambda rows: {} if rows is ms else None)
    monkeypatch.setattr(server, "external_stats", lambda rows: {"internal": len(rows)})
    monkeypatch.setattr(server, "kind_subfacets", lambda rows: {})

    first = _FakeHandler()
    server.Handler.do_GET(first)
    assert first.payload["total"] == 1 and len(loads) == 1

    cached = _FakeHandler()
    server.Handler.do_GET(cached)
    assert cached.payload == first.payload and len(loads) == 1

    gen[0] = 8
    server.Handler.do_GET(_FakeHandler())
    assert len(loads) == 2, "写操作后 stats 必须立即重算"


def test_thumb_ffmpeg_concurrency_is_bounded(tmp_path, monkeypatch):
    """按需封面抽帧并发不得超过信号量上限（大库首屏不能拉满 ffmpeg 进程）。"""
    src = tmp_path / "src.mp4"
    src.write_bytes(b"fake video")
    state = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def fake_run(cmd, **_kw):
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.06)
        with open(cmd[-1], "wb") as f:
            f.write(b"jpeg")
        with lock:
            state["cur"] -= 1
        return SimpleNamespace(stderr=b"")

    monkeypatch.setattr(core, "THUMBS", str(tmp_path))
    monkeypatch.setattr(core, "thumb_path", lambda mid: str(tmp_path / (mid + ".jpg")))
    monkeypatch.setattr(core, "get_material",
                        lambda mid: {"id": mid, "kind": "videos", "location": "internal"})
    monkeypatch.setattr(core, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(core, "_abs_source", lambda _m: str(src))
    monkeypatch.setattr(core, "_run_ffmpeg_safe", fake_run)
    monkeypatch.setattr(core, "_THUMB_SLOTS", threading.BoundedSemaphore(2))

    barrier = threading.Barrier(6)
    out = {}

    def worker(i):
        barrier.wait(timeout=5)
        out[i] = core.make_thumb("m%d" % i)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=5)

    assert all(not t.is_alive() for t in ts)
    assert all(p and os.path.isfile(p) for p in out.values())
    assert 1 <= state["max"] <= 2, "实测并发 %s 超过信号量上限 2" % state["max"]


def test_css_content_visibility_covers_mixed_layout():
    """长列表 CSS 虚拟化必须同时覆盖单卡与整行合集，且打印时恢复完整渲染。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    css_path = os.path.join(root, "transform", "app", "globals.css")
    if not os.path.isfile(css_path):
        import pytest
        pytest.skip("transform 前端不在本工作区")
    with open(css_path, "r", encoding="utf-8") as f:
        css = f.read()
    assert ".hub-grid > .hub-card { content-visibility: auto;" in css
    assert ".hub-grid > .hub-collection { content-visibility: auto;" in css
    assert "contain-intrinsic-size: auto 15rem" in css
    assert "contain-intrinsic-size: auto 6rem" in css
    assert "@media print" in css and "content-visibility: visible" in css
