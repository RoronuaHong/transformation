# -*- coding: utf-8 -*-
"""上传入库最佳实践:可执行文件拒收 / 来源标签 / sha256 去重 / pending_map。"""
import os

import core


def _make(name, data=b"hello hub"):
    os.makedirs(core.INGEST, exist_ok=True)
    p = os.path.join(core.INGEST, name)
    with open(p, "wb") as f:
        f.write(data)
    return p


def test_ingest_file_rejects_executable():
    src = _make("tool.exe", b"MZ fake exe")
    r = core.ingest_file(src, move=True)
    assert r and r["status"] == "rejected" and r["reason"] == "blocked_ext"
    # 拒收件移入 TRASH,不残留 ingest 目录(否则 /api/ingest 重扫会反复处理)
    assert not os.path.exists(src)
    assert os.path.isdir(core.TRASH)


def test_ingest_file_upload_tag_and_dedup():
    src = _make("note.txt", b"hello hub")
    r1 = core.ingest_file(src, move=True, source="upload")
    assert r1 and r1["status"] == "added"
    m = core.get_material(r1["id"])
    assert "upload" in (m.get("tags") or [])
    # 相同内容再次上传 → duplicate,且指回已有条目(sha256 去重铁律)
    src2 = _make("note_copy.txt", b"hello hub")
    r2 = core.ingest_file(src2, move=True, source="upload")
    assert r2 and r2["status"] == "duplicate" and r2["id"] == r1["id"]


def test_pending_map_excludes_docs():
    # docs 类素材不进 pending(画面类派生数据不适用);空库/纯文档库 → {}
    assert core.pending_map() == {}
    src = _make("a.txt", b"x")
    r = core.ingest_file(src, move=True, source="upload")
    assert r and r["status"] == "added"
    assert core.pending_map() == {}
