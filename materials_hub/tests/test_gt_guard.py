# -*- coding: utf-8 -*-
"""GT 语料指纹守卫回归测试:指纹敏感性 / 分级 stale / 幂等 / 备份 / 护栏。

全程 monkeypatch 临时 GT 路径 + 假语料,不碰生产 GT、不连 ollama。
运行: python -m pytest tests/test_gt_guard.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402
import gt_guard  # noqa: E402
import gen_ground_truth as gg  # noqa: E402


def _ms(n=2):
    out = []
    for i in range(n):
        mid = "m%011d" % i
        name = ("旺财" if i == 0 else "橄榄油") + ".jpg"
        out.append({
            "id": mid, "sha256": chr(ord("a") + i) * 64, "size": 100 + i,
            "name": name, "orig_name": name, "kind": "images",
            "tags": "", "description": "", "ai_tags": "",
            "external_path": "", "rel_path": name,
        })
    return out


def _targets(tmp_path):
    tg = str(tmp_path / "eval_ground_truth.json")
    ig = str(tmp_path / "eval_image_gt.json")
    tm = str(tmp_path / "eval_ground_truth.meta.json")
    im = str(tmp_path / "eval_image_gt.meta.json")
    return (
        {"key": "text", "gt": tg, "meta": tm, "auto": True},
        {"key": "image", "gt": ig, "meta": im, "auto": False},
    )


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _setup(tmp_path, monkeypatch, ms=None, text_gt=None, image_gt=None):
    ms = _ms() if ms is None else ms
    targets = _targets(tmp_path)
    monkeypatch.setattr(gt_guard, "TARGETS", targets)
    monkeypatch.setattr(core, "all_materials", lambda: ms)
    # build_gt(verify=True) 会跑两次检索;mock 成「全库都召回得到」即可离线验证
    monkeypatch.setattr(core, "search",
                        lambda q, limit=20, mode="auto": [{"id": m["id"]} for m in ms])
    _write(targets[0]["gt"], text_gt if text_gt is not None
           else {"旺财": [_ms()[0]["id"]], "橄榄油": [_ms()[1]["id"]]})
    _write(targets[1]["gt"], image_gt if image_gt is not None
           else {"旺财": [_ms()[0]["id"]]})
    return ms, targets


def test_corpus_fingerprint_sensitive_to_corpus_not_tags():
    a = _ms()
    assert gt_guard.corpus_fingerprint(a) == gt_guard.corpus_fingerprint(list(reversed(a)))
    assert gt_guard.corpus_fingerprint(a) != gt_guard.corpus_fingerprint(a + [{
        "id": "new000000001", "sha256": "z" * 64, "size": 1, "name": "n.jpg",
        "orig_name": "n.jpg", "kind": "images"}])
    renamed = [dict(a[0], name="改名.jpg"), a[1]]
    assert gt_guard.corpus_fingerprint(a) != gt_guard.corpus_fingerprint(renamed)
    # autotag 改写 tags/description 不应触发「语料集合变化」(否则 CI 天天红)
    retagged = [dict(a[0], tags="pet,dog", description="一只狗"), a[1]]
    assert gt_guard.corpus_fingerprint(a) == gt_guard.corpus_fingerprint(retagged)
    assert gt_guard.text_fingerprint(a) != gt_guard.text_fingerprint(retagged)
    print("PASS test_corpus_fingerprint_sensitive_to_corpus_not_tags")


def test_status_legacy_without_meta_is_blocking(tmp_path, monkeypatch):
    ms, _ = _setup(tmp_path, monkeypatch)
    st = gt_guard.status(ms)
    assert st["blocking"] is True
    assert st["targets"]["text"]["state"] == "legacy", st
    assert any("基线" in r for r in st["targets"]["text"]["hard_reasons"])
    print("PASS test_status_legacy_without_meta_is_blocking")


def test_adopt_then_check_fresh(tmp_path, monkeypatch):
    ms, targets = _setup(tmp_path, monkeypatch)
    assert gt_guard.main(["--check"]) == 2          # 无基线 → CI 拦下
    assert gt_guard.main(["--adopt"]) == 0          # 只建基线,不改 GT
    assert json.load(open(targets[0]["gt"], encoding="utf-8")) == {
        "旺财": [_ms()[0]["id"]], "橄榄油": [_ms()[1]["id"]]}
    assert gt_guard.main(["--check"]) == 0
    assert gt_guard.status(ms)["blocking"] is False
    print("PASS test_adopt_then_check_fresh")


def test_corpus_growth_detected_even_when_all_old_ids_exist(tmp_path, monkeypatch):
    """核心盲区:新增素材但旧 id 都在 → gt_staleness 看不出,指纹必须报 stale。"""
    ms, _ = _setup(tmp_path, monkeypatch)
    gt_guard.main(["--adopt"])
    grown = ms + [{"id": "m99999999999", "sha256": "q" * 64, "size": 9,
                   "name": "urban.jpg", "orig_name": "urban.jpg", "kind": "images",
                   "tags": "", "description": "", "external_path": "",
                   "rel_path": "urban.jpg"}]
    monkeypatch.setattr(core, "all_materials", lambda: grown)
    st = gt_guard.status(grown)
    assert st["blocking"] is True and st["targets"]["text"]["state"] == "stale", st
    assert any("指纹变化" in r for r in st["targets"]["text"]["hard_reasons"])
    assert gt_guard.main(["--check"]) == 2
    print("PASS test_corpus_growth_detected_even_when_all_old_ids_exist")


def test_soft_text_change_is_warn_not_blocking(tmp_path, monkeypatch):
    ms, _ = _setup(tmp_path, monkeypatch)
    gt_guard.main(["--adopt"])
    retagged = [dict(ms[0], tags="pet,dog"), ms[1]]
    monkeypatch.setattr(core, "all_materials", lambda: retagged)
    st = gt_guard.status(retagged)
    assert st["blocking"] is False, st
    assert st["targets"]["text"]["state"] == "warn", st
    assert gt_guard.main(["--check"]) == 0
    print("PASS test_soft_text_change_is_warn_not_blocking")


def test_ci_skip_when_no_corpus(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(core, "all_materials", lambda: [])
    assert gt_guard.main(["--check", "--ci"]) == 0
    assert gt_guard.main(["--check"]) == 2     # 非 CI 模式仍严格
    print("PASS test_ci_skip_when_no_corpus")


def test_dry_never_writes(tmp_path, monkeypatch):
    ms, targets = _setup(tmp_path, monkeypatch)
    before = {t["gt"]: open(t["gt"], encoding="utf-8").read() for t in targets}
    assert gt_guard.main(["--dry"]) == 0
    for t in targets:
        assert open(t["gt"], encoding="utf-8").read() == before[t["gt"]]
        assert not os.path.exists(t["meta"])
    print("PASS test_dry_never_writes")


def test_apply_keeps_human_labels_backs_up_and_is_idempotent(tmp_path, monkeypatch):
    ms, targets = _setup(tmp_path, monkeypatch)
    human = {"人工标注的查询": [ms[1]["id"]], "旺财": [ms[0]["id"]]}
    _write(targets[0]["gt"], human)
    assert gt_guard.main(["--apply"]) == 0
    after = json.load(open(targets[0]["gt"], encoding="utf-8"))
    assert "人工标注的查询" in after, after        # 人工标注没被抹平
    assert after["人工标注的查询"] == [ms[1]["id"]], after
    assert os.path.isfile(targets[0]["meta"])
    backups = [f for f in os.listdir(gt_guard._backup_dir())]
    assert backups and any("eval_ground_truth.json" in f for f in backups), backups
    assert gt_guard.main(["--check"]) == 0
    # 幂等:再 apply 不应新增备份,也不改 GT
    _before = open(targets[0]["gt"], encoding="utf-8").read()
    n_before = len(os.listdir(gt_guard._backup_dir()))
    assert gt_guard.main(["--apply"]) == 0
    assert open(targets[0]["gt"], encoding="utf-8").read() == _before
    assert len(os.listdir(gt_guard._backup_dir())) == n_before
    print("PASS test_apply_keeps_human_labels_backs_up_and_is_idempotent")


def test_image_gt_only_prunes_dead_ids_never_invents_queries(tmp_path, monkeypatch):
    ms, targets = _setup(tmp_path, monkeypatch,
                         image_gt={"人工图像查询": [_ms()[0]["id"]],
                                   "全是死 id": ["deleted00001"]})
    # 语料增长触发 hard stale,让 --apply 处理两个 target
    grown = ms + [{"id": "m99999999999", "sha256": "q" * 64, "size": 9,
                   "name": "urban.jpg", "orig_name": "urban.jpg", "kind": "images",
                   "tags": "", "description": "", "external_path": "",
                   "rel_path": "urban.jpg"}]
    monkeypatch.setattr(core, "all_materials", lambda: grown)
    gt_guard.main(["--adopt"])
    monkeypatch.setattr(core, "all_materials", lambda: grown)
    assert gt_guard.main(["--apply"]) == 0
    img = json.load(open(targets[1]["gt"], encoding="utf-8"))
    assert "人工图像查询" in img and "全是死 id" not in img, img
    assert "urban" not in img, "图像 GT 不应自动新增查询: %s" % img
    print("PASS test_image_gt_only_prunes_dead_ids_never_invents_queries")


def test_status_rejects_non_dict_gt(tmp_path, monkeypatch):
    ms, targets = _setup(tmp_path, monkeypatch)
    _write(targets[0]["gt"], ["not", "a", "dict"])
    st = gt_guard.status(ms)
    assert st["targets"]["text"]["state"] == "missing", st
    assert st["blocking"] is True
    print("PASS test_status_rejects_non_dict_gt")
