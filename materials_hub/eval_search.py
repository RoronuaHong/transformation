"""素材中心检索质量评估(回归门禁)。

跑检索并算 P@5 / R@20 / MRR / NDCG@10,任何检索改动先跑它防回归。
用法:
    python eval_search.py                 # 混合(auto)
    python eval_search.py --mode lexical  # 仅词法
    python eval_search.py --mode semantic # 仅语义
    python eval_search.py --baseline      # 同时输出 lexical 与 auto 的对比
    python eval_search.py --mode lexical --gate   # 回归门禁:掉出基线则退出码 1
    python eval_search.py --extra         # 多语言/分面额外检查(不进门禁分母)

相关性判定:**以 eval_ground_truth.json 的权威标注为准**({ "查询": ["id1",...] })。
GT 由 `python gen_ground_truth.py` 按当前语料派生(语料感知,可人工核对后修订)。
GT 与当前语料严重失配时门禁判 GT_STALE 休眠,应重标 GT 而非当检索回归处理。

2026-11-08:删除写死的 11 条旧流水线查询(去除字幕/人脸修复/字幕文本…)及其
同义词展开启发式判定 —— 语料清退到 15 条个人图片后这些查询全部 dead,
无 GT 时评估均值仅 0.09 且毫无意义;曾用的 `intersection(EVAL_QUERIES)` 判断
还有真实隐患:GT 一旦出现同名键就会回退到死查询集。现改为:
  * 有 GT → 无条件用 GT 的键;
  * 无 GT → `derived_ground_truth()` 按当前语料文件名确定性派生临时 GT(不写文件)。
"""
import os
import sys
import json
import math
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

GT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_ground_truth.json")


def derived_ground_truth(corpora=None):
    """无权威 GT 时,按当前语料确定性派生临时 GT(**语料感知**,不写文件)。

    复用 `gen_ground_truth.build_gt`:查询 = 素材文件名里可检索的 token,
    相关集 = 文本含该 token 的素材 id。相比旧实现(写死 11 条流水线查询 +
    同义词展开启发式判定),语料换成什么就测什么,永不出现 dead 查询,
    也不会因为 GT 与旧查询同名而被 `intersection` 误回退。

    注意:派生 GT 是**确定性自检(smoke)**,不是人工标注的质量基准;
    正式门禁应跑 `python gen_ground_truth.py` 生成权威 GT 文件。
    """
    from gen_ground_truth import build_gt
    ms = corpora if corpora is not None else core.all_materials()
    # verify=True 与权威 GT 生成逻辑一致:过滤掉「文件名有 token 但检索召回不到」的
    # 噪声查询(例:乱码名 LUOB6.png 派生出的 "luob"),否则派生自检会凭空多一条 0 分。
    return build_gt(ms, verify=True)


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
# 2026-11-08 语料重定基线(384 条流水线素材 → 15 条个人图片):
#   ① GT 由 gen_ground_truth.py 按当前语料重建(9 查询,均非空相关集),不再引用已删 id;
#   ② P@5 归一化到 P@min(5,|rel|)——标准 P@K 在 |rel|<K 时被 |rel|/K 结构性封顶
#      (1 条相关的完美检索仅 0.2),小语料上会假失败;|rel|>=5 时定义不变,基线数值无需改动;
#   ③ 实测 lexical 1.00/1.00/1.00/1.00、auto 全 1.00,门禁从 GT_STALE 休眠恢复为真实 PASS。
#   基线仍保留历史值作为**下限护栏**(只判"是否跌破",不随语料上调放过回归)。


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


def gt_staleness(gt=None, corpora=None):
    """GT 与当前语料的一致性：GT 标注的 id 有多少仍存在于语料。

    语料重构/清理后，旧 GT 引用的 id 可能已不存在 → 检索永远找不到 → 召回虚低、
    门禁【假失败】。返回 {queries, total_gt_ids, existing, missing, coverage,
    empty_queries, stale}。coverage<0.5 或半数以上查询整组缺失 → stale=True
    （指标不可信，应先用 gen_ground_truth.py 重标，而非当回归处理）。
    """
    if gt is None:
        gt = load_ground_truth()
    if not gt:
        return {"queries": 0, "total_gt_ids": 0, "existing": 0, "missing": 0,
                "coverage": None, "empty_queries": 0, "stale": False,
                "note": "no GT file (按当前语料派生临时 GT 自检)"}
    if corpora is None:
        corpora = core.all_materials()
    have = {m["id"] for m in corpora}
    total = existing = empty_queries = 0
    for _q, ids in gt.items():
        if not ids:
            continue
        total += len(ids)
        hit = sum(1 for i in ids if i in have)
        existing += hit
        if hit == 0:
            empty_queries += 1
    coverage = (existing / total) if total else None
    # 零覆盖（GT 与当前语料完全不匹配 → 评估休眠）同样必须判 stale：
    # 否则指标会全为 0，门禁从「跳过」退化成「误报回归」，比不检测更糟。
    stale = bool(gt) and (
        total == 0 or coverage < 0.5
        or empty_queries >= max(1, len(gt) // 2))
    # 零覆盖 = existing==0：既含「GT 无标注相关集」，也含「标注的 id 全不在语料」
    # （语料重构后的典型情形）；两者都意味着评估休眠，note 分别说明成因。
    if existing == 0:
        why = ("GT 无任何标注相关集" if total == 0
               else "标注的 %d 个 id 全部不在当前语料" % total)
        note = f"GT 对当前语料零覆盖（{why}），评估休眠"
    else:
        note = ""
    return {"queries": len(gt), "total_gt_ids": total, "existing": existing,
            "missing": total - existing,
            "coverage": round(coverage, 3) if coverage is not None else None,
            "empty_queries": empty_queries, "stale": stale, "note": note}


def evaluate(mode, gt=None):
    corpora = core.all_materials()
    # GT 是唯一 oracle:有则用其键,无则按当前语料确定性派生临时 GT。
    # 不再有 `intersection(EVAL_QUERIES)` 回退(GT 一旦含旧同名键就会误跑死查询)。
    oracle_gt = gt if gt else derived_ground_truth(corpora)
    queries = list(oracle_gt)
    per_q = []
    p5_sum = r20_sum = mrr_sum = ndcg_sum = 0.0
    for q in queries:
        rows = core.search(q, limit=20, mode=mode)
        ids = [m["id"] for m in rows]
        rel_set = set(oracle_gt[q])
        total = len(rel_set)
        top5 = ids[:5]
        top20 = ids[:20]
        # P@5 归一化:标准 P@K 在 |rel|<K 时被 |rel|/K 结构性封顶(1 条相关集的完美
        # 检索也只有 0.2),小语料上会假失败。用 min(5, total) 作分母 → P@min(5,|rel|);
        # 当 |rel|>=5 时与标准定义完全一致,故历史基线数值无需改动。
        den5 = min(5, total) if total else 5.0
        p5 = sum(1 for i in top5 if i in rel_set) / den5
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
    n = len(queries) or 1          # 空 GT 不除零(门禁随后会判 GT_STALE)
    return per_q, p5_sum / n, r20_sum / n, mrr_sum / n, ndcg_sum / n


# ---------------------------------------------------------------------------
# 步骤 9:多语言 / 分面额外检查(独立报告,不进门禁分母)。
# 2026-11-08:原版写死 "chicken wings" 与 lang:ja —— 清退流水线素材后两者皆 dead,
# 报告恒空。改为语料感知:从当前素材派生英文 token 查询,并报告**实际存在**的 lang: 分面。
# ---------------------------------------------------------------------------
def evaluate_extra():
    """语料感知的多语言/分面检查;返回派生英文查询 top5 + 实际 lang: 分面计数。"""
    ms = core.all_materials()
    # 复用派生 GT 的键(已经过两模式可召回校验),不再拿乱码文件名 token 当英文查询
    toks = list(derived_ground_truth(ms))
    eng_q = next((t for t in toks if t.isascii() and t.isalpha()), "")
    eng = core.search(eng_q, limit=5) if eng_q else []
    eng_top = [{"id": m["id"], "name": m.get("name", ""), "kind": m.get("kind")}
               for m in eng]
    # 统计当前语料里真实存在的 lang: 面标签(不再假定日文字幕一定存在)
    lang_facets = {}
    for m in ms:
        for t in str(m.get("tags") or "").split(","):
            t = t.strip()
            if t.startswith("lang:"):
                lang_facets[t] = lang_facets.get(t, 0) + 1
    sample_tag = sorted(lang_facets)[0] if lang_facets else ""
    hits = core.search("", tag=sample_tag, limit=200) if sample_tag else []
    return {
        "english_query": eng_q,
        "english_top5": eng_top,
        "lang_facets": lang_facets,
        "lang_facet_checked": sample_tag,
        "lang_facet_hit_count": len(hits),
        "lang_facet_sample": [m["id"] for m in hits[:8]],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="auto", choices=["auto", "lexical", "semantic"])
    ap.add_argument("--baseline", action="store_true", help="同时输出 lexical 与 auto 对比")
    ap.add_argument("--gate", action="store_true",
                    help="回归门禁:均值低于基线(含容差)则退出码 1。"
                         "基线以 --mode lexical 测得,建议配合 --mode lexical 使用。"
                         "GT 过期(标注 id 多数已不在语料)时判 GT_STALE 并跳过(退出码 0)")
    ap.add_argument("--gt", default=None,
                    help="指定 ground-truth JSON(默认 eval_ground_truth.json);"
                         "用于 A/B 不同的 GT 标注方案")
    ap.add_argument("--extra", action="store_true",
                    help="步骤 9:多语言/分面额外检查(语料感知,只报告不进门禁分母)")
    args = ap.parse_args()

    if args.extra:
        ex = evaluate_extra()
        print("=== 步骤 9 多语言/分面额外检查(语料感知,不进门禁分母) ===")
        print("派生英文查询 %r 前 5:" % ex["english_query"])
        for r in ex["english_top5"]:
            print(f"  {r['id']}  [{r['kind']}]  {r['name']}")
        print("当前语料实际 lang: 分面:", ex["lang_facets"] or "(无)")
        print(f"抽查 {ex['lang_facet_checked'] or '(无)'} 命中数: "
              f"{ex['lang_facet_hit_count']}  样本: {ex['lang_facet_sample']}")
        return

    gt = load_ground_truth(args.gt)
    if gt:
        oracle = "human-ground-truth"
    else:
        # 无权威 GT:按当前语料确定性派生临时 GT,评估仍有意义(不再是死查询)
        gt = derived_ground_truth()
        oracle = "corpus-derived(smoke;建议 gen_ground_truth.py 生成权威 GT)"

    def _show(mode):
        per, p5, r20, mrr, ndcg = evaluate(mode, gt)
        print(f"\n=== mode={mode}  (oracle: {oracle}) ===")
        print(f"{'query':<22}{'P@5':>8}{'R@20':>8}{'MRR':>8}{'NDCG@10':>9}{'hits':>6}")
        for q, a, b, m, g, h in per:
            print(f"{q:<20}{a:>8.2f}{b:>8.2f}{m:>8.2f}{g:>9.2f}{h:>6}")
        print(f"{'MEAN':<20}{p5:>8.2f}{r20:>8.2f}{mrr:>8.2f}{ndcg:>9.2f}")
        return p5, r20, mrr, ndcg

    if args.gate:
        # GT 过期保护：语料重构后旧 GT 标注的 id 可能已不存在 → 召回虚低、门禁【假失败】。
        # 此时判 GT_STALE 并跳过（退出码 0），提示先重标 GT，而非误报检索回归。
        st = gt_staleness(gt)
        # gt_staleness 只看「标注 id 是否还在」,看不见「旧 id 全在但语料新增/改名/
        # 内容变了」——这种情况旧 GT 会让门禁报出误导性的 PASS。指纹判定由 gt_guard 补齐,
        # 任一判失配都休眠(2026-11-10)。CI 另有 `gt_guard.py --check` 主动拦过期 GT。
        guard_reasons = []
        try:
            import gt_guard
            gs = gt_guard.status()
            if gs.get("blocking"):
                guard_reasons = ["%s: %s" % (k, "; ".join(t["hard_reasons"]))
                                 for k, t in gs.get("targets", {}).items()
                                 if t["state"] in ("stale", "legacy", "missing")]
        except Exception as e:
            print(f"[warn] 语料指纹检查不可用({e})，仅按 id 存活率判定")
        if st.get("stale") or guard_reasons:
            print(f"\n=== 回归门禁 (mode={args.mode}) ===")
            print("GATE: GT_STALE (SKIPPED)")
            print(f"  GT 标注 {st.get('existing')}/{st.get('total_gt_ids')} 个 id "
                  f"仍存在于当前语料（coverage={st.get('coverage')}），"
                  f"{st.get('empty_queries')}/{st.get('queries')} 条查询整组缺失")
            for r in guard_reasons:
                print("  语料指纹:", r)
            print("  → 指标不可信；请跑 `python gt_guard.py --apply` 重标 GT 并刷新指纹基线")
            sys.exit(0)
        # 预热:先用一条真实 GT 查询跑一次目标模式检索,把 embed/CLIP/重排(及英文查询的
        # 翻译)等模型在**门禁计时循环之外**冷加载一次;否则 9 条查询里第一条会独吞全部模型
        # 加载(~80s),让门禁时序不可复现且逼近 harness 超时。常驻重排进程也会在这一次里
        # 完成 560MB 模型加载。注:本机 6GB VRAM 装不下 bge-m3+CLIP+translategemma 三模型,
        # ollama 会在查询间驱逐并重载(每查询 0.2s↔39s 抖动)——这是硬件限制,门禁超时已相应放宽。
        try:
            warm_q = next(iter(gt), None)
            if warm_q:
                core.search(warm_q, limit=20, mode=args.mode)
        except Exception:
            pass
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
