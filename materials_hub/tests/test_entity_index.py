# -*- coding: utf-8 -*-
"""结构化实体倒排索引(Netflix §8 P1-1)单元测试。

不依赖真实模型/库:用临时 INDEX_DIR + AUTOTAG_DIR,monkeypatch all_materials,
验证 build_entity_inverted_index 建索引 + entity_search 的 AND/OR 组合约束。
"""
import json
import os

import core


MATS = [
    {"id": "e1", "name": "a", "tags": "sp,type:video", "description": ""},
    {"id": "e2", "name": "b", "tags": "sp", "description": ""},
    {"id": "e3", "name": "c", "tags": "鱼", "description": ""},
]


def _write_autotag(mid, verified, auto=None):
    os.makedirs(core.AUTOTAG_DIR, exist_ok=True)
    sc = core.autotag_sidecar_path(mid)
    with open(sc, "w", encoding="utf-8") as f:
        json.dump({"model": "x", "min_prob": 0.02,
                   "verified": verified, "auto": auto or []},
                  f, ensure_ascii=False)


def test_build_and_search_and_or(tmp_path, monkeypatch):
    core.INDEX_DIR = os.path.join(str(tmp_path), "index")
    core.AUTOTAG_DIR = os.path.join(str(tmp_path), "autotags")
    monkeypatch.setattr(core, "all_materials", lambda: MATS)
    _write_autotag("e1", [{"zh": "红烧鸡翅", "en": "braised chicken wings", "score": 0.3},
                          {"zh": "鸡翅", "en": "chicken wings", "score": 0.4}])
    _write_autotag("e2", [{"zh": "鸡翅", "en": "chicken wings", "score": 0.4}])
    # e3 实体来自 tags 字段精确命中 vocab("鱼")
    r = core.build_entity_inverted_index()
    assert r["available"] is True
    assert r["materials_with_entities"] == 3
    assert r["entities"] == 3  # 红烧鸡翅, 鸡翅, 鱼

    # 单实体
    res = core.entity_search("鸡翅")
    assert {x["id"] for x in res} == {"e1", "e2"}

    # AND:同时含 红烧鸡翅 + 鸡翅 → 仅 e1
    res = core.entity_search(["红烧鸡翅", "鸡翅"], op="and")
    assert [x["id"] for x in res] == ["e1"]

    # OR:并集 → e1, e2
    res = core.entity_search(["红烧鸡翅", "鸡翅"], op="or")
    assert {x["id"] for x in res} == {"e1", "e2"}

    # tags 字段实体:鱼 → e3
    res = core.entity_search("鱼")
    assert [x["id"] for x in res] == ["e3"]

    # 未知实体 → 空
    assert core.entity_search("不存在的实体xyz") == []


def test_load_missing_returns_none(tmp_path):
    core.INDEX_DIR = os.path.join(str(tmp_path), "index2")
    assert core.load_entity_inverted() is None


if __name__ == "__main__":
    import tempfile
    import types
    d = tempfile.mkdtemp()

    class _MP:
        def setattr(self, obj, name, val):
            setattr(obj, name, val)
    test_build_and_search_and_or(d, _MP())
    test_load_missing_returns_none(d)
    print("ALL PASS")
