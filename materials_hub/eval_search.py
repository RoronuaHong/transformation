"""素材中心检索质量评估(回归门禁)。

固化 README 里的中文查询集,跑检索并算 P@5 / R@20,任何检索改动先跑它防回归。
用法:
    python eval_search.py                 # 混合(auto)
    python eval_search.py --mode lexical  # 仅词法
    python eval_search.py --mode semantic # 仅语义
    python eval_search.py --baseline      # 同时输出 lexical 与 auto 的对比

相关性判定(默认启发式):用 core.expand_query 把中文查询扩出的英文词,命中素材的
tags/name/description/path 即判相关 —— 这是 README 里 P@5 提升叙事的直接代理指标。
**权威数字需人工标注**:把每句查询对应的相关素材 id 写进 eval_ground_truth.json
({ "查询": ["id1","id2",...], ... }),本脚本检测到后会改用精确标注计算(覆盖启发式)。
"""
import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

# 固化查询集(取自 README 实测场景)。新增查询直接在下面加一行即可。
EVAL_QUERIES = [
    "去除字幕只留背景",
    "把模糊画面变清晰",
    "按时间切的片段",
    "字幕文本文件",
    "对比渲染结果",
    "人脸修复",
    "基准测试视频",
    "b站下载的字幕",
    "修好的成品",
]

GT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_ground_truth.json")


def expected_tokens(q):
    ex = core.expand_query(q)
    if ex == q:
        return []
    return [t for t in ex[len(q):].strip().split() if t]


def _mat_text(m):
    return " ".join(str(m.get(k, "")) for k in
                    ("tags", "name", "description", "external_path", "rel_path")).lower()


def heuristic_rel(q, rows):
    toks = expected_tokens(q)
    if not toks:
        return set()
    rel = set()
    for m in rows:
        t = _mat_text(m)
        if any(tok.lower() in t for tok in toks):
            rel.add(m["id"])
    return rel


def load_ground_truth():
    if os.path.exists(GT_FILE):
        try:
            with open(GT_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[warn] 读取 {GT_FILE} 失败: {e}")
    return None


def evaluate(mode, gt=None):
    corpora = core.all_materials()
    total_rel = {}
    if gt is None:
        # 语料级相关计数(用于召回率分母):对每句查询,全库里启发式相关的条目数
        for q in EVAL_QUERIES:
            total_rel[q] = len(heuristic_rel(q, corpora))

    per_q = []
    p5_sum = r20_sum = 0.0
    for q in EVAL_QUERIES:
        rows = core.search(q, limit=20, mode=mode)
        ids = [m["id"] for m in rows]
        if gt and q in gt:
            rel_set = set(gt[q])
            total = len(rel_set)
        else:
            rel_set = heuristic_rel(q, rows)
            total = total_rel.get(q, len(rel_set)) or 1
        top5 = ids[:5]
        top20 = ids[:20]
        p5 = sum(1 for i in top5 if i in rel_set) / 5.0
        r20 = sum(1 for i in top20 if i in rel_set) / total if total else 0.0
        p5_sum += p5
        r20_sum += r20
        per_q.append((q, p5, r20, len(ids)))
    n = len(EVAL_QUERIES)
    return per_q, p5_sum / n, r20_sum / n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "lexical", "semantic"])
    ap.add_argument("--baseline", action="store_true", help="同时输出 lexical 与 auto 对比")
    args = ap.parse_args()

    gt = load_ground_truth()
    oracle = "human-ground-truth" if gt else "heuristic(synonym-expansion)"

    def _show(mode):
        per, p5, r20 = evaluate(mode, gt)
        print(f"\n=== mode={mode}  (oracle: {oracle}) ===")
        print(f"{'query':<22}{'P@5':>8}{'R@20':>8}{'hits':>6}")
        for q, a, b, h in per:
            print(f"{q:<20}{a:>8.2f}{b:>8.2f}{h:>6}")
        print(f"{'MEAN':<20}{p5:>8.2f}{r20:>8.2f}")
        return p5, r20

    if args.baseline:
        la, ra = _show("lexical")
        ba, rb = _show("auto")
        print(f"\nlift(auto - lexical):  P@5 {ba-la:+.2f}   R@20 {rb-ra:+.2f}")
    else:
        _show(args.mode)


if __name__ == "__main__":
    core.init_hub()
    main()
