"""素材中心检索质量评估(回归门禁)。

固化 README 里的中文查询集,跑检索并算 P@5 / R@20,任何检索改动先跑它防回归。
用法:
    python eval_search.py                 # 混合(auto)
    python eval_search.py --mode lexical  # 仅词法
    python eval_search.py --mode semantic # 仅语义
    python eval_search.py --baseline      # 同时输出 lexical 与 auto 的对比
    python eval_search.py --mode lexical --gate   # 回归门禁:掉出基线则退出码 1

相关性判定(默认启发式):用 core.expand_query 把中文查询扩出的英文词,命中素材的
tags/name/description/path 即判相关 —— 这是 README 里 P@5 提升叙事的直接代理指标。
**权威数字需人工标注**:把每句查询对应的相关素材 id 写进 eval_ground_truth.json
({ "查询": ["id1","id2",...], ... }),本脚本检测到后会改用精确标注计算(覆盖启发式)。
"""
import os
import sys
import json
import math
import re
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

# 固化查询集(取自 README 实测场景)。新增查询直接在下面加一行即可。
# 2026-10-09 语料重构(移除 256 条 mode-renders QA 渲染):5 个死亡查询移除
# (对比渲染结果/用 Lama 补全的画面/字形检测的渲染输出/视觉语言模型抽的关键帧/
#  去台标 delogo 处理的片段——其 GT 目标全部随语料移除),余 11 查询。
EVAL_QUERIES = [
    "去除字幕只留背景",
    "把模糊画面变清晰",
    "按时间切的片段",
    "字幕文本文件",
    "人脸修复",
    "基准测试视频",
    "b站下载的字幕",
    "修好的成品",
    # 窄查询(2026-09-28 补):相关集 1-6 条,大相关性集下 P@5 饱和无法区分排序,靠这些恢复区分度
    "去马赛克处理结果",
    "繁体中文字幕 srt",
    "b站视频的音频文件",
]

GT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_ground_truth.json")


def expected_tokens(q):
    ex = core.expand_query(q)
    if ex == q:
        return []
    return [t for t in ex[len(q):].strip().split() if t]


def _mat_text(m):
    # OCR 文本只进语义(稠密)索引,不进词法 token 索引(见 core._material_text);
    # 门禁测的是词法检索质量,故相关性判定也必须剥掉 description 的 [OCR] 段,
    # 否则 240 条素材批量 OCR 后自由字幕文本会稀释相关性分母/分子(ndcg 0.84→0.80)。
    desc = re.sub(r"\s*\[OCR\][\s\S]*$", "", str(m.get("description", ""))).strip()
    return " ".join(
        (desc if k == "description" else str(m.get(k, "")))
        for k in ("tags", "name", "description", "external_path", "rel_path")
    ).lower()


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


def load_ground_truth(path=None):
    p = path or GT_FILE
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[warn] 读取 {p} 失败: {e}")
    return None


# ---------------------------------------------------------------------------
# 回归门禁基线(2026-10-06 实测,已含 GT 漏标补全)
#   语料:384 条(bridge 重建后)· oracle:16 查询 human ground-truth(427 条标注)· mode:lexical
#   沿革:344 语料期为 0.86 → 重建后 GT 漏标致 0.60 → 按窄面标签补全 GT(374→427)后 **0.71**。
#   补全方案经 A/B 实测选定:high-only(+53)零回退且 MRR 0.75→0.85;
#   全量(+640,含宽面/词元弱证据)虽 P@5 0.80 但 R@20 反降至 0.73,且标注过宽会稀释区分度,
#   故仅作备选(`eval_ground_truth.all.json`),未采纳。详见《最佳实践》§15.4。
#   门禁用途:任何检索/同义词改动跑 `--gate`,均值掉出基线(容差 0.02)即失败(退出码 1)。
# ---------------------------------------------------------------------------
# 2026-10-09 语料重构后重测(384->128,移除 256 条 mode-renders QA 渲染;GT 427->109 标注/11 查询)。
# lexical 为门禁主基线;auto 单列基线(重构后稠密 RRF 在小语料+小 GT 上实测低于 lexical,
# 如实记录,重构前 auto lift≈0 的结论在新语料上不成立,待 GT 重新充实后再评估)。
# 2026-10-10 二次收口:清除 3 个语义已死查询(按时间切的片段/基准测试视频/去马赛克处理结果
# ——真目标在 10-09 mode-renders 手术中被删,幸存标注与现库命名错位;备份 backup-20261010.json),
# GT 11→8 查询、98→82 标注。基线随实测收紧(lexical p5 .59→.78 / mrr .73→.93)。
GATE_BASELINE = {"p5": 0.78, "r20": 0.92, "mrr": 0.93, "ndcg": 0.92}
# 2026-10-10:多模型栈(SigLIP2+bge-reranker-v2-m3 ONNX 精排+受控词表 autotags 通道)落地后,
# auto 模式随实测收紧(r20 .70→.84 / ndcg .66→.75)。
AUTO_GATE_BASELINE = {"p5": 0.49, "r20": 0.84, "mrr": 0.80, "ndcg": 0.75}
GATE_TOL = 0.02          # 容差:允许浮点/排序稳定性带来的微小波动
GATE_METRICS = ("p5", "r20", "mrr", "ndcg")


def check_gate(p5, r20, mrr, ndcg, baseline=None, tol=GATE_TOL):
    """纯函数:实测均值 vs 门禁基线。返回 (ok, details)。

    details 每项 {metric, baseline, actual, delta, pass};任一指标低于
    `baseline - tol` 即判失败。刻意做成纯函数便于离线单测(mock 无关)。
    """
    base = dict(GATE_BASELINE if baseline is None else baseline)
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


def evaluate(mode, gt=None):
    corpora = core.all_materials()
    total_rel = {}
    if gt is None:
        # 语料级相关计数(用于召回率分母):对每句查询,全库里启发式相关的条目数
        for q in EVAL_QUERIES:
            total_rel[q] = len(heuristic_rel(q, corpora))

    queries = EVAL_QUERIES
    if gt and not set(gt).intersection(EVAL_QUERIES):
        queries = list(gt)
    per_q = []
    p5_sum = r20_sum = mrr_sum = ndcg_sum = 0.0
    for q in queries:
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
        # MRR / NDCG@10:对排序质量敏感,P@5 在大相关性集下饱和时用它区分 lexical vs auto
        mrr = 0.0
        for i, mid in enumerate(ids):
            if mid in rel_set:
                mrr = 1.0 / (i + 1)
                break
        k = 10
        dcg = sum(1.0 / math.log2(i + 2) for i, mid in enumerate(ids[:k]) if mid in rel_set)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(min(k, total))) or 1.0
        ndcg = dcg / idcg
        p5_sum += p5
        r20_sum += r20
        mrr_sum += mrr
        ndcg_sum += ndcg
        per_q.append((q, p5, r20, mrr, ndcg, len(ids)))
    n = len(queries)
    return per_q, p5_sum / n, r20_sum / n, mrr_sum / n, ndcg_sum / n


# ---------------------------------------------------------------------------
# 步骤 9:不进旧中文门禁分母的两条多语言检查(独立报告,不影响门禁均值)
#   ① 英文问句(经翻译桥)应命中画面是鸡翅的素材;② lang:ja 过滤应命中日文字幕文件。
# ---------------------------------------------------------------------------
def evaluate_extra():
    """返回 {english_chicken_wings_top5, lang_ja_subs_count, lang_ja_subs_sample}。"""
    eng = core.search("chicken wings", limit=5)
    eng_top = [{"id": m["id"], "name": m.get("name", ""), "kind": m.get("kind")}
               for m in eng]
    ja = core.search("", tag="lang:ja", limit=200)
    ja_subs = [m for m in ja if m.get("kind") == "subs"]
    return {
        "english_chicken_wings_top5": eng_top,
        "lang_ja_subs_count": len(ja_subs),
        "lang_ja_subs_sample": [m["id"] for m in ja_subs[:8]],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "lexical", "semantic"])
    ap.add_argument("--baseline", action="store_true", help="同时输出 lexical 与 auto 对比")
    ap.add_argument("--gate", action="store_true",
                    help="回归门禁:均值低于基线(含容差)则退出码 1。"
                         "基线以 --mode lexical 测得,建议配合 --mode lexical 使用")
    ap.add_argument("--gt", default=None,
                    help="指定 ground-truth JSON(默认 eval_ground_truth.json);"
                         "用于 A/B 不同的 GT 标注方案")
    ap.add_argument("--extra", action="store_true",
                    help="步骤 9:额外跑两条多语言检查(英文 chicken wings + lang:ja 过滤),"
                         "只报告不影响旧中文门禁")
    args = ap.parse_args()

    if args.extra:
        ex = evaluate_extra()
        print("=== 步骤 9 多语言额外检查(不进中文门禁分母) ===")
        print("英文 chicken wings 前 5:")
        for r in ex["english_chicken_wings_top5"]:
            print(f"  {r['id']}  [{r['kind']}]  {r['name']}")
        print(f"lang:ja 字幕命中数: {ex['lang_ja_subs_count']}  样本: {ex['lang_ja_subs_sample']}")
        return

    gt = load_ground_truth(args.gt)
    oracle = "human-ground-truth" if gt else "heuristic(synonym-expansion)"

    def _show(mode):
        per, p5, r20, mrr, ndcg = evaluate(mode, gt)
        print(f"\n=== mode={mode}  (oracle: {oracle}) ===")
        print(f"{'query':<22}{'P@5':>8}{'R@20':>8}{'MRR':>8}{'NDCG@10':>9}{'hits':>6}")
        for q, a, b, m, g, h in per:
            print(f"{q:<20}{a:>8.2f}{b:>8.2f}{m:>8.2f}{g:>9.2f}{h:>6}")
        print(f"{'MEAN':<20}{p5:>8.2f}{r20:>8.2f}{mrr:>8.2f}{ndcg:>9.2f}")
        return p5, r20, mrr, ndcg

    if args.gate:
        _per, p5, r20, mrr, ndcg = evaluate(args.mode, gt)
        base = AUTO_GATE_BASELINE if args.mode == "auto" else None
        ok, details = check_gate(p5, r20, mrr, ndcg, baseline=base)
        print(f"\n=== 回归门禁 (mode={args.mode}, oracle={oracle}) ===")
        print(f"{'metric':<8}{'baseline':>10}{'actual':>9}{'delta':>9}  result")
        for d in details:
            print(f"{d['metric']:<8}{d['baseline']:>10.2f}{d['actual']:>9.2f}"
                  f"{d['delta']:>+9.2f}  {'PASS' if d['pass'] else 'FAIL'}")
        print("GATE:", "PASS" if ok else "FAIL")
        sys.exit(0 if ok else 1)

    if args.baseline:
        la, ra, lm, ln = _show("lexical")
        ba, rb, bm, bn = _show("auto")
        print(f"\nlift(auto - lexical):  P@5 {ba-la:+.2f}   R@20 {rb-ra:+.2f}   "
              f"MRR {bm-lm:+.2f}   NDCG@10 {bn-ln:+.2f}")
    else:
        _show(args.mode)


if __name__ == "__main__":
    core.init_hub()
    main()
