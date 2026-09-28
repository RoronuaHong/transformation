"""重排(stage-2 cross-encoder)集成测试:不依赖真实 ollama rerank 模型,用 mock 验证
_ollama_rerank 的索引解析,以及 semantic_rank 真地把 stage-2 重排结果应用到了最终排序。"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core


def test_ollama_rerank_parses_indices():
    orig = core._http_json

    def fake(url, payload=None, timeout=90):
        assert url.endswith("/api/rerank"), url
        n = len(payload["documents"])
        # 模拟 cross-encoder:index 越大相关性越高 → 排序后应为逆序
        return {"results": [{"index": i, "relevance_score": float(i)}
                            for i in range(n)]}

    core._http_json = fake
    try:
        docs = ["a", "b", "c", "d"]
        assert core._ollama_rerank("q", docs, "fake-model") == [3, 2, 1, 0]
        # 未设模型 → None(调用方跳过重排)
        assert core._ollama_rerank("q", docs, "") is None
    finally:
        core._http_json = orig
    print("PASS test_ollama_rerank_parses_indices")


def test_semantic_rank_applies_rerank():
    orig_probe = core.embed_probe
    orig_load = core.load_vectors
    orig_emb = core.embed_texts
    orig_rr = core._ollama_rerank
    orig_env = os.environ.get("VITUAL_RERANK_MODEL")
    os.environ["VITUAL_RERANK_MODEL"] = "fake-model"   # 打开 stage-2 重排开关
    # 打桩:embedding 可用、所有向量相同(全部通过 min_cos 过滤)、query 命中
    core.embed_probe = lambda *a, **k: {"ok": True, "model": "m", "url": "", "err": ""}
    core.load_vectors = lambda ids, model: {i: [1.0, 0.0, 0.0] for i in ids}
    core.embed_texts = lambda texts: [[1.0, 0.0, 0.0]]
    core._ollama_rerank = lambda q, docs, model=None: list(range(len(docs) - 1, -1, -1))
    try:
        base = [{"id": f"m{i}", "name": f"n{i}", "kind": "docs", "rel_path": "",
                 "tags": "", "description": f"d{i}", "ai_tags": "", "size": 1,
                 "ext": "", "location": "", "external_path": ""} for i in range(4)]
        out, used = core.semantic_rank("q", base, base)
        ids = [m["id"] for m in out]
        assert ids == ["m3", "m2", "m1", "m0"], ids   # 被 stage-2 反转
        assert used is True
    finally:
        core.embed_probe = orig_probe
        core.load_vectors = orig_load
        core.embed_texts = orig_emb
        core._ollama_rerank = orig_rr
        if orig_env is None:
            os.environ.pop("VITUAL_RERANK_MODEL", None)
        else:
            os.environ["VITUAL_RERANK_MODEL"] = orig_env
    print("PASS test_semantic_rank_applies_rerank")


def test_semantic_rank_applies_lexical_rerank():
    # 内置离线 lexical reranker(VITUAL_RERANK_MODEL=lexical)也走同一条 stage-2 管线
    orig_probe = core.embed_probe
    orig_load = core.load_vectors
    orig_emb = core.embed_texts
    orig_env = os.environ.get("VITUAL_RERANK_MODEL")
    os.environ["VITUAL_RERANK_MODEL"] = "lexical"
    core.embed_probe = lambda *a, **k: {"ok": True, "model": "m", "url": "", "err": ""}
    core.load_vectors = lambda ids, model: {i: [1.0, 0.0, 0.0] for i in ids}
    core.embed_texts = lambda texts: [[1.0, 0.0, 0.0]]
    try:
        base = [
            {"id": "a", "name": "无关键字的条目", "kind": "docs", "rel_path": "",
             "tags": "", "description": "完全无关的画面内容", "ai_tags": "", "size": 1,
             "ext": "", "location": "", "external_path": ""},
            {"id": "b", "name": "去字幕结果", "kind": "docs", "rel_path": "",
             "tags": "type:output", "description": "去除字幕只留背景的输出", "ai_tags": "",
             "size": 1, "ext": "", "location": "", "external_path": ""},
        ]
        out, used = core.semantic_rank("去除字幕只留背景", base, base)
        ids = [m["id"] for m in out]
        assert ids == ["b", "a"], ids        # 离线 lexical reranker 把命中查询词的 b 顶上来
        assert used is True
    finally:
        core.embed_probe = orig_probe
        core.load_vectors = orig_load
        core.embed_texts = orig_emb
        if orig_env is None:
            os.environ.pop("VITUAL_RERANK_MODEL", None)
        else:
            os.environ["VITUAL_RERANK_MODEL"] = orig_env
    print("PASS test_semantic_rank_applies_lexical_rerank")


def test_chunk_search_splits_and_matches():
    # 父子分块:长 description 按段落切,词法命中子块返回父素材
    rows = core.chunk_search("去字幕", limit=5)
    assert isinstance(rows, list)          # 不抛异常即可(库内可能有/无命中)
    # _split_chunks 基本行为
    ch = core._split_chunks("第一句。第二句。第三句很长很长很长很长很长很长很长很长很长很长很长很长。", size=10)
    assert len(ch) >= 2, ch
    print("PASS test_chunk_search_splits_and_matches")


def test_chunk_score_reads_text_file():
    # 关联文本文件也应被分块检索(父子分块覆盖真实笔记/.md)
    import tempfile, os as _os
    fd, path = tempfile.mkstemp(suffix=".md")
    with _os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# 笔记\n这段代码实现了 dehardsub 去硬字幕的逻辑。\n第二段讲 facefix 人脸修复。")
    try:
        m = {"id": "x1", "name": "n", "kind": "docs", "rel_path": "", "tags": "",
             "description": "", "ai_tags": "", "size": 1, "ext": "", "location": "",
             "external_path": path}
        # 关键词命中文件内子块(库内 description 为空,纯靠文件内容)
        assert core.chunk_score(core._tokens("去硬字幕"), m) > 0
        # 无关词不应命中
        assert core.chunk_score(core._tokens("zzznotexist"), m) == 0
    finally:
        _os.remove(path)
    print("PASS test_chunk_score_reads_text_file")


def test_search_cache_persist(tmp=None):
    # 磁盘持久化缓存:开启 VITUAL_CACHE_PERSIST 后,search 结果应能落盘并被重新载入
    import tempfile, os as _os, pickle
    d = tempfile.mkdtemp()
    db = _os.path.join(d, "hub.db")  # 仅用于隔离缓存路径,实际用 core 的 INDEX_DIR
    old = core.os.environ.get("VITUAL_CACHE_PERSIST")
    oldp = core.os.environ.get("VITUAL_CACHE_SEARCH")
    core.os.environ["VITUAL_CACHE_PERSIST"] = "1"
    core.os.environ["VITUAL_CACHE_SEARCH"] = "1"
    # 把缓存路径指向临时文件,避免污染 index/
    core._CACHE_PATH = _os.path.join(d, "search_cache.pkl")
    core._SEARCH_CACHE = {}
    try:
        r1 = core.search("字幕", limit=3)
        assert _os.path.exists(core._CACHE_PATH), "缓存应已落盘"
        # 清空内存,从磁盘重载,二次检索应命中磁盘缓存且结果一致
        core._SEARCH_CACHE = core._cache_load()
        r2 = core.search("字幕", limit=3)
        assert [m["id"] for m in r1] == [m["id"] for m in r2]
    finally:
        core.os.environ.pop("VITUAL_CACHE_PERSIST", None)
        core.os.environ.pop("VITUAL_CACHE_SEARCH", None)
        if old is not None: core.os.environ["VITUAL_CACHE_PERSIST"] = old
        if oldp is not None: core.os.environ["VITUAL_CACHE_SEARCH"] = oldp
        core._SEARCH_CACHE = {}
    print("PASS test_search_cache_persist")


if __name__ == "__main__":
    test_ollama_rerank_parses_indices()
    test_semantic_rank_applies_rerank()
    test_chunk_search_splits_and_matches()
    test_chunk_score_reads_text_file()
    test_search_cache_persist()
    test_semantic_rank_applies_lexical_rerank()
    print("OK")
