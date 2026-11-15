#!/usr/bin/env python3
"""Multi-pass Whisper ASR + vote/rule finalize + suspicious re-listen.

Pass A/B/C → merge → mark suspicious (repeat / A·B·C conflict)
→ clip re-listen → optional LLM only on remaining suspicious.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave
from collections import Counter
from pathlib import Path

from translation_integrity import is_hard_pollution, sanitize_hotwords

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
    # kitchen / short-video ASR near-misses
    ("洗一次烧", "洗一次勺"),
    ("不用洗烧", "不用洗勺"),
    ("还不用洗烧", "还不用洗勺"),
    ("还不用洗烧勺", "还不用洗勺"),
    ("水龙头已离结", "水龙头已拧结"),
    ("水龙头与李杰", "水龙头已拧结"),
    ("水龙头已李杰", "水龙头已拧结"),
    ("既不穿位", "既不串味"),
    ("不穿位", "不串味"),
    ("既不串位", "既不串味"),
    ("要好了岗位", "要好了感慨"),
    ("安装要好了岗位", "安装要好了感慨"),
    ("蒜皮煮动", "蒜皮自动"),
    ("顺其主动脱离", "蒜皮自动脱离"),
    ("隔下金盔", "阁下金贵"),
    ("隔下金贵", "阁下金贵"),
    ("不惜牛市", "不行牛屎"),
    ("不幸牛市", "不行牛屎"),
    ("大大提升企业指数", "大大提升幸福指数"),
    ("效率直超留学线", "效率直超流水线"),
    ("是为了好宰", "是为了好摘"),
    # martial-arts / short-drama near-misses
    ("绝无耳心", "绝无二心"),
    ("绝无二新", "绝无二心"),
    ("微风八面", "威风八面"),
    ("所向批评", "所向披靡"),
    ("人尽其难", "人尽其才"),
    ("而经常切朋友的辣椒", "而经常切到手的辣椒"),
]

PREFERRED_TOKENS = (
    "告老师",
    "窝囊气",
    "路边摊",
    "端茶倒水",
    "助助兴",
    "踏踏实实走",
    "跑路了兄弟",
    "洗勺",
    "拧结",
    "串味",
    "感慨",
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
    "洗一次烧",
    "不穿位",
    "离结",
    "岗位",
    # Bilibili / YouTube outro & hard-sub credit hallucinations
    "明镜与点点",
    "请不吝点赞",
    "中文字幕志愿者",
    "词曲",
    "订阅订阅",
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
    # 热词污染（BV id / try: 占位标题）不得进入 A/B/C 投票，也不能作为 LLM 校对参考。
    if is_hard_pollution(b):
        b = ""
    if is_hard_pollution(c):
        c = ""
    if is_hard_pollution(a):
        return rule_fix(c or b or "")
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
        text = rule_fix(s.get("text") or "")
        if ov / dur < 0.25 and text and not any(x in text for x in BAD_TOKENS):
            inserts.append(
                {
                    "start": float(s["start"]),
                    "end": float(s["end"]),
                    "text": text,
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
        if is_hard_pollution(b):
            b = ""
        if is_hard_pollution(c):
            c = ""
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
        if is_hard_pollution(s.get("text")):
            reasons.append("hard_pollution")
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
            # Reject outro / credit / watermark hallucinations
            if any(x in new_text for x in BAD_TOKENS) and not any(
                x in old for x in BAD_TOKENS
            ):
                print(f"  [relisten] reject bad-token {new_text!r} (keep {old!r})")
                continue
            if is_hard_pollution(new_text):
                print(f"  [relisten] reject id/placeholder {new_text!r} (keep {old!r})")
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


def refine_bounds_from_words(seg) -> tuple[float, float]:
    """用 word 级时间戳裁掉段级 VAD 静音填充（speech_pad_ms=400 造成早/迟）。

    返回 (start, end)：取段内首/末词边界，并夹紧在段边界附近（word 偶发越界）。
    无词时间戳时回退段级时间。这是 WhisperX 思路的轻量落地：段级时间戳本身
    不准（首尾含静音），字级对齐才能精确卡住"出现/消失"边界。
    """
    seg_start = float(getattr(seg, "start", 0) or 0)
    seg_end = float(getattr(seg, "end", 0) or 0)
    words = getattr(seg, "words", None)
    if not words:
        return seg_start, seg_end
    ws = [float(w.start) for w in words if getattr(w, "start", None) is not None]
    we = [float(w.end) for w in words if getattr(w, "end", None) is not None]
    if not ws or not we:
        return seg_start, seg_end
    start = min(ws)
    end = max(we)
    start = max(seg_start - 0.05, min(start, seg_end))
    end = min(seg_end + 0.05, max(end, seg_start))
    if end <= start:
        return seg_start, seg_end
    return start, end


def _run_one_pass(
    model, wav: Path, name: str, language: str | None, **kwargs
) -> tuple[list[dict], str]:
    print(f"[whisper-mp] pass {name} ...")
    kw = dict(kwargs)
    if language:
        kw["language"] = language
    # word 级时间戳：精确卡住语音边界，去掉段级 VAD 静音填充造成的早/迟。
    kw.setdefault("word_timestamps", True)
    segs_iter, info = model.transcribe(str(wav), **kw)
    out: list[dict] = []
    for s in segs_iter:
        text = (s.text or "").strip()
        if not text:
            continue
        start, end = refine_bounds_from_words(s)
        out.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "avg_logprob": float(getattr(s, "avg_logprob", 0.0) or 0.0),
                "no_speech_prob": float(getattr(s, "no_speech_prob", 0.0) or 0.0),
            }
        )
    detected = str(getattr(info, "language", "") or "")
    print(
        f"[whisper-mp] pass {name} done n={len(out)} "
        f"lang={detected} p={info.language_probability:.2f}"
    )
    return out, detected


# Silero VAD defaults aligned with faster-whisper production guides.
VAD_PARAMS_DEFAULT = {
    "min_silence_duration_ms": 500,
    "speech_pad_ms": 400,
    "threshold": 0.5,
}


# Global lock: serialize Whisper model load+transcribe so at most one model is
# resident in RAM/VRAM at any time. Prevents N× OOM (mkl_malloc abort) when
# multiple ASR jobs run concurrently (e.g. batch workers, or future parallelism).
# Single sequential runs are unaffected — the lock is uncontended.
_ASR_MODEL_LOCK = threading.Lock()


def _memory_snapshot() -> dict[str, float] | None:
    """Windows 内存快照(GB)，明确区分物理 RAM 与提交量/页面文件三种口径。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = _MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        gb = 1024 ** 3
        return {
            "ram_total": st.ullTotalPhys / gb,
            "ram_avail": st.ullAvailPhys / gb,
            # ull*PageFile 是「提交上限 / 剩余可提交量」，不是页面文件使用量；
            # 页面文件大小 ≈ 提交上限 - 物理内存。
            "commit_limit": st.ullTotalPageFile / gb,
            "commit_avail": st.ullAvailPageFile / gb,
            "pagefile_size": max(0.0, (st.ullTotalPageFile - st.ullTotalPhys) / gb),
        }
    except Exception:
        return None


def _available_ram_gb() -> float | None:
    """Best-effort available physical RAM in GB; None if undetected."""
    snap = _memory_snapshot()
    return snap["ram_avail"] if snap else None


def _vram_free_gb() -> float | None:
    """Best-effort free VRAM via nvidia-smi; None when no NVIDIA GPU / CLI."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if r.returncode != 0 or not (r.stdout or "").strip():
            return None
        return int(r.stdout.strip().splitlines()[0].strip()) / 1024
    except Exception:
        return None


def _log_asr_memory(model_id: str, device: str | None = None) -> None:
    """加载前打印三种独立口径的内存，避免把物理 RAM / 提交量 / 显存混为一谈。"""
    snap = _memory_snapshot()
    if snap is None:
        return
    avail = snap["ram_avail"]
    # 日志保持 ASCII：Windows 控制台可能是 GBK，中文/特殊符号会显示成乱码。
    print(
        f"[whisper-mp] memory: RAM avail={avail:.1f}/{snap['ram_total']:.1f}GB | "
        f"commit avail={snap['commit_avail']:.1f}/{snap['commit_limit']:.1f}GB | "
        f"pagefile={snap['pagefile_size']:.1f}GB"
    )
    if device == "cuda":
        vram = _vram_free_gb()
        if vram is not None:
            print(
                f"[whisper-mp] memory: VRAM free={vram:.1f}GB "
                "(separate from system RAM)"
            )
    if avail < LOW_RAM_GB:
        print(
            f"[whisper-mp] WARNING: low RAM ({avail:.1f}GB) for {model_id}; "
            f"MKL may fail mkl_malloc; DNNL backend is used at low RAM."
        )


# 低于该可用物理内存(GB)时把 ctranslate2 从 MKL 切到 DNNL；同时是告警线。
# 实测依据(ctranslate2 4.8.1, CT2_VERBOSE 可观测后端, 2026-11):
#   - RAM 6.4GB + commit 6.3GB (2026-11-05 现场): MKL mkl_malloc 进程级失败
#   - RAM 8GB  + commit 宽裕   (2026-11-14): MKL 加载 large-v3 成功(avail 8→6.15GB)
#   - RAM 10GB / 15.5GB + commit 宽裕: MKL 均成功
# 故最坏失败点约 6.5GB 物理 RAM(且 commit 也紧张时)；9GB 是覆盖最坏情况的安全余量，
# 不是模型内存需求(large-v3 int8 实测仅占约 1.5GB RAM)。>=9GB 保持 MKL(packed GEMM)性能。
LOW_RAM_GB = 9.0


def ensure_ct2_backend() -> None:
    """低内存 Windows 上把 ctranslate2 从 MKL 切到 DNNL，避开 mkl_malloc 失败。

    后端在原生扩展初始化时确定，因此必须在 import faster_whisper 之前调用；
    CT2_USE_MKL=0 使 SGEMM/GEMM_S8 走 DNNL（CT2_VERBOSE=1 可观测）。
    用户显式设置的 CT2_* / OMP_* 环境变量一律优先，大内存机器保持 MKL 性能。
    """
    if os.name != "nt" or "CT2_USE_MKL" in os.environ:
        return
    if "ctranslate2" in sys.modules or "faster_whisper" in sys.modules:
        return
    avail = _available_ram_gb()
    if avail is None or avail >= LOW_RAM_GB:
        return
    os.environ["CT2_USE_MKL"] = "0"
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("MKL_NUM_THREADS", "2")
    print(f"[whisper-mp] low RAM ({avail:.1f}GB): MKL -> DNNL backend")


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
    ensure_ct2_backend()
    from faster_whisper import WhisperModel

    # 占位标题 / 视频 ID 不能当词汇种子（会让 B 遍与 relisten 整段吐 BV 号）。
    hotwords = sanitize_hotwords(hotwords)
    _log_asr_memory(model_id, device)
    print(f"[whisper-mp] loading {model_id} device={device} compute={compute}")
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    os.environ.setdefault("MKL_NUM_THREADS", "4")
    _ASR_MODEL_LOCK.acquire()
    try:
        model = WhisperModel(
            model_id,
            device=device,
            compute_type=compute,
            cpu_threads=4,
            num_workers=1,
        )
    except Exception:
        _ASR_MODEL_LOCK.release()
        raise
    try:
        pass_a, lang_a = _run_one_pass(
            model,
            wav,
            "A_baseline",
            language,
            vad_filter=True,
            vad_parameters=dict(VAD_PARAMS_DEFAULT),
            beam_size=5,
            best_of=5,
            temperature=0.0,
            condition_on_previous_text=False,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
        )
        b_kw: dict = {
            "vad_filter": True,
            "vad_parameters": dict(VAD_PARAMS_DEFAULT),
            "beam_size": 8,
            "best_of": 5,
            "temperature": 0.0,
            "condition_on_previous_text": True,
            "compression_ratio_threshold": 2.4,
            "log_prob_threshold": -1.0,
            "no_speech_threshold": 0.6,
        }
        if hotwords:
            # Vocabulary seed only (not instructions) - faster-whisper best practice.
            b_kw["initial_prompt"] = hotwords.strip()[:224]
            b_kw["hotwords"] = hotwords.strip()[:64]
        pass_b, _lang_b = _run_one_pass(model, wav, "B_hotwords", language, **b_kw)
        pass_c, _lang_c = _run_one_pass(
            model,
            wav,
            "C_novad",
            language,
            vad_filter=False,
            beam_size=5,
            best_of=5,
            condition_on_previous_text=True,
            temperature=0.0,
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
        )

        raw = {"A": pass_a, "B": pass_b, "C": pass_c, "lang": lang_a}
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
        _ASR_MODEL_LOCK.release()

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
            # 无剩余可疑原因且无非预期 token → 直接保留原句
            if not sus and not any(x in a for x in BAD_TOKENS):
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
            "你是中文字幕校对员。综合 A/B/C 三次听写，只输出【这一条】正确口语中文。\n"
            "硬性规则：\n"
            "1. 禁止合并、拼接邻句；禁止补上下句内容。\n"
            "2. 只改明显错词/同音误听，不要改写风格。\n"
            "3. 输出长度必须接近 A（±4 字以内），不要解释、不要标点说明。\n"
            "4. 若是重复配乐/梗，优先合理歌词（如「跑路了兄弟 跑路了」）。\n"
            f"{hint}"
            f"可疑原因: {', '.join(sus) or '无'}\n"
            f"A: {a}\nB: {b}\nC: {c}\n校对:"
        )
        try:
            raw = ollama_generate(host, model, prompt)
        except Exception as e:
            print(f"  [correct] #{i} keep ({e})")
            out.append({"start": s["start"], "end": s["end"], "text": rule_fix(a)})
            continue
        line = ""
        for ln in raw.splitlines():
            ln = ln.strip().strip("\"'“”")
            ln = re.sub(r"^(校对|答案|输出)[:：]\s*", "", ln)
            if ln:
                line = ln
                break
        # Reject merges / hallucinations that balloon past the source cue.
        max_len = max(len(a) + 4, int(len(a) * 1.3) + 1)
        if not line or len(line) > max_len:
            if line and len(line) > max_len:
                print(f"  [correct] #{i} reject long ({len(line)}>{max_len}) keep A")
            line = a
        fixed = rule_fix(line)
        if is_hard_pollution(fixed):
            for cand in (a, c, b):
                if cand and not is_hard_pollution(cand):
                    fixed = rule_fix(cand)
                    break
        # Reject over-truncation that drops domain nouns still present in A.
        if a and len(fixed) + 4 < len(a):
            keepers = ("空气炸锅", "蒜皮", "只需", "手套", "月饼", "豆干", "洗勺", "拧结")
            if any(k in a for k in keepers) and not any(k in fixed for k in keepers):
                print(f"  [correct] #{i} reject short drop-token keep A")
                fixed = rule_fix(a)
        out.append({"start": s["start"], "end": s["end"], "text": fixed})
        if fixed != rule_fix(a) and fixed != a:
            print(f"  [correct] #{i} {a} => {fixed}")
        else:
            print(f"  [correct] #{i} keep {fixed}")
    return out


def restore_truncated_from_baseline(
    segs: list[dict],
    baseline: list[dict],
) -> list[dict]:
    """If LLM left a proper prefix of baseline A, restore the full A cue."""
    out: list[dict] = []
    for s in segs:
        cur = rule_fix(s.get("text") or "")
        a = rule_fix(nearest_text(baseline, float(s["start"]), float(s["end"])))
        if is_hard_pollution(cur) and not is_hard_pollution(a):
            cur = a
        na = re.sub(r"\s+", "", a)
        nc = re.sub(r"\s+", "", cur)
        if a and nc and na != nc and na.startswith(nc) and len(na) >= len(nc) + 2:
            cur = a
        elif a and any(k in a and k not in cur for k in ("蒜皮", "空气炸锅", "只需", "油脆脆")):
            cur = a
        out.append({"start": s["start"], "end": s["end"], "text": cur})
    return out


def load_hotwords(text: str | None, path: Path | None) -> str | None:
    parts: list[str] = []
    if text:
        parts.append(text.strip())
    if path and path.exists():
        parts.append(path.read_text(encoding="utf-8").strip())
    joined = "\n".join(p for p in parts if p)
    return sanitize_hotwords(joined or None)


def save_raw(path: Path, raw: dict, meta: dict | None = None) -> None:
    payload = {"meta": meta or {}, "passes": raw}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_internal_fields(segs: list[dict]) -> list[dict]:
    keep = ("start", "end", "text")
    return [{k: s[k] for k in keep if k in s} for s in segs]
