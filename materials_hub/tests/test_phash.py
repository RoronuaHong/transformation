# -*- coding: utf-8 -*-
"""test_phash.py — pHash 近重复检测(dHash 感知哈希)的离线测试。

全 mock 驱动:库用临时文件,mock ffmpeg_path/subprocess.run,不跑真 ffmpeg、
不动真实索引与素材。复跑:python tests/test_phash.py
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core

_TMP = None


def _setup():
    """惰性初始化临时环境(直接跑与 pytest 均可用)。"""
    global _TMP
    if _TMP is None:
        _TMP = tempfile.mkdtemp(prefix="hub_phash_test_")
        core.INDEX_DB = os.path.join(_TMP, "hub.db")
        core.PHASH_DIR = os.path.join(_TMP, "phash")
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


def _write_hash(mid, hx):
    """手工预写一条 phash sidecar(16 位 hex)。"""
    _setup()
    os.makedirs(core.PHASH_DIR, exist_ok=True)
    with open(core.phash_path(mid), "w", encoding="utf-8") as f:
        f.write(hx)


def test_bytes_to_dhash_pure():
    # ① 纯函数:72 字节全 0 → 像素相等(不满足 左>右)→ 0
    assert core._bytes_to_dhash(bytes(72)) == 0
    # 手工构造:第 0 行递减(200,180,...,40)→ 每对 左>右 → 低 8 位全 1;
    # 第 1 行递增(0,10,20,...)→ 每对 左<右 → 对应 8 位全 0;其余行恒定 → 0
    data = bytearray(72)
    for c in range(9):
        data[c] = 200 - c * 20              # 递减行
        data[9 + c] = c * 10                # 递增行
    h = core._bytes_to_dhash(bytes(data))
    assert h is not None and (h & 0xFF) == 0xFF, hex(h)
    assert (h >> 8) & 0xFF == 0x00, hex(h)
    # 长度不足 72 字节 → None
    assert core._bytes_to_dhash(bytes(71)) is None
    assert core._bytes_to_dhash(b"") is None
    print("PASS test_bytes_to_dhash_pure")


def test_hamming():
    # ② 汉明距离
    assert core.hamming(0b1010, 0b0101) == 4
    assert core.hamming(0, 0) == 0
    assert core.hamming(0xFFFFFFFFFFFFFFFF, 0) == 64
    print("PASS test_hamming")


def test_phash_material_cached_no_subprocess():
    # ③ 缓存路径:预写 sidecar → cached,绝不能碰子进程
    _mk_material("p1")
    _write_hash("p1", "abcdef0123456789")

    def boom(*a, **kw):
        raise AssertionError("subprocess.run must not be called when cached")

    orig = (core.subprocess.run, core.ffmpeg_path)
    core.subprocess.run = boom
    core.ffmpeg_path = lambda: "ffmpeg"
    try:
        r = core.phash_material("p1")
    finally:
        core.subprocess.run, core.ffmpeg_path = orig
    assert r["status"] == "cached" and r["hash"] == "abcdef0123456789", r
    print("PASS test_phash_material_cached_no_subprocess")


def test_phash_material_guards():
    # ④ guards:非媒体 → skipped;无 ffmpeg → skipped;素材不存在 → error
    _mk_material("p2", kind="docs", name="d.md")
    r = core.phash_material("p2")
    assert r["status"] == "skipped" and "kind=" in r["reason"], r
    _mk_material("p3")
    orig = core.ffmpeg_path
    core.ffmpeg_path = lambda: None
    try:
        r2 = core.phash_material("p3")
    finally:
        core.ffmpeg_path = orig
    assert r2["status"] == "skipped" and r2["reason"] == "no_ffmpeg", r2
    r3 = core.phash_material("no_such_id")
    assert r3["status"] == "error" and r3["reason"] == "not_found", r3
    print("PASS test_phash_material_guards")


def test_similar_assets():
    # ⑤ 临时库放 3 个素材并手工写 sidecar:自身全 1、A 全 0(距离 64)、B 仅低 8 位不同(距离 8)
    _setup()
    os.makedirs(core.PHASH_DIR, exist_ok=True)
    for fn in os.listdir(core.PHASH_DIR):        # 隔离:清掉前序用例遗留的 sidecar
        os.remove(os.path.join(core.PHASH_DIR, fn))
    _mk_material("me")
    _mk_material("a1")
    _mk_material("b1")
    _write_hash("me", "ffffffffffffffff")
    _write_hash("a1", "0000000000000000")
    _write_hash("b1", "ffffffffffffff00")
    r = core.similar_assets("me", max_dist=10)
    assert r["status"] == "ok" and r["total"] == 2, r
    assert [s["id"] for s in r["similar"]] == ["b1"], r
    assert r["similar"][0]["dist"] == 8 and r["similar"][0]["name"], r
    # 无 sidecar → no_hash
    assert core.similar_assets("never_hashed")["status"] == "no_hash"
    print("PASS test_similar_assets")


def test_phash_path_injection():
    # ⑥ 防注入:含分隔符/冒号/空 id 拒绝生成 sidecar 路径
    _setup()
    assert core.phash_path("../evil") is None
    assert core.phash_path("") is None
    assert core.phash_path("a:b") is None
    p = core.phash_path("p0")
    assert p and p.endswith("p0.txt") and os.path.dirname(p) == core.PHASH_DIR, p
    print("PASS test_phash_path_injection")


def test_phash_material_ok_writes_sidecar():
    # 附加:ok 路径全流程(mock 72 字节灰度像素)——落盘 hex、ffmpeg 参数、history 审计
    _mk_material("p4")
    pixels = bytes(range(72))

    class _FakeProc:
        stdout = pixels
        stderr = b""
        returncode = 0

    calls = []
    orig = (core.subprocess.run, core.ffmpeg_path)
    core.subprocess.run = lambda cmd, **kw: (calls.append(cmd), _FakeProc())[1]
    core.ffmpeg_path = lambda: "ffmpeg"
    try:
        r = core.phash_material("p4")
    finally:
        core.subprocess.run, core.ffmpeg_path = orig
    assert r["status"] == "ok", r
    with open(core.phash_path("p4"), "r", encoding="utf-8") as f:
        hx = f.read().strip()
    assert hx == r["hash"] and len(hx) == 16, (hx, r)
    assert hx == f"{core._bytes_to_dhash(pixels):016x}", hx   # 落盘值与纯函数一致
    assert "-vf" in calls[0] and "scale=9:8,format=gray" in calls[0], calls
    hs = core.get_history(limit=5, target_id="p4")
    assert any(h["action"] == "phash" and h["detail"] == f"hash={hx}" for h in hs), hs
    print("PASS test_phash_material_ok_writes_sidecar")


if __name__ == "__main__":
    test_bytes_to_dhash_pure()
    test_hamming()
    test_phash_material_cached_no_subprocess()
    test_phash_material_guards()
    test_similar_assets()
    test_phash_path_injection()
    test_phash_material_ok_writes_sidecar()
    _teardown()
    print("OK")
