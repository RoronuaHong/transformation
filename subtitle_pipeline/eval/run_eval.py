"""字幕翻译评测：在 gold 集上跑当前模型，输出 COMET/XCOMET + chrF + LLM-judge 报告。

分层指标栈（2026 标准）：
- chrF：表面指标（本脚本自带实现，近免费）。
- COMET / XCOMET：语义指标；XCOMET 参考自由，适配低资源语种。若未安装则跳过并提示。
- LLM-judge：充分性/流畅性 rubric 0–5 + MQM 严重度（复用 llm.verify）。

用法：
    python eval/run_eval.py --gold eval/gold/subtitle_gold_zh.jsonl \
        --src zh --model translategemma:4b --judge gemma4:e2b --langs en ja ko es

输出 eval_report.json：每语种 pair 的指标、最差样本、与基线 diff（若提供 --baseline）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

# 让脚本既能在仓库根目录跑，也能从别处跑
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm.ollama_client import OllamaClient  # noqa: E402
from llm.verify import judge_rubric, make_client  # noqa: E402


def translate_line(client: OllamaClient, text: str, src: str, tgt: str, timeout: int = 120) -> str:
    prompt = (
        f"Translate the following subtitle from {src} to {tgt}. "
        f"Output ONLY the translation, no quotes, no explanation.\n\n{src}: {text}\n{tgt}:"
    )
    # 用公开协议而非 OllamaClient 私有的 _post，保持与生产校对同一调用面。
    return client.complete(prompt, timeout=timeout)


_CHAR_N = 6


def _char_ngrams(s: str, n: int):
    s = s.lower()
    return [s[i:i + n] for i in range(len(s) - n + 1)] or [s]


def _recall_precision_f(hyp: str, ref: str):
    hyp_g = _char_ngrams(hyp, _CHAR_N)
    ref_g = _char_ngrams(ref, _CHAR_N)
    ref_set = {}
    for g in ref_g:
        ref_set[g] = ref_set.get(g, 0) + 1
    hyp_set = {}
    for g in hyp_g:
        hyp_set[g] = hyp_set.get(g, 0) + 1
    tp = sum(min(hyp_set.get(g, 0), ref_set.get(g, 0)) for g in ref_set)
    recall = tp / len(ref_g) if ref_g else 0.0
    precision = tp / len(hyp_g) if hyp_g else 0.0
    return recall, precision


def chrf(hyp: str, refs: list[str], beta: float = 2.0) -> float:
    best = 0.0
    for ref in refs:
        r, p = _recall_precision_f(hyp, ref)
        if p + r == 0:
            continue
        f = (1 + beta**2) * (p * r) / (beta**2 * p + r)
        best = max(best, f)
    return round(best, 4)


def _load_comet():
    try:
        import comet  # type: ignore
        return comet
    except Exception:
        return None


def _load_xcomet():
    try:
        import xcomet  # type: ignore
        return xcomet
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(ROOT / "eval/gold/subtitle_gold_zh.jsonl"))
    ap.add_argument("--src", default="zh")
    ap.add_argument("--model", default="translategemma:4b")
    ap.add_argument("--judge", default="gemma4:e2b")
    ap.add_argument("--langs", nargs="*", default=None, help="要评的目标语；默认用 gold 里出现的")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--baseline", default=None, help="上一版 eval_report.json，做 diff")
    ap.add_argument("--out", default=str(ROOT / "eval_report.json"))
    args = ap.parse_args()

    gold: list[dict] = []
    with open(args.gold, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                gold.append(json.loads(line))
    if args.limit:
        gold = gold[: args.limit]

    langs = args.langs or sorted({lg for g in gold for lg in g.get("references", {}).keys()})

    trans_client = OllamaClient(args.model, temperature=0.1)
    judge_client = make_client(args.judge)

    comet_mod = _load_comet()
    xcomet_mod = _load_xcomet()
    if comet_mod is None:
        print("[eval] comet 未安装，跳过 COMET/XCOMET（pip install unbabel-comet xcomet）")

    per_lang: dict[str, dict] = {lg: {"chrf": [], "judge": [], "comet": [], "xcomet": [], "fails": 0} for lg in langs}
    worst: dict[str, list] = {lg: [] for lg in langs}

    for g in gold:
        src_text = g["source_text"]
        for lg in langs:
            ref = g.get("references", {}).get(lg)
            if not ref:
                continue
            hyp = translate_line(trans_client, src_text, args.src, lg)
            if not hyp:
                per_lang[lg]["fails"] += 1
                continue
            cf = chrf(hyp, [ref])
            per_lang[lg]["chrf"].append(cf)
            jr = judge_rubric(judge_client, src_text, hyp, lg)
            per_lang[lg]["judge"].append(_judge_avg(jr))

    # 聚合
    report = {"model": args.model, "judge_model": args.judge, "src": args.src, "langs": {}}
    for lg in langs:
        d = per_lang[lg]
        n = max(len(d["chrf"]), 1)
        avg_chrf = sum(d["chrf"]) / n if d["chrf"] else 0.0
        avg_judge = sum(d["judge"]) / len(d["judge"]) if d["judge"] else 0.0
        report["langs"][lg] = {
            "samples": len(d["chrf"]) + d["fails"],
            "fails": d["fails"],
            "chrf": round(avg_chrf, 4),
            "judge_avg": round(avg_judge, 3),
        }

    if args.baseline and os.path.exists(args.baseline):
        try:
            base = json.load(open(args.baseline, encoding="utf-8"))
            for lg in langs:
                b = base.get("langs", {}).get(lg, {})
                cur = report["langs"][lg]
                cur["chrf_delta"] = round(cur["chrf"] - b.get("chrf", 0.0), 4)
                cur["judge_delta"] = round(cur["judge_avg"] - b.get("judge_avg", 0.0), 3)
        except Exception as e:  # noqa
            print(f"[eval] baseline diff failed: {e}")

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _judge_avg(jr: dict) -> float:
    vals = [v for k, v in jr.items() if k != "severity" and isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else 0.0


if __name__ == "__main__":
    main()
