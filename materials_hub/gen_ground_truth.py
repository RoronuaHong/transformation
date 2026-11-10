# -*- coding: utf-8 -*-
"""生成/重建 eval_ground_truth.json(检索门禁的权威相关集)。

用法:
    python gen_ground_truth.py           # 合并模式:保留仍有效的旧标注 + 补新查询
    python gen_ground_truth.py --force   # 重建模式:只按当前语料生成
    python gen_ground_truth.py --dry     # 只预览,不写文件

设计原则(2026-11-08 重写):
  * **语料感知**:查询从当前素材文件名里可检索的 token(中文串 / ASCII 词)自动派生,
    相关集 = 文本含该 token 的素材 id(确定性、可人工核对,不依赖模型自身输出)。
    绝不写死旧流水线语料的查询(如 dehardsub/srt),否则语料一换 GT 立即全 dead。
  * **永不写空查询**:空相关集会把门禁均值拉成 0 → 假失败。
  * **合并模式清理陈旧键**:旧 GT 里 id 已全部不在当前语料的查询会被丢弃
    (原先"绝不覆盖已有键"会把 9 条死查询永久留在 GT,重跑即复活)。
  * 只按当前语料生成,不改动任何素材。

门禁读到该文件即改用 human-ground-truth 作为 oracle。GT 与语料严重失配时
eval_search.py 会判 GT_STALE 并跳过评估(而非误报回归)。
"""
import json
import os
import re
import sys

import core

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "eval_ground_truth.json")
FORCE = "--force" in sys.argv
DRY = "--dry" in sys.argv

# 文件名里这些 token 太泛(扩展名/通用词),不能当查询
STOP = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "pdf", "mp4", "srt", "ass",
    "image", "images", "img", "photo", "photos", "picture", "pictures",
    "微信图片", "图片", "照片", "截图", "final", "copy", "test", "tmp",
}
CN_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
EN_RUN = re.compile(r"[A-Za-z]{3,}")


def blob(m):
    """可检索文本(与 core._material_text 的字段口径保持一致)。"""
    return " ".join(str(m.get(k, "") or "") for k in
                    ("name", "orig_name", "tags", "description",
                     "external_path", "rel_path")).lower()


def candidate_queries(ms):
    """从当前语料文件名派生候选查询 token(去重、去停用、去纯 hex/uuid)。"""
    toks = []
    for m in ms:
        name = str(m.get("orig_name") or m.get("name") or "")
        stem = os.path.splitext(name)[0]
        for run in CN_RUN.findall(stem) + EN_RUN.findall(stem):
            t = run.lower()
            if t in STOP or len(t) < 2:
                continue
            if re.fullmatch(r"[0-9a-f]{8,}", t):       # uuid / 哈希片段
                continue
            if t not in toks:
                toks.append(t)
    return toks


def build_gt(ms, verify=True):
    """按当前语料构造 GT;verify=True 时要求两门禁模式都能检索到相关集。"""
    texts = {m["id"]: blob(m) for m in ms}
    gt = {}
    seen_sets = set()
    for q in candidate_queries(ms):
        rel = [mid for mid, t in texts.items() if q in t]
        if not rel or len(rel) > 8:      # 空集 / 过泛查询都不写
            continue
        key = tuple(sorted(rel))
        if key in seen_sets:             # 同一相关集的冗余 token(如 FunUnion/fununion)只留一个
            continue
        if verify:
            # 至少 lexical 与 auto 两种模式都要能召回,避免单模式门禁假失败
            ok = True
            for mode in ("lexical", "auto"):
                try:
                    ids = {x["id"] for x in core.search(q, limit=20, mode=mode)}
                except Exception:
                    ok = False
                    break
                if not (set(rel) & ids):
                    ok = False
                    break
            if not ok:
                continue
        gt[q] = sorted(rel)
        seen_sets.add(key)
    return gt


def valid_existing(old, have):
    """保留仍有效的旧标注(id 至少还有一个在当前语料);丢弃陈旧/空查询。"""
    kept = {}
    for q, ids in old.items():
        if isinstance(ids, list) and any(i in have for i in ids):
            kept[q] = [i for i in ids if i in have]
    return kept


def merged_gt(ms, old=None, force=False, verify=True):
    """纯函数:按当前语料 + 可选旧 GT 合并出权威 GT。返回 (merged, mode, stats)。

    抽出为独立函数是 2026-11-10 的改动:`gt_guard.py` 的语料变更自动重标钩子需要
    **复用同一套合并语义**(保留仍有效的人工标注、只补新查询、丢弃死查询),
    否则两条路径会各自演化,自动重标可能悄悄抹掉人工标注。
    verify=False 供离线/快速预览(不跑检索),正式重标用 True。
    """
    have = {m["id"] for m in ms}
    fresh = build_gt(ms, verify=verify)
    old = old or {}
    if force or not old:
        merged = dict(fresh)
        mode = "force-rebuild" if force else "init"
        kept, added = 0, len(fresh)
    else:
        kept_gt = valid_existing(old, have)
        merged = dict(kept_gt)
        new_keys = [k for k in fresh if k not in merged]
        for k in new_keys:
            merged[k] = fresh[k]
        added, kept = len(new_keys), len(kept_gt)
        mode = "merge(+%d new, kept %d valid, dropped %d stale/empty)" % (
            added, kept, len(old) - kept)
    stats = {"materials": len(ms), "fresh_queries": len(fresh),
             "kept": kept, "added": added, "dropped": len(old) - kept if old else 0,
             "total": len(merged)}
    return merged, mode, stats


def main():
    core.init_hub()
    ms = core.all_materials()
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = json.load(f)
        except Exception as e:
            print("[warn] 现文件不可读(%s),按重建处理" % e)
            old = {}
    merged, _mode, stats = merged_gt(ms, old=old, force=FORCE, verify=True)
    print("当前语料 %d 条,派生有效查询 %d 条,合并后 %d 条" % (
        stats["materials"], stats["fresh_queries"], stats["total"]))
    if DRY:
        print("DRY-RUN(未写入) mode=%s total=%d" % (_mode, len(merged)))
    else:
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        print("wrote %s  (mode=%s, total %d queries)" % (OUT, _mode, len(merged)))
    for k, v in merged.items():
        print("  %-16s n=%-3d %s" % (k[:16], len(v), [i[:8] for i in v[:4]]))
    return merged


if __name__ == "__main__":
    main()
