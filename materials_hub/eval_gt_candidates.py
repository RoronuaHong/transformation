"""GT 补全候选清单(证据驱动,供人工逐条确认)。

背景:语料由 344 → 384 条(bridge 重建)后,`eval_ground_truth.json` 因按旧语料标注而
**漏标新增相关项**(如 31 条 `type:benchmark` 素材中 GT 仅标 18 条),导致 P@5 由 0.86
掉到 0.60。**本脚本不改写权威 GT**,只产出候选 + 证据,交由人工核对。

证据分两级(避免误补):
  strong —— 面标签:在 GT 相关项里覆盖率高,且在库内非泛滥;
  weak   —— 文件名词元:易误伤(例:`clean_silent.mp4` 是 demux 产物,并非「变清晰」结果),
            仅作提示,必须人工判断。

用法:
    python eval_gt_candidates.py                 # 生成 eval_gt_candidates.json + 控制台摘要
    python eval_gt_candidates.py --apply         # 另写 eval_ground_truth.proposed.json(不覆盖原 GT)

核心函数 `build_candidates()` 为纯函数(只吃 gt 与 materials 列表),便于离线单测。
"""
import os
import re
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402

GT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "eval_ground_truth.json")
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "eval_gt_candidates.json")
PROPOSED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "eval_ground_truth.proposed.json")

# 不具区分度的标签:平台/来源面 与 关系/时间码面,不作为证据
GENERIC_TAGS = {"sp", "upload", "bilibili", "youtube", "demux"}
GENERIC_PREFIXES = ("job:", "parent:", "t_start:", "t_end:", "has_audio:")
# 文件名里的扩展名/容器词,不算语义词元
GENERIC_NAME_TOKENS = {"mp4", "jpg", "jpeg", "png", "json", "srt", "ass",
                       "wav", "mov", "webm", "m4a", "mkv", "mp3", "txt",
                       "md", "gif", "pdf"}
MIN_TAG_COV = 0.6            # 标签在 GT 相关项中的最低覆盖率
MAX_CORPUS_MULT = 4.0        # 标签在库内的总数不超过 GT 条数的该倍数(防泛滥标签)
# 「可靠」判别:标签在库内总数需接近 GT 条数(说明该面被 GT 基本穷尽,漏的就是漏标)。
# 反例:type:render 在库内 ~250 条而 GT 只标 80 条,多出的 170 条既含漏标也含无关渲染,
# 仅凭标签无法区分 —— 这类标为 broad(需人工),不进 high 置信。
RELIABLE_MULT = 2.0
RELIABLE_SLACK = 10
MIN_NAME_COV = 0.6           # 文件名词元在 GT 相关项中的最低覆盖率
MIN_NAME_LEN = 4             # 词元最短长度(过滤噪声)


def _tag_set(m_or_tags):
    t = m_or_tags.get("tags") if isinstance(m_or_tags, dict) else m_or_tags
    out = set()
    for x in (t or "").split(","):
        x = x.strip()
        if not x:
            continue
        out.add(x)
    return out


def _is_generic_tag(t):
    return t in GENERIC_TAGS or t.startswith(GENERIC_PREFIXES)


def _name_tokens(name):
    return {x for x in re.findall(r"[a-z0-9]+", (name or "").lower())
            if len(x) >= MIN_NAME_LEN and x not in GENERIC_NAME_TOKENS}


def build_candidates(gt, materials):
    """纯函数:为每句查询产出未标注候选 + 证据。

    gt: {查询: [id, ...]};materials: [素材 dict(id/name/kind/tags), ...]
    返回 {查询: {gt_count, strong_tags, weak_tokens, candidates:[...]}}
    """
    by_id = {m.get("id"): m for m in materials}
    report = {}
    for q, ids in (gt or {}).items():
        ids = [i for i in (ids or []) if i]
        gts = [by_id[i] for i in ids if i in by_id]
        if not gts:
            report[q] = {"gt_count": len(ids), "strong_tags": {},
                         "weak_tokens": {}, "candidates": [],
                         "note": "GT 项在库中不存在,跳过"}
            continue
        n = len(gts)

        # 强证据:面标签
        tag_cov = {}
        for m in gts:
            for t in _tag_set(m):
                if _is_generic_tag(t):
                    continue
                tag_cov[t] = tag_cov.get(t, 0) + 1
        strong = {}
        for t, c in tag_cov.items():
            cov = c / float(n)
            corpus = sum(1 for m in materials if t in _tag_set(m))
            if cov >= MIN_TAG_COV and corpus <= max(n * MAX_CORPUS_MULT, n + 5):
                # 面是否「窄到可信」:库内总数接近 GT 条数 → 漏的基本就是漏标
                reliable = corpus <= max(n * RELIABLE_MULT, n + RELIABLE_SLACK)
                strong[t] = {"gt_coverage": round(cov, 3), "corpus": corpus,
                             "broad": not reliable}

        # 弱证据:文件名词元
        tok_cov = {}
        for m in gts:
            for tk in _name_tokens(m.get("name")):
                tok_cov[tk] = tok_cov.get(tk, 0) + 1
        weak = {tk: round(c / float(n), 3) for tk, c in tok_cov.items()
                if c / float(n) >= MIN_NAME_COV}

        cands = []
        for m in materials:
            mid = m.get("id")
            if not mid or mid in ids:
                continue
            mtags = _tag_set(m)
            hit_strong = sorted(t for t in strong if t in mtags)
            hit_reliable = [t for t in hit_strong if not strong[t]["broad"]]
            hit_broad = [t for t in hit_strong if strong[t]["broad"]]
            mtoks = _name_tokens(m.get("name"))
            hit_weak = sorted(t for t in weak if t in mtoks)
            if not hit_strong and not hit_weak:
                continue
            if hit_reliable:
                conf = "high"          # 窄面标签:基本可判定为漏标
            elif hit_broad:
                conf = "review"        # 宽面标签(如 type:render):需人工判断
            else:
                conf = "low"           # 仅文件名词元:最易误伤
            cands.append({
                "id": mid, "name": m.get("name"), "kind": m.get("kind"),
                "tags": (m.get("tags") or "")[:120],
                "strong": hit_strong, "reliable": hit_reliable,
                "broad": hit_broad, "weak": hit_weak,
                "confidence": conf,
            })
        _order = {"high": 0, "review": 1, "low": 2}
        cands.sort(key=lambda c: (_order.get(c["confidence"], 3),
                                  -len(c["reliable"]), c["name"] or ""))
        report[q] = {"gt_count": n, "strong_tags": strong,
                     "weak_tokens": weak, "candidates": cands}
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="另写 eval_ground_truth.proposed.json(合并 high 置信候选);不覆盖原 GT")
    ap.add_argument("--include-review", action="store_true",
                    help="--apply 时一并纳入 review(宽面标签)候选")
    ap.add_argument("--include-low", action="store_true",
                    help="--apply 时一并纳入 low 置信(文件名词元)候选")
    ap.add_argument("--proposed", default=PROPOSED_FILE,
                    help="--apply 时提案输出路径(默认 eval_ground_truth.proposed.json)")
    args = ap.parse_args()

    if not os.path.exists(GT_FILE):
        print("未找到", GT_FILE)
        return 1
    with open(GT_FILE, encoding="utf-8") as f:
        gt = json.load(f)
    core._init_db()
    materials = core.all_materials()

    report = build_candidates(gt, materials)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=== GT 补全候选(证据驱动,需人工逐条确认)===")
    tot = {"high": 0, "review": 0, "low": 0}
    for q, r in report.items():
        cnt = {"high": 0, "review": 0, "low": 0}
        for c in r["candidates"]:
            cnt[c["confidence"]] = cnt.get(c["confidence"], 0) + 1
        for k in tot:
            tot[k] += cnt[k]
        tags_info = {t: ("broad" if v.get("broad") else "reliable")
                     for t, v in r["strong_tags"].items()}
        print("\n[%s] GT %d 条 | 强标签 %s | 弱词元 %s"
              % (q, r["gt_count"], tags_info, list(r["weak_tokens"])))
        print("   候选 high=%d review=%d low=%d" % (
            cnt["high"], cnt["review"], cnt["low"]))
        for c in r["candidates"][:5]:
            print("     [%s] %-28s %-7s %s%s" % (
                c["confidence"][:4], (c["name"] or "")[:28], c["kind"],
                ("S:" + ",".join(c["strong"])) if c["strong"] else "",
                (" W:" + ",".join(c["weak"])) if c["weak"] else ""))
        if cnt["review"]:
            print("     (review: 命中宽面标签如 type:render,是否相关需人工判断)")
        if cnt["low"]:
            print("     (low: 仅凭文件名词元,最易误伤,必须人工判断)")
    print("\n合计: high=%d review=%d low=%d;明细 -> %s"
          % (tot["high"], tot["review"], tot["low"], OUT_FILE))

    if args.apply:
        merged = {q: list(ids or []) for q, ids in gt.items()}
        added = 0
        keep = {"high"}
        if args.include_review:
            keep.add("review")
        if args.include_low:
            keep.add("low")
        for q, r in report.items():
            for c in r["candidates"]:
                if c["confidence"] in keep:
                    if c["id"] not in merged[q]:
                        merged[q].append(c["id"])
                        added += 1
        with open(args.proposed, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
        print("已写出合并提案(未覆盖原 GT): %s (新增 %d 条)" % (args.proposed, added))
    return 0


if __name__ == "__main__":
    sys.exit(main())
