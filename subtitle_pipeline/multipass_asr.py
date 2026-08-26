#!/usr/bin/env python3
"""Multi-pass Whisper ASR + vote/rule finalize + suspicious re-listen.

Pass A/B/C → merge → mark suspicious (repeat / A·B·C conflict)
→ clip re-listen → optional LLM only on remaining suspicious.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.request
import wave
from collections import Counter
from pathlib import Path

DEFAULT_RULE_FIXES: list[tuple[str, str]] = [
    ("跑路了 油辣 跑路了", "跑路了兄弟 跑路了"),
    ("跑路了油辣跑路了", "跑路了兄弟跑路了"),
    ("跑路了兄弟,跑路了", "跑路了兄弟 跑路了"),
    ("跑路了兄弟，跑路了", "跑路了兄弟 跑路了"),
    ("风后工资卡", "婚后工资卡"),
    ("受祸当气", "受窝囊气"),
    ("祸当气", "窝囊气"),
    ("高老师", "告老师"),
    ("路边烫", "路边摊"),
    ("光还打", "光挨打"),
    ("踏踏实走", "踏踏实实走"),
    ("强100倍", "强一百倍"),
    ("出出兴", "助助兴"),
    ("唱个舞", "唱首歌"),
    ("导演嘉宾", "端茶倒水"),
    ("端点嘉宾", "端茶倒水"),
    ("端饭加冰", "端茶倒水"),
    ("烘配", "烘焙"),
]

PREFERRED_TOKENS = (
    "告老师",
    "窝囊气",
    "路边摊",
    "端茶倒水",
    "助助兴",
    "踏踏实实走",
    "跑路了兄弟",
)

BAD_TOKENS = (
    "祸当气",
    "高老师",
    "路边烫",
    "出出兴",
    "导演嘉宾",
    "端点嘉宾",
    "端饭加冰",
    "光还打",
    "油辣",
)

REPEAT_MIN = 3  # same normalized text ≥ N → chorus / meme → re-listen


def norm_key(text: str) -> str:
    t = re.sub(r"\s+", "", (text or "").strip())
    t = re.sub(r"[，。！？、,.!?;；:：\"'“”]", "", t)
    return t


def rule_fix(text: str, extra: list[tuple[str, str]] | None = None) -> str:
    for a, b in list(DEFAULT_RULE_FIXES) + list(extra or []):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def nearest_text(segs: list[dict], start: float, end: float) -> str:
    best = ""
    best_ov = -1.0
    for s in segs:
        ov = overlap(start, end, s["start"], s["end"])
        if ov > best_ov:
            best_ov = ov
            best = s["text"]
    if best_ov <= 0 and segs:
        mid = (start + end) / 2.0
        best = min(segs, key=lambda x: abs(((x["start"] + x["end"]) / 2.0) - mid))["text"]
    return best


def pick_text(a: str, b: str, c: str) -> str:
    a, b, c = a.strip(), b.strip(), c.strip()
    if not a:
        return rule_fix(c or b)
    cand = a
    for alt in (c, b):
        if not alt:
            continue
        if len(alt) > max(len(a) * 1.8, len(a) + 8):
            continue
        if abs(len(alt) - len(a)) > max(4, len(a) // 3):
            continue
        if any(p in alt for p in PREFERRED_TOKENS) and not any(p in a for p in PREFERRED_TOKENS):
            cand = alt
            break
        if any(x in a for x in BAD_TOKENS) and not any(x in alt for x in BAD_TOKENS):
            cand = alt
            break
    return rule_fix(cand)


def fill_c_gaps(pass_a: list[dict], pass_c: list[dict], merged: list[dict]) -> list[dict]:
    inserts: list[dict] = []
    for s in pass_c:
        dur = max(0.01, float(s["end"]) - float(s["start"]))
        ov = 0.0
        for a in pass_a:
            ov = max(ov, overlap(s["start"], s["end"], a["start"], a["end"]))
        if ov / dur < 0.25 and (s.get("text") or "").strip():
            inserts.append(
                {
                    "start": float(s["start"]),
                    "end": float(s["end"]),
                    "text": rule_fix(s["text"]),
                }
            )
    if not inserts:
        return merged
    out = list(merged) + inserts
    out.sort(key=lambda x: (x["start"], x["end"]))
    for i in range(1, len(out)):
        if out[i - 1]["end"] > out[i]["start"]:
            out[i - 1]["end"] = out[i]["start"]
    cleaned = []
    for s in out:
        if s["end"] <= s["start"]:
            continue
        t = (s.get("text") or "").strip()
        if not t:
            continue
        cleaned.append({"start": s["start"], "end": s["end"], "text": t})
    return cleaned


def merge_passes(
    pass_a: list[dict],
    pass_b: list[dict],
    pass_c: list[dict],
) -> list[dict]:
    merged: list[dict] = []
    for s in pass_a:
        a = s["text"]
        b = nearest_text(pass_b, s["start"], s["end"])
        c = nearest_text(pass_c, s["start"], s["end"])
        text = pick_text(a, b, c)
        if not text:
            continue
        merged.append(
            {
                "start": float(s["start"]),
                "end": float(s["end"]),
                "text": text,
                "a": a,
                "b": b,
                "c": c,
            }
        )
    return fill_c_gaps(pass_a, pass_c, merged)


def mark_suspicious(
    segs: list[dict],
    pass_a: list[dict],
    pass_b: list[dict],
    pass_c: list[dict],
    repeat_min: int = REPEAT_MIN,
) -> list[dict]:
    """Annotate each cue with suspicious reason(s)."""
    counts = Counter(norm_key(s["text"]) for s in segs if norm_key(s["text"]))
    out = []
    for s in segs:
        reasons: list[str] = []
        key = norm_key(s["text"])
        a = s.get("a") or nearest_text(pass_a, s["start"], s["end"])
        b = s.get("b") or nearest_text(pass_b, s["start"], s["end"])
        c = s.get("c") or nearest_text(pass_c, s["start"], s["end"])
        # Already clip-relistened: only keep bad_token if still present
        if s.get("relisten"):
            if any(x in (s["text"] or "") for x in BAD_TOKENS):
                reasons.append("bad_token")
            item = dict(s)
            item["a"], item["b"], item["c"] = a, b, c
            item["suspicious"] = reasons
            out.append(item)
            continue
        if key and counts[key] >= repeat_min:
            reasons.append(f"repeat×{counts[key]}")
        na, nb, nc = norm_key(a), norm_key(b), norm_key(c)
        # conflict only when B/C are short enough to be same cue (not merged)
        if nb and na and nb != na and len(nb) <= max(len(na) * 1.8, len(na) + 6):
            reasons.append("A≠B")
        if nc and na and nc != na and len(nc) <= max(len(na) * 1.8, len(na) + 6):
            reasons.append("A≠C")
        if any(x in (s["text"] or "") for x in BAD_TOKENS):
            reasons.append("bad_token")
        item = dict(s)
        item["a"], item["b"], item["c"] = a, b, c
        item["suspicious"] = reasons
        out.append(item)
    n = sum(1 for x in out if x["suspicious"])
    print(f"[qa] suspicious cues={n}/{len(out)}")
    return out


def cut_wav_clip(src: Path, start: float, end: float, dst: Path, pad: float = 0.12) -> None:
    with wave.open(str(src), "rb") as w:
        rate = w.getframerate()
        width = w.getsampwidth()
        ch = w.getnchannels()
        nframes = w.getnframes()
        a = max(0, int((start - pad) * rate))
        b = min(nframes, int((end + pad) * rate))
        w.setpos(a)
        data = w.readframes(max(0, b - a))
    with wave.open(str(dst), "wb") as out:
        out.setnchannels(ch)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(data)


def relisten_clip(model, clip: Path, language: str | None, hotwords: str | None) -> str:
    kw: dict = {
        "vad_filter": False,
        "beam_size": 8,
        "condition_on_previous_text": False,
        "word_timestamps": True,
        "temperature": 0.0,
    }
    if language:
        kw["language"] = language
    if hotwords:
        kw["initial_prompt"] = hotwords.strip()[:400]
    segs_iter, _ = model.transcribe(str(clip), **kw)
    parts: list[str] = []
    for s in segs_iter:
        t = (s.text or "").strip()
        if t:
            parts.append(t)
    return rule_fix(" ".join(parts))


def apply_relisten(
    model,
    wav: Path,
    segs: list[dict],
    language: str | None,
    hotwords: str | None = None,
) -> list[dict]:
    """Re-ASR each unique repeated / suspicious text once; broadcast to all matches."""
    # Group by norm key among suspicious
    groups: dict[str, list[int]] = {}
    for i, s in enumerate(segs):
        if not s.get("suspicious"):
            continue
        key = norm_key(s["text"]) or f"__idx_{i}"
        groups.setdefault(key, []).append(i)

    if not groups:
        print("[relisten] nothing to do")
        return segs

    print(f"[relisten] {len(groups)} unique suspicious groups")
    with tempfile.TemporaryDirectory(prefix="asr_relisten_") as td:
        tdir = Path(td)
        for key, idxs in groups.items():
            # use first occurrence as clip window
            i0 = idxs[0]
            s0 = segs[i0]
            clip = tdir / f"clip_{i0}.wav"
            cut_wav_clip(wav, s0["start"], s0["end"], clip)
            new_text = relisten_clip(model, clip, language, hotwords)
            old = s0["text"]
            if not new_text:
                print(f"  [relisten] keep {old!r} (empty)")
                continue
            new_text = new_text.replace(",", " ").replace("，", " ")
            new_text = rule_fix(new_text)
            # Reject overlong merges (clip sometimes pulls neighbor speech)
            if len(norm_key(new_text)) > max(len(norm_key(old)) * 2, len(norm_key(old)) + 8):
                print(f"  [relisten] reject long {new_text!r} (keep {old!r})")
                continue
            if norm_key(new_text) == norm_key(old):
                print(f"  [relisten] unchanged {old!r}")
                continue
            print(f"  [relisten] {old!r} => {new_text!r}  (×{len(idxs)})")
            for i in idxs:
                segs[i]["text"] = new_text
                segs[i]["relisten"] = True
                segs[i]["suspicious"] = [
                    r for r in segs[i].get("suspicious") or [] if r != "bad_token"
                ]
    return segs


def _run_one_pass(model, wav: Path, name: str, language: str | None, **kwargs) -> list[dict]:
    print(f"[whisper-mp] pass {name} ...")
    kw = dict(kwargs)
    if language:
        kw["language"] = language
    segs_iter, info = model.transcribe(str(wav), **kw)
    out: list[dict] = []
    for s in segs_iter:
        text = (s.text or "").strip()
        if not text:
            continue
        out.append(
            {
                "start": round(float(s.start), 3),
                "end": round(float(s.end), 3),
                "text": text,
                "avg_logprob": float(getattr(s, "avg_logprob", 0.0) or 0.0),
                "no_speech_prob": float(getattr(s, "no_speech_prob", 0.0) or 0.0),
            }
        )
    print(
        f"[whisper-mp] pass {name} done n={len(out)} "
        f"lang={info.language} p={info.language_probability:.2f}"
    )
    return out


def transcribe_multipass(
    wav: Path,
    model_id: str,
    device: str,
    compute: str,
    language: str | None,
    hotwords: str | None = None,
    release_cuda=None,
    relisten: bool = True,
) -> tuple[list[dict], dict]:
    """Return (merged segments, raw passes dict)."""
    from faster_whisper import WhisperModel

    print(f"[whisper-mp] loading {model_id} device={device} compute={compute}")
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    os.environ.setdefault("MKL_NUM_THREADS", "4")
    model = WhisperModel(
        model_id,
        device=device,
        compute_type=compute,
        cpu_threads=4,
        num_workers=1,
    )
    try:
        pass_a = _run_one_pass(
            model,
            wav,
            "A_baseline",
            language,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        b_kw: dict = {
            "vad_filter": True,
            "beam_size": 8,
            "condition_on_previous_text": True,
        }
        if hotwords:
            b_kw["initial_prompt"] = hotwords.strip()
        pass_b = _run_one_pass(model, wav, "B_hotwords", language, **b_kw)
        pass_c = _run_one_pass(
            model,
            wav,
            "C_novad",
            language,
            vad_filter=False,
            beam_size=5,
            condition_on_previous_text=True,
            temperature=0.0,
        )

        raw = {"A": pass_a, "B": pass_b, "C": pass_c}
        merged = merge_passes(pass_a, pass_b, pass_c)
        merged = mark_suspicious(merged, pass_a, pass_b, pass_c)
        if relisten:
            merged = apply_relisten(model, wav, merged, language, hotwords)
            # re-mark after relisten (repeats may remain but text fixed)
            merged = mark_suspicious(merged, pass_a, pass_b, pass_c)
        print(f"[whisper-mp] merged cues={len(merged)}")
    finally:
        del model
        if release_cuda:
            release_cuda()
        print("[whisper-mp] released model memory")

    return merged, raw


def ollama_generate(
    host: str,
    model: str,
    prompt: str,
    timeout: int = 180,
) -> str:
    """Polish helper — routes through LLM profile (chat role). ``host`` unused."""
    from llm.factory import complete

    return complete("chat", prompt, timeout=timeout, model=model)


def llm_correct_segments(
    segs: list[dict],
    host: str,
    model: str,
    work_triples: list[dict] | None = None,
    only_suspicious: bool = True,
    danmaku_hints: str | None = None,
) -> list[dict]:
    """LLM polish. Default: only cues still marked suspicious (after relisten)."""
    out: list[dict] = []
    for i, s in enumerate(segs, 1):
        a = s["text"]
        sus = s.get("suspicious") or []
        # skip if only_suspicious and nothing left to fix
        if only_suspicious:
            # after relisten_done, skip unless still bad_token / A≠B / A≠C / repeat
            active = [r for r in sus if r not in ("relisten_done",)]
            if not active and not any(x in a for x in BAD_TOKENS):
                out.append({"start": s["start"], "end": s["end"], "text": a})
                continue

        b = c = a
        if work_triples and i - 1 < len(work_triples):
            b = work_triples[i - 1].get("b", a)
            c = work_triples[i - 1].get("c", a)
        elif s.get("b") or s.get("c"):
            b = s.get("b") or a
            c = s.get("c") or a

        hint = f"\n弹幕/热词参考: {danmaku_hints[:300]}\n" if danmaku_hints else "\n"
        prompt = (
            "你是中文字幕校对员。综合三次听写，只输出一句正确口语中文，不要解释。\n"
            "若是重复配乐/梗，优先合理歌词（如「跑路了兄弟 跑路了」）。\n"
            f"{hint}"
            f"可疑原因: {', '.join(sus) or '无'}\n"
            f"A: {a}\nB: {b}\nC: {c}\n校对:"
        )
        try:
            raw = ollama_generate(host, model, prompt)
        except Exception as e:
            print(f"  [correct] #{i} keep ({e})")
            out.append({"start": s["start"], "end": s["end"], "text": a})
            continue
        line = ""
        for ln in raw.splitlines():
            ln = ln.strip().strip("\"'“”")
            ln = re.sub(r"^(校对|答案|输出)[:：]\s*", "", ln)
            if ln:
                line = ln
                break
        if not line or len(line) > max(len(a), 8) * 2.5:
            line = a
        fixed = rule_fix(line)
        out.append({"start": s["start"], "end": s["end"], "text": fixed})
        print(f"  [correct] #{i} {a} => {fixed}")
    return out


def load_hotwords(text: str | None, path: Path | None) -> str | None:
    parts: list[str] = []
    if text:
        parts.append(text.strip())
    if path and path.exists():
        parts.append(path.read_text(encoding="utf-8").strip())
    joined = "\n".join(p for p in parts if p)
    return joined or None


def save_raw(path: Path, raw: dict, meta: dict | None = None) -> None:
    payload = {"meta": meta or {}, "passes": raw}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_internal_fields(segs: list[dict]) -> list[dict]:
    keep = ("start", "end", "text")
    return [{k: s[k] for k in keep if k in s} for s in segs]
