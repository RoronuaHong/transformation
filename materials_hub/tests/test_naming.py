# -*- coding: utf-8 -*-
"""test_naming.py — 素材命名规范化(见 naming.py 的六条硬规则)。

覆盖:① 描述性(自解释、含中文阶段/语种)② 一致性(统一 `_`、无空格/特殊字符)
③ 唯一性(重名必须消歧)④ 保留原名 token(溯源 + 保住英文/跨语命中)
⑤ 幂等(重复规范化不叠加前缀)⑥ 只改显示名(磁盘文件与 rel_path/external_path 不动)。

全 mock 驱动(临时库),不碰真实索引。复跑:python tests/test_naming.py
"""
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import naming  # noqa: E402

_TMP = None


def _setup():
    global _TMP
    if _TMP is None:
        _TMP = tempfile.mkdtemp(prefix="hub_naming_")
        core.INDEX_DB = os.path.join(_TMP, "hub.db")
        core._init_db()


def _teardown():
    global _TMP
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP = None


def _mk(mid, kind, name, tags="", orig=None):
    """造一条 external 引用素材(文件在临时目录,删索引绝不碰真实素材)。"""
    _setup()
    f = os.path.join(_TMP, f"{mid}_{name}")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("x")
    orig = orig or name
    core.add_material(id=mid, kind=kind, ext=os.path.splitext(name)[1], name=name,
                      rel_path="", size=1, sha256="n" * 64, tags=tags, description="",
                      source="t", orig_name=orig, created_at="2026-01-01")
    core._update_material(mid, location="external", external_path=f)
    return core.get_material(mid)


def test_canonical_name_is_descriptive_and_keeps_original_token():
    """① 描述性 + ④ 保留原名 token:中文描述在前,英文原名 token 仍在名字里。"""
    n = naming.canonical_name("001_delogo.mp4", "silent",
                              tags="sp,job:BV1aDb56iEvu,type:render", mid="abc123456789")
    assert "去台标" in n, n             # 中文自解释
    assert "delogo" in n, n             # 原名 token 保留(保住英文查询命中 + 溯源)
    assert n.endswith(".mp4"), n        # 扩展名不变
    print("PASS test_canonical_name_is_descriptive_and_keeps_original_token ->", n)


def test_canonical_name_no_spaces_and_consistent_separator():
    """② 一致性:生成的各部分统一 `_` 分隔、无空格、无文件名不安全字符。

    注:原名词干**逐字符保留**(溯源 + 不丢 token,见 naming._safe_stem),
    故连字符只可能出现在被保留的原名词干里,不算风格不一致。
    """
    for raw in ("source_video-f30280.m4a", "_clean_sttn_writing.mp4", "zh-Hant.srt", "in_00.mp4"):
        n = naming.canonical_name(raw, "videos", tags="sp,job:abcdef123456", mid="z" * 12)
        assert " " not in n, (raw, n)
        assert not n.startswith("_"), (raw, n)
        for ch in '\\/:*?"<>|':
            assert ch not in n, (raw, n, ch)
        # 原名词干逐字符保留
        assert naming._safe_stem(os.path.splitext(raw)[0]) in n, (raw, n)
    print("PASS test_canonical_name_no_spaces_and_consistent_separator")


def test_canonical_name_language_from_tag():
    """语种走受控 lang: 标签;字幕类文件名本身是语种码时也能识别(含 zh-Hant)。"""
    n1 = naming.canonical_name("zh.srt", "subs", tags="sp,lang:zh,job:aaaaaa111111", mid="m" * 12)
    assert "中文" in n1, n1
    n2 = naming.canonical_name("zh-Hant.srt", "subs", tags="sp,job:aaaaaa111111", mid="m" * 12)
    assert "繁体中文" in n2, n2          # 键是 zh-hant,stem 归一后是 zh_Hant,须还原连字符命中
    print("PASS test_canonical_name_language_from_tag ->", n1, "|", n2)


def test_plan_renames_makes_every_name_unique():
    """③ 唯一性:同名的 21 条 media_status.json 必须被消歧成 21 个不同名字。"""
    mats = [{"id": "id%02d" % i, "kind": "docs", "name": "media_status.json",
             "orig_name": "media_status.json", "tags": "sp,job:job%02d" % (i % 3),
             "description": ""} for i in range(21)]
    plan = naming.plan_renames(mats)
    names = [n for _, n in plan]
    assert len(set(names)) == len(names), "仍有重名: %s" % (len(names) - len(set(names)))
    print("PASS test_plan_renames_makes_every_name_unique -> 21 -> %d unique" % len(set(names)))


def test_normalize_is_idempotent():
    """⑤ 幂等:基于 orig_name 推导,重复规范化不会不断叠加中文前缀。"""
    _mk("i1", "silent", "001_delogo.mp4", tags="sp,job:BV1aDb56iEvu")
    n1 = core.normalize_name("i1")
    n2 = core.normalize_name("i1")      # 再跑一次
    assert n1 == n2, (n1, n2)
    assert n1.count("去台标") == 1, n1   # 前缀没有被叠两层
    print("PASS test_normalize_is_idempotent ->", n1)


def test_normalize_only_touches_display_name():
    """⑥ 只改显示名:name 变了,orig_name/rel_path/external_path/磁盘文件一律不动。"""
    m = _mk("d1", "silent", "clean.mp4", tags="sp,job:BBBBBB111111")
    before = dict(m)
    disk = m["external_path"]
    assert os.path.isfile(disk)
    new = core.normalize_name("d1")
    after = core.get_material("d1")
    assert after["name"] == new and new != before["name"], (before["name"], new)
    assert after["orig_name"] == before["orig_name"], "orig_name(真实文件名)被改了"
    assert after["external_path"] == before["external_path"], "external_path 被改了"
    assert after["rel_path"] == before["rel_path"], "rel_path 被改了"
    assert os.path.isfile(disk), "磁盘文件被删/移了(违反安全清理铁律)"
    print("PASS test_normalize_only_touches_display_name ->", before["name"], "->", new)


def test_ingest_registers_canonical_name():
    """未来入库自动命名:scan/ingest 注册后显示名即规范化(不需手工再跑 rename)。"""
    _setup()
    kind_dir = os.path.join(core.MATERIALS if hasattr(core, "MATERIALS") else _TMP, "videos")
    os.makedirs(kind_dir, exist_ok=True)
    p = os.path.join(kind_dir, "naming_scan_src.mp4")
    with open(p, "wb") as f:
        f.write(b"\x00" * 16)
    core.scan_materials()
    rows = [m for m in core.all_materials() if "naming_scan_src" in (m.get("orig_name") or "")]
    assert rows, "scan 未登记该文件"
    assert rows[0]["name"] != rows[0]["orig_name"], "入库后未自动规范化显示名"
    assert rows[0]["rel_path"] and os.path.isfile(os.path.join(core.HUB, rows[0]["rel_path"]))
    print("PASS test_ingest_registers_canonical_name ->", rows[0]["name"])
    _teardown()


if __name__ == "__main__":
    test_canonical_name_is_descriptive_and_keeps_original_token()
    test_canonical_name_no_spaces_and_consistent_separator()
    test_canonical_name_language_from_tag()
    test_plan_renames_makes_every_name_unique()
    test_normalize_is_idempotent()
    test_normalize_only_touches_display_name()
    test_ingest_registers_canonical_name()
    print("test_naming: 7/7 green")
