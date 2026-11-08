# -*- coding: utf-8 -*-
"""派生数据孤儿扫描/清理回归测试(最佳实践:主记录删除须级联失效派生 sidecar/向量)。

用相对计数,避免依赖具体测试索引的既有派生数据量。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa


def test_orphan_purge_roundtrip():
    orphan_id = "verify_orphan_zzz"
    before = core.count_orphan_derived()  # 基线:造孤儿之前
    # 造孤儿:1) OCR sidecar(指向不存在的素材) 2) 向量行(best-effort)
    os.makedirs(core.OCR_DIR, exist_ok=True)
    op = core.ocr_sidecar_path(orphan_id)
    with open(op, "w", encoding="utf-8") as f:
        f.write("stale")
    con = core._con()
    vec_ok = False
    try:
        con.execute("DELETE FROM embeddings WHERE mid=?", (orphan_id,))
        con.execute("INSERT OR REPLACE INTO embeddings(mid,model,dim,sig,vec,updated_at)"
                    " VALUES(?,?,?,?,?,?)", (orphan_id, "t", 1, "s", b"\x00", "2026-11-08"))
        con.commit()
        vec_ok = True
    except Exception:
        vec_ok = False
    finally:
        con.close()

    mid = core.count_orphan_derived()  # 此刻已含刚造的孤儿
    created = mid - before
    assert created >= 1, "应至少检出 1 条孤儿(created=%d, before=%d)" % (created, before)

    removed = core.purge_orphan_derived(dry_run=False)
    assert removed >= created, "应清理 >= created 条孤儿(removed=%d created=%d)" % (removed, created)
    assert core.count_orphan_derived() == before, "清理后孤儿数应回到基线"

    if os.path.isfile(op):
        os.remove(op)
    print("PASS test_orphan_purge_roundtrip (vec_ok=%s created=%d removed=%d)" % (vec_ok, created, removed))


if __name__ == "__main__":
    test_orphan_purge_roundtrip()
