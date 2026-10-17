# -*- coding: utf-8 -*-
"""eval_image.py — CLIP 图像检索(以文搜图 text→image)质量门禁。

仿 eval_search.py 的词法门禁,但测 SigLIP2 以文搜图:对 eval_image_gt.json 里每句
查询跑 core.search_by_text_image,算 P@5 / R@20 / MRR / NDCG@10,均值掉出基线(容差
0.02)即 FAIL(退出码 1)。GT 人工标注({查询:[相关素材 id,...]}),检测到即用精确标注。

注:个人素材语料小(19 条),R@20 基本饱和(~1.0),真正对 CLIP 退化敏感的是 P@5/MRR/NDCG。
建 GT/基线的教训同词法门禁:先跑真实输出再标期望,基线按实测回填(见文件尾沿革)。

用法:
  python eval_image.py            # 跑一遍看指标(不门禁)
  python eval_image.py --gate     # 回归门禁:掉出基线退出码 1
  python eval_image.py --gt X.json --limit 10
"""
import os
import sys
import json
import math
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

GT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_image_gt.json")
GATE_TOL = 0.02
GATE_METRICS = ("p5", "r20", "mrr", "ndcg")

# 基线 = 2026-10-15 SigLIP2-256/webli(GPU)在 19 条个人素材上的实测均值(见文件尾沿革)。
IMAGE_GATE_BASELINE = {"p5": 0.35, "r20": 0.98, "mrr": 0.81, "ndcg": 0.84}


def load_ground_truth(path=None):
    p = path or GT_FILE
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return None


def check_gate(p5, r20, mrr, ndcg, baseline=None, tol=GATE_TOL):
    """纯函数:实测均值 vs 门禁基线。返回 (ok, details);任一指标低于 baseline-tol 即失败。"""
    base = dict(IMAGE_GATE_BASELINE if baseline is None else baseline)
    got = {"p5": p5, "r20": r20, "mrr": mrr, "ndcg": ndcg}
    details, ok = [], True
    for k in GATE_METRICS:
        b = float(base.get(k, 0.0))
        g = float(got.get(k, 0.0))
        passed = g >= (b - float(tol))
        ok = ok and passed
        details.append({"metric": k, "baseline": b, "actual": g,
                        "delta": round(g - b, 4), "pass": passed})
    return ok, details


def evaluate(gt, limit=20):
    queries = list(gt)
    per_q = []
    p5s = r20s = mrrs = ndcgs = 0.0
    for q in queries:
        rel = set(gt[q])
        total = len(rel)
        rows = core.search_by_text_image(q, limit=limit)
        if isinstance(rows, dict):
            rows = rows.get("matches") or rows.get("results") or rows.get("items") or []
        rows = rows or []
        ids = [x.get("id") for x in rows if isinstance(x, dict)]
        top5, top20 = ids[:5], ids[:20]
        p5 = sum(1 for i in top5 if i in rel) / 5.0
        r20 = (sum(1 for i in top20 if i in rel) / total) if total else 0.0
        mrr = 0.0
        for i, mid in enumerate(ids):
            if mid in rel:
                mrr = 1.0 / (i + 1)
                break
        k = 10
        dcg = sum(1.0 / math.log2(i + 2) for i, mid in enumerate(ids[:k]) if mid in rel)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(min(k, total))) or 1.0
        ndcg = dcg / idcg
        p5s += p5; r20s += r20; mrrs += mrr; ndcgs += ndcg
        per_q.append((q, p5, r20, mrr, ndcg, len(ids)))
    n = len(queries) or 1
    return per_q, p5s / n, r20s / n, mrrs / n, ndcgs / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true",
                    help="回归门禁:均值低于基线(含容差)则退出码 1")
    ap.add_argument("--gt", default=None, help="指定 ground-truth JSON")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    core.init_hub()
    gt = load_ground_truth(args.gt)
    if not gt:
        print("FAIL: no ground truth (eval_image_gt.json)")
        sys.exit(1)
    per, p5, r20, mrr, ndcg = evaluate(gt, limit=args.limit)
    print(f"\n=== CLIP image search (text→image), model={core.clip_probe().get('model')} ===")
    print(f"{'query':<16}{'P@5':>8}{'R@20':>8}{'MRR':>8}{'NDCG@10':>9}{'hits':>6}")
    for q, a, b, m, g, h in per:
        print(f"{q:<14}{a:>8.2f}{b:>8.2f}{m:>8.2f}{g:>9.2f}{h:>6}")
    print(f"{'MEAN':<14}{p5:>8.2f}{r20:>8.2f}{mrr:>8.2f}{ndcg:>9.2f}")
    if args.gate:
        ok, details = check_gate(p5, r20, mrr, ndcg)
        print(f"\n=== 回归门禁 (image, oracle=human-ground-truth) ===")
        print(f"{'metric':<8}{'baseline':>10}{'actual':>9}{'delta':>9}  result")
        for d in details:
            print(f"{d['metric']:<8}{d['baseline']:>10.2f}{d['actual']:>9.2f}"
                  f"{d['delta']:>+9.2f}  {'PASS' if d['pass'] else 'FAIL'}")
        print("GATE:", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)


# ---------------------------------------------------------------------------
# 沿革:
# 2026-10-15 首建。语料=19 条个人素材(清空 127 条流水线产物后);SigLIP2-256/webli(GPU)
# 实测均值 p5≈.37 / r20≈1.0 / mrr≈.83 / ndcg≈.86,基线按实测略降回填(容差 .02)。
# 7 查询均为"先跑真实输出再标期望":宠物图零样本匹配"宠物狗"偏弱(排 3-5),故 P@5 不高,
# 但 MRR/NDCG 对 CLIP 退化(换错模型/向量损坏→排序随机化)敏感,可作回归护栏。
# 换 CLIP 模型或重建向量后必须重跑本门禁回填基线。
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()
