# -*- coding: utf-8 -*-
"""素材版本管理(维度 J)回归测试:结构化写前快照 + 精确回滚 + 护栏。

conftest 自动隔离临时 DB,每个测试从空库开始。
运行: python -m pytest tests/test_versions.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402


def _mk(mid="v1", tags="", description="", ai_tags=""):
    core.add_material(id=mid, kind="images", ext=".jpg", name=mid + ".jpg",
                      rel_path="", size=1, sha256="a" * 64, tags=tags,
                      description=description, source="", orig_name=mid + ".jpg",
                      created_at="2026-01-01")
    if ai_tags:
        core._update_material(mid, ai_tags=ai_tags)
    # 清掉 _mk 自身可能产生的版本,让每个用例从确定的基线开始
    con = core._con()
    con.execute("DELETE FROM versions")
    con.commit()
    con.close()
    return core.get_material(mid)


def test_tag_change_creates_structured_snapshot():
    """可编辑元数据变更前存整行写前快照,且审计与快照在同一事务。"""
    _mk(tags="old")
    r = core.update_tags("v1", "new")
    assert r["ok"] and r["version_id"], r
    vs = core.list_versions("v1")
    assert len(vs) == 1 and vs[0]["action"] == "update_tags", vs
    v = core.get_version(r["version_id"])
    assert v["payload"]["tags"] == "old", v["payload"]
    assert isinstance(v["payload"], dict) and "description" in v["payload"]
    hist = core.get_history(target_id="v1")
    assert any(h["action"] == "update_tags" and "->" in h["detail"] for h in hist), hist
    print("PASS test_tag_change_creates_structured_snapshot")


def test_description_change_creates_version():
    _mk(description="before")
    r = core.update_description("v1", "after")
    assert r["ok"] and r["version_id"], r
    assert core.get_version(r["version_id"])["payload"]["description"] == "before"
    print("PASS test_description_change_creates_version")


def test_derived_fields_and_noop_do_not_create_versions():
    """派生字段(location/external_path/size)与无变化写入都不该灌版本表。"""
    _mk(tags="x")
    core._update_material("v1", location="external", external_path="/tmp/x")
    assert core.list_versions("v1") == [], core.list_versions("v1")
    r = core._versioned_update("v1", {"tags": "x"})
    assert r["ok"] and not r["changed"] and r["version_id"] is None, r
    assert core.list_versions("v1") == []
    print("PASS test_derived_fields_and_noop_do_not_create_versions")


def test_tags_and_ai_tags_written_atomically_as_one_version():
    """purge_ai_tags 与 tags 是同一事务、只产生一个版本(旧版是两次独立 UPDATE)。"""
    _mk(tags="", ai_tags="ghost,keep")
    r = core.update_tags("v1", "keep")
    m = core.get_material("v1")
    assert m["tags"] == "keep" and m["ai_tags"] == "keep", m
    assert len(core.list_versions("v1")) == 1, core.list_versions("v1")
    print("PASS test_tags_and_ai_tags_written_atomically_as_one_version")


def test_restore_dry_run_is_readonly():
    """回滚默认 dry-run,绝不写库(CLI --apply / MCP confirm 才放行)。"""
    _mk(tags="original")
    core.update_tags("v1", "changed")
    vid = core.list_versions("v1")[0]["id"]
    d = core.restore_version(vid)
    assert d["ok"] and d["dry_run"] and d["changed"], d
    assert d["fields"]["tags"]["restore"] == "original", d
    assert core.get_material("v1")["tags"] == "changed"
    assert len(core.list_versions("v1")) == 1
    print("PASS test_restore_dry_run_is_readonly")


def test_restore_applies_and_is_itself_reversible():
    """真正回滚恢复快照,并把回滚前状态存成新版本 → 回滚可再回滚,历史不丢。"""
    _mk(tags="original", description="d0")
    core.update_tags("v1", "changed")
    core.update_description("v1", "d1")
    vid = core.list_versions("v1")[-1]["id"]      # 最早那条(tags=original,d0)
    r = core.restore_version(vid, actor="cli", dry_run=False)
    assert r["ok"] and r["changed"] and r["version_id"], r
    m = core.get_material("v1")
    assert m["tags"] == "original" and m["description"] == "d0", m
    # 回滚前状态(d1/changed)被存成新版本,可反向回滚
    new_vid = core.list_versions("v1")[0]["id"]
    assert new_vid != vid
    assert core.get_version(new_vid)["payload"]["description"] == "d1"
    r2 = core.restore_version(new_vid, dry_run=False)
    assert r2["ok"] and core.get_material("v1")["description"] == "d1"
    assert any(h["action"] == "restore_version" for h in core.get_history(target_id="v1"))
    print("PASS test_restore_applies_and_is_itself_reversible")


def test_restore_missing_version_is_safe():
    assert core.restore_version(999999)["ok"] is False
    assert core.get_version(999999) is None
    assert core.version_diff(999999) is None
    print("PASS test_restore_missing_version_is_safe")


def test_version_diff_and_stats_and_prune():
    _mk(tags="a")
    core.update_tags("v1", "b")
    core.update_tags("v1", "c")
    vid = core.list_versions("v1")[-1]["id"]
    d = core.version_diff(vid)
    assert d["tags"]["version"] == "a" and d["tags"]["current"] == "c", d
    st = core.version_stats()
    assert st["versions"] == 2 and st["materials_with_versions"] == 1, st
    dry = core.prune_versions("v1", keep=1)
    assert dry["dry_run"] and dry["would_delete"] == 1 and dry["deleted"] == 0, dry
    real = core.prune_versions("v1", keep=1, dry_run=False)
    assert real["deleted"] == 1 and len(core.list_versions("v1")) == 1, real
    print("PASS test_version_diff_and_stats_and_prune")


if __name__ == "__main__":
    test_tag_change_creates_structured_snapshot()
    test_description_change_creates_version()
    test_derived_fields_and_noop_do_not_create_versions()
    test_tags_and_ai_tags_written_atomically_as_one_version()
    test_restore_dry_run_is_readonly()
    test_restore_applies_and_is_itself_reversible()
    test_restore_missing_version_is_safe()
    test_version_diff_and_stats_and_prune()
    print("OK")
