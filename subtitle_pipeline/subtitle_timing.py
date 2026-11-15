"""字幕时间轴规范化 + 硬字幕显示区间对齐（纯逻辑，可单测）。

对齐的最佳实践（Netflix Timed Text / Aegisub / WhisperX）：
- 用 word/字级时间戳替代 faster-whisper 段级时间戳，精确卡住"出现/消失"边界，
  去掉 VAD 默认 400ms 静音填充造成的"太早出现、太迟消失"；
- 相邻 cue 不重叠、留小间隙；单条最小时长 ~0.8s、最大 ~7s；
- 硬字幕源视频：用原片 OCR 提取每句"显示区间"，按文字对齐覆盖到翻译 cue 的
  时间轴，从而严格按"原片字幕显示时间"出轴。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_PUNCT = re.compile(
    r"[\s，。！？、,.!?;；:：\"'“”‘’()（）\-—_/\\|｜]+"
)


def _norm(t: str) -> str:
    """去掉标点/空白，用于字幕文字相似度比较。"""
    return _PUNCT.sub("", (t or "").strip())


def _sim(a: str, b: str) -> float:
    """字幕文字相似度：字符级 Jaccard 与序列比的综合（0~1）。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    jac = inter / union if union else 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    return max(jac, seq * 0.9)


def normalize_cue_timing(
    cues: list[dict],
    *,
    min_dur: float = 0.8,
    max_dur: float = 7.0,
    gap: float = 0.04,
) -> list[dict]:
    """时间轴规范化（best-practice 数值）。

    - 按 start 排序；丢弃/修复非正时长；
    - 收敛重叠：cue[i].start 前移到 cue[i-1].end + gap；
    - 最小时长：在不挤占下一条的前提下把过短 cue 延长到 min_dur；
    - 最大时长：超过 max_dur 的 cue 截断尾端（兜底，正常不应触发）；
    - 不额外前移起点（避免"太早"）。
    """
    if not cues:
        return []
    cs = []
    for c in cues:
        try:
            s = float(c.get("start", 0) or 0)
            e = float(c.get("end", 0) or 0)
        except (TypeError, ValueError):
            continue
        if e <= s:
            e = s + 0.1
        cs.append([s, e, (c.get("text") or "").strip()])
    cs.sort(key=lambda x: (x[0], x[1]))

    # 1) 收敛重叠（前移起点，同时等量后移终点保住时长）：
    # 只推起点会让「整条嵌套在上一条内」的 cue 变成 start>end，进而产出乱序 SRT。
    for i in range(1, len(cs)):
        if cs[i][0] < cs[i - 1][1] + gap:
            delta = (cs[i - 1][1] + gap) - cs[i][0]
            cs[i][0] += delta
            cs[i][1] += delta

    # 2) 最小时长 + 最大时长（受下一条起点约束，避免新重叠）
    for i in range(len(cs)):
        nxt = cs[i + 1][0] if i + 1 < len(cs) else None
        dur = cs[i][1] - cs[i][0]
        if dur < min_dur:
            target = cs[i][0] + min_dur
            if nxt is not None:
                target = min(target, nxt - gap)
            if target > cs[i][0]:
                cs[i][1] = target
        if cs[i][1] - cs[i][0] > max_dur:
            cs[i][1] = cs[i][0] + max_dur
        if cs[i][1] <= cs[i][0]:
            cs[i][1] = cs[i][0] + 0.1

    return [
        {"start": round(s, 3), "end": round(e, 3), "text": t}
        for s, e, t in cs
    ]


def align_cues_to_intervals(
    cues: list[dict],
    intervals: list[dict],
    *,
    sim_thr: float = 0.5,
    lookahead: int = 8,
) -> list[tuple[int, dict | None]]:
    """对齐：cues 与 intervals 均为对话顺序（源字幕与硬字幕显示同序）。

    对每条 cue，在所有**尚未使用**的区间中，取"文字相似且时间就近"的最优者
    （相似度减去时间距离惩罚）。这样重复字幕（同一句出现两次）会各自匹配到
    时间最近的区间，不会因有序指针漂移而错配；恒定水印/场景文字区间因与任何
    cue 文字都不相似而自然落空。
    """
    n = len(intervals)
    used = [False] * n
    out: list[tuple[int, dict | None]] = []
    for i, c in enumerate(cues):
        cs = float(c.get("start", 0) or 0)
        best_idx: int | None = None
        best_score = -1.0
        for k in range(n):
            if used[k]:
                continue
            s = _sim(c.get("text", ""), intervals[k].get("text", ""))
            if s < sim_thr:
                continue
            iv = intervals[k]
            iv_mid = (float(iv.get("start", 0) or 0) + float(iv.get("end", 0) or 0)) / 2.0
            tdist = abs(iv_mid - cs)
            score = s - min(tdist / 30.0, 0.9)  # 30s 距离惩罚封顶 0.9
            if score > best_score:
                best_score = score
                best_idx = k
        if best_idx is not None:
            out.append((i, intervals[best_idx]))
            used[best_idx] = True
        else:
            out.append((i, None))
    return out


def compare_coverage(
    cues: list[dict],
    intervals: list[dict],
    *,
    sim_thr: float = 0.5,
    drift_thr: float = 0.6,
    short_text_max: int = 1,
) -> dict:
    """双向覆盖对比：以原片硬字幕 intervals 为真源，检查软字幕 cues 是否
    完整且不漂移。这是「与原硬字幕对比」门禁的核心——比对齐更严，专门抓
    「硬字幕有、软字幕没有」的漏句（ASR 整句漏转时 align 不会报 UNMATCHED，
    因为没有多余 cue；只有反向查 interval 覆盖才能发现）。

    返回 report:
      - missing_in_srt: 没有任何 cue 覆盖的硬字幕区间（真·缺句）[{start,end,text,reason}]
      - extra_in_srt:   任何硬字幕区间都不像的 cue（ASR 幻觉/错句，软警告）
      - drift:          能配上但 start 偏差 > drift_thr 的 cue [{index,delta,...}]
      - matched / cue_count / interval_count / coverage
      - ok:             missing_in_srt 为空且 coverage >= 0.95

    一条 interval 只要被「任一 cue 文字相似」或「连续多条 cue 拼起来相似」
    （无间隙长字幕被 ASR 切碎的情形）覆盖，即视为已覆盖。
    """
    n = len(intervals)
    covered_iv: set[int] = set()
    covered_cue: set[int] = set()

    # 1) 单条 cue -> interval（文字相似即可，不限时间先后）
    for i, c in enumerate(cues):
        ctxt = _norm(c.get("text", ""))
        if not ctxt:
            continue
        for k, iv in enumerate(intervals):
            if _sim(ctxt, iv.get("text", "")) >= sim_thr:
                covered_iv.add(k)
                covered_cue.add(i)

    # 2) 连续多条 cue 拼接 -> 一条 interval（ASR 把无间隙长字幕切成多句）
    for k, iv in enumerate(intervals):
        if k in covered_iv:
            continue
        ivtxt = _norm(iv.get("text", ""))
        for i in range(len(cues)):
            joined = ""
            for j in range(i, min(i + 6, len(cues))):  # 最多并 6 条，防误吞整段
                joined += _norm(cues[j].get("text", ""))
                if joined and _sim(joined, ivtxt) >= sim_thr:
                    covered_iv.add(k)
                    covered_cue.update(range(i, j + 1))
                    break
            if k in covered_iv:
                break

    # 极短叹词（"啊"/"嗯"，<=short_text_max 字）单独列为软告警，不阻断门禁。
    missing, minor_missing = [], []
    for k, iv in enumerate(intervals):
        if k in covered_iv:
            continue
        txt = (iv.get("text") or "").strip()
        item = {"start": round(float(iv.get("start", 0) or 0), 3),
                "end": round(float(iv.get("end", 0) or 0), 3),
                "text": txt, "reason": "hardsub_line_absent_in_srt"}
        (minor_missing if len(_norm(txt)) <= short_text_max else missing).append(item)
    extra = [
        {"index": i, "start": round(float(c.get("start", 0) or 0), 3),
         "text": (c.get("text") or "").strip(), "reason": "srt_line_not_in_hardsub"}
        for i, c in enumerate(cues) if i not in covered_cue
    ]

    # 3) 时间漂移：对文字高度相似（>=0.8）的 cue/interval 配对照起点。
    # 重复句（"打死他"×3）必须取「时间最近」的那条 interval，不能都配到第一条，
    # 否则会误报巨大漂移。用 (相似度, -时间距离) 做排序键。
    used_iv_drift: set[int] = set()
    drift = []
    cand = []
    for i, c in enumerate(cues):
        ctxt = _norm(c.get("text", ""))
        if not ctxt:
            continue
        cs = float(c.get("start", 0) or 0)
        for k, iv in enumerate(intervals):
            s = _sim(ctxt, iv.get("text", ""))
            if s >= 0.8:
                ivs = float(iv.get("start", 0) or 0)
                cand.append((i, k, s, abs(cs - ivs)))
    # 每条 cue 取最相似、同相似则时间最近；interval 在漂移配对里只用一次
    cand.sort(key=lambda x: (-x[2], x[3]))
    seen_cue: set[int] = set()
    for i, k, _s, _td in cand:
        if i in seen_cue or k in used_iv_drift:
            continue
        seen_cue.add(i)
        used_iv_drift.add(k)
        cs = float(cues[i].get("start", 0) or 0)
        ivs = float(intervals[k].get("start", 0) or 0)
        delta = round(cs - ivs, 3)
        if abs(delta) > drift_thr:
            drift.append({"index": i, "text": (cues[i].get("text") or "").strip(),
                          "srt_start": round(cs, 3), "hardsub_start": round(ivs, 3),
                          "delta": delta})

    # 覆盖率分母排除极短叹词（它们已单列为 minor_missing，不参与门禁判定）
    effective = n - len(minor_missing)
    coverage = (len(covered_iv) / effective) if effective > 0 else 1.0
    return {
        "cue_count": len(cues),
        "interval_count": n,
        "matched": len(covered_iv),
        "coverage": round(coverage, 4),
        "missing_in_srt": missing,
        "minor_missing": minor_missing,
        "extra_in_srt": extra,
        "drift": drift,
        "ok": (not missing) and coverage >= 0.95,
    }


def merge_adjacent_intervals(
    intervals: list[dict],
    *,
    gap_max: float = 0.4,
    sim_thr: float = 0.85,
) -> list[dict]:
    """合并「同句闪断重显」的相邻区间：间隔 <gap_max 且文字相似。

    后一条文本**不得比前一条短**："打死他打死他"→"打死他" 是换了新句
    （子串因字符集相同 sim 恒 1.0，必须靠长度拦），误并成一条长 interval
    再被 max_dur 截断会吞掉后一句。
    """
    merged: list[dict] = []
    for iv in intervals:
        if (merged and iv["start"] - merged[-1]["end"] < gap_max
                and _sim(iv.get("text", ""), merged[-1].get("text", "")) >= sim_thr
                and len(_norm(iv.get("text", ""))) >= len(_norm(merged[-1].get("text", "")))):
            merged[-1]["end"] = iv["end"]
        else:
            merged.append(dict(iv))
    return merged


def apply_interval_timing(
    cues: list[dict],
    aligned: list[tuple[int, dict | None]],
    *,
    pad: float = 0.0,
) -> list[dict]:
    """按对齐结果生成新 cues：命中 → 用硬字幕区间时间；未命中 → 保留原时间。

    ``pad`` 为可选的额外留白（默认 0 = 严格等于原片显示时间）。
    """
    res: list[dict] = []
    for i, c in enumerate(cues):
        iv = aligned[i][1] if i < len(aligned) else None
        if iv:
            s = max(0.0, float(iv.get("start", 0) or 0) - pad)
            e = float(iv.get("end", 0) or 0) + pad
            if e <= s:
                e = s + 0.1
            res.append({"start": round(s, 3), "end": round(e, 3), "text": c.get("text", "")})
        else:
            res.append(
                {
                    "start": round(float(c.get("start", 0) or 0), 3),
                    "end": round(float(c.get("end", 0) or 0), 3),
                    "text": c.get("text", ""),
                }
            )
    return res
