"""Translation integrity — 源/译文硬污染的单一真源防线。

2026-11-13 事故：占位标题 ``try:bilibili:BV1sgWn68Ewt`` 被当 ASR 热词，
B 遍 Whisper 整段吐出 BV 号；投票融合把 6 条 cue 写成 ID，20 语译文原样
复制（主副译"同污共识"还会绕过 Judge）；另一个视频的厨房术语表被复用，
越南语轨出现 ``{'Vietnamese': 'Lau đũa'}``。格式 QA 只查 cue 数/时间轴，
因此全部漏过。

本模块被 multipass_asr / pipeline / llm.verify / discover.run_batch 共用：
- is_hard_pollution / clean_model_output：识别并拒收 ID、占位符、字典字面量
- sanitize_hotwords：热词清洗（不把 job id 当词汇种子）
- repair_source_segments：从 multipass_raw 的 A/C 遍恢复被污染的源 cue
- filter_glossary_by_source：跨 job 术语表（完全不相关）不得进入翻译提示词
- qa_source_integrity / qa_translation_integrity：写进 QA 的结构化检查
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from langs import to_traditional

_ID_RE = re.compile(r"\bBV[0-9A-Za-z]{8,}\b", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"^(?:try|job|pack)\s*[:：]\s*[\w.\-]+$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_DICT_RE = re.compile(r"^\s*\{['\"]?[\w\u4e00-\u9fff]+['\"]?\s*:.+?\}\s*$", re.DOTALL)
_LANG_KEY_RE = re.compile(r"\{['\"][A-Za-z\u4e00-\u9fff]+['\"]\s*:")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_PUNCT_RE = re.compile(r"[\s，。！？、,.!?;；:：\"'“”‘’()（）\-]")

CJK_LANGS = {"zh", "zh-Hant", "ja", "ko"}
# proofread_lines 传的是 lang_name()（如 "Japanese"），pipeline 传的是码（"ja"）。
_CJK_NAME_MAP = {
    "japanese": "ja", "korean": "ko",
    "traditional chinese": "zh-Hant", "chinese (traditional)": "zh-Hant",
    "simplified chinese": "zh", "chinese": "zh",
}
_CODE_ALIASES = {
    "zh-hant": "zh-Hant", "zh-tw": "zh-Hant", "zh-hk": "zh-Hant",
    "zh-hans": "zh", "zh-cn": "zh", "ja-jp": "ja", "ko-kr": "ko",
}


def _lang_code(value: str) -> str:
    key = str(value or "").strip().lower()
    return _CJK_NAME_MAP.get(key) or _CODE_ALIASES.get(key) or key


def _cjk_overlap(a: str, b: str) -> float:
    ca = set(_CJK_RE.findall(_PUNCT_RE.sub("", a or "")))
    cb = set(_CJK_RE.findall(_PUNCT_RE.sub("", b or "")))
    return len(ca & cb) / max(1, len(ca), len(cb)) if cb else 0.0


# opencc 的 s2t 不会把"繁体异体字"归并到标准繁体（如 絶 ≠ 絕：二者都是 绝 的
# 繁写，仅字形不同）。模型在日轨常输出异体字形，导致与 to_traditional(src) 比对
# 失败、漏判"照抄中文"。下面是音义全同的纯字形异体，比较前统一归并到标准繁体。
_VARIANT_TRANS = str.maketrans({
    "絶": "絕", "裡": "裏", "峯": "峰", "羣": "群",
    "䌽": "彩", "甦": "蘇", "堃": "坤", "喆": "哲",
})


def _shared_kanji_idiom(text: str, src: str) -> bool:
    """日中共用的四字熟语（天下無敵 等）：纯汉字、≤4 字、且与繁体源**不同形**。

    2026-11-15 修正：若译文只是把中文源句原样照抄（含繁体异体，如 絶無二心），
    那不是"共用熟语"，而是漏译/未翻译——必须让 ``wrong_language`` 判硬错误、
    交给副译修复（proofread_lines 在主译硬错误且副译干净时会自动采用副译）。
    只有与源句**不同形**（用不同汉字表达，确属日语写法，如 ``忠孝節義`` ≠ 源
    ``威风八面``）才放行。比较前双方都过 ``to_traditional`` + 异体归一，避免
    异体字导致的漏检。

    >>> _shared_kanji_idiom("忠孝節義", "威风八面")   # 不同形，真日语熟语
    True
    >>> _shared_kanji_idiom("威風八面", "威风八面")   # 照抄源，非熟语
    False
    >>> _shared_kanji_idiom("絶無二心", "绝无二心")   # 异体照抄，同样非熟语
    False
    """
    t = (text or "").strip()
    if not t or len(t) > 4 or not all(_CJK_RE.match(ch) for ch in t):
        return False
    trad_t = to_traditional(t).translate(_VARIANT_TRANS)
    trad_src = to_traditional(src or "").translate(_VARIANT_TRANS)
    if trad_t == trad_src:
        return False  # 与源同形（含异体）= 照抄中文，不是共用熟语
    return True


def wrong_language(text: str | None, target_lang: str, src: str = "") -> bool:
    """目标语不是该语言（非 CJK 语写中文 / 日韩轨直接抄中文源句）。

    硬错误：需要副译替换或强制重译；与 is_hard_pollution 分开统计。
    """
    t = (text or "").strip()
    if not t or is_hard_pollution(t):
        return False
    lang = _lang_code(target_lang)
    if lang not in CJK_LANGS:
        return bool(_CJK_RE.search(t))
    if lang in ("zh", "zh-Hant"):
        return False          # 简繁同形是合法结果，不能判「未翻译」
    if lang == "ko":
        return bool(_CJK_RE.search(t)) and not _HANGUL_RE.search(t)
    if lang == "ja":
        # 生产基线：日文字幕应出现假名；纯汉字串（抄中文/术语表说明）要重译。
        # 例外：日中共用四字熟语（天下無敵）本身合法，只留 ja_no_kana 软提示。
        if not _CJK_RE.search(t) or _KANA_RE.search(t):
            return False
        return not _shared_kanji_idiom(t, src)
    return False


def overlong(text: str | None, src: str, target_lang: str) -> bool:
    """日/韩轨译文远长于源句 = 串句或把语境说明当台词（拉丁语系不判，易误伤）。"""
    lang = _lang_code(target_lang)
    if lang not in ("ja", "ko"):
        return False
    t, s = (text or "").strip(), (src or "").strip()
    if not t or not s:
        return False
    return len(t) > max(len(s) * 2.2, len(s) + 6)


def is_hard_pollution(text: str | None) -> bool:
    """ID / 占位标题 / 字典字面量 / 裸 URL：绝不可能是合法字幕译文。"""
    t = (text or "").strip()
    if not t:
        return False
    if _ID_RE.search(t) or _PLACEHOLDER_RE.match(t):
        return True
    if _DICT_RE.match(t) or _LANG_KEY_RE.search(t):
        return True
    if _URL_RE.fullmatch(t):
        return True
    return False


def sanitize_hotwords(text: str | None) -> str | None:
    """清掉占位标题 / 视频 ID / URL 行，保留真正的领域词。

    try 工作台把 ``try:<platform>:<id>`` 当占位标题传给 ASR 热词，会让
    faster-whisper 把 ID 当台词反复吐出（B 遍 + relisten 全污染）。
    """
    if not text:
        return None
    kept: list[str] = []
    for line in str(text).splitlines():
        raw = line.strip()
        if not raw:
            continue
        cleaned = _ID_RE.sub("", raw)
        cleaned = _PLACEHOLDER_RE.sub("", cleaned)
        cleaned = _URL_RE.sub("", cleaned).strip(" ,;:：-")
        if cleaned and not is_hard_pollution(cleaned):
            kept.append(cleaned)
    return "\n".join(kept) or None


def clean_model_output(text: str | None) -> str:
    """模型返回 dict/ID/占位符时视为无效，交给调用方重试或回落。"""
    t = (text or "").strip()
    return "" if is_hard_pollution(t) else t


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _nearest_clean(passes: list[dict], start: float, end: float) -> str:
    """取与 [start,end] 重叠最多且不含硬污染的文本；无重叠时取时间中点最近者。"""
    best, best_ov = "", 0.0  # 仅正重叠才选中；全为 0 时走下方时间中点回退
    for s in passes:
        cand = (s.get("text") or "").strip()
        if not cand or is_hard_pollution(cand):
            continue
        ov = _overlap(start, end, float(s["start"]), float(s["end"]))
        if ov > best_ov:
            best, best_ov = cand, ov
    if best:
        return best
    mid = (start + end) / 2.0
    cands = [s for s in passes if (s.get("text") or "").strip()
             and not is_hard_pollution(s["text"])]
    if not cands:
        return ""
    return min(cands, key=lambda s: abs(((float(s["start"]) + float(s["end"])) / 2) - mid))["text"].strip()


def qa_source_integrity(segments: list[dict], *, source_lang: str = "zh") -> dict:
    issues = [
        {"index": i + 1, "text": (s.get("text") or "").strip(), "reason": "hard_pollution"}
        for i, s in enumerate(segments)
        if is_hard_pollution(s.get("text"))
    ]
    return {
        "source_lang": source_lang,
        "cues": len(segments),
        "issues": issues,
        "ok": not issues,
    }


def repair_source_segments(
    segments: list[dict],
    out_dir: Path,
    *,
    write_report: bool = True,
) -> tuple[list[dict], dict]:
    """翻译前修复被 ID 污染的源 cue（A/C 遍恢复）；无法恢复则明确报错。

    与「校对失败不阻断翻译」的铁律不同：源字幕本身不是台词时，继续翻译只会
    把污染复制进 20 语，必须 fail-fast 并给出可操作提示。
    """
    out_dir = Path(out_dir)
    before = qa_source_integrity(segments)
    if before["ok"]:
        return segments, {"before": before, "after": before, "repaired": 0,
                          "unresolved": 0, "raw_used": False}

    raw_path = out_dir / "media" / "multipass_raw.json"
    passes: dict[str, list[dict]] = {}
    if raw_path.is_file():
        try:
            data = json.loads(raw_path.read_text(encoding="utf-8"))
            passes = {k: v for k, v in (data.get("passes") or {}).items()
                      if isinstance(v, list)}
        except (OSError, ValueError, TypeError):
            passes = {}

    repaired, unresolved = 0, 0
    out: list[dict] = []
    for i, seg in enumerate(segments, 1):
        s = dict(seg)
        if is_hard_pollution(s.get("text")):
            cand = ""
            for key in ("C", "A", "B"):
                cand = _nearest_clean(passes.get(key) or [],
                                      float(s["start"]), float(s["end"]))
                if cand:
                    break
            if cand:
                s["text"] = cand
                repaired += 1
            else:
                unresolved += 1
        out.append(s)

    after = qa_source_integrity(out)
    report = {
        "before": before,
        "after": after,
        "repaired": repaired,
        "unresolved": unresolved,
        "raw_used": bool(passes),
    }
    if write_report:
        (out_dir / "media").mkdir(parents=True, exist_ok=True)
        (out_dir / "media" / "translate_source_qa.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if unresolved:
        raise RuntimeError(
            f"源字幕有 {unresolved} 条 ID/占位符污染且 multipass_raw 无可用听写，"
            f"拒绝翻译以免污染全部语种：{out_dir}"
        )
    return out, report


def filter_glossary_by_source(glossary: dict | None, src_lines: list[str]) -> tuple[dict, dict]:
    """术语表与源文完全不相干时丢弃（跨 job 复用会串稿，如厨房词进 AI 对话）。"""
    gloss = glossary or {}
    joined = "\n".join(str(x or "") for x in src_lines)
    kept: dict = {"names": {}, "terms": {}, "asr_fix": {}}
    dropped: dict = {"names": [], "terms": [], "asr_fix": []}
    for section in ("names", "terms", "asr_fix"):
        for key, val in (gloss.get(section) or {}).items():
            if str(key) and str(key) in joined:
                kept[section][key] = val
            else:
                dropped[section].append(key)
    total = sum(len(gloss.get(k) or {}) for k in ("names", "terms", "asr_fix"))
    kept_n = sum(len(v) for v in kept.values())
    report = {
        "total": total,
        "kept": kept_n,
        "dropped": total - kept_n,
        "dropped_keys": dropped,
        "ok": total == 0 or kept_n > 0,
    }
    return (gloss if report["ok"] else {}), report


def qa_translation_integrity(
    source: list[dict],
    target: list[dict],
    *,
    target_lang: str,
) -> dict:
    """结构化译文检查：issues=硬错误(影响 ok)，warnings=软提示(人工参考)。

    zh-Hant 与简体中文本来就同形（「大人的武功」），不能判「未翻译」；
    日语合法四字熟语也可以无假名，故无假名只作 warning，只有与源句汉字
    高度重合（直接抄中文）或非 CJK 语种出现汉字才是硬错误。
    """
    issues: list[dict] = []
    warnings: list[dict] = []
    lang = _lang_code(target_lang)
    n = min(len(source), len(target))
    for i in range(n):
        src = (source[i].get("text") or "").strip()
        tgt = (target[i].get("text") or "").strip()
        if is_hard_pollution(tgt):
            issues.append({"index": i + 1, "text": tgt, "reason": "hard_pollution"})
            continue
        same_as_src = (
            _PUNCT_RE.sub("", tgt) == _PUNCT_RE.sub("", src)
            or _PUNCT_RE.sub("", tgt) == _PUNCT_RE.sub("", to_traditional(src))
        )
        if (
            lang != "zh-Hant" and src and same_as_src
            and not (lang == "ja" and _shared_kanji_idiom(tgt, src))
        ):
            issues.append({"index": i + 1, "text": tgt, "reason": "untranslated"})
        if lang not in CJK_LANGS and _CJK_RE.search(tgt):
            issues.append({"index": i + 1, "text": tgt, "reason": "cjk_residue"})
        if lang == "ja" and _CJK_RE.search(tgt) and not _KANA_RE.search(tgt):
            warnings.append({"index": i + 1, "text": tgt, "reason": "ja_no_kana"})
        if lang in ("ja", "ko") and wrong_language(tgt, lang, src):
            issues.append({"index": i + 1, "text": tgt, "reason": "wrong_language"})
        if overlong(tgt, src, lang):
            issues.append({"index": i + 1, "text": tgt, "reason": "overlong"})
        if lang == "zh-Hant" and (
            len(_PUNCT_RE.sub("", src)) != len(_PUNCT_RE.sub("", tgt))
        ):
            issues.append({"index": i + 1, "text": tgt,
                           "reason": "zh_hant_length_mismatch"})
    if len(source) != len(target):
        issues.append({"index": 0, "text": "", "reason": "cue_count_mismatch"})
    return {
        "target_lang": target_lang,
        "cues": n,
        "issues": issues,
        "warnings": warnings,
        "ok": not issues,
    }
