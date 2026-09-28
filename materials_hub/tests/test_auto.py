# -*- coding: utf-8 -*-
"""test_auto.py — 事件驱动自动处理链(pending_processing / auto_process_material /
auto_process_all)的离线测试。

全 mock 驱动:库用临时文件,mock make_thumb/ocr_material/build_shot_index/
phash_material/build_embeddings,不跑真 ffmpeg、不连 ollama、不动真实索引与素材。
复跑:python tests/test_auto.py
"""
import os
import sys
import json
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

_TMP = None


def _setup(reset=False):
    """惰性初始化临时环境(直接跑与 pytest 均可用);reset=True 强制重建(用例间隔离)。"""
    global _TMP
    if _TMP is not None and reset:
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP = None
    if _TMP is None:
        _TMP = tempfile.mkdtemp(prefix="hub_auto_test_")
        core.INDEX_DB = os.path.join(_TMP, "hub.db")
        core.OCR_DIR = os.path.join(_TMP, "ocr")
        core.SHOT_DIR = os.path.join(_TMP, "shots")
        core.PHASH_DIR = os.path.join(_TMP, "phash")
        core.THUMBS = os.path.join(_TMP, "thumbs")
        core._init_db()


def _teardown():
    global _TMP
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP = None


def _mk_material(mid, kind="videos", name="v.mp4"):
    _setup()
    # 用 external 引用临时空文件,避免触碰真实 materials/ 目录
    f = os.path.join(_TMP, f"{mid}_{name}")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("x")
    core.add_material(id=mid, kind=kind, ext=os.path.splitext(name)[1], name=name,
                      rel_path="", size=1, sha256="x" * 64, tags="", description="",
                      source="", orig_name=name, created_at="2026-01-01")
    core._update_material(mid, location="external", external_path=f)
    return core.get_material(mid)


def _stub_pipeline(calls, fail_ocr=False):
    """打桩四个处理步骤 + build_embeddings:记录调用顺序、按需落 sidecar(即标记"已处理")。
    fail_ocr=True 时 ocr_material 抛异常,用于验证单项失败不中断。
    返回 (还原函数, embed 调用次数列表)。"""
    orig = (core.make_thumb, core.ocr_material, core.build_shot_index,
            core.phash_material, core.build_embeddings)
    embed_calls = []

    def fake_thumb(mid, *a, **kw):
        calls.append("thumb")
        os.makedirs(core.THUMBS, exist_ok=True)
        p = core.thumb_path(mid)
        with open(p, "w", encoding="utf-8") as f:
            f.write("jpg")
        return p

    def fake_ocr(mid, *a, **kw):
        calls.append("ocr")
        if fail_ocr:
            raise RuntimeError("ocr boom")
        os.makedirs(core.OCR_DIR, exist_ok=True)
        with open(core.ocr_sidecar_path(mid), "w", encoding="utf-8") as f:
            f.write("ocr text")
        return {"id": mid, "status": "ok", "chars": 8}

    def fake_shots(mid, *a, **kw):
        calls.append("shots")
        os.makedirs(core.SHOT_DIR, exist_ok=True)
        with open(core.shot_index_path(mid), "w", encoding="utf-8") as f:
            json.dump({"duration": 1.0, "threshold": 0.3,
                       "scenes": [{"start": 0.0, "end": 1.0}]}, f)
        return {"id": mid, "status": "ok", "scenes": 1}

    def fake_phash(mid, *a, **kw):
        calls.append("phash")
        os.makedirs(core.PHASH_DIR, exist_ok=True)
        with open(core.phash_path(mid), "w", encoding="utf-8") as f:
            f.write("0" * 16)
        return {"id": mid, "status": "ok", "hash": "0" * 16}

    def fake_embed(*a, **kw):
        embed_calls.append(1)
        return {"available": False, "embedded": 0}

    core.make_thumb, core.ocr_material = fake_thumb, fake_ocr
    core.build_shot_index, core.phash_material = fake_shots, fake_phash
    core.build_embeddings = fake_embed

    def restore():
        core.make_thumb, core.ocr_material, core.build_shot_index = orig[0], orig[1], orig[2]
        core.phash_material, core.build_embeddings = orig[3], orig[4]
    return restore, embed_calls


def test_pending_processing():
    _mk_material("p1")
    # videos 缺全部 → 4 True
    p = core.pending_processing("p1")
    assert p == {"thumb": True, "ocr": True, "shots": True, "phash": True}, p
    # 建 OCR sidecar 后 → ocr False,其余仍 True
    os.makedirs(core.OCR_DIR, exist_ok=True)
    with open(core.ocr_sidecar_path("p1"), "w", encoding="utf-8") as f:
        f.write("x")
    p2 = core.pending_processing("p1")
    assert p2["ocr"] is False, p2
    assert p2["thumb"] and p2["shots"] and p2["phash"], p2
    # docs 素材 → 全 False(docs/subs 等不需要画面类派生数据)
    _mk_material("p2", kind="docs", name="d.md")
    p3 = core.pending_processing("p2")
    assert p3 == {"thumb": False, "ocr": False, "shots": False, "phash": False}, p3
    # 不存在的素材 → 全 False(安全缺省)
    assert not any(core.pending_processing("no_such_id").values())
    print("PASS test_pending_processing")


def test_auto_process_material_order_and_isolation():
    _mk_material("a1")
    calls = []
    restore, _ = _stub_pipeline(calls, fail_ocr=True)
    try:
        r = core.auto_process_material("a1")
    finally:
        restore()
    # 顺序:thumb → ocr → shots → phash
    assert calls == ["thumb", "ocr", "shots", "phash"], calls
    st = r["steps"]
    assert st["thumb"]["status"] == "ok", st
    assert st["ocr"]["status"] == "error" and "RuntimeError" in st["ocr"]["reason"], st
    assert st["shots"]["status"] == "ok" and st["phash"]["status"] == "ok", st
    # ocr 抛异常不中断:shots/phash 的 sidecar 仍然落盘
    assert os.path.isfile(core.shot_index_path("a1")), st
    assert os.path.isfile(core.phash_path("a1")), st
    # history 有 auto_process 审计(ok=3/4:thumb/shots/phash 成功,ocr 失败)
    hs = core.get_history(limit=10, target_id="a1")
    assert any(h["action"] == "auto_process" and h["detail"] == "ok=3/4" for h in hs), hs
    print("PASS test_auto_process_material_order_and_isolation")


def test_auto_process_all_pending_and_limit():
    _setup(reset=True)                        # 用例间隔离:清掉前例遗留的 pending 素材
    _mk_material("b1")
    _mk_material("b2")
    _mk_material("b3", kind="docs", name="d.md")
    calls = []
    restore, embed_calls = _stub_pipeline(calls)
    try:
        r = core.auto_process_all(limit=1)
        # 只处理 pending 的 videos/images;limit=1 → 只处理 1 条;docs 不参与
        assert r["processed"] == 1 and len(r["results"]) == 1, r
        assert r["results"][0]["id"] in ("b1", "b2"), r
        assert all(x["id"] != "b3" for x in r["results"]), r
        # build_embeddings 在整批最后被调一次(增量)
        assert len(embed_calls) == 1, embed_calls
        done = r["results"][0]["id"]
        assert not any(core.pending_processing(done).values()), done
        # 剩下那条 pending 素材补齐
        r2 = core.auto_process_all()
        assert r2["processed"] == 1, r2
        assert len(embed_calls) == 2, embed_calls
    finally:
        restore()
    print("PASS test_auto_process_all_pending_and_limit")


def test_all_done_then_zero_processed():
    _setup(reset=True)                        # 用例间隔离
    _mk_material("c1")
    calls = []
    restore, _e = _stub_pipeline(calls)
    try:
        assert any(core.pending_processing("c1").values())
        r1 = core.auto_process_all()
        assert r1["processed"] == 1, r1
        # 全部处理完 → pending 全 False
        assert core.pending_processing("c1") == {"thumb": False, "ocr": False,
                                                 "shots": False, "phash": False}
        # 再跑一次 → 幂等,处理 0 条
        r2 = core.auto_process_all()
        assert r2["processed"] == 0 and r2["results"] == [], r2
    finally:
        restore()
    print("PASS test_all_done_then_zero_processed")


if __name__ == "__main__":
    try:
        test_pending_processing()
        test_auto_process_material_order_and_isolation()
        test_auto_process_all_pending_and_limit()
        test_all_done_then_zero_processed()
    finally:
        _teardown()
    print("OK")
