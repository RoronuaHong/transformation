# -*- coding: utf-8 -*-
"""test_tech.py — 技术元数据 + 治理报告计数。

技术元数据(Cloudinary MAM 2026 / Adobe AEM「技术类元数据」):时长/分辨率/编码/帧率/采样率。
要点:① 派生数据只落 sidecar(可重建)② 非媒体类跳过 ③ 幂等(cached)
④ 无 ffprobe 时优雅跳过 ⑤ 绝不写 description(该列参与主排序,写派生文本会稀释排序)。

治理报告:`counts` 必须是**真实总数**(截断前统计)——曾因先截断再计数,
limit=0 时四类全报 0,把「缺描述 305 条」这类真问题完全掩盖。

全 mock 驱动(临时库 + 临时 sidecar),不碰真实索引。复跑:python tests/test_tech.py
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

_TMP = None


def _setup():
    global _TMP
    if _TMP is None:
        _TMP = tempfile.mkdtemp(prefix="hub_tech_")
        core.INDEX_DB = os.path.join(_TMP, "hub.db")
        core.TECH_DIR = os.path.join(_TMP, "tech")
        core.OCR_DIR = os.path.join(_TMP, "ocr")
        core._init_db()


def _teardown():
    global _TMP
    if _TMP:
        shutil.rmtree(_TMP, ignore_errors=True)
        _TMP = None


def _mk(mid, kind, name="a.mp4", tags="", desc=""):
    _setup()
    f = os.path.join(_TMP, f"{mid}_{name}")
    with open(f, "wb") as fh:
        fh.write(b"\x00" * 32)
    core.add_material(id=mid, kind=kind, ext=os.path.splitext(name)[1], name=name,
                      rel_path="", size=32, sha256="t" * 64, tags=tags, description=desc,
                      source="t", orig_name=name, created_at="2026-01-01")
    core._update_material(mid, location="external", external_path=f)
    return core.get_material(mid)


def test_tech_sidecar_roundtrip():
    """① 落 sidecar 且可读回;路径约定 index/tech/<id>.json。"""
    _mk("t1", "videos")
    assert core.tech_path("t1").endswith(os.path.join("tech", "t1.json"))
    assert core.read_tech("t1") == {}
    os.makedirs(core.TECH_DIR, exist_ok=True)
    with open(core.tech_path("t1"), "w", encoding="utf-8") as f:
        json.dump({"width": 1920, "height": 1080, "duration": 12.5}, f)
    t = core.read_tech("t1")
    assert t["width"] == 1920 and t["height"] == 1080 and t["duration"] == 12.5, t
    print("PASS test_tech_sidecar_roundtrip")


def test_tech_skips_non_media_kind():
    """② 文档/字幕等非媒体类不采集(ffprobe 无意义)。"""
    _mk("t2", "docs", name="d.json")
    r = core.tech_material("t2")
    assert r["status"] == "skipped" and r["reason"] == "kind_not_media", r
    print("PASS test_tech_skips_non_media_kind")


def test_tech_is_idempotent_cached():
    """③ 幂等:已有 sidecar 且未 force → cached,不重复探测。"""
    _mk("t3", "videos")
    os.makedirs(core.TECH_DIR, exist_ok=True)
    with open(core.tech_path("t3"), "w", encoding="utf-8") as f:
        json.dump({"width": 640}, f)
    r = core.tech_material("t3")
    assert r["status"] == "cached" and r["tech"]["width"] == 640, r
    print("PASS test_tech_is_idempotent_cached")


def test_probe_media_without_ffprobe():
    """④ 无 ffprobe 时返回 {} 并优雅跳过(不抛错、不写坏数据)。"""
    _mk("t4", "videos")
    orig = core._ffprobe_path
    try:
        core._ffprobe_path = lambda: None
        assert core._probe_media(core._material_abs_path(core.get_material("t4"))) == {}
        r = core.tech_material("t4", force=True)
        assert r["status"] == "skipped", r
    finally:
        core._ffprobe_path = orig
    print("PASS test_probe_media_without_ffprobe")


def test_tech_never_writes_description():
    """⑤ 绝不写 description(参与主排序,写派生文本会稀释排序——OCR/命名两次教训)。"""
    _mk("t5", "videos", desc="原始人工描述")
    os.makedirs(core.TECH_DIR, exist_ok=True)
    with open(core.tech_path("t5"), "w", encoding="utf-8") as f:
        json.dump({"width": 1280}, f)
    core.tech_material("t5")            # cached 路径
    assert core.get_material("t5")["description"] == "原始人工描述"
    assert not os.path.exists(os.path.join(core.TECH_DIR, "t5.json.bak"))
    print("PASS test_tech_never_writes_description")


def test_governance_counts_are_true_totals_not_samples():
    """治理审计:`counts` 必须是截断前的真实总数(否则 limit=0 时四类全 0,问题被掩盖)。"""
    _teardown()          # 隔离:清掉前面用例留下的素材,否则计数会串
    _setup()
    for i in range(7):
        _mk("g%d" % i, "videos", name="v%d.mp4" % i)   # 7 条都无描述
    r = core.governance_report(limit=2)
    assert r["counts"]["missing_desc"] == 7, r["counts"]     # 真实总数,不是样本数 2
    assert len(r["issues"]["missing_desc"]) == 2, r["issues"]  # issues 才是样本
    assert r["truncated"]["missing_desc"] is True, r
    r0 = core.governance_report(limit=0)
    assert r0["counts"]["missing_desc"] == 7, r0["counts"]   # limit=0 也不能归零
    print("PASS test_governance_counts_are_true_totals_not_samples")
    _teardown()


def test_describe_never_overwrites_human_description():
    """描述生成只补空描述,绝不覆盖人工/上游已有描述。"""
    _mk("d1", "docs", name="x.json", desc="人工写的描述")
    assert core.describe_material("d1") == "人工写的描述"
    assert core.get_material("d1")["description"] == "人工写的描述"
    print("PASS test_describe_never_overwrites_human_description")


def test_describe_without_stage_has_no_generic_chinese():
    """安全变体(with_stage=False):描述只含技术事实/项目的拉丁与数字,**不含中文**。

    实测:中文阶段词写进 description 会让门禁 r20 0.77→0.76(FAIL)——
    它们在 name 里已以 3.0 权重命中,再以 1.5 权重重复计入会改变相对排序。
    故自动描述刻意只用不会与中文查询碰撞的 latin/数字。
    """
    _mk("d2", "videos")
    os.makedirs(core.TECH_DIR, exist_ok=True)
    with open(core.tech_path("d2"), "w", encoding="utf-8") as f:
        json.dump({"width": 1920, "height": 1080, "codec": "h264", "duration": 12.5}, f)
    d = core.describe_material("d2", dry_run=True, with_stage=False)
    assert d, d
    assert not any("\u4e00" <= c <= "\u9fff" for c in d), d   # 无中文 → 不与中文查询碰撞
    assert "1920x1080" in d and "h264" in d, d
    print("PASS test_describe_without_stage_has_no_generic_chinese ->", d)
    _teardown()


if __name__ == "__main__":
    test_tech_sidecar_roundtrip()
    test_tech_skips_non_media_kind()
    test_tech_is_idempotent_cached()
    test_probe_media_without_ffprobe()
    test_tech_never_writes_description()
    test_governance_counts_are_true_totals_not_samples()
    test_describe_never_overwrites_human_description()
    test_describe_without_stage_has_no_generic_chinese()
    print("test_tech: 8/8 green")
