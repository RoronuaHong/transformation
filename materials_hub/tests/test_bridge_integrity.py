"""桥接 / 外部引用完整性离线测试。

运行: python tests/test_bridge_integrity.py
- 对真实索引只读校验 health()/external_stats()(broken==0)
- 用 monkeypatch 验证 broken_externals 能识别缺失文件、prune 只删索引不删原文件
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

ORIG_ALL = core.all_materials
ORIG_CON = core._con


def test_health_ok_on_real_index():
    core.init_hub()
    h = core.health()
    st = core.external_stats()
    assert h["ok"] is True, h
    assert st["broken"] == 0, st
    print("  index total=%d external=%d broken=%d" % (h["total"], st["external"], st["broken"]))


def test_broken_externals_detects_missing():
    fake = [{"id": "real1", "location": "external",
             "external_path": core.__file__},   # 真实存在的文件,不应判失效
            {"id": "miss1", "location": "external", "external_path": "D:\\no\\such\\file_xyz.png"},
            {"id": "internal1", "location": "internal", "external_path": ""}]
    core.all_materials = lambda: fake
    try:
        broken = core.broken_externals()
        ids = {m["id"] for m in broken}
        assert "miss1" in ids, ids
        assert "real1" not in ids, ids      # 存在的文件不算失效
        assert "internal1" not in ids, ids  # 内部素材不检查
    finally:
        core.all_materials = ORIG_ALL


def test_prune_only_deletes_index():
    sqls = []
    removed_files = []

    class FakeConn:
        def execute(self, sql, params=None):
            sqls.append((sql, params))

        def executemany(self, sql, params=None):
            sqls.append((sql, params))

        def commit(self):
            pass

        def close(self):
            pass

    core._con = lambda: FakeConn()
    core.all_materials = lambda: [{"id": "miss1", "location": "external",
                                   "external_path": "D:\\no\\such\\file_xyz.png"}]
    real_remove = os.remove
    os.remove = lambda p: removed_files.append(p)  # 若有人删文件则记录
    try:
        broken = core.prune_broken_externals()
        assert any("DELETE FROM materials" in s[0] for s in sqls), sqls
        assert removed_files == [], "prune 不应删除原文件!"
        assert any("miss1" in str(p) for _, p in sqls), sqls
    finally:
        core._con = ORIG_CON
        core.all_materials = ORIG_ALL
        os.remove = real_remove


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
