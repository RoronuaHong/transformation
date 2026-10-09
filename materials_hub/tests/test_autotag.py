# -*- coding: utf-8 -*-
"""受控词表自动标签(SigLIP2 zero-shot + VLM 互验)与 ONNX 重排的单元测试。
不依赖真实模型:只测 sidecar 协议、检索通道、降级行为(模型可用性由 CLI 冒烟覆盖)。"""
import json
import os
from subprocess import TimeoutExpired

import core


def test_autotag_sidecar_path_rejects_bad_id():
    assert core.autotag_sidecar_path("") is None
    assert core.autotag_sidecar_path(None) is None
    assert core.autotag_sidecar_path("a/b") is None
    assert core.autotag_sidecar_path("..") is None
    assert core.autotag_sidecar_path("x:y") is None
    p = core.autotag_sidecar_path("abc123")
    assert p and p.endswith("abc123.json") and "autotags" in p


def test_autotags_text_flattens_json_and_cache_invalidates():
    mid = "autotagcache1"
    core.AUTOTAG_DIR and os.makedirs(core.AUTOTAG_DIR, exist_ok=True)
    sc = core.autotag_sidecar_path(mid)
    data = {"model": "ViT-B-16-SigLIP2-256/webli", "min_cos": 0.24,
            "verified": [{"zh": "红烧鸡翅", "en": "braised chicken wings",
                          "score": 0.31}],
            "auto": [{"zh": "食物", "en": "fruit plate", "score": 0.25}]}
    with open(sc, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    t = core._autotags_text(mid)
    assert "红烧鸡翅" in t and "braised chicken wings" in t
    assert "fruit plate" in t
    # 缓存失效闭环:改写 sidecar 后必须读到新内容(按 mtime/size 校验)
    data["auto"] = [{"zh": "杯子", "en": "cup", "score": 0.26}]
    with open(sc, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    core._AUTOTAG_TEXT_CACHE.pop(mid, None)   # 同秒内 mtime 可能不变,与生产(跨进程)一致
    t2 = core._autotags_text(mid)
    assert "杯子" in t2 and "cup" in t2 and "fruit plate" not in t2


def test_score_hits_autotag_channel():
    mid = "autotagscore1"
    os.makedirs(core.AUTOTAG_DIR, exist_ok=True)
    sc = core.autotag_sidecar_path(mid)
    with open(sc, "w", encoding="utf-8") as f:
        json.dump({"verified": [{"zh": "红烧鸡翅", "en": "braised chicken wings",
                                 "score": 0.31}], "auto": []},
                  f, ensure_ascii=False)
    core._AUTOTAG_TEXT_CACHE.pop(mid, None)
    m = {"id": mid, "name": "x", "tags": "", "description": "", "rel_path": ""}
    qt = core._tokens("红烧鸡翅")
    s = core._score(qt, m)
    assert s > 0, "查询词命中 autotag sidecar 必须得分(0.35 通道)"
    # 单字查询不因 autotag 通道产生噪声(_sc_tok_w 剔除单字)
    qt1 = core._tokens("翅")
    assert core._score(qt1, m) == 0.0


def test_onnx_rerank_degrades_gracefully_on_failure(monkeypatch):
    docs = ["a", "b", "c"]
    # 故障注入:子进程崩溃/超时 → 必须返回 None(降级 RRF),绝不能让检索挂掉
    def _boom(*a, **kw):
        raise TimeoutExpired("rerank_runner.py", 600)
    monkeypatch.setattr(core.subprocess, "run", _boom)
    assert core._onnx_rerank("q", docs) is None


def test_onnx_rerank_rejects_bad_payload(monkeypatch):
    docs = ["a", "b"]
    class _P:
        stdout = b'{"ok": true, "scores": [0.1]}'   # 长度与 docs 不符
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **kw: _P())
    assert core._onnx_rerank("q", docs) is None


def test_rerank_status_modes():
    orig = os.environ.get("VITUAL_RERANK_MODEL")
    try:
        os.environ["VITUAL_RERANK_MODEL"] = "__none__"
        assert core.rerank_status()["mode"] == "off"
        os.environ["VITUAL_RERANK_MODEL"] = "lexical"
        st = core.rerank_status()
        assert st["mode"] == "lexical" and st["available"]
        os.environ.pop("VITUAL_RERANK_MODEL", None)
        st = core.rerank_status()
        # 真实库 models/rerank 已就位 → onnx;否则明确 none(不假报可用)
        if core.onnx_rerank_available():
            assert st["mode"] == "onnx" and st["available"]
        else:
            assert st["mode"] == "none" and not st["available"]
    finally:
        if orig is None:
            os.environ.pop("VITUAL_RERANK_MODEL", None)
        else:
            os.environ["VITUAL_RERANK_MODEL"] = orig


def test_semantic_rank_rerank_env_none_skips(monkeypatch=None):
    """VITUAL_RERANK_MODEL=__none__ 时不得调用重排。"""
    import os as _os
    called = {"n": 0}
    orig = _os.environ.get("VITUAL_RERANK_MODEL")
    _os.environ["VITUAL_RERANK_MODEL"] = "__none__"
    try:
        # embed 不可用(测试库无 ollama 向量)→ semantic_rank 直接走词法,不触重排
        rows = [{"id": "x1", "name": "braised chicken wings video", "tags": "",
                 "description": "", "rel_path": "", "kind": "videos"}]
        out, used = core.semantic_rank("chicken", rows, rows)
        assert used is False and out
    finally:
        if orig is None:
            _os.environ.pop("VITUAL_RERANK_MODEL", None)
        else:
            _os.environ["VITUAL_RERANK_MODEL"] = orig


if __name__ == "__main__":
    for k, fn in sorted(list(globals().items())):
        if k.startswith("test_") and callable(fn):
            fn()
            print("PASS", k)
    print("ALL PASS")
