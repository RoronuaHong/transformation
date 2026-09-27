"""嵌入检索离线测试:验证 VITUAL_EMBED_MODEL 切换与任意维度(如 bge-m3 1024-d)。

运行: python tests/test_embed_alt.py
全程 monkeypatch 掉 _http_json / load_vectors,不连 ollama、不写 hub.db。
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

ORIG_HTTP = core._http_json
ORIG_CACHE = dict(core._EMBED_CACHE)


def _restore():
    core._http_json = ORIG_HTTP
    core._EMBED_CACHE.clear()
    core._EMBED_CACHE.update(ORIG_CACHE)


def test_probe_selects_named_model():
    os.environ["VITUAL_EMBED_MODEL"] = "bge-m3"
    core._EMBED_CACHE.update(probed=False)

    def fake(url, payload=None, timeout=90):
        if url.endswith("/api/tags"):
            return {"models": [
                {"name": "bge-m3:latest", "capabilities": ["embedding"]},
                {"name": "nomic-embed-text", "capabilities": ["embedding"]},
            ]}
        if url.endswith("/api/embed"):
            return {"embeddings": [[0.01] * 1024 for _ in range(len(payload["input"]))]}
        return ORIG_HTTP(url, payload, timeout)

    core._http_json = fake
    try:
        info = core.embed_probe(refresh=True)
        assert info["ok"] is True, info
        assert info["model"] == "bge-m3:latest", info["model"]
        vecs = core.embed_texts(["hello", "world"])
        assert vecs is not None and len(vecs) == 2 and len(vecs[0]) == 1024, len(vecs[0]) if vecs else None
    finally:
        _restore()


def test_semantic_rank_alt_dim():
    os.environ["VITUAL_EMBED_MODEL"] = "bge-m3"
    core._EMBED_CACHE.update(probed=True, ok=True, model="bge-m3:latest")

    mids = ["a", "b", "c", "d"]
    vecs = {m: [0.0] * 1024 for m in mids}
    vecs["b"] = [1.0 / math.sqrt(1024)] * 1024  # query "b" 的最近邻
    core.load_vectors = lambda mids_, model=None: {m: vecs[m] for m in mids_}

    def fake(url, payload=None, timeout=90):
        if url.endswith("/api/embed"):
            return {"embeddings": [[1.0 / math.sqrt(1024)] * 1024 for _ in range(len(payload["input"]))]}
        return ORIG_HTTP(url, payload, timeout)

    core._http_json = fake
    try:
        base = [{"id": m, "name": m, "tags": "", "description": "", "rel_path": ""} for m in mids]
        ranked, _ = core.semantic_rank("search_query: b", base, base)
        assert ranked and ranked[0]["id"] == "b", [r["id"] for r in ranked]
    finally:
        _restore()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fail = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            fail += 1
            print("FAIL", t.__name__, "->", e)
    print(f"\n{len(tests)-fail}/{len(tests)} passed")
    sys.exit(1 if fail else 0)
