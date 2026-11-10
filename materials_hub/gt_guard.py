# -*- coding: utf-8 -*-
"""GT 语料指纹守卫:检测语料变更并(显式授权后)自动重标检索 GT。

背景(2026-11-10,最佳实践维度 J 续):
  旧链路只有 `eval_search.gt_staleness()` 的**标注 id 存活率**检测 —— 它看不见
  「旧 id 全都还在、但语料新增/改名/内容变了」的情形,门禁会拿过期 GT 报出误导性的
  PASS。而且检测到 stale 时只能打印 SKIP + 让人手动跑 `gen_ground_truth.py`,
  全仓没有任何代码自动调用生成器,语料一变就长期休眠。

本模块补上两件事:
  1. **语料指纹基线**(`<gt>.meta.json` sidecar):记录生成 GT 时的素材集合指纹。
     不塞进 GT 文件本体 —— `eval_search` / `gen_ground_truth` 都直接遍历 `gt.items()`,
     加元数据键会被当成一条查询(这类「给数据文件加字段」的坑见分析文档 §27)。
  2. **分级 stale 判定 + 自动重标钩子**:
     - hard(阻塞 CI):语料指纹变化 / 条数变化 / 无 meta 基线 / GT id 大量失效;
     - soft(仅告警):可检索文本(tags/description)被 autotag 改写 —— 这类变化频繁,
       不该让 CI 天天红,但 `--apply` 会顺带重标并刷新基线。

用法:
    python gt_guard.py --check           # 只读;fresh=0, stale=2(CI 用,绝不改文件)
    python gt_guard.py --check --ci      # 同上,但无语料库(0 条)时明确 SKIP 返回 0
    python gt_guard.py --dry             # 预览重标会写入什么(离线快速,不写文件)
    python gt_guard.py --apply           # 真重标:备份 → 原子写 → 刷新指纹基线
    python gt_guard.py --adopt           # 存量迁移:只建立指纹基线,不改任何 GT
    python gt_guard.py --watch [--interval 60] [--apply]
        # 常驻监听语料指纹:变化时提示 `python gt_guard.py --apply` 重标;
        # 加 --apply 则在硬失配时自动重标。每轮只看指纹(廉价,不跑检索),Ctrl-C 退出


安全护栏(GT 是人工质量基准,绝不能被静默覆写):
  * 只有显式 `--apply` 才写;CI 只跑 `--check`,**永远不改仓库里的 GT**;
  * `--apply` 前把旧 GT 备份到 index/gt_backups/,写入走 `.tmp` + os.replace 原子替换;
  * 文本 GT 复用 `gen_ground_truth.merged_gt()` 的同一套合并语义(保留仍有效的人工
    标注、只补新查询、丢弃死 id),不会把人工标注抹平成机器派生值;
  * 图像 GT 需 CLIP 人工核对,**不自动新增查询**,只裁剪已失效 id 并对齐基线;
  * 幂等:fresh 的 target 直接跳过,重复 `--apply` 不会反复备份/改写。

门禁联动:`eval_search.py --gate` 在 GT_STALE 分支提示本脚本;MCP `gt_guard_status`
可让 Agent 只读查询一致性状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TEXT_GT = os.path.join(HERE, "eval_ground_truth.json")
IMAGE_GT = os.path.join(HERE, "eval_image_gt.json")
META_SCHEMA = 1


def _backup_dir():
    # 动态取值:测试/conftest 会重定向 core.INDEX_DIR,导入期快照会落回真实 index
    # (core.py 曾有同类「导入期路径快照」缺陷,勿重蹈)。
    return os.path.join(core.INDEX_DIR, "gt_backups")

# 指纹只取**稳定字段**:describe/autotag 会改写 tags/description/ai_tags,
# 若纳入集合指纹会让每次自动打标都触发「语料变更」(误报)。那些字段另算 text_hash。
_STABLE_KEYS = ("id", "sha256", "size", "name", "orig_name", "kind")


def _meta_path(gt_path):
    stem, ext = os.path.splitext(gt_path)
    return stem + ".meta" + ext


TARGETS = (
    {"key": "text", "gt": TEXT_GT, "meta": _meta_path(TEXT_GT), "auto": True},
    {"key": "image", "gt": IMAGE_GT, "meta": _meta_path(IMAGE_GT), "auto": False},
)


def _load_json(path):
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _digest(parts):
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def corpus_fingerprint(ms):
    """素材集合/文件本体指纹:新增、删除、改名、内容变化都会变。"""
    rows = sorted(ms, key=lambda m: str(m.get("id") or ""))
    return _digest("|".join(str(m.get(k, "") or "") for k in _STABLE_KEYS)
                   for m in rows)


def text_fingerprint(ms):
    """可检索文本指纹(soft 信号):gen_ground_truth.blob 口径。"""
    from gen_ground_truth import blob
    rows = sorted(ms, key=lambda m: str(m.get("id") or ""))
    return _digest(str(m.get("id") or "") + ":" + blob(m) for m in rows)


def _id_staleness(target, gt, ms):
    """复用两条门禁各自的失配判定,避免本模块另立一套阈值。"""
    try:
        if target == "text":
            import eval_search
            return eval_search.gt_staleness(gt or {}, corpora=ms)
        import eval_image
        return eval_image.gt_staleness(gt or {}, corpora=ms)
    except Exception as e:
        return {"stale": False, "note": "staleness check unavailable: %s" % e,
                "coverage": None, "empty_queries": 0}


def status(ms=None):
    """只读:返回各 GT 的 freshness 状态。MCP `gt_guard_status` 直接调用本函数。

    返回 {corpus_hash, text_hash, materials, blocking(bool), targets:{key: {...}}}
    state ∈ fresh / warn(soft) / stale(hard) / legacy(无基线) / missing(无 GT)。
    """
    if ms is None:
        ms = core.all_materials()
    ch, th = corpus_fingerprint(ms), text_fingerprint(ms)
    out = {"corpus_hash": ch, "text_hash": th, "materials": len(ms),
           "blocking": False, "targets": {}}
    for t in TARGETS:
        raw_gt = _load_json(t["gt"])
        gt = raw_gt if isinstance(raw_gt, dict) else None
        meta = _load_json(t["meta"])
        hard, soft = [], []
        if not gt:
            hard.append("GT 文件缺失、不可读或不是 {查询: id列表} 对象")
        if not meta:
            hard.append("无指纹基线(legacy GT;先跑 --adopt 或 --apply 建立基线)")
        else:
            if meta.get("corpus_hash") != ch:
                hard.append("语料指纹变化 %s -> %s" % (meta.get("corpus_hash"), ch))
            if meta.get("materials") != len(ms):
                hard.append("素材条数变化 %s -> %d" % (meta.get("materials"), len(ms)))
            if meta.get("text_hash") != th:
                soft.append("可检索文本变化(autotag/describe),建议重标")
        st = _id_staleness(t["key"], gt, ms)
        if st.get("stale"):
            hard.append("GT 标注 id 大量失效(coverage=%s, empty_queries=%s)" % (
                st.get("coverage"), st.get("empty_queries")))
        if not gt:
            state = "missing"
        elif not meta:
            state = "legacy"
        elif hard:
            state = "stale"
        elif soft:
            state = "warn"
        else:
            state = "fresh"
        out["targets"][t["key"]] = {
            "state": state, "gt_file": os.path.basename(t["gt"]),
            "meta_file": os.path.basename(t["meta"]),
            "queries": len(gt) if isinstance(gt, dict) else 0,
            "auto_rebaseline": t["auto"], "hard_reasons": hard, "soft_reasons": soft,
            "id_staleness": st,
        }
        if state in ("stale", "legacy", "missing"):
            out["blocking"] = True
    return out


def _backup(path):
    if not os.path.isfile(path):
        return None
    bdir = _backup_dir()
    os.makedirs(bdir, exist_ok=True)
    dst = os.path.join(bdir, "%s.%s.bak" % (
        os.path.basename(path), time.strftime("%Y%m%d-%H%M%S", time.gmtime())))
    shutil.copy2(path, dst)
    return dst


def _atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _write_meta(t, merged, ch, th, ms, note=""):
    _atomic_write_json(t["meta"], {
        "schema": META_SCHEMA, "gt_file": os.path.basename(t["gt"]),
        "corpus_hash": ch, "text_hash": th, "materials": len(ms),
        "queries": len(merged) if isinstance(merged, dict) else 0,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "generator": "gt_guard.py", "note": note,
    })


def _rebaseline(t, gt, ms, ch, th, force=False, verify=True):
    """重标单个 target。返回 {action, queries, backup, mode, note}。"""
    from gen_ground_truth import merged_gt, valid_existing
    if t["auto"]:
        merged, mode, stats = merged_gt(ms, old=gt, force=force, verify=verify)
        note = "auto-rebaseline (%s)" % mode
    else:
        # 图像 GT 的查询是人工挑的、相关集依赖 CLIP 画面语义:绝不自动新增查询,
        # 只裁掉已不存在的 id(安全、不引入未经核对的标注),再对齐指纹基线。
        have = {m["id"] for m in ms}
        merged = valid_existing(gt or {}, have) if gt else {}
        mode = "prune-dead-ids (kept %d, dropped %d)" % (
            len(merged), len(gt or {}) - len(merged))
        note = "image GT 未自动新增查询,请人工核对 (eval_image.py)"
        if not merged:
            # 全空说明语料已被清空/整体重构:写空 GT 只会让门禁永远休眠,
            # 保留原文件并让 status 继续报 stale,交人工重标。
            return {"action": "skipped", "queries": 0, "backup": None,
                    "mode": mode, "note": "裁剪后为空,保留原 GT 待人工重标"}
    backup = _backup(t["gt"])
    _atomic_write_json(t["gt"], merged)
    _write_meta(t, merged, ch, th, ms, note=note)
    return {"action": "rebaselined", "queries": len(merged), "backup": backup,
            "mode": mode, "note": note}


def watch_iter(prev):
    """一次轮询:返回 (changed, st)。changed 当语料指纹或素材条数相对上一轮变化。

    只比对指纹(廉价、不跑检索),适合常驻监听;即便 corpus 改了,只要指纹(集合 +
    可检索文本)没变就视为无变化。
    """
    st = status()
    if prev is None:
        return False, st
    changed = (st["corpus_hash"] != prev["corpus_hash"]
               or st["text_hash"] != prev["text_hash"]
               or st["materials"] != prev["materials"])
    return changed, st


def watch(interval=60, apply=False, max_iter=None, _sleep=time.sleep):
    """增量监听语料指纹:检测到变化就提示重标(或 --apply 自动重标)。Ctrl-C 退出。

    默认**只读告警**,绝不自动写任何文件;只有 apply=True 才在检测到硬失配时自动跑
    `main(["--apply"])` 重标(走与人工一致的备份+原子写+刷新基线护栏)。每次轮询只看
    指纹(廉价),不触发模型推理,可常驻后台。
    """
    prev = None
    it = 0
    try:
        while True:
            if max_iter is not None and it >= max_iter:
                break
            it += 1
            changed, st = watch_iter(prev)
            if changed:
                if st["blocking"]:
                    if apply:
                        print("[watch] 检测到语料硬失配,自动重标...")
                        main(["--apply"])
                    else:
                        print("[watch] 检测到语料变更(硬失配),请跑: "
                              "python gt_guard.py --apply")
                else:
                    print("[watch] 检测到可检索文本变更(软失配),仅告警,不重标 "
                          "(重标请显式 `python gt_guard.py --apply`)")
                prev = st
            else:
                prev = st
            if max_iter is not None and it >= max_iter:
                break
            _sleep(interval)
    except KeyboardInterrupt:
        print("[watch] 收到中断,退出监听")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in argv or not any(
        f in argv for f in ("--dry", "--apply", "--adopt", "--watch"))
    ci = "--ci" in argv
    dry = "--dry" in argv
    apply = "--apply" in argv
    adopt = "--adopt" in argv
    force = "--force" in argv
    do_watch = "--watch" in argv
    interval = 60
    if "--interval" in argv:
        try:
            interval = int(argv[argv.index("--interval") + 1])
        except (ValueError, IndexError):
            pass
    core.init_hub()
    ms = core.all_materials()
    st = status(ms)
    print("语料 %d 条  corpus_hash=%s  text_hash=%s" % (
        st["materials"], st["corpus_hash"], st["text_hash"]))
    for key, t in st["targets"].items():
        print("  [%s] %-14s state=%-7s queries=%d%s" % (
            key, t["gt_file"], t["state"], t["queries"],
            ("  " + "; ".join(t["hard_reasons"] + t["soft_reasons"]))
            if (t["hard_reasons"] or t["soft_reasons"]) else ""))

    if ci and not ms:
        print("CI-SKIP: 本环境无语料库(0 条素材),跳过 GT 指纹检查(CI 不误报)")
        return 0

    if do_watch:
        print("[watch] 启动监听(每 %ds 轮询语料指纹; %s; Ctrl-C 退出)" % (
            interval, "硬失配时自动重标" if apply else "只读告警"))
        return watch(interval=interval, apply=apply)

    if check:
        if st["blocking"]:
            print("GT_STALE: 需重标 → 本地跑 `python gt_guard.py --apply`;"
                  "CI 应让本步骤失败以阻止过期 GT 被当作有效门禁")
            return 2
        print("GT_FRESH: 指纹与所有 GT 一致")
        return 0

    if adopt:
        for t in TARGETS:
            gt = _load_json(t["gt"])
            if not isinstance(gt, dict) or not gt:
                print("  [skip] %s 无有效 GT,不建立基线" % os.path.basename(t["gt"]))
                continue
            _write_meta(t, gt, st["corpus_hash"], st["text_hash"], ms,
                        note="adopt: 现有 GT 视为可信基线(未改动 GT)")
            print("  [adopt] %s -> %s" % (
                os.path.basename(t["gt"]), os.path.basename(t["meta"])))
        return 0

    if dry:
        from gen_ground_truth import merged_gt
        for t in TARGETS:
            cur = st["targets"][t["key"]]
            if cur["state"] == "fresh":
                print("  [dry] %-14s up-to-date" % os.path.basename(t["gt"]))
                continue
            if t["auto"]:
                merged, _m, s = merged_gt(ms, old=_load_json(t["gt"]),
                                          force=force, verify=False)
                print("  [dry] %-14s 将写 %d 查询(新增 %d / 丢弃 %d)"
                      % (os.path.basename(t["gt"]), s["total"], s["added"], s["dropped"]))
            else:
                print("  [dry] %-14s 仅裁剪失效 id + 对齐基线(不新增查询)"
                      % os.path.basename(t["gt"]))
        print("DRY-RUN: 未写入任何文件")
        return 0

    if apply:
        changed = 0
        for t in TARGETS:
            cur = st["targets"][t["key"]]
            if cur["state"] == "fresh" and not force:
                print("  [apply] %-14s up-to-date(跳过,幂等)" % os.path.basename(t["gt"]))
                continue
            if cur["state"] == "missing" and not t["auto"]:
                print("  [skip] %-14s 无 GT,不自动创建(需人工标注)" % os.path.basename(t["gt"]))
                continue
            r = _rebaseline(t, _load_json(t["gt"]), ms, st["corpus_hash"],
                            st["text_hash"], force=force, verify=True)
            if r["action"] == "skipped":
                print("  [skip] %-14s %s" % (os.path.basename(t["gt"]), r["note"]))
                continue
            print("  [apply] %-14s %s queries=%d backup=%s" % (
                os.path.basename(t["gt"]), r["mode"], r["queries"], r["backup"]))
            changed += 1
        print("APPLIED: %d 个 GT 已重标(其余 fresh 跳过)" % changed)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
