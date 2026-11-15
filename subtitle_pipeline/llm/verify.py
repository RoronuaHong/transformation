"""多模型翻译校对：副译共识 + 回译 + Judge rubric（规划 §13.2）。

设计目标（2026 最佳实践）：
- 多模型共识：主译走 `translate` role(translategemma:4b)，副译/回译/Judge 走
  独立的 `verify` role(gemma4:e2b 或云模型)——两个不同模型才抓得住误译。
- 回译校验（reverse translation）：把译文回译成源语，与原句比对，抓漏译/弱化/漂移。
- Judge（LLM-as-judge，GEMBA-MQM 风格）：充分性/流畅性/术语一致/无增删/文化适配，0–5 + 严重度。
- 生产热路径只 Judge「主副译不一致」的 cue；评测集全量 Judge 在离线跑。

本模块同时服务两处（不再只供 eval）：
1. `eval/run_eval.py` 的离线评测（backtranslate / judge_rubric / make_client）。
2. 生产翻译热路径（`proofread_lines`）：由 pipeline.translate_segments 与
   discover/run_batch.translate_summary_locale 调用。

**降级契约**：校对所有异常都必须被吞掉并返回原译文，绝不能让翻译 job 失败。
所有客户端统一走 `LLMClient.complete()`（不再用 OllamaClient 私有的 `_post`），
这样 local / tokenhub / openai / hybrid profile 都能当校对模型。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field

from translation_integrity import is_hard_pollution, overlong, wrong_language

# 默认模型仅作兜底常量；真实模型由 llm profile 的 `verify` role 决定（单一真源）。
DEFAULT_VERIFY_MODEL = "gemma4:e2b"
VALID_MODES = ("off", "consensus", "full")

_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def verify_mode() -> str:
    """生产校对档位：off=关闭, consensus=仅 Judge 不一致 cue(默认), full=全量 Judge。"""
    raw = (os.environ.get("VITUAL_TRANSLATE_VERIFY") or "consensus").strip().lower()
    return raw if raw in VALID_MODES else "consensus"


def verify_judge_budget() -> int:
    """单批次最多 Judge 多少条，防止 20 语 × 长视频把算力打爆；超出标记人工复核。"""
    try:
        return max(1, int(os.environ.get("VITUAL_VERIFY_MAX_JUDGE") or "48"))
    except ValueError:
        return 48


def make_client(model: str | None = None):
    """构造校对客户端：走 active profile 的 `verify` role（可 provider fallback）。

    `model` 只覆盖模型名，provider/base_url/key 仍继承 profile，避免像旧版那样
    硬编码 `OllamaClient(model)` 把校对锁死在本地。
    """
    from llm.factory import get_client

    return get_client("verify", model=model) if model else get_client("verify")


def _client_models() -> tuple[str, str]:
    from llm.factory import get_role_config

    try:
        t = get_role_config("translate")
        v = get_role_config("verify")
        return f"{t.provider}:{t.model}", f"{v.provider}:{v.model}"
    except Exception:
        return "", DEFAULT_VERIFY_MODEL


def _post(client, prompt: str, timeout: int) -> str:
    """统一入口：所有 LLMClient 都实现 complete(prompt, *, timeout)。"""
    return client.complete(prompt, timeout=timeout)


def _json_array(raw: str) -> list:
    """从模型输出里抠出 JSON 数组（容忍围栏与尾部解释）。"""
    if not raw:
        return []
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    try:
        val = json.loads(text)
        if isinstance(val, list):
            return val
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            val = json.loads(m.group(0))
            if isinstance(val, list):
                return val
        except (json.JSONDecodeError, ValueError):
            pass
    return []


@dataclass
class VerifyResult:
    backtrans_score: float = 0.0        # 回译与原句语义相似度粗分 0–1
    judge: dict = field(default_factory=dict)  # {adequacy, fluency, terminology, no_add_omit, culture, severity, decision}
    decision: str = "accept"            # accept | human_review | reject


@dataclass
class ProofreadReport:
    """生产校对报告，写进 translate_qa_<lang>.json 的 `proofread` 键。"""
    status: str = "ok"                  # ok | disabled | skipped | error
    mode: str = "consensus"
    translate_model: str = ""
    verify_model: str = ""
    total: int = 0
    consensus: int = 0                  # 主副译一致 → 免 Judge 直接 accept
    divergent: int = 0                  # 主副译不一致
    judged: int = 0                     # 实际跑 Judge 的 cue 数
    repaired: int = 0                   # Judge 择优后替换的 cue 数
    human_review: int = 0               # 需人工复核的 cue 数
    avg_judge: float | None = None      # Judge 5 维均分(0–5)
    flags: list[dict] = field(default_factory=list)  # 逐条待复核明细
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def backtranslate(client, text: str, src_lang: str, tgt_lang: str, timeout: int = 120) -> str:
    """把目标语文本回译成源语，用于与原句比对。"""
    prompt = (
        f"Translate the following {tgt_lang} subtitle line back into {src_lang}. "
        f"Output ONLY the translation, no quotes, no explanation.\n\n{tgt_lang}: {text}\n{src_lang}:"
    )
    return _post(client, prompt, timeout)


def _semantic_hit(src: str, back: str) -> float:
    """极简语义召回：关键 token 重合率（CJK 按字、拉丁按词）。用于回译粗分，非最终裁决。"""
    def toks(s: str) -> set[str]:
        s = (s or "").lower()
        latin = set(re.findall(r"[a-z0-9]+", s))
        cjk = set(re.findall(r"[\u3400-\u9fff]", s))
        return latin | cjk
    a, b = toks(src), toks(back)
    if not a:
        return 0.0
    return len(a & b) / len(a)


_JUDGE_KEYS = ("adequacy", "fluency", "terminology", "no_add_omit", "culture")


def judge_rubric(client, src: str, translation: str, tgt_lang: str, timeout: int = 180) -> dict:
    """Judge 按 rubric 打分，返回 0–5 各项 + 严重度 + 决策。"""
    prompt = (
        f"You are a strict subtitle translation quality judge for {tgt_lang}.\n"
        "Score the translation of the source line on these dimensions, each 0-5 (5 best):\n"
        "1) adequacy (meaning preserved, no omission)\n"
        "2) fluency (natural, idiomatic)\n"
        "3) terminology (proper nouns/brand/music terms correct)\n"
        "4) no_add_omit (no added or dropped content)\n"
        "5) culture (culturally adapted, not awkward literal)\n"
        "MQM severity: use major/critical ONLY for meaning errors (mistranslation, "
        "omission, added content, wrong proper noun). Use 'none' whenever the meaning "
        "is preserved. Do NOT use 'minor' for idiom/honorific style or culture-fit "
        "preference alone — those are valid translation choices, not errors.\n"
        "Respond with ONLY a JSON object, e.g.:\n"
        '{"adequacy":5,"fluency":4,"terminology":5,"no_add_omit":5,"culture":4,"severity":"none"}\n\n'
        f"source: {src}\n{tgt_lang}: {translation}\n"
    )
    raw = _post(client, prompt, timeout)
    result = {k: 0 for k in _JUDGE_KEYS}
    result["severity"] = "unknown"
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            for k in result:
                if k in data:
                    result[k] = data[k]
        except json.JSONDecodeError:
            pass
    sev = str(result.get("severity", "unknown")).lower()
    avg = judge_score(result)
    if sev in ("critical", "major") or avg < 3.0:
        decision = "reject"
    elif sev == "minor" or avg < 4.0:
        decision = "human_review"
    else:
        decision = "accept"
    result["decision"] = decision
    return result


def judge_score(jr: dict) -> float:
    """5 维均分 0–5；未知/解析失败返回 0。"""
    vals = [jr.get(k, 0) for k in _JUDGE_KEYS]
    nums = [float(v) for v in vals if isinstance(v, (int, float))]
    return sum(nums) / len(nums) if nums else 0.0


def run_verify(
    src: str,
    t1: str,
    t2: str | None = None,
    *,
    src_lang: str = "zh",
    tgt_lang: str = "en",
    backtrans_client=None,
    judge_client=None,
) -> VerifyResult:
    """编排：副译不一致则回译 + Judge 裁决；一致则直接 accept（离线评测/单条用）。"""
    if t2 is None or _normalize(t2) == _normalize(t1):
        return VerifyResult(backtrans_score=1.0, judge={"decision": "accept"}, decision="accept")

    res = VerifyResult()
    if backtrans_client is not None:
        back = backtranslate(backtrans_client, t1, src_lang, tgt_lang)
        res.backtrans_score = round(_semantic_hit(src, back), 3)
    if judge_client is not None:
        res.judge = judge_rubric(judge_client, src, t1, tgt_lang)
        res.decision = res.judge.get("decision", "accept")
    return res


def _normalize(text: str) -> str:
    """一致性比对用的归一化：去空白/标点/大小写，保留 CJK 与字母数字。"""
    return re.sub(r"[^\w]+", "", (text or "").lower(), flags=re.UNICODE)


def second_translation(client, lines: list[str], src_lang: str, tgt_lang: str, timeout: int = 180) -> list[str]:
    """副译：用独立的 `verify` 模型把同一批源句重译一遍（批量，1 次调用）。"""
    numbered = "\n".join(f"{i+1}. {s}" for i, s in enumerate(lines))
    prompt = (
        f"Translate these {src_lang} subtitle lines into {tgt_lang}.\n"
        "Rules: natural spoken subtitles, concise; one output line per input line, "
        "same order; do not merge, split, explain, or add notes. "
        "Keep names and numbers consistent.\n"
        "Return ONLY a JSON array of strings.\n\n" + numbered
    )
    raw = _post(client, prompt, timeout)
    arr = _json_array(raw)
    return [str(x).strip() for x in arr]


def proofread_lines(
    src_lines: list[str],
    t1_lines: list[str],
    *,
    src_lang: str = "zh",
    tgt_lang: str = "en",
    client=None,
    mode: str | None = None,
    timeout: int = 180,
) -> tuple[list[str], ProofreadReport]:
    """生产校对入口：副译共识 → 不一致 cue 回译 + Judge → 择优修复。

    返回 (最终译文, 报告)。**任何异常都吞掉并原样返回 t1**，保证翻译 job 不被校对拖垮。
    字幕与笔记/文档本地化共用此函数，避免两条路径各自实现导致再次「不对齐」。
    """
    mode = mode or verify_mode()
    final = list(t1_lines)
    report = ProofreadReport(mode=mode, total=len(t1_lines))
    report.translate_model, report.verify_model = _client_models()

    if mode == "off":
        report.status = "disabled"
        return final, report
    if not t1_lines:
        report.status = "ok"
        return final, report
    # 同族模型无法形成有效异议：跳过并给出明确状态，避免「假校对」。
    if report.translate_model and report.translate_model == report.verify_model:
        report.status = "skipped"
        report.error = "verify model equals translate model; configure llm profile `verify`"
        return final, report

    try:
        c = client or make_client()
        t2 = second_translation(c, src_lines, src_lang, tgt_lang, timeout=timeout)
        if len(t2) != len(t1_lines):
            # 副译漏行是常见格式抖动：重译一次；仍不齐则按 index 补空继续校对，
            # 不让一条格式错误把整批校对作废（缺失行保守保留主译）。
            t2 = second_translation(c, src_lines, src_lang, tgt_lang, timeout=timeout)
        if len(t2) != len(t1_lines):
            report.flags.append({
                "index": -1, "reason": "second_translation_length_mismatch",
                "expected": len(t1_lines), "got": len(t2),
            })
            t2 = (list(t2) + [""] * len(t1_lines))[:len(t1_lines)]

        budget = verify_judge_budget()
        judge_used = 0  # 仅统计真正调用 Judge 的 cue；硬错误 cue 不占预算
        scores: list[float] = []
        for i, (src, primary, alt) in enumerate(zip(src_lines, t1_lines, t2)):
            bad_primary = (is_hard_pollution(primary)
                           or wrong_language(primary, tgt_lang, src)
                           or overlong(primary, src, tgt_lang))
            bad_alt = (is_hard_pollution(alt)
                       or wrong_language(alt, tgt_lang, src)
                       or overlong(alt, src, tgt_lang))
            # 主副译都输出同一个 BV id / 都错写中文时 _normalize 相等 → 旧逻辑
            # 判「共识」直接放行。完整性错误必须先于共识比对处理。
            if bad_primary:
                report.divergent += 1
                if alt and not bad_alt:
                    final[i] = alt
                    report.repaired += 1
                    report.flags.append({"index": i, "src": src, "from": primary,
                                         "to": alt, "reason": "hard_issue_alt"})
                else:
                    report.human_review += 1
                    report.flags.append({"index": i, "src": src, "primary": primary,
                                         "alt": alt, "reason": "hard_issue_unresolved"})
                continue
            if not alt or bad_alt:
                report.consensus += 1  # 副译无有效输出 → 保守保留主译
                continue
            if _normalize(primary) == _normalize(alt):
                report.consensus += 1
                continue

            report.divergent += 1
            needs_judge = mode == "full" or judge_used < budget
            if not needs_judge:
                report.human_review += 1
                report.flags.append({"index": i, "src": src, "primary": primary,
                                     "alt": alt, "reason": "judge_budget_exceeded"})
                continue
            judge_used += 1

            jr = judge_rubric(c, src, primary, tgt_lang, timeout=timeout)
            report.judged += 1
            scores.append(judge_score(jr))
            decision = jr.get("decision", "accept")
            if decision == "accept":
                continue

            # 回译粗分只作诊断（且只在主译有争议时算，省 1 次 LLM/句）。
            try:
                back = backtranslate(c, primary, src_lang, tgt_lang)
                back_score = round(_semantic_hit(src, back), 3)
            except Exception:
                back_score = None

            # 主译被质疑（reject 或 human_review）都让 Judge 评副译择优替换；
            # 旧版只对 reject 择优，minor 一律只打标 → 重跑 20 语 repaired=0。
            jr2 = judge_rubric(c, src, alt, tgt_lang, timeout=timeout)
            report.judged += 1
            scores.append(judge_score(jr2))
            if judge_score(jr2) > judge_score(jr) + 0.25:
                final[i] = alt
                report.repaired += 1
                report.flags.append({"index": i, "src": src, "from": primary,
                                     "to": alt, "reason": "judge_preferred_alt",
                                     "backtrans_score": back_score,
                                     "primary_judge": jr, "alt_judge": jr2})
                continue
            report.human_review += 1
            report.flags.append({"index": i, "src": src, "primary": primary,
                                 "alt": alt, "judge": jr, "alt_judge": jr2,
                                 "backtrans_score": back_score,
                                 "reason": decision})

        if scores:
            report.avg_judge = round(sum(scores) / len(scores), 3)
        report.status = "ok"
    except Exception as e:  # noqa: BLE001 — 校对失败绝不能阻断翻译
        report.status = "error"
        report.error = f"{type(e).__name__}: {e}"
        return list(t1_lines), report
    return final, report


# ── 笔记/文档本地化校对 ────────────────────────────────────────────────
# notes payload 是嵌套 JSON（title/one_liner/summary + focuses/key_points/
# hard_points 里的 title/detail + outline/speakers/topics/quotes 数组）。
# 这些字符串才是"译文"；image/clip/start_sec/语言码是媒体元数据，不能翻也不能改。
_TEXT_LEAF_KEYS = {"title", "one_liner", "summary", "keypoints_intro", "detail", "text"}
_TEXT_CONTAINER_KEYS = {
    "outline", "speakers", "topics", "quotes",
    "focuses", "key_points", "hard_points",
}


def _collect_texts(node, path: tuple, out: list[tuple[tuple, str]]) -> None:
    """收集可译字符串叶子及其路径；跳过媒体/元数据键。"""
    if isinstance(node, dict):
        for key, val in node.items():
            if isinstance(val, str):
                if key in _TEXT_LEAF_KEYS and val.strip():
                    out.append((path + (key,), val))
            elif isinstance(val, (dict, list)) and (
                key in _TEXT_CONTAINER_KEYS or key in _TEXT_LEAF_KEYS
            ):
                _collect_texts(val, path + (key,), out)
    elif isinstance(node, list):
        for idx, val in enumerate(node):
            if isinstance(val, str):
                if val.strip():
                    out.append((path + (idx,), val))
            elif isinstance(val, (dict, list)):
                _collect_texts(val, path + (idx,), out)


def _get_path(node, path: tuple):
    cur = node
    for part in path:
        if isinstance(cur, list):
            if not isinstance(part, int) or part >= len(cur):
                return None
        elif isinstance(cur, dict):
            if part not in cur:
                return None
        else:
            return None
        cur = cur[part]
    return cur


def _set_path(node, path: tuple, value) -> None:
    cur = node
    for part in path[:-1]:
        cur = cur[part]
    cur[path[-1]] = value


def proofread_notes(
    src_payload: dict,
    tgt_payload: dict,
    *,
    src_lang: str,
    tgt_lang: str,
    client=None,
    mode: str | None = None,
) -> tuple[dict, ProofreadReport]:
    """笔记/文档本地化的多模型校对：抽取可译字段 → proofread_lines → 回填。

    与字幕走完全相同的副译共识 + Judge + 择优逻辑（单一真源）。
    任何异常都由 proofread_lines 吞掉，tgt_payload 原样返回。
    """
    import copy

    out = copy.deepcopy(tgt_payload)
    slots: list[tuple[tuple, str]] = []
    _collect_texts(out, (), slots)
    if not slots:
        r = ProofreadReport(status="ok", mode=mode or verify_mode())
        r.translate_model, r.verify_model = _client_models()
        return out, r

    # 源文按同一路径取；结构不齐时用空串（该行只做保守保留）。
    src_lines = [str(_get_path(src_payload, p) or "").replace("\n", " ").strip() for p, _ in slots]
    t1_lines = [t.replace("\n", " ").strip() for _, t in slots]
    final, report = proofread_lines(
        src_lines, t1_lines,
        src_lang=src_lang, tgt_lang=tgt_lang,
        client=client, mode=mode,
    )
    for (path, _orig), flat, text in zip(slots, t1_lines, final):
        # 校对未改动该行(off/异常/共识/全一致)时 final==压平后的 flat,
        # 此时不得写回,否则把多行 summary/detail 静默压成单行。保留 tgt_payload 原文。
        if text == flat:
            continue
        try:
            _set_path(out, path, text)
        except (KeyError, IndexError, TypeError):
            continue
    return out, report
